"""成功样本集构建（pattern_miner）

对齐规划：plans/04-回测引擎.md 4.1 与第十四章第 8 项
  职责：从 outcome 分流结果中筛出"成功样本"（success），
        提取起爆前全维度特征，形成正样本集（供模式库/归因/条件概率消费）。

  成功/失败样本提取逻辑统一收敛到 extract_for_outcomes（本文件），
  failure_miner 复用同一函数，确保正负样本共用同一套特征口径（正负可比）。
"""
from __future__ import annotations

import numpy as np

from app.core.logger import logger
from app.backtest.outcome_tracker import LABEL_SUCCESS, LABEL_NEAR
from app.backtest.feature_extractor import extract_features, FeatureVector

# 默认纳入成功样本集的标签（可扩展，如把 near_success 一并纳入训练）
POSITIVE_LABELS = (LABEL_SUCCESS,)


def extract_for_outcomes(
    df,
    outcomes: list,
    labels: tuple[str, ...],
    extra: dict | None = None,
    extra_map: dict[int, dict] | None = None,
) -> list[FeatureVector]:
    """通用：从 outcome 样本中筛出 labels 命中的样本并提取特征向量。

    Args:
        df: prepare_stock_data 输出（与 outcomes 同股票）
        outcomes: OutcomeSample 列表
        labels: 要纳入的标签元组
        extra: 全局额外特征（对全部样本相同）
        extra_map: 按 t0_idx 的额外特征映射（每样本独立，来自现有数据库增强表）

    Returns:
        FeatureVector 列表（已携带 _outcome_t5/_outcome_t20 内部键）
    """
    result: list[FeatureVector] = []
    for o in outcomes:
        if o.label not in labels:
            continue
        try:
            ex = (extra_map or {}).get(o.t0_idx, extra) if extra_map else extra
            fv = extract_features(
                df, o.t0_idx,
                ts_code=o.ts_code, form_type=o.form_type, label=o.label,
                extra=ex,
            )
            # 携带 outcome 结果（后续条件概率/绩效/A-B 验证使用）
            fv.features["_outcome_t5"] = o.outcome_t5 if o.outcome_t5 is not None else np.nan
            fv.features["_outcome_t20"] = o.outcome_t20 if o.outcome_t20 is not None else np.nan
            # 携带 T0 日期（plans/23 T1 修复）：绩效净值必须按"交易日等权聚合"，
            # 否则同一天的几十上百笔样本会被当成连续多次下注，净值指数级爆炸。
            fv.features["_outcome_t0_date"] = str(o.t0_date)[:10]
            result.append(fv)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"pattern_miner 提取特征失败 {o.ts_code}@{o.t0_idx}: {exc}")
            continue
    return result


def mine_positive_patterns(
    df,
    outcomes: list,
    extra: dict | None = None,
    extra_map: dict[int, dict] | None = None,
    labels: tuple[str, ...] = POSITIVE_LABELS,
) -> list[FeatureVector]:
    """构建成功样本集（正样本模式）。

    Returns:
        正样本 FeatureVector 列表
    """
    fvs = extract_for_outcomes(df, outcomes, labels, extra, extra_map)
    if fvs:
        logger.debug(f"pattern_miner: 成功样本 {len(fvs)} 条")
    return fvs


def merge_positive_sets(batches: list[list[FeatureVector]]) -> list[FeatureVector]:
    """合并多批成功样本（供 runner 在逐股循环后汇总）。"""
    merged: list[FeatureVector] = []
    for b in batches:
        merged.extend(b)
    return merged


# 便于调试：样本标签常量再导出
__all__ = ["mine_positive_patterns", "extract_for_outcomes", "merge_positive_sets", "POSITIVE_LABELS"]
