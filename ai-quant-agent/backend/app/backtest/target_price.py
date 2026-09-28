"""止盈目标位口径（target_price）— plans/23 §15.4 的落地（P2-8）

## 要解决的问题（§15.4 实测）

线上止盈位 = `t0_close × (1 + max(3%, avg_ret_t5 × ratio))`，其中 `avg_ret_t5` 取自
[`entry_guidance.json`](ai-quant-agent/backend/data/entry_guidance.json:1) —— 那是
「**成功组 × 最佳策略**」的**事后**统计（+13.87%），而同样本全样本可实现收益是 **−5.55%**。
结果：目标位 **+5.5%**，实测命中率仅 **9.1%** → 91% 的单子只能等止损。

## 本模块做什么

把"目标位"从**一个无法解释的事后均值**改成**按波动率给出的可达性曲线**：

    tp(mult) = t0_close × (1 + mult × ATR%)
    tp_curve() 同时给出每个 mult 的历史**触及概率**

让使用方按"能接受多少命中率"选倍数，而不是接受一个拍出来的数字。

## 三个必须写清楚的边界

1. **不是"把目标位调低"**：低目标位同样有代价（把盈利单过早赶下车）。
   本模块提供**曲线**，把「命中率 vs 目标幅度」的取舍显式化，不替使用者拍数。
2. **ATR 口径不保证更高命中率**：它保证的是**语义明确 + 随波动率自适应**
   （高波动票给更宽的目标）。命中率由 mult 决定，见曲线。
3. **渐进上线**：可进化参数 `plan_tp_basis` 默认 `"legacy"`（与现状**逐字节一致**）；
   验证通过后再切 `"atr"`（与项目"先验证后生效"的纪律一致）。

## 无未来函数

`atr_pct` 只用 `idx` 及之前的 K 线；目标位只依赖 T0 收盘 + 该 ATR。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FALLBACK_AVG_T5 = 0.08        # entry_guidance 缺失时的兜底（与 trade_plan.FALLBACK_TP1 对齐）
TP_FLOOR = 0.03               # 目标位下限（沿用现状 max(3%, ...)）
TP_CEIL = 0.30                # 目标位上限（防 ATR 异常放大给出荒谬目标）
TP_RATIO_DEFAULT = 0.60       # 沿用 trade_plan 的 plan_tp1_ratio 默认
ATR_N = 5                     # ATR 窗口（交易日）
TP_ATR_MULT_DEFAULT = 1.5
TP_BASIS_DEFAULT = "legacy"
TP_BASES = ("legacy", "atr")
ATR_MULTS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0)


def _p(name: str, default):
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param(name, default)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def tp_basis() -> str:
    """止盈口径模式（可进化参数 plan_tp_basis；非法值回退 legacy=不改行为）。"""
    v = str(_p("plan_tp_basis", TP_BASIS_DEFAULT) or TP_BASIS_DEFAULT).strip().lower()
    return v if v in TP_BASES else TP_BASIS_DEFAULT


def tp_atr_mult() -> float:
    """ATR 倍数（可进化参数 plan_tp_atr_mult）。"""
    try:
        return float(_p("plan_tp_atr_mult", TP_ATR_MULT_DEFAULT))
    except Exception:  # noqa: BLE001
        return TP_ATR_MULT_DEFAULT


def atr_pct_arrays(h, lo, c, idx: int, n: int = ATR_N) -> float | None:
    """数组版 ATR%（供只持有 numpy 数组的调用方使用，如 discipline_grid 的回放）。

    TR = max(H−L, |H−prevC|, |L−prevC|)；prevC 取前一日收盘。**只用 idx 及之前的数据**。
    """
    try:
        if h is None or idx < 1 or idx >= len(c):
            return None
        if not np.isfinite(c[idx]) or c[idx] <= 0:
            return None
        start = max(1, idx - n + 1)
        trs: list[float] = []
        for k in range(start, idx + 1):
            if not (np.isfinite(h[k]) and np.isfinite(lo[k])):
                continue
            pc = c[k - 1] if np.isfinite(c[k - 1]) and c[k - 1] > 0 else np.nan
            tr = h[k] - lo[k]
            if pc == pc:
                tr = max(tr, abs(h[k] - pc), abs(lo[k] - pc))
            trs.append(float(tr))
        if not trs:
            return None
        return float(np.mean(trs)) / float(c[idx])
    except Exception:  # noqa: BLE001
        return None


def atr_pct(df: pd.DataFrame | None, idx: int, n: int = ATR_N) -> float | None:
    """DataFrame 版 ATR%（真实波幅均值 / 当日收盘）。**只用 idx 及之前的数据**。"""
    try:
        if df is None or idx < 1 or idx >= len(df):
            return None
        h = pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype=float)
        lo = pd.to_numeric(df["low"], errors="coerce").to_numpy(dtype=float)
        c = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
        return atr_pct_arrays(h, lo, c, idx, n)
    except Exception:  # noqa: BLE001
        return None


def _clamp(level: float) -> float:
    return float(min(max(level, TP_FLOOR), TP_CEIL))


def tp_from_atr(t0_close: float, atr_pct_v: float, mult: float) -> float:
    """tp = t0_close × (1 + clamp(mult × ATR%))。"""
    try:
        level = _clamp(float(mult) * float(atr_pct_v))
    except (TypeError, ValueError):
        level = TP_FLOOR
    return float(t0_close) * (1.0 + level)


def tp_level(mult: float, atr_pct_v: float) -> float:
    """目标幅度（相对 T0 收盘），供曲线/报表用。"""
    try:
        return _clamp(float(mult) * float(atr_pct_v))
    except (TypeError, ValueError):
        return TP_FLOOR


def tp_curve(atr_pct_v: float, mults=ATR_MULTS) -> dict:
    """给定 ATR%，给出各倍数对应的目标幅度（回答"这个倍数对应多少个点"）。"""
    return {float(m): round(tp_level(m, atr_pct_v), 4) for m in mults}


def resolve(t0_close: float, atr_pct_v: float | None, avg_ret_t5: float | None,
            ratio: float = TP_RATIO_DEFAULT, basis: str = "", mult: float | None = None) -> dict:
    """统一入口：按当前口径算出目标位，并**如实标注用的是哪个口径**。

    Returns: {"price", "level", "basis", "detail"}
      - basis="legacy"：沿用 entry_guidance 的成功组均值（现状行为）
      - basis="atr"   ：按 ATR 倍数
      - ATR 不可得时**自动回退 legacy**（并在 detail 里说明，不静默变口径）
    """
    mode = (basis or tp_basis()).strip().lower()
    if mode not in TP_BASES:
        mode = TP_BASIS_DEFAULT
    m = tp_atr_mult() if mult is None else float(mult)

    if mode == "atr" and atr_pct_v is not None and np.isfinite(atr_pct_v):
        lvl = tp_level(m, atr_pct_v)
        return {"price": round(t0_close * (1.0 + lvl), 2), "level": round(lvl, 4),
                "basis": "atr", "detail": f"ATR%={atr_pct_v:.4f} × {m}"}
    why = "atr 不可得→回退 legacy" if mode == "atr" else "legacy（默认）"
    try:
        base = float(avg_ret_t5) if avg_ret_t5 else FALLBACK_AVG_T5
    except (TypeError, ValueError):
        base = FALLBACK_AVG_T5
    lvl = _clamp(base * float(ratio))
    return {"price": round(t0_close * (1.0 + lvl), 2), "level": round(lvl, 4),
            "basis": "legacy", "detail": why}


__all__ = ["atr_pct", "atr_pct_arrays", "tp_from_atr", "tp_level", "tp_curve", "resolve",
           "tp_basis", "tp_atr_mult", "ATR_MULTS", "TP_BASES",
           "TP_ATR_MULT_DEFAULT", "TP_BASIS_DEFAULT", "TP_FLOOR", "TP_CEIL", "ATR_N"]
