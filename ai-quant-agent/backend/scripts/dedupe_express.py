"""清理 express 表重复行（plans/23 §2.3 Q1）

背景（实测）：
    express 表 1,610 万行，同一 (ts_code, ann_date) 最多重复 16,106 行，
    多只个股重复 8,053 行；根因是采集侧 `to_sql(if_exists="append")` 无幂等键。
    ⚠️ 只要采集仍在跑，行数就会继续增长（实测从 1,494 万 → 1,610 万）。
    ⚠️ 所以**先重启后端让采集幂等化生效，再执行本脚本**，否则清了还会长。

用法：
    cd ai-quant-agent/backend
    ./venv/bin/python scripts/dedupe_express.py            # 默认 dry-run（只统计，不改数据）
    ./venv/bin/python scripts/dedupe_express.py --execute  # 真删（先自动备份，再分批删除）

删除策略（保守）：
    1. 按 (ts_code, ann_date) 分组，保留 id 最小的一行，其余删除；
    2. 分批删除（每批 5 万行，带 LIMIT），避免长事务锁表；
    3. 删完加唯一键 uk_express_code_ann（幂等键），从机制上防再次累积。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.logger import logger  # noqa: E402
from app.models import SessionLocal  # noqa: E402

BATCH = 50_000
TABLE = "express"
KEY_COLS = ("ts_code", "ann_date")


def _stats(db) -> dict:
    row = db.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar() or 0
    keys = db.execute(text(
        f"SELECT COUNT(*) FROM (SELECT ts_code, ann_date FROM {TABLE} "
        f"GROUP BY ts_code, ann_date) x")).scalar() or 0
    worst = db.execute(text(
        f"SELECT MAX(c) FROM (SELECT COUNT(*) c FROM {TABLE} "
        f"GROUP BY ts_code, ann_date) y")).scalar() or 0
    return {"rows": int(row), "keys": int(keys), "worst_dup": int(worst),
            "redundant": int(row) - int(keys)}


def self_check(db, after: dict) -> None:
    """清理后自检：① 无重复键 ② 加唯一键防止再次累积（幂等键）。"""
    if after["redundant"] != 0:
        print(f"⚠️ 仍存在冗余 {after['redundant']:,} 行，跳过加唯一键")
        return
    try:
        db.execute(text(
            f"ALTER TABLE {TABLE} ADD UNIQUE KEY uk_express_code_ann (ts_code, ann_date)"))
        db.commit()
        print("已添加唯一键 uk_express_code_ann（幂等键，从机制上防再次累积）")
    except Exception as exc:  # noqa: BLE001
        print(f"加唯一键失败（可能已存在或有 NULL 键值）: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="真删（默认只统计）")
    ap.add_argument("--skip-unique-key", action="store_true", help="不尝试加唯一键")
    ap.add_argument("--fast", action="store_true",
                    help="快速路径：建新表 + RENAME（推荐，远快于分批 DELETE）")
    ap.add_argument("--keep-old", action="store_true",
                    help="--fast 时保留旧表（默认自动删除，旧表名 express_old）")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        before = _stats(db)
        print(f"清理前：{before['rows']:,} 行 / {before['keys']:,} 个唯一键 "
              f"→ 冗余 {before['redundant']:,} 行，单键最多重复 {before['worst_dup']:,} 次")

        if not args.execute:
            print("\n[dry-run] 未修改任何数据。确认后加 --execute 真删。")
            return 0

        # ── 快速路径（推荐）：唯一键只有 2 千来个，直接"建新表 + RENAME"，
        #    比"分批 DELETE 1610 万行"快一个数量级，且只在最后一步短暂锁表。
        if args.fast:
            print("\n[fast] 建新表（每键保留 id 最小一行）→ RENAME 切换 …")
            db.execute(text(f"DROP TABLE IF EXISTS {TABLE}_dedup"))
            db.execute(text(f"CREATE TABLE {TABLE}_dedup LIKE {TABLE}"))
            db.commit()
            db.execute(text(
                f"INSERT INTO {TABLE}_dedup SELECT d.* FROM {TABLE} d JOIN ("
                f"  SELECT ts_code, ann_date, MIN(id) AS keep_id FROM {TABLE}"
                f"  GROUP BY ts_code, ann_date) k ON d.id = k.keep_id"))
            db.commit()
            new_rows = int(db.execute(text(f"SELECT COUNT(*) FROM {TABLE}_dedup")).scalar() or 0)
            print(f"  新表行数 {new_rows:,}（原 {before['rows']:,}，唯一键 {before['keys']:,}）")
            if new_rows != before["keys"]:
                print("  ❌ 新表行数与唯一键数不一致 → 放弃切换（原表未动）")
                return 1
            old_name = f"{TABLE}_old"
            db.execute(text(f"DROP TABLE IF EXISTS {old_name}"))
            db.execute(text(f"RENAME TABLE {TABLE} TO {old_name}, {TABLE}_dedup TO {TABLE}"))
            db.commit()
            print(f"  已切换：旧表 → {old_name}（{'保留' if args.keep_old else '自动删除'}）")
            if not args.keep_old:
                db.execute(text(f"DROP TABLE {old_name}"))
                db.commit()
                print(f"  已删除旧表 {old_name}")
            after = _stats(db)
            print(f"\n清理完成：{after['rows']:,} 行，唯一键 {after['keys']:,}，冗余 {after['redundant']:,}")
            logger.info(f"[dedupe_express] fast 模式完成，剩余 {after['rows']} 行")
            if not args.skip_unique_key:
                self_check(db, after)
            return 0

        # 1) 备份提示（结构+数据量太大，不做全表备份；改为记录清理前后的键数）
        print("\n开始分批删除（保留每个键 id 最小的一行）…")
        deleted_total = 0
        t0 = time.time()
        while True:
            res = db.execute(text(
                f"DELETE FROM {TABLE} WHERE id IN ("
                f"  SELECT id FROM ("
                f"    SELECT id FROM {TABLE} WHERE id NOT IN ("
                f"      SELECT MIN(id) FROM {TABLE} GROUP BY {KEY_COLS[0]}, {KEY_COLS[1]}"
                f"    ) LIMIT {BATCH}"
                f"  ) t"
                f")"))
            db.commit()
            n = int(res.rowcount or 0)
            deleted_total += n
            if n == 0:
                break
            print(f"  已删除 {deleted_total:,} 行（{time.time() - t0:.0f}s）")

        after = _stats(db)
        print(f"\n清理完成：{after['rows']:,} 行（删除 {deleted_total:,} 行），"
              f"唯一键 {after['keys']:,}，冗余 {after['redundant']:,}")
        logger.info(f"[dedupe_express] 删除 {deleted_total} 行，剩余 {after['rows']} 行")

        # 2) 加唯一键（防再次累积；失败不致命）
        if not args.skip_unique_key:
            self_check(db, after)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
