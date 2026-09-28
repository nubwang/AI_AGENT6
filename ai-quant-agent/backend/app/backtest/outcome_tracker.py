"""正负分流（outcome_tracker）

对齐规划：plans/04-回测引擎.md 3.3
  - 核心原则：负样本必须与正样本"同形态"（V 反转的失败只能配 V 反转的失败）
  - 涨幅从启动点 T0 算起
  - 分流规则（同一形态内部）：
      success      ：T0 后 90 日内 最高价/启动价 - 1 ≥ 3 倍
      near_success ：[2.5, 3.0) 边缘段，不成为正样本；回落>30% 转 failure_A
      failure_A    ：最高涨幅 [50%, 150%) 且从最高点回落 >30%
      failure_B    ：启动后 20 个交易日涨幅 <20%
      discard      ：其余（保持纯净）
  - 多周期结果：记录 T+1/T+5/T+20/T+90 涨幅与最大回撤
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logger import logger

SUCCESS_GAIN = 3.0          # 成功：3 倍
NEAR_SUCCESS_GAIN = 2.5     # 边缘段下界
FAILURE_A_MIN = 0.50        # failure_A 涨幅下界 50%
FAILURE_A_MAX = 1.50        # failure_A 涨幅上界 150%
FAILURE_A_DRAWDOWN = 0.30   # failure_A 从最高点回落 >30%
FAILURE_B_WIN = 20          # failure_B 观察窗口 20 日
FAILURE_B_GAIN = 0.20       # failure_B 涨幅 <20%
OUTCOME_WIN = 90            # 结局窗口 90 交易日

LABEL_SUCCESS = "success"
LABEL_NEAR = "near_success"
LABEL_FAILURE_A = "failure_A"
LABEL_FAILURE_B = "failure_B"
LABEL_DISCARD = "discard"


@dataclass
class OutcomeSample:
    """一个带结局标签的样本。"""
    ts_code: str
    form_type: str              # A/B/C/D/E
    label: str                  # success / failure_A / failure_B / discard / near_success
    t0_idx: int                 # 启动点下标
    t0_date: pd.Timestamp
    start_price: float
    peak_price: float
    peak_gain: float            # 启动价→最高价涨幅
    peak_idx: int
    peak_date: pd.Timestamp
    max_drawdown: float         # 启动后最大回撤
    outcome_t1: float | None = None
    outcome_t5: float | None = None
    outcome_t20: float | None = None
    outcome_t90: float | None = None
    truncated: bool = False     # 结局窗口是否被数据末端截断（⑬ 结局不完整）
    detail: dict = field(default_factory=dict)


def _track_outcomes(df: pd.DataFrame, t0_idx: int, window: int = OUTCOME_WIN) -> tuple[dict, int, pd.Timestamp, float, bool]:
    """从 T0 起跟踪未来 window 个交易日。

    Returns:
        (outcomes, peak_idx, peak_date, max_drawdown, truncated)
        outcomes: {"t1","t5","t20","t90","peak_gain"} 相对启动价
        truncated: 结局窗口是否被数据末端截断（未走满 window 个交易日）
    """
    close = df["adj_close"].to_numpy()
    n = len(close)
    start_price = float(close[t0_idx])
    if start_price <= 0:
        return {}, t0_idx, df["trade_date"].iloc[t0_idx], 0.0, False

    end = min(t0_idx + window, n)
    seg_close = close[t0_idx : end + 1]
    if len(seg_close) == 0:
        return {}, t0_idx, df["trade_date"].iloc[t0_idx], 0.0, False

    truncated = (t0_idx + window) > (n - 1)

    rel = seg_close / start_price - 1.0  # 相对涨幅序列
    peak_idx_rel = int(np.argmax(rel))
    peak_idx = t0_idx + peak_idx_rel
    peak_gain = float(rel[peak_idx_rel])
    peak_date = df["trade_date"].iloc[peak_idx]

    # 最大回撤：从最高点回落的最大幅度
    running_peak = np.maximum.accumulate(seg_close)
    drawdowns = (seg_close - running_peak) / running_peak
    max_dd = float(np.min(drawdowns)) if len(drawdowns) else 0.0

    def _ret(offset: int) -> float | None:
        idx = t0_idx + offset
        if idx < n:
            return float(close[idx] / start_price - 1.0)
        return None

    outcomes = {
        "t1": _ret(1),
        "t5": _ret(5),
        "t20": _ret(20),
        "t90": _ret(90),
        "peak_gain": peak_gain,
    }
    return outcomes, peak_idx, peak_date, max_dd, truncated


def classify_outcome(df: pd.DataFrame, cls, window: int = OUTCOME_WIN) -> OutcomeSample:
    """对单个已分类行情段进行结局分流。"""
    t0_idx = cls.t0_idx
    start_price = cls.start_price
    outcomes, peak_idx, peak_date, max_dd, truncated = _track_outcomes(df, t0_idx, window)

    peak_gain = outcomes.get("peak_gain", cls.peak_gain)
    t20 = outcomes.get("t20")

    # 分流规则（在同一形态内部）
    if peak_gain >= SUCCESS_GAIN:
        label = LABEL_SUCCESS
    elif peak_gain >= NEAR_SUCCESS_GAIN:
        # 边缘段：不成为正样本，若回落 >30% 转 failure_A，否则 discard
        if max_dd < -FAILURE_A_DRAWDOWN:
            label = LABEL_FAILURE_A
        else:
            label = LABEL_NEAR
    elif FAILURE_A_MIN <= peak_gain < FAILURE_A_MAX and max_dd < -FAILURE_A_DRAWDOWN:
        label = LABEL_FAILURE_A
    elif t20 is not None and t20 < FAILURE_B_GAIN:
        label = LABEL_FAILURE_B
    else:
        label = LABEL_DISCARD

    # ⑬ 结局窗口被截断且未达 3 倍 → 无法确认成败，归 discard（由 filter_noisy_samples 剔除）
    if truncated and label != LABEL_SUCCESS:
        label = LABEL_DISCARD

    return OutcomeSample(
        ts_code=cls.segment.ts_code,
        form_type=cls.form_type,
        label=label,
        t0_idx=t0_idx,
        t0_date=df["trade_date"].iloc[t0_idx],
        start_price=start_price,
        peak_price=cls.peak_price,
        peak_gain=peak_gain,
        peak_idx=peak_idx,
        peak_date=peak_date,
        max_drawdown=max_dd,
        outcome_t1=outcomes.get("t1"),
        outcome_t5=outcomes.get("t5"),
        outcome_t20=outcomes.get("t20"),
        outcome_t90=outcomes.get("t90"),
        truncated=truncated,
        detail=cls.detail,
    )


def batch_classify_outcomes(df: pd.DataFrame, classified: list) -> list[OutcomeSample]:
    """批量分流。"""
    result: list[OutcomeSample] = []
    for cls in classified:
        try:
            sample = classify_outcome(df, cls)
            result.append(sample)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"outcome 分流失败 {getattr(cls, 'segment', None)}: {exc}")
    return result


def sample_summary(samples: list[OutcomeSample]) -> dict:
    """统计正负样本分布。"""
    from collections import Counter

    counter = Counter(s.label for s in samples)
    return {
        "total": len(samples),
        "success": counter.get(LABEL_SUCCESS, 0),
        "near_success": counter.get(LABEL_NEAR, 0),
        "failure_A": counter.get(LABEL_FAILURE_A, 0),
        "failure_B": counter.get(LABEL_FAILURE_B, 0),
        "discard": counter.get(LABEL_DISCARD, 0),
        "by_form": {f: Counter(s.label for s in samples if s.form_type == f) for f in ("A", "B", "C", "D", "E", "U")},
    }


def filter_noisy_samples(
    df: pd.DataFrame,
    outcomes: list[OutcomeSample],
    long_suspend_days: int = 30,
    max_gap_days: int = 20,
    adj_anomaly_pct: float = 0.25,
) -> list[OutcomeSample]:
    """样本精准度过滤（对齐 plans/04 3.10 噪音规避：⑲ ⑬ ⑥ ⑨ ⑧）。

    目的：保证"增加样本不混入噪音"，宁可少而精准，勿多而失真。

    - ⑲ 涨跌停无法成交：T0 当天涨停封死（一字板/封板，close>=up_limit*0.997）
        无法买入 → 剔除（该 success 不可交易，会虚高推荐命中）
    - ⑬ 结局不完整：结局窗口被数据末端截断且未达 3 倍 → 无法确认成败，剔除
        （已确认 3 倍的截断样本保留，由 classify_outcome 归 success）
    - ⑥ 长期停牌后复牌跳空：T0 前后窗口内相邻交易日间隔 > long_suspend_days
        自然日（长期停牌）→ 复牌跳空暴涨非可交易爆发，剔除
    - ⑨ 停牌过多：T0 前 60 日窗口内最大日间隔 > max_gap_days 自然日 → 剔除
    - ⑧ 复权异常/除权跳空：前复权序列单日 |涨跌幅| > adj_anomaly_pct（超出
        所有板块涨跌停上限+缓冲）→ 复权因子缺失/错误导致假跳空，剔除。
        正常前复权序列除权日平滑，单日波动受涨跌停限制，不应出现此类跳变。

    Returns:
        过滤后的样本列表
    """
    if df is None or df.empty or not outcomes:
        return outcomes
    sealed = df["is_sealed_up"].to_numpy(dtype=bool) if "is_sealed_up" in df.columns else None
    dates = df["trade_date"].to_numpy(dtype="datetime64[D]") if "trade_date" in df.columns else None
    adj_close = df["adj_close"].to_numpy(dtype=float) if "adj_close" in df.columns else None
    result: list[OutcomeSample] = []
    for o in outcomes:
        t0 = o.t0_idx
        # ⑲ T0 当天封死不可买
        if sealed is not None and t0 < len(sealed) and bool(sealed[t0]):
            continue
        # ⑬ 结局截断未达 3 倍（已由 classify_outcome 归 discard，此处兜底剔除）
        if o.truncated and o.label == LABEL_DISCARD:
            continue
        # ⑥ / ⑨ 停牌检测（停牌日已从 df 剔除，用相邻交易日间隔估算）
        if dates is not None:
            win = dates[max(0, t0 - 60): min(t0 + 90, len(dates))]
            if len(win) >= 3:
                gaps = np.diff(win.astype("int64")).astype(float)
                if gaps.max() > long_suspend_days:
                    continue
            pre = dates[max(0, t0 - 60): t0 + 1]
            if len(pre) >= 3:
                pre_gaps = np.diff(pre.astype("int64")).astype(float)
                if pre_gaps.max() > max_gap_days:
                    continue
        # ⑧ 复权异常：窗口内单日 |涨跌幅| 超阈值（复权因子错误导致假跳空）
        if adj_close is not None:
            ac = adj_close[max(0, t0 - 60): min(t0 + 90, len(adj_close))]
            if len(ac) >= 3:
                rets = np.abs(np.diff(ac) / np.maximum(np.abs(ac[:-1]), 1e-9))
                if np.nanmax(rets) > adj_anomaly_pct:
                    continue
        result.append(o)
    return result
