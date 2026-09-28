"""自证测试数据门 CLI（plans/25 P0.5）

回答两个"动手前必须先知道"的问题：
  ① **能自证到多早**（各表数据边界 → 最早可自证日期）
  ② **幸存者偏差有多大**（历史出现过的股票 − 今日存续 = 退市缺口）

跑法：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/selfproof_data_gate.py
    # 深度模式：额外实算退市缺口（daily 表区间 DISTINCT，较慢）
    cd ai-quant-agent/backend && ./venv/bin/python scripts/selfproof_data_gate.py --deep
    # 指定 as_of
    cd ai-quant-agent/backend && ./venv/bin/python scripts/selfproof_data_gate.py --as-of 20260503
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import selfproof_data_gate as G  # noqa: E402
from app.backtest import selfproof_policy as P  # noqa: E402
from app.models import SessionLocal  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default="", help="as_of 日期 YYYYMMDD（默认取 daily 最新交易日）")
    ap.add_argument("--deep", action="store_true", help="额外实算退市缺口（较慢）")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        res = G.run(db, as_of=args.as_of, deep=args.deep)
    finally:
        db.close()

    g, s = res["data_gate"], res["survivorship"]
    print(f"\n=== 自证测试数据门（as_of={res['as_of']}）===")
    print(f"最早可自证日期: {g['earliest_full_as_of']}   可回放至: {g['latest_as_of']}   "
          f"判定: {g['verdict']}")
    print("\n-- 关键表（决定最早可自证日期）--")
    for r in g["critical"]:
        print(f"  {r['table']:18s} {r['date_col']:12s} {r['min']} → {r['max']}  ~{r['rows']} 行")
    if g["optional"]:
        print("\n-- 非关键表（缺失只降级特征，不否决）--")
        for r in g["optional"]:
            print(f"  {r['table']:18s} {r['date_col']:12s} {r['min']} → {r['max']}  ~{r['rows']} 行")
    if g["missing"]:
        print("\n-- 缺失表 --")
        for m in g["missing"]:
            print(f"  ❌ {m['table']:18s} critical={m['critical']}  {m['why']}")
    for n in g["notes"]:
        print(f"  ⚠️  {n}")

    print("\n=== 幸存者偏差 ===")
    print(f"  今日存续股票池: {s.get('pool_size')} 只")
    print(f"  as_of 之后才上市（须剔除）: {s.get('listed_after_as_of')} 只")
    print(f"  stock_basic 是否有 list_status/delist_date 列: "
          f"{s.get('has_list_status')} / {s.get('has_delist_date')}")
    print(f"  stock_basic 实际列: {', '.join(s.get('stock_basic_columns') or [])}")
    if s.get("hist_codes_all") is not None:
        print(f"  daily 全历史出现过的 ts_code: {s['hist_codes_all']} 只")
        print(f"  **退市缺口**（历史出现过但今天不在池里）: {s['gap_estimate']} 只 "
              f"（{s.get('gap_pct')}%）")
    if s.get("gap_upto_as_of") is not None:
        print(f"  截至 as_of 的缺口: {s['gap_upto_as_of']} 只")
    if s.get("structural_warning"):
        print(f"  🚨 {s['structural_warning']}")
    if s.get("error") or s.get("deep_error") or s.get("gap_error"):
        print(f"  ⚠️  {s.get('error') or s.get('deep_error') or s.get('gap_error')}")
    print(f"  ⚠️  {s['warning']}")
    for m in s["mitigation"]:
        print(f"     · {m}")

    out = P.sandbox_path("data_gate.json")
    ok = P.write_json_guarded(out, res)
    print(f"\n落盘（沙箱，走护栏）: {out} → {'✅' if ok else '❌'}")
    if s.get("error") or g["verdict"] == "blocked":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
