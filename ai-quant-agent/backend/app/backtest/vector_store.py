"""ChromaDB 模式库（vector_store）

对齐规划：plans/04-回测引擎.md 二/四/五
  - 正/负模式库按启动形态分组存储：
      short_term_patterns_success_A~D / short_term_patterns_failure_A~D
  - metadata 记录 form_type / cluster_id / ts_code / t0_date / outcome
  - 形态匹配（粗筛）：用归一化特征向量做余弦相似度检索 Top-K
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logger import logger
from app.backtest.feature_extractor import FEATURE_NAMES, MATCH_FEATURES

try:
    import chromadb
    CHROMA_AVAILABLE = True
except Exception:  # noqa: BLE001
    CHROMA_AVAILABLE = False

# 形态类型（正/负模式库都按此分组；E 连板难买入不建正库）
FORMS = ("A", "B", "C", "D")
# ChromaDB 持久化路径
CHROMA_PATH = "./data/chroma_db"

# 集合前缀带当前特征维度（ChromaDB 集合创建后固定 embedding 维度；
# 特征数变化时用新维度集合，避免 add 维度不匹配报错）
_COLLECTION_PREFIX = "short_term_patterns"
# 模式库默认维度 = 形态核心子集维度（每日推荐新建 VectorStore 时与库内向量一致）
_DEFAULT_DIM = len(MATCH_FEATURES)


@dataclass
class MatchResult:
    """形态匹配结果。"""
    ts_code: str
    form_type: str
    similarity: float
    label: str
    t0_date: str = ""
    outcome_t5: float | None = None

    def to_dict(self) -> dict:
        return {
            "ts_code": self.ts_code,
            "form_type": self.form_type,
            "similarity": round(self.similarity, 4),
            "label": self.label,
            "t0_date": self.t0_date,
            "outcome_t5": self.outcome_t5,
        }


class VectorStore:
    """ChromaDB 模式库管理。"""

    def __init__(self, path: str = CHROMA_PATH, dim: int | None = None):
        self.path = path
        self.dim = int(dim) if dim else _DEFAULT_DIM
        self.client = None
        self._collections: dict = {}
        # 本次构建的元信息（plans/23 T8）：reset=是否全量重建、written_this_run=本次写入条数
        # 供回测报告核对"模式库条数 == 本次样本数"，及时发现再次累积
        self.last_build: dict = {}
        if not CHROMA_AVAILABLE:
            logger.warning("chromadb 未安装，模式库将退化为内存模式")
        else:
            try:
                self.client = chromadb.PersistentClient(path=path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"ChromaDB 初始化失败: {exc}，退化为内存模式")
                self.client = None

    def _collection_name(self, label: str, form_type: str) -> str:
        return f"{_COLLECTION_PREFIX}_d{self.dim}_{label}_{form_type}"

    def _get_collection(self, label: str, form_type: str):
        """获取（或创建）集合。"""
        name = self._collection_name(label, form_type)
        if name in self._collections:
            return self._collections[name]
        if self.client is not None:
            col = self.client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )
        else:
            # 内存降级：用简单 dict 存储
            col = _InMemoryCollection(name)
        self._collections[name] = col
        return col

    def add_patterns(
        self,
        label: str,
        form_type: str,
        vectors: np.ndarray,
        metadatas: list[dict],
    ) -> int:
        """向模式库写入一批向量。

        Args:
            label: "success" / "failure"
            form_type: "A"/"B"/"C"/"D"
            vectors: (N, D) 归一化特征向量
            metadatas: 与向量对应的元信息列表

        Returns:
            写入数量
        """
        if vectors is None or len(vectors) == 0:
            return 0
        if form_type not in FORMS:
            logger.warning(f"形态 {form_type} 不建模式库（仅 A/B/C/D）")
            return 0
        col = self._get_collection(label, form_type)
        ids = [f"{label}_{form_type}_{uuid.uuid4().hex[:12]}" for _ in range(len(vectors))]
        embeddings = vectors.tolist()
        if self.client is not None:
            col.add(embeddings=embeddings, metadatas=metadatas, ids=ids)
        else:
            col.add(embeddings, metadatas, ids)
        return len(ids)

    def reset(self, label: str, form_type: str) -> bool:
        """清空某 (label, form_type) 模式库（plans/23 §2.4 T8 修复）。

        修复前：add_patterns 用 uuid4 作 id 且从不清理 → 每次回测都往同一 collection
        **追加**，模式库无限累积（实测 vector_store_stats 6,249 条 vs 单次回测仅 424 条样本，
        即同一案例存在多份副本）。后果：Top-K 相似检索被"同一案例的多份副本"占满，
        稀释案例多样性，相似度评分失真。
        修复后：回测重建模式库前先清空对应集合（全量重建语义，与"回测产出唯一版本"一致）。
        """
        name = self._collection_name(label, form_type)
        self._collections.pop(name, None)
        if self.client is None:
            return True
        try:
            self.client.delete_collection(name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"清空模式库 {name} 跳过（可能不存在）: {exc}")
            return False

    def query(
        self,
        label: str,
        form_type: str,
        vector: np.ndarray,
        top_k: int = 20,
    ) -> list[MatchResult]:
        """在指定模式库检索 Top-K 最相似向量。

        Args:
            label: "success" / "failure"
            form_type: 形态
            vector: 当前股票归一化特征向量 (D,)
            top_k: 返回数量

        Returns:
            MatchResult 列表（按相似度降序）
        """
        if vector is None or len(vector) == 0:
            return []
        col = self._get_collection(label, form_type)
        results = col.query(vector.tolist(), n_results=top_k)
        matches: list[MatchResult] = []
        metas = results.get("metadatas", [])
        dists = results.get("distances", [])
        # ChromaDB 真实集合对单个查询返回嵌套 [[...]]；内存实现返回扁平 [...]
        # 统一取最内层列表，避免 float(list) 报错
        if metas and isinstance(metas[0], list):
            metas = metas[0]
        if dists and isinstance(dists[0], list):
            dists = dists[0]
        for meta, dist in zip(metas, dists):
            # ChromaDB cosine 距离 → 相似度 = 1 - distance
            similarity = 1.0 - float(dist)
            matches.append(
                MatchResult(
                    ts_code=str(meta.get("ts_code", "")),
                    form_type=str(meta.get("form_type", form_type)),
                    similarity=similarity,
                    label=str(meta.get("label", label)),
                    t0_date=str(meta.get("t0_date", "")),
                    outcome_t5=meta.get("outcome_t5"),
                )
            )
        return matches

    def count(self, label: str, form_type: str) -> int:
        """统计某模式库向量数。"""
        col = self._get_collection(label, form_type)
        return col.count()

    def stats(self) -> dict:
        """全库统计。"""
        result = {}
        for label in ("success", "failure"):
            for form in FORMS:
                try:
                    result[f"{label}_{form}"] = self.count(label, form)
                except Exception:  # noqa: BLE001
                    result[f"{label}_{form}"] = 0
        return result


class _InMemoryCollection:
    """ChromaDB 不可用时的内存降级实现（支持 add/query/count）。"""

    def __init__(self, name: str):
        self.name = name
        self._embeddings: list[list[float]] = []
        self._metadatas: list[dict] = []
        self._ids: list[str] = []

    def add(self, embeddings, metadatas, ids):
        self._embeddings.extend(embeddings)
        self._metadatas.extend(metadatas)
        self._ids.extend(ids)

    def query(self, query_embedding, n_results=20):
        q = np.array(query_embedding, dtype=float)
        if not self._embeddings:
            return {"metadatas": [], "distances": [], "ids": []}
        emb = np.array(self._embeddings, dtype=float)
        # 余弦距离
        dots = emb @ q
        norms = np.linalg.norm(emb, axis=1) * np.linalg.norm(q)
        norms = np.where(norms == 0, 1e-9, norms)
        sims = dots / norms
        k = min(n_results, len(self._embeddings))
        idx = np.argsort(-sims)[:k]
        return {
            "metadatas": [self._metadatas[i] for i in idx],
            "distances": [1 - float(sims[i]) for i in idx],
            "ids": [self._ids[i] for i in idx],
        }

    def count(self):
        return len(self._embeddings)


def build_vector_store(
    feature_vectors: list,
    meta_list: list[dict],
    store: VectorStore | None = None,
    normalizer=None,
    feature_names: list[str] | None = None,
    reset: bool = True,
) -> VectorStore:
    """从特征向量批量构建模式库（按 label + form_type 分组）。

    Args:
        feature_vectors: FeatureVector 列表
        meta_list: 与向量对齐的元信息列表（含 ts_code/form_type/label/t0_date/outcome）
        store: 已有 VectorStore（可选，用于增量）
        normalizer: 已固化的 FeatureNormalizer（用固定参数归一化，保证库内与每日推荐一致）；
                    未提供时内部 fit 训练（回测内部自洽）
        feature_names: 参与向量匹配的特征子集（默认形态核心 MATCH_FEATURES ~33 维；
                       资金/财务/事件等"状态"维度不进向量，避免稀释形态相似度）
        reset: 是否在写入前清空对应集合（默认 True = 全量重建，防模式库跨回测累积；
               plans/23 T8）。传 False 才做"增量追加"。

    Returns:
        VectorStore
    """
    store = store or VectorStore()
    from app.backtest.feature_extractor import FEATURE_NAMES, MATCH_FEATURES
    from app.backtest.feature_extractor import FeatureNormalizer

    if not feature_vectors:
        return store

    # 归一化：优先用固化参数；否则内部训练（参数固定后库内向量与每日推荐可比）
    feature_names = list(feature_names or MATCH_FEATURES)
    mat = np.array([f.to_array(FEATURE_NAMES) for f in feature_vectors], dtype=float)
    if normalizer is None:
        normalizer = FeatureNormalizer.fit(mat, names=FEATURE_NAMES)
    full_norm = normalizer.transform(mat)
    # 只取形态核心子集列（向量库存形态维度；状态维度用于条件概率/归因）
    idx = [FEATURE_NAMES.index(n) for n in feature_names]
    norm = full_norm[:, idx]
    # 集合维度 = 实际向量维度（形态子集），而非全特征维度（保证 ChromaDB 集合维度一致）
    store.dim = norm.shape[1]

    # 按 (label, form_type) 分组写入
    from collections import defaultdict

    groups: dict[tuple[str, str], tuple[list[int], list[dict]]] = defaultdict(lambda: ([], []))
    for i, fv in enumerate(feature_vectors):
        label = "success" if fv.label == "success" else "failure"
        form = fv.form_type
        if form not in FORMS:
            continue
        meta = meta_list[i] if i < len(meta_list) else {}
        groups[(label, form)][0].append(i)

        def _meta_val(v):
            """转为 ChromaDB 可接受的标量（str/int/float/bool）；NaN/None/异常 → None（跳过该键）。"""
            if v is None:
                return None
            if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                return None
            if isinstance(v, (np.integer,)):
                return int(v)
            if isinstance(v, (np.floating,)):
                return float(v)
            if isinstance(v, (np.bool_,)):
                return bool(v)
            if isinstance(v, (str, int, float, bool)):
                return v
            return str(v)

        # 注意：ChromaDB 的 add 不接受 metadata 值为 None，故 None 键直接剔除
        meta_out: dict = {
            "ts_code": fv.ts_code,
            "form_type": fv.form_type,
            "label": fv.label,
            "t0_date": str(meta.get("t0_date", "")),
        }
        t5 = _meta_val(meta.get("outcome_t5"))
        t20 = _meta_val(meta.get("outcome_t20"))
        if t5 is not None:
            meta_out["outcome_t5"] = t5
        if t20 is not None:
            meta_out["outcome_t20"] = t20

        groups[(label, form)][1].append(meta_out)

    total_written = 0
    for (label, form), (idx_list, metas) in groups.items():
        if reset:
            # 全量重建：先清空再写入，避免与历史回测的副本叠加（T8）
            store.reset(label, form)
        vecs = norm[idx_list]
        n = store.add_patterns(label, form, vecs, metas)
        total_written += n
        logger.info(f"模式库 {label}_{form} {'重建' if reset else '追加'} {n} 条")

    # 记录本次重建规模（T8 验收口径：全量重建时模式库总数应等于本次写入条数）
    store.last_build = {
        "reset": bool(reset),
        "written_this_run": int(total_written),
    }
    return store
