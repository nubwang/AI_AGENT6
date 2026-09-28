"""模式有效性验证 + A/B 对比实验 + 上线决策门（validator）

对齐规划：plans/04-回测引擎.md 4.6 / 5.2 / 5.4 / 5.5
  - Walk-Forward 样本外验证
  - A/B/C 三方案对比（A 基线 / B 负模式库 / C 完整方案）
  - 统计显著性检验：McNemar 检验 + 配对 t 检验
  - 上线决策门（Go / No-Go）
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np

from app.core.logger import logger


@dataclass
class PlanMetrics:
    """单方案的评估指标。"""
    name: str
    hit_rate: float = 0.0        # T+5 上涨 >0%
    accuracy: float = 0.0        # T+5 涨幅 >5%
    t20_cont: float = 0.0        # T+20 延续率
    double_confirm: float = 0.0  # 双确认率
    avg_t5: float = 0.0
    max_drawdown: float = 0.0
    n: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ABResult:
    """A/B 对比实验结果。"""
    plans: dict = field(default_factory=dict)      # {"A": PlanMetrics, ...}
    p_mcnemar: float | None = None                 # McNemar p 值
    p_paired_t: float | None = None                # 配对 t 检验 p 值
    accuracy_lift: float | None = None             # C vs A 准确率提升（百分点）
    ci_lower: float | None = None                  # 95% CI
    ci_upper: float | None = None
    decision: str = "No-Go"
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "plans": {k: v.to_dict() for k, v in self.plans.items()},
            "p_mcnemar": self.p_mcnemar,
            "p_paired_t": self.p_paired_t,
            "accuracy_lift": self.accuracy_lift,
            "ci": [self.ci_lower, self.ci_upper],
            "decision": self.decision,
            "reasons": self.reasons,
        }


def _chi2_sf(chi2: float, df: int = 1) -> float:
    """卡方分布生存函数（上尾概率）近似。用不完全伽马函数。

    简化实现：用正态近似（对于 df=1，卡方√χ² 近似标准正态）。
    """
    if chi2 <= 0:
        return 1.0
    # 对于 df=1，P(χ²>z) ≈ 2*(1-Φ(√z))，使用误差函数
    z = math.sqrt(chi2)
    # Φ(z) = 0.5*(1+erf(z/√2))
    phi = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    p = 2 * (1 - phi)  # 双侧
    return min(1.0, p)


def _t_sf(t: float, df: int) -> float:
    """t 分布双侧 p 值（用 Beta 不完全函数近似）。

    简化：对 |t| 用正态近似 + 自由度修正（df>30 时用正态）。
    """
    if df <= 0:
        return 1.0
    if df >= 30:
        z = abs(t)
        phi = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        return 2 * (1 - phi)
    # 小样本近似：用 t 值经验估计（简化，够用）
    z = abs(t) * math.sqrt(df / max(df - 1.5, 0.5))
    phi = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return min(1.0, 2 * (1 - phi))


def _mcnemar(plan_a_correct: np.ndarray, plan_c_correct: np.ndarray) -> float:
    """McNemar 检验：比较两个方案在成对样本上的正确性。

    H0：两方案准确率无差异。返回 p 值。
    b = A 对 C 错；c = A 错 C 对。
    """
    b = int(((plan_a_correct == 1) & (plan_c_correct == 0)).sum())
    c = int(((plan_a_correct == 0) & (plan_c_correct == 1)).sum())
    if b + c == 0:
        return 1.0
    # McNemar 卡方 = (|b-c|-1)^2 / (b+c) （连续性校正）
    chi2 = (abs(b - c) - 1) ** 2 / (b + c)
    return _chi2_sf(chi2, 1)


def _paired_t(plan_a: np.ndarray, plan_c: np.ndarray) -> tuple[float, float, float, float]:
    """配对 t 检验。返回 (p_value, mean_diff, ci_lower, ci_upper)。"""
    diff = plan_c - plan_a
    if len(diff) < 2 or diff.std() == 0:
        return 1.0, 0.0, 0.0, 0.0
    n = len(diff)
    mean = float(diff.mean())
    se = float(diff.std(ddof=1) / math.sqrt(n))
    t_stat = mean / se if se > 0 else 0.0
    p = _t_sf(t_stat, n - 1)
    ci_lower = mean - 1.96 * se
    ci_upper = mean + 1.96 * se
    return p, mean, ci_lower, ci_upper


def compute_plan_metrics(name: str, t5: list[float | None], t20: list[float | None] | None = None) -> PlanMetrics:
    """计算单方案指标。t5/t20 为各交易日的收益（与另一方案对齐）。"""
    m = PlanMetrics(name=name)
    t5_arr = np.array([x for x in t5 if x is not None], dtype=float)
    if len(t5_arr) == 0:
        return m
    m.n = len(t5_arr)
    m.hit_rate = float((t5_arr > 0).mean())
    m.accuracy = float((t5_arr > 0.05).mean())
    m.avg_t5 = float(t5_arr.mean())
    if t20:
        t20_arr = np.array([x for x in t20 if x is not None], dtype=float)
        if len(t20_arr):
            m.t20_cont = float((t20_arr > 0).mean())
            pair = min(len(t5_arr), len(t20_arr))
            m.double_confirm = float(np.mean([t5_arr[i] > 0.05 and t20_arr[i] > 0 for i in range(pair)]))
    # 简单回撤（用累积收益近似）
    eq = np.cumprod(1 + t5_arr)
    peak = np.maximum.accumulate(eq)
    m.max_drawdown = float(np.min((eq - peak) / np.maximum(peak, 1e-9)))
    return m


def run_ab_validation(
    plan_a_t5: list[float | None],
    plan_c_t5: list[float | None],
    plan_b_t5: list[float | None] | None = None,
    plan_a_t20: list[float | None] | None = None,
    plan_c_t20: list[float | None] | None = None,
    min_lift: float = 3.0,   # 最小可接受准确率提升（百分点）
    alpha: float = 0.05,
) -> ABResult:
    """执行 A/B 对比验证并输出上线决策。

    Args:
        plan_a_t5: 基线方案（仅正向）各交易日的 T+5 收益
        plan_c_t5: 完整方案各交易日的 T+5 收益
        plan_b_t5: 中间方案（+负模式库）可选
        plan_a_t20/plan_c_t20: T+20 收益（可选）
        min_lift: 最小可接受提升（百分点）
        alpha: 显著性水平

    Returns:
        ABResult
    """
    res = ABResult()

    # 对齐长度（按同一交易日配对）
    n = min(len(plan_a_t5), len(plan_c_t5))
    if n == 0:
        res.reasons.append("无有效配对样本")
        return res
    a = np.array([plan_a_t5[i] if plan_a_t5[i] is not None else 0.0 for i in range(n)])
    c = np.array([plan_c_t5[i] if plan_c_t5[i] is not None else 0.0 for i in range(n)])

    res.plans["A"] = compute_plan_metrics("A", list(a), plan_a_t20)
    res.plans["C"] = compute_plan_metrics("C", list(c), plan_c_t20)
    if plan_b_t5:
        b = np.array([plan_b_t5[i] if plan_b_t5[i] is not None else 0.0 for i in range(min(len(plan_b_t5), n))])
        res.plans["B"] = compute_plan_metrics("B", list(b))

    # 正确性（T+5 涨>5% 视为正确）
    a_correct = (a > 0.05).astype(int)
    c_correct = (c > 0.05).astype(int)

    # 统计检验
    res.p_mcnemar = _mcnemar(a_correct, c_correct)
    p_paired, mean_diff, ci_lower, ci_upper = _paired_t(a_correct, c_correct)
    res.p_paired_t = p_paired
    res.accuracy_lift = float((c_correct.mean() - a_correct.mean()) * 100)  # 百分点
    res.ci_lower = ci_lower * 100
    res.ci_upper = ci_upper * 100

    # ── 上线决策门（Go/No-Go）──
    acc_a = res.plans["A"].accuracy * 100
    acc_c = res.plans["C"].accuracy * 100
    reasons = res.reasons

    if res.p_mcnemar >= alpha and res.p_paired_t >= alpha:
        reasons.append(f"提升不显著（McNemar p={res.p_mcnemar:.3f}, 配对t p={res.p_paired_t:.3f}）")
    if res.accuracy_lift < min_lift:
        reasons.append(f"准确率提升 {res.accuracy_lift:.1f}pct < 最小可接受 {min_lift}pct")
    if res.ci_lower is not None and res.ci_lower <= 0:
        reasons.append(f"95% CI [{res.ci_lower:.2f}, {res.ci_upper:.2f}] 含 0")
    if res.plans["C"].max_drawdown < res.plans["A"].max_drawdown - 0.05:
        reasons.append("最大回撤显著劣化")

    res.decision = "Go" if not reasons else "No-Go"
    if res.decision == "Go":
        reasons.append(f"样本外验证通过：准确率 {acc_a:.1f}% → {acc_c:.1f}%（+{res.accuracy_lift:.1f}pct, p<{alpha}）")

    return res
