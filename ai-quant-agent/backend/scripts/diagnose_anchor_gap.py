"""**只读**诊断：锚定表（daily / daily_basic / adj_factor）的"对齐 + 完整性"

## 为什么要有这个脚本（一次真实事故的复盘）

2026-09-21 盘中（13:20）回测被数据门禁拦下：

    锚定表未对齐：daily(20260918), adj_factor(20260921), daily_basic(20260918)
    锚定表落后 1 个交易日（数据 20260918，最近交易日 20260921）

**门禁是对的**，但当时我（AI）错误地去"补最新数据"，结果踩了两个坑，
必须固化成教训：

    坑 1｜`adj_factor` / `daily` / `daily_basic` 的 PRIMARY KEY 都是**自增 `id`**，
          `(ts_code, trade_date)` **只是普通索引（MUL）** ⇒
          `INSERT ... ON DUPLICATE KEY UPDATE` **永远不会触发**，它是**纯 INSERT**
          ⇒ 会静默产生重复行
          （`app/api/ws.py:673` 早就记录过同类事故：express 表 1,494 万行冗余就是这么来的）；
    坑 2｜库里 `trade_date` 是 **`varchar(10)` 且存 `'YYYYMMDD'`**（**不带横线**），
          而 ORM 模型 `app/models/stock.py::AdjFactor` 写的是 `Column(Date)` ——
          **模型与真实表不一致**；按 DATE 语义传 `'2026-09-17'` 会写进**格式不同的行**。

## ★ 操作纪律（血泪换来的）

    1. **做回测/研究不需要"最新数据"** —— 用**最近一个"完整且对齐"的交易日**及以前即可；
       本层所有结论（plans/24 §八~§11.29）都基于 **≤ 20260918** 的数据，**不受任何影响**；
    2. **当日数据要到收盘后（约 16:00 之后）才能采集**，盘中取 `daily` 必得 0 行；
       因此**盘中出现"锚定表落后 1 个交易日"是正常时序**，不是故障；
    3. **不要在盘中"补数据"**；也不要写任何"补数脚本"去绕过门禁 ——
       正确做法是等 16:00 后走**正常采集流程**；
    4. 任何**写库**动作之前，必须先核验：① 表的**真实** PK/唯一键 ② `trade_date` 的**真实类型与格式**
       （`SHOW COLUMNS` + `SHOW INDEX`，**不要相信 ORM 模型**），并准备好**可验证的回滚**。

## 本脚本做什么

    **只读**：打印最近 N 个交易日的逐日行数 + 对齐情况 + 完整性判定（不写任何一行数据）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/diagnose_anchor_gap.py
    cd ai-quant-agent/backend && ./venv/bin/python scripts/diagnose_anchor_gap.py --days 12
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.models import SessionLocal  # noqa: E402

ANCHORS = ("daily", "daily_basic", "adj_factor")
EXTRA = ("stk_limit", "suspend_d", "moneyflow", "index_daily")
# 单日"完整"的行数下限 —— **按表的自然基数分别设**：
# `suspend_d` 每日仅 10~20 只（停牌股）、`index_daily` 是"指数"不是个股
# ⇒ 它们套个股表的 5000 阈值会**误报**（0 = 不设阈值）。
MIN_ROWS = {"daily": 5000, "daily_basic": 5000, "adj_factor": 5000,
            "stk_limit": 5000, "moneyflow": 5000, "suspend_d": 0, "index_daily": 0}


def main() -> int:
    ap = argparse.ArgumentParser(description="只读诊断：锚定表对齐与完整性")
    ap.add_argument("--days", type=int, default=10, help="看最近几个交易日（默认 10）")
    args = ap.parse_args()

    print("=" * 104)
    print("锚定表对齐 / 完整性诊断（**只读**，不写任何数据）")
    print("=" * 104)
    with SessionLocal() as db:
        # 最近 N 个"已开市"交易日（按 trade_cal，而不是按各表 max —— 避免被抢跑的表带偏）
        cal = db.execute(text(
            "SELECT cal_date FROM trade_cal WHERE is_open = 1 AND cal_date <= "
            "DATE_FORMAT(NOW(), '%Y%m%d') AND exchange = 'SSE' "
            "ORDER BY cal_date DESC LIMIT :n"), {"n": args.days}).fetchall()
        days = [str(r[0]) for r in cal][::-1]

        print("① 表结构核对（**不要相信 ORM 模型**：模型说 Date，真实可能是 varchar）")
        print("-" * 104)
        for t in ANCHORS:
            try:
                cols = db.execute(text(f"SHOW COLUMNS FROM {t}")).fetchall()
                pk = [c[0] for c in cols if c[3] == "PRI"]
                td = next((c for c in cols if c[0] == "trade_date"), None)
                print(f"  {t:<13} PK={pk}  trade_date 真实类型={td[1] if td else '?'}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {t:<13} [跳过] {str(exc)[:60]}")
        print()

        print(f"② 逐日行数（最近 {len(days)} 个交易日）")
        print("-" * 104)
        cols_show = list(ANCHORS) + list(EXTRA)
        hdr = "日期       " + "".join(f"{t[:10]:>11}" for t in cols_show)
        print(hdr)
        print("-" * len(hdr))
        for d in days:
            cells, flags = [], []
            for t in cols_show:
                try:
                    n = int(db.execute(text(
                        f"SELECT COUNT(*) FROM {t} WHERE trade_date = :d"), {"d": d}).scalar() or 0)
                except Exception:  # noqa: BLE001
                    n = -1
                lo = MIN_ROWS.get(t, 5000)
                mark = "!" if (lo and 0 < n < lo) else ""
                if t in ANCHORS and lo and 0 < n < lo:
                    flags.append(f"{t} 残缺({n})")
                cells.append(f"{n:>10}{mark}")
            print(f"{d}  " + "".join(cells))
            if flags:
                print(f"{'':12}  ⚠️ " + " · ".join(flags))
        print("  （数字后带 `!` = 该日行数低于**该表的自然基数**下限；个股表阈值为 5000；"
              "`suspend_d` / `index_daily` 不设阈值）")
        print()

        print("③ 对齐判定（门禁的核心口径：锚定表必须**同日**）")
        print("-" * 104)
        mx = {}
        for t in ANCHORS:
            try:
                mx[t] = str(db.execute(text(f"SELECT MAX(trade_date) FROM {t}")).scalar())
            except Exception:  # noqa: BLE001
                mx[t] = "?"
        for t in ANCHORS:
            print(f"  {t:<13} MAX = {mx[t]}")
        same = len(set(mx.values())) == 1
        latest_open = days[-1] if days else "?"
        print()
        if same:
            print(f"  ⇒ 锚定表**已对齐**于 {mx['daily']}；最近开市日 {latest_open}")
            if mx["daily"] != latest_open:
                print("     ⚠️ 落后最近开市日 ⇒ **若当前时间 < 16:00，这是正常时序**"
                      "（当日数据要收盘后才发布/采集）")
        else:
            lead = [t for t in ANCHORS if mx[t] == max(mx.values())]
            lag = [t for t in ANCHORS if mx[t] == min(mx.values())]
            print(f"  ❌ 锚定表**未对齐**：领先 {lead} @ {max(mx.values())}，"
                  f"落后 {lag} @ {min(mx.values())}")
            print("     ⇒ 门禁**应当**拦下回测（这是正确行为）。")
        print()

        print("④ 处置建议（**不写数据**）")
        print("-" * 104)
        print("  · 做回测/研究：**用「最近一个完整且对齐的交易日」及以前的数据**即可，")
        print("    不需要最新数据；盘中不要去补、不要去删。")
        print("  · 若要更新到最新：等**收盘后（约 16:00 之后）**走正常采集流程，")
        print("    采集完成后再跑本脚本确认「锚定表同日 + 个股表每日行数 ≥ 5000」。")
        print("  · 严禁为绕过门禁而写「补数脚本」：本仓库已记录过两次同类事故")
        print("    （express 1,494 万行冗余 / adj_factor 误插 16,695 行重复）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
