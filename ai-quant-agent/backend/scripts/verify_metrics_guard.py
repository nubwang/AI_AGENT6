"""验证：绩效指标口径修复 + 质量护栏（plans/23 T1/T1b）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_metrics_guard.py

覆盖：
  A. 复现旧症状（条件集全正收益）→ 必须被护栏标记为不可用于决策
  B. 同日多笔必须等权聚合，不再顺序累乘爆炸（旧口径曾产出 9.5e22）
  C. 正常样本 → 回撤为非 0、累计收益合理、质量门不报 error
  D. 正负样本区分度检验必须能识别"标签失效"
  E. 兼容性：旧 build_equity_curve 仍可调用
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest.metrics import (  # noqa: E402
    build_equity_curve, build_equity_curve_by_date, check_discrimination,
    compute_metrics, SCOPE_CONDITIONAL, SCOPE_TOP_N,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


# ── A. 复现旧症状：条件集全正收益（success + failure_A 都涨过） ──
print("\n[A] 旧症状复现：全部样本 T+5 为正（条件集幸存者偏差）")
t5_all_pos = [0.08, 0.12, 0.05, 0.20, 0.03] * 40      # 200 条，全正
m_a = compute_metrics(t5_all_pos, scope=SCOPE_CONDITIONAL)
check("A1 旧口径下 T+5 正收益率=100%（症状可复现）", abs(m_a.t5_pos_ratio - 1.0) < 1e-9,
      f"t5_pos_ratio={m_a.t5_pos_ratio:.2f}")
check("A2 护栏识别『无亏损样本』", any(i["code"] == "NO_LOSING_SAMPLES" for i in m_a.issues))
check("A3 护栏识别『条件集口径不可决策』", any(i["code"] == "CONDITIONAL_SCOPE" for i in m_a.issues))
check("A4 护栏识别『无回撤』", any(i["code"] == "NO_DRAWDOWN" for i in m_a.issues))
check("A5 诚实命名与旧别名一致（防误用拿 win_rate 当命中率）",
      m_a.win_rate == m_a.t5_pos_ratio == m_a.hit_rate)

# ── B. 同日多笔等权聚合（核心修复） ──
print("\n[B] 净值口径：同日 100 笔 +10% 必须等权聚合")
day = "20260601"
recs = [(day, 0.10)] * 100
eq, dates, stats = build_equity_curve_by_date(recs, cost_pct=0.0)
check("B1 同日 100 笔只产生 1 个净值点（n_days=1）", stats["n_days"] == 1, f"n_days={stats['n_days']}")
check("B2 当日组合收益 = 10%（非 1.1^100）", abs(eq[-1] - 1.10) < 1e-9, f"净值={eq[-1]:.4f}")
check("B3 样本数仍统计为 100", stats["n_samples"] == 100)

m_b = compute_metrics([r for _, r in recs], dates=[d for d, _ in recs], scope=SCOPE_TOP_N,
                      benchmark_t5=[-0.02] * 100)
check("B4 累计收益不再爆炸", abs(m_b.cum_return) < 1.0, f"cum_return={m_b.cum_return:.4f}")
check("B5 不存在 CUM_RETURN_IMPLAUSIBLE 报错",
      not any(i["code"] == "CUM_RETURN_IMPLAUSIBLE" for i in m_b.issues))

# 成本必须生效
eq_c, _, _ = build_equity_curve_by_date([(day, 0.10)], cost_pct=0.0015)
check("B6 成本已扣除（10% → 9.85%）", abs(eq_c[-1] - 1.0985) < 1e-9, f"净值={eq_c[-1]:.4f}")

# ── C. 正常样本：质量门不报 error ──
print("\n[C] 正常样本（有涨有跌、跨多日）")
norm = []
for i in range(60):
    d = f"2026{4 + i // 20:02d}{1 + i % 20:02d}"
    r = 0.09 if i % 3 == 0 else (-0.04 if i % 3 == 1 else 0.01)
    norm.append((d, r))
m_c = compute_metrics([r for _, r in norm], dates=[d for d, _ in norm], scope=SCOPE_TOP_N,
                      benchmark_t5=[0.01] * 60)
check("C1 最大回撤为非 0（真实组合必有回撤）", m_c.max_drawdown < 0, f"max_dd={m_c.max_drawdown:.4f}")
check("C2 累计收益在合理区间(<10x)", abs(m_c.cum_return) < 10, f"cum_return={m_c.cum_return:.3f}")
check("C3 质量门非 error", m_c.quality_gate != "error", f"gate={m_c.quality_gate}")
check("C4 对照组超额已计算", m_c.benchmark_n == 60 and m_c.lift_t5_pos != 0,
      f"lift={m_c.lift_t5_pos:+.2f}pct p={m_c.lift_p_value}")

# ── D. 区分度检验 ──
print("\n[D] 正负样本区分度检验（T3 标签失效识别）")
d_bad = check_discrimination([0.08] * 80 + [-0.02] * 20, [0.07] * 79 + [-0.02] * 21)
check("D1 正负几乎相同时判定『未通过』", d_bad["passed"] is False, d_bad["message"])
d_ok = check_discrimination([0.09] * 70 + [-0.05] * 30, [0.02] * 30 + [-0.06] * 70)
check("D2 正负差异明显时判定『通过』", d_ok["passed"] is True, d_ok["message"])

# ── E. 兼容性 ──
print("\n[E] 兼容性")
legacy = build_equity_curve([0.01, 0.02, None, 0.03])
check("E1 旧 build_equity_curve 仍可调用（长度正确）", len(legacy) == 4, f"len={len(legacy)}")

print(f"\n{'=' * 60}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
