"""条件概率统计（threshold_analyzer）⭐用户核心需求

对齐规划：plans/04-回测引擎.md 4.7
  - 统计"特征阈值 → 上涨概率"的条件概率表（全表全字段）
  - 单特征阈值：分位数/信息增益自动挖最优切分点 → 各区间上涨概率
  - 多特征组合：组合条件 → 联合上涨概率
  - 每日推荐按命中的条件组合计算综合上涨概率排序

  说明：本模块基于特征向量矩阵计算条件概率表。
        "全表全字段"由 feature_extractor 的 FEATURE_SCHEMA 与关联表字段扩展决定，
        此处支持任意数值字段的阈值统计。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logger import logger

# 条件概率表最小样本量（避免小样本偶然）
MIN_SAMPLE = 30
# 上涨判定：T+5 涨幅 >5% 视为上涨（主评估口径）
UP_THRESHOLD = 0.05
# 未命中任何规则的"中性基线"（B3 修复）：原实现未命中时返回样本内 baseline_prob，
# 而该值在 success 为主的样本下虚高（实测 0.79），导致每日推荐所有股票 up_probability ≥79%、
# 主评分失去区分度。改为中性 0.5——只有真正命中高胜率条件才显著抬升概率。
NEUTRAL_BASELINE = 0.6
# 条件概率表落盘（回测产出 → 每日推荐复用同一份，防参数漂移）
PROB_TABLE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "probability_table.json",
)


@dataclass
class ThresholdRule:
    """单特征阈值规则。"""
    feature: str
    threshold: float          # 阈值（>= threshold 为高分组）
    sample_count: int
    up_prob: float            # 高分组的 T+5 上涨概率
    baseline_prob: float      # 全样本基线上涨概率
    gain: float               # 信息增益/提升
    direction: str = "high"   # high=高于阈值 / low=低于阈值


@dataclass
class ComboRule:
    """多特征组合规则。"""
    features: list[str]
    thresholds: list[float]
    sample_count: int
    up_prob: float
    baseline_prob: float
    lift: float
    directions: list[str] = field(default_factory=list)  # 每个条件的方向（high/low），与 features 对齐


@dataclass
class ProbabilityTable:
    """条件概率表。"""
    rules: list[ThresholdRule] = field(default_factory=list)
    combos: list[ComboRule] = field(default_factory=list)
    baseline_prob: float = 0.0
    sample_count: int = 0

    def to_records(self) -> list[dict]:
        return [
            {
                "type": "single",
                "feature": r.feature,
                "threshold": r.threshold,
                "sample_count": r.sample_count,
                "up_prob": r.up_prob,
                "baseline_prob": r.baseline_prob,
                "gain": r.gain,
            }
            for r in self.rules
        ] + [
            {
                "type": "combo",
                "features": " + ".join(c.features),
                "thresholds": c.thresholds,
                "sample_count": c.sample_count,
                "up_prob": c.up_prob,
                "baseline_prob": c.baseline_prob,
                "lift": c.lift,
            }
            for c in self.combos
        ]


def _is_up(outcome_t5: float | None) -> bool:
    """上涨判定：T+5 涨幅 > 5%。"""
    return outcome_t5 is not None and outcome_t5 > UP_THRESHOLD


def analyze_single_features(
    feature_matrix: pd.DataFrame,
    labels: np.ndarray,
    min_sample: int = MIN_SAMPLE,
    quantiles: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8),
    min_gain: float = 0.0,
    max_overlap: float = 0.9,
    directions: tuple[str, ...] = ("high",),
    min_edge: float = 0.0,
    max_coverage: float = 1.0,
) -> list[ThresholdRule]:
    """对每个数值特征统计"各分位阈值切分后的上涨概率"。

    Args:
        feature_matrix: 特征 DataFrame（列=特征）
        labels: 上涨标签数组（0/1），与 feature_matrix 行对齐
        min_sample: 每组最小样本量
        quantiles: 尝试的切分分位
        min_gain: 规则相对基线的最小信息增益（过滤无效/微增益规则）
        max_overlap: 已选规则高分组样本的 Jaccard 重叠率上限；超过则视为冗余规则丢弃
            （修复：起爆点附近技术指标高度共线，多规则命中同一批样本、统计完全相同，
             导致概率表区分度低——对共线特征只保留增益最高的一条）
        directions: 分析的方向。默认只挖掘 "high"（值越大→上涨率越高）；
            条件参数分析（build_condition_table）传 ("high", "low")，同时考察
            "越来越大"与"越来越小"对上涨的影响（用户架构：参考条件，非筛选）。
        min_edge: 相对基线的最小提升（plans/23 T2）。up_prob 只比基线高一点点的"伪高胜率
            规则"没有意义，默认 0（保持向后兼容），条件表传 CONDITION_MIN_EDGE。
        max_coverage: 单条规则覆盖率上限（plans/23 T2 根因修复）。像 `v <= 80%分位`
            这种覆盖过半样本的"宽规则"毫无筛选力，却会让"命中≥1条规则"恒为真
            （实测导致 A/B 方案 C 命中全样本 424/424 → C≡A、提升恒为 0、决策永远 No-Go）。
            默认 1.0（不限制），条件表传 CONDITION_MAX_COVERAGE。

    Returns:
        最优切分规则列表（每特征每方向最多一条，且高分组样本不高度重叠）
    """
    n = len(labels)
    if n == 0:
        return []
    baseline = float(labels.mean())

    candidates: list[tuple[ThresholdRule, np.ndarray]] = []
    for col in feature_matrix.columns:
        values = feature_matrix[col].to_numpy(dtype=float)
        valid = ~np.isnan(values)
        if valid.sum() < min_sample * 2:
            continue

        v = values[valid]
        y = labels[valid]
        best: ThresholdRule | None = None
        best_mask: np.ndarray | None = None
        for direction in directions:
            for q in quantiles:
                thr = np.nanquantile(v, q)
                if q == 1.0 or q == 0.0:
                    continue
                # high：v >= thr（越来越大）；low：v <= thr（越来越小）
                if direction == "low":
                    mask = v <= thr
                else:
                    mask = v >= thr
                if mask.sum() < min_sample or (~mask).sum() < min_sample:
                    continue
                # 覆盖率上限：宽规则无筛选力（plans/23 T2 根因）
                if max_coverage < 1.0 and mask.sum() > max_coverage * len(v):
                    continue
                up = float(y[mask].mean())
                if up < baseline + min_edge:
                    continue
                # 信息增益（简化：与基线的提升）
                gain = up - baseline
                if best is None or gain > best.gain:
                    # 对齐到完整样本长度 n（NaN 位 False），供跨特征 Jaccard 去重比较
                    # （条件表原始值含 NaN，各特征有效样本数不同，mask 必须同长）
                    full_mask = np.zeros(n, dtype=bool)
                    full_mask[valid] = mask
                    best = ThresholdRule(
                        feature=str(col), threshold=float(thr),
                        sample_count=int(mask.sum()), up_prob=up,
                        baseline_prob=baseline, gain=gain, direction=direction,
                    )
                    best_mask = full_mask
        if best is not None and best_mask is not None and best.gain >= min_gain:
            candidates.append((best, best_mask))

    # 重叠去重：按 gain 降序，高分组样本 Jaccard 重叠率 >= max_overlap 的冗余规则跳过
    candidates.sort(key=lambda c: -c[0].gain)
    chosen: list[ThresholdRule] = []
    chosen_masks: list[np.ndarray] = []
    for rule, mask in candidates:
        redundant = False
        for cm in chosen_masks:
            inter = int(np.logical_and(mask, cm).sum())
            union = int(np.logical_or(mask, cm).sum())
            if union > 0 and inter / union >= max_overlap:
                redundant = True
                break
        if redundant:
            continue
        chosen.append(rule)
        chosen_masks.append(mask)

    if len(chosen) < len(candidates):
        logger.info(
            f"概率表规则去重：{len(candidates)} 条候选 → {len(chosen)} 条"
            f"（剔除 {len(candidates) - len(chosen)} 条高重叠冗余规则）"
        )
    return chosen


def analyze_combos(
    feature_matrix: pd.DataFrame,
    labels: np.ndarray,
    top_rules: list[ThresholdRule],
    max_depth: int = 3,
    min_sample: int = MIN_SAMPLE,
    min_lift: float = 0.10,
) -> list[ComboRule]:
    """从单特征高胜率阈值中逐步组合（2→3→4 条件），挖掘联合上涨概率。

    Args:
        feature_matrix: 特征 DataFrame
        labels: 上涨标签
        top_rules: 单特征最优规则（取 Top-N 参与组合）
        max_depth: 最大组合条件数
        min_sample: 组合样本量下限
        min_lift: 组合相对基线的最小提升

    Returns:
        组合规则列表（按 lift 排序）
    """
    if not top_rules or len(labels) == 0:
        return []
    baseline = float(labels.mean())
    n = len(labels)
    combos: list[ComboRule] = []

    # 取 top 单规则（最多 8 个）参与组合
    pool = top_rules[:8]

    def _mask(rule: ThresholdRule) -> np.ndarray:
        v = feature_matrix[rule.feature].to_numpy(dtype=float)
        # 按规则方向判断：high=值越大（>= 阈值）；low=值越小（<= 阈值）
        if rule.direction == "low":
            return v <= rule.threshold
        return v >= rule.threshold

    # 2 条件组合
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            m = _mask(pool[i]) & _mask(pool[j])
            if m.sum() < min_sample:
                continue
            p = float(labels[m].mean())
            if p - baseline >= min_lift:
                combos.append(
                    ComboRule(
                        features=[pool[i].feature, pool[j].feature],
                        thresholds=[pool[i].threshold, pool[j].threshold],
                        directions=[pool[i].direction, pool[j].direction],
                        sample_count=int(m.sum()), up_prob=p, baseline_prob=baseline, lift=p - baseline,
                    )
                )

    # 3 条件组合
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            for k in range(j + 1, len(pool)):
                m = _mask(pool[i]) & _mask(pool[j]) & _mask(pool[k])
                if m.sum() < min_sample:
                    continue
                p = float(labels[m].mean())
                if p - baseline >= min_lift:
                    combos.append(
                        ComboRule(
                            features=[pool[i].feature, pool[j].feature, pool[k].feature],
                            thresholds=[pool[i].threshold, pool[j].threshold, pool[k].threshold],
                            directions=[pool[i].direction, pool[j].direction, pool[k].direction],
                            sample_count=int(m.sum()), up_prob=p, baseline_prob=baseline, lift=p - baseline,
                        )
                    )

    combos.sort(key=lambda c: -c.lift)
    return combos


def build_probability_table(
    feature_matrix: pd.DataFrame,
    labels: np.ndarray,
    min_sample: int = MIN_SAMPLE,
) -> ProbabilityTable:
    """构建完整条件概率表（单特征 + 组合）。

    Args:
        feature_matrix: 特征 DataFrame（已归一化或原始均可，分位切分对量纲不敏感）
        labels: 上涨标签 0/1

    Returns:
        ProbabilityTable
    """
    table = ProbabilityTable()
    if len(labels) == 0:
        return table
    table.sample_count = int(len(labels))
    table.baseline_prob = float(np.nanmean(labels))

    # 单特征
    single = analyze_single_features(feature_matrix, labels, min_sample)
    table.rules = single

    # 组合
    table.combos = analyze_combos(feature_matrix, labels, single, min_sample=min_sample)

    return table


def _rule_hit(v, rule: ThresholdRule) -> bool:
    """单条规则命中判定：按方向 high（>= 阈值）或 low（<= 阈值）。"""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return False
    if rule.direction == "low":
        return v <= rule.threshold
    return v >= rule.threshold


def score_by_probability_table(
    feature_dict: dict,
    table: ProbabilityTable,
    up_threshold: float = UP_THRESHOLD,
) -> tuple[float, list[dict]]:
    """对一只股票的当前特征，按条件概率表计算综合上涨概率。

    Args:
        feature_dict: 该股票当前特征 {feature: value}
        table: 条件概率表（含 high/low 方向规则）

    Returns:
        (综合上涨概率, 命中的规则列表)
    """
    if not table.rules and not table.combos:
        return NEUTRAL_BASELINE, []

    hits: list[dict] = []
    # 单特征命中（按方向：high=值越大 / low=值越小）
    for r in table.rules:
        v = feature_dict.get(r.feature, np.nan)
        if _rule_hit(v, r):
            hits.append({
                "feature": r.feature, "threshold": r.threshold,
                "up_prob": r.up_prob, "direction": r.direction,
            })

    # B3 修复：未命中任何高胜率条件 → 返回中性基线 0.5（而非样本内虚高 baseline_prob，
    # 避免每日推荐全部股票概率 ≥0.79、主评分失去区分度）。
    if not hits:
        return NEUTRAL_BASELINE, []

    # 综合概率：1 - Π(1 - p_i)（独立条件近似）
    combined = 1.0
    for h in hits[:10]:
        combined *= 1.0 - h["up_prob"]
    prob = 1.0 - combined

    # 用组合规则修正（若命中某组合，取更高概率；组合内各条件按各自方向判定）
    for c in table.combos:
        all_hit = True
        for f, t, d in zip(c.features, c.thresholds, c.directions or ["high"] * len(c.features)):
            v = feature_dict.get(f, np.nan)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                all_hit = False
                break
            hit = (v <= t) if d == "low" else (v >= t)
            if not hit:
                all_hit = False
                break
        if all_hit and c.up_prob > prob:
            prob = c.up_prob
            hits.append({"combo": "+".join(c.features), "up_prob": c.up_prob})

    return min(prob, 0.99), hits


def save_probability_table(table: ProbabilityTable, path: str = PROB_TABLE_FILE) -> None:
    """固化条件概率表到磁盘（每日推荐加载同一份）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "baseline_prob": float(table.baseline_prob),
            "sample_count": int(table.sample_count),
            "rules": [r.__dict__ for r in table.rules],
            "combos": [c.__dict__ for c in table.combos],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        logger.info(f"条件概率表已保存: {path}（{len(table.rules)} 规则 + {len(table.combos)} 组合）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"保存条件概率表失败: {exc}")


def load_probability_table(path: str = PROB_TABLE_FILE) -> ProbabilityTable | None:
    """加载已固化的条件概率表。"""
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        rules = [ThresholdRule(**r) for r in d.get("rules", [])]
        combos = [ComboRule(**c) for c in d.get("combos", [])]
        return ProbabilityTable(
            rules=rules,
            combos=combos,
            baseline_prob=float(d.get("baseline_prob", 0.0)),
            sample_count=int(d.get("sample_count", 0)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"加载条件概率表失败: {exc}")
        return None


# ──────────────────────────────────────────────
# 采样条件参数表（用户架构：基本面/筹码/资金等不进向量特征、不归一化，
# 作为采样条件独立统计"越来越大/越来越小对上涨的影响"）
# ──────────────────────────────────────────────
CONDITION_TABLE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "condition_table.json",
)
# 条件表规则质量门槛（plans/23 T2）：
#   min_edge     —— up_prob 必须比基线高 5pct 以上，否则不算"高胜率条件"
#   max_coverage —— 单条规则最多覆盖 50% 样本，超出即"宽规则"，无筛选力
# 修复前：只要 up_prob >= baseline 就成规则，30 特征×2 方向×4 分位几乎必然
# 产生"每条样本都命中"的宽规则 → A/B 方案 C ≡ 方案 A（提升恒为 0，决策永远 No-Go）。
CONDITION_MIN_EDGE = 0.05
CONDITION_MAX_COVERAGE = 0.5


def build_condition_table(
    condition_matrix: pd.DataFrame,
    labels: np.ndarray,
    min_sample: int = MIN_SAMPLE,
) -> ProbabilityTable:
    """对条件参数（原始值，不归一化）做分位切分统计，构建"条件→T+5上涨率"表。

    与 build_probability_table 的区别：
      - 输入是条件参数原始值（bps/人均持股/筹码集中度/ROE 等），不做 Z-score 归一化，
        因为条件参数的"绝对大小/趋势"本身就是分析对象（越大越集中/越小越亏损等）。
      - 分位切分对量纲不敏感，阈值即原始值分位点，供每日推荐按原始值查表。
      - 同时考察 high（越来越大→上涨率）与 low（越来越小→上涨率）两个方向
        （用户架构：参考条件，非筛选；不浪费拆分出的条件参数）。
    供 runner 在采样阶段构建并固化 condition_table.json。
    """
    table = ProbabilityTable()
    if len(labels) == 0 or condition_matrix is None or condition_matrix.empty:
        return table
    table.sample_count = int(len(labels))
    table.baseline_prob = float(np.nanmean(labels))
    # 规则质量门槛可进化（plans/23 §十二）：进化大脑可调 min_edge / max_coverage
    min_edge, max_coverage = CONDITION_MIN_EDGE, CONDITION_MAX_COVERAGE
    try:
        from app.agents import evolution_config
        min_edge = float(evolution_config.get_num("bt_cond_min_edge", CONDITION_MIN_EDGE))
        max_coverage = float(evolution_config.get_num("bt_cond_max_coverage", CONDITION_MAX_COVERAGE))
    except Exception:  # noqa: BLE001
        pass
    table.rules = analyze_single_features(
        condition_matrix, labels, min_sample, directions=("high", "low"),
        min_edge=min_edge, max_coverage=max_coverage,
    )
    table.combos = analyze_combos(condition_matrix, labels, table.rules, min_sample=min_sample)
    return table


def save_condition_table(table: ProbabilityTable, path: str = CONDITION_TABLE_FILE) -> None:
    """固化采样条件参数表（每日推荐按原始值查表打分）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "baseline_prob": float(table.baseline_prob),
            "sample_count": int(table.sample_count),
            "rules": [r.__dict__ for r in table.rules],
            "combos": [c.__dict__ for c in table.combos],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        logger.info(f"采样条件参数表已保存: {path}（{len(table.rules)} 规则 + {len(table.combos)} 组合）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"保存采样条件参数表失败: {exc}")


def load_condition_table(path: str = CONDITION_TABLE_FILE) -> ProbabilityTable | None:
    """加载已固化的采样条件参数表。"""
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        rules = [ThresholdRule(**r) for r in d.get("rules", [])]
        combos = [ComboRule(**c) for c in d.get("combos", [])]
        return ProbabilityTable(
            rules=rules,
            combos=combos,
            baseline_prob=float(d.get("baseline_prob", 0.0)),
            sample_count=int(d.get("sample_count", 0)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"加载采样条件参数表失败: {exc}")
        return None
