"""进化大脑（evolution_agent）— 对齐 plans/11 进化Agent §7

MVP：读 L0 摘要 → LLM 提案 → 规则层宪法校验 → 候选池（evolution_decisions.json，pending 待人工确认）。

MVP 不自动应用：提案经人工确认（confirm_proposal）后由 self_improver 热生效。
"""
from __future__ import annotations

import json
import os
import time

from app.core.logger import logger
from app.agents.llm_client import deepseek_chat
from app.agents import evolution_config
from app.agents import evolution_summarizer
from app.agents import self_improver
from app.agents import evolution_shadow

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
DECISIONS_FILE = os.path.join(DATA_DIR, "evolution_decisions.json")

_SYSTEM = """你是量化系统的进化大脑，核心使命：进化出"高概率上涨的每日推荐"（plans/17）。
基于系统近期表现、次日自我验证结论（L0 摘要中的"次日验证/失效归因/改进方向"——即"为什么推荐的股票没涨"）与当前参数，
提出最小改进提案（每次只改一个参数，G3）。
优先参考失效归因的 param_hint，优先调整每日推荐/回测/次日验证相关参数（daily_* / hit_threshold / t1_hit_threshold / risk_* / outcome_* / 各阈值），
只允许调整 evolution_config 中登记的参数。每个提案必须给出可追溯的数据依据（引用次日T+1命中率/T+5命中率/样本数/归因类型），
证据不可编造。若当前表现正常无需调整，输出空数组 []。
输出 JSON 数组，每项：{"param": "参数名", "new": 新值(数值), "reason": "为什么改(≤80字)", "evidence": "数据依据(引用具体统计)"}。"""

_USER_TMPL = """系统当前状态：
{summary}

可调参数：
{params}

请输出改进提案（JSON 数组）。"""


def _read_decisions() -> list:
    try:
        with open(DECISIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


MAX_DECISIONS = 500   # Bug18：候选池/决策历史滚动上限，防无限增长

def _save_decisions(records: list) -> None:
    # Bug18：保留最近 MAX_DECISIONS 条（保留 pending，优先裁剪已终结记录）
    if len(records) > MAX_DECISIONS:
        pending = [r for r in records if r.get("status") == "pending"]
        rest = [r for r in records if r.get("status") != "pending"]
        records = pending + rest[-(MAX_DECISIONS - len(pending)):]
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(DECISIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution_agent] 候选池写入失败: {exc}")


def _constitution_check(proposal: dict, summary: dict) -> dict:
    """规则层宪法校验（MVP）：
    参数存在 / 数值范围 / 非保护区 / 样本量门槛 / 候选池无同参数 pending / 证据非空。
    """
    param = proposal.get("param")
    new = proposal.get("new")
    if not param:
        return {"ok": False, "reason": "缺参数名"}
    params = evolution_config.params_table()
    p = params.get(param)
    if not p:
        return {"ok": False, "reason": f"未知参数 {param}（不在可调表）"}

    # 保护区（H1）
    if p.get("file", "") in evolution_config.protected_files():
        return {"ok": False, "reason": f"{param} 目标文件在进化保护区"}

    # 参数冻结（H3）：影子期参数禁止触碰（含新提案）
    if evolution_shadow.is_frozen(param):
        return {"ok": False, "reason": f"{param} 正在影子期冻结（H3），禁止改动"}

    # 数值范围
    try:
        nv = float(new)
    except (TypeError, ValueError):
        return {"ok": False, "reason": f"{param} 新值非数值"}
    if "min" in p and nv < float(p["min"]):
        return {"ok": False, "reason": f"{param} 新值 {nv} 低于下限 {p['min']}"}
    if "max" in p and nv > float(p["max"]):
        return {"ok": False, "reason": f"{param} 新值 {nv} 高于上限 {p['max']}"}

    # 样本量门槛（H5/D14：提案证据样本 ≥ 阈值）
    thr = evolution_config.get("thresholds", {})
    min_sample = thr.get("proposal_evidence_min", 30)
    samples = summary.get("monitor", {}).get("samples", 0)
    if samples < min_sample:
        return {"ok": False, "reason": f"样本 {samples} < {min_sample}，证据不足"}

    # 证据非空（G2 简化：MVP 校验非空，完整可追溯留扩展）
    if not proposal.get("evidence"):
        return {"ok": False, "reason": "缺少数据依据 evidence"}

    # 否证清单硬约束（plans/21 §九.4）：该参数历史上已被证明无效/更差 ≥N 次 → 拒绝重复同向改动
    try:
        from app.kb import kb_context as _kctx
        v = _kctx.veto_check(param=param)
        if v.get("blocked"):
            return {"ok": False, "reason": v["reason"]}
    except Exception:  # noqa: BLE001
        pass

    # 候选池去重：无同参数 pending
    for r in _read_decisions():
        if r.get("param") == param and r.get("status") == "pending":
            return {"ok": False, "reason": f"{param} 已有待确认提案"}

    return {"ok": True}


def _auto_allowed(param: str) -> bool:
    """auto 模式确认前置检查（规划 §11 A4/B3 + 守护暂停防御）：返回是否允许自动确认该参数提案。"""
    try:
        from app.agents import evolution_guard
        if evolution_guard.is_paused():
            return False
        if evolution_guard.frequency_check().get("cooldown_suggested"):
            return False
    except Exception:  # noqa: BLE001
        pass
    # 同参数冷却（A4）：距上次该参数成功应用 < cooldown_days 天内不再自动改
    try:
        from datetime import datetime
        cd = int(evolution_config.get("cooldown_days", 10) or 10)
        st = evolution_guard._read_state()
        for r in reversed(st.get("applied_log", [])):
            if r.get("param") == param and r.get("status") == "ok":
                ts = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
                return (datetime.now() - ts).days >= cd
    except Exception:  # noqa: BLE001
        pass
    return True


def evaluate() -> dict:
    """生成改进提案（LLM）→ 宪法校验 → 候选池。MVP 不自动应用。"""
    # Bug19：先 flush 异步事件队列，保证摘要能读到刚入队的反思/榜单事件（H4 事件驱动）
    try:
        from app.agents import evolution_events
        evolution_events.flush_now()
    except Exception:  # noqa: BLE001
        pass
    summary = evolution_summarizer.build_l0_summary()
    text = evolution_summarizer.build_l0_text(summary)
    params = evolution_config.params_table()
    # plans/21 §8.2：私有域知识库综合分析（历史同类改动结论 + 否证清单 + 高置信教训 + 成功/失败对照）
    kb_ctx: dict = {}
    kb_txt = ""
    try:
        from app.kb import kb_context as _kctx
        kb_ctx = _kctx.build_knowledge_context(query=(text or "")[:120])
        kb_txt = _kctx.knowledge_txt(kb_ctx)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[evolution_agent] 知识库上下文跳过: {exc}")
    # Bug24：参数表标注 Tier 层级（Tier1 热生效 / Tier2 需重启），引导 LLM 优先微调 Tier1
    params_txt = "\n".join(
        f"- {k}: current={v.get('current', v.get('default'))}, range=[{v.get('min')},{v.get('max')}], "
        f"step={v.get('step')}, tier={v.get('tier', 1)}, {v.get('desc', '')}"
        for k, v in params.items()
    )
    # Bug23：样本不足直接跳过 LLM（对齐 §6/§14 token 预算）——宪法校验必拒，无需浪费调用
    try:
        min_sample = (evolution_config.get("thresholds", {}) or {}).get("proposal_evidence_min", 30)
        samples = summary.get("monitor", {}).get("samples", 0)
    except Exception:  # noqa: BLE001
        min_sample, samples = 30, 0
    if samples < min_sample:
        return {"proposals": [], "skipped": "sample_insufficient",
                "reason": f"样本 {samples} < {min_sample}，跳过 LLM 评估（省 token）", "summary": text}
    try:
        result = deepseek_chat([
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _USER_TMPL.format(
                summary=text + ("\n\n" + kb_txt if kb_txt else ""), params=params_txt)},
        ])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution_agent] LLM 提案失败: {exc}")
        return {"proposals": [], "error": str(exc), "summary": text}

    proposals = result if isinstance(result, list) else ([] if not result else [result])
    # Bug36：每批提案上限（规划 §11"每批最多 M 个提案"，防 LLM 一次给一堆全自动进化/占满影子）
    try:
        max_batch = int(evolution_config.get("max_proposals_per_batch", 3) or 3)
    except Exception:  # noqa: BLE001
        max_batch = 3
    proposals = proposals[:max_batch]
    accepted: list[dict] = []
    rejected_n = 0
    records = _read_decisions()
    # Bug2 修复：提案 id 用历史最大 id + 1（避免历史清理/重复后冲突）
    next_id = max((int(r.get("id", 0)) for r in records), default=0) + 1
    for p in proposals:
        check = _constitution_check(p, summary)
        if check["ok"]:
            sim = evolution_shadow.simulate_threshold(p.get("param"), p.get("new"))  # F3
            # 提案卡片（E8）：含回测模拟 + 证据追溯 + 最小改动说明（G3），供前端审批展示
            rec = {
                "id": next_id,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "param": p.get("param"), "new": p.get("new"),
                "old": evolution_config.get_param(p.get("param")),
                "reason": p.get("reason", ""), "evidence": p.get("evidence", ""),
                "backtest_sim": sim,  # F3
                "card": {
                    "param": p.get("param"), "old": evolution_config.get_param(p.get("param")),
                    "new": p.get("new"), "direction": sim.get("direction", "-"),
                    "baseline_hit_rate": sim.get("baseline_hit_rate", 0.0),
                    "impact_note": sim.get("impact_note", ""),
                    "evidence_trace": p.get("evidence", "")[:120],
                    "minimal_change": f"仅调整 {p.get('param')}（一参数一改，G3）",
                },
                "status": "pending",
            }
            # plans/21 §七：提案入私有域知识库（实验台账 + 因果链），并关联教训以回馈置信度
            try:
                from app.kb import kb_ingest as _kbi
                old_v = rec.get("old")
                exp_id = (f"experiment:{time.strftime('%Y%m%d')}:{rec.get('param')}:"
                          f"{old_v}->{p.get('new')}")
                _kbi.record_experiment(
                    exp_id, kind="param",
                    target={"param": rec.get("param"),
                            "file": (params.get(rec.get("param")) or {}).get("file")},
                    change={"old": old_v, "new": p.get("new")},
                    hypothesis=str(p.get("reason") or "")[:200],
                    evidence_in=str(p.get("evidence") or "")[:200],
                    status="proposed",
                    related_lessons=[l.get("id") for l in (kb_ctx.get("lessons") or [])],
                )
                rec["kb_experiment"] = exp_id
            except Exception:  # noqa: BLE001
                pass
            accepted.append(rec)
        else:
            rejected_n += 1
            logger.info(f"[evolution_agent] 提案被宪法校验拒绝: {check['reason']}")
    records.extend(accepted)
    _save_decisions(records)
    # 自动进化模式（approval_mode=auto，用户要求：无需人工审核，直接进化，每次留备份可回滚）
    auto_mode = (evolution_config.get("approval_mode") or "manual") == "auto"
    auto_confirmed: list[dict] = []
    if auto_mode and accepted:
        for rec in accepted:
            if not _auto_allowed(rec["param"]):
                # 守护暂停 / 全局冷却（B3）/ 同参数冷却（A4）→ 跳过自动确认，保持 pending
                rec["auto_skip"] = True
                logger.info(f"[evolution_agent] 自动进化跳过（冷却/暂停）: {rec['param']}")
                continue
            res = confirm_proposal(rec["id"])
            # confirm_proposal 已更新磁盘状态；回读最新状态（影子期/已应用/失败）
            fresh = next((r for r in _read_decisions() if r.get("id") == rec["id"]), None)
            rec["status"] = (fresh or rec).get("status", rec.get("status", "pending"))
            auto_confirmed.append({
                "id": rec["id"], "param": rec["param"], "old": rec["old"], "new": rec["new"],
                "status": rec["status"], "ok": res.get("ok", False),
                "action": res.get("action", res.get("status", "")), "result": res,
            })
            logger.info(
                f"[evolution_agent] 自动进化（auto）: {rec['param']} {rec['old']} -> {rec['new']} → {rec['status']}")
        # Tier2/3 待重启补丁 → 自动触发独立进程重启（E2：脚本含避让窗口 + 健康检查 + 失败回滚 .bak）
        try:
            from app.agents import self_improver as _si
            if _si.pending_restart():
                _si.evolution_apply()
        except Exception:  # noqa: BLE001
            pass
    logger.info(
        f"[evolution_agent] 评估完成: 提案 {len(accepted)}，拒绝 {rejected_n}，自动进化 {len(auto_confirmed)}，累计候选池 {len(_read_decisions())}")
    return {"proposals": accepted, "rejected": rejected_n, "summary": text,
            "auto": auto_mode, "auto_confirmed": auto_confirmed}


def confirm_proposal(proposal_id: int) -> dict:
    """人工确认提案。确定性参数（Tier1）→ 进入影子期（先验证后生效 F1，settle 后裁决）；
    非影子参数 → self_improver 直接应用。"""
    records = _read_decisions()
    for r in records:
        if r.get("id") == proposal_id and r.get("status") == "pending":
            params = evolution_config.params_table()
            p = params.get(r.get("param"), {})
            if p.get("shadow_eligible") and p.get("hot_reload"):
                # 先验证后生效：进入影子期（主流程仍旧，settle_shadow 后裁决生效/回滚）
                res = evolution_shadow.start_shadow(r["param"], r.get("old"), r["new"])
                if res.get("ok"):
                    r["status"] = "shadowing"
                    r["shadow_status"] = res.get("status")
                else:
                    # Bug1 修复：并行上限拒绝 → 保持 pending，不误标 shadowing
                    r["status"] = "pending"
                    r["confirm_reject"] = res.get("reason", "影子启动失败")
                    r["result"] = res
            else:
                res = self_improver.apply_proposal(
                    {"param": r["param"], "new": r["new"], "reason": r.get("reason", ""),
                     "evidence": r.get("evidence", "")})
                r["status"] = "applied" if res.get("ok") else "failed"
                r["result"] = res
            r["confirmed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_decisions(records)
            return res
    return {"ok": False, "action": "reject", "message": f"提案 {proposal_id} 不存在或非待确认"}


def pending_proposals() -> list:
    """候选池待确认提案。"""
    return [r for r in _read_decisions() if r.get("status") == "pending"]


def decisions() -> list:
    return _read_decisions()


__all__ = ["evaluate", "confirm_proposal", "pending_proposals", "decisions"]
