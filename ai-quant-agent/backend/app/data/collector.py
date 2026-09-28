"""数据采集器（支持全部42个API）"""
import pandas as pd
from sqlalchemy import text
from app.core.logger import logger
from app.core.config import settings
from app.data.tushare_api import tushare_client
from app.data.rate_limiter import slimiter
from app.models import SessionLocal


class DataCollector:
    """数据采集器"""

    def __init__(self):
        self.client = tushare_client
        self.db = SessionLocal

    def get_trade_dates(self, start: str = "20100101", end: str = "") -> list:
        """获取交易日列表"""
        import datetime
        end = end or datetime.date.today().strftime("%Y%m%d")
        with slimiter("trade_cal"):
            df = self.client.trade_cal(start_date=start, end_date=end)
        if df is not None and not df.empty:
            return sorted(df[df["is_open"] == 1]["cal_date"].tolist())
        return []

    def get_all_stocks(self) -> list:
        """获取所有股票代码"""
        db = self.db()
        try:
            result = db.execute(text("SELECT ts_code FROM stock_basic"))
            return [row[0] for row in result]
        finally:
            db.close()

    # 这些列不参与 `ON DUPLICATE KEY UPDATE`（键列 / 自增主键）
    _UPSERT_SKIP = ("id", "ts_code", "trade_date", "cal_date", "ann_date")

    def collect_and_store(self, table: str, df: pd.DataFrame):
        """采集并存入数据库（**幂等 upsert**）。

        ★ 2026-09-21 修正（plans/24 §11.36.9，一次真实事故）：
          原实现用 `to_sql(if_exists="append", method="multi", chunksize=500)`。
          在**有唯一键**的表上（如 `adj_factor` 的 `uk_ts_date`），
          只要**任意一行**与已有键冲突，**整批 500 行 INSERT 就会失败**，
          而 `except` 只打一条 warning ⇒ **静默丢数据**。
          实测后果：自愈采集跑了 **10,684 个任务**，`adj_factor` **一行都没变**
          （每天都返回该股近几日因子，其中最新那天已存在 ⇒ 冲突 ⇒ 整批丢弃）。

          改为 `INSERT ... ON DUPLICATE KEY UPDATE`：
            · 表**有**唯一键 ⇒ 已有键则更新、缺的键则插入（真正的幂等，残缺可自愈）；
            · 表**无**唯一键 ⇒ MySQL 不触发该分支，行为与原来的 append **完全一致**（向后兼容）。
        """
        if df is None or df.empty:
            return
        cols = [str(c) for c in df.columns]
        if not cols:
            return
        ph = ", ".join(f":{c}" for c in cols)
        upd = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in cols
                        if c not in self._UPSERT_SKIP)
        sql = (f"INSERT INTO `{table}` ({', '.join('`' + c + '`' for c in cols)}) "
               f"VALUES ({ph})")
        sql += (f" ON DUPLICATE KEY UPDATE {upd}" if upd
                else " ON DUPLICATE KEY UPDATE `id`=`id`")
        db = self.db()
        try:
            n = 0
            for i in range(0, len(df), 500):
                chunk = df.iloc[i:i + 500]
                recs = chunk.astype(object).where(pd.notnull(chunk), None).to_dict("records")
                if not recs:
                    continue
                db.execute(text(sql), recs)
                n += len(recs)
            db.commit()
            logger.info(f"{table}: {n} 条（upsert）")
        except Exception as e:
            db.rollback()
            logger.warning(f"{table} 存储失败: {e}")
        finally:
            db.close()

    def collect_daily(self, trade_date: str) -> pd.DataFrame:
        with slimiter("daily"):
            return self.client.daily(trade_date=trade_date)

    def collect_daily_basic(self, trade_date: str) -> pd.DataFrame:
        with slimiter("daily_basic"):
            return self.client.daily_basic(trade_date=trade_date)

    def collect_moneyflow(self, trade_date: str) -> pd.DataFrame:
        with slimiter("moneyflow"):
            return self.client.moneyflow(trade_date=trade_date)

    def collect_stock_basic(self) -> pd.DataFrame:
        with slimiter("stock_basic"):
            return self.client.stock_basic()


data_collector = DataCollector()
