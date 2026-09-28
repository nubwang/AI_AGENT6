"""补 stk_limit 的 trade_date 索引（幂等）

故障复盘（2026-09-23 实测）
- `stk_limit` 既有索引都以 `ts_code` 为前导列（uk_stk_limit_ts_date /
  idx_stk_limit_fixed_code_date），缺少以 `trade_date` 为前导的索引。
- 于是 `SELECT MAX(trade_date) FROM stk_limit` 只能全索引扫描 1690 万行 → **单条 43s**
  （两轮实测均稳定复现，非冷缓存问题）。
- 该查询位于进化中心的 L0 数据链路探针
  （`app/agents/evolution_summarizer._data_chain_probe`），由
  `GET /api/v1/system/evolve/status` → `evolution_guard.guard_scan()` → `build_l0_summary()`
  调用；冷启动时把进化中心整页 17 个并发请求一起拖到前端 axios 30s 超时
  （浏览器报 `timeout of 30000ms exceeded`）。
- 同一条慢查询也让 `WHERE trade_date = 'YYYYMMDD'` 的等值统计（回测数据门禁的
  覆盖率抽查）从毫秒级退化到几十秒。

修复
- 加索引 `idx_stk_limit_date (trade_date)`：`MAX(trade_date)` 变为覆盖索引查找（0.19s），
  等值统计变为 covering index lookup（0.005s）。
- MySQL 8 的 ADD INDEX 属于在线 DDL（INPLACE，允许并发 DML），实测 1690 万行约 237s。

用法
    cd ai-quant-agent/backend
    ./venv/bin/python scripts/migrate_stk_limit_date_index.py            # 只检查/演练
    ./venv/bin/python scripts/migrate_stk_limit_date_index.py --execute  # 真正建索引
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# 允许 `./venv/bin/python scripts/migrate_stk_limit_date_index.py` 直接运行
# （项目脚本统一引导，见 scripts/migrate_adj_factor_unique.py）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.models import SessionLocal  # noqa: E402

TABLE = "stk_limit"
INDEX = "idx_stk_limit_date"
COL = "trade_date"


def _index_exists(db) -> bool:
    n = db.execute(text(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema=DATABASE() AND table_name=:t AND index_name=:i"),
        {"t": TABLE, "i": INDEX}).scalar()
    return bool(n)


def _probe_ms(db, sql: str) -> tuple[float, object]:
    t0 = time.time()
    v = db.execute(text(sql)).scalar()
    return round((time.time() - t0) * 1000, 1), v


def main() -> int:
    ap = argparse.ArgumentParser(description="补 stk_limit.trade_date 索引（幂等）")
    ap.add_argument("--execute", action="store_true",
                    help="实际执行建索引（默认只检查并演练）")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        # 1) 现状：这条 MAX 查询就是"进化中心超时"的根源，先量出基线
        ms, v = _probe_ms(db, f"SELECT MAX({COL}) FROM {TABLE}")
        print(f"[baseline] SELECT MAX({COL}) FROM {TABLE} → {v}（耗时 {ms} ms）")

        if _index_exists(db):
            print(f"[skip] {TABLE}.{INDEX} 已存在，无需处理")
            return 0

        rows = db.execute(text(
            "SELECT table_rows FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name=:t"), {"t": TABLE}).scalar()
        print(f"[info] {TABLE} 约 {int(rows or 0):,} 行；缺少 {INDEX}({COL})")

        if not args.execute:
            print("[dry-run] 未做任何改动。加 --execute 才会建索引"
                  "（在线 DDL，预计 3~5 分钟，期间可正常读写）")
            return 0

        # 2) 建索引（MySQL 8 在线 DDL：INPLACE，不阻塞并发 DML）
        t0 = time.time()
        db.execute(text(f"ALTER TABLE {TABLE} ADD INDEX {INDEX} ({COL})"))
        db.commit()
        print(f"[ok] {INDEX} 创建完成，耗时 {time.time() - t0:.0f}s")

        # 3) 复核：MAX 查询与等值统计都应回到毫秒级
        ms2, v2 = _probe_ms(db, f"SELECT MAX({COL}) FROM {TABLE}")
        print(f"[verify] SELECT MAX({COL}) FROM {TABLE} → {v2}（耗时 {ms2} ms）")
        sql_d = (f"SELECT COUNT(*) FROM {TABLE} WHERE {COL} = "
                 f"'{(str(v2) or '')[:4]}-{(str(v2) or '')[4:6]}-{(str(v2) or '')[6:8]}'")
        ms3, v3 = _probe_ms(db, sql_d)
        print(f"[verify] 等值统计同日行数 → {v3}（耗时 {ms3} ms）")
        print("[done] 进化中心 `/system/evolve/status` 冷启动应回到秒级；"
              "回测数据门禁的覆盖率抽查也不再卡顿")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
