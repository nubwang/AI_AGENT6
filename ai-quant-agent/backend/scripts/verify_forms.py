"""规则形态验证命令行脚本（规划 12 / 14）

用法：
  cd ai-quant-agent/backend && python scripts/verify_forms.py [--max-stocks N] [--start-date 20150101] [--no-auto]

说明：
  运行全市场规则形态验证：全市场逐日扫描 → 45% 硬门槛 + 基线 + 显著性 + 样本外验证
  → 系统自总结规则挖掘（auto）→ 固化 backend/data/form_leaderboard.json。
  每日推荐（daily_scan）加载该文件启用规则形态信号（信号 2），与 20 天向量等权融合排序。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.rule_verifier import run_rule_verification  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="规则形态验证 + 固化 form_leaderboard.json")
    p.add_argument("--max-stocks", type=int, default=None, help="限制股票数（调试用）")
    p.add_argument("--start-date", default="20150101", help="数据起始日期 YYYYMMDD")
    p.add_argument("--end-date", default="", help="数据截止日期（空=最新）")
    p.add_argument("--no-auto", action="store_true", help="跳过系统自总结规则挖掘")
    p.add_argument("--min-hit-rate", type=float, default=0.45, help="45% 硬门槛（可调）")
    p.add_argument("--min-samples", type=int, default=30, help="可交易样本量下限")
    p.add_argument("--p-value", type=float, default=0.05, help="显著性水平（默认 0.05；扩充规则库可放宽到 0.10）")
    p.add_argument("--min-oos-hit-rate", type=float, default=0.0,
                   help="OOS 段 T+1 上涨率下限（默认 0.0=不低于全量基线；传 0.45 恢复旧口径）")
    p.add_argument("--step", type=int, default=1, help="扫描步长（1=逐日最严谨；5=加速约 3h；10=约 1.5h）")
    args = p.parse_args()

    print("开始规则形态全市场验证（耗时较长，请耐心等待）...")
    report = run_rule_verification(
        max_stocks=args.max_stocks,
        start_date=args.start_date,
        end_date=args.end_date,
        step=args.step,
        min_hit_rate=args.min_hit_rate,
        min_samples=args.min_samples,
        p_value_alpha=args.p_value,
        min_oos_hit_rate=args.min_oos_hit_rate,
        mine_auto=not args.no_auto,
        save=True,
        progress_cb=lambda stage, pct: print(f"[{pct:5.1%}] {stage}"),
    )
    print("=" * 60)
    print(f"规则形态验证完成: verified {report.get('verified_count', 0)} / total {report.get('total_rules', 0)}")
    print(f"基线 T+1 上涨率: {report.get('baseline_t1', 0):.4f}")
    print(f"回退模式(幸存<3): {report.get('fallback', False)}")
    for r in report.get("rules", []):
        print(f"  [PASS] {r.get('key'):<20} {r.get('name'):<16} "
              f"hit_rate_t1={r.get('hit_rate_t1', 0):.4f} samples={r.get('samples', 0)}")
    for d in report.get("deleted", []):
        print(f"  [DEL ] {d.get('key'):<20} reason={d.get('reason')}")
    if report.get("error"):
        print(f"ERROR: {report['error']}")
        sys.exit(1)
    if report.get("fallback"):
        print("提示：幸存规则 < 3，每日推荐将回退为'仅 20 天向量'模式（规划 12 兜底）")


if __name__ == "__main__":
    main()
