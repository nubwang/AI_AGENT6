"""系统管理接口"""
import time

from fastapi import APIRouter, HTTPException
from app.core.config import settings
from app.core.logger import logger

router = APIRouter(prefix="/api/v1/system", tags=["系统管理"])


@router.get("/health")
def health_check():
    """健康检查"""
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


@router.get("/stats")
def system_stats():
    """系统统计"""
    return {
        "status": "running",
        "data_collection_time": settings.data_collect_time,
        "prediction_time": settings.prediction_time,
    }


# ── 进化 Agent（对齐 plans/11 进化Agent §15.7）──────────────
@router.get("/evolve/status")
def evolve_status():
    """进化状态：候选池 + 影子实验 + 守护报告 + 终身记账 + 审批模式。"""
    from app.agents import (evolution_agent, evolution_shadow, evolution_guard,
                            evolution_ledger, evolution_config)
    try:
        return {
            "pending": evolution_agent.pending_proposals(),
            "shadow": evolution_shadow.shadow_status(),
            "guard": evolution_guard.guard_scan(),
            "ledger": evolution_ledger.ledger_report(),
            "paused": evolution_guard.is_paused(),
            "approval_mode": evolution_config.get("approval_mode") or "manual",
            "recent_proposals": evolution_agent.decisions()[-20:],
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/evolve/evaluate")
def evolve_evaluate():
    """手工触发评估（生成改进提案进候选池）。守护暂停时拒绝。"""
    from app.agents import evolution_agent, evolution_guard
    if evolution_guard.is_paused():
        return {"ok": False, "reason": "进化已暂停（守护）"}
    return evolution_agent.evaluate()


@router.post("/evolve/confirm")
def evolve_confirm(proposal_id: int):
    """人工确认提案（确定性参数进影子期 / 非影子直接应用）。"""
    from app.agents import evolution_agent
    return evolution_agent.confirm_proposal(proposal_id)


@router.post("/evolve/settle")
def evolve_settle(param: str):
    """结算影子实验（裁决生效 / 回滚 / 样本不足继续）。"""
    from app.agents import evolution_shadow
    return evolution_shadow.settle_shadow(param)


@router.post("/evolve/rollback")
def evolve_rollback(index: int = 1):
    """回滚最近第 index 次成功应用（index=1 最近一次；规划 §8.5 可回退任意历史版本）。"""
    from app.agents import self_improver
    return self_improver.rollback_last(index)


@router.post("/evolve/pause")
def evolve_pause(reason: str = "人工暂停"):
    """人工暂停进化（Bug27：守护可自动暂停，但需人工可控；防误暂停后无法恢复）。"""
    from app.agents import evolution_guard
    evolution_guard.pause(reason)
    return {"ok": True, "paused": True, "reason": reason}


@router.post("/evolve/resume")
def evolve_resume():
    """人工恢复进化（Bug27：解除守护/人工暂停）。"""
    from app.agents import evolution_guard
    evolution_guard.resume()
    return {"ok": True, "paused": False}


# ── 扩展2：Tier2/3 独立进程重启（E2/G9）──────────────
@router.get("/evolve/restart/status")
def evolve_restart_status():
    """待重启状态：是否有未生效的 Tier2/3 代码改动。"""
    from app.agents import self_improver
    return {"pending": self_improver.pending_restart()}


@router.get("/evolve/events")
def evolve_events_list(limit: int = 50):
    """事件流（§15.7）：前端进化中心展示最近事件。"""
    from app.agents import evolution_events
    return {"events": evolution_events.pending_events(0, limit=limit)}


@router.post("/evolve/event")
def evolve_event_report(level: str = "INFO", source: str = "frontend", message: str = ""):
    """前端错误/告警上报（§15.2）：前端 console / 接口 4xx-5xx → 进化事件流。"""
    if not message:
        return {"ok": False, "reason": "缺少 message"}
    from app.agents import evolution_events
    evolution_events.record_event(level.upper() if level else "INFO", source, message, 1)
    return {"ok": True}


# ── 次日自我验证（plans/17：每天新日线行情出来后，验证"为什么推荐的股票没涨"）────
_VERIFY_STATE = {
    "status": "idle",          # idle / running / success / noop / failed
    "message": "",
    "started_at": "",
    "finished_at": "",
    "report": None,
}


def _verify_worker(limit_days: int | None, max_failures: int) -> None:
    from app.agents import daily_verify
    _VERIFY_STATE["status"] = "running"
    _VERIFY_STATE["message"] = "次日自我验证进行中（T+1 回填 → 失效归因）"
    try:
        res = daily_verify.run_daily_verify(limit_days=limit_days, max_failures=max_failures)
        _VERIFY_STATE["report"] = res
        if res.get("ok"):
            latest = res.get("latest") or {}
            _VERIFY_STATE["status"] = "success"
            _VERIFY_STATE["message"] = (
                f"验证完成 {latest.get('date', '')}: T+1命中率 {latest.get('t1_hit_rate', 0) * 100:.1f}% "
                f"(涨{latest.get('up_n', 0)}/平{latest.get('flat_n', 0)}/跌{latest.get('down_n', 0)})"
            )
        else:
            _VERIFY_STATE["status"] = "noop"
            _VERIFY_STATE["message"] = res.get("reason", "暂无新验证")
        _VERIFY_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as exc:  # noqa: BLE001
        _VERIFY_STATE["status"] = "failed"
        _VERIFY_STATE["message"] = f"次日自我验证失败: {exc}"
        _VERIFY_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


@router.get("/evolve/verify/status")
def evolve_verify_status():
    """次日自我验证任务状态 + 最近一期验证报告（前端进化中心展示）。"""
    return {
        **_VERIFY_STATE,
        "report": (_VERIFY_STATE.get("report") or {}).get("latest") if _VERIFY_STATE.get("report") else None,
        "backfill": (_VERIFY_STATE.get("report") or {}).get("backfill") if _VERIFY_STATE.get("report") else None,
    }


@router.post("/evolve/verify/run")
def evolve_verify_run(limit_days: int | None = None, max_failures: int = 0):
    """手动触发次日自我验证（后台：T+1 回填 → 识别失败 → 失效归因 → 固化 verify_kb）。"""
    import threading
    if _VERIFY_STATE["status"] == "running":
        return {"ok": False, "message": "次日验证已在进行中"}
    _VERIFY_STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _VERIFY_STATE["finished_at"] = ""
    _VERIFY_STATE["report"] = None
    t = threading.Thread(target=_verify_worker, args=(limit_days, max_failures or 0), daemon=True)
    t.start()
    return {"ok": True, "message": "次日自我验证已提交（后台执行）"}


@router.get("/evolve/verify/report")
def evolve_verify_report():
    """次日验证知识库报告（最新 + 历史 + 统计）。"""
    from app.agents import daily_verify
    return daily_verify.verify_report()


@router.post("/evolve/restart")
def evolve_restart():
    """触发独立进程重启（进化 Agent 不 kill 自己，由 scripts/restart_backend.sh 执行）。"""
    from app.agents import self_improver
    return self_improver.evolution_apply()


# ── 生成式代码进化（plans/18：进化大脑像 ZOO CODE 一样自己写代码，闲时自动改回测/推荐逻辑）──
@router.get("/evolve/code/status")
def evolve_code_status():
    """生成式代码进化状态：闲时检测 + 待确认代码提案 + 历史。"""
    from app.agents import code_writer, evolution_config
    st = code_writer.status()
    st["code_evolution"] = evolution_config.get("code_evolution", {})
    return st


@router.post("/evolve/code/propose")
def evolve_code_propose(mode: str = "manual", anchor_key: str | None = None):
    """手动触发"进化大脑自己写代码"：基于近期表现生成安全代码提案。
    mode=manual 存候选待确认；mode=auto 直接应用（须系统空闲）。
    """
    from app.agents import code_writer, evolution_summarizer
    summary = evolution_summarizer.build_l0_text()
    return code_writer.propose_code_evolution(summary=summary, mode=mode, anchor_key=anchor_key)


@router.post("/evolve/code/apply")
def evolve_code_apply(patch_id: int):
    """人工确认应用一条待确认的生成式代码提案（走完整安全链：AST 白名单→备份→编译→回滚）。"""
    from app.agents import code_writer
    return code_writer.apply_pending(patch_id)


@router.post("/evolve/code/rollback")
def evolve_code_rollback(index: int = 1):
    """回滚最近第 index 次生成式代码补丁（恢复版本化备份 + 清待重启标记）。"""
    from app.agents import code_writer
    return code_writer.rollback_last_code(index)


# ── 报错监控 → 进化大脑修复 bug（用户新需求）──────────────
@router.get("/evolve/bugfix/errors")
def evolve_bugfix_errors(hours: float = 24, limit: int = 50):
    """查看近期后台报错（ERROR/WARNING，按重复次数排序），进化大脑据此修复 bug。"""
    from app.agents import evolution_events
    return {"errors": evolution_events.recent_errors(hours=hours, limit=limit)}


@router.post("/evolve/bugfix/run")
def evolve_bugfix_run(hours: float = 24, mode: str = "auto", max_errors: int = 15):
    """触发进化大脑修复后台报错 bug（读取近期报错 → LLM 定位 → 生成修复补丁）。
    mode=auto 直接应用（走完整安全链）；mode=manual 存待确认。"""
    from app.agents import code_writer, evolution_guard
    if evolution_guard.is_paused():
        return {"ok": False, "reason": "进化已暂停（守护），跳过 bug 修复"}
    return code_writer.propose_bug_fix(hours=hours, mode=mode, max_errors=max_errors)


@router.get("/evolve/map")
def evolve_map():
    """系统图谱：回测与每日推荐的逻辑+参数（实时动态，随进化修改自动更新）。

    进化大脑据此定位可优化的环节/参数；同时写 evolution_map.json 快照供前端/审计。
    """
    from app.agents import system_map
    return system_map.save_map()


# ── 回测控制与诊断（plans/19：进化大脑控制回测，考虑"是不是回测的问题"）──
@router.get("/evolve/backtest/diag")
def evolve_backtest_diag():
    """回测健康诊断：对比回测命中率 vs 每日推荐实盘命中率，
    判断是不是回测逻辑的问题（失效/产物脱节/市场/正常）。"""
    from app.agents import backtest_ctl
    return backtest_ctl.backtest_health()


@router.get("/evolve/backtest/status")
def evolve_backtest_status():
    """回测运行状态（进化大脑控制回测）。"""
    from app.agents import backtest_ctl
    return backtest_ctl.status()


@router.post("/evolve/backtest/run")
def evolve_backtest_run(start_date: str = "20100101", end_date: str = "",
                              max_stocks: int | None = None, force: bool = False):
    """进化大脑/人工控制回测：触发一次回测（后台，限频 + 不抢占已运行任务）。"""
    from app.agents import backtest_ctl
    return backtest_ctl.trigger_backtest(start_date=start_date, end_date=end_date,
                                         max_stocks=max_stocks, source="evolution_api",
                                         force=force)


# ── 私有域知识库（plans/21：进化大脑长期记忆；分析必查、越用越准）──────────
@router.get("/evolve/kb/stats")
def evolve_kb_stats():
    """知识库总览：档案量 / 教训分布 / 检索层与向量可用性 / 写入网关状态。"""
    from app.kb import kb_writer, kb_distiller, kb_index, kb_evidence
    # 成功侧规律文本（用户诉求"成功上涨的股票为何成功上涨"）：库中成功模式教训的可读摘要
    try:
        from app.agents import success_attrib
        success_txt = success_attrib.build_success_txt(limit=3)
    except Exception:  # noqa: BLE001
        success_txt = ""
    return {"writer": kb_writer.stats(), "lessons": kb_distiller.stats(),
            "index": kb_index.stats(), "success_pattern_text": success_txt,
            # plans/23·24·25 模块验证产出的接入情况（"神经"是否连通）
            "evidence": kb_evidence.stats()}


@router.get("/evolve/kb/evidence")
def evolve_kb_evidence(limit: int = 30, module: str | None = None):
    """证据型知识清单（plans/23·24·25 的验证结论 + 交易纪律教训）。

    这些是"被统计检验过的事实"：洗盘 A/B、四类买点成交与收益、博弈特征 FDR 结果、
    情绪门控、纪律网格择优、自证样本外超额、最佳入场、条件概率、形态规则有效性、成本口径。
    它们既是进化大脑每次提案的依据，也是核对"新增模块是否真的接上了大脑"的清单。
    """
    from app.kb import kb_evidence
    rows = kb_evidence.evidence_lessons(limit=max(1, min(limit, 200)))
    if module:
        rows = [r for r in rows
                if str((r.get("scope") or {}).get("module") or "") == module]
    return {"stats": kb_evidence.stats(),
            "items": [{"id": r.get("id"), "type": r.get("type"),
                       "text": r.get("text"),
                       "module": (r.get("scope") or {}).get("module") or r.get("type"),
                       "n": (r.get("support") or {}).get("n"),
                       "confidence": r.get("confidence"), "status": r.get("status"),
                       "hint": (r.get("actionable") or {}).get("hint")} for r in rows]}


@router.get("/evolve/permissions")
def evolve_permissions():
    """进化大脑的权限域与唯一判据（用户决策：介入全部，唯一原则=提高 D+1 与主升浪准确率）。

    - 可改：**全部业务代码**（app/**、scripts/**、frontend/src/**）+ **可新建文件/模块**
    - 不可改：度量与守护（命中判定、evolution_guard/events、evolution_config 宪法键、执行链本体）
      —— 因为改了它就能伪造"提升"，唯一原则就无法被证明
    - 判据：D+1 命中率 + 主升浪命中率（main_metric，immutable，守护哈希校验）
    """
    from app.agents import code_writer, evolution_config, evolution_guard
    mm = evolution_config.get("main_metric", {}) or {}
    ce = evolution_config.get("code_evolution", {}) or {}
    files = code_writer.evolvable_files()
    return {
        "goal": mm.get("goal") or ce.get("target") or "",
        "metric": {
            "primary": mm.get("primary") or ["t1_hit_rate", "wave_hit_rate"],
            "hit_window": mm.get("hit_window"),
            "hit_definition": mm.get("hit_definition"),
            "immutable": bool(mm.get("immutable", True)),
        },
        "scope": {
            "editable_roots": list(code_writer.EDITABLE_ROOTS),
            "editable_files_n": len(files),
            "can_create_files": True,
            "modes": ["function", "anchor", "full", "new_file", "param"],
        },
        "locked_files": list(code_writer.PERMISSION_LOCKED),
        "locked_funcs": [{"file": f, "function": fn}
                         for f, fn in sorted(code_writer.METRIC_CORE_FUNCS)],
        "security_level": ce.get("security_level"),
        "approval": ce.get("approval"),
        "scope_note": ce.get("scope") or "",
        "constitution_ok": evolution_guard.check_constitution().get("ok"),
    }


@router.get("/evolve/auto_apply")
def evolve_auto_apply_status():
    """自动生效与午间自进化状态（用户决策：不要人工重启 + 每天中午 DeepSeek 闲时自行进化）。

    - pending_restart：是否有"已改代码但未生效"的改动（守护会自动重启让它生效）
    - in_trade_window：交易窗口内不重启（避免打断盘中推荐），等 12:00-13:00 / 15:00 后
    - llm_period：当前 DeepSeek 计费时段（off_peak = 空闲半价）
    - should_restart_now / decision_reason：重启守护的当前判定（纯决策，可直接核对）
    - schedule：午间自动进化 + 重启守护的计划
    """
    from app.agents import evolution_config, self_improver
    ce = evolution_config.get("code_evolution", {}) or {}
    d = self_improver.auto_restart_decision()
    return {
        "pending_restart": bool(d.get("pending")),
        "pending_detail": d.get("pending"),
        "in_trade_window": d.get("in_trade_window"),
        "llm_period": d.get("llm_period"),
        "should_restart_now": d.get("should_restart"),
        "decision_reason": d.get("reason"),
        "last_auto_restart_epoch": d.get("last_auto_restart_epoch"),
        "schedule": {
            "noon_auto_evolve": bool(ce.get("noon_auto_evolve", True)),
            "noon_time": f"{int(ce.get('noon_hour', 12)):02d}:{int(ce.get('noon_minute', 5)):02d}",
            "require_off_peak": bool(ce.get("require_off_peak", True)),
            "auto_restart": bool(ce.get("auto_restart", True)),
            "restart_guard": "每 10 分钟检查（交易窗口内自动跳过）",
            "trade_windows": [list(w) for w in self_improver.TRADE_WINDOWS],
        },
    }


@router.get("/evolve/kb/search")
def evolve_kb_search(q: str = "", stage: str | None = None, side: str | None = None,
                           form: str | None = None, days: int = 90, limit: int = 20):
    """混合检索：案例（结构化为主）+ 教训（语义/BM25）+ 成功/失败对照配对。"""
    from app.kb import kb_index
    out: dict = {
        "cases": kb_index.search_cases(side=side, stage=stage, form_type=form,
                                       days=days, limit=limit),
        "lessons": kb_index.search_lessons(query=q, stage=stage, limit=5),
    }
    if side != "failure":
        out["control_pairs"] = kb_index.similar_cases(form_type=form, stage=stage, k=3)
    return out


@router.get("/evolve/kb/case/{case_id}")
def evolve_kb_case(case_id: str):
    """案例详情（含同形态同日"未上涨"对照，防幸存者偏差）。"""
    from app.kb import kb_store
    c = kb_store.get("case", case_id)
    if not c:
        raise HTTPException(status_code=404, detail=f"案例 {case_id} 不存在")
    controls = kb_store.query("case", {
        "date": c.get("date"), "form_type": c.get("form_type"), "side": "failure"}, limit=5)
    return {"case": c, "controls": controls}


@router.get("/evolve/kb/experiments")
def evolve_kb_experiments(verdict: str | None = None, status: str | None = None,
                                target: str | None = None, limit: int = 50):
    """实验台账 + 改动因果链 + 否证统计（进化过程全档案）。"""
    from app.kb import kb_store
    filters: dict = {}
    if verdict:
        filters["verdict"] = verdict
    if status:
        filters["status"] = status
    rows = kb_store.query("experiment", filters, limit=limit)
    if target:
        rows = [e for e in rows if target in str(e.get("target"))]
    changes = kb_store.query("change", limit=min(limit, 50))
    veto: dict[str, int] = {}
    for e in kb_store.query("experiment", limit=500):
        if str(e.get("verdict") or "") in ("worse", "no_change", "rolled_back",
                                           "abandoned", "invalid"):
            key = str((e.get("target") or {}).get("param")
                      or (e.get("target") or {}).get("file") or "unknown")
            veto[key] = veto.get(key, 0) + 1
    return {"experiments": rows, "changes": changes,
            "veto_counts": dict(sorted(veto.items(), key=lambda kv: -kv[1])[:20])}


@router.get("/evolve/kb/lessons")
def evolve_kb_lessons(status: str | None = None, type_: str | None = None,
                            min_n: int = 1, limit: int = 50):
    """教训/规律排行（含置信度、样本量、支持案例、否证次数）。"""
    from app.kb import kb_store
    filters: dict = {}
    if status:
        filters["status"] = status
    if type_:
        filters["type"] = type_
    rows = [l for l in kb_store.query("lesson", filters, limit=300)
            if int((l.get("support") or {}).get("n") or 0) >= int(min_n or 1)]
    rows.sort(key=lambda x: (-float(x.get("confidence") or 0),))
    return {"lessons": rows[:limit], "total": len(rows)}


@router.post("/evolve/kb/lesson/feedback")
def evolve_kb_lesson_feedback(lesson_id: str, status: str = "",
                                    confidence: float | None = None, note: str = ""):
    """人工纠正/废弃教训（错误归因治理：防污染被反复继承放大）。"""
    from app.kb import kb_store
    l = kb_store.get("lesson", lesson_id)
    if not l:
        raise HTTPException(status_code=404, detail=f"教训 {lesson_id} 不存在")
    if status and status not in ("active", "candidate", "weakened", "retired"):
        raise HTTPException(status_code=400, detail="status 非法")
    support = dict(l.get("support") or {})
    if note:
        audit = list(support.get("human_audit") or [])
        audit.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note, "status": status})
        support["human_audit"] = audit[-20:]
    ok = kb_store.upsert("lesson", {
        **l,
        "support": support,
        "status": status or l.get("status"),
        "confidence": float(confidence) if confidence is not None else l.get("confidence"),
    })
    return {"ok": ok, "lesson_id": lesson_id,
            "status": status or l.get("status")}


@router.post("/evolve/kb/backfill")
def evolve_kb_backfill(periods: int = 60, background: bool = True):
    """回填历史知识（verify_kb + monitor_state + 榜单明细 → 案例/教训/环境快照）。"""
    from app.kb import kb_ingest
    if background:
        import threading
        threading.Thread(target=kb_ingest.backfill_from_verify_kb, kwargs={"periods": periods},
                         daemon=True).start()
        return {"ok": True, "message": f"回填已在后台开始（最近 {periods} 期）"}
    return kb_ingest.backfill_from_verify_kb(periods=periods)


@router.post("/evolve/kb/maintain")
def evolve_kb_maintain(warm_vector: bool = False):
    """知识库维护：外部知识挂引用 + 教训衰减/归档 + 向量索引刷新 + context 清理。

    warm_vector=True 时额外加载 embedding 模型预热（约 10s / 1.3GB 常驻显存），
    可让后续分析路径首次调用即时可用；显存紧张时保持默认 False。
    """
    from app.kb import kb_context, kb_store, kb_index
    out = kb_context.distill_and_reindex()
    if warm_vector:
        out["warm_vector"] = kb_index.warm_up(full=True)
    out["pruned_context"] = kb_store.prune_context(400)
    return out


# ── 交易纪律（plans/22）：计划台账 / 触价回放 / 纪律统计 / 反事实网格 ──────────────
@router.get("/evolve/plan/stats")
def evolve_plan_stats(days: int = 60):
    """纪律统计：成交率、含成本收益、纪律 alpha（vs 无脑持有）、失效类型占比、分形态表现。"""
    from app.backtest import plan_tracker
    out = plan_tracker.plan_stats(days=days)
    out["text"] = plan_tracker.build_plan_txt(days=days)
    return out


@router.get("/evolve/plan/list")
def evolve_plan_list(limit: int = 100, form: str | None = None,
                           failure: str | None = None, date: str | None = None):
    """计划台账明细（含回放结果：成交/离场/收益/基准/纪律 alpha/失效类型）。"""
    from app.kb import kb_store, kb_writer
    kb_writer.flush_now()
    rows = kb_store.query("plan", limit=max(1, min(limit, 1000)))
    if form:
        rows = [r for r in rows if str(r.get("form_type")) == form]
    if failure:
        rows = [r for r in rows if str(r.get("failure_type")) == failure]
    if date:
        rows = [r for r in rows if str(r.get("date"))[:8] == str(date)[:8]]
    rows.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("ts_code") or "")), reverse=True)
    return {"count": len(rows), "limit": limit, "rows": rows[:max(1, min(limit, 1000))]}


@router.post("/evolve/plan/replay")
def evolve_plan_replay(force: bool = False, limit: int = 200, background: bool = True):
    """触价回放：用日线 high/low 模拟"严格按计划执行"，产出成交/止损/止盈/到期与纪律 alpha。"""
    from app.backtest import plan_tracker
    if background:
        import threading
        threading.Thread(target=plan_tracker.replay_pending,
                         kwargs={"limit": limit, "force": force}, daemon=True).start()
        return {"ok": True, "message": f"回放已在后台开始（{'全部重放' if force else '仅未回放'}，上限 {limit} 条）"}
    return plan_tracker.replay_pending(limit=limit, force=force)


@router.post("/evolve/plan/backfill")
def evolve_plan_backfill(limit: int = 600, background: bool = True):
    """用历史 EKB 案例重建计划并回放（基准价取该案例推荐日收盘价，非"今天"）→ 立刻有历史纪律样本。"""
    from app.backtest import plan_tracker
    if background:
        import threading
        threading.Thread(target=plan_tracker.backfill_from_cases,
                         kwargs={"limit": limit}, daemon=True).start()
        return {"ok": True, "message": f"计划回填已在后台开始（上限 {limit} 个案例）"}
    return plan_tracker.backfill_from_cases(limit=limit)


@router.post("/evolve/plan/lessons")
def evolve_plan_lessons(days: int = 60):
    """把高频纪律失效沉淀为教训（进 L0 与检索；重复犯同类错误会被否证清单拦）。"""
    from app.backtest import plan_tracker
    return {"ok": True, "lessons": plan_tracker.distill_lessons(days=days)}


@router.get("/evolve/plan/grid")
def evolve_plan_grid():
    """纪律网格（反事实评估）：每形态最优止损/止盈折扣/持有期/追高上限 + 样本内外验证。"""
    from app.backtest import discipline_grid
    g = discipline_grid.load_grid()
    return {"grid": g, "enabled": discipline_grid.grid_enabled(),
            "adopted": {f: discipline_grid.plan_override(f)
                        for f in (g.get("by_form") or {}).keys()},
            "text": discipline_grid.grid_txt()}


@router.post("/evolve/plan/grid/build")
def evolve_plan_grid_build(limit: int = 600, background: bool = True):
    """重建纪律网格（对每只历史样本在 4×3×3×5=180 组纪律下反事实回放，约 10~30s）。"""
    from app.backtest import discipline_grid
    if background:
        import threading
        threading.Thread(target=discipline_grid.build_grid,
                         kwargs={"limit": limit}, daemon=True).start()
        return {"ok": True, "message": f"纪律网格重建已在后台开始（上限 {limit} 个样本）"}
    return discipline_grid.build_grid(limit=limit)
