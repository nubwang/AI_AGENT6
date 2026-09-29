"""补齐 stage2 按日表的「半截日」—— 走**正常采集链路**，不写任何裸 SQL

## 为什么要有这个脚本

`app/api/ws.py::stage2_daily()` 的增量判据曾是 `date <= MAX(trade_date)`，
而"半截日"（如 `stk_limit` 最新日只有沪市 2361 行 / 正常日 5647 行）的
MAX 已经等于最新交易日 ⇒ 这些残缺日**永远不在重建集合里** ⇒ **残缺永不自愈**。
现在 `stage2_incomplete_days()` 已修复该缺口（下次采集会自动重排），
本脚本用于**立即补齐**，不必等下一轮采集。

## 口径纪律（照抄 `diagnose_anchor_gap.py` 的血泪教训）

    1. **只走正常链路**：`DataCollector.collect_and_store()`（`INSERT ... ON DUPLICATE
       KEY UPDATE`，幂等 upsert）。它正是 `ws.py` 采集消费函数所用的同一条路径。
    2. **不写任何裸 SQL、不做 dedupe、不删行** —— 本仓库已发生过两次写库事故
       （express 1,494 万行冗余 / adj_factor 误插 16,695 行重复）。
    3. 默认 **dry-run**，只列清单；必须显式 `--apply` 才发请求与写库。
    4. 补齐前后都数一遍行数，**必须看到行数上升**才算成功（否则就是"跑了但一行没变"，
       即 `collect_and_store` docstring 记录过的静默丢数据模式）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/heal_halfday_tables.py
    cd ai-quant-agent/backend && ./venv/bin/python scripts/heal_halfday_tables.py --apply
    cd ai-quant-agent/backend && ./venv/bin/python scripts/heal_halfday_tables.py --apply --api stk_limit
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.api.ws import STAGE2_BASE_ROWS, stage2_incomplete_days  # noqa: E402
from app.data.collector import DataCollector  # noqa: E402
from app.data.rate_limiter import slimiter  # noqa: E402
from app.models import SessionLocal  # noqa: E402


def rows_on(api: str, d: str) -> int:
    with SessionLocal() as db:
        return int(db.execute(
            text(f"SELECT COUNT(*) FROM {api} WHERE trade_date = :d"), {"d": d}
        ).scalar() or 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐 stage2 按日表的半截日（正常采集链路）")
    ap.add_argument("--apply", action="store_true", help="真正发请求并写库（默认只 dry-run）")
    ap.add_argument("--api", default="", help="只补指定表（默认白名单全部）")
    args = ap.parse_args()

    names = [args.api] if args.api else list(STAGE2_BASE_ROWS)
    bad = stage2_incomplete_days(names)

    print("=" * 100)
    print(f"stage2 半截日补齐（模式={'APPLY（会写库）' if args.apply else 'DRY-RUN'}）")
    print("=" * 100)
    if not bad:
        print("\n未检出半截日 ✅ 无需补齐。")
        return 0

    total_days = sum(len(v) for v in bad.values())
    print(f"\n待补：{total_days} 个 (表, 日期) 组合")
    for name in sorted(bad):
        print(f"  {name:<12} " + ", ".join(sorted(bad[name])))

    if not args.apply:
        print("\n[DRY-RUN] 未发任何请求、未写任何数据。加 --apply 执行。")
        return 0

    col = DataCollector()
    ok = noop = 0
    print("\n" + "-" * 100)
    for name in sorted(bad):
        fetch = getattr(col.client, name, None)
        if fetch is None:
            print(f"  {name:<12} [跳过] tushare_client 无此接口")
            continue
        for d in sorted(bad[name]):
            before = rows_on(name, d)
            try:
                with slimiter(name):
                    df = fetch(trade_date=d)
            except Exception as exc:  # noqa: BLE001
                print(f"  {name:<12} {d}  请求失败：{str(exc)[:80]}")
                continue
            n_api = 0 if df is None else len(df)
            if df is not None and not df.empty:
                col.collect_and_store(name, df)
            after = rows_on(name, d)
            gained = after - before
            flag = "✅" if gained > 0 else ("⏸ 无变化" if n_api == 0 else "⚠ 未写入（疑似 upsert 未命中唯一键）")
            if gained > 0:
                ok += 1
            elif n_api:
                noop += 1
            print(f"  {name:<12} {d}  API 返回 {n_api:>5} 行  "
                  f"{before:>5} → {after:>5}（+{gained}）  {flag}")

    print("\n" + "-" * 100)
    print(f"完成：{ok} 个日期行数增加；{noop} 个日期 API 有数据但库内未变")

    # 补齐后复核：仍残缺的日期必须如实列出（不要"报喜不报忧"）
    left = stage2_incomplete_days(names)
    if left:
        print("\n⚠️ 仍未补齐：")
        for name in sorted(left):
            print(f"  {name:<12} " + ", ".join(sorted(left[name])))
        print("  ⇒ 若 API 返回本身就缺（非写库问题），说明是 Tushare 侧数据尚未发布，"
              "等下一轮采集；若 API 返回完整而库内未变，查 collect_and_store 的唯一键匹配。")
    else:
        print("\n✅ 全部补齐：白名单表已无半截日。")
    print("\n建议随后跑：./venv/bin/python scripts/diagnose_stk_limit_gap.py 复核覆盖率")
    return 0


if __name__ == "__main__":
    sys.exit(main())
