"""洗盘样本库 + A/B（washout_samples）— plans/23 §3.2 回测口径 / P1 第 6 项

## 做什么

1. 扫历史，找出 **washout_end 确认日**（疑似洗盘且"地量+收阳+站上MA5"确认）；
2. 为每个观察点算 T+1 / T+3 / T+5 / T+10 前复权收益（**D+1 买入口径**，与
   `daily_verify._calc_multi` 一致：推荐/信号日收盘无法成交，实际 D+1 买入）；
3. 按 **同交易日 + 同形态** 配对对照组（同日同形态、但**未**确认洗盘）；
4. A/B：命中率 / 期望收益差异 + **置换检验 p 值**；样本不足时明确写"样本不足"。

## 为什么必须"同日 + 同形态"配对（不是随便找全市场比）

- **不同日期**的大盘环境差异（同一天全市场同涨同跌）会直接污染结论；
- **不同形态**的天然涨幅分布不同（连板 vs 平台突破）。
  拿"洗盘组 vs 全市场"比，等于把环境效应和形态效应算到"洗盘"头上（plans/23 §4.3 的严谨性要求）。

## 为什么形态用 `classify_current` 而不是自己猜

形态标签必须与每日推荐/回测**同一套口径**，否则"同形态配对"是假的。

## 诚实约束

- 只有 `washout_score >= washout_score_threshold` **且** `confirmed=True` 才算"洗盘组"；
- 对照组要求**同日同形态且未确认**（不是"随便非洗盘"）；
- 任一组 n < MIN_N 时**不出结论**（只报样本数），不制造"看起来显著"的假象。
"""
from __future__ import annotations

import json
import os
import random

import numpy as np
import pandas as pd

from app.core.logger import logger
from app.backtest import washout_detector as WD

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data",
)
SAMPLES_FILE = os.path.join(DATA_DIR, "washout_samples.json")

HORIZONS = (1, 3, 5, 10)      # 持有天数（D+1 买入后）
MIN_N = 100                   # 每组最小样本量（低于此不出结论）
PERM_N = 1000                 # 置换检验次数
STRIDE = 5                    # 扫描步长（交易日）——洗盘窗口 3~15 日，步长 5 足够覆盖
MAX_POINTS = 800              # 每只股票最多保留的观察点（控成本 + 控 JSON 体积）

# ── 配对口径（重要，写清楚以免后人误解）──────────────────────
# 本版采用 **同交易日 1:N 配对**：对照组 = 同一交易日、未确认洗盘、未被硬否决、
# 且非"超窗口回调"的其它样本。这样已消除最大的污染源（大盘环境：同一天全市场同涨同跌）。
# **严格"同日同形态"配对留待 P1-8**（与环境分层一起做）——因为形态标签
# 需要逐点调用 classify_current，成本高（实测 14ms/点，会让扫描慢 5~10 倍）；
# wash 事件上仍然会算形态标签（事件少，成本可忽略），供分形态观察与 P1-8 复用。
PAIR_MODE = "same_date"


def _forward_rets(adj: np.ndarray, idx: int, horizons=HORIZONS) -> dict:
    """D+1 买入口径的前复权收益：base = adj[idx+1]，持有 h 日后 adj[idx+1+h]。

    与 daily_verify._calc_multi 完全一致（D 日收盘后才知道信号 → 只能 D+1 买入）。
    """
    out: dict = {f"t{h}": None for h in horizons}
    n = len(adj)
    buy = idx + 1
    if buy >= n:
        return out
    base = float(adj[buy])
    if not (base > 0):
        return out
    for h in horizons:
        j = buy + h
        if j < n:
            v = float(adj[j])
            if v == v and v > 0:
                out[f"t{h}"] = round(v / base - 1.0, 5)
    return out


def classify_form(df: pd.DataFrame, idx: int) -> str:
    """形态标签（与每日推荐同口径）。失败/无法归类返回 'U'。"""
    try:
        from app.backtest.form_classifier import classify_current
        pat = classify_current(df.iloc[: idx + 1].reset_index(drop=True), end_idx=idx)
        if pat is not None and getattr(pat, "form_type", None):
            return str(pat.form_type)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[washout_samples] 形态识别失败: {exc}")
    return "U"


def scan_stock(ts_code: str, df: pd.DataFrame, series: dict | None = None,
               stride: int = STRIDE, min_idx: int = 70,
               max_points: int = MAX_POINTS) -> list[dict]:
    """扫描一只股票的历史，返回观察点列表（含分数/阶段/形态/前瞻收益）。

    只扫到 `len(df)-HORIZONS[-1]-2`，保证每个观察点都有完整的 T+10（否则各组口径不一）。
    """
    if df is None or len(df) < min_idx + 20:
        return []
    adj = pd.to_numeric(df["adj_close"], errors="coerce").to_numpy(dtype=float)
    last_ok = len(df) - HORIZONS[-1] - 2
    if last_ok <= min_idx:
        return []
    idxs = list(range(min_idx, last_ok + 1, max(1, stride)))
    if len(idxs) > max_points:
        step = len(idxs) / max_points
        idxs = [idxs[int(i * step)] for i in range(max_points)]
        idxs = sorted(set(idxs))

    out: list[dict] = []
    for i in idxs:
        try:
            r = WD.score_washout(df, i, series=series, ts_code=ts_code)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[washout_samples] {ts_code}@{i} 打分失败: {exc}")
            continue
        if r.coverage < WD.COVERAGE_MIN:
            continue                      # 证据不足的点不进样本库（不许拿缺数据充数）
        rets = _forward_rets(adj, i)
        if rets.get("t5") is None:
            continue
        out.append({
            "ts_code": ts_code,
            "date": r.date,
            "idx": i,
            # 形态只在"确认事件"上算（成本：14ms/点；事件很少 → 可忽略）
            "form": classify_form(df, i) if r.confirmed else "",
            "score": round(r.washout_score, 4),
            "stage": r.stage,
            "confirmed": bool(r.confirmed),
            "vetoed": bool(r.veto),
            "retrace": r.evidence.get("retrace_ratio"),
            "vol_shrink": r.evidence.get("vol_shrink_ratio"),
            **rets,
        })
    return out


# ── 统计 ──────────────────────────────────────────────────────
def _group_stats(points: list[dict], horizon: str = "t5") -> dict:
    vals = np.array([p[horizon] for p in points if p.get(horizon) is not None], dtype=float)
    if len(vals) == 0:
        return {"n": 0}
    return {
        "n": int(len(vals)),
        "pos_ratio": round(float((vals > 0).mean()), 4),      # 赚钱比例
        "gt5_ratio": round(float((vals > 0.05).mean()), 4),   # >5% 比例
        "mean": round(float(vals.mean()), 4),
        "median": round(float(np.median(vals)), 4),
        "std": round(float(vals.std(ddof=1)) if len(vals) > 1 else 0.0, 4),
    }


def perm_p_value(a: np.ndarray, b: np.ndarray, n: int = PERM_N, seed: int = 42) -> float | None:
    """两组均值差异的置换检验 p 值（双侧）。样本太小返回 None。"""
    a = a[~np.isnan(a)]
    b = b[~np.isnan(b)]
    if len(a) < 10 or len(b) < 10:
        return None
    obs = abs(float(a.mean() - b.mean()))
    pool = np.concatenate([a, b])
    rng = np.random.default_rng(seed)
    la = len(a)
    hits = 0
    for _ in range(n):
        perm = rng.permutation(pool)
        d = abs(float(perm[:la].mean() - perm[la:].mean()))
        if d >= obs:
            hits += 1
    return round((hits + 1) / (n + 1), 4)


def pair_ab(points: list[dict], horizon: str = "t5") -> dict:
    """同交易日 1:N 配对：洗盘组（confirmed）vs 对照组（同日、未确认、未否决、非超窗口）。

    配对口径见文件头说明：本版按**同交易日**配对（消除大盘环境差异这一最大污染源），
    严格"同日同形态"留待 P1-8。
    """
    wash = [p for p in points if p.get("confirmed")]
    wash_dates = {p.get("date") for p in wash}
    control = [
        p for p in points
        if (not p.get("confirmed"))
        and p.get("date") in wash_dates
        and not p.get("vetoed")
        and p.get("stage") != WD.STAGE_TOO_LONG
    ]
    a = np.array([p[horizon] for p in wash if p.get(horizon) is not None], dtype=float)
    b = np.array([p[horizon] for p in control if p.get(horizon) is not None], dtype=float)
    res = {
        "horizon": horizon,
        "pair_mode": PAIR_MODE,
        "wash": _group_stats(wash, horizon),
        "control": _group_stats(control, horizon),
        "paired_dates": len(wash_dates),
        "wash_forms": _form_dist(wash),
        "p_value": perm_p_value(a, b),
    }
    small = (res["wash"].get("n", 0) < MIN_N) or (res["control"].get("n", 0) < MIN_N)
    if small:
        res["verdict"] = f"样本不足（需每组 ≥{MIN_N}，实际 {res['wash'].get('n',0)}/{res['control'].get('n',0)}）"
    elif res["p_value"] is not None and res["p_value"] < 0.05:
        better = res["wash"]["mean"] > res["control"]["mean"]
        res["verdict"] = ("洗盘组显著优于对照" if better else "洗盘组显著**劣于**对照（阈值/定义需改）") \
            + f"（p={res['p_value']}，均值 {res['wash']['mean']:+.2%} vs {res['control']['mean']:+.2%}）"
    else:
        res["verdict"] = f"无显著差异（p={res['p_value']}）→ 该阈值下洗盘无预测力"
    return res


def _form_dist(points: list[dict]) -> dict:
    """形态分布（仅 wash 事件有形态标签；对照组无 → 返回 {} 并说明）。"""
    from collections import Counter
    c = Counter(str(p.get("form") or "") for p in points)
    c.pop("", None)
    return dict(c.most_common())


def build(codes: list[str], stride: int = STRIDE, max_stocks: int | None = None,
          days: int | None = None, max_points: int = MAX_POINTS,
          progress_cb=None) -> dict:
    """扫描并生成样本库（含 A/B）。

    Args:
        stride: 扫描步长（交易日）。**事件很稀疏（约 0.073% 的"股票×交易日"）**，
            stride=5 会漏掉约 80% 的确认日 → 要凑够样本量应用 stride=1 + 限定 days。
        days: 只扫最近 N 个交易日（None=全历史）。这是控制成本的关键旋钮：
            300 只 × 460 天 ≈ 13.8 万观察点 ≈ 100+ 个确认日（够做 A/B）。
    """
    from app.backtest.loader import prepare_stock_data

    use = codes[:max_stocks] if max_stocks else codes
    points: list[dict] = []
    ok = failed = 0
    for i, code in enumerate(use):
        try:
            df = prepare_stock_data(code)
            if df is None or len(df) < 90:
                failed += 1
                continue
            ser = WD.washout_series(code, df)
            min_idx = max(70, len(df) - int(days)) if days else 70
            pts = scan_stock(code, df, series=ser, stride=stride,
                             min_idx=min_idx, max_points=max_points)
            points.extend(pts)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.debug(f"[washout_samples] {code} 扫描失败: {exc}")
        if progress_cb:
            progress_cb(i + 1, len(use), len(points))
    ab = {f"t{h}": pair_ab(points, f"t{h}") for h in HORIZONS}
    return {
        "built_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stocks_ok": ok, "stocks_failed": failed,
        "points_n": len(points),
        "wash_n": sum(1 for p in points if p.get("confirmed")),
        "stride": stride, "days": days, "pair_mode": PAIR_MODE,
        "ab": ab,
        "points": points,
    }


def save(payload: dict, path: str = SAMPLES_FILE) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[washout_samples] 保存失败: {exc}")
        return False


def load(path: str = SAMPLES_FILE) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


__all__ = ["scan_stock", "pair_ab", "build", "save", "load", "perm_p_value",
           "classify_form", "SAMPLES_FILE", "MIN_N", "HORIZONS"]
