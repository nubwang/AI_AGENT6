"""政策知识库 RAG（policy_kb）

对齐 plans/03-Agent系统.md §18（P0 基础层）+ §16.2 权威信源：
  - 结构分块：按章/节/条/段落边界切块（500-800 字，重叠 100 字），每块挂元数据（doc/章节/时间/级别/行业）
  - 本地 embedding：BAAI/bge-large-zh-v1.5（sentence_transformers）；模型不可用 → 降级仅 BM25
  - Chroma 文本集合 policy_kb（复用 backtest/vector_store 的 PersistentClient 模式）
  - 混合召回：BM25（rank_bm25，精确）+ 稠密向量（语义）+ RRF 融合
  - 重排序：BAAI/bge-reranker-v2-m3（可选，模型不可用跳过）
  - 强制原文溯源：检索结果带 section 定位与原文片段

数据源：news_collector 入库的 policy_kb.sqlite（policy_docs 表），亦支持 ingest_texts 直接喂文本。
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import sqlite3
import threading

import numpy as np

from app.core.logger import logger
from app.agents import evolution_config  # F2：可进化参数热生效（CHUNK_SIZE/时间衰减）
from app.data.news_collector import POLICY_DB

CHUNK_SIZE = 400        # 单块字符数（匹配 bge-large-zh 512 token 上限；中文≈1 token/字）；可进化 F2
CHUNK_OVERLAP = 100     # 块重叠
_DECAY_HALFLIFE = 180.0  # 时效衰减半衰期（天）；可进化 F2（evolution_config: policy_kb_decay_halflife）
COLLECTION_NAME = "policy_kb"
EMBED_MODEL = "BAAI/bge-large-zh-v1.5"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

# Chroma 持久化路径（与形态库同目录）
CHROMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "chroma_db",
)

_embedder = None
_reranker = None
_bm25_index = None
_bm25_docs: list[dict] = []
_client = None            # Chroma client 缓存（避免每次新建）
_collection = None        # Chroma 集合缓存
_indexed_ids: set = set()  # 已入 Chroma 的 chunk id（增量建库跳过）
_BUILD_LOCK = threading.Lock()  # G：建库/检索并发保护


# ── 模型懒加载（不可用降级）────────────────────────────────────
def _load_embedder():
    """加载 bge embedding 模型。失败返回 None（稠密检索降级为仅 BM25）。"""
    global _embedder
    if _embedder is not None:
        return _embedder
    try:
        from sentence_transformers import SentenceTransformer
        # 说明：macOS 无 torch>=2.6 wheel；已降级 transformers<4.45 绕过 torch.load 版本检查
        _embedder = SentenceTransformer(EMBED_MODEL)
        logger.info(f"[policy_kb] embedding 模型加载成功: {EMBED_MODEL}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[policy_kb] embedding 模型加载失败（降级仅 BM25）: {exc}")
        _embedder = False
    return _embedder or None


def _load_reranker():
    """加载重排序模型。失败返回 None（跳过重排）。"""
    global _reranker
    if _reranker is not None:
        return _reranker
    try:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder(RERANK_MODEL, max_length=512)
        logger.info(f"[policy_kb] 重排序模型加载成功: {RERANK_MODEL}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[policy_kb] 重排序模型加载失败（跳过）: {exc}")
        _reranker = False
    return _reranker or None


_embed_warned = False


def embed(texts: list[str]) -> np.ndarray | None:
    """批量 embedding（分批控制内存）。模型不可用/失败返回 None（一次性告警，避免刷日志）。"""
    global _embed_warned
    enc = _load_embedder()
    if not enc:   # None 或 False 均视为不可用
        return None
    try:
        batch = 64
        outs = []
        for i in range(0, len(texts), batch):
            outs.append(enc.encode(texts[i:i + batch], normalize_embeddings=True))
        return np.concatenate(outs, axis=0) if outs else None
    except Exception as exc:  # noqa: BLE001
        if not _embed_warned:
            logger.warning(f"[policy_kb] embedding 失败（后续静默降级）: {exc}")
            _embed_warned = True
        return None


# ── 结构分块 ───────────────────────────────────────────────────
def _section_split(text: str) -> list[str]:
    """结构优先切分：按章/节/条/序号标题边界切块。"""
    # 匹配 "第X章/第X节/第X条/一、二、/1. 2." 等结构边界
    pattern = re.compile(
        r"(第[一二三四五六七八九十百千\d]+[章节条][^\n]{0,30}[\n：:]|"
        r"[一二三四五六七八九十]+[、.][^\n]{0,30}[\n：:]|"
        r"\d+[、.][^\n]{0,30}[\n：:])"
    )
    parts = []
    last = 0
    for m in pattern.finditer(text):
        if m.start() > last:
            parts.append(text[last:m.start()].strip())
        last = m.start()
    parts.append(text[last:].strip())
    return [p for p in parts if p]


def _merge_chunks(sections: list[str], size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """把结构段合并/拆分到目标长度（重叠衔接，避免切断逻辑）。"""
    chunks: list[str] = []
    buf = ""
    for sec in sections:
        if len(buf) + len(sec) + 1 <= size:
            buf = (buf + "\n" + sec).strip()
            continue
        # 段落超长：按句子切
        if len(sec) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            sentences = re.split(r"(?<=[。！？；])", sec)
            cur = ""
            for s in sentences:
                if len(cur) + len(s) <= size:
                    cur += s
                else:
                    if cur:
                        chunks.append(cur)
                        # 重叠衔接：取上一块末尾 overlap 字
                        cur = cur[-overlap:] + s if overlap else s
            if cur:
                chunks.append(cur)
        else:
            if buf:
                chunks.append(buf)
            buf = sec
    if buf:
        chunks.append(buf)
    return [c for c in chunks if len(c) >= 10]


def _parse_dt(s) -> int:
    """publish_time → YYYYMMDD 数字（供检索时效降权）。解析失败返回 0。"""
    if not s:
        return 0
    m = re.search(r"(20\d{2})[-/年.]?(\d{1,2})[-/月.]?(\d{1,2})", str(s))
    if m:
        return int(f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}")
    return 0


def _days_since(dt: int) -> int:
    """YYYYMMDD → 距今天数（非法返回 0）。"""
    if not dt:
        return 0
    try:
        d = datetime.date(dt // 10000, (dt // 100) % 100, dt % 100)
        return (datetime.date.today() - d).days
    except ValueError:
        return 0


# 行业板块关键词词典（O3：分块时给文本打板块标签，供检索 meta_filter 精确过滤）
_SECTOR_KEYWORDS: dict[str, list[str]] = {
    "储能": ["储能", "电化学", "抽水蓄能", "新型电力系统", "动力电池", "钠离子", "液流电池"],
    "集成电路": ["集成电路", "芯片", "半导体", "晶圆", "光刻", "国产替代", "先进制程"],
    "人工智能": ["人工智能", "大模型", "算力", "智算", "数据要素", "AI"],
    "设备更新": ["设备更新", "以旧换新", "大规模设备", "技术改造"],
    "新能源车": ["新能源汽车", "充电桩", "锂电", "动力电池", "智能网联"],
    "低空经济": ["低空经济", "无人机", "eVTOL", "通用航空"],
    "机器人": ["机器人", "人形机器人", "具身智能", "减速器"],
    "医药": ["医药", "创新药", "医疗器械", "集采", "生物医药"],
    "军工": ["军工", "国防", "航空发动机", "军工电子"],
    "消费": ["消费", "内需", "扩内需", "促消费", "提振消费"],
    "基建": ["基建", "新型基础设施", "交通", "水利", "城市更新"],
    "数字经济": ["数字经济", "数据要素", "数据资产", "算力"],
}


def _tag_industries(text: str) -> list[str]:
    """按行业关键词词典给文本打板块标签（去重，最多 4 个）。"""
    hits = [name for name, kws in _SECTOR_KEYWORDS.items() if any(k in text for k in kws)]
    return hits[:4]


def chunk_document(title: str, content: str, meta: dict | None = None) -> list[dict]:
    """对单篇政策文档做结构分块，每块带元数据与章节定位（含行业标签 + 发布时间戳）。"""
    if not content:
        return []
    sections = _section_split(content) or [content]
    size = int(evolution_config.get_param("policy_kb_chunk_size", CHUNK_SIZE) or CHUNK_SIZE)  # F2 热生效
    chunks = _merge_chunks(sections, size=size)
    meta = meta or {}
    publish_dt = _parse_dt(meta.get("publish_time", ""))
    out = []
    for i, text in enumerate(chunks):
        industries = _tag_industries(text)
        out.append({
            "text": text[:500],
            "metadata": {
                "doc": (title or "")[:100],
                "section": f"chunk{i + 1}",
                "source": meta.get("source", ""),
                "category": meta.get("category", ""),
                "publish_time": meta.get("publish_time", ""),
                "publish_dt": publish_dt,
                "industries": ",".join(industries),
                "url": meta.get("url", ""),
            },
        })
    return out


# ── Chroma 文本集合 ────────────────────────────────────────────
def _chunk_id(c: dict) -> str:
    """生成稳定唯一 chunk id（doc + section + 文本前缀 hash），避免 title 前 20 字相同导致 id 冲突。"""
    m = c.get("metadata", {})
    return hashlib.md5(
        f"{m.get('doc', '')}|{m.get('section', '')}|{c.get('text', '')[:50]}".encode()
    ).hexdigest()[:24]


def _get_collection():
    global _client, _collection
    if _collection is not None:
        return _collection
    import chromadb
    if _client is None:
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    _collection = _client.get_or_create_collection(
        COLLECTION_NAME, metadata={"hnsw:space": "cosine"},
    )
    return _collection


# ── BM25 索引（内存，建库时构建）───────────────────────────────
_jieba_ok = None   # None=未检测 / True=可用 / False=降级 bigram


def _tokenize(text: str) -> list[str]:
    """BM25 分词：优先 jieba（精确中文），不可用降级字符 bigram + 英文/数字词。"""
    global _jieba_ok
    if _jieba_ok is None:
        try:
            import jieba
            _jieba_ok = True
        except Exception:  # noqa: BLE001
            _jieba_ok = False
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    if _jieba_ok:
        try:
            import jieba
            tokens += [t for t in jieba.cut(text) if len(t) >= 2]
        except Exception:  # noqa: BLE001
            _jieba_ok = False
            tokens += _bigram(text)
    else:
        tokens += _bigram(text)
    return [t for t in dict.fromkeys(tokens) if t]   # H：去重保序（re+jieba 可能重复提取英文/数字）


def _bigram(text: str) -> list[str]:
    chinese = "".join(re.findall(r"[\u4e00-\u9fa5]", text))
    return [chinese[i:i + 2] for i in range(max(0, len(chinese) - 1))]


def _rebuild_bm25(chunks: list[dict]) -> None:
    global _bm25_index, _bm25_docs
    from rank_bm25 import BM25Okapi
    _bm25_docs = list(chunks)
    _bm25_index = BM25Okapi([_tokenize(c["text"]) for c in _bm25_docs])


def _bm25_search(query: str, top_k: int = 20) -> list[tuple[int, float]]:
    if _bm25_index is None or not _bm25_docs:
        return []
    scores = _bm25_index.get_scores(_tokenize(query))
    order = np.argsort(-scores)[:top_k]
    return [(int(i), float(scores[i])) for i in order if scores[i] > 0]


# ── 建库 ───────────────────────────────────────────────────────
def build_index(docs: list[dict] | None = None, clear: bool = False) -> dict:
    """从 policy_kb.sqlite（或传入 docs）读取政策文档 → 分块 → 建 BM25 + Chroma 向量索引。

    Args:
        docs: [{"title","content","source","category","publish_time","url"}]；None → 从 SQLite 读
        clear: 是否重建集合
    """
    with _BUILD_LOCK:   # G：建库加锁，避免与并发检索/定时任务竞争
        if docs is None:
            db = sqlite3.connect(POLICY_DB)
            try:
                rows = db.execute(
                    "SELECT title, content, source, category, publish_time, url FROM policy_docs"
                ).fetchall()
            finally:
                db.close()
            docs = [{"title": r[0], "content": r[1], "source": r[2], "category": r[3],
                     "publish_time": r[4], "url": r[5]} for r in rows]
        chunks: list[dict] = []
        for d in docs:
            chunks.extend(chunk_document(d.get("title", ""), d.get("content", ""), d))
        if not chunks:
            logger.warning("[policy_kb] 无可入库政策文档（先运行 news_collector 采集）")
            return {"chunks": 0, "docs": len(docs), "note": "语料为空"}

        # BM25（全量重建，内存索引便宜）
        _rebuild_bm25(chunks)
        # 稠密向量（增量：只 embedding 新 chunk；clear 时先清空旧分块再 embedding）
        dense_ok = False
        col = None
        try:
            new_chunks = chunks if clear else [c for c in chunks if _chunk_id(c) not in _indexed_ids]
            if clear:
                # F：先删旧分块再 embedding（模型不可用时也保证无残留）
                col = _get_collection()
                try:
                    col.delete(where={"section": {"$ne": ""}})
                except Exception:  # noqa: BLE001
                    pass
                _indexed_ids.clear()
            if new_chunks:
                emb = embed([c["text"] for c in new_chunks])
                if emb is not None:
                    if col is None:
                        col = _get_collection()
                    ids = [_chunk_id(c) for c in new_chunks]
                    col.upsert(ids=ids, embeddings=emb.tolist(),
                               documents=[c["text"] for c in new_chunks],
                               metadatas=[c["metadata"] for c in new_chunks])
                    _indexed_ids.update(ids)
                    dense_ok = True
                else:
                    dense_ok = False
            else:
                dense_ok = True   # 无新 chunk（已全部入库）
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[policy_kb] 向量入库失败（仅 BM25）: {exc}")
        logger.info(
            f"[policy_kb] 建库完成: 文档 {len(docs)}，分块 {len(chunks)}，"
            f"新embedding {len(new_chunks) if 'new_chunks' in dir() else '?'}，稠密={'✓' if dense_ok else '✗'}"
        )
        return {"chunks": len(chunks), "docs": len(docs), "dense": dense_ok}


# ── 混合召回 + 重排 + 溯源 ─────────────────────────────────────
def _rrf(*rankings: list[tuple[int, float]], k: int = 60) -> list[tuple[int, float]]:
    """RRF 倒数排名融合。rankings 元素: [(idx, score)]。"""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, (idx, _s) in enumerate(ranking):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return ranked


def ensure_index() -> dict:
    """幂等建库：BM25 已建则跳过；否则从 SQLite 建库（供启动预热/定时任务）。"""
    if _bm25_index is not None and _bm25_docs:
        return {"chunks": len(_bm25_docs), "cached": True}
    return build_index()


def retrieve(query: str, top_k: int = 5, meta_filter: str | None = None,
             max_age_days: int | None = None) -> list[dict]:
    """混合召回：BM25 + 稠密向量（Chroma）→ RRF 融合 → 时效降权 → 可选重排 → doc 去重 → 带溯源返回。

    Args:
        query: 检索词/板块名
        top_k: 返回条数
        meta_filter: 按 metadata 全字段（doc/category/source/industries 等）子串过滤
        max_age_days: 可选，排除发布时间超过 N 天的政策（未知时间不排除）
    """
    if _bm25_index is None:
        ensure_index()
    if not _bm25_docs:
        return []
    rankings: list[list[tuple[int, float]]] = []

    # 稀疏 BM25
    bm = _bm25_search(query, top_k=20)
    if bm:
        rankings.append(bm)

    # 稠密向量（Chroma）
    dense_rank: list[tuple[int, float]] = []
    try:
        emb = embed([query])
        if emb is not None:
            col = _get_collection()
            n_retrieve = max(1, min(20, len(_bm25_docs)))
            res = col.query(query_embeddings=emb.tolist(), n_results=n_retrieve)
            ids = res.get("ids", [[]])[0] or []
            dists = res.get("distances", [[]])[0] or []
            id2idx = {_chunk_id(c): i for i, c in enumerate(_bm25_docs)}
            for did, dist in zip(ids, dists):
                if did in id2idx:
                    dense_rank.append((id2idx[did], 1.0 - float(dist)))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[policy_kb] 稠密检索失败: {exc}")
    if dense_rank:
        rankings.append(sorted(dense_rank, key=lambda x: -x[1]))

    if not rankings:
        return []
    fused = _rrf(*rankings)[: top_k * 3]

    # O1 时效降权：越新的政策分越高（publish_dt 未知不惩罚，半衰期 180 天）
    weighted: list[tuple[int, float]] = []
    for i, s in fused:
        dt = int(_bm25_docs[i]["metadata"].get("publish_dt", 0) or 0)
        decay = 1.0
        if dt:
            d = _days_since(dt)
            half = float(evolution_config.get_param("policy_kb_decay_halflife", _DECAY_HALFLIFE)
                         or _DECAY_HALFLIFE)  # F2 热生效
            decay = 0.5 ** (max(0, d) / half)
        weighted.append((i, s * decay))
    cands = [(_bm25_docs[i], s) for i, s in weighted if 0 <= i < len(_bm25_docs)]

    # meta_filter 全字段匹配（doc/category/source/industries/publish_time 等）
    if meta_filter:
        cands = [(c, s) for c, s in cands
                 if meta_filter in " | ".join(str(v) for v in c.get("metadata", {}).values())]

    # max_age_days 硬过滤（可选：排除超过 N 天的旧政策）
    if max_age_days is not None:
        cands = [(c, s) for c, s in cands
                 if not int(c.get("metadata", {}).get("publish_dt", 0) or 0)
                 or _days_since(int(c.get("metadata", {}).get("publish_dt", 0))) <= max_age_days]

    # 可选重排（bge-reranker）
    rr = _load_reranker()
    if rr is not None and len(cands) > 1:
        try:
            pairs = [[query, c["text"][:500]] for c, _ in cands]
            scores = rr.predict(pairs)
            cands = [c for c, _ in sorted(zip(cands, scores), key=lambda x: -float(x[1]))]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[policy_kb] 重排失败: {exc}")

    # O2 doc 去重：每篇政策最多保留 1 块（保证 top_k 覆盖不同政策，提升多样性）
    seen_docs: set[str] = set()
    dedup: list[tuple[dict, float]] = []
    for c, s in cands:
        d = c.get("metadata", {}).get("doc", "")
        if d in seen_docs:
            continue
        seen_docs.add(d)
        dedup.append((c, s))
    cands = dedup[:top_k]

    results = []
    for c, score in cands:
        meta = c.get("metadata", {})
        results.append({
            "text": c["text"],
            "score": round(float(score), 4),
            "source": meta.get("source", ""),
            "category": meta.get("category", ""),
            "publish_time": meta.get("publish_time", ""),
            "doc": meta.get("doc", ""),
            "section": meta.get("section", ""),      # 强制溯源：章节定位
            "industries": meta.get("industries", ""),
            "url": meta.get("url", ""),
        })
    return results


def ingest_texts(texts: list[dict]) -> dict:
    """直接喂入政策文本建库（Tavily 抓取/手动语料场景）。texts: [{"title","content",...}]。"""
    return build_index(texts, clear=False)


def stats() -> dict:
    """知识库统计。"""
    try:
        db = sqlite3.connect(POLICY_DB)
        n = db.execute("SELECT COUNT(*) FROM policy_docs").fetchone()[0]
        db.close()
    except Exception:  # noqa: BLE001
        n = 0
    try:
        col = _get_collection()
        vec = col.count()
    except Exception:  # noqa: BLE001
        vec = 0
    return {"docs": n, "chunks_in_memory": len(_bm25_docs), "chunks_in_chroma": vec,
            "dense_available": _embedder is not None and _embedder is not False}


__all__ = ["build_index", "ensure_index", "retrieve", "ingest_texts", "chunk_document", "stats", "embed"]
