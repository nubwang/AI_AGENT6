"""迁移：`adj_factor` 去重 + 加 `(ts_code, trade_date)` 唯一键（2026-09-21）

## 为什么要做（背景，均已实测）

    · `adj_factor` 的 PRIMARY KEY 是**自增 `id`**，`(ts_code, trade_date)` 只是**普通索引**
      ⇒ `INSERT ... ON DUPLICATE KEY UPDATE` **永远不触发** ⇒ 它是**纯 INSERT**
      ⇒ 任何"补数/增量"脚本都会**静默产生重复行**
      （`app/api/ws.py:673` 早记录过同类事故：express 表 1,494 万行冗余就是这么来的）；
    · 实测：`adj_factor` 有 **552,000 组重复**（每组 2 行），
      **重复组内 `adj_factor` 值 100% 一致**（不一致组 = 0）⇒ **去重无损**；
    · `daily` / `daily_basic` 实测 **0 重复**（它们不在此脚本范围内，另行处理）。

## 本脚本做什么（**顺序不可调换**）

    ① **守卫**：先复验"重复组的 adj_factor 值完全一致" —— 任一不一致就**中止，不删任何行**；
    ② **去重**：按年份分批 `DELETE a ... JOIN b ... AND a.id > b.id`（保留最小 id），
       每批 commit（避免大事务撑爆 undo）；每批前后核对行数；
    ③ **加唯一键**：`ALTER TABLE adj_factor DROP INDEX idx_ts_code, ADD UNIQUE KEY uk_ts_date (ts_code, trade_date)`
       （原 `idx_ts_code` 就是 `(ts_code, trade_date)` 复合索引，改为 UNIQUE 即可，**不新增索引体积**）；
    ④ **实测幂等性**：对一条已存在的 (ts_code, trade_date) 做 upsert，**总行数必须不变**
       —— 这是"以后补数不会再重复"的**功能性证明**。

## ⚠️ 关于唯一键的口径

用户指示为「以股票代码为唯一约束」。**单列 `UNIQUE(ts_code)` 不可行**（一只股票有 ~6000 个交易日，
必然冲突）⇒ 本脚本按**自然键 `(ts_code, trade_date)`**（股票代码 + 日期）实施。
若原意不同，请回退本脚本的第 ③ 步（`DROP INDEX uk_ts_date, ADD INDEX idx_ts_code (...)`）。

## 运行

    cd ai-quant-agent/backend && ./venv/bin/python scripts/migrate_adj_factor_unique.py --dry-run
    cd ai-quant-agent/backend && ./venv/bin/python scripts/migrate_adj_factor_unique.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.core.logger import logger  # noqa: E402
from app.models import SessionLocal  # noqa: E402

TABLE = "adj_factor"
YEARS = list(range(2001, 2027))
UK_NAME = "uk_ts_date"
OLD_INDEX = "idx_ts_code"


def _total(db) -> int:
    return int(db.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar() or 0)


def _dup_groups(db) -> int:
    return int(db.execute(text(
        f"SELECT COUNT(*) FROM (SELECT ts_code, trade_date FROM {TABLE} "
        f"GROUP BY ts_code, trade_date HAVING COUNT(*) > 1) x")).scalar() or 0)


def _mismatched_groups(db) -> int:
    """重复组内 `adj_factor` 值不一致的组数（**必须为 0 才允许删**）。"""
    return int(db.execute(text(
        f"SELECT COUNT(*) FROM (SELECT ts_code, trade_date FROM {TABLE} "
        f"GROUP BY ts_code, trade_date HAVING COUNT(*) > 1 "
        f"AND COUNT(DISTINCT adj_factor) > 1) x")).scalar() or 0)


def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser(description="adj_factor 去重 + 唯一键")
    ap.add_argument("--dry-run", action="store_true", help="只审计，不改动")
    args = ap.parse_args()
    t0 = time.time()

    print("=" * 100)
    print("迁移：`adj_factor` 去重 + 加 `(ts_code, trade_date)` 唯一键")
    print("=" * 100)
    with SessionLocal() as db:
        print("① 守卫：复验重复组的值一致性（**不一致就中止**）")
        print("-" * 100)
        tot0 = _total(db)
        dup0 = _dup_groups(db)
        mis0 = _mismatched_groups(db)
        print(f"  总行数 {tot0:,} · 重复键组 {dup0:,} · **值不一致的组 {mis0}**")
        if mis0:
            print(f"  ❌ 有 {mis0} 组重复行的 adj_factor 值不同 ⇒ **中止，不删任何行**")
            print("     需要人工判断该保留哪一个值。")
            return 1
        if not dup0:
            print("  ✅ 无重复键 ⇒ 跳过去重，直接尝试加唯一键")
        print("  ✅ 守卫通过：去重**无损**（同键多行的值完全相同）")
        print()
        if args.dry_run:
            print("  [dry-run] 未做任何改动。")
            return 0

        if dup0:
            print("② 去重（按年份分批，保留最小 id）")
            print("-" * 100)
            deleted_total = 0
            for y in YEARS:
                a, b = f"{y}0101", f"{y}1231"
                before = _total(db)
                try:
                    r = db.execute(text(
                        f"DELETE a FROM {TABLE} a JOIN {TABLE} b "
                        f"ON a.ts_code = b.ts_code AND a.trade_date = b.trade_date "
                        f"AND a.id > b.id "
                        f"WHERE a.trade_date BETWEEN :a AND :b"), {"a": a, "b": b})
                    db.commit()
                    d = int(r.rowcount or 0)
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    print(f"  {y}  ❌ 失败：{str(exc)[:90]}")
                    print("     ⇒ 中止（已提交的年份保持，未提交的回滚）")
                    return 1
                deleted_total += d
                if d:
                    print(f"  {y}  删除 {d:>6} 行  （{before:,} → {_total(db):,}）")
            logger.info(f"[migrate_adj] 去重完成，共删 {deleted_total} 行")
            print(f"\n  合计删除 **{deleted_total:,}** 行；预期 = {dup0:,}（差 {deleted_total - dup0}）")
            print()
            print("③ 去重后复验")
            print("-" * 100)
            tot1 = _total(db)
            dup1 = _dup_groups(db)
            print(f"  总行数 {tot0:,} → {tot1:,}（应 = {tot0 - dup0:,}）")
            print(f"  重复键组 {dup0:,} → {dup1}（应为 0）")
            if dup1 or tot1 != tot0 - dup0:
                print("  ⚠️ 与预期不符 ⇒ **不加唯一键**，请人工核对")
                return 1
            print("  ✅ 去重干净")
            print()

        print("④ 加唯一键（原普通复合索引 `idx_ts_code` → UNIQUE `uk_ts_date`）")
        print("-" * 100)
        for stmt in (f"ALTER TABLE {TABLE} DROP INDEX {OLD_INDEX}",
                     f"ALTER TABLE {TABLE} ADD UNIQUE KEY {UK_NAME} (ts_code, trade_date)"):
            try:
                db.execute(text(stmt))
                db.commit()
                print(f"  ✅ {stmt}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"  ❌ {stmt}\n     {str(exc)[:120]}")
                return 1
        print()

        print("⑤ 索引现状")
        print("-" * 100)
        for r in db.execute(text(f"SHOW INDEX FROM {TABLE}")).fetchall():
            print(f"  {r[2]:<14} seq={r[3]} 列={r[4]} unique={'YES' if r[1] == 0 else 'no'}")
        print()

        print("⑥ ★ 实测幂等性（证明「以后补数不会再重复」）")
        print("-" * 100)
        row = db.execute(text(
            f"SELECT ts_code, trade_date, adj_factor FROM {TABLE} ORDER BY id DESC LIMIT 1")).fetchone()
        n_before = _total(db)
        db.execute(text(
            f"INSERT INTO {TABLE} (ts_code, trade_date, adj_factor) VALUES (:c,:d,:f) "
            f"ON DUPLICATE KEY UPDATE adj_factor = VALUES(adj_factor)"),
            {"c": row[0], "d": row[1], "f": row[2]})
        db.commit()
        n_after = _total(db)
        dups_after = _dup_groups(db)
        print(f"  对已有键 ({row[0]} / {row[1]}) 做 upsert：")
        print(f"    总行数 {n_before:,} → {n_after:,}（**应不变**）· 重复键组 {dups_after}（应为 0）")
        ok = (n_before == n_after) and (dups_after == 0)
        print(f"  ⇒ {'✅ 幂等成立：ON DUPLICATE KEY UPDATE 生效' if ok else '❌ 幂等未成立，请人工检查'}")
        print()
        print(f"  用时 {time.time() - t0:.0f}s")
    print("\n⚠️ 后续：`daily` / `daily_basic` 实测 0 重复，可另行加同样的唯一键（表更大，耗时更长）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
