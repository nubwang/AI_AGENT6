"""通用表去重（plans/23 §2.3 Q1/Q2）—— 可指定任意表/键，不依赖 id 列

背景：采集侧 `to_sql(if_exists="append")` 无幂等键 → 重复采集即重复插入。
  - express  ：实测 1,610 万行 → 去重后 2,008 行（99.99% 冗余），已修复
  - stk_limit：个别交易日行数超过全市场股票数（20230921 达 13,228 行）→ 疑似重复行

设计要点（实测踩坑后修正）：
  1. **不做重量级预统计**：`COUNT(DISTINCT ts_code, trade_date)` 在千万级表上会跑很久
     （曾把脚本卡死）。改为用 `information_schema.table_rows` 近似 + 建完新表后再精确计数。
  2. **无 id 列也能去重**：按 `keys` 分组取 `MAX(其余列)`（同键其余值本就相同，等价）。
  3. **按月分批插入**：避免 `ROW_NUMBER()` 大排序把 MySQL 临时目录写满（实测 errno 28）。
  4. **写前空间预检** + **行数校验**：校验不通过就放弃切换，原表不动。

用法：
    cd ai-quant-agent/backend
    ./venv/bin/python scripts/dedupe_table.py --table stk_limit --execute --fast
    ./venv/bin/python scripts/dedupe_table.py --table stk_limit --execute --fast --keep-old
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.logger import logger  # noqa: E402
from app.models import SessionLocal  # noqa: E402

KEY_PRESETS: dict[str, tuple[str, ...]] = {
    "express": ("ts_code", "ann_date"),
    "stk_limit": ("ts_code", "trade_date"),
    "daily": ("ts_code", "trade_date"),
    "daily_basic": ("ts_code", "trade_date"),
    "moneyflow": ("ts_code", "trade_date"),
    "suspend_d": ("ts_code", "trade_date"),
    "adj_factor": ("ts_code", "trade_date"),
    "weekly": ("ts_code", "trade_date"),      # 实测有重复（'000563.SZ-20260731'）
    "monthly": ("ts_code", "trade_date"),
}


DATE_COL_CANDIDATES = ("trade_date", "ann_date", "end_date", "ex_date", "start_date",
                       "cal_date", "f_ann_date", "report_date")


def _pick_date_col(keys: tuple[str, ...], override: str = "") -> str:
    """确定"用于按月分批"的日期列。

    ⚠️ 血泪教训（2026-09-20）：早期版本**硬编码 `trade_date`**，并且按月分批 + 建新表时
    依赖列顺序，结果 `stk_limit` 上把 `ts_code`/`trade_date` 两个**列标签调换**了
    （新表按 keys 顺序建列），直到回测数据门禁报出"最新 920992.B"才被发现。
    现在：① 日期列显式传入/自动识别（不再假设叫 trade_date）；② 重建后做列语义校验。
    """
    if override:
        return override
    for c in keys:
        if c in DATE_COL_CANDIDATES:
            return c
    return "trade_date"


def _column_sanity(db, table: str, date_col: str) -> tuple[bool, str]:
    """重建后的**列语义校验**：日期列必须像日期，ts_code 必须像代码。

    这是防"列错位/标签调换"的最后一道闸：不看代码怎么写，只看数据长什么样。
    返回 (ok, detail)。
    """
    try:
        row = db.execute(text(
            f"SELECT COUNT(*), "
            f"  SUM(`{date_col}` REGEXP '^[0-9]{{8}}$'), "
            f"  SUM(`{date_col}` LIKE '%.%'), "
            f"  SUM(`ts_code` LIKE '%.%'), "
            f"  SUM(`ts_code` REGEXP '^[0-9]{{8}}$') "
            f"FROM (SELECT `{date_col}`, `ts_code` FROM {table} LIMIT 200000) t")).fetchone()
        n, d_ok, d_code, c_ok, c_date = [int(x or 0) for x in row]
        if n == 0:
            return False, "抽样为空"
        d_ratio, c_ratio = d_ok / n, c_ok / n
        detail = (f"抽样 {n:,} 行：{date_col} 像日期 {d_ratio:.1%}（像代码 {d_code / n:.1%}）；"
                  f"ts_code 像代码 {c_ratio:.1%}（像日期 {c_date / n:.1%}）")
        if d_ratio < 0.95 or c_ratio < 0.95:
            return False, detail + " → 列语义异常（疑似列错位/标签调换），已放弃切换"
        return True, detail
    except Exception as exc:  # noqa: BLE001
        return False, f"列语义校验失败（表结构不含 ts_code？）: {str(exc)[:120]}"


def _approx_rows(db, table: str) -> int:
    """近似行数（information_schema，O(1)，不扫表）。"""
    return int(db.execute(text(
        "SELECT table_rows FROM information_schema.tables "
        "WHERE table_schema=DATABASE() AND table_name=:t"), {"t": table}).scalar() or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True)
    ap.add_argument("--keys", default="", help="逗号分隔；缺省按预设")
    ap.add_argument("--execute", action="store_true", help="真执行（默认 dry-run）")
    ap.add_argument("--fast", action="store_true", help="建新表 + RENAME（推荐）")
    ap.add_argument("--keep-old", action="store_true", help="保留旧表（默认删除）")
    ap.add_argument("--no-unique-key", action="store_true", help="不加唯一键")
    ap.add_argument("--mode", choices=["rebuild", "delete"], default="rebuild",
                    help="rebuild=建新表+切换（默认，适合重复量大）；"
                         "delete=**只删重复行**（保留每组 MIN(id)，需自增 id 列）")
    ap.add_argument("--date-col", default="", help="用于按月分批的日期列（缺省自动识别）")
    args = ap.parse_args()

    table = args.table
    keys = tuple(k.strip() for k in args.keys.split(",") if k.strip()) or KEY_PRESETS.get(table, ())
    if not keys:
        print(f"❌ 未指定 --keys，且 {table} 无预设键")
        return 1

    db = SessionLocal()
    try:
        approx = _approx_rows(db, table)
        print(f"表 {table}，去重键 {keys}，近似行数 {approx:,}（information_schema）")

        free_gb = shutil.disk_usage("/").free / 1024 ** 3
        need_gb = max(1.0, approx * 60 / 1024 ** 3)
        print(f"空间预检：可用 {free_gb:.1f}G ≥ 需要 {need_gb:.1f}G ? {'OK' if free_gb >= need_gb else '不足'}")
        if free_gb < need_gb:
            print("❌ 空间不足，已中止（避免写坏数据）")
            return 1

        if not args.execute:
            print("\n[dry-run] 未修改数据。加 --execute --fast 执行。")
            return 0

        cols = [r[0] for r in db.execute(text(f"SHOW COLUMNS FROM {table}"))]
        other_cols = [c for c in cols if c not in keys]
        # 关键修复：目标列顺序必须与 SELECT 的表达式顺序**逐一对齐**，
        # 否则（老版本用 SHOW COLUMNS 全列 vs keys+others）就会出现"值按位置写进别的列"
        # → 列标签调换（stk_limit 就是这么被写坏的）。
        sel_cols = list(keys) + other_cols
        collist = ", ".join(f"`{c}`" for c in sel_cols)
        key_expr = ", ".join(f"`{c}`" for c in keys)
        date_col = _pick_date_col(keys, args.date_col)
        if date_col not in cols:
            print(f"❌ 日期列 {date_col} 不在表里（现有列：{cols}）→ 请用 --date-col 指定")
            return 1
        print(f"  日期列 = {date_col}（按月分批）；目标列序 = {sel_cols}")

        # ★ 轻量模式（plans/24 §11.36.13b）：只**删除重复行**（保留每组 MIN(id)），不重建表。
        #   为什么需要它：实测 `moneyflow` 真实 1404.8 万行里**只有 5061 行重复（0.036%）**，
        #   为此重建整表 = 33 分钟 + 一倍磁盘空间，**完全不划算**（而且会因估算误差被误判丢弃）。
        #   重复量大时仍应用默认 rebuild 模式。
        if args.mode == "delete":
            if "id" not in cols:
                print(f"❌ delete 模式需要自增主键 `id`（{table} 没有）→ 请用默认 rebuild 模式")
                return 1
            key_sel = ", ".join(f"`{c}`" for c in keys)
            join_sql = " AND ".join(f"t.`{c}` = d.`{c}`" for c in keys)
            months_d = [r[0] for r in db.execute(text(
                f"SELECT DISTINCT LEFT(`{date_col}`, 6) m FROM {table} "
                f"WHERE `{date_col}` IS NOT NULL ORDER BY m"))]
            print(f"\n[delete] 按 {len(months_d)} 个月分批删除重复行（保留每组 MIN(id)）…")
            t0 = time.time()          # ← 该变量原本只在 rebuild 分支里定义
            total_del = 0
            for i, m in enumerate(months_d):
                res = db.execute(text(
                    f"DELETE t FROM {table} t JOIN ("
                    f"  SELECT {key_sel}, MIN(id) AS keep_id FROM {table} "
                    f"  WHERE `{date_col}` LIKE :m GROUP BY {key_sel} HAVING COUNT(*) > 1"
                    f") d ON {join_sql} AND t.id > d.keep_id"), {"m": f"{m}%"})
                db.commit()
                total_del += int(res.rowcount or 0)
                if (i + 1) % 40 == 0 or (i + 1) == len(months_d):
                    print(f"    进度 {i + 1}/{len(months_d)} 月，已删 {total_del:,} 行"
                          f"（{time.time() - t0:.0f}s）")
            print(f"  删除重复 {total_del:,} 行")
            if not args.no_unique_key:
                try:
                    db.execute(text(f"ALTER TABLE {table} ADD UNIQUE KEY uk_{table}_key ({key_sel})"))
                    db.commit()
                    print(f"  已添加唯一键 uk_{table}_key（幂等键）")
                except Exception as exc:  # noqa: BLE001
                    print(f"  加唯一键失败（可能已存在 / 仍有残留重复）: {str(exc)[:160]}")
            logger.info(f"[dedupe_table:delete] {table} 删除 {total_del}")
            return 0

        print(f"\n[fast] 建新表 → 按月分批插入（键外列取 MAX，等价）→ 校验 → RENAME …")
        t0 = time.time()
        db.execute(text(f"DROP TABLE IF EXISTS {table}_dedup"))
        db.execute(text(f"CREATE TABLE {table}_dedup LIKE {table}"))
        db.commit()

        if other_cols:
            agg = ", ".join(f"MAX(`{c}`) AS `{c}`" for c in other_cols)
            sel = ", ".join(f"`{c}`" for c in keys) + ", " + agg
        else:
            sel = ", ".join(f"`{c}`" for c in keys)

        months = [r[0] for r in db.execute(text(
            f"SELECT DISTINCT LEFT(`{date_col}`, 6) m FROM {table} "
            f"WHERE `{date_col}` IS NOT NULL ORDER BY m"))]
        total_m = len(months)
        for i, m in enumerate(months):
            db.execute(text(
                f"INSERT INTO {table}_dedup ({collist}) SELECT {sel} FROM {table} "
                f"WHERE `{date_col}` LIKE :m GROUP BY {key_expr}"), {"m": f"{m}%"})
            db.commit()
            if (i + 1) % 20 == 0 or (i + 1) == total_m:
                print(f"    进度 {i + 1}/{total_m} 月（{time.time() - t0:.0f}s）")

        # ★ 行数校验必须用**真实** COUNT(*)（plans/24 §11.36.13b）：
        #   `information_schema.TABLE_ROWS` 是 InnoDB **估算值**（抽样统计，会偏低）。
        #   实测踩到：moneyflow 估 1367 万、真实更多 ⇒ 新表 1404 万被判定为"比原表还多"，
        #   **把完全正确的去重结果 DROP 掉，33 分钟白跑**。估算值只配用来做空间预检，不配做校验。
        old_exact = int(db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
        new_rows = int(db.execute(text(f"SELECT COUNT(*) FROM {table}_dedup")).scalar() or 0)
        print(f"  真实行数：原表 {old_exact:,} → 新表 {new_rows:,}"
              f"（去重 {old_exact - new_rows:,} 行，耗时 {time.time() - t0:.0f}s）")
        print(f"  （information_schema 估算 {approx:,}，相对真实 {approx - old_exact:+,}）")
        if new_rows <= 0 or new_rows > old_exact:
            print("  ❌ 行数校验不通过（新表为空、或**比原表还多**——后者只可能是分组键/分批逻辑出错）")
            print(f"     → 放弃切换，**保留 `{table}_dedup` 供人工核查**（原表未动；确认后可手动 DROP）")
            return 1

        # ★ 月份覆盖校验（plans/24 §11.36.13，血泪新增）：
        #   原校验**只拦"行数变多"**，而"行数变少"既可能是正常去重，也可能是**脚本没跑完**
        #   （实测：moneyflow 按时长分批写到 2010 年就被 kill，新表只有 225 万行 vs 原表 1367 万，
        #    2024/2025/2026 三个月是 **0 行** —— 若此时校验通过并 RENAME，就把近年数据"去"没了）。
        #   所以必须比对**日期覆盖的月份数**：新表月份少于原表 ⇒ 判定为"未完成"，放弃切换。
        try:
            m_old = int(db.execute(text(
                f"SELECT COUNT(DISTINCT LEFT(`{date_col}`, 6)) FROM {table} "
                f"WHERE `{date_col}` IS NOT NULL")).scalar() or 0)
            m_new = int(db.execute(text(
                f"SELECT COUNT(DISTINCT LEFT(`{date_col}`, 6)) FROM {table}_dedup "
                f"WHERE `{date_col}` IS NOT NULL")).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            m_old = m_new = -1
            print(f"  ! 月份覆盖校验查询失败（按保守处理）: {str(exc)[:120]}")
        print(f"  月份覆盖：原表 {m_old} 月 / 新表 {m_new} 月")
        if m_old >= 0 and m_new < m_old:
            print(f"  ❌ 月份覆盖不全（新 {m_new} < 原 {m_old}）⇒ **判定为写入未完成**，放弃切换（原表未动）")
            db.execute(text(f"DROP TABLE IF EXISTS {table}_dedup"))
            db.commit()
            return 1

        # 列语义校验（防列错位/标签调换——这类问题不会报错，只会静默毁数据）
        ok, detail = _column_sanity(db, f"{table}_dedup", date_col)
        print(f"  列语义校验：{'✅' if ok else '❌'} {detail}")
        if not ok:
            print("  → 放弃切换（原表未动）")
            db.execute(text(f"DROP TABLE IF EXISTS {table}_dedup"))
            db.commit()
            return 1

        old = f"{table}_old"
        db.execute(text(f"DROP TABLE IF EXISTS {old}"))
        db.execute(text(f"RENAME TABLE {table} TO {old}, {table}_dedup TO {table}"))
        db.commit()
        print(f"  ✅ 已切换（旧表 → {old}，{'保留' if args.keep_old else '删除'}）")
        if not args.keep_old:
            db.execute(text(f"DROP TABLE {old}"))
            db.commit()
        print(f"清理完成：{approx:,} → {new_rows:,} 行（减少 {max(0, approx - new_rows):,}）")

        if not args.no_unique_key:
            try:
                db.execute(text(
                    f"ALTER TABLE {table} ADD UNIQUE KEY uk_{table}_key ({key_expr})"))
                db.commit()
                print(f"已添加唯一键 uk_{table}_key（幂等键）")
            except Exception as exc:  # noqa: BLE001
                print(f"加唯一键失败（可能已存在）: {str(exc)[:120]}")
        logger.info(f"[dedupe_table] {table} {approx} → {new_rows}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
