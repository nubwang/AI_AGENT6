"""环境分层（regime_split）— plans/23 §3.1 ②择时 / P1 第 8 项

## 为什么需要它

规划把"择时"列为六段式的 ②，缺口是：**同一形态在强/震/弱三种环境下的胜率差异没人统计过**。
P1-6 的洗盘 A/B 给出了一个否证结论（四周期均不显著），其中一条待验证原因是
**"环境混杂"**：牛市里"洗盘"遍地、熊市里"洗盘"多失败 —— 混在一起算，自然看不出差异。

本模块提供：
  1. **环境三态判定**（与规划口径一致）：强势 = 指数 20 日动量 > +3% **且**站上 60 日线；
     弱势 = 20 日动量 < -3%；其余为震荡。
  2. **按环境分层重跑任意 A/B 或分层统计**（复用 `washout_samples.pair_ab` 的配对与置换检验）。

## ⚠️ 无未来函数（硬约束）

`regime_of(date)` **只用该日期及之前**的指数数据（`<= date`），
与"当日收盘后才知道环境"的实盘时序一致。绝不使用之后的数据。

## 数据来源

复用 [`failure_context._load_index_panel()`](ai-quant-agent/backend/app/agents/failure_context.py:1)
的指数面板（3 个指数、落盘缓存 12h），避免重复全表扫 2800 万行的 `index_daily`。
"""
from __future__ import annotations

import json
import os

import numpy as np

from app.core.logger import logger

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data",
)
REGIME_FILE = os.path.join(DATA_DIR, "regime_series.json")

BENCH_CODE = "000001.SH"     # 上证综指为环境基准（与 extra_feature_loader._market_env 一致）
MOM_DAYS = 20                # 动量窗口（规划：20 日动量）
MA_DAYS = 60                 # 均线（规划：站上 60 日线）
STRONG = 0.03                # 强势阈值（+3%）
WEAK = -0.03                 # 弱势阈值（-3%）

R_STRONG = "强势"
R_MIXED = "震荡"
R_WEAK = "弱势"
REGIMES = (R_STRONG, R_MIXED, R_WEAK)


def _norm_date(v) -> str:
    s = str(v or "").strip().split(" ")[0].replace("-", "")
    return s if len(s) == 8 and s.isdigit() else ""


def load_panel(force: bool = False) -> dict:
    """指数面板 {code: [[date, close], ...]}（复用 failure_context 的缓存）。"""
    try:
        from app.agents.failure_context import _load_index_panel
        return _load_index_panel(force=force) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[regime_split] 指数面板加载失败: {exc}")
        return {}


def regime_of(date: str, panel: dict | None = None, code: str = BENCH_CODE) -> dict:
    """某交易日的环境三态（**只用 <= date 的指数数据**）。

    Returns: {"date","regime","mom20","above_ma60","close","ma60","ok"}
      - `ok=False` 表示指数数据不足/缺失 → regime="震荡" 且 `missing=True`（不硬判）
    """
    out = {"date": _norm_date(date), "regime": R_MIXED, "mom20": None,
           "above_ma60": None, "close": None, "ma60": None, "ok": False, "missing": False}
    d = out["date"]
    if not d:
        out["missing"] = True
        return out
    panel = panel if panel is not None else load_panel()
    rows = panel.get(code) or []
    if not rows:
        out["missing"] = True
        return out
    dates = [r[0] for r in rows]
    hi = 0
    lo, h = 0, len(dates)
    while lo < h:                       # 二分：最后一个 <= d 的位置 + 1
        mid = (lo + h) // 2
        if dates[mid] <= d:
            lo = mid + 1
        else:
            h = mid
    hi = lo
    if hi < MA_DAYS:
        out["missing"] = True
        return out
    closes = [float(r[1]) for r in rows[max(0, hi - MA_DAYS): hi]]
    cur = closes[-1]
    ma60 = float(np.mean(closes)) if closes else float("nan")
    mom = cur / float(rows[hi - 1 - MOM_DAYS][1]) - 1.0 if hi - 1 - MOM_DAYS >= 0 else float("nan")
    above = bool(cur > ma60) if ma60 == ma60 else None
    out.update({"close": round(cur, 4), "ma60": None if ma60 != ma60 else round(ma60, 4),
                "mom20": None if mom != mom else round(mom, 4), "above_ma60": above, "ok": True})
    if mom == mom and mom >= STRONG and above:
        out["regime"] = R_STRONG          # 强势：动量 >+3% 且站上 60 日线（必须同时满足）
    elif mom == mom and mom <= WEAK:
        out["regime"] = R_WEAK
    else:
        out["regime"] = R_MIXED
    return out


def classify_series(dates: list[str], panel: dict | None = None) -> dict:
    """批量判定（同一 panel 复用一次，避免重复加载）。返回 {date: regime}。"""
    p = panel if panel is not None else load_panel()
    return {_norm_date(d): regime_of(d, p)["regime"] for d in dates if _norm_date(d)}


def regime_map_for_points(points: list[dict], panel: dict | None = None,
                          cache_file: str = REGIME_FILE) -> dict:
    """给观察点批量打环境标签（带落盘缓存，避免每次重算）。

    Returns: {date: {"regime","mom20","above_ma60"}}
    """
    p = panel if panel is not None else load_panel()
    cache: dict = {}
    if cache_file and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f) or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[regime_split] 环境缓存读取失败: {exc}")
    dates = sorted({_norm_date(x.get("date")) for x in points if _norm_date(x.get("date"))})
    todo = [d for d in dates if d not in cache]
    for d in todo:
        cache[d] = regime_of(d, p)
    if todo and cache_file:
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[regime_split] 环境缓存写入失败: {exc}")
    return cache


def stratify_ab(points: list[dict], horizons: tuple[str, ...] = ("t1", "t3", "t5", "t10"),
                cache_file: str = REGIME_FILE) -> dict:
    """按环境三态分别重跑 A/B（洗盘 vs 同日未确认对照）。

    口径与 `washout_samples.pair_ab` 完全一致，只是**先把样本按环境切开**——
    这样"环境混杂"这条原因就能被证实或排除。
    """
    from app.backtest import washout_samples as WS

    rmap = regime_map_for_points(points, cache_file=cache_file)
    out: dict = {"by_regime": {}, "counts": {}}
    for r in REGIMES:
        sub = [p for p in points if (rmap.get(_norm_date(p.get("date"))) or {}).get("regime") == r]
        out["counts"][r] = {
            "points": len(sub),
            "wash": sum(1 for p in sub if p.get("confirmed")),
        }
        out["by_regime"][r] = {h: WS.pair_ab(sub, h) for h in horizons}
    return out


def form_leaderboard(points: list[dict], min_n: int = 60, cache_file: str = REGIME_FILE) -> dict:
    """分环境 × 形态排行榜（要求 points 带 `form` 字段）。

    输出每个 (环境, 形态) 的样本量/赚钱率/>5%率/均值，并按 **同环境的全体均值** 算超额
    （避免"牛市里什么都赚"被误读成"某形态特别强"）。
    """
    rmap = regime_map_for_points(points, cache_file=cache_file)
    board: dict = {}
    for r in REGIMES:
        sub = [p for p in points
               if (rmap.get(_norm_date(p.get("date"))) or {}).get("regime") == r
               and p.get("form") and p.get("t5") is not None]
        if not sub:
            continue
        base = float(np.mean([p["t5"] for p in sub]))
        forms: dict = {}
        for f in sorted({str(p["form"]) for p in sub}):
            g = [p for p in sub if str(p["form"]) == f]
            if len(g) < min_n:
                forms[f] = {"n": len(g), "note": f"样本不足（<{min_n}）"}
                continue
            vals = np.array([p["t5"] for p in g], dtype=float)
            forms[f] = {
                "n": int(len(vals)),
                "pos_ratio": round(float((vals > 0).mean()), 4),
                "gt5_ratio": round(float((vals > 0.05).mean()), 4),
                "mean": round(float(vals.mean()), 4),
                "excess_vs_regime": round(float(vals.mean() - base), 4),
            }
        board[r] = {"baseline_mean": round(base, 4), "n": len(sub), "forms": forms}
    return board


__all__ = ["regime_of", "classify_series", "regime_map_for_points", "stratify_ab",
           "form_leaderboard", "load_panel", "REGIMES", "R_STRONG", "R_MIXED", "R_WEAK",
           "REGIME_FILE"]
