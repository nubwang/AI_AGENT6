"""复权计算工具 — 用 adj_factor 对股价进行前复权/后复权

Tushare 的 daily 接口返回的是**原始价格**（未复权），
adj_factor 接口提供每日的复权因子。

复权公式（来自 Tushare 官方文档）：
  前复权价 = raw_price × adj_factor ÷ 最新adj_factor
  后复权价 = raw_price × adj_factor

本项目的选择：**前复权** ✅
  理由：
  1. 模式匹配需要历史价格连续可比，前复权让过去的价格"折现"到当前
  2. 所有技术指标（均线/MACD/RSI/KDJ）必须在前复权数据上计算才正确
  3. 同花顺、东方财富、通达信默认展示的就是前复权
  4. 当前股价保持实际市场价格，不扭曲

用法：
  from app.data.adjust import get_adj_close, get_adj_dataframe
  
  # 获取某只股票某日的前复权收盘价
  price = get_adj_close("600036.SH", "2024-01-15")
  
  # 对整个DataFrame进行前复权
  df = get_adj_dataframe(df, "600036.SH")
"""
import pandas as pd
from sqlalchemy import text
from app.models import SessionLocal


def load_adj_factors(ts_code: str) -> pd.DataFrame:
    """从数据库加载某只股票的复权因子"""
    db = SessionLocal()
    try:
        df = pd.read_sql(
            text(f"SELECT trade_date, adj_factor FROM adj_factor WHERE ts_code='{ts_code}' ORDER BY trade_date"),
            db.connection(),
        )
        if df.empty:
            return df
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df.set_index("trade_date", inplace=True)
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        db.close()


def get_latest_adj_factor(ts_code: str, upto: str | None = None) -> float:
    """获取最新复权因子（前复权的分母）。

    Args:
        upto: 截止日 YYYYMMDD（None = 全表最新，实时口径，行为不变）。
            **自证回放必须传 as_of** —— 否则分母取"今天"的因子，未来除权会改写历史价格序列
            （plans/25 F3）。
    """
    db = SessionLocal()
    try:
        sql = f"SELECT adj_factor FROM adj_factor WHERE ts_code='{ts_code}'"
        if upto:
            sql += f" AND trade_date <= '{str(upto).strip().replace('-', '')}'"
        sql += " ORDER BY trade_date DESC LIMIT 1"
        r = db.execute(text(sql)).scalar()
        return float(r) if r else 1.0
    except Exception:
        return 1.0
    finally:
        db.close()


def get_adj_close(ts_code: str, trade_date: str) -> float | None:
    """获取某只股票在某个交易日的前复权收盘价
    
    Args:
        ts_code: 股票代码，如 "600036.SH"
        trade_date: 交易日，格式 "YYYYMMDD" 或 "YYYY-MM-DD"
    
    Returns:
        前复权收盘价，如果数据不存在返回 None
    """
    # 统一日期格式
    trade_date_clean = trade_date.replace("-", "")
    
    db = SessionLocal()
    try:
        # 查原始收盘价
        row = db.execute(
            text(f"SELECT close FROM daily WHERE ts_code='{ts_code}' AND trade_date='{trade_date_clean}'")
        ).fetchone()
        if not row:
            return None
        raw_close = float(row[0])
        
        # 查复权因子
        adj_row = db.execute(
            text(f"SELECT adj_factor FROM adj_factor WHERE ts_code='{ts_code}' AND trade_date='{trade_date_clean}'")
        ).fetchone()
        if not adj_row:
            return raw_close  # 没有复权因子则返回原始价
        
        adj = float(adj_row[0])
        
        # 查最新复权因子
        latest_adj = get_latest_adj_factor(ts_code)
        
        # 前复权计算
        return round(raw_close * adj / latest_adj, 4)
    except Exception:
        return None
    finally:
        db.close()


def adjust_dataframe(df: pd.DataFrame, ts_code: str, method: str = "forward") -> pd.DataFrame:
    """对DataFrame进行复权处理
    
    Args:
        df: 包含 open/high/low/close 列的DataFrame
        ts_code: 股票代码
        method: 'forward' 前复权 (默认) | 'backward' 后复权
    
    Returns:
        复权后的DataFrame
    """
    if df.empty:
        return df
    
    # 获取最新复权因子
    latest_adj = get_latest_adj_factor(ts_code)
    if latest_adj == 1.0:
        return df  # 没有复权数据，返回原数据
    
    # 获取每日复权因子
    adj_factors = load_adj_factors(ts_code)
    if adj_factors.empty:
        return df
    
    # 合并复权因子到DataFrame
    price_cols = ["open", "high", "low", "close"]
    for col in price_cols:
        if col not in df.columns:
            continue
        # 确保类型正确
        df[col] = pd.to_numeric(df[col], errors="coerce")
    
    # 按日期对齐
    df = df.copy()
    if "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df.set_index("trade_date", inplace=True)
    
    # 合并因子
    df = df.join(adj_factors[["adj_factor"]], how="left")
    df["adj_factor"].ffill(inplace=True)  # 用前一个因子填充缺失
    df["adj_factor"].fillna(1.0, inplace=True)
    
    # 计算复权价
    for col in price_cols:
        if col not in df.columns:
            continue
        if method == "forward":
            df[col] = round(df[col] * df["adj_factor"] / latest_adj, 4)
        else:  # backward
            df[col] = round(df[col] * df["adj_factor"], 4)
    
    df.drop(columns=["adj_factor"], inplace=True, errors="ignore")
    return df
