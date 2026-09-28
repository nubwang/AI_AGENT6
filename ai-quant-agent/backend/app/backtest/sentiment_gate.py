"""情绪择时闸门（sentiment_gate）— plans/24 人心博弈层 H2

## 要回答的可证伪问题

**"按情绪状态决定该不该出手，能不能改善结果？"**

具体化（plans/24 §3.4 的"隔岸观火 / 走为上"）：
    - 情绪过热（炸板率飙升 / 赚钱效应转负 / 连板高度下台阶）→ 若前向收益显著为负 ⇒ 该空仓
    - 情绪冰点（涨停稀少 / 赚钱效应极低）→ 若前向收益显著为负 ⇒ 也该空仓（"不接飞刀"）
    - 反之若冰点后收益显著为正 ⇒ **冰点反而是机会**（与"隔岸观火"相反，必须用数据裁决）

## ⚠️ 为什么用"全市场日度面板"，而不是现有的推荐样本

plans/23 §15.4 的教训：`entry_guidance.avg_ret_t5` 是"成功组 × 最佳策略"的**事后取优**口径，
把 355 条推荐样本的期望从实测 **−5.55%** 说成了 **+13.87%**。
**任何用"已被筛选过的样本"做的择时结论都不可信**（选择偏差）。

因此本模块用**全市场等权面板**：每个交易日，把**所有** A 股的前向收益取等权平均。
样本量 ≈ 659 天 × 5000 只，且没有任何筛选 → **无选择偏差**。

## ★ 天然无未来函数（本模块最重要的性质）

前向收益定义为「**T+1 开盘买入**，持有 h 个交易日，T+1+h 开盘卖出」：

    fwd_h(T) = open(T+1+h) / open(T+1) − 1

于是：
    - 情绪读数 = T 日**收盘后**可知（sentiment.py 已保证）；
    - 收益 = 从 T+1 **开盘**开始；
    ⇒ 二者配对**天然不存在未来函数**，无需额外错位。

（用 T+1 开盘而非 T 收盘，是因为 T 日收盘的情绪在 T 日收盘那一刻才知道，
 不可能以 T 日收盘价成交 —— 那是"看着收盘价下单"的经典未来函数。）

## 判据（提前声明，避免事后找解释）

对每个情绪指标分 5 组（等频），比较各组的平均前向收益：

  - **统计显著**：极端组 vs 其余组的 Welch t 检验 `p < 0.05`；
  - **有实盘意义**：|极端组均值 − 全局均值| ≥ `MIN_EDGE`（默认 0.5%，
    因为一个交易回合的双边成本约 0.2~0.3%，低于此的"优势"会被成本吃掉）；
  - 两者同时满足才算"**有依据**"，才允许考虑启用闸门。

## 用法

    cd ai-quant-agent/backend && ./venv/bin/python -m app.backtest.sentiment_gate
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.backtest import sentiment as ST
from app.backtest.stats_correction import bh_fdr, newey_west_reg_t
from app.core.logger import logger
from app.models import SessionLocal

GATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "sentiment_gate.json",
)
# ★ 复核用原始逐日数据（plans/24 §11.28 A3）：`GATE_FILE` 只有汇总 p / 优势，
#   做 HAC 复核必须拿到「情绪指标逐日值 + fwd 逐日值」⇒ **另存**，不动 GATE_FILE 结构。
GATE_SERIES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "sentiment_gate_series.json",
)

HORIZONS = (1, 5, 10)
MIN_EDGE = 0.005          # 有实盘意义的最小优势（0.5%）
ALPHA = 0.05              # 显著性水平
MAX_ABS_FWD = 1.5         # 剔除 |前向收益| > 150% 的异常（新股/数据问题）
RECENT_N = 30             # "近期窗口"长度：用于与推荐样本做**同期**对照

# 参与检验的情绪指标（全部来自 sentiment_daily）
FEATURES = ("seal_ratio", "zha_rate", "money_effect", "max_board", "n_up",
            "n_lb2", "main_net", "up_down_ratio", "n_yizi")


def _welch(a: np.ndarray, b: np.ndarray) -> dict:
    """Welch t 检验（不假设等方差）。样本不足或退化返回 p=None。"""
    a = np.asarray([x for x in a if np.isfinite(x)], dtype="float64")
    b = np.asarray([x for x in b if np.isfinite(x)], dtype="float64")
    if len(a) < 5 or len(b) < 5:
        return {"t": None, "p": None, "n_a": len(a), "n_b": len(b),
                "mean_a": None, "mean_b": None, "degenerate": True}
    ma, mb = float(a.mean()), float(b.mean())
    va, vb = float(a.var(ddof=1)), float(b.var(ddof=1))
    se = np.sqrt(va / len(a) + vb / len(b))
    if not (se > 1e-12):
        return {"t": 0.0, "p": 1.0, "n_a": len(a), "n_b": len(b),
                "mean_a": ma, "mean_b": mb, "degenerate": True}
    t = (ma - mb) / se
    # Welch–Satterthwaite 自由度 + t 分布 CDF（用标准库 math，避免额外依赖）
    import math
    df = (va / len(a) + vb / len(b)) ** 2 / (
        (va / len(a)) ** 2 / (len(a) - 1) + (vb / len(b)) ** 2 / (len(b) - 1))
    # 用正态近似求双侧 p（n≥100 时与 t 分布几乎一致）
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return {"t": round(float(t), 4), "p": round(float(p), 6), "df": round(float(df), 1),
            "n_a": len(a), "n_b": len(b), "mean_a": round(ma, 5), "mean_b": round(mb, 5),
            "degenerate": False}


def build_panel(horizons: tuple[int, ...] = HORIZONS, d0: str = "",
                d1: str = "", keep_stock: bool = False) -> pd.DataFrame:
    """全市场前向收益面板（T+1 开盘买入）。

    Args:
        keep_stock: False（默认）→ 按日聚合的等权均值面板
                    （index = trade_date，columns = fwd1/fwd5/fwd10）；
                    True → **个股级**明细（columns = trade_date/ts_code/fwd*），
                    供个股特征检验复用**同一份口径**（plans/24 H3 用）。
                    默认 False 时行为与原来完全一致。
    """
    sql = ("SELECT trade_date, ts_code, open FROM daily "
           "WHERE trade_date >= :a AND trade_date <= :b")
    parts: list[pd.DataFrame] = []
    with SessionLocal() as db:
        conn = db.connection()
        for a, b in ST._year_spans(d0, d1):
            df = pd.read_sql(text(sql), conn, params={"a": a, "b": b})
            if df is not None and not df.empty:
                parts.append(df)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "", regex=False)
    df["open"] = pd.to_numeric(df["open"], errors="coerce")
    df = df[(df["open"] > 0)].dropna(subset=["open"])
    # ★ 关键：按股票排序后做 shift，得到"T+1 开盘"与"T+1+h 开盘"
    df = df.sort_values(["ts_code", "trade_date"], kind="mergesort")
    g = df.groupby("ts_code", sort=False)["open"]
    entry = g.shift(-1)                       # T+1 开盘（买入价）
    for h in horizons:
        exit_ = g.shift(-(1 + h))             # T+1+h 开盘（卖出价）
        col = f"fwd{h}"
        df[col] = exit_ / entry - 1.0
        df.loc[df[col].abs() > MAX_ABS_FWD, col] = np.nan
    cols = [f"fwd{h}" for h in horizons]
    if keep_stock:
        # 个股级明细：下游（game_features）按 (trade_date, ts_code) 合并特征。
        # 口径与聚合模式**完全同源**（同一段代码算出的 fwd）。
        out = df[["trade_date", "ts_code"] + cols].copy()
        out["trade_date"] = out["trade_date"].astype(str)
        out["ts_code"] = out["ts_code"].astype(str)
        return out.reset_index(drop=True)
    panel = df.groupby("trade_date")[cols].mean()
    panel.index = panel.index.astype(str)
    return panel


def _labels(vals: pd.Series, k: int = 5) -> pd.Series:
    """等频分 k 组（0..k-1）；重复值多时分位可能重合 → 用 rank 兜底。"""
    v = pd.to_numeric(vals, errors="coerce")
    if v.notna().sum() < k * 10:
        return pd.Series([np.nan] * len(v), index=v.index)
    try:
        lab = pd.qcut(v, k, labels=False, duplicates="drop")
    except Exception:  # noqa: BLE001
        lab = pd.Series([np.nan] * len(v), index=v.index)
    return lab


def analyze_feature(feat: str, sent: dict, panel: pd.DataFrame,
                    horizon: int = 5, k: int = 5) -> dict:
    """单指标分析：分 k 组比前向收益，检验"极端组 vs 其余"。"""
    col = f"fwd{horizon}"
    rows = []
    for day, v in sent.items():
        if day in panel.index and v.get(feat) is not None:
            fv_raw = panel.at[day, col]                  # type: ignore[arg-type]
            try:
                fv = float(fv_raw)                       # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if np.isfinite(fv):
                rows.append((day, float(v[feat]), fv))
    if len(rows) < 60:
        return {"feature": feat, "horizon": horizon, "n": len(rows), "verdict": "样本不足"}
    d = pd.DataFrame(rows, columns=["day", "x", "fwd"])
    d["q"] = _labels(d["x"], k)
    d = d.dropna(subset=["q"])
    if d.empty:
        return {"feature": feat, "horizon": horizon, "n": len(rows), "verdict": "分组失败"}

    groups = []
    for gq in sorted(d["q"].unique()):
        sub = d[d["q"] == gq]
        groups.append({"q": int(gq), "n": int(len(sub)),
                       "x_mean": round(float(sub["x"].mean()), 4),
                       "fwd_mean": round(float(sub["fwd"].mean()), 5)})
    overall = float(d["fwd"].mean())

    # 极端组 = q 最小 / q 最大
    lo_q, hi_q = int(d["q"].min()), int(d["q"].max())
    lo = d[d["q"] == lo_q]["fwd"].to_numpy()
    hi = d[d["q"] == hi_q]["fwd"].to_numpy()
    mid = d[(d["q"] > lo_q) & (d["q"] < hi_q)]["fwd"].to_numpy()
    t_lo = _welch(lo, mid)
    t_hi = _welch(hi, mid)

    def _judge(t: dict, side: str) -> dict:
        if not t.get("p"):
            return {"side": side, "significant": False, "edge": None, "verdict": "样本不足"}
        edge = float(t["mean_a"]) - float(t["mean_b"])
        sig = t["p"] < ALPHA
        meaningful = abs(edge) >= MIN_EDGE
        ok = bool(sig and meaningful)
        return {"side": side, "significant": bool(sig), "edge": round(edge, 5),
                "p": t["p"], "n": t["n_a"], "verdict": "有依据" if ok else "无依据",
                "reason": ("显著且有幅度" if ok else
                           ("显著但幅度不足（会被成本吃掉）" if sig and not meaningful
                            else "不显著"))}

    j_lo, j_hi = _judge(t_lo, "低值组"), _judge(t_hi, "高值组")
    # ★ HAC 复核（plans/24 §11.28 A3）：本检验是「跨日两组比较」，而 fwd_h 本身重叠
    #   ⇒ 两组均值差的普通检验偏乐观 ⇒ 改用**回归系数**的 HAC 标准误复核。
    #   子样本 = 「极端组 ∪ 中段组」并**保持时间顺序**（HAC 依赖时间序）；D = 1 为极端组。
    d = d.reset_index(drop=True)
    mid_mask = ((d["q"] > lo_q) & (d["q"] < hi_q)).to_numpy()
    hac_lag = max(0, horizon - 1)

    def _pair(is_extreme: np.ndarray) -> tuple[float, float]:
        sel = is_extreme | mid_mask
        sub = d.loc[sel]
        rr = newey_west_reg_t(sub["fwd"].to_numpy(dtype="float64"),
                              is_extreme[sel].astype("float64"), hac_lag)
        return float(rr["t_naive"]), float(rr["t_hac"])

    _tn_lo, _th_lo = _pair((d["q"] == lo_q).to_numpy())
    _tn_hi, _th_hi = _pair((d["q"] == hi_q).to_numpy())
    for _jd, _tn, _th in ((j_lo, _tn_lo, _th_lo), (j_hi, _tn_hi, _th_hi)):
        _jd["t_naive"] = None if _tn != _tn else round(_tn, 3)
        _jd["t_hac"] = None if _th != _th else round(_th, 3)
        _jd["hac_lag"] = hac_lag
        _jd["hac_note"] = ("t_hac = Newey-West 回归系数 t（仅披露）；"
                           "t_naive 为同方差 OLS（合并两样本口径）")
    verdict = "有依据" if (j_lo["verdict"] == "有依据" or j_hi["verdict"] == "有依据") else "无依据"
    return {
        "feature": feat, "horizon": horizon, "n": int(len(d)),
        "overall_fwd": round(overall, 5),
        "groups": groups,
        "low": j_lo, "high": j_hi,
        "spread_hi_minus_lo": round(float(hi.mean() - lo.mean()), 5),
        "verdict": verdict,
    }


def gate_report(horizon: int = 5, save: bool = True) -> dict:
    """对全部候选指标做检验，输出"闸门是否值得做"的结论。"""
    t0 = time.time()
    sent_raw = ST.load_sentiment()
    if not sent_raw or not sent_raw.get("days"):
        return {"ok": False, "msg": "缺少 data/sentiment_daily.json，请先构建情绪日线"}
    days = sent_raw["days"]
    rng = sent_raw.get("range") or [min(days), max(days)]

    panel = build_panel(HORIZONS, rng[0], rng[1])
    if panel.empty:
        return {"ok": False, "msg": "前向收益面板为空"}

    common = [d for d in days if d in panel.index]

    # ── 1) 收集**所有**检验（9 指标 × 3 期限 × 2 侧）──
    results = []
    for feat in FEATURES:
        results.append(analyze_feature(feat, days, panel, horizon=horizon))

    tests: list[dict] = []
    for h in HORIZONS:
        for feat in FEATURES:
            r = analyze_feature(feat, days, panel, horizon=h)
            for side, key in (("low", "low"), ("high", "high")):
                t = r.get(key) or {}
                if t.get("p") is not None:
                    tests.append({
                        "feature": feat, "horizon": h, "side": side,
                        "p": float(t["p"]), "edge": t.get("edge"),
                        "n": t.get("n"), "q": None, "fdr_pass": False,
                        "meaningful": False, "usable": False,
                        # ★ HAC 披露（§11.28 A3）
                        "t_naive": t.get("t_naive"), "t_hac": t.get("t_hac"),
                        "hac_lag": t.get("hac_lag"), "hac_pass": False,
                    })

    # ── 2) ★ 多重检验校正（**必须做**：不校正就报结论 = 自欺）──
    fdr = bh_fdr([t["p"] for t in tests], alpha=ALPHA)
    q_map = dict(zip(fdr.get("idx") or [], fdr.get("q") or []))
    for i, t in enumerate(tests):
        q = q_map.get(i)
        t["q"] = q
        t["fdr_pass"] = bool(q is not None and q <= ALPHA)
        t["meaningful"] = (t.get("edge") is not None
                           and abs(float(t["edge"])) >= MIN_EDGE)
        t["usable"] = bool(t["fdr_pass"] and t["meaningful"])
        _th = t.get("t_hac")
        t["hac_pass"] = bool(_th is not None and abs(float(_th)) >= 2.0)
    usable = [t for t in tests if t["usable"]]
    raw_sig = [t for t in tests if t["p"] < ALPHA and t["meaningful"]]

    # 打印：主期限逐指标 + 校正对比
    for r in results:
        mark = "✅" if r.get("verdict") == "有依据" else "  "
        print(f"  {mark} {r.get('feature'):<14} n={r.get('n')} "
              f"全样本 fwd{horizon}={r.get('overall_fwd')} "
              f"低组 {r.get('low', {}).get('verdict')} "
              f"高组 {r.get('high', {}).get('verdict')}")
    print(f"  ── 未校正显著（且|优势|>={MIN_EDGE:.1%}）：{len(raw_sig)} 个"
          f" → BH-FDR 校正后剩 {len(usable)} 个（m={fdr.get('m')}）")
    out = {
        "ok": True,
        "range": rng,
        "n_days_with_panel": len(common),
        "horizon_main": horizon,
        "min_edge": MIN_EDGE,
        "alpha": ALPHA,
        "overall_market_fwd": {f"fwd{h}": round(float(panel[f"fwd{h}"].mean()), 5)
                               for h in HORIZONS},
        "results": results,
        "fdr": {"method": fdr.get("method"), "alpha": ALPHA, "m": fdr.get("m"),
                "n_reject_raw": len(raw_sig), "n_reject_q": len(usable),
                "threshold": fdr.get("threshold")},
        "raw_significant": raw_sig,
        "usable_tests": usable,
        "n_usable": len(usable),
        # ★ HAC 口径披露（§11.28 A3）：判定仍用 Welch p（与 `t_naive` 同方向）
        "t_basis": ("`t_naive` = 同方差 OLS 回归系数 t（合并两样本口径，**与判定同方向**）；"
                    "`t_hac` = Newey-West 回归系数 t（lag = 持有期 − 1，**仅披露、不参与判定**）"),
        "n_hac_robust": int(sum(1 for t in tests if t.get("hac_pass"))),
        "verdict": ("存在可用情绪闸门" if usable else "无可采纳的情绪闸门（FDR 校正后无依据）"),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "note": ("全市场等权面板，T+1 开盘买入；情绪与收益天然无未来函数。"
                 f"判据：**BH-FDR 校正后** q<{ALPHA} 且 |优势|>={MIN_EDGE:.1%}"),
    }
    # ★ 近期窗口：与"推荐样本"（只覆盖最近 ~1.5 个月）做**同期**对照。
    #   不这样做就会犯"拿 2.6 年均值对比 1.5 个月样本"的错误（两者不可比）。
    recent = panel.tail(RECENT_N)
    out["recent_window"] = {
        "n": int(len(recent)),
        "from": str(recent.index.min()) if len(recent) else "",
        "to": str(recent.index.max()) if len(recent) else "",
        **{f"fwd{h}": round(float(recent[f"fwd{h}"].mean()), 5) for h in HORIZONS},
    }
    # ★ 复核用序列**另存**（plans/24 §11.28 A3）：情绪指标的逐日值 + fwd 逐日值。
    #   复核脚本据此**独立重算**，而不是抄 GATE_FILE 里的汇总数字。
    ser = {"range": [str(rng[0]), str(rng[1])], "horizons": list(HORIZONS),
           "min_edge": MIN_EDGE, "x": {}, "fwd": {}}
    for feat in FEATURES:
        ser["x"][feat] = {str(k): float(v[feat]) for k, v in days.items()
                          if isinstance(v, dict) and v.get(feat) is not None}
    for h in HORIZONS:
        col = f"fwd{h}"
        ser["fwd"][col] = {str(k): float(v) for k, v in panel[col].items() if v == v}
    if save:
        try:
            os.makedirs(os.path.dirname(GATE_FILE), exist_ok=True)
            with open(GATE_FILE, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[gate] 落盘失败：{exc}")
        try:
            with open(GATE_SERIES_FILE, "w", encoding="utf-8") as f:
                json.dump(ser, f, ensure_ascii=False, indent=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[gate] 序列落盘失败：{exc}")
    logger.info(f"[gate] 完成：{out['verdict']}（FDR 后可用 {len(usable)} 个），"
                f"耗时 {out['elapsed_sec']}s")
    return out


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="情绪择时闸门检验（plans/24 H2）")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    print("=" * 88)
    print("情绪择时闸门检验（plans/24 H2）—— 全市场等权面板")
    print("=" * 88)
    r = gate_report(horizon=args.horizon, save=not args.no_save)
    if not r.get("ok"):
        print(f"失败：{r.get('msg')}")
        return 1
    print()
    print(f"面板覆盖 {r['n_days_with_panel']} 个交易日：{r['range'][0]} ~ {r['range'][1]}")
    print(f"全市场等权前向收益均值（全区间）："
          + " ｜ ".join(f"{k}={v:.3%}" for k, v in r["overall_market_fwd"].items()))
    rw = r.get("recent_window") or {}
    if rw:
        print(f"全市场等权前向收益均值（近 {rw.get('n')} 日 {rw.get('from')}~{rw.get('to')}，"
              f"与推荐样本同期）："
              + " ｜ ".join(f"{k}={rw[k]:.3%}" for k in rw if k.startswith("fwd")))
        print("  ⚠️ 与推荐样本的对照必须用**同期**数字："
              "plans/23 §15.6 实测推荐样本 D+1 开盘基准 T+5 期望 = −5.55%")
    print()
    print(f"  多重检验：m={r['fdr']['m']}，未校正显著 {r['fdr']['n_reject_raw']} 个"
          f" → BH-FDR 校正后 {r['fdr']['n_reject_q']} 个")
    if r.get("raw_significant"):
        print("  未校正显著明细（供对照，**不作为依据**）：")
        for x in r["raw_significant"]:
            print(f"    · fwd{x['horizon']} {x['feature']} {x['side']}组 "
                  f"edge={x['edge']} p={x['p']} q={x['q']}")
    if r.get("usable_tests"):
        print("  ★ 校正后可用（这是唯一能当依据的）：")
        for x in r["usable_tests"]:
            print(f"    · fwd{x['horizon']} {x['feature']} {x['side']}组 "
                  f"edge={x['edge']} q={x['q']} n={x['n']}")
    print()
    print(f"结论：{r['verdict']}（可用 {r['n_usable']} 个）")
    print(f"判据：{r['note']}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
