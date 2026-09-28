"""召回层：全量 3 倍行情段扫描（surge_scanner）

对齐规划：plans/04-回测引擎.md 3.1
  - 宽松扫描：所有 "低点→高点 ≤90 交易日 涨幅≥2.5 倍（预标记）" 的行情段
  - 有效局部低点：低点日收盘价低于其前后各 3 日
  - 只过滤"绝对无法交易"的（ST/退市/次新/极端停牌），不筛形态
  - 输出候选行情段，供第二级形态分类
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import pandas as pd

from app.core.logger import logger
from app.backtest.loader import (
    prepare_stock_data,
    is_st,
    filter_liquidity,
)

# 预标记阈值（候选）：保证真实 3 倍因复权/除权误差落在 [2.5,3.0) 时不漏
CANDIDATE_GAIN = 2.5
# 窗口最大跨度（交易日）
MAX_WINDOW = 90
# 有效局部低点的前后验证日数
LOW_LOOKBACK = 3
# 次新过滤：上市不足交易日数
MIN_LIST_DAYS = 60
# 单日脉冲过滤：低点日需低于前后各 N 日收盘
NEIGHBOR = 3


@dataclass
class SurgeSegment:
    """候选 3 倍行情段。"""
    ts_code: str
    low_date: pd.Timestamp
    low_price: float
    high_date: pd.Timestamp
    high_price: float
    gain: float          # 高点/低点 - 1
    duration: int        # 低点→高点 交易日跨度
    low_idx: int         # 在序列中的下标
    high_idx: int
    detail: dict = field(default_factory=dict)


def _find_valid_lows(df: pd.DataFrame) -> list[int]:
    """识别有效局部低点（波谷）：收盘价严格低于其前后各 NEIGHBOR 日。

    用"严格小于"避免平坦区产生大量假低点；
    并对相邻低点去重（同一波谷只保留最深点）。
    """
    closes = df["adj_close"].to_numpy()
    n = len(closes)
    candidate: list[int] = []
    for i in range(NEIGHBOR, n - NEIGHBOR):
        c = closes[i]
        if c != c:
            continue
        prev = closes[i - NEIGHBOR : i]
        nxt = closes[i + 1 : i + 1 + NEIGHBOR]
        if len(prev) and len(nxt):
            # 严格小于前后窗口内的最低价 → 真正的波谷
            if c < prev.min() and c <= nxt.min():
                candidate.append(i)

    # 相邻候选去重：若两个候选间隔很小，保留更低者
    lows: list[int] = []
    for i in candidate:
        if lows and i - lows[-1] <= NEIGHBOR:
            if closes[i] < closes[lows[-1]]:
                lows[-1] = i
        else:
            lows.append(i)
    return lows


def scan_single_stock(
    df: pd.DataFrame,
    ts_code: str,
    candidate_gain: float = CANDIDATE_GAIN,
    max_window: int = MAX_WINDOW,
) -> list[SurgeSegment]:
    """对单只股票的全复权数据扫描候选 3 倍行情段。

    Args:
        df: prepare_stock_data 输出（含 adj_close，按 trade_date 升序）
        ts_code: 股票代码
        candidate_gain: 预标记阈值（默认 2.5）
        max_window: 最大窗口跨度（默认 90 交易日）

    Returns:
        候选行情段列表（未去重叠）
    """
    if df is None or df.empty or len(df) < (NEIGHBOR * 2 + 1):
        return []

    # 用前复权收盘价计算
    price = df["adj_close"].to_numpy()
    n = len(price)
    segments: list[SurgeSegment] = []

    low_idxs = _find_valid_lows(df)
    for i in low_idxs:
        base = price[i]
        if base is None or base == 0:
            continue
        # 向后 max_window 个交易日内找最高点
        hi = float(price[i])
        hi_idx = i
        for j in range(i, min(i + max_window, n)):
            if price[j] > hi:
                hi = price[j]
                hi_idx = j
        if hi_idx == i:
            continue
        gain = hi / base - 1
        if gain >= candidate_gain:
            segments.append(
                SurgeSegment(
                    ts_code=ts_code,
                    low_date=df["trade_date"].iloc[i],
                    low_price=float(base),
                    high_date=df["trade_date"].iloc[hi_idx],
                    high_price=float(hi),
                    gain=float(gain),
                    duration=hi_idx - i,
                    low_idx=i,
                    high_idx=hi_idx,
                )
            )
    return segments


def dedupe_segments(segments: list[SurgeSegment]) -> list[SurgeSegment]:
    """去重叠：同一股票重叠行情段保留涨幅最大的一段。

    按 low_idx 排序，若两个段窗口重叠，保留 gain 更大者。
    """
    if not segments:
        return []
    segs = sorted(segments, key=lambda s: (s.low_idx, -s.gain))
    result: list[SurgeSegment] = []
    for s in segs:
        # 与结果中最后一段重叠则比较
        if result and result[-1].low_idx <= s.low_idx <= result[-1].high_idx:
            if s.gain > result[-1].gain:
                result[-1] = s
        else:
            result.append(s)
    return result


def scan_market(
    df_pool: pd.DataFrame,
    start_date: str | None = None,
    end_date: str | None = None,
    max_stocks: int | None = None,
) -> list[SurgeSegment]:
    """全市场扫描候选 3 倍行情段（按股票循环，可限流）。

    Args:
        df_pool: load_stock_pool() 输出
        start_date/end_date: 日期范围 YYYYMMDD
        max_stocks: 最多处理股票数（用于测试/小规模验证）

    Returns:
        全部候选行情段（已去重叠）
    """
    if df_pool is None or df_pool.empty:
        logger.warning("scan_market: 股票池为空")
        return []

    codes = df_pool["ts_code"].tolist()
    if max_stocks:
        codes = codes[:max_stocks]

    all_segments: list[SurgeSegment] = []
    _scan_t0 = time.time()
    for idx, ts_code in enumerate(codes):
        try:
            if is_st(ts_code, df_pool):
                continue
            df = prepare_stock_data(ts_code, start_date, end_date)
            if df.empty:
                continue
            # 次新过滤（上市不足 60 个交易日 → 数据量不足 60+N 的跳过）
            if len(df) < MIN_LIST_DAYS:
                continue
            # 流动性过滤
            df = filter_liquidity(df)
            if df.empty:
                continue
            segs = scan_single_stock(df, ts_code)
            all_segments.extend(dedupe_segments(segs))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"scan_market: {ts_code} 处理失败: {exc}")
            continue

        # 进度日志：每 50 只 + 耗时提示（避免长时间"卡住"假象）
        if (idx + 1) % 50 == 0 or (idx + 1) == len(codes):
            elapsed = time.time() - _scan_t0
            rate = (idx + 1) / elapsed if elapsed > 0 else 0.0
            eta = (len(codes) - idx - 1) / rate if rate > 0 else 0.0
            logger.info(
                f"scan_market 进度: {idx + 1}/{len(codes)} ({idx + 1:5.1%}), "
                f"累计候选 {len(all_segments)}, 已用 {elapsed:.0f}s, 预计剩余 {eta:.0f}s"
            )

    logger.info(f"scan_market 完成: 共扫描 {len(codes)} 只, 候选行情段 {len(all_segments)}")
    return all_segments
