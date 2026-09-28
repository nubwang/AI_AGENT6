"""回测数据加载与预处理

负责从 MySQL 加载回测所需数据，并做核心预处理：
  - 前复权（用 adj_factor）
  - 停牌过滤（suspend_d）
  - 涨停/跌停过滤（stk_limit，封板判定 close >= up_limit*0.997）
  - ST/退市/次新过滤（stock_basic）
  - 全市场股票池加载

对齐规划：plans/04-回测引擎.md 第三章（样本精准采集）与第五章（可交易性过滤）
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.models import SessionLocal
from app.core.logger import logger

# 封板判定阈值（触及涨停价 99.7% 即视为封死，无法成交）
SEAL_RATIO = 0.997
# 次新过滤：上市不足 N 个交易日剔除
MIN_LIST_TRADING_DAYS = 60
# 停牌容忍：特征/结局窗口内停牌累计占比超过该值则剔除
MAX_SUSPEND_RATIO = 0.20
# 流动性过滤：启动点前日均成交额下限
# 注意：daily.amount 单位为"千元"，5000 万元 = 50000 千元
MIN_AVG_AMOUNT = 50_000  # 5000 万元（千元口径）


def load_stock_pool() -> pd.DataFrame:
    """加载全市场股票池（含行业/上市日期/是否ST）。

    Returns:
        DataFrame: [ts_code, symbol, name, industry, market, list_date, delist_date]
    """
    db = SessionLocal()
    try:
        df = pd.read_sql(
            text(
                "SELECT ts_code, symbol, name, industry, market, list_date "
                "FROM stock_basic WHERE list_date IS NOT NULL"
            ),
            db.connection(),
        )
        if df.empty:
            return df
        # list_date 兼容纯日期(YYYYMMDD)与带时间(datetime)两种存储格式
        df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce")
        return df
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"load_stock_pool 失败: {exc}")
        return pd.DataFrame()
    finally:
        db.close()


def load_daily(ts_code: str, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    """加载某只股票日线数据（原始价，未复权）。

    Args:
        ts_code: 股票代码
        start_date: 起始日期 YYYYMMDD
        end_date: 截止日期 YYYYMMDD

    Returns:
        DataFrame: [trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount]
    """
    db = SessionLocal()
    try:
        sql = (
            f"SELECT trade_date, open, high, low, close, pre_close, `change`, pct_chg, vol, amount "
            f"FROM daily WHERE ts_code='{ts_code}'"
        )
        if start_date:
            sql += f" AND trade_date>='{start_date}'"
        if end_date:
            sql += f" AND trade_date<='{end_date}'"
        sql += " ORDER BY trade_date"
        df = pd.read_sql(text(sql), db.connection())
        if df is None or df.empty:
            return pd.DataFrame()
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        for col in ("open", "high", "low", "close", "pre_close", "pct_chg"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["vol"] = pd.to_numeric(df["vol"], errors="coerce")
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        return df
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"load_daily({ts_code}) 失败: {exc}")
        return pd.DataFrame()
    finally:
        db.close()


def load_adj_close(ts_code: str, df: pd.DataFrame | None = None,
                   adj_base_date: str | None = None) -> pd.DataFrame:
    """前复权处理：close 乘 adj_factor 除以最新 adj_factor。

    对齐 plans/04 3.10 ⑧（复权错误/除权跳空）：
      1. 缺失复权因子用前值填充（ffill），而非 fillna(1.0)——
         fillna(1.0) 会让缺失日价格被错误缩放（close/latest），产生假跳空/假 3 倍段。
      2. 同时输出 adj_open/adj_high/adj_low，保证技术指标（ATR/布林带/KDJ）在
         除权日前后一致复权，避免 open/high/low 用原始价导致失真。

    Args:
        ts_code: 股票代码
        df: 已加载的日线 DataFrame（可选，内部加载 adj_factor）
        adj_base_date: 前复权**基准日**（YYYYMMDD）。None = 全表最新因子（实时口径，行为逐字节不变）；
            自证回放**必须传 as_of** —— 否则未来发生的除权会改写 as_of 之前的整段价格序列
            （绝对价格、止损/追高上限等价格化参数都会受影响，见 plans/25 F3）。

    Returns:
        df 增加 'adj_factor'/'adj_close'/'adj_open'/'adj_high'/'adj_low' 列
    """
    if df is None or df.empty:
        return df
    db = SessionLocal()
    try:
        adj = pd.read_sql(
            text(f"SELECT trade_date, adj_factor FROM adj_factor WHERE ts_code='{ts_code}' ORDER BY trade_date"),
            db.connection(),
        )
        if adj.empty:
            df["adj_close"] = df["close"]
            return df
        adj["trade_date"] = pd.to_datetime(adj["trade_date"])
        adj = adj.drop_duplicates("trade_date").set_index("trade_date")["adj_factor"]
        adj = adj.sort_index()          # 显式排序：下面的时间切片依赖有序索引
        if adj_base_date:
            # 回放口径：分母 = **截至 as_of 的最新因子**（时序与当年实盘一致）
            _base = pd.Timestamp(str(adj_base_date).strip().replace("-", ""))
            _hist = adj[adj.index <= _base]
            latest = float(_hist.iloc[-1]) if len(_hist) else float(adj.iloc[0])
        else:
            latest = float(adj.iloc[-1]) if len(adj) else 1.0
        # 缺失因子：ffill 用前一个因子填充；最早期仍缺失的保持 1.0（该段不复权）
        df["adj_factor"] = df["trade_date"].map(adj)
        df["adj_factor"] = df["adj_factor"].ffill().fillna(1.0)
        for col in ("close", "open", "high", "low"):
            if col in df.columns:
                df[f"adj_{col}"] = df[col] * df["adj_factor"] / latest
        return df
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"load_adj_close({ts_code}) 失败: {exc}")
        df["adj_close"] = df["close"]
        return df
    finally:
        db.close()


def load_suspend_days(ts_code: str) -> set:
    """加载某只股票所有停牌交易日。

    说明：suspend_d 表实际字段为 suspend_timing / suspend_type；
    以"存在 suspend_timing 记录"视为停牌（suspended 的交易日）。
    """
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                f"SELECT trade_date FROM suspend_d WHERE ts_code='{ts_code}'"
            )
        ).fetchall()
        dates = set()
        for r in rows:
            try:
                dates.add(pd.Timestamp(r[0]))
            except Exception:  # noqa: BLE001
                continue
        return dates
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"load_suspend_days({ts_code}) 失败: {exc}")
        return set()
    finally:
        db.close()


def load_limit_prices(ts_code: str, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    """加载涨跌停价格。

    Returns:
        DataFrame: [trade_date, up_limit, down_limit]
    """
    db = SessionLocal()
    try:
        sql = f"SELECT trade_date, up_limit, down_limit FROM stk_limit WHERE ts_code='{ts_code}'"
        if start_date:
            sql += f" AND trade_date>='{start_date}'"
        if end_date:
            sql += f" AND trade_date<='{end_date}'"
        sql += " ORDER BY trade_date"
        df = pd.read_sql(text(sql), db.connection())
        if df.empty:
            return df
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"load_limit_prices({ts_code}) 失败: {exc}")
        return pd.DataFrame()
    finally:
        db.close()


def is_st(ts_code: str, df_basic: pd.DataFrame | None = None) -> bool:
    """判断是否 ST 股（按名称含 ST 或退市整理）。"""
    if df_basic is not None and not df_basic.empty:
        row = df_basic[df_basic["ts_code"] == ts_code]
        if not row.empty and "name" in row.columns:
            name = str(row.iloc[0]["name"])
            return "ST" in name.upper() or "退" in name
    db = SessionLocal()
    try:
        r = db.execute(text(f"SELECT name FROM stock_basic WHERE ts_code='{ts_code}'")).fetchone()
        return bool(r and ("ST" in str(r[0]).upper() or "退" in str(r[0])))
    except Exception:  # noqa: BLE001
        return False
    finally:
        db.close()


def prepare_stock_data(ts_code: str, start_date: str | None = None, end_date: str | None = None,
                       adj_base_date: str | None = None) -> pd.DataFrame:
    """一站式准备某只股票的回测数据（前复权 + 停牌剔除 + 封板标记）。

    Args:
        start_date/end_date: 取数区间（回放时 end_date = as_of）
        adj_base_date: 前复权基准日；**回放时传 as_of**（否则用全表最新因子，含未来除权前视，F3）

    Returns:
        DataFrame: [trade_date, open, high, low, close, pre_close, pct_chg, vol, amount,
                    adj_close, is_sealed_up, is_sealed_down]
    """
    df = load_daily(ts_code, start_date, end_date)
    if df.empty:
        return df

    # 前复权（adj_base_date 仅在回放时传入 → 把基准因子锚到 as_of）
    df = load_adj_close(ts_code, df, adj_base_date=adj_base_date)

    # 停牌剔除
    suspend = load_suspend_days(ts_code)
    if suspend:
        df = df[~df["trade_date"].isin(suspend)].copy()

    # 封板标记（收盘价触及涨停/跌停视为封死不可成交）
    limits = load_limit_prices(ts_code, start_date, end_date)
    if not limits.empty:
        limits = limits.drop_duplicates("trade_date").set_index("trade_date")
        df["up_limit"] = df["trade_date"].map(limits["up_limit"])
        df["down_limit"] = df["trade_date"].map(limits["down_limit"])
        df["is_sealed_up"] = df["close"] >= df["up_limit"] * SEAL_RATIO
        df["is_sealed_down"] = df["close"] <= df["down_limit"] * (2 - SEAL_RATIO)
        df["is_sealed_up"] = df["is_sealed_up"].fillna(False)
        df["is_sealed_down"] = df["is_sealed_down"].fillna(False)
    else:
        df["up_limit"] = None
        df["down_limit"] = None
        df["is_sealed_up"] = False
        df["is_sealed_down"] = False

    df = df.sort_values("trade_date").reset_index(drop=True)
    return df


def filter_liquidity(df: pd.DataFrame, lookback: int = 60, min_amount: float = MIN_AVG_AMOUNT) -> pd.DataFrame:
    """流动性过滤：保留"日均成交额 >= min_amount"的股票数据。

    用"最近 80% 区间"的日均成交额（而非最早 lookback 日），
    避免早期流动性差但后期有 3 倍行情的股票被误滤。

    注意：daily.amount 单位为千元，min_amount 默认 50000 千元 = 5000 万元。
    若 amount 缺失/为空，视为不达流动性。
    """
    if df.empty or len(df) < lookback:
        return df
    amt = df["amount"].to_numpy(dtype=float)
    amt = amt[~np.isnan(amt)]
    if len(amt) < lookback // 2:
        return pd.DataFrame()
    # 取最近 80% 区间（避开上市初期流动性不足）
    amt = amt[int(len(amt) * 0.2):]
    if len(amt) < lookback // 2:
        return pd.DataFrame()
    avg_amount = float(amt.mean())
    return df if avg_amount >= min_amount else pd.DataFrame()


def filter_suspend_ratio(df: pd.DataFrame, window: int, max_ratio: float = MAX_SUSPEND_RATIO) -> bool:
    """判断某窗口内停牌占比是否超标（停牌日已提前剔除，此处用缺失交易日近似）。

    说明：stop 日已在 prepare_stock_data 剔除，这里以"窗口应有交易日 vs 实际交易日"估算停牌比例。
    简化实现：若窗口内实际连续交易日远少于应有时（存在长缺口），视为停牌过多。
    """
    if df.empty:
        return True
    if len(df) >= window:
        return False
    # 数据量不足窗口 → 用日期跨度估算
    span_days = (df["trade_date"].iloc[-1] - df["trade_date"].iloc[0]).days
    # 交易日约等于工作日，跨度太大而交易日太少 → 停牌过多
    return span_days > int(window * 1.8)


def get_trade_dates(start_date: str, end_date: str) -> list:
    """获取区间内全部交易日（trade_cal）。"""
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                f"SELECT cal_date FROM trade_cal WHERE is_open=1 "
                f"AND cal_date>='{start_date}' AND cal_date<='{end_date}' ORDER BY cal_date"
            )
        ).fetchall()
        return [pd.Timestamp(r[0]) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"get_trade_dates 失败: {exc}")
        return []
    finally:
        db.close()


def get_latest_daily_date():
    """获取股票日线（daily 表）最新交易日，作为每日推荐等"数据时间基准"。

    每日推荐的结果日期应以该日期为准（而非运行当天日期）：
    例如 8/12 运行扫描但日线数据仅采集到 8/11，则结果日期应为 20260811，
    避免用运行日期虚标尚未有数据的交易日。

    Returns:
        pd.Timestamp | None: 最新日线日期；无日线数据时返回 None
    """
    db = SessionLocal()
    try:
        r = db.execute(text("SELECT MAX(trade_date) FROM daily")).fetchone()
        if r and r[0] is not None:
            return pd.Timestamp(r[0])
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"get_latest_daily_date 失败: {exc}")
        return None
    finally:
        db.close()
