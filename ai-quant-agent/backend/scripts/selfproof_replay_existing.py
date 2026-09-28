"""模式 C CLI：历史榜单零成本回灌结算（plans/25 §三 / P3）

用**已经落盘的历史榜单** + 之后的真实行情，算 T+1/T+5/T+20 与**超额**（vs 全市场等权），
零 LLM 调用、零 API 费用。先给覆盖度，再给结论。

跑法：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/selfproof_replay_existing.py
    # 只看最近 60 天榜单（每天最多 30 条，控制耗时）
    cd ai-quant-agent/backend && ./venv/bin/python scripts/selfproof_replay_existing.py --dates 60 --top 30
    # 只打印覆盖度，不结算
    cd ai-quant-agent/backend && ./venv/bin/python scripts/selfproof_replay_existing.py --coverage-only
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import selfproof_existing as SE  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", type=int, default=0, help="只结算最近 N 个有榜单的交易日（0=全部）")
    ap.add_argument("--top", type=int, default=0, help="每天最多结算 N 条推荐（0=全部）")
    ap.add_argument("--coverage-only", action="store_true", help="只打印覆盖度")
    args = ap.parse_args()

    cov = SE.coverage()
    print("\n=== 覆盖度（先看清楚有多少料）===")
    print(f"  榜单目录: {cov['board_dir']}")
    print(f"  榜单文件: {cov['board_files']} 个 | 覆盖日期: {cov['board_date_range'] or '（空）'}")
    print(f"  来源分布: {cov['board_source'] or '（空）'}")
    print(f"  推荐条数: 共 {cov['picks_total']} 条（日均 {cov['picks_per_day_avg']}）")
    print(f"  monitor_state: {cov['monitor_hits']} 条命中记录，其中已回填 T+5 {cov['monitor_hits_with_t5']} 条")
    if cov["board_dates"]:
        print(f"  榜单日期: {cov['board_dates'][:5]} … {cov['board_dates'][-3:]}")

    if args.coverage_only:
        return 0

    res = SE.run(max_dates=(args.dates or None), max_picks_per_day=(args.top or None))
    if not res.get("result"):
        print(f"\n⚠️ {res.get('reason')}")
        return 0

    st = res["result"]["stats"]
    print("\n=== 结算结果（D+1 买入口径；净口径已扣 "
          f"{res['result']['cost_pct'] * 100:.2f}% 往返成本）===")
    print(f"  推荐 {res['result']['n_picks']} 条 | 已结算 T+5 {res['result']['n_settled_t5']} 条 "
          f"| 未到期/无数据 {res['result']['n_pending_t5']} 条")
    for k, label in (("t1", "T+1"), ("t5", "T+5"), ("t20", "T+20")):
        s = st[k]
        print(f"  {label:5s} n={s['n']:>4d} 上涨率={s['win_rate']} 均值={s.get('avg') or s.get('avg_gross')}")
    e = st["excess_t5"]
    print(f"  **超额 T+5**（主指标）n={e['n']} 胜率={e['win_rate']} 均值={e['avg']} t={e['t_stat']}")
    v = res["verdict"]
    print(f"  样本判定: {'✅ 充足' if v.get('sufficient_samples_board') else '⚠️ 不足（只作观察）'}"
          f"  [门槛 n>={v.get('min_samples')}]")

    if res["result"]["by_form_type"]:
        print("\n  分形态 T+5：")
        for form, s in list(res["result"]["by_form_type"].items())[:8]:
            print(f"    {form:6s} n={s['n']:>4d} 均值={s['avg_t5']} 胜率={s['win_rate_t5']}")

    # ── 台账口径（monitor_state：样本大、已回填、零成本）──
    mon = res.get("result_monitor") or {}
    if mon.get("ok"):
        ms = mon["stats"]
        print("\n=== 台账口径（monitor_state 已回填记录；≠ 最终榜单，两者不可混算）===")
        print(f"  记录 {mon['n_records']} 条（已结算 T+5 {mon['n_settled_t5']} 条）"
              f" | 日期区间 {mon['date_range']}")
        for k, label in (("t1", "T+1"), ("t5", "T+5"), ("t20", "T+20")):
            s = ms[k]
            print(f"  {label:5s} n={s['n']:>5d} 上涨率={s['win_rate']} "
                  f"均值={s.get('avg') or s.get('avg_gross')}")
        me = ms["excess_t5"]
        print(f"  **超额 T+5**（主指标）n={me['n']} 胜率={me['win_rate']} "
              f"均值={me['avg']} t={me['t_stat']}")
        print(f"  样本判定: {'✅ 充足' if (res['verdict'].get('sufficient_samples_monitor')) else '⚠️ 不足（只作观察）'}")
        if mon.get("by_form_type"):
            print("  分形态 T+5：")
            for form, s in list(mon["by_form_type"].items())[:8]:
                print(f"    {form:6s} n={s['n']:>5d} 均值={s['avg_t5']} 胜率={s['win_rate_t5']}")

    print(f"\n报告（沙箱）: {os.path.join(SE.OUT_DIR, 'report.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
