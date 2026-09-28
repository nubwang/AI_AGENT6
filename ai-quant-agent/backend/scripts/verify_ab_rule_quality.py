"""验证：A/B 方案 C 的口径修复 + 条件表规则质量门槛（plans/23 T2）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_ab_rule_quality.py

背景（T2 实测症状）：
    plan_c_rule_hits == plan_a_count == 424 → C 命中全样本 → C≡A →
    accuracy_lift=0.0、p_mcnemar=1.0、decision 永远 No-Go。
    根因：条件表 = "N 个特征 × 2 方向 × 4 分位" 各留一条规则，**单条规则都合法，
    但规则并集必然覆盖绝大多数样本** → "命中≥1条" 恒为真。

覆盖：
  R1. 复现根因：旧口径（无门槛）下，规则并集覆盖率 > 90%
  R2. 修复：新口径下每条规则覆盖率 ≤50% 且 up_prob ≥ 基线+5pct
  R3. 修复：C 改为"按概率取前 30%"，覆盖率受控且 C 组概率显著高于其余
  R4. 概率无区分度（无规则命中/全部落中性基线）→ 判 A/B 无效
  R5. A/B 门槛常量齐备（防止被误删导致回归）
  R6. build_condition_table 真实产出满足规则质量门槛
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.backtest.threshold_analyzer import (  # noqa: E402
    analyze_single_features, build_condition_table, score_by_probability_table,
    CONDITION_MIN_EDGE, CONDITION_MAX_COVERAGE, NEUTRAL_BASELINE, ProbabilityTable,
)
from app.backtest.runner import (  # noqa: E402
    C_TARGET_COVERAGE, MAX_C_COVERAGE, MIN_C_PROB_GAP,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


rng = np.random.default_rng(7)
N = 500
NFEAT = 20          # 20 个条件特征（真实系统有 30 个）
FEATS = [f"f{i}" for i in range(NFEAT)]

y = (rng.random(N) < 0.60).astype(float)                 # 基线上涨率 0.60
mat = pd.DataFrame({f: rng.normal(0, 1, N) for f in FEATS})   # 与 y 完全独立的噪音特征
print(f"基线上涨率 = {y.mean():.3f}；特征 {NFEAT} 个（与结果独立，用于复现『弱规则并集』）\n")


def mask_by_hits(table: ProbabilityTable, m: pd.DataFrame) -> tuple[float, int]:
    """旧 C 口径：命中 ≥1 条规则即入选。返回 (覆盖率, 有命中样本数)。"""
    hit = 0
    for i in range(len(m)):
        fd = {c: float(m.iloc[i][c]) for c in m.columns}
        _, hits = score_by_probability_table(fd, table)
        if hits:
            hit += 1
    return hit / len(m), hit


# ── R1: 复现根因 ──
print("[R1] 旧口径（min_edge=0, 无覆盖率上限）")
rules_old = analyze_single_features(mat, y, min_sample=30, directions=("high", "low"),
                                    min_edge=0.0, max_coverage=1.0)
tbl_old = ProbabilityTable(rules=rules_old, baseline_prob=float(y.mean()), sample_count=N)
cov_old, hits_old = mask_by_hits(tbl_old, mat)
print(f"    规则数 {len(rules_old)}，命中≥1条规则的样本 {hits_old}/{N} → 覆盖率 {cov_old:.1%}")
check("R1 复现『规则并集覆盖全样本』（旧口径覆盖率 > 90%）", cov_old > MAX_C_COVERAGE,
      f"cov={cov_old:.1%}")

# ── R2: 新门槛 ──
print("\n[R2] 新口径（min_edge=5pct, max_coverage=50%）")
rules_new = analyze_single_features(mat, y, min_sample=30, directions=("high", "low"),
                                    min_edge=CONDITION_MIN_EDGE,
                                    max_coverage=CONDITION_MAX_COVERAGE)
covs = [r.sample_count / N for r in rules_new]
print(f"    规则数 {len(rules_new)}，覆盖率 {[f'{c:.0%}' for c in covs]}")
check("R2 每条规则覆盖率 ≤ 50%", all(c <= CONDITION_MAX_COVERAGE + 1e-9 for c in covs),
      f"max={max(covs) if covs else 0:.0%}")
check("R2 每条规则 up_prob ≥ 基线+5pct",
      all(r.gain >= CONDITION_MIN_EDGE - 1e-9 for r in rules_new))

# ── R3/R4: 新 C 口径（按概率取前 30%）──
def mask_top(table: ProbabilityTable, m: pd.DataFrame) -> tuple[float, float]:
    """新 C 口径：按综合概率取前 C_TARGET_COVERAGE。返回 (覆盖率, 概率差)。"""
    probs = np.zeros(len(m))
    for i in range(len(m)):
        fd = {c: float(m.iloc[i][c]) for c in m.columns}
        prob, _ = score_by_probability_table(fd, table)
        probs[i] = float(prob)
    k = max(1, int(round(len(probs) * C_TARGET_COVERAGE)))
    order = np.argsort(-probs)
    sel = np.zeros(len(probs), dtype=bool)
    sel[order[:k]] = True
    gap = float(probs[sel].mean() - probs[~sel].mean()) if (~sel).any() else 0.0
    return float(sel.mean()), gap


# 用"有真实区分度"的场景验证新口径可用（造一个强特征）
strong = pd.DataFrame({"s1": rng.normal(0, 1, N)})
y_s = (rng.random(N) < 0.5).astype(float)
y_s[strong["s1"] >= np.quantile(strong["s1"], 0.7)] = 1.0
tbl_s = build_condition_table(strong, y_s, min_sample=30)
cov_s, gap_s = mask_top(tbl_s, strong)
print(f"\n[R3] 新 C 口径（强特征场景）：覆盖率 {cov_s:.1%}，C 组与其余概率差 {gap_s:+.4f}")
check("R3 覆盖率受控在目标值附近", abs(cov_s - C_TARGET_COVERAGE) < 0.02, f"cov={cov_s:.1%}")
check("R3 概率差 ≥ MIN_C_PROB_GAP（有区分度，A/B 有效）", gap_s >= MIN_C_PROB_GAP,
      f"gap={gap_s:+.4f}")

# 无条件表（全部中性基线）→ 无区分度
tbl_empty = ProbabilityTable(rules=[], baseline_prob=float(y.mean()), sample_count=N)
cov_e, gap_e = mask_top(tbl_empty, mat)
print(f"[R4] 空规则表：覆盖率 {cov_e:.1%}，概率差 {gap_e:+.4f}")
check("R4 无规则命中 → 概率差为 0 → 应判 A/B 无效", gap_e < MIN_C_PROB_GAP,
      f"gap={gap_e:+.4f}")
check("R4 中性基线未被误改", abs(NEUTRAL_BASELINE - 0.6) < 1e-9, f"NEUTRAL={NEUTRAL_BASELINE}")

# ── R5: 门槛常量齐备 ──
print("\n[R5] 门槛常量")
check("R5 C_TARGET_COVERAGE 合理（0<cov<0.5）", 0 < C_TARGET_COVERAGE < 0.5,
      f"={C_TARGET_COVERAGE}")
check("R5 MAX_C_COVERAGE 与 MIN_C_PROB_GAP 已定义",
      MAX_C_COVERAGE > 0 and MIN_C_PROB_GAP > 0,
      f"max_cov={MAX_C_COVERAGE}, min_gap={MIN_C_PROB_GAP}")

# ── R6: build_condition_table 真实产出 ──
print("\n[R6] build_condition_table 真实产出")
cond_mat = pd.DataFrame({f: rng.normal(0, 1, N) for f in FEATS[:5]})
tbl = build_condition_table(cond_mat, y.astype(float), min_sample=30)
cvs = [r.sample_count / N for r in tbl.rules]
print(f"    规则数 {len(tbl.rules)}，覆盖率 {[f'{c:.0%}' for c in cvs]}")
check("R6 所有规则覆盖率 ≤ 50%",
      all(c <= CONDITION_MAX_COVERAGE + 1e-9 for c in cvs))
check("R6 所有规则 gain ≥ 5pct",
      all(r.gain >= CONDITION_MIN_EDGE - 1e-9 for r in tbl.rules))

print(f"\n{'=' * 60}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
