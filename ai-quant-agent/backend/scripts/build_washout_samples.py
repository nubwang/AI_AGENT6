"""构建洗盘样本库 + A/B（plans/23 §3.2 / P1 第 6 项）

用途：回答"洗盘识别到底有没有用"，并**为阈值提供依据**（不是拍脑袋调参）。

运行（建议先用小样本试跑，确认输出可读再放大）：
    cd ai-quant-agent/backend
    ./venv/bin/python scripts/build_washout_samples.py --stocks 150 --stride 5
    ./venv/bin/python scripts/build_washout_samples.py --stocks 400 --stride 3 --seed 7

产物：`data/washout_samples.json`（含 points 明细 + 各周期 A/B 结论，可复现）

⚠️ 成本提示：每只股票要 `prepare_stock_data` + 两次镜像查询（~1.5s），
    150 只约 4~6 分钟；全部 5500 只不建议一次跑完（分几天/分批次跑同一文件会覆盖）。
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import washout_samples as WS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=150, help="扫描股票数（默认 150）")
    ap.add_argument("--stride", type=int, default=WS.STRIDE, help="扫描步长（交易日，默认 5）")
    ap.add_argument("--days", type=int, default=460,
                    help="只扫最近 N 个交易日（默认 460≈2 年；None/0=全历史，很慢）")
    ap.add_argument("--seed", type=int, default=42, help="抽样随机种子（保证可复现）")
    ap.add_argument("--all", action="store_true", help="扫描全部股票（很慢，慎用）")
    args = ap.parse_args()
    days = args.days if args.days and args.days > 0 else None

    from app.backtest.loader import load_stock_pool
    pool = load_stock_pool()
    if pool is None or len(pool) == 0:
        print("❌ 股票池为空（先跑采集）")
        return 1
    codes = list(pool["ts_code"])
    if not args.all and args.stocks < len(codes):
        random.seed(args.seed)
        codes = random.sample(codes, args.stocks)

    print(f"扫描 {len(codes)} 只（stride={args.stride}，days={days}，seed={args.seed}）…")
    print("提示：确认日很稀疏（~0.073% 的股票×交易日）→ 凑样本量请用 stride=1 + 限定 days")
    t0 = time.time()

    def _prog(i, total, pts):
        if i % 10 == 0 or i == total:
            print(f"  {i}/{total}  累计观察点 {pts}  ({time.time()-t0:.0f}s)")

    payload = WS.build(codes, stride=args.stride, days=days, progress_cb=_prog)
    payload["seed"] = args.seed
    ok = WS.save(payload)

    print(f"\n=== 样本库 ===")
    print(f"成功 {payload['stocks_ok']} 只 / 失败 {payload['stocks_failed']} 只，"
          f"观察点 {payload['points_n']}，其中 washout_end 确认 {payload['wash_n']} 个"
          f"（{payload['wash_n']/max(1,payload['points_n'])*100:.2f}%）")

    print("\n=== A/B（同交易日 1:N 配对：洗盘组 vs 同日未确认/未否决对照）===")
    for h, ab in payload["ab"].items():
        w, c = ab.get("wash", {}), ab.get("control", {})
        print(f"\n[{h.upper()}] 配对 {ab.get('paired_dates')} 个(日期,形态)组合")
        print(f"  洗盘组 n={w.get('n',0)} 赚钱率={w.get('pos_ratio')} >5%率={w.get('gt5_ratio')} "
              f"均值={w.get('mean')} 中位={w.get('median')}")
        print(f"  对照组 n={c.get('n',0)} 赚钱率={c.get('pos_ratio')} >5%率={c.get('gt5_ratio')} "
              f"均值={c.get('mean')} 中位={c.get('median')}")
        print(f"  p={ab.get('p_value')}  →  {ab.get('verdict')}")

    print(f"\n落盘：{'✅ data/washout_samples.json' if ok else '❌ 写入失败'}"
          f"（耗时 {time.time()-t0:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
