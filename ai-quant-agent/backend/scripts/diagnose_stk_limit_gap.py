"""**只读**诊断：stk_limit（涨跌停价）的"半采日"缺口

## 为什么要有这个脚本

2026-09-28 回测被数据门禁拦下，报错里混着两条**不同性质**的问题，必须分开看：

    锚定表未对齐：daily(20260924), adj_factor(20260928), daily_basic(20260924)   ← 过期缓存造成的**假警报**
    stk_limit（20260923）落后锚定表（20260924）1 个交易日                        ← 这条是**真的**

其中"锚定表未对齐"已由 `data_validator._refresh_boundaries()` 修掉（缓存不再提供过期
`max_date`）。剩下真正拦人的是 `stk_limit`：**它不是"日期没到"，而是"只采了一部分"**。

实测（`diagnose_anchor_gap.py` 的逐日行数表）：

    20260923  stk_limit = 5647   ← 正常
    20260924  stk_limit = 2361   ← 半采（约 42%）
    20260928  stk_limit = 2361   ← 半采，且与 09-24 是**同一个数字**

而 `daily` 同日是 5557 只。后果：`loader.load_limit_prices` 的"涨停不可买"过滤
只对 42% 的股票生效 ⇒ 剩下 58% 里若有"一字板买不进"的样本会被当成**可买** ⇒
系统性**高估收益**。这是会污染结论的那一类问题，门禁拦它有道理。

## 本脚本回答三个问题

    1. 缺口有多大、按交易所/板块分别是多少（决定是"全市场都没采"还是"只漏了某些板"）
    2. 涨跌停价字段是否"行在但值是 NULL"（决定补数方式：重采 vs 修值）
    3. 缺口集合是否等于**更早某天**的快照（决定是"漏采"还是"写错日期/写错批次"）

运行（**只读**，不写一行数据）：

    cd ai-quant-agent/backend && ./venv/bin/python scripts/diagnose_stk_limit_gap.py
    cd ai-quant-agent/backend && ./venv/bin/python scripts/diagnose_stk_limit_gap.py --days 10
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.models import SessionLocal  # noqa: E402

# 个股表的"自然基数"下限：单日行数低于它即视为半采（与 data_validator.BASE_ROWS_MIN 同源）
MIN_ROWS = 5000
# 与当日 daily 行数的比值下限：低于此比例即判"半采"
COVER_RATIO = 0.6


def _open_days(db, n: int) -> list[str]:
    """最近 n 个已开市交易日（按 trade_cal，不按各表 MAX —— 避免被抢跑的表带偏）。"""
    rows = db.execute(
        text("SELECT cal_date FROM trade_cal WHERE is_open=1 "
             "AND cal_date <= DATE_FORMAT(NOW(), '%Y%m%d') AND exchange='SSE' "
             "ORDER BY cal_date DESC LIMIT :n"),
        {"n": n},
    ).fetchall()
    return [str(r[0]) for r in rows][::-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="只读诊断：stk_limit 半采缺口")
    ap.add_argument("--days", type=int, default=10, help="看最近几个交易日（默认 10）")
    args = ap.parse_args()

    print("=" * 100)
    print("stk_limit 半采缺口诊断（**只读**）")
    print("=" * 100)

    with SessionLocal() as db:
        days = _open_days(db, args.days)

        # ── ① 逐日总量 + 覆盖率 ──
        print("\n① 逐日总量与覆盖率（基准 = 当日 daily 行数）")
        print("-" * 100)
        print(f"  {'日期':<10} {'stk_limit':>10} {'daily':>8} {'覆盖率':>8}   判定")
        gap_days: list[str] = []
        for d in days:
            n1 = int(db.execute(text(
                "SELECT COUNT(*) FROM stk_limit WHERE trade_date=:d"), {"d": d}).scalar() or 0)
            n2 = int(db.execute(text(
                "SELECT COUNT(*) FROM daily WHERE trade_date=:d"), {"d": d}).scalar() or 0)
            ratio = (n1 / n2) if n2 else 0.0
            bad = bool(n2 and (n1 < MIN_ROWS or ratio < COVER_RATIO))
            if bad:
                gap_days.append(d)
            print(f"  {d:<10} {n1:>10} {n2:>8} {ratio * 100:>7.1f}%   "
                  + ("❌ 半采" if bad else "✅ 完整"))

        if not gap_days:
            print("\n  ⇒ 最近窗口内 stk_limit 无半采，门禁若仍拦人请查其它表。")
            return 0

        # ── ② 缺口按交易所/前缀拆解 ──
        newest = gap_days[-1]
        print(f"\n② 缺口构成（以 {newest} 为例，按代码前缀）")
        print("-" * 100)
        lim = dict(db.execute(text(
            "SELECT LEFT(ts_code,3) p, COUNT(*) FROM stk_limit WHERE trade_date=:d GROUP BY p"),
            {"d": newest}).fetchall())
        day = dict(db.execute(text(
            "SELECT LEFT(ts_code,3) p, COUNT(*) FROM daily WHERE trade_date=:d GROUP BY p"),
            {"d": newest}).fetchall())
        for p in sorted(set(lim) | set(day)):
            n1, n2 = int(lim.get(p, 0)), int(day.get(p, 0))
            mark = "   ← 整块缺失" if n1 == 0 and n2 else ("   ← 缺" if n1 < n2 else "")
            print(f"      前缀 {p:<7} stk_limit={n1:>5}  daily={n2:>5}{mark}")

        # ── ③ 行在但值为 NULL？──
        print("\n③ 涨跌停价字段是否为空（决定补数方式：重采 vs 修值）")
        print("-" * 100)
        cols = sorted(str(r[0]) for r in db.execute(text("SHOW COLUMNS FROM stk_limit")).fetchall())
        print(f"  列: {cols}")
        for col in ("up_limit", "down_limit"):
            if col not in cols:
                continue
            for d in gap_days[-3:]:
                r = db.execute(text(
                    f"SELECT COUNT(*), COUNT({col}) FROM stk_limit WHERE trade_date=:d"),
                    {"d": d}).fetchone()
                tot, nonnull = int(r[0]), int(r[1])
                print(f"  {d}  {col:<11} 总行={tot:>5}  非空={nonnull:>5}  "
                      + ("← 存在 NULL 行" if nonnull < tot else ""))

        # ── ④ 缺口是否等于更早某天的快照 ──
        print("\n④ 缺口集合 vs 历史日期快照（漏采？还是写错了日期/批次？）")
        print("-" * 100)
        cur = {r[0] for r in db.execute(text(
            "SELECT ts_code FROM stk_limit WHERE trade_date=:d"), {"d": newest})}
        day_codes = {r[0] for r in db.execute(text(
            "SELECT ts_code FROM daily WHERE trade_date=:d"), {"d": newest})}
        missing = day_codes - cur
        print(f"  {newest}：有行情 {len(day_codes)} 只，有涨跌停价 {len(cur)} 只，缺口 {len(missing)} 只")
        for d in days:
            if d == newest:
                continue
            other = {r[0] for r in db.execute(text(
                "SELECT ts_code FROM stk_limit WHERE trade_date=:d"), {"d": d})}
            hit = len(missing & other) / len(missing) if missing else 0.0
            print(f"  缺口 ∩ {d} 的 stk_limit = {len(missing & other):>5} 只"
                  f"（占缺口 {hit * 100:>5.1f}%）")
        print("  ⇒ 若某天占比接近 100%，说明是**同一批股票**的重复缺失（或批次写错），"
              "应从采集侧定位，而不是逐日重采。")

        print("\n⑤ 处置建议（本脚本不写数据）")
        print("-" * 100)
        print("  · 只影响『涨停不可买』过滤的准确性 ⇒ 会**高估**收益，属「必须修」的那一类；")
        print("  · 走**正常采集**补齐（`app/api/ws.py` 的采集链路，幂等 upsert），"
              "严禁为绕过门禁写补数脚本（已发生过 express 1,494 万行冗余事故）；")
        print("  · 修完用 scripts/diagnose_anchor_gap.py 复核：锚定表同日 + 个股表每日 ≥ 5000 行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
