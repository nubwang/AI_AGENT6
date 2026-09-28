"""一次性补齐：概念/板块 + 市场参考数据（对齐 plans/01 数据模块）

补齐 7 张未建/未采集的表：
  concept / concept_detail / ths_index / ths_daily / ths_member / limit_list_d / hsgt
  （另附 margin 融资融券，若也缺失）

用途：
  - 候选股"所属板块/概念"（candidate_profiler → Agent 政策解读/消息面查询的基础）
  - 市场参考（龙虎榜/沪深港通，供后续增强）

运行：cd backend && ./venv/bin/python scripts/collect_sector_data.py
说明：concept_detail/ths_member 需遍历概念列表，Tushare 限流约 200/min，全程预计 5-10 分钟。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/

import pandas as pd  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models import Base, SessionLocal  # noqa: E402
from app.data.tushare_api import tushare_client  # noqa: E402

RATE_SEC = 0.35  # Tushare 限流 ~200/min，每次调用间隔


def ensure_tables():
    """建缺失表（幂等：已存在的表跳过）。"""
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    print("✅ 建表完成（缺失表已创建）")


def store(table: str, df: pd.DataFrame) -> int:
    """入库（append）。"""
    if df is None or df.empty:
        return 0
    db = SessionLocal()
    try:
        df.to_sql(table, db.connection(), if_exists="append", index=False, method="multi", chunksize=2000)
        db.commit()
        return len(df)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"  ⚠️ store {table} 失败: {exc}")
        return 0
    finally:
        db.close()


def fetch_safe(fn, **kw) -> pd.DataFrame:
    time.sleep(RATE_SEC)
    try:
        return fn(**kw)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ {fn.__name__} 调用失败: {exc}")
        return pd.DataFrame()


def recent_trade_dates(n: int = 8) -> list[str]:
    """最近 n 个开放交易日（YYYYMMDD）。"""
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT cal_date FROM trade_cal WHERE is_open=1 ORDER BY cal_date DESC LIMIT :n"),
            {"n": n},
        ).fetchall()
        return [str(r[0]).replace("-", "")[:8] for r in rows]
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 获取交易日失败: {exc}")
        return []
    finally:
        db.close()


def main():
    ensure_tables()

    # ── 1. concept + concept_detail ──
    print("=== concept（概念板块列表）===")
    concepts = fetch_safe(tushare_client.concept)
    n = store("concept", concepts)
    print(f"concept: {n} 条")
    codes = concepts["code"].tolist() if not concepts.empty else []
    print(f"=== concept_detail（遍历 {len(codes)} 个概念）===")
    cnt = 0
    for i, c in enumerate(codes):
        df = fetch_safe(tushare_client.concept_detail, code=c)
        if not df.empty:
            cnt += store("concept_detail", df)
        if (i + 1) % 50 == 0:
            print(f"  concept_detail 进度 {i + 1}/{len(codes)}，累计 {cnt}")
    print(f"concept_detail: 累计 {cnt} 条")

    # ── 2. ths_index + ths_member ──
    print("=== ths_index（同花顺概念列表）===")
    ths = fetch_safe(tushare_client.ths_index)
    n = store("ths_index", ths)
    print(f"ths_index: {n} 条")
    ths_codes = ths["ts_code"].tolist() if not ths.empty else []
    print(f"=== ths_member（遍历 {len(ths_codes)} 个概念）===")
    cnt = 0
    for i, c in enumerate(ths_codes):
        df = fetch_safe(tushare_client.ths_member, ts_code=c)
        if not df.empty:
            cnt += store("ths_member", df)
        if (i + 1) % 50 == 0:
            print(f"  ths_member 进度 {i + 1}/{len(ths_codes)}，累计 {cnt}")
    print(f"ths_member: 累计 {cnt} 条")

    # ── 3. 按日接口：ths_daily / limit_list_d / hsgt / margin（最近交易日）──
    dates = recent_trade_dates(5)
    print(f"=== 按日接口（最近交易日: {dates}）===")
    for d in dates:
        for name, fn in (
            ("limit_list_d", tushare_client.limit_list_d),
            ("hsgt", tushare_client.hsgt),
            ("margin", tushare_client.margin),
            ("ths_daily", tushare_client.ths_daily),
        ):
            df = fetch_safe(fn, trade_date=d)
            n = store(name, df)
            if n:
                print(f"  {name}@{d}: {n} 条")

    print("\n✅ 补齐完成")


if __name__ == "__main__":
    main()
