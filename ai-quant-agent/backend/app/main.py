"""FastAPI 应用入口（v0.139+）"""
import asyncio
import threading
import datetime
import time
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.logger import logger
from app.api import predictions, system, agent, data, ws, backtest, selfproof
from app.backtest import monitor
from app.backtest.daily_scan import run_daily_scan


def _evol_num(name: str, default):
    """读数值型可进化参数（plans/23 §十二：采集时序阈值交给进化大脑统一调优）。"""
    try:
        from app.agents import evolution_config
        return evolution_config.get_num(name, default)
    except Exception:  # noqa: BLE001
        return default


def _evolution_idle_check(tag: str) -> bool:
    """进化中心空闲检查（用户要求：进化中心不在其他功能运行时运行）。

    后台有 回测/每日扫描/Agent精筛/反思/数据采集 任一在跑 → 跳过本次进化任务。
    返回 True=可执行；False=跳过（记日志，等空闲后再执行）。
    """
    try:
        from app.agents import code_writer
        idle = code_writer.is_idle()
    except Exception:  # noqa: BLE001
        return True
    if not idle["idle"]:
        logger.info(f"[evolution] {tag} 跳过：后台任务运行中 {idle['busy']}，等空闲再执行")
        return False
    return True


def _run_learning_job() -> None:
    """每日统一学习任务（§15/§19/§20）：个股反思 + 政策自学 + 消息面反思 + 决策反思（后台线程）。"""
    if not _evolution_idle_check("统一学习"):
        return
    from app.agents.learning_runner import run_all

    def _job():
        try:
            logger.info("[learning] 每日统一学习任务启动")
            res = run_all()
            logger.info(f"[learning] 每日统一学习完成: {res}")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"每日统一学习任务失败: {exc}")

    threading.Thread(target=_job, daemon=True).start()


def _run_policy_kb_job() -> None:
    """每日政策知识库任务（§18/§21）：采集权威信源 → 建 RAG 索引（后台线程）。"""
    from app.data.news_collector import collect
    from app.data import policy_kb

    def _job():
        try:
            logger.info("[policy_kb] 每日采集+建库任务启动")
            from app.data.news_collector import prune_old
            res = collect()
            pruned = prune_old(keep_years=5)   # D：清理超过 5 年的旧政策，控制库体积
            logger.info(f"[policy_kb] 采集完成: 新增{res.get('inserted')} 清理{pruned} 累计{res.get('total')}")
            # 新增/清理/首次时干净重建（clear=True 清空旧残留；文档量小成本可接受）
            stats = policy_kb.stats()
            if res.get("inserted", 0) > 0 or pruned > 0 or stats["chunks_in_memory"] == 0:
                kb = policy_kb.build_index(clear=True)
                logger.info(f"[policy_kb] 建库完成: {kb}")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"政策知识库任务失败: {exc}")

    threading.Thread(target=_job, daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理（替代旧版 on_event）"""
    logger.info(f"{settings.app_name} v{settings.app_version} 启动完成")
    # 待重启标记自愈（BugFix）：进程成功启动 = 磁盘上的 Tier2/3 代码已加载生效，
    # 清除 evolution_restart_pending.json，避免进化中心"一直有待重启代码"。
    # 正常路径由 restart_backend.sh 在成功重启后清除；此处兜底（如手动重启/脚本异常）。
    try:
        from app.agents import self_improver
        if self_improver.pending_restart():
            self_improver.clear_restart_flag()
            logger.info("[evolution] 启动时清除遗留待重启标记（代码已加载生效）")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[evolution] 启动时清除待重启标记跳过: {exc}")
    # 进化事件流：logger Hook 接入（E1 防重入，全项目错误/告警汇聚，供进化 Agent）
    try:
        from app.agents import evolution_events
        evolution_events.attach_logger_hook()
        logger.info("[evolution] 进化事件流日志 Hook 已接入")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[evolution] 事件流 Hook 接入跳过: {exc}")
    # 启动时自动续采未完成的数据采集任务（断点续采）
    # 后台任务运行, 不阻塞服务器启动(采集中的阻塞操作已放入线程池)
    if ws.has_pending_tasks() > 0:
        asyncio.create_task(ws.resume_pending())
        logger.info("已启动后台断点续采任务")

    # 政策知识库预热（后台，避免首次精筛触发慢建库/模型加载）
    def _warm_policy_kb():
        try:
            from app.data import policy_kb
            policy_kb.ensure_index()
            logger.info("[policy_kb] 预热建库完成")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[policy_kb] 预热跳过: {exc}")

    threading.Thread(target=_warm_policy_kb, daemon=True).start()

    # 主事件循环引用：供后台调度线程向主循环提交 async 采集协程（保证 WS 进度广播可用）
    _main_loop = asyncio.get_running_loop()

    # 持续监控定时任务（对齐规划 5.6）：每月 1 日 00:30 月度滚动再验证
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        monitor.run_monitor,
        CronTrigger(day=1, hour=0, minute=30),
        id="backtest_monthly_monitor",
        replace_existing=True,
    )
    # 统一学习层定时任务（§15/§19/§20）：每日 19:10 个股/政策/消息面/决策统一回填+反思
    # （plans/23 §2.5 V5：原 13:35 属盘中时段，与用户看盘/盘中请求争资源；现统一错峰到
    #   17:30 数据采集完成之后的晚间链路，且依赖当日最新数据，晚跑更准）
    # 用户决策：19:00 下班关机 → 学习/进化类一律排中午；当日增量在 18:50（下班前、采集后）补跑。
    # 说明：学习依赖当日采集数据（17:30 才有），故"中午处理历史 + 18:50 处理当日增量"双点执行，
    #       既不因关机丢任务，也不丢当日数据。
    scheduler.add_job(
        _run_learning_job,
        CronTrigger(hour=12, minute=50),
        id="daily_learning",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_learning_job,
        CronTrigger(hour=18, minute=50),
        id="daily_learning_evening",
        replace_existing=True,
    )
    # 政策知识库定时任务（§18/§21）：每日 08:00 采集权威信源 + 建 RAG 索引
    scheduler.add_job(
        _run_policy_kb_job,
        CronTrigger(hour=8, minute=0),
        id="daily_policy_kb",
        replace_existing=True,
    )
    # 晚间补采（O4）：18:00 再次采集，覆盖当日午后/晚间发布（如晚间国常会/部委发文）
    scheduler.add_job(
        _run_policy_kb_job,
        CronTrigger(hour=18, minute=0),
        id="evening_policy_kb",
        replace_existing=True,
    )
    # 进化守护巡检（plans/11 H1/I2）：每日 12:05 —— 宪法哈希/越权/频率/主指标/自身健康，异常自动暂停
    def _guard_scan_job():
        try:
            from app.agents import evolution_guard
            report = evolution_guard.guard_scan()
            if not report["constitution"].get("ok"):
                evolution_guard.pause("宪法完整性异常（非人工确认）")
            elif report["main_metric"].get("pause_suggested"):
                evolution_guard.pause("主指标持续下降")
            logger.info(f"[evolution] 守护巡检完成: 宪法ok={report['constitution'].get('ok')} "
                        f"频率={report['frequency']} 主指标={report['main_metric']}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[evolution] 守护巡检失败: {exc}")

    # V5：12:05（盘中）→ 18:10（晚间链路起点）
    scheduler.add_job(_guard_scan_job, CronTrigger(hour=18, minute=10),
                      id="evolution_guard_scan", replace_existing=True)

    # 次日自我验证（plans/17）：每日 12:10（推后一天，用户要求：不赶当天 17:30，
    # 改到次日中午跑——此时前一日 16:30 采集的数据已完全齐备，验证"前日推荐在最新交易日"的表现）
    # 对推荐做 T+1 自我验证 → 回答"为什么推荐的股票没涨" → 失效归因 →
    # 固化 verify_kb + 写进化事件，供进化大脑做每日推荐/回测逻辑进化（快反馈，替代 T+5 慢反馈）
    def _daily_verify_job():
        if not _evolution_idle_check("次日自我验证"):
            return
        try:
            from app.agents import daily_verify, evolution_guard
            if evolution_guard.is_paused():
                logger.info("[evolution] 次日验证跳过（进化已暂停）")
                return
            # 效果回滚门（plans/19）：连续命中率下降的进化补丁自动回滚（恢复 .bak + 待重启生效）
            try:
                from app.agents import code_writer as _cw
                _reg = _cw.check_regression()
                if _reg.get("rolled_back"):
                    logger.warning(f"[code_writer] 效果回滚门: {_reg['message']} → "
                                   f"{[_r.get('target') for _r in _reg['rolled_back']]}")
                else:
                    logger.info(f"[code_writer] 效果回滚门: {_reg['message']}")
            except Exception as _e:  # noqa: BLE001
                logger.warning(f"[code_writer] 效果回滚门检查失败: {_e}")
            res = daily_verify.run_daily_verify()
            logger.info(f"[evolution] 次日自我验证: {res}")
            # 交易纪律闭环（plans/22 P1）：结算待回放计划（T+1/T+5 触价）→ 纪律统计 → 高频失效沉淀为教训。
            # 与次日验证同跑：此时"前一日推荐 + 最新交易日行情"已齐备，回放口径与验证口径一致。
            try:
                from app.backtest import plan_tracker
                _rp = plan_tracker.replay_pending(limit=200)
                _ls = plan_tracker.distill_lessons(days=60)
                logger.info(f"[discipline] 计划触价回放 {_rp}；纪律教训沉淀 {_ls} 条")
            except Exception as _e:  # noqa: BLE001
                logger.warning(f"[discipline] 纪律闭环失败（不影响主流程）: {_e}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[evolution] 次日自我验证失败: {exc}")

    # V5：12:10（盘中）→ 18:20（此时 17:30 采集的当日数据已齐备，验证口径与数据口径一致）
    scheduler.add_job(_daily_verify_job, CronTrigger(hour=18, minute=20),
                      id="daily_self_verify", replace_existing=True)

    # 回测自动验证（plans/19 优化）：进化大脑改回测逻辑后写"待回测验证"标记，
    # 由 18:30（16:30 采集+扫描完成后、收盘后）独立触发，避免 12:10 触发回测跨交易时段。
    def _backtest_verify_job():
        if not _evolution_idle_check("待回测验证"):
            return
        try:
            from app.agents import backtest_ctl
            from pathlib import Path as _Path
            _flag = _Path(__file__).resolve().parents[1] / "data" / "pending_backtest_verify.json"
            if not _flag.exists():
                return
            if backtest_ctl.is_running():
                logger.info("[backtest_ctl] 待回测验证：回测已在运行，保留标记下次再验")
                return
            r = backtest_ctl.trigger_backtest(source="evolution_verify", force=True)
            logger.info(f"[backtest_ctl] 自动重跑回测（验证进化改动）: {r}")
            try:
                _flag.unlink()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[backtest_ctl] 待回测验证处理失败: {exc}")

    # V5：18:30 → 20:00（排在 19:40 代码进化之后，避免回测与"改了代码待验证"互相等待）
    # 依赖当日收盘数据（收盘后才有）：排在 18:45（下班前），避免 20:00 时机器已关机
    scheduler.add_job(_backtest_verify_job, CronTrigger(hour=18, minute=45),
                      id="backtest_auto_verify", replace_existing=True)

    # 影子实验结算（plans/11 F1）：每日 12:20 —— 裁决到期影子（生效/回滚/样本不足继续）
    def _shadow_settle_job():
        if not _evolution_idle_check("影子实验结算"):
            return
        try:
            from app.agents import evolution_shadow
            for param in list(evolution_shadow.shadow_status().keys()):
                res = evolution_shadow.settle_shadow(param)
                logger.info(f"[evolution] 影子结算 {param}: {res}")
            # 效果追踪（F7/§15.4）：对 improve_effect 中 tracking 的改进刷新当前命中率对比
            try:
                from app.agents import evolution_shadow as _sh
                eff = _sh._read_json(_sh.EFFECT_FILE, {"records": []})
                tracked = {r.get("param") for r in eff.get("records", []) if r.get("status") == "tracking"}
                for param in tracked:
                    _sh.track_effect(param)
                if tracked:
                    logger.info(f"[evolution] 效果追踪刷新: {sorted(tracked)}")
            except Exception as exc2:  # noqa: BLE001
                logger.warning(f"[evolution] 效果追踪刷新失败: {exc2}")
            # Bug20：终身记账 G4 定期更新 —— 刷新所有 active 记账的当前效果
            try:
                from app.agents import evolution_ledger
                report = evolution_ledger.ledger_report()
                for e in report.get("active", []):
                    evolution_ledger.update_entry(e.get("param"))
                if report.get("active"):
                    logger.info(f"[evolution] 终身记账刷新: {[e.get('param') for e in report.get('active')]}")
            except Exception as exc3:  # noqa: BLE001
                logger.warning(f"[evolution] 终身记账刷新失败: {exc3}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[evolution] 影子结算失败: {exc}")

    # V5：12:20（盘中）→ 18:30
    scheduler.add_job(_shadow_settle_job, CronTrigger(hour=18, minute=30),
                      id="evolution_shadow_settle", replace_existing=True)

    # 惰性评估低频保底（plans/11 H4）：每 3 日评估一次（事件驱动已由 learning_runner I6 触发，
    # 此 job 保底，避免稳定期无事件时长期不评估）
    def _lazy_evaluate_job():
        if not _evolution_idle_check("惰性评估"):
            return
        try:
            from app.agents import evolution_agent, evolution_guard
            if evolution_guard.is_paused():
                logger.info("[evolution] 惰性评估跳过（进化已暂停）")
                return
            res = evolution_agent.evaluate()
            logger.info(f"[evolution] 惰性评估: 提案 {len(res.get('proposals', []))}，拒绝 {res.get('rejected', 0)}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[evolution] 惰性评估失败: {exc}")

    # V5：每 3 日 12:35（盘中）→ 18:40
    # 不依赖当日数据 → 中午（用户 19:00 后关机）
    scheduler.add_job(_lazy_evaluate_job, CronTrigger(day="*/3", hour=12, minute=40),
                      id="evolution_lazy_evaluate", replace_existing=True)

    # 生成式代码进化（plans/18）：每日 12:50（午间休市/下午开盘初，通常无回测/推荐/采集在跑），
    # 仅在系统完全空闲（is_idle）且 code_evolution 开启时执行——进化大脑"自己写代码"，
    # 对接回测/每日推荐逻辑，目标"高概率次日大涨 + 后续主升浪"。
    def _code_evolution_job():
        try:
            from app.agents import (code_writer, evolution_config, evolution_guard,
                                    evolution_summarizer, self_improver)
            ce = evolution_config.get("code_evolution", {}) or {}
            if not ce.get("enabled", False):
                logger.info("[code_writer] 生成式代码进化未开启（code_evolution.enabled=false），跳过")
                return
            if evolution_guard.is_paused():
                logger.info("[code_writer] 生成式代码进化跳过（进化已暂停）")
                return
            idle = code_writer.is_idle()
            if not idle["idle"]:
                logger.info(f"[code_writer] 系统忙（{idle['busy']}），跳过生成式代码进化（等闲时）")
                return
            # 每日补丁上限（max_code_patches_per_day）：当天已应用的生成式代码补丁达到上限则跳过
            try:
                _max_pd = int(ce.get("max_code_patches_per_day", 2) or 2)
            except (TypeError, ValueError):  # noqa: BLE001
                _max_pd = 2
            if _max_pd > 0:
                try:
                    _st = code_writer._read_state()
                    _today = time.strftime("%Y-%m-%d")
                    _done = sum(1 for h in _st.get("history", [])
                                if h.get("status") == "applied" and str(h.get("ts", "")).startswith(_today)
                                and h.get("file"))
                    if _done >= _max_pd:
                        logger.info(f"[code_writer] 今日生成式代码补丁已达上限 {_max_pd}，跳过（已应用 {_done}）")
                        return
                except Exception as _e:  # noqa: BLE001
                    logger.debug(f"[code_writer] 每日补丁上限检查跳过: {_e}")
            mode = ce.get("approval", "manual") or "manual"
            summary = evolution_summarizer.build_l0_text()

            applied: list[str] = []
            backtest_affected = False
            # 全自动闭环（plans/18）：auto 模式先自动处理遗留 pending 提案（上次未生效的进化补丁）
            if mode == "auto":
                for p in code_writer.pending_patches():
                    pid = int(p.get("id") or 0)
                    if not pid:
                        continue
                    r = code_writer.apply_pending(pid)
                    if r.get("ok"):
                        applied.append(str(r.get("target") or p.get("file") or f"patch#{pid}"))
                        if r.get("backtest_affected"):
                            backtest_affected = True
                        logger.info(f"[code_writer] 遗留 pending #{pid} 已自动应用: {r.get('target')}")
                    else:
                        logger.warning(f"[code_writer] 遗留 pending #{pid} 应用失败: {r.get('message')}")

            # 本轮新生成提案（auto 直接应用 / manual 存 pending 待确认）
            res = code_writer.propose_code_evolution(summary=summary, mode=mode)
            logger.info(f"[code_writer] 闲时生成式代码进化({mode}): {res}")
            if res.get("ok") and res.get("target"):
                applied.append(str(res["target"]))
            if res.get("backtest_affected"):
                backtest_affected = True

            # 代码补丁已落盘但需独立进程重启才生效 → 自动触发重启（进化 Agent 不 kill 自己，
            # 由 restart_backend.sh 在避让窗口执行 + 健康检查 + 失败回滚 .bak）
            if applied and self_improver.pending_restart():
                rr = self_improver.evolution_apply()
                logger.info(f"[code_writer] 全自动生效：触发独立进程重启 {rr}")
            elif applied:
                logger.info(f"[code_writer] 已应用 {len(applied)} 个补丁，无待重启标记（热生效/参数级改动）")

            # plans/19：改了回测逻辑 → 写"待回测验证"标记，重启后由次日验证任务自动重跑回测验证
            if backtest_affected:
                try:
                    import json as _json
                    import time as _time
                    from pathlib import Path as _Path
                    _flag = _Path(__file__).resolve().parents[1] / "data" / "pending_backtest_verify.json"
                    _flag.parent.mkdir(parents=True, exist_ok=True)
                    _flag.write_text(_json.dumps({
                        "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
                        "files": applied[:5], "reason": "回测逻辑被进化大脑修改，需重跑回测验证"},
                        ensure_ascii=False, indent=2), encoding="utf-8")
                    logger.info("[code_writer] 已标记待回测验证（重启后自动重跑回测验证改动效果）")
                except Exception as _e:  # noqa: BLE001
                    logger.warning(f"[code_writer] 待回测验证标记写入失败: {_e}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[code_writer] 闲时生成式代码进化失败: {exc}")

    # V5：12:50（盘中）→ 19:40 → **13:30（用户 19:00 关机；午间第二次机会，
    # 若 12:05 因系统忙/时段判断跳过时兜底，仍在 DeepSeek 空闲窗口内）**
    scheduler.add_job(_code_evolution_job, CronTrigger(hour=13, minute=30),
                      id="code_evolution_idle", replace_existing=True)

    # 报错监控 → 进化大脑修复 bug（用户新需求）：每日多次检查近期后台接口报错，
    # 有可修 bug 则自动生成修复补丁（走 apply_code_patch 完整安全链：签名校验/备份/编译/回滚门）。
    # 12:45/16:45/20:45 三个非交易密集时段（收盘后/午间）检查，避免交易时段打扰。
    def _bugfix_monitor_job():
        try:
            from app.agents import code_writer, evolution_guard, evolution_config, self_improver
            if evolution_guard.is_paused():
                logger.info("[code_writer] 报错监控跳过（进化已暂停）")
                return
            ce = evolution_config.get("code_evolution", {}) or {}
            if not ce.get("enabled", False):
                logger.info("[code_writer] 报错监控跳过（code_evolution.enabled=false）")
                return
            idle = code_writer.is_idle()
            if not idle["idle"]:
                logger.info(f"[code_writer] 报错监控跳过（系统忙 {idle['busy']}）")
                return
            res = code_writer.propose_bug_fix(hours=24, mode="auto", max_errors=15)
            logger.info(f"[code_writer] 报错监控: {res.get('action')} — {res.get('message')}")
            # 修复了可改代码且待重启 → 触发独立进程重启生效
            if res.get("ok") and res.get("action") == "apply_patch" and self_improver.pending_restart():
                rr = self_improver.evolution_apply()
                logger.info(f"[code_writer] bug 修复补丁已应用，触发独立进程重启 {rr}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[code_writer] 报错监控失败: {exc}")

    # V5：12:45/16:45 → 19:20/20:45 → **12:20/13:50（都在午间空闲窗口，用户 19:00 关机）**
    scheduler.add_job(_bugfix_monitor_job, CronTrigger(hour=12, minute=20),
                      id="bugfix_monitor_noon", replace_existing=True)
    scheduler.add_job(_bugfix_monitor_job, CronTrigger(hour=13, minute=50),
                      id="bugfix_monitor_night", replace_existing=True)

    # ── 待重启兜底守护（用户决策：不要人工重启，改动自己生效）──────────
    # 每 10 分钟检查：有"待重启"改动 + 非交易窗口 + 未暂停 → 自动触发独立进程重启
    # （restart_backend.sh：避让窗口 + 健康检查 + 失败回滚 .bak）。
    # 交易窗口内自动跳过，等 12:00-13:00 或 15:00 后的第一个安全窗口自行生效。
    def _restart_guard_job():
        try:
            d = self_improver.auto_restart_decision()
            if d.get("should_restart"):
                r = self_improver.restart_now(reason="restart_guard")
                logger.info(f"[restart_guard] 自动重启：{r.get('action')}（{d.get('reason')}）")
            elif d.get("pending"):
                logger.info(f"[restart_guard] 暂不重启：{d.get('reason')}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[restart_guard] 检查失败: {exc}")

    scheduler.add_job(_restart_guard_job, CronTrigger(minute="*/10"),
                      id="restart_guard", replace_existing=True)

    # ── 午间自动进化（用户决策：每天中午 DeepSeek 闲时自行进化并自动生效）──────
    # 12:05 落在 DeepSeek 空闲时段（工作日 12:00-14:00 半价，直接省 50%）且非交易窗口，
    # 既不打断盘中推荐，也不需要人工重启：改完由 _restart_guard_job 兜底自动生效。
    def _noon_evolution_job():
        try:
            from app.agents import evolution_config as _ec
            ce = _ec.get("code_evolution", {}) or {}
            if not ce.get("enabled", False):
                logger.info("[noon] 午间自动进化跳过（code_evolution.enabled=false）")
                return
            if ce.get("require_off_peak", True) and self_improver.llm_period() != "off_peak":
                logger.info(f"[noon] 午间自动进化跳过（当前 {self_improver.llm_period()}，"
                            f"等空闲时段执行以省 50% 成本）")
                return
            if self_improver.in_trade_window():
                logger.info("[noon] 午间自动进化跳过（交易窗口内）")
                return
            logger.info("[noon] 午间自动进化开始（DeepSeek 空闲时段，免人工）")
            _code_evolution_job()
            _restart_guard_job()          # 兜底：若仍有待重启标记则立即生效
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[noon] 午间自动进化失败: {exc}")

    scheduler.add_job(_noon_evolution_job,
                      CronTrigger(day_of_week="mon-fri", hour=12, minute=5),
                      id="code_evolution_noon", replace_existing=True)

    # 每周日批次应用（plans/11 D13）：候选池待确认提案在 shadow_eligible 时统一进入影子期
    def _weekly_apply_job():
        if not _evolution_idle_check("每周批次应用"):
            return
        try:
            from app.agents import evolution_agent, evolution_shadow, evolution_config, evolution_guard
            if evolution_guard.is_paused():
                logger.info("[evolution] 批次应用跳过（进化已暂停）")
                return
            applied = 0
            skipped = 0
            for p in evolution_agent.pending_proposals():
                params = evolution_config.params_table()
                meta = params.get(p.get("param"), {})
                if meta.get("shadow_eligible") and meta.get("hot_reload"):
                    # Bug35：批次应用也尊重 A4/B3 冷却（避免绕过 evaluate 的冷却控制）
                    if not evolution_agent._auto_allowed(p["param"]):
                        skipped += 1
                        continue
                    res = evolution_agent.confirm_proposal(p["id"])
                    # Bug29：仅确认成功（进入影子/应用）才计数；并行满/冻结拒绝不计
                    if res.get("ok"):
                        applied += 1
            logger.info(f"[evolution] 每周批次应用: 进入影子 {applied} 个提案（冷却跳过 {skipped}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[evolution] 每周批次应用失败: {exc}")

    # V5：周日 13:20 → 19:50（与晚间链路一致，避免任何"盘中时段"任务；周日虽非交易日，
    # 但统一口径后可用静态检查守住"无任务落在 9:30-11:30 / 13:00-15:00"）
    # 周批次 → 周日中午（用户 19:00 关机）
    scheduler.add_job(_weekly_apply_job, CronTrigger(day_of_week="sun", hour=12, minute=35),
                      id="evolution_weekly_apply", replace_existing=True)

    # 审批超时清理（plans/11 I5）：待审批提案超过 N 天未处理 → 默认拒绝（从候选池清除）
    def _pending_cleanup_job():
        if not _evolution_idle_check("审批超时清理"):
            return
        try:
            from app.agents import evolution_agent, evolution_config
            timeout_days = int(evolution_config.get("approval_timeout_days", 7) or 7)
            records = evolution_agent.decisions()
            import time as _t
            from datetime import datetime
            changed = 0
            for r in records:
                if r.get("status") != "pending":
                    continue
                try:
                    ts = datetime.strptime(r.get("ts", ""), "%Y-%m-%d %H:%M:%S")
                except Exception:  # noqa: BLE001
                    continue
                if (_t.time() - ts.timestamp()) > timeout_days * 86400:
                    r["status"] = "rejected_timeout"
                    r["reject_reason"] = f"审批超时 {timeout_days} 天未处理（I5）"
                    changed += 1
            if changed:
                evolution_agent._save_decisions(records)
                logger.info(f"[evolution] 审批超时清理: {changed} 个提案默认拒绝")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[evolution] 审批超时清理失败: {exc}")

    # V5：13:05（盘中）→ 18:50
    scheduler.add_job(_pending_cleanup_job, CronTrigger(hour=12, minute=55),
                      id="evolution_pending_cleanup", replace_existing=True)

    # 私有域知识库维护（plans/21 §六/§5.4）：教训时效衰减 + 向量索引刷新 + context 滚动清理
    # 低频（每日 13:15）执行：稳定期几乎无写入，成本可忽略；保证检索层始终可用。
    def _kb_maintain_job():
        if not _evolution_idle_check("知识库维护"):
            return
        try:
            from app.kb import kb_context, kb_store
            out = kb_context.distill_and_reindex()
            out["pruned_context"] = kb_store.prune_context(400)
            logger.info(f"[kb] 知识库维护完成: 衰减 {out.get('decayed')} 条教训，"
                        f"向量索引 {out.get('indexed')} 条，清理快照 {out.get('pruned_context')} 条")
            # 纪律网格（plans/22 P2）：历史样本反事实择优 → discipline_grid.json（供 trade_plan 采纳）。
            # 低频刷新（缺表或超 3 天）：重算需加载历史日线，约 10~30s，不适合每日热跑。
            try:
                import os as _os
                import time as _t
                from app.backtest import discipline_grid as _dg
                _need = (not _os.path.exists(_dg.GRID_FILE)) or (
                    _t.time() - _os.path.getmtime(_dg.GRID_FILE) > 3 * 86400)
                if _need:
                    _g = _dg.build_grid(limit=600)
                    _gl = _g.get("global") or {}
                    logger.info(f"[discipline] 纪律网格刷新: 样本 {_g.get('samples')}，形态 "
                                f"{len(_g.get('by_form') or {})}，全局期望 {_gl.get('utility')}"
                                f"（改进 {_gl.get('improvement')}）")
            except Exception as _e2:  # noqa: BLE001
                logger.warning(f"[discipline] 纪律网格刷新失败（不影响主流程）: {_e2}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[kb] 知识库维护失败（不影响主流程）: {exc}")

    # V5：13:15（盘中）→ 19:00
    scheduler.add_job(_kb_maintain_job, CronTrigger(hour=12, minute=45),
                      id="kb_maintain", replace_existing=True)

    # ── 每日采集+推荐（plans/23 §2.5 V1/V2/V5 修复）──────────────────────────
    # 修复前：固定在 16:30 触发。A 股 15:00 收盘后 Tushare 日频数据（尤其 daily_basic /
    # moneyflow）常在 17:00 后才就绪 → 16:30 采集"采了个空"（实测 93 秒即"完成"，日线
    # 仍停在前一天），紧接着幂等守卫判定"该数据日期已存在推荐结果"→ 自动推荐被跳过。
    # 结果：自动链路连续多日空转（09-17 16:30 与 09-18 09:15/09:21/09:55/11:00 共 5 次 skipped），
    # 只能靠人工 POST /scan?force=true 补跑。
    # 修复后：① 主任务移到 17:30；② 采集后校验"日线是否推进到应有交易日"；
    #        ③ 未就绪则 30 分钟后自动重试（最多 3 次）；④ 21:00 兜底再跑一次；
    #        ⑤ 其余定时任务全部错峰到 17:30 之后（不再占用盘中时段）。
    _COLLECT_RETRY = {
        "count": 0,
        # 可进化（plans/23 §十二）：重试次数/间隔由进化大脑调优（默认 3 次 × 30 分钟）
        "max": int(_evol_num("collect_retry_max", 3)),
        "delay_min": int(_evol_num("collect_retry_delay_min", 30)),
    }

    def _expected_data_date() -> str:
        """应有数据的最近交易日（YYYYMMDD）。

        规则：trade_cal 中 <= today 的最后一个开市日；若 today 本身是开市日且当前
        时间早于 17:10（Tushare 日频通常 17:00 后才齐备），则视为前一开市日。
        """
        try:
            from app.models import SessionLocal
            from sqlalchemy import text as _text
            db = SessionLocal()
            try:
                rows = db.execute(
                    _text("SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date<=:t "
                          "ORDER BY cal_date DESC LIMIT 2"),
                    {"t": time.strftime("%Y%m%d")},
                ).fetchall()
            finally:
                db.close()
            dates = [str(r[0]) for r in rows]
            if not dates:
                return ""
            if dates[0] == time.strftime("%Y%m%d") and time.strftime("%H%M") < "1710" and len(dates) > 1:
                return dates[1]
            return dates[0]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[data] 计算应有数据日期失败（忽略就绪校验）: {exc}")
            return ""

    def _schedule_collect_retry(retry: int, why: str) -> None:
        """数据未就绪时安排下一次采集重试（指数退避由 retry 次数控制）。"""
        if retry >= _COLLECT_RETRY["max"]:
            logger.error(
                f"[data] 采集重试已达上限（{_COLLECT_RETRY['max']} 次）放弃本次：{why}；"
                f"等待 21:00 兜底任务"
            )
            try:
                from app.agents import evolution_events
                evolution_events.record_event(
                    "ERROR", "data", f"每日采集重试耗尽（{why}），当日自动推荐可能缺失，请人工检查")
            except Exception:  # noqa: BLE001
                pass
            return
        delay = _COLLECT_RETRY["delay_min"]
        logger.info(f"[data] {why} → {delay} 分钟后重试采集（第 {retry + 1}/{_COLLECT_RETRY['max']} 次）")
        try:
            scheduler.add_job(
                lambda: _run_daily_collection_job(retry + 1),
                trigger="date",
                run_date=datetime.datetime.now() + datetime.timedelta(minutes=delay),
                id=f"daily_collection_retry_{retry + 1}",
                replace_existing=True,
                misfire_grace_time=600,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[data] 安排采集重试失败: {exc}")

    def _run_daily_collection_job(retry: int = 0) -> None:
        """每日定时数据采集+推荐：将 run_full_collection 提交到主事件循环执行，
        内部已按阶段1→4 依次运行，并含跨进程采集锁防重入；在主循环运行可正常广播WS进度。
        采集完成后（fut.result() 阻塞等待）继续运行每日推荐扫描。

        用户要求：定时任务在前端要有反应（按钮/日志/"执行中"状态），不能静默执行——
        统一走 predictions.start_scan_task（更新 _SCAN_STATE + 捕获日志 + Agent 精筛 + 落盘），
        前端 GET /scan/status 轮询即可看到定时扫描的实时进度与日志。

        用户要求：定时拉取数据/每日推荐扫描须在后台无其它任务时才触发——
        若正在回测/采集/每日扫描/Agent精筛/反思等任一后台任务运行中，则跳过本次定时触发。
        """
        # 空闲检查：后台忙（回测/采集/每日扫描/Agent精筛/反思）则不触发本次定时采集+扫描
        try:
            from app.agents import code_writer
            _idle = code_writer.is_idle()
        except Exception:  # noqa: BLE001
            _idle = {"idle": True, "busy": []}
        if not _idle["idle"]:
            logger.info(f"[data] 定时采集+每日推荐跳过：后台任务运行中 {_idle['busy']}，本次不触发")
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(ws.run_full_collection(), _main_loop)
            logger.info(f"[data] 已触发每日数据采集(阶段1→4){'（重试 %d）' % retry if retry else ''}")
            fut.result()  # 等待采集完成（主循环内运行, job线程阻塞等待）
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"每日数据采集调度失败: {exc}")
            _schedule_collect_retry(retry, "采集异常")
            return

        # ── 数据就绪校验（V1 核心）：日线必须推进到应有交易日，否则重试 ──
        latest_s = ""
        try:
            from app.backtest.loader import get_latest_daily_date
            _latest = get_latest_daily_date()
            latest_s = _latest.strftime("%Y%m%d") if _latest is not None else ""
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[data] 读取最新日线日期失败: {exc}")
        expected = _expected_data_date()
        if expected and latest_s and latest_s < expected:
            logger.warning(f"[data] 数据未就绪：日线最新 {latest_s} < 应有 {expected}（Tushare 尚未更新）")
            try:
                from app.agents import evolution_events
                evolution_events.record_event(
                    "WARNING", "data",
                    f"采集完成但数据未就绪（日线 {latest_s} < 应有 {expected}），将自动重试")
            except Exception:  # noqa: BLE001
                pass
            _schedule_collect_retry(retry, f"数据未就绪({latest_s}<{expected})")
            return

        logger.info(
            f"[data] 数据采集完成（日线 {latest_s or '未知'}）→ 按 auto_scan_after_collect 模式决定"
            f"『自动扫描 / 弹窗询问 / 不扫』（默认 confirm：前端弹窗让你决定）"
        )
        try:
            # 用户要求（2026-09-20）：不再"每次都自动跑"，默认只标记"待确认"由前端弹窗询问。
            from app.api.predictions import handle_after_collection
            res = handle_after_collection(source="schedule", data_date=latest_s)
            logger.info(f"[data] 采集后处理（定时来源）: {res.get('status')} — {res.get('message')}")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"采集后处理失败: {exc}")

    scheduler.add_job(
        _run_daily_collection_job,
        CronTrigger(hour=17, minute=30),
        id="daily_data_collection",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 21:00 兜底：若当日自动链路因数据未就绪/后台忙碌而未产出推荐，再跑一次
    scheduler.add_job(
        _run_daily_collection_job,
        CronTrigger(hour=21, minute=0),
        id="daily_data_collection_night",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.start()
    logger.info("已启动回测月度监控 + 每日推荐扫描 + 统一学习层 + 政策知识库 + 进化Agent + 每日数据采集定时任务")
    yield
    scheduler.shutdown(wait=False)
    # Bug21：进程关闭前 flush 异步事件队列（防未落库事件丢失）
    try:
        from app.agents import evolution_events
        evolution_events.flush_now()
    except Exception:  # noqa: BLE001
        pass
    # plans/21：关闭前 flush 知识库写入队列（否则队列中未落库的案例/教训会随进程退出丢失）
    try:
        from app.kb import kb_writer
        kb_writer.flush_now(timeout=3.0)
    except Exception:  # noqa: BLE001
        pass
    logger.info("应用关闭")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(predictions.router)
app.include_router(system.router)
app.include_router(agent.router)
app.include_router(data.router)
app.include_router(ws.router)
app.include_router(backtest.router)
app.include_router(selfproof.router)   # 自证测试（plans/25）
