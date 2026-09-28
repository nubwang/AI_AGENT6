"""知识库检索层（kb_index）— 对齐 plans/21 §6.2 / §8.1

三层检索（可用性优先、成本递增、自动降级）：
  1. **结构化精确检索**（SQLite，主力）：案例按 环节/形态/环境/时间 过滤 —— 零幻觉、零 token、零成本
  2. **稀疏检索**（BM25 / FTS5 + jieba）：教训文本召回 —— 模型不可用时的兜底
  3. **稠密检索**（bge-large-zh → ChromaDB 集合 evolution_kb_lesson）：语义近邻 —— "今天像历史上哪一天"

融合：RRF；时间衰减：exp(-Δdays / half_life) 让近期规律优先。
对外还提供"否证清单"（历史已试且变差的改动），供提案硬约束（plans/21 §九.4）。
"""
from __future__ import annotations

import math
import os
import time

from app.core.logger import logger
from app.kb import kb_store, kb_writer

CHROMA_PATH = os.path.join(kb_store.DATA_DIR, "chroma_db")
_COLLECTION = "evolution_kb_lesson"

_client = None
_collection = None
_embedder_warned = False
_EMBED_BATCH = 16   # 单批 embedding 条数（控内存/显存峰值）


# ── 配置 ─────────────────────────────────────────────────────
def _cfg() -> dict:
    try:
        from app.agents import evolution_config
        return evolution_config.get("knowledge_base", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _half_life() -> float:
    try:
        return float((_cfg().get("lesson", {}) or {}).get("half_life_days", 120))
    except Exception:  # noqa: BLE001
        return 120.0


def _days_ago(date_str: str) -> float:
    s = str(date_str or "")[:10].replace("-", "")
    try:
        t = time.strptime(s, "%Y%m%d")
        return max(0.0, (time.time() - time.mktime(t)) / 86400.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _decay(date_str: str) -> float:
    hl = max(1.0, _half_life())
    return max(0.3, math.exp(-_days_ago(date_str) / hl))


# ── ③ 向量层（bge + ChromaDB；不可用自动跳过）────────────────
def _embed(texts: list[str]):
    """复用 policy_kb 的 bge embedding（失败返回 None → 降级）。"""
    global _embedder_warned
    try:
        from app.data.policy_kb import embed
        return embed(texts)
    except Exception as exc:  # noqa: BLE001
        if not _embedder_warned:
            logger.warning(f"[kb_index] embedding 不可用（降级为 BM25/结构化检索）: {exc}")
            _embedder_warned = True
        return None


def _get_collection():
    global _client, _collection
    if _collection is not None:
        return _collection
    if not (_cfg().get("vector", {}) or {}).get("enabled", True):
        return None
    try:
        import chromadb
        _client = _client or chromadb.PersistentClient(path=CHROMA_PATH)
        _collection = _client.get_or_create_collection(
            name=_COLLECTION, metadata={"hnsw:space": "cosine"})
        return _collection
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[kb_index] 向量库不可用: {exc}")
        return None


def index_lessons(limit: int = 9000, max_new: int = 800) -> int:
    """把教训**轮转增量**写入向量库（幂等 upsert）。返回本轮入向量库条数。

    - 全库 6k+ 条一次 embedding 会撑爆显存（Mac MPS），故每轮只处理 max_new 条，
      用 kb_meta 游标轮转，多轮维护即可覆盖全库（单轮峰值可控）。
    - 排序优先级：active > candidate > reference，其次置信度、样本量
      （保证"高价值知识"最先具备语义检索能力）。
    """
    col = _get_collection()
    if col is None:
        return 0
    lessons = kb_store.query("lesson", limit=limit)
    if not lessons:
        return 0
    _rank = {"active": 0, "weakened": 1, "candidate": 2, "reference": 3, "retired": 4}
    lessons.sort(key=lambda l: (
        _rank.get(str(l.get("status") or ""), 2),
        -float(l.get("confidence") or 0.0),
        -int((l.get("support") or {}).get("n") or 0),
    ))
    cursor = 0
    try:
        cursor = int(kb_store.meta_get("lesson_index_cursor", "0") or 0)
    except Exception:  # noqa: BLE001
        cursor = 0
    cursor = cursor % max(1, len(lessons))
    window = lessons[cursor:cursor + max(1, int(max_new))]
    if not window:                       # 到尾部则回绕
        window = lessons[:max(1, int(max_new))]
        cursor = 0
    try:
        kb_store.meta_set("lesson_index_cursor",
                          str((cursor + len(window)) % max(1, len(lessons))))
    except Exception:  # noqa: BLE001
        pass
    lessons = window
    docs: list[str] = []
    metas: list[dict] = []
    ids: list[str] = []
    for l in lessons:
        text = " ".join(str(x) for x in (
            l.get("text"), (l.get("actionable") or {}).get("hint") if isinstance(l.get("actionable"), dict) else l.get("actionable"),
            str((l.get("scope") or {}).get("stage") or ""),
            str((l.get("scope") or {}).get("error_type") or ""),
            str((l.get("scope") or {}).get("success_type") or ""),
        ) if x)
        if not text.strip():
            continue
        docs.append(text)
        metas.append({"status": str(l.get("status") or ""), "type": str(l.get("type") or ""),
                      "confidence": float(l.get("confidence") or 0.0),
                      "n": int((l.get("support") or {}).get("n") or 0)})
        ids.append(str(l.get("id")))
    # 分批 embedding + upsert（控显存峰值：Mac MPS 上大 batch 易 OOM；失败即停不阻塞）
    total = 0
    for i in range(0, len(docs), _EMBED_BATCH):
        chunk_docs = docs[i:i + _EMBED_BATCH]
        chunk_ids = ids[i:i + _EMBED_BATCH]
        chunk_metas = metas[i:i + _EMBED_BATCH]
        vectors = _embed(chunk_docs)
        if vectors is None:
            # 先释放重排模型占用再重试一次（MPS 显存受限时的实用缓解）
            _release_reranker()
            vectors = _embed(chunk_docs)
            if vectors is None:
                logger.warning("[kb_index] 向量索引中断（embedding 不可用/显存不足），"
                               "已索引部分保留，检索自动降级为 BM25/结构化")
                break
        try:
            col.upsert(ids=chunk_ids,
                       embeddings=[v.tolist() if hasattr(v, "tolist") else list(v) for v in vectors],
                       metadatas=chunk_metas,   # type: ignore[arg-type]
                       documents=chunk_docs)
            total += len(chunk_ids)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[kb_index] 向量 upsert 失败: {exc}")
            break
    return total


def _release_reranker() -> None:
    """释放 policy_kb 缓存的重排模型，给 embedding 腾显存（下次检索时自动重载）。"""
    try:
        from app.data import policy_kb
        if getattr(policy_kb, "_reranker", None) not in (None, False):
            policy_kb._reranker = None
            logger.info("[kb_index] 已释放重排模型缓存以腾出显存")
    except Exception:  # noqa: BLE001
        pass


def _vector_search(query: str, k: int = 20) -> list[tuple[str, float]]:
    """向量检索 → [(lesson_id, score)]；不可用返回 []。"""
    col = _get_collection()
    if col is None or not query:
        return []
    vec = _embed([query])
    if vec is None:
        return []
    try:
        v = vec[0]
        res = col.query(query_embeddings=[v.tolist() if hasattr(v, "tolist") else list(v)],
                        n_results=max(1, k))
        ids = res.get("ids") or []
        dists = res.get("distances") or []
        docs = res.get("documents") or []
        if ids and isinstance(ids[0], list):
            ids = ids[0]
        if dists and isinstance(dists[0], list):
            dists = dists[0]
        if docs and isinstance(docs[0], list):
            docs = docs[0]
        # plans/21 §6.2：可选重排（配置 knowledge_base.vector.rerank，模型不可用自动跳过）
        if (_cfg().get("vector", {}) or {}).get("rerank", True) and docs:
            rr = _rerank(query, ids, docs, k)
            if rr:
                return rr
        out: list[tuple[str, float]] = []
        for i, d in zip(ids, dists):
            dv = float(d[0]) if isinstance(d, list) and d else (0.0 if isinstance(d, list) else float(d))
            out.append((str(i), 1.0 - dv))
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[kb_index] 向量检索失败: {exc}")
        return []


def _rerank(query: str, ids: list, docs: list, k: int) -> list[tuple[str, float]]:
    """bge-reranker 重排（复用 policy_kb 的模型加载；不可用/失败返回空 → 调用方走原排序）。"""
    try:
        from app.data.policy_kb import _load_reranker   # type: ignore[attr-defined]
        rr = _load_reranker()
        if not rr:
            return []
        scores = rr.predict([(query, str(d or "")) for d in docs])
        order = sorted(range(min(len(ids), len(docs))), key=lambda i: -float(scores[i]))
        return [(str(ids[i]), 1.0 - i / max(1, len(order) + 1)) for i in order[:k]]
    except Exception:  # noqa: BLE001
        return []


def _bm25_search(query: str, k: int = 20) -> list[tuple[str, float]]:
    """稀疏检索：优先 FTS5（jieba 分词），失败回退 LIKE。返回 [(lesson_id, score)]。"""
    if not query:
        return []
    rows = kb_store.search_lessons_fts(query, limit=k)
    if not rows:
        rows = kb_store.search_lessons_like(query[:20], limit=k)
    out = []
    for i, r in enumerate(rows):
        out.append((str(r.get("id")), 1.0 - i / max(1, len(rows))))  # 名次分（无 BM25 分数时用排名）
    return out


def _rrf(rank_lists: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion。"""
    score: dict[str, float] = {}
    for lst in rank_lists:
        for rank, doc in enumerate(lst, start=1):
            score[doc] = score.get(doc, 0.0) + 1.0 / (k + rank)
    return score


# ── 教训检索（混合）─────────────────────────────────────────
def search_lessons(query: str = "", stage: str | None = None, type_: str | None = None,
                   include_retired: bool = False, limit: int = 5,
                   min_n: int = 1, use_vector: bool = True) -> list[dict]:
    """教训检索：结构化过滤 + 稀疏/稠密混合排序 + 时效衰减。

    use_vector=False 时**只用结构化 + FTS/BM25**（不加载 embedding/重排模型）——
    热路径（L0 摘要、守护巡检、前端刷新）必须走这条路，避免秒级模型加载与显存争抢。
    """
    kb_writer.flush_now()
    filters = {}
    if type_:
        filters["type"] = type_
    # 性能：按置信度排序**只取少量**（原 limit=500 会解析 500 条 JSON，实测 1.9s/次，热路径不可接受）
    fast_n = max(50, int(limit) * 10)
    if not query:
        pool = kb_store.query("lesson", filters, order="confidence DESC", limit=fast_n)
        if not include_retired:
            pool = [l for l in pool if l.get("status") not in ("retired", "archived")]
        if stage:
            pool = [l for l in pool
                    if str((l.get("scope") or {}).get("stage") or "") == stage] or pool
        pool = [l for l in pool if int((l.get("support") or {}).get("n") or 0) >= int(min_n or 1)]
        return pool[:limit]
    pool = kb_store.query("lesson", filters, order="confidence DESC", limit=200)
    if not pool:
        return []
    if not include_retired:
        # archived 与 retired 默认都不进检索（记录仍可查、可自动复活）
        pool = [l for l in pool if l.get("status") not in ("retired", "archived")]
    if stage:
        pool = [l for l in pool
                if str((l.get("scope") or {}).get("stage") or "") == stage] or pool
    pool = [l for l in pool if int((l.get("support") or {}).get("n") or 0) >= int(min_n or 1)]
    if not pool:
        return []
    by_id = {str(l.get("id")): l for l in pool}

    final: dict[str, float] = {}
    if query:
        vec = [i for i, _ in _vector_search(query, k=30) if i in by_id] if use_vector else []
        bm = [i for i, _ in _bm25_search(query, k=30) if i in by_id]
        final = _rrf([vec, bm])
        if not final:   # 检索无结果 → 全部按置信度排序（保证总有可用知识）
            final = {i: 0.0 for i in by_id}
    else:
        final = {i: 0.0 for i in by_id}

    count_only = filters.get("type")   # 指定类型检索时不降权（供前端"外部知识"筛选）
    ranked = []
    for lid, s in final.items():
        l = by_id[lid]
        support = l.get("support") or {}
        conf = float(l.get("confidence") or 0.0)
        last = str(support.get("last_date") or l.get("ts") or "")
        score = s * 10.0 + conf + 0.001 * int(support.get("n") or 0)
        # 外部引用型知识（external_*）不抢一线自产规律的位置；未指定类型查询时轻微降权
        if not count_only and str(l.get("type") or "").startswith("external_"):
            score *= 0.75
        score *= _decay(last)
        ranked.append((score, l))
    ranked.sort(key=lambda x: -x[0])
    return [l for _, l in ranked[:limit]]


# ── 案例检索 ─────────────────────────────────────────────────
def search_cases(side: str | None = None, stage: str | None = None,
                 form_type: str | None = None, days: int = 90,
                 ts_code: str | None = None, limit: int = 20) -> list[dict]:
    """结构化案例检索（主力：精确、零成本）。"""
    kb_writer.flush_now()
    filters: dict = {}
    if side:
        filters["side"] = side
    if form_type:
        filters["form_type"] = form_type
    if ts_code:
        filters["ts_code"] = ts_code
    rows = kb_store.query("case", filters, limit=max(limit * 4, 100))
    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - max(0, days) * 86400))
    out = [c for c in rows if str(c.get("date") or "")[:8] >= cutoff]
    if stage:
        out = [c for c in out if str((c.get("cause") or {}).get("stage") or c.get("stage") or "") == stage] or out
    out.sort(key=lambda c: str(c.get("date") or ""), reverse=True)
    return out[:limit]


def similar_cases(form_type: str | None = None, stage: str | None = None,
                  regime: str | None = None, k: int = 5,
                  need_control: bool = True) -> dict:
    """相似情境案例（成功/失败并排，含对照），供分析与归因复用。

    need_control=True 时，成功案例会带上"同形态同日未上涨"的对照案例（防幸存者偏差）。
    """
    succ = search_cases(side="success", form_type=form_type, stage=stage, days=180, limit=k)
    fail = search_cases(side="failure", form_type=form_type, stage=stage, days=180, limit=k)
    if regime:
        succ = [c for c in succ if str((c.get("env") or {}).get("regime")) == regime] or succ
        fail = [c for c in fail if str((c.get("env") or {}).get("regime")) == regime] or fail
    pairs = []
    if need_control:
        for s in succ:
            ctrl = [f for f in fail if f.get("date") == s.get("date")
                    and f.get("form_type") == s.get("form_type")]
            pairs.append({"success": s, "controls": ctrl[:2] or fail[:2]})
    return {"success": succ, "failure": fail, "pairs": pairs}


# ── 否证清单（防重复无效改动，plans/21 §九.4）────────────────
_NEG_VERDICTS = ("worse", "no_change", "rolled_back", "abandoned", "invalid")


def tried_history(param: str | None = None, file: str | None = None,
                  limit: int = 20) -> list[dict]:
    """历史同类改动（含结论），用于"提案前先看试过什么"。"""
    kb_writer.flush_now()
    rows = kb_store.query("experiment", limit=500)
    out = []
    for e in rows:
        tgt = e.get("target") or {}
        if param and str(tgt.get("param") or "") != param:
            continue
        if file and str(tgt.get("file") or tgt.get("source") or "") != file:
            continue
        out.append(e)
    out.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
    return out[:limit]


def veto_list(param: str | None = None, file: str | None = None,
              min_count: int | None = None) -> dict:
    """否证清单：某参数/文件历史上被证明"更差/无效"的改动次数与证据。

    blocked=True 表示达到"硬拒"门槛（evolution_config.guard.block_repeat_failed，默认 2）。
    """
    if min_count is None:
        try:
            min_count = int((_cfg().get("guard", {}) or {}).get("block_repeat_failed", 2))
        except Exception:  # noqa: BLE001
            min_count = 2
    hist = [e for e in tried_history(param=param, file=file, limit=200)
            if str(e.get("verdict") or "") in _NEG_VERDICTS]
    return {
        "blocked": len(hist) >= max(1, int(min_count)),
        "count": len(hist),
        "threshold": max(1, int(min_count)),
        "evidence": [{"ts": e.get("ts"), "verdict": e.get("verdict"),
                      "reason": (e.get("verdict_reason") or "")[:160],
                      "change": e.get("change")} for e in hist[:5]],
    }


def best_practice(limit: int = 5) -> list[dict]:
    """历史被证明有效的改动（improved），供提案优先复用。"""
    kb_writer.flush_now()
    rows = [e for e in kb_store.query("experiment", limit=500)
            if str(e.get("verdict") or "") == "improved"]
    rows.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
    return rows[:limit]


def evidence_lessons(limit: int = 6) -> list[dict]:
    """证据型知识：plans/23·24·25 的验证结论 + 交易纪律教训（kb_evidence 接入）。

    这些是"被统计检验过的事实"（洗盘是否有效 / 哪个买点能赚 / 哪个博弈特征通过 FDR /
    自证样本外超额 / 纪律网格最优参数），必须能被 L0 与综合分析直接取用 —— 否则
    进化大脑只能凭个案教训猜，等于神经元没接上。
    """
    rows = kb_store.query_lessons_by_type(
        ["evidence", "discipline"], limit=limit,
        exclude_status=("archived", "retired"))
    # 样本量键归一化：纪律教训存的是 cases_n/total_n，不归一会让下游显示 n=None
    # （把"有 359 单样本的实证"误当成无样本观察）
    for l in rows:
        s = dict(l.get("support") or {})
        if not s.get("n"):
            s["n"] = s.get("cases_n") or s.get("total_n") or 1
            l["support"] = s
    return rows


def l0_block(query: str = "", stage: str | None = None, limit_lessons: int = 3) -> dict:
    """L0 摘要用的知识库块（plans/21 §8.1：L0 必读要带"最相关教训 + 活跃实验 + 否证目标"）。

    以 SQL 为主（仅在有 query 时走向量），成本低；供 evolution_summarizer 注入 L0。
    """
    kb_writer.flush_now()
    # 热路径：① 不走向量（避免加载 bge/reranker，实测走向量 3.8s）；② 只取 Top-3（SQL 直取，避免解析 500 条 JSON）
    lessons = kb_store.query("lesson", {"status": "active"}, order="confidence DESC",
                             limit=limit_lessons)
    if len(lessons) < limit_lessons:
        lessons += kb_store.query("lesson", {"status": "candidate"}, order="confidence DESC",
                                  limit=limit_lessons - len(lessons))
    # 成功侧规律（用户诉求"成功上涨的股票为何成功上涨"）必须进 L0，不能只留在库里
    succ = [l for l in kb_store.query("lesson", {"type": "success_pattern"},
                                      order="confidence DESC", limit=4)
            if l.get("status") in ("active", "candidate")][:2]
    # plans/23·24·25 的实证结论（L0 必读）：SQL 直取，热路径成本可控
    ev = evidence_lessons(limit=6)
    all_exp = kb_store.query("experiment", limit=300)
    running = [e for e in all_exp
               if str(e.get("status") or "") in ("proposed", "applied", "shadowing")]
    neg = [e for e in all_exp if str(e.get("verdict") or "") in _NEG_VERDICTS]
    blocked = sorted({str((e.get("target") or {}).get("param")
                          or (e.get("target") or {}).get("file") or "unknown") for e in neg})
    return {
        "cases": kb_store.count("case"),
        "lessons": kb_store.count("lesson"),
        "experiments": kb_store.count("experiment"),
        "changes": kb_store.count("change"),
        "active_lessons": [
            # n 回退 cases_n/total_n：纪律教训（plan_tracker/discipline_grid）用的是后者，
            # 否则 L0 会把"有 359 单样本的实证"误显示成 n=None
            {"text": (l.get("text") or "")[:120],
             "n": ((l.get("support") or {}).get("n")
                   or (l.get("support") or {}).get("cases_n")
                   or (l.get("support") or {}).get("total_n")),
             "confidence": l.get("confidence"), "status": l.get("status"),
             "stage": (l.get("scope") or {}).get("stage")}
            for l in lessons],
        "success_lessons": [
            {"text": (l.get("text") or "")[:120],
             "n": (l.get("support") or {}).get("n"),
             "confidence": l.get("confidence")}
            for l in succ],
        "evidence": [
            {"text": (l.get("text") or "")[:120],
             "n": (l.get("support") or {}).get("n"),
             "confidence": l.get("confidence"),
             "module": (l.get("scope") or {}).get("module") or l.get("type"),
             "status": l.get("status"),
             "hint": str((l.get("actionable") or {}).get("hint") or "")[:100]}
            for l in ev],
        "running_experiments": [
            {"ts": e.get("ts"), "target": e.get("target"),
             "status": e.get("status"), "verdict": e.get("verdict")} for e in running[:5]],
        "blocked_targets": blocked[:10],
    }


def l0_text(block: dict | None = None) -> str:
    """知识库块 → L0 文本行（供进化大脑 Prompt）。"""
    b = block or l0_block()
    lines = [
        f"私有域知识库: 案例 {b.get('cases', 0)}（成功+失败）/ 教训 {b.get('lessons', 0)}"
        f" / 实验 {b.get('experiments', 0)} / 改动 {b.get('changes', 0)}"
    ]
    ls = b.get("active_lessons") or []
    if ls:
        lines.append("历史高置信教训: " + " | ".join(
            f"{l.get('text')}（n={l.get('n')}，置信{float(l.get('confidence') or 0):.2f}）"
            for l in ls[:3]))
    sl = b.get("success_lessons") or []
    if sl:
        lines.append("成功规律（为什么涨，需配对对照验证）: " + " | ".join(
            f"{l.get('text')}（n={l.get('n')}，置信{float(l.get('confidence') or 0):.2f}）"
            for l in sl))
    ev = b.get("evidence") or []
    if ev:
        lines.append("实证结论（回测/检验过，可直接指导调参）: " + " | ".join(
            f"{l.get('text')}（n={l.get('n')}）" for l in ev[:3]))
    run = b.get("running_experiments") or []
    if run:
        lines.append("进行中实验: " + " | ".join(
            f"{(e.get('target') or {}).get('param') or (e.get('target') or {}).get('file')}"
            f"[{e.get('status')}]" for e in run[:3]))
    blk = b.get("blocked_targets") or []
    if blk:
        lines.append(f"否证清单（已有无效/更差改动记录，勿重复）: {'、'.join(blk[:6])}")
    return "\n".join(lines)


def warm_up(full: bool = False) -> dict:
    """预热：FTS/jieba 首调约 1s（一次性成本），可在维护任务里提前跑掉，
    避免用户首次查询/守护巡检时卡顿。

    full=True 时同时加载 embedding 模型（约 10s / 1.3GB，占显存；默认不做）。
    """
    kb_writer.flush_now()
    t0 = time.time()
    try:
        kb_store.search_lessons_fts("预热", limit=1)
    except Exception:  # noqa: BLE001
        pass
    fts_ms = int((time.time() - t0) * 1000)
    vec_ok = None
    vec_ms = 0
    if full:
        t1 = time.time()
        vec_ok = _embed(["预热"]) is not None
        vec_ms = int((time.time() - t1) * 1000)
    return {"fts_ms": fts_ms, "vector_loaded": vec_ok, "vector_ms": vec_ms}


def stats() -> dict:
    """检索层健康度（向量是否可用 / 案例与教训量）。"""
    col = _get_collection()
    out = {
        "vector_available": col is not None,
        "collection": _COLLECTION if col is not None else None,
        "cases": kb_store.count("case"),
        "lessons": kb_store.count("lesson"),
        "experiments": kb_store.count("experiment"),
        "changes": kb_store.count("change"),
        "plans": kb_store.count("plan"),
        "evidence": len(kb_store.query_lessons_by_type(["evidence", "discipline"],
                                                       limit=1000)),
    }
    try:
        out["vectors"] = col.count() if col is not None else 0
    except Exception:  # noqa: BLE001
        out["vectors"] = 0
    return out


__all__ = ["index_lessons", "search_lessons", "search_cases", "similar_cases",
           "tried_history", "veto_list", "best_practice", "evidence_lessons",
           "l0_block", "l0_text", "warm_up", "stats"]
