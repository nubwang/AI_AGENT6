"""修复 stk_limit 列标签调换（2026-09-20 实测发现）

**症状（回测被数据门禁拦下时暴露）**
```
核心表日期不一致：最新 920992.B，落后 daily(20260918), adj_factor(20260918), daily_basic(20260918)
```
`920992.B` 不是日期，是**北交所代码** `920992.BJ` 的字典序最大值 → 说明 `trade_date` 列里装的是 ts_code。
实测确认：
```
ts_code   | trade_date | up_limit | down_limit
20100104  | 000001.SZ  |   26.81  |   21.93      ← 两列标签互换（行内数据是对的）
```
16,989,354 行里 `trade_date LIKE '%.%'`（含点，即代码）占 100%，`trade_date REGEXP '^[0-9]{8}$'` 占 0%。

**成因**：Q2 那次 `stk_limit` 重建（去重）时键顺序被写成 `(trade_date, ts_code)`，
新表就按这个顺序建列 → 列标签调换；之后所有按 `trade_date` 的查询/门禁都读到代码。

**本脚本**：把两列**标签换回来**（无损，行内配对关系不变），并做前后强校验；幂等（已修则跳过）。
注意：**不做任何"猜测式修补"**——只交换标签，不动 up_limit/down_limit 数值。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/fix_stk_limit_columns.py --dry-run
    cd ai-quant-agent/backend && ./venv/bin/python scripts/fix_stk_limit_columns.py --execute
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.core.logger import logger
from app.models import SessionLocal

TABLE = "stk_limit"
NEW = "stk_limit_fixed"
BAK = "stk_limit_swapped_bak"
COL_FIXED = ["ts_code", "trade_date", "up_limit", "down_limit"]
# 列健康判定阈值（容忍极少量脏值，但绝不能是"整体调换"）
CODE_RATIO = 0.95   # ts_code 列里"含点"的比例（代码形如 000001.SZ / 920992.BJ）
DATE_RATIO = 0.95   # trade_date 列里"是 8 位数字"的比例


def _col_stats(db, table: str) -> dict:
    """统计两列各自的"像代码/像日期"比例（用小样本即可判定是否整体调换）。"""
    n = int(db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
    if n == 0:
        return {"rows": 0}
    row = db.execute(text(
        f"SELECT SUM(ts_code LIKE '%.%'), SUM(ts_code REGEXP '^[0-9]{{8}}$'), "
        f"       SUM(trade_date LIKE '%.%'), SUM(trade_date REGEXP '^[0-9]{{8}}$'), "
        f"       COUNT(DISTINCT ts_code), COUNT(DISTINCT trade_date) "
        f"FROM {table}")).fetchone()
    code_in_ts, date_in_ts, code_in_td, date_in_td, d_ts, d_td = [int(x or 0) for x in row]
    return {
        "rows": n,
        "ts_code_is_code": code_in_ts / n,
        "ts_code_is_date": date_in_ts / n,
        "trade_date_is_code": code_in_td / n,
        "trade_date_is_date": date_in_td / n,
        "distinct_ts_code": d_ts,
        "distinct_trade_date": d_td,
    }


def _verdict(s: dict) -> str:
    """'swapped' / 'ok' / 'unknown'（不猜：无法判定就不动数据）。"""
    if not s.get("rows"):
        return "unknown"
    if s["trade_date_is_code"] >= CODE_RATIO and s["ts_code_is_date"] >= DATE_RATIO:
        return "swapped"
    if s["ts_code_is_code"] >= CODE_RATIO and s["trade_date_is_date"] >= DATE_RATIO:
        return "ok"
    return "unknown"


def _fmt(s: dict) -> str:
    if not s.get("rows"):
        return "空表"
    return (f"行数 {s['rows']:,}；ts_code 像代码 {s['ts_code_is_code']:.1%}/像日期 {s['ts_code_is_date']:.1%}；"
            f"trade_date 像代码 {s['trade_date_is_code']:.1%}/像日期 {s['trade_date_is_date']:.1%}；"
            f"distinct ts_code={s['distinct_ts_code']:,} trade_date={s['distinct_trade_date']:,}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default=TABLE)
    ap.add_argument("--dry-run", action="store_true", default=False)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--keep-bak", action="store_true", help="保留旧表（默认校验通过后删除）")
    args = ap.parse_args()
    table = args.table

    db = SessionLocal()
    try:
        before = _col_stats(db, table)
        print(f"[前] {table}: {_fmt(before)}")
        v = _verdict(before)
        if v == "ok":
            print("✅ 列标签正常，无需修复（幂等退出）")
            return 0
        if v == "unknown":
            print("❌ 无法判定（既不像正常也不像整体调换）→ **不动数据**，需人工核查。")
            return 1
        print("⚠️ 判定：两列标签已调换（trade_date 装代码、ts_code 装日期）→ 可无损换回")

        if args.dry_run or not args.execute:
            print("\n[dry-run] 未修改数据。加 --execute 执行。")
            return 0

        free_gb = os.statvfs("/").f_bavail * os.statvfs("/").f_frsize / 1024 ** 3
        need_gb = before["rows"] * 60 / 1024 ** 3
        print(f"空间预检：可用 {free_gb:.1f}G ≥ 需要 {need_gb:.1f}G ? {'OK' if free_gb >= need_gb else '不足'}")
        if free_gb < need_gb:
            print("❌ 空间不足，已中止（避免写坏数据）")
            return 1

        t0 = time.time()
        db.execute(text(f"DROP TABLE IF EXISTS {NEW}"))
        db.execute(text(
            f"CREATE TABLE {NEW} ("
            f"  ts_code VARCHAR(20) NOT NULL,"
            f"  trade_date VARCHAR(10) NOT NULL,"
            f"  up_limit DOUBLE,"
            f"  down_limit DOUBLE,"
            f"  KEY idx_{NEW}_code_date (ts_code, trade_date)"
            f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))
        db.commit()
        print(f"  建新表 {NEW}（正确列序：ts_code, trade_date, up_limit, down_limit）")

        # 关键：显式列名 + 显式交换位置（绝不依赖"位置插入"）
        db.execute(text(
            f"INSERT INTO {NEW} (ts_code, trade_date, up_limit, down_limit) "
            f"SELECT trade_date, ts_code, up_limit, down_limit FROM {table}"))
        db.commit()
        print(f"  数据搬运完成（{time.time() - t0:.0f}s）")

        after = _col_stats(db, NEW)
        print(f"[后] {NEW}: {_fmt(after)}")
        ok_rows = after["rows"] == before["rows"]
        ok_fmt = after.get("ts_code_is_code", 0) >= CODE_RATIO and after.get("trade_date_is_date", 0) >= DATE_RATIO
        print(f"  校验：行数一致={ok_rows}（{before['rows']:,} vs {after['rows']:,}），格式正确={ok_fmt}")
        if not (ok_rows and ok_fmt):
            print("❌ 校验不通过 → 放弃切换（原表未动）")
            db.execute(text(f"DROP TABLE IF EXISTS {NEW}"))
            db.commit()
            return 1

        db.execute(text(f"DROP TABLE IF EXISTS {BAK}"))
        db.execute(text(f"RENAME TABLE {table} TO {BAK}, {NEW} TO {table}"))
        db.commit()
        print(f"  ✅ 已切换：{table}（正确列序）／旧表 → {BAK}")

        if not args.keep_bak:
            db.execute(text(f"DROP TABLE {BAK}"))
            db.commit()
            print(f"  已删除旧表 {BAK}（如需保留请加 --keep-bak）")
        logger.info(f"[fix_stk_limit_columns] 修复完成：{before['rows']:,} 行，列标签换回")
        print(f"\n完成：{before['rows']:,} 行修复（{time.time() - t0:.0f}s）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
