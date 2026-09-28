"""失败样本集构建（failure_miner）

对齐规划：plans/04-回测引擎.md 4.2 与第十四章第 8 项
  职责：从 outcome 分流结果中筛出"失败样本"（failure_A / failure_B），
        提取起爆前全维度特征，形成负样本集。

  与 pattern_miner 共用 extract_for_outcomes（正负样本同一套特征口径，确保可比），
  本模块只做"负样本标签选择 + 语义化包装"。

  负样本类别说明：
    failure_A：涨了 50%~150% 但没到 3 倍且从高点回落 >30%（"看着能成、实际失败"）
    failure_B：启动后 20 日涨幅 <20%（"直接熄火"）
  每日推荐的反向排除（recommender）主要消费 failure_A 的形态画像。
"""
from __future__ import annotations

from app.core.logger import logger
from app.backtest.outcome_tracker import LABEL_FAILURE_A, LABEL_FAILURE_B
from app.backtest.feature_extractor import FeatureVector
from app.backtest.pattern_miner import extract_for_outcomes

# 默认纳入失败样本集的标签
NEGATIVE_LABELS = (LABEL_FAILURE_A, LABEL_FAILURE_B)


def mine_failure_patterns(
    df,
    outcomes: list,
    extra: dict | None = None,
    extra_map: dict[int, dict] | None = None,
    labels: tuple[str, ...] = NEGATIVE_LABELS,
) -> list[FeatureVector]:
    """构建失败样本集（负样本模式）。

    Args:
        df: prepare_stock_data 输出
        outcomes: OutcomeSample 列表
        extra: 全局额外特征（资金面/基本面等）
        extra_map: 按 t0_idx 的额外特征映射（来自现有数据库增强表）
        labels: 要纳入的失败标签（默认 failure_A + failure_B）

    Returns:
        负样本 FeatureVector 列表
    """
    fvs = extract_for_outcomes(df, outcomes, labels, extra, extra_map)
    if fvs:
        logger.debug(f"failure_miner: 失败样本 {len(fvs)} 条")
    return fvs


def split_by_type(feature_vectors: list[FeatureVector]) -> dict[str, list[FeatureVector]]:
    """按失败细分类型分组（failure_A / failure_B）。"""
    groups: dict[str, list[FeatureVector]] = {}
    for fv in feature_vectors:
        groups.setdefault(fv.label, []).append(fv)
    return groups


__all__ = ["mine_failure_patterns", "split_by_type", "NEGATIVE_LABELS"]
