"""回测引擎 API

对齐规划：plans/04-回测引擎.md 十、后端 API 规划
  - POST /api/v1/backtest/run         触发回测（离线/在线）
  - GET  /api/v1/backtest/report      回测报告（绩效+归因+条件概率表+样本统计）
  - GET  /api/v1/backtest/status      回测任务状态
  - GET  /api/v1/backtest/segments    查询行情段（成功/失败可过滤）
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.logger import logger
from app.backtest.runner import run_backtest, BacktestConfig, BacktestResult
from app.backtest import monitor

router = APIRouter(prefix="/api/v1/backtest", tags=["回测引擎"])

# 回测结果落盘目录（本地持久化，进程重启不丢失）
RESULT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "backtest_results",
)
LATEST_FILE = os.path.join(RESULT_DIR, "backtest_latest.json")

# 回测任务状态（单例简单实现，可扩展为持久化队列）
_STATE = {
    "status": "idle",          # idle / running / success / failed
    "started_at": "",
    "finished_at": "",
    "message": "",
    "progress": 0.0,
    "result": None,            # BacktestResult.to_dict()
    "logs": [],                # 回测详细日志（实时，前端展示后端在做什么）
}


class _ListHandler(logging.Handler):
    """捕获回测期间的 quant 日志到 _STATE['logs']（前端实时展示）。"""

    def __init__(self, target: list):
        super().__init__()
        self.target = target

    def emit(self, record):
        try:
            msg = record.getMessage()
            if len(self.target) >= 500:
                self.target.pop(0)
            self.target.append(f"{time.strftime('%H:%M:%S')} [{record.levelname}] {msg}")
        except Exception:  # noqa: BLE001
            pass


def _result_body(d: dict) -> dict:
    """兼容两种结构：扁平结构（BacktestResult.to_dict()）与包了一层 "result" 的旧结构。"""
    if isinstance(d, dict) and isinstance(d.get("result"), dict):
        return d["result"]
    return d if isinstance(d, dict) else {}


def _save_result(result_dict: dict) -> str:
    """回测结果落盘（时间戳版本 + latest 覆盖），带**指标质量门**（plans/23 §2.4 T1b）。

    质量门（performance.quality_gate，由 metrics.validate_metrics 产出）：
      - "error"：指标口径失真（cum_return 天文数字 / 平均 T+5>100% / 样本为空 …）
        → 只写"时间戳版本"供排查，**拒绝覆盖 backtest_latest.json**，
          防止失真指标被下游（进化主指标 / 前端面板 / 推荐 Gate）当作事实消费。
      - "warn" / "ok"：正常覆盖 latest，并把 quality_gate 提升为顶层字段便于读取。

    返回实际写入的主文件路径（被护栏拦住时 latest 保持不变）。
    """
    body = _result_body(result_dict)
    perf = body.get("performance") or {}
    gate = str(perf.get("quality_gate") or "unknown")
    issues = perf.get("issues") or []
    blocked = gate == "error"

    os.makedirs(RESULT_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(RESULT_DIR, f"backtest_{ts}.json")
    if isinstance(result_dict, dict):
        result_dict["quality_gate"] = gate
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result_dict, f, ensure_ascii=False, default=str)

    if blocked:
        logger.error(
            f"回测指标质量门未通过（{gate}），拒绝覆盖 backtest_latest.json；"
            f"本次仅保留时间戳版本 {os.path.basename(path)}；"
            f"问题: {[i.get('code') for i in issues]}"
        )
        try:
            from app.agents import evolution_events
            evolution_events.record_event(
                "ERROR", "backtest",
                f"回测质量门未通过（{gate}）：{[i.get('code') for i in issues]}；latest 未被覆盖",
            )
        except Exception:  # noqa: BLE001
            pass
    else:
        with open(LATEST_FILE, "w", encoding="utf-8") as f:
            json.dump(result_dict, f, ensure_ascii=False, default=str)
        logger.info(f"回测结果已落盘: {path}（质量门 {gate}）")

    # 私有域知识库（plans/21 §七）：回测结果 → 实验台账 + 口径指纹（防"改口径让指标变好看"）
    try:
        from app.kb import kb_ingest
        kb_ingest.ingest_backtest(
            body, source="api_backtest",
            period=result_dict.get("config") or body.get("config") or {})
    except Exception:  # noqa: BLE001
        pass  # 知识库入库失败不阻塞回测主流程
    # 进化事件流（plans/11 §15.2）：回测摘要事件（复用落盘 + 1 行钩子）
    try:
        from app.agents import evolution_events
        ab = body.get("ab_validation", {}) or {}
        evolution_events.record_event(
            "INFO", "backtest",
            f"回测 {ts}: 样本 {perf.get('total_trades', '?')}, "
            f"口径 {perf.get('scope', '?')}, 质量门 {gate}, "
            f"A/B 命中差 {ab.get('accuracy_lift', '?')}",
        )
    except Exception:  # noqa: BLE001
        pass  # 事件上报失败不阻塞主流程
    return path


class BacktestRunRequest(BaseModel):
    start_date: str = Field(default="20100101", description="数据起始日期 YYYYMMDD")
    end_date: str = Field(default="", description="数据截止日期（空=最新）")
    max_stocks: int | None = Field(default=None, ge=1, description="限制股票数（调试用）")


def _run_worker(cfg: BacktestConfig) -> None:
    """后台线程执行回测。"""
    try:
        _STATE["status"] = "running"
        _STATE["message"] = "回测进行中"
        _STATE["progress"] = 0.0
        _STATE["logs"] = []

        def _progress(stage: str, pct: float):
            _STATE["progress"] = round(pct, 3)
            _STATE["message"] = f"{stage}"

        # 附加日志捕获 handler：把回测期间所有模块的详细日志收集到 _STATE['logs']
        handler = _ListHandler(_STATE["logs"])
        logger.addHandler(handler)
        try:
            result = run_backtest(cfg, progress_cb=_progress)
        finally:
            logger.removeHandler(handler)
        _STATE["result"] = result.to_dict()
        # 回测结果落盘（本地持久化）
        try:
            _save_result(_STATE["result"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"回测结果落盘失败: {exc}")
        # 持续监控：回测完成后自动记录基线（预测命中率 + A/B 决策门）
        try:
            monitor.record_baseline(_STATE["result"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"记录监控基线失败: {exc}")
        _STATE["status"] = "success"
        _STATE["message"] = f"回测完成，耗时 {result.elapsed:.1f}s"
        _STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _STATE["progress"] = 1.0
    except Exception as exc:  # noqa: BLE001
        _STATE["status"] = "failed"
        _STATE["message"] = f"回测失败: {exc}"
        _STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        logger.exception("回测失败")


def _system_busy_for_backtest() -> dict | None:
    """回测前置忙检查（BugFix：回测与每日扫描并发争抢 MySQL 导致互相拖慢到近 2 小时）。

    回测的 scan_market 与 daily_scan 都会全市场逐股拉数/打 MySQL，二者之间没有互斥锁，
    并发时各自被拖到极慢，前端看起来像"卡住"。触发回测前检查：
      每日扫描(daily_scan) / Agent精筛(agent_refine) / 反思(reflection) / 数据采集(collection)
    任一在跑 → 拒绝本次触发并提示稍后，避免并发。
    Returns: {"status","message"} 拒绝原因；None=系统空闲可触发
    """
    try:
        from app.agents import code_writer
        idle = code_writer.is_idle()
        if not idle["idle"]:
            busy = idle.get("busy", [])
            # backtest 自身 running 不在此判断（外层已有 _STATE 检查）；只拦会争抢 MySQL 的任务
            blockers = [b for b in busy if b in ("daily_scan", "agent_refine", "reflection", "collection")]
            if blockers:
                return {"status": "busy",
                        "message": f"系统忙（{'/'.join(blockers)} 运行中），回测会与其并发争抢数据库被拖慢，请稍后重试"}
    except Exception:  # noqa: BLE001
        pass
    return None


@router.post("/run")
async def trigger_backtest(req: BacktestRunRequest):
    """触发回测任务（后台执行）。"""
    if _STATE["status"] == "running":
        return {"status": "running", "message": "已有回测任务在执行，请等待完成"}
    # BugFix：避免回测与每日扫描/Agent精筛/采集并发（近 2 小时慢到像卡住）
    busy = _system_busy_for_backtest()
    if busy:
        return busy

    cfg = BacktestConfig(
        start_date=req.start_date,
        end_date=req.end_date or "",
        max_stocks=req.max_stocks,
    )
    _STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _STATE["finished_at"] = ""
    _STATE["status"] = "pending"
    _STATE["progress"] = 0.0
    _STATE["message"] = "回测任务已提交"

    t = threading.Thread(target=_run_worker, args=(cfg,), daemon=True)
    t.start()
    return {"status": "pending", "message": "回测任务已提交，请轮询 /status"}


@router.get("/status")
async def backtest_status():
    """回测任务状态 + 详细日志（后端在做什么）。"""
    return {
        "status": _STATE["status"],
        "started_at": _STATE["started_at"],
        "finished_at": _STATE["finished_at"],
        "message": _STATE["message"],
        "progress": _STATE["progress"],
        "logs": list(_STATE.get("logs", []))[-200:],
    }


@router.get("/report")
async def backtest_report():
    """回测报告（优先内存，否则读本地落盘的最新结果）。"""
    if _STATE["result"] is not None:
        return {"result": _STATE["result"]}
    if os.path.exists(LATEST_FILE):
        try:
            with open(LATEST_FILE, "r", encoding="utf-8") as f:
                return {"result": json.load(f), "source": "disk"}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取落盘报告失败: {exc}")
    return {"message": "尚无回测结果，请先触发 /run", "result": None}


@router.get("/reports")
async def backtest_reports():
    """列出历史回测结果（本地落盘文件）。"""
    try:
        if not os.path.exists(RESULT_DIR):
            return {"reports": []}
        files = sorted(os.listdir(RESULT_DIR), reverse=True)
        reports = []
        for fn in files:
            if not fn.endswith(".json"):
                continue
            path = os.path.join(RESULT_DIR, fn)
            summary = None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                summary = {
                    "samples": d.get("samples", {}).get("total"),
                    "elapsed": d.get("elapsed"),
                    "go": d.get("ab_validation", {}).get("go"),
                }
            except Exception:  # noqa: BLE001
                pass
            reports.append({
                "filename": fn,
                "size": os.path.getsize(path),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
                "summary": summary,
            })
        return {"reports": reports}
    except Exception as exc:  # noqa: BLE001
        return {"reports": [], "error": str(exc)}


@router.get("/report/{filename}")
async def backtest_report_file(filename: str):
    """读取指定历史回测报告（本地落盘文件）。"""
    safe = os.path.basename(filename)
    path = os.path.join(RESULT_DIR, safe)
    if not safe.endswith(".json") or not os.path.exists(path):
        return {"message": "报告不存在", "result": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {"result": json.load(f)}
    except Exception as exc:  # noqa: BLE001
        return {"message": f"读取失败: {exc}", "result": None}


@router.get("/segments")
async def backtest_segments(label: str = ""):
    """查询行情段（label: success/failure_A/failure_B/all）。"""
    if _STATE["result"] is None:
        return {"segments": []}
    segments = _STATE["result"].get("segments", [])
    if label and label != "all":
        # segments 本身不含 label，这里简化：仅按全量返回（详细过滤需持久化）
        pass
    return {"segments": segments[:200]}


# ──────────────────────────────────────────────
# 持续监控（月度滚动再验证，对齐规划 5.6 / 第十四章第 18 项）
# ──────────────────────────────────────────────
_MONITOR_STATE = {
    "status": "idle",          # idle / running / success / failed
    "message": "",
    "report": None,
}


def _monitor_worker(date: str) -> None:
    """后台执行持续监控。"""
    try:
        _MONITOR_STATE["status"] = "running"
        _MONITOR_STATE["message"] = "监控运行中"
        _MONITOR_STATE["report"] = monitor.run_monitor(date)
        _MONITOR_STATE["status"] = "success"
        _MONITOR_STATE["message"] = "监控完成"
    except Exception as exc:  # noqa: BLE001
        _MONITOR_STATE["status"] = "failed"
        _MONITOR_STATE["message"] = f"监控失败: {exc}"
        logger.exception("持续监控失败")


class MonitorHitsRequest(BaseModel):
    records: list[dict] = Field(default_factory=list, description="每日推荐命中记录（date/ts_code/form_type/pred_prob/t5_ret）")


@router.post("/monitor/run")
async def run_monitor_endpoint(date: str = ""):
    """触发持续监控（月度漂移 + 模式失效检测），后台执行。"""
    if _MONITOR_STATE["status"] == "running":
        return {"status": "running", "message": "监控任务已在进行"}
    t = threading.Thread(target=_monitor_worker, args=(date,), daemon=True)
    t.start()
    return {"status": "pending", "message": "监控任务已提交"}


@router.get("/monitor/report")
async def monitor_report_endpoint():
    """获取最近监控报告（实际命中率 vs 回测基线 + 模式失效检测）。"""
    if _MONITOR_STATE["report"] is not None:
        return {"report": _MONITOR_STATE["report"]}
    return {"report": monitor.get_monitor_report()}


@router.post("/monitor/hits")
async def record_hits_endpoint(req: MonitorHitsRequest):
    """记录每日推荐实际命中（由每日推荐流水线在 T+5 后回填 t5_ret）。"""
    n = monitor.record_hits(req.records)
    return {"added": n}


@router.post("/monitor/revalidate")
async def monitor_revalidate_endpoint():
    """月度滚动再验证：用最近一次回测结果刷新基线并给出上线建议。"""
    if _STATE["result"] is None:
        return {"message": "尚无回测结果，请先运行回测", "result": None}
    return monitor.roll_revalidate(_STATE["result"])


# ──────────────────────────────────────────────
# 规则形态验证（规划 12 / 14：验证 → 固化 form_leaderboard.json → 每日推荐双信号）
# 每日推荐 daily_scan 加载该文件启用规则形态信号（信号 2），与 20 天向量等权融合。
# 此前该流程无任何触发入口，导致 form_leaderboard.json 从未生成、规则信号静默退化。
# ──────────────────────────────────────────────
_RULE_VERIFY_STATE = {
    "status": "idle",          # idle / running / success / failed
    "started_at": "",
    "finished_at": "",
    "message": "",
    "progress": 0.0,
    "report": None,
    "logs": [],
}


def _rule_verify_worker(
    max_stocks: int | None,
    start_date: str,
    end_date: str,
    mine_auto: bool,
    p_value_alpha: float,
    min_oos_hit_rate: float,
) -> None:
    """后台线程执行规则形态全市场验证。"""
    try:
        _RULE_VERIFY_STATE["status"] = "running"
        _RULE_VERIFY_STATE["message"] = "规则形态验证进行中"
        _RULE_VERIFY_STATE["progress"] = 0.0
        _RULE_VERIFY_STATE["logs"] = []

        def _progress(stage: str, pct: float):
            _RULE_VERIFY_STATE["progress"] = round(pct, 3)
            _RULE_VERIFY_STATE["message"] = f"{stage}"

        handler = _ListHandler(_RULE_VERIFY_STATE["logs"])
        logger.addHandler(handler)
        try:
            from app.backtest.rule_verifier import run_rule_verification
            report = run_rule_verification(
                max_stocks=max_stocks,
                start_date=start_date,
                end_date=end_date,
                mine_auto=mine_auto,
                p_value_alpha=p_value_alpha,
                min_oos_hit_rate=min_oos_hit_rate,
                progress_cb=_progress,
            )
        finally:
            logger.removeHandler(handler)
        _RULE_VERIFY_STATE["report"] = report
        _RULE_VERIFY_STATE["status"] = "success"
        _RULE_VERIFY_STATE["message"] = (
            f"规则形态验证完成：verified {report.get('verified_count', 0)} 条"
            f"，fallback {report.get('fallback', False)}"
        )
        _RULE_VERIFY_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _RULE_VERIFY_STATE["progress"] = 1.0
    except Exception as exc:  # noqa: BLE001
        _RULE_VERIFY_STATE["status"] = "failed"
        _RULE_VERIFY_STATE["message"] = f"规则形态验证失败: {exc}"
        _RULE_VERIFY_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        logger.exception("规则形态验证失败")


class RuleVerifyRequest(BaseModel):
    max_stocks: int | None = Field(default=None, ge=1, description="限制股票数（调试用）")
    start_date: str = Field(default="20150101", description="数据起始日期 YYYYMMDD")
    end_date: str = Field(default="", description="数据截止日期（空=最新）")
    mine_auto: bool = Field(default=True, description="是否执行系统自总结规则挖掘")
    p_value_alpha: float = Field(default=0.05, ge=0.0, le=1.0,
                                 description="显著性水平（扩充规则库可放宽到 0.10）")
    min_oos_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0,
                                    description="OOS 段 T+1 上涨率下限（0=不低于基线；0.45=旧口径）")


@router.post("/verify-rules")
async def trigger_rule_verify(req: RuleVerifyRequest):
    """触发规则形态全市场验证（后台执行），固化 backend/data/form_leaderboard.json。"""
    if _RULE_VERIFY_STATE["status"] == "running":
        return {"status": "running", "message": "规则形态验证已在进行中，请等待完成"}
    _RULE_VERIFY_STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _RULE_VERIFY_STATE["finished_at"] = ""
    _RULE_VERIFY_STATE["status"] = "pending"
    _RULE_VERIFY_STATE["progress"] = 0.0
    _RULE_VERIFY_STATE["message"] = "规则形态验证任务已提交"
    t = threading.Thread(
        target=_rule_verify_worker,
        args=(req.max_stocks, req.start_date, req.end_date, req.mine_auto,
              req.p_value_alpha, req.min_oos_hit_rate),
        daemon=True,
    )
    t.start()
    return {"status": "pending", "message": "规则形态验证已提交，请轮询 /verify-rules/status"}


@router.get("/verify-rules/status")
async def rule_verify_status():
    """规则形态验证任务状态 + 详细日志。"""
    return {
        "status": _RULE_VERIFY_STATE["status"],
        "started_at": _RULE_VERIFY_STATE["started_at"],
        "finished_at": _RULE_VERIFY_STATE["finished_at"],
        "message": _RULE_VERIFY_STATE["message"],
        "progress": _RULE_VERIFY_STATE["progress"],
        "logs": list(_RULE_VERIFY_STATE.get("logs", []))[-200:],
    }


@router.get("/verify-rules/report")
async def rule_verify_report():
    """规则形态验证结果（排行榜）。"""
    if _RULE_VERIFY_STATE["report"] is not None:
        return {"report": _RULE_VERIFY_STATE["report"]}
    return {"message": "尚无规则形态验证结果，请先触发 /verify-rules", "report": None}
