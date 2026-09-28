"""一次性清理：阶段3历史遗留的空转增量任务（配合"披露日期"采集优化）。

背景：旧版阶段3增量采集在"两次报告期披露之间"会对财务类表
(income/balancesheet/cashflow/fina_indicator/top10_holders/top10_floatholders/
fina_mainbz) 为每只股票反复发起请求并得到 0 行，白白消耗 Tushare 配额。
当前队列遗留约 3.5 万个 pending 财务增量任务。本脚本仅删除这些历史遗留任务
（保留 adj_factor/weekly 等日频/周频真实数据任务）。

下次触发阶段3时，新的"披露计划日期"逻辑会自动重建精确队列：
未到预约披露日的股票不会入队（该报告期未披露，请求必空）。

用法（先停止阶段3采集，再执行）：
  cd backend && venv/bin/python scripts/cleanup_stage3_incremental.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text  # noqa: E402

from app.models import SessionLocal  # noqa: E402
from app.api.ws import (  # noqa: E402
    ensure_skip_table,
    release_collect_lock,
    try_acquire_collect_lock,
)

# 低频财务类表才会在两次披露之间反复 0 行空转；trade 类型(日频/周频真实数据)不清理
TRADE_APIS = {"adj_factor", "weekly"}
FINANCIAL_APIS = ("income", "balancesheet", "cashflow", "fina_indicator",
                  "fina_mainbz", "top10_holders", "top10_floatholders")


def main() -> None:
    ensure_skip_table()
    # 采集运行中(跨进程锁被持有)则拒绝，避免与运行中的 worker 冲突
    if not try_acquire_collect_lock():
        print("⏭️ 采集锁被占用（采集可能正在运行）。请先在数据管理页停止采集后再执行本脚本。")
        sys.exit(1)
    try:
        db = SessionLocal()
        try:
            rows = db.execute(
                text(
                    "SELECT task_key, api FROM collection_tasks "
                    "WHERE stage=3 AND status='pending' AND task_key LIKE :pat"
                ),
                {"pat": "%:incr:%"},
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            print(f"查询阶段3增量任务失败: {exc}")
            return
        finally:
            db.close()

        # 仅处理财务类空转增量任务，排除 adj_factor/weekly(真实日频/周频数据)
        fin = [(key, api) for key, api in rows if api not in TRADE_APIS]
        n = len(fin)
        if n == 0:
            print("没有遗留的 pending 财务增量任务，无需清理。")
            return

        t0 = time.time()
        db = SessionLocal()
        try:
            fin_apis = ",".join(f"'{a}'" for a in FINANCIAL_APIS)
            r = db.execute(
                text(
                    "DELETE FROM collection_tasks "
                    f"WHERE stage=3 AND status='pending' AND task_key LIKE :pat "
                    f"AND api IN ({fin_apis})"
                ),
                {"pat": "%:incr:%"},
            )
            db.commit()
            print(f"已删除 pending 财务增量任务: {r.rowcount}（保留 {len(rows) - n} 个 trade 真实增量）")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"删除失败: {exc}")
            return
        finally:
            db.close()
        print(f"完成，耗时 {time.time() - t0:.1f}s。请重启后端使披露日期逻辑生效，下次阶段3会自动重建精确队列。")
    finally:
        release_collect_lock()


if __name__ == "__main__":
    main()
