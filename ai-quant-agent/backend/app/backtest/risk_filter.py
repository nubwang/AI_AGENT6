"""每日推荐风险过滤（risk_filter）

对齐规划：plans/09-每日推荐详细规划.md 四/第四层
  风险过滤规则（在打分后、上榜前剔除不可买/高风险标的）：
    - ST / 退市整理 → 剔除（loader.is_st）
    - 当日涨停封死 / 一字板（无法买入）→ 剔除
    - 股权质押比例过高（>50%，来自 pledge_stat）→ 剔除
    - 流动性不足（最近 20 日日均成交额 < 下限）→ 剔除
    - 数据量不足（无法可靠提取特征/识别形态）→ 剔除
"""
from __future__ import annotations

import numpy as np

from app.backtest.loader import is_st, MIN_AVG_AMOUNT, MIN_LIST_TRADING_DAYS

# 高质押阈值（质押比例 > 50% 视为高风险）
PLEDGE_RISK_RATIO = 0.50


def check_risk(
    ts_code: str,
    df=None,
    pledge_ratio: float | None = None,
    basic=None,
    st_risk_flag: float | None = None,
    min_avg_amount: float = MIN_AVG_AMOUNT,
    min_days: int = MIN_LIST_TRADING_DAYS,
) -> dict:
    """对单只"疑似启动"股票做风险过滤。

    Args:
        ts_code: 股票代码
        df: prepare_stock_data 输出（含 is_sealed_up / amount，按日期升序）
        pledge_ratio: 股权质押比例（0~1，来自 extra 的 pledge_ratio）
        basic: stock_basic DataFrame（is_st 用）
        st_risk_flag: 即将*ST 高风险预判 0/1（净资产<0 或 连续两期亏损，来自 extra）
        min_avg_amount: 日均成交额下限（千元）
        min_days: 最少交易日（次新过滤）

    Returns:
        {"pass": bool, "reasons": [拒绝原因, ...]}
    """
    reasons: list[str] = []

    # 数据量不足（无法提取 20 日特征/识别形态）
    if df is None or df.empty:
        reasons.append("无数据")
        return {"pass": False, "reasons": reasons}
    if len(df) < min_days:
        reasons.append("次新/数据不足")
        return {"pass": False, "reasons": reasons}

    # 已 ST / 退市（名称含 ST，规划 09 风控规则）
    try:
        if is_st(ts_code, basic):
            reasons.append("ST/退市整理")
    except Exception:  # noqa: BLE001
        pass

    # 即将*ST 高风险（用户补充：净资产<0 或 连续两期亏损 → 特别高风险，提前规避）
    if st_risk_flag is not None and st_risk_flag == st_risk_flag and float(st_risk_flag) > 0:
        reasons.append("即将*ST高风险(净资产<0或连续亏损)")

    # 当日涨停封死 / 一字板（无法买入）
    if "is_sealed_up" in df.columns:
        if bool(df["is_sealed_up"].iloc[-1]):
            reasons.append("涨停封死不可买")

    # 流动性不足（最近 20 日日均成交额）
    if "amount" in df.columns:
        amt = df["amount"].tail(20).to_numpy(dtype=float)
        amt = amt[~np.isnan(amt)]
        if len(amt) >= 5 and float(amt.mean()) < min_avg_amount:
            reasons.append("流动性不足")

    # 高质押（兼容 0-1 与百分比%两种单位）
    if pledge_ratio is not None and pledge_ratio == pledge_ratio:
        pr = pledge_ratio / 100.0 if pledge_ratio > 1.0 else pledge_ratio
        if pr > PLEDGE_RISK_RATIO:
            reasons.append(f"高质押({pr:.0%})")

    return {"pass": len(reasons) == 0, "reasons": reasons}
