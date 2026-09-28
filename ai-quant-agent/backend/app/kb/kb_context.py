"""综合分析上下文构建（kb_context）— 对齐 plans/21 §八 读取路径

用户核心诉求："分析的时候，要结合总结出来的知识库以及经验都要利用上，综合分析，
要不然每次都是单独分析很片面"。

本模块是"综合分析"的唯一入口：把 EKB 里的历史经验压缩成**可直接注入 LLM 的上下文**，
并强制带上四条防片面硬规则（plans/21 §8.3）：
  1. 必须有对照（成功/失败配对）
  2. 必须分层（市场环境 regime）
  3. 必须看样本与置信度（n<10 标注证据不足）
  4. 必须看已试历史（试过什么、结果如何、哪些被否证）

对外：
  - build_knowledge_context(...)   结构化上下文（dict）
  - knowledge_txt(...)             注入用文本（token 受限）
  - veto_check(param/file)         否证硬约束（提案前必查）
  - reuse_attribution(pick, hit)   失效归因复用历史高置信结论（不调 LLM，省 token）
"""
from __future__ import annotations

import time

from app.core.logger import logger
from app.kb import kb_index, kb_store, kb_writer, kb_distiller

_TXT_LESSON_MAX = 140
_TXT_CASE_MAX = 120


def _cfg_ctx() -> dict:
    try:
        from app.agents import evolution_config
        return (evolution_config.get("knowledge_base", {}) or {}).get("context", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _top_k_cases() -> int:
    try:
        return int(_cfg_ctx().get("top_k_cases", 5))
    except Exception:  # noqa: BLE001
        return 5


def _top_k_lessons() -> int:
    try:
        return int(_cfg_ctx().get("top_k_lessons", 3))
    except Exception:  # noqa: BLE001
        return 3


def _case_brief(c: dict) -> dict:
    o = c.get("outcome") or {}
    cause = c.get("cause") or {}
    return {
        "id": c.get("id"), "date": c.get("date"), "ts_code": c.get("ts_code"),
        "form_type": c.get("form_type"), "side": c.get("side"),
        "t1_ret": o.get("t1_ret"), "excess_t1": o.get("excess_t1"),
        "regime": (c.get("env") or {}).get("regime"),
        "stage": cause.get("stage") or c.get("stage"),
        "error_type": cause.get("error_type"),
        "success_type": cause.get("success_type"),
        "lesson": (cause.get("lesson") or "")[:_TXT_CASE_MAX],
    }


def _lesson_brief(l: dict) -> dict:
    support = l.get("support") or {}
    return {
        "id": l.get("id"), "text": (l.get("text") or "")[:_TXT_LESSON_MAX],
        "type": l.get("type"), "stage": (l.get("scope") or {}).get("stage"),
        "n": support.get("n"), "confidence": l.get("confidence"),
        "status": l.get("status"),
        "hint": str((l.get("actionable") or {}).get("hint") or "")[:100],
        "effect_verdict": l.get("effect_verdict"),
    }


def build_knowledge_context(stage: str | None = None, form_type: str | None = None,
                            regime: str | None = None, query: str = "",
                            param: str | None = None, file: str | None = None,
                            days: int = 180) -> dict:
    """构建综合分析上下文（结构化）。

    Args:
        stage/form_type/regime: 当前关注的情境（用于检索同类经验）
        query: 语义检索关键词（如"为什么推荐没涨"）
        param/file: 关注的可进化参数/文件（用于查历史改动与否证清单）
    """
    kb_writer.flush_now()
    k_cases = _top_k_cases()
    k_lessons = _top_k_lessons()
    q = query or " ".join(x for x in (stage, form_type, regime) if x)
    similar = kb_index.similar_cases(form_type=form_type, stage=stage, regime=regime, k=k_cases)
    lessons = kb_index.search_lessons(query=q, stage=stage, limit=k_lessons)
    success_lessons = kb_index.search_lessons(query=q, stage=stage, type_="success_pattern", limit=2)
    tried = kb_index.tried_history(param=param, file=file, limit=5)
    best = kb_index.best_practice(limit=3)
    veto = kb_index.veto_list(param=param, file=file)
    # plans/23·24·25 的实证结论（洗盘/买点/博弈特征/情绪门控/纪律网格/自证 OOS）
    ev = kb_index.evidence_lessons(limit=6)
    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stats": {"cases": kb_store.count("case"), "lessons": kb_store.count("lesson"),
                  "experiments": kb_store.count("experiment")},
        "lessons": [_lesson_brief(l) for l in lessons],
        "success_lessons": [_lesson_brief(l) for l in success_lessons],
        "evidence": [{"id": l.get("id"), "text": (l.get("text") or "")[:_TXT_LESSON_MAX],
                      "module": (l.get("scope") or {}).get("module") or l.get("type"),
                      "n": (l.get("support") or {}).get("n"),
                      "confidence": l.get("confidence"), "status": l.get("status"),
                      "hint": str((l.get("actionable") or {}).get("hint") or "")[:100]}
                     for l in ev],
        "cases": {
            "success": [_case_brief(c) for c in similar.get("success", [])],
            "failure": [_case_brief(c) for c in similar.get("failure", [])],
        },
        "pairs": [{"success": _case_brief(p["success"]),
                   "controls": [_case_brief(c) for c in p.get("controls", [])]}
                  for p in similar.get("pairs", [])],
        "tried": [{"ts": e.get("ts"), "target": e.get("target"), "change": e.get("change"),
                   "verdict": e.get("verdict"), "reason": (e.get("verdict_reason") or "")[:120]}
                  for e in tried],
        "best_practice": [{"ts": e.get("ts"), "target": e.get("target"),
                           "change": e.get("change"), "reason": (e.get("verdict_reason") or "")[:120]}
                          for e in best],
        "veto": veto,
    }


def knowledge_txt(ctx: dict | None = None, **kwargs) -> str:
    """把上下文压成可注入 LLM 的文本块（带回溯依据与硬规则提示）。

    token 预算：一般 < 800 token（教训 3 条 + 案例 5 组 + 否证清单）。
    """
    ctx = ctx or build_knowledge_context(**kwargs)
    lines: list[str] = ["【私有域知识库（历史经验，必须结合使用）】"]

    lessons = ctx.get("lessons") or []
    if lessons:
        parts = []
        for l in lessons:
            n = int(l.get("n") or 0)
            warn = "（证据不足，仅作观察）" if n < 10 else ""
            parts.append(f"{l.get('text')}（n={n}，置信{float(l.get('confidence') or 0):.2f}"
                         f"，环节 {l.get('stage') or '-'}）{warn}")
        lines.append("· 失效规律: " + " | ".join(parts))

    sl = ctx.get("success_lessons") or []
    if sl:
        lines.append("· 成功规律: " + " | ".join(
            f"{l.get('text')}（n={l.get('n')}，置信{float(l.get('confidence') or 0):.2f}）" for l in sl))

    ev = ctx.get("evidence") or []
    if ev:
        lines.append("· 实证结论（回测/检验过，可直接指导调参）: " + " | ".join(
            f"{l.get('text')}（n={l.get('n')}，来源 {l.get('module')}）" for l in ev[:4]))

    pairs = ctx.get("pairs") or []
    if pairs:
        seg = []
        for p in pairs[:2]:
            s = p.get("success") or {}
            ctrls = p.get("controls") or []
            c = ctrls[0] if ctrls else {}
            seg.append(f"{s.get('date')} {s.get('ts_code')} 成功(T+1 {s.get('t1_ret')}，超额 {s.get('excess_t1')})"
                       f" vs 同日同形态未涨 {c.get('ts_code')}(T+1 {c.get('t1_ret')}，归因 {c.get('error_type') or '-'})")
        lines.append("· 成功/失败对照: " + " ; ".join(seg))

    tried = ctx.get("tried") or []
    if tried:
        lines.append("· 历史同类改动: " + " | ".join(
            f"{(t.get('target') or {}).get('param') or (t.get('target') or {}).get('file') or t.get('target')}"
            f"({t.get('change')}) → {t.get('verdict') or '未决'}"
            + (f"：{t.get('reason')}" if t.get("reason") else "")
            for t in tried[:3]))

    best = ctx.get("best_practice") or []
    if best:
        lines.append("· 历史有效改法(可优先复用): " + " | ".join(
            f"{(b.get('target') or {}).get('param') or (b.get('target') or {}).get('file')}{b.get('change')}"
            for b in best[:2]))

    veto = ctx.get("veto") or {}
    if veto.get("count"):
        lines.append(f"· 否证清单: 同类改动已有 {veto.get('count')} 次被证明无效/更差"
                     + ("（达到硬拒门槛，禁止重复同向改动）" if veto.get("blocked") else "（慎重复试）"))
        for e in (veto.get("evidence") or [])[:2]:
            lines.append(f"  - {e.get('ts')} {e.get('verdict')}: {e.get('reason')}")

    lines.append("硬规则：①结论必须结合上述历史（禁止单点分析）；②成功结论必须有失败对照；"
                 "③必须标注市场环境与样本量；④证据不足(n<10)只能作观察，不得据此下确定性结论；"
                 "⑤已有实证结论（洗盘/买点/博弈特征/纪律/自证）时**必须引用或解释为何不采纳**，"
                 "不得重复验证已被否证的方向。")
    return "\n".join(lines)


def veto_check(param: str | None = None, file: str | None = None) -> dict:
    """提案前置硬校验（plans/21 §九.4）：命中否证清单 → 拒绝同向改动。

    Returns: {"blocked": bool, "reason": str, "evidence": [...]}
    """
    v = kb_index.veto_list(param=param, file=file)
    if not v.get("blocked"):
        return {"blocked": False, "count": v.get("count", 0), "evidence": v.get("evidence") or []}
    head = (v.get("evidence") or [{}])[0]
    return {
        "blocked": True,
        "count": v.get("count"),
        "reason": (f"该目标（{param or file}）历史上已有 {v.get('count')} 次同类改动被证明"
                   f"无效或更差（最近：{head.get('ts')} {head.get('verdict')}：{head.get('reason')}），"
                   f"按防重复试错规则拒绝同向提案；如需再试必须提供新类型证据（如环境变化+新样本≥30）"),
        "evidence": v.get("evidence") or [],
    }


def reuse_attribution(form_type: str = "", min_confidence: float = 0.7,
                      min_n: int = 30) -> dict | None:
    """归因复用：该形态已有**高置信 + 大样本**的历史失效规律 → 直接复用结论（不调 LLM，省 token）。

    命中条件：教训类型 failure_pattern + 置信度 ≥ 0.7 + 样本 ≥ 30
             +（若给 form_type）该教训的 scope.form_types 覆盖此形态。
    未命中返回 None → 调用方走正常 LLM 归因。
    """
    kb_writer.flush_now()
    best: dict | None = None
    best_conf = -1.0
    for l in kb_store.query("lesson", {"type": "failure_pattern"}, limit=200):
        sc = l.get("scope") or {}
        fts = [str(x) for x in (sc.get("form_types") or [])]
        if form_type and fts and form_type not in fts:
            continue
        if float(l.get("confidence") or 0) < min_confidence:
            continue
        n = int((l.get("support") or {}).get("n") or 0)
        if n < min_n:
            continue
        hint = str((l.get("actionable") or {}).get("hint") or "")
        conf_now = float(l.get("confidence") or 0)
        if conf_now <= best_conf:
            continue
        best_conf = conf_now
        best = {
            "error_type": sc.get("error_type") or "其他",
            "stage": sc.get("stage") or "other",
            "stage_hint": (l.get("text") or "")[:80],
            "optimize": hint,
            "lesson": l.get("text"),
            "fix": hint,
            "signal_hint": "",
            "param_hint": "无需调参",
            "confidence": "high",
            "reused": True,
            "from_lesson": l.get("id"),
            "support_n": n,
            "confidence_score": conf_now,
        }
    return best


def distill_and_reindex() -> dict:
    """维护任务：外部知识库挂引用 + 教训时效衰减 + 向量索引刷新（建议每日/每周低频调用）。"""
    external = {}
    inherited: dict = {}
    evidence: dict = {}
    try:
        from app.kb import kb_ingest
        external = kb_ingest.import_external_kbs()   # plans/21 §三：外部软知识库只读挂引用
        # 补全"失败但无归因"的案例（同形态就近继承，标 low/不污染教训库）→ 归因覆盖率趋近 100%
        inherited = kb_ingest.inherit_missing_attribution()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[kb_context] 外部知识库挂引用/归因补全跳过: {exc}")
    # plans/23·24·25 模块验证产出（洗盘/买点/博弈特征/情绪门控/纪律网格/自证）→ EKB
    # 让"新增的每一个模块"都成为进化大脑的神经，而不是散落在 data/*.json 里
    try:
        from app.kb import kb_evidence
        evidence = kb_evidence.absorb_all()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[kb_context] 模块证据接入跳过: {exc}")
    decayed = kb_distiller.decay_all()
    archived = kb_distiller.archive_stale()   # 长期单例教训归档（压缩不删事实，防库无限膨胀）
    indexed = kb_index.index_lessons()
    # 预热 FTS/jieba（首调约 1s 的一次性成本），避免用户首次查询/巡检时卡顿
    try:
        warm = kb_index.warm_up(full=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[kb_context] 检索预热跳过: {exc}")
        warm = {}
    return {"external": external, "attribution_inherited": inherited,
            "evidence": evidence,
            "decayed": decayed, "archived": archived,
            "indexed": indexed, "warm": warm, "index_stats": kb_index.stats()}


__all__ = ["build_knowledge_context", "knowledge_txt", "veto_check",
           "reuse_attribution", "distill_and_reindex"]
