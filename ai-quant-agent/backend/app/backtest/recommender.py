"""每日推荐引擎（recommender）

对齐规划：plans/04-回测引擎.md 五/六
  每日推荐流程（三层）：
    第零层 形态识别：识别当前股票启动形态（A/B/C/D）
    第一层 形态粗筛：ChromaDB 正模式库余弦相似度，选候选池 Top-100
    第二层 条件概率打分：检查满足的高胜率阈值条件 → 综合上涨概率（主评分）
    第三层 反向排除：与负模式库相似度高 → 压低/剔除
    风险过滤 → 按综合上涨概率排序 → TOP-30

  用户核心需求：按"满足哪些高胜率条件 → 综合上涨概率"排序，
                而非只按形态相似度。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.core.logger import logger
from app.backtest.threshold_analyzer import ProbabilityTable, score_by_probability_table
from app.backtest.vector_store import VectorStore


@dataclass
class Recommendation:
    """一条推荐。"""
    ts_code: str
    name: str = ""
    form_type: str = ""
    prob: float = 0.0                 # 综合上涨概率（主评分）
    positive_score: float = 0.0       # 正向分（条件概率）
    negative_score: float = 0.0       # 负向分（与失败模式相似度）
    similarity: float = 0.0           # 形态相似度（粗筛用）
    hit_rules: list = field(default_factory=list)
    risk_level: str = "低"

    def to_dict(self) -> dict:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "form_type": self.form_type,
            "prob": round(self.prob, 4),
            "positive_score": round(self.positive_score, 4),
            "negative_score": round(self.negative_score, 4),
            "similarity": round(self.similarity, 4),
            "hit_rules": self.hit_rules,
            "risk_level": self.risk_level,
        }


class DailyRecommender:
    """每日推荐器（依赖模式库 + 条件概率表）。"""

    def __init__(
        self,
        vector_store: VectorStore | None = None,
        prob_table: ProbabilityTable | None = None,
        lambda_exclude: float = 0.5,
        neg_high_threshold: float = 0.75,
    ):
        self.store = vector_store or VectorStore()
        self.prob_table = prob_table or ProbabilityTable()
        self.lambda_exclude = lambda_exclude
        self.neg_high_threshold = neg_high_threshold

    def _classify_form(self, feature_dict: dict) -> str:
        """简化形态识别：优先用已提取的 form_type，或按特征推断。"""
        form = feature_dict.get("_form_type", "A")
        return str(form) if form in ("A", "B", "C", "D") else "A"

    def recommend_single(
        self,
        ts_code: str,
        form_type: str,
        feature_vector: np.ndarray,
        feature_dict: dict,
        name: str = "",
    ) -> Recommendation:
        """对单只股票计算推荐（综合上涨概率 + 反向排除）。"""
        # ── 第一层：形态粗筛（正模式库相似度）──
        matches = self.store.query("success", form_type, feature_vector, top_k=1)
        similarity = matches[0].similarity if matches else 0.0

        # ── 第二层：条件概率打分（主评分）──
        prob, hits = score_by_probability_table(feature_dict, self.prob_table)

        # ── 第三层：反向排除（负模式库）──
        neg_matches = self.store.query("failure", form_type, feature_vector, top_k=1)
        negative_score = neg_matches[0].similarity if neg_matches else 0.0

        final_prob = prob * (1 - self.lambda_exclude * negative_score)
        risk = "高" if negative_score > self.neg_high_threshold else ("中" if negative_score > 0.6 else "低")

        return Recommendation(
            ts_code=ts_code,
            name=name,
            form_type=form_type,
            prob=final_prob,
            positive_score=prob,
            negative_score=negative_score,
            similarity=similarity,
            hit_rules=hits,
            risk_level=risk,
        )

    def recommend_market(
        self,
        candidates: list[dict],
        top_k: int = 30,
    ) -> list[Recommendation]:
        """对候选池计算推荐并排序。

        Args:
            candidates: [{"ts_code","name","form_type","feature_vector","feature_dict"}]
            top_k: 返回 TOP-K

        Returns:
            按综合上涨概率降序的 Recommendation 列表
        """
        recs: list[Recommendation] = []
        for c in candidates:
            try:
                r = self.recommend_single(
                    ts_code=c["ts_code"],
                    form_type=c.get("form_type", "A"),
                    feature_vector=c["feature_vector"],
                    feature_dict=c.get("feature_dict", {}),
                    name=c.get("name", ""),
                )
                recs.append(r)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"推荐 {c.get('ts_code')} 失败: {exc}")
                continue
        recs.sort(key=lambda r: -r.prob)
        return recs[:top_k]
