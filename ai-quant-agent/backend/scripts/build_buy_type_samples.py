"""构建四类买点回测样本 + 高开分桶报告（plans/23 §3.3 / P1 第 7 项）

产出 `data/buy_type_samples.json`，回答规划 §3.3 要求的：
  触发率 / 成交率（挂不上→统计偏差）/ 封板不可成交占比 / T+1·T+5 胜率与期望 / 失效模式，
外加**高开幅度分桶胜率**（规划特别强调的那条现实约束）。

运行：
    cd ai-quant-agent/backend
    ./venv/bin/python scripts/build_buy_type_samples.py --stocks 200 --days 460
    ./venv/bin/python scripts/build_buy_type_samples.py --stocks 400 --days 700 --seed 7

⚠️ 成本：stride 固定为 1（买点看的是"每一天"），200 只 × 460 天 ≈ 9.2 万评估点，约 4~6 分钟。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import buy_types as BT
from app.backtest import regime_split as RS

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUT_FILE = os.path.join(DATA_DIR, "buy_type_samples.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=200)
    ap.add_argument("--days", type=int, default=460, help="只扫最近 N 个交易日")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    from app.backtest.loader import load_stock_pool, prepare_stock_data
    pool = load_stock_pool()
    if pool is None or len(pool) == 0:
        print("❌ 股票池为空（先跑采集）")
        return 1
    codes = list(pool["ts_code"])
    if not args.all and args.stocks < len(codes):
        random.seed(args.seed)
        codes = random.sample(codes, args.stocks)

    print(f"扫描 {len(codes)} 只（stride=1，最近 {args.days} 交易日，seed={args.seed}）…")
    t0 = time.time()
    recs: list[dict] = []
    ok = failed = 0
    for i, code in enumerate(codes):
        try:
            df = prepare_stock_data(code)
            if df is None or len(df) < 40:
                failed += 1
                continue
            min_idx = max(22, len(df) - int(args.days))
            recs.extend(BT.scan_stock(df, code, stride=1, min_idx=min_idx))
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ! {code}: {type(exc).__name__}: {exc}")
        if (i + 1) % 20 == 0 or i == len(codes) - 1:
            print(f"  {i + 1}/{len(codes)}  评估点 {len(recs)}  ({time.time()-t0:.0f}s)")

    summary = BT.summarize(recs)
    gaps = {t: BT.gap_buckets(recs, t) for t in BT.BUY_TYPES}
    # 环境分层（复用 regime_split，秒级）
    rmap = RS.regime_map_for_points(recs)
    by_regime: dict = {}
    for r in RS.REGIMES:
        sub = [x for x in recs if (rmap.get(str(x.get("date"))) or {}).get("regime") == r]
        by_regime[r] = {"points": len(sub), **{t: BT.summarize(sub)[t] for t in BT.BUY_TYPES}}

    payload = {"built_at": time.strftime("%Y-%m-%d %H:%M:%S"), "stocks_ok": ok,
               "stocks_failed": failed, "points": len(recs), "days": args.days,
               "seed": args.seed, "summary": summary, "gap_buckets": gaps,
               "by_regime": by_regime, "records": recs}
    try:
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        saved = True
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 写入失败: {exc}")
        saved = False

    print(f"\n=== 四类买点（{len(recs)} 个评估点）===")
    print(f"{'买点':<12}{'触发':>8}{'成交':>8}{'成交率':>8}{'T+1均值':>10}{'T+1胜率':>9}"
          f"{'T+5均值':>10}{'T+5胜率':>9}")
    for t in BT.BUY_TYPES:
        s = summary[t]
        print(f"{s['cn']:<12}{s['trigger_n']:>8}{s['fill_n']:>8}{s['fill_rate']:>8.3f}"
              f"{str(s['t1'].get('mean')):>10}{str(s['t1'].get('pos')):>9}"
              f"{str(s['t5'].get('mean')):>10}{str(s['t5'].get('pos')):>9}")
    print("\n未成交原因分布：")
    for t in BT.BUY_TYPES:
        print(f"  {summary[t]['cn']}: {summary[t]['reasons']}")

    print("\n=== 高开幅度分桶（T+5，成交样本）===")
    for t in BT.BUY_TYPES:
        print(f"\n[{BT.BUY_TYPE_CN[t]}]")
        print(f"  {'分桶':<10}{'n':>8}{'成交':>8}{'成交率':>8}{'T+5均值':>10}{'T+5胜率':>9}")
        for name, b in gaps[t].items():
            print(f"  {name:<10}{b['n']:>8}{b['fill_n']:>8}{b['fill_rate']:>8.3f}"
                  f"{str(b['t5'].get('mean')):>10}{str(b['t5'].get('pos')):>9}")

    print(f"\n落盘：{'✅ data/buy_type_samples.json' if saved else '❌ 失败'}"
          f"（耗时 {time.time()-t0:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
