"""成功案例归因（success_attrib）— 对齐 plans/21 §5.6 成功侧知识

用户核心诉求："**成功上涨的股票为何成功上涨**"——失败侧已有 daily_verify/reflect，
成功侧完全缺失，导致系统只学会"避开什么"，学不会"抓住什么"。

关键设计（防幸存者偏差，plans/21 §九）：
  - 每例成功必须配 `pair_k`（默认 2）例 **同形态、同日、未上涨** 的失败案例作对照，
    成对喂给 LLM，问"差异变量是什么"，禁止单侧总结；
  - 每日 LLM 归因采样上限 `success.attrib_per_day`（默认 5，控 token）；
  - 归因结果回写 case.cause + 蒸馏为 success_pattern 教训（kb_distiller）。

闭环：daily_verify 入库 case(success) → 本模块配对归因 → 教训 → 供进化大脑/写代码/对话检索。
"""
from __future__ import annotations

import time

from app.core.logger import logger
from app.agents.llm_client import deepseek_chat
from app.kb import kb_store, kb_writer, kb_distiller

_SUCCESS_TYPES = ("形态质量(颈线突破/量能配合) / 题材政策驱动 / 资金流入 / 板块效应 / 市场环境顺风 / 随机波动")

_SYSTEM = """你是一名A股量化复盘分析师。任务：剖析"被系统推荐后确实上涨"的成功案例，
回答**它为什么成功上涨**，并与同日同形态但**未上涨的对照案例**比较，找出**关键差异变量**。
要求：
1. 必须基于给出的对照差异，禁止事后诸葛式泛泛而谈；
2. 结论要**可操作**（能指向具体环节/参数/信号），供进化大脑复用；
3. 区分"可复制的规律"与"随机波动"（若差异不显著，必须判为随机波动）。
输出 JSON 对象（不要其他文字）。"""

_USER_TMPL = """【成功案例】{s_name}（{s_code}）推荐日 {date}
推荐依据：{s_pick}
实际：次日(T+1, D+1买入持有1日)涨幅 {s_t1}%，跑赢大盘 {s_excess}pp；T+5 {s_t5}
环境：{env}

【对照案例】同日同形态（{form}）但未上涨的 {k} 只：
{controls}

请输出 JSON：
{{
  "success_type": "从这些里选一个：{types}",
  "key_signal": "最关键的可复制信号（不超过20字）",
  "diff_vs_control": "与对照最本质的差异变量（不超过50字）",
  "lesson": "一句话可复用教训（不超过40字）",
  "fix": "如何在推荐/筛选环节复现该成功要素（不超过50字，可操作）",
  "stage": "该要素落在哪个环节（stock_pool/form_identify/feature_extract/pattern_match/neg_exclude/condition_score/rule_signal/risk_filter/rank_select/agent_refine/market/other）",
  "param_hint": "可调整的参数方向或"无需调参"",
  "replicable": "high/mid/low（可复制性）",
  "confidence": "high/mid/low"
}}"""


def _cfg() -> dict:
    try:
        from app.agents import evolution_config
        return (evolution_config.get("knowledge_base", {}) or {}).get("success", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _per_day() -> int:
    try:
        return int(_cfg().get("attrib_per_day", 5))
    except Exception:  # noqa: BLE001
        return 5


def _pair_k() -> int:
    try:
        return max(1, int(_cfg().get("pair_k", 2)))
    except Exception:  # noqa: BLE001
        return 2


def _fmt_pick(c: dict) -> str:
    s = c.get("snapshot") or {}
    parts = [f"形态 {c.get('form_type') or '?'}",
             f"相似度 {s.get('similarity')}",
             f"综合上涨概率 {s.get('up_probability')}",
             f"负向抑制 {s.get('negative_score')}"]
    if s.get("hit_rules"):
        parts.append("规则命中 " + "、".join(str(x) for x in s["hit_rules"][:3]))
    if s.get("hit_conditions"):
        parts.append("条件命中 " + "、".join(str(x) for x in s["hit_conditions"][:5]))
    if s.get("agent_reason"):
        parts.append(f"Agent理由 {s['agent_reason'][:60]}")
    return "；".join(parts)


def _fmt_control(c: dict) -> str:
    o = c.get("outcome") or {}
    cause = c.get("cause") or {}
    return (f"- {c.get('name') or c.get('ts_code')}：T+1 {float(o.get('t1_ret') or 0) * 100:+.2f}%，"
            f"依据（{_fmt_pick(c)}），失效归因：{cause.get('error_type') or '未归因'}"
            f"{('（' + str(cause.get('stage_hint')) + '）') if cause.get('stage_hint') else ''}")


def build_pairs(date_str: str, limit: int | None = None, force: bool = False) -> list[dict]:
    """构建"成功 + 同日同形态失败对照"配对（数据取自 EKB 案例档案）。

    force=False（默认）只取尚无成功归因的案例（幂等，不重复烧 token）。
    """
    date_str = str(date_str)[:8]
    kb_writer.flush_now()   # 保证同批写入的 case 可读
    cases = kb_store.query("case", {"date": date_str}, limit=2000)
    if not cases:
        return []
    succ = [c for c in cases if c.get("side") == "success"]
    fail = [c for c in cases if c.get("side") == "failure"]
    if not succ:
        return []
    # 只归因尚无成功归因的案例（幂等，避免重复烧 token）
    todo = [c for c in succ
            if force or not (c.get("cause") or {}).get("success_type")]
    if limit is not None and limit > 0:
        todo = todo[:limit]
    k = _pair_k()
    pairs: list[dict] = []
    for s in todo:
        form = s.get("form_type") or ""
        ctrl = [c for c in fail if (c.get("form_type") or "") == form][:k]
        if len(ctrl) < k:
            ctrl = (ctrl + [c for c in fail if c not in ctrl])[:k]
        if not ctrl:
            continue   # 无对照 → 不做单侧总结（防幸存者偏差）
        pairs.append({"success": s, "controls": ctrl})
    return pairs


def _attribute_one(pair: dict) -> dict | None:
    s = pair["success"]
    ctrl = pair["controls"]
    o = s.get("outcome") or {}
    env = s.get("env") or {}
    try:
        user = _USER_TMPL.format(
            s_name=s.get("name") or s.get("ts_code"), s_code=s.get("ts_code"),
            date=s.get("date"), s_pick=_fmt_pick(s),
            s_t1=f"{float(o.get('t1_ret') or 0) * 100:.2f}",
            s_excess=f"{float(o.get('excess_t1') or 0) * 100:.2f}" if o.get("excess_t1") is not None else "N/A",
            s_t5=(f"{float(o.get('t5_ret') or 0) * 100:.2f}%" if o.get("t5_ret") is not None else "未到"),
            env=f"{env.get('regime', 'unknown')}（指数T+1 {env.get('index_t1_ret')}）",
            form=s.get("form_type") or "?",
            k=len(ctrl),
            controls="\n".join(_fmt_control(c) for c in ctrl),
            types=_SUCCESS_TYPES,
        )
        result = deepseek_chat([
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[success_attrib] 归因调用失败 {s.get('ts_code')}: {exc}")
        return None
    return result if isinstance(result, dict) else None


def run_success_attrib(date: str | None = None, dates: list[str] | None = None,
                       max_per_day: int | None = None, force: bool = False) -> dict:
    """成功侧归因主入口（默认归因最近一期）。

    Args:
        date: 指定单个日期（YYYYMMDD）
        dates: 指定多期（daily_verify 批量调用）
        max_per_day: 覆盖配置的每日采样上限
        force: True 时对已有归因的案例也重新归因
    """
    per_day = max_per_day if max_per_day is not None else _per_day()
    if per_day <= 0:
        return {"ok": False, "reason": "成功侧归因已关闭（success.attrib_per_day<=0）"}
    targets = [str(d)[:8] for d in (dates or ([date] if date else [])) if d]
    if not targets:
        kb_writer.flush_now()
        latest = kb_store.query("case", {"side": "success"}, limit=1)
        if latest:
            targets = [str(latest[0].get("date"))[:8]]
    if not targets:
        return {"ok": False, "reason": "库中暂无成功案例（需先完成次日验证入库）"}

    all_attributed: list[dict] = []
    for d in targets:
        for p in build_pairs(d, limit=per_day, force=force):
            a = _attribute_one(p)
            if not a:
                continue
            s = p["success"]
            cause = {
                "success_type": a.get("success_type", "其他成功模式"),
                "key_signal": a.get("key_signal", ""),
                "diff_vs_control": a.get("diff_vs_control", ""),
                "lesson": a.get("lesson", ""),
                "success_lesson": a.get("lesson", ""),
                "fix": a.get("fix", ""),
                "stage": a.get("stage", "other"),
                "param_hint": a.get("param_hint", "无需调参"),
                "replicable": a.get("replicable", "low"),
                "confidence": a.get("confidence", "mid"),
                "attributed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "control_cases": [c.get("id") for c in p["controls"]],
            }
            # 回写案例（同步 upsert：成功归因低频，需立即对后续检索可见）
            rec = dict(s)
            rec["cause"] = cause
            rec["confidence"] = 0.75 if a.get("confidence") == "high" else (
                0.55 if a.get("confidence") == "mid" else 0.35)
            rec["stage"] = a.get("stage") or rec.get("stage") or ""
            rec["source"] = f"{rec.get('source') or 'ekb'}+success_attrib"
            kb_store.upsert("case", rec)
            all_attributed.append(rec)

    lessons = kb_distiller.absorb_success_cases(all_attributed)
    if all_attributed:
        logger.info(f"[success_attrib] 成功归因 {len(all_attributed)} 例 → 成功模式教训 {lessons} 条")
    return {"ok": True, "attributed": len(all_attributed), "lessons": lessons,
            "dates": targets,
            "samples": [{"ts_code": c.get("ts_code"), "type": (c.get("cause") or {}).get("success_type")}
                        for c in all_attributed]}


def build_success_txt(limit: int = 3) -> str:
    """成功模式结论文本（供 L0 摘要/进化大脑：该抓住什么）。"""
    kb_writer.flush_now()
    lessons = [l for l in kb_store.query("lesson", {"type": "success_pattern"}, limit=50)
               if l.get("status") in ("active", "candidate")]
    if not lessons:
        return ""
    lessons.sort(key=lambda x: (-float(x.get("confidence") or 0),))
    lines = []
    for l in lessons[:limit]:
        n = (l.get("support") or {}).get("n", 0)
        lines.append(f"{l.get('text')}（{n}例，置信 {float(l.get('confidence') or 0):.2f}）")
    return "成功模式: " + " | ".join(lines)


__all__ = ["build_pairs", "run_success_attrib", "build_success_txt"]
