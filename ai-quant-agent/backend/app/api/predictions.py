"""预测榜单接口（每日推荐，对齐 plans/09-每日推荐详细规划.md 八）

  - GET  /api/v1/predictions/today         获取今日榜单（综合上涨概率 + 命中条件）
  - GET  /api/v1/predictions/date/{date}   获取指定日期历史榜单
  - GET  /api/v1/predictions/today/{ts_code} 个股推荐详情
  - POST /api/v1/predictions/scan          手动触发每日全市场扫描（后台）
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date

from fastapi import APIRouter

from app.core.logger import logger
from app.core.config import settings
from app.backtest.daily_scan import run_daily_scan, load_report
from app.backtest.loader import get_latest_daily_date
from app.agents import run_agent_refine

router = APIRouter(prefix="/api/v1/predictions", tags=["预测榜单"])

# 每日扫描任务状态
_SCAN_STATE = {
    "status": "idle",          # idle / running / success / failed
    "message": "",
    "started_at": "",
    "finished_at": "",
    "report": None,
    "logs": [],                # 扫描详细日志（实时，前端展示后端在做什么）
}
# 扫描代际计数器：force 强制重扫时 +1。旧扫描线程写状态前校验代际，
# 若已被新扫描接管则丢弃自身结果，避免"旧线程跑完覆盖新线程状态"。
_SCAN_GENERATION = {"gen": 0}
# 停止扫描信号（协作式取消）：前端「停止扫描」→ POST /scan/stop → 置位 →
# daily_scan 逐股循环在下一次迭代前退出（不落盘、不登记命中，见 daily_scan._run_daily_scan_impl）。
# 每次启动新扫描前 clear()，保证信号不会"跨扫描残留"（否则新扫描会立刻自我中断）。
_SCAN_CANCEL = threading.Event()
# 扫描"疑似卡死"阈值：running 超过该时长（秒）仍无 finished，视为异常，
# 允许用户 force 强制重扫（默认 3 小时；全市场扫描正常需数小时，见 O2 优化）
SCAN_STUCK_THRESHOLD = 3 * 3600


class _ListHandler(logging.Handler):
    """捕获扫描期间的 quant 日志到 _SCAN_STATE['logs']（前端实时展示）。"""

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


def _data_date() -> str:
    """每日推荐"数据时间基准"：以股票日线最新日期为准（而非运行当天日期）。

    与 run_daily_scan 的结果日期口径保持一致，保证"今日"查询、落盘文件名
    均与榜单结果日期（daily 表最新交易日）对齐，避免数据未更新时虚标日期。
    """
    latest = get_latest_daily_date()
    if latest is not None:
        return latest.strftime("%Y%m%d")
    return date.today().strftime("%Y%m%d")


def _existing_report_guard(today: str) -> dict | None:
    """幂等守卫：自动触发(拉取数据/定时)当日已存在结果时跳过重扫。

    背景 Bug：结果日期以"最新日线日期"为准。当最新日线日期(如 20260831)当日已生成过
    结果(agent_v1 25 条)后，用户再次"拉取最新数据"自动触发扫描，会再次对同一数据日期做
    全市场扫描 + Agent 精筛，并触发 record_agent_hits() 覆盖式登记——该方法会先删除该日期
    全部旧命中记录再重登记(25→23→10 条)，导致完整命中集逐版覆盖丢失，
    污染 monitor_state / T+5 回填 / 次日自我验证 / 进化反馈。

    修复：自动路径(auto_data/schedule)命中即跳过；手动扫描(manual)保留 vN 多版能力，
    force=true 强制重扫仍可用。

    Args:
        today: 目标数据日期 YYYYMMDD（= 最新日线日期，与榜单口径一致）

    Returns:
        已有结果时的跳过响应 dict；无结果返回 None（继续扫描）。
    """
    if _load_agent_report(today) is not None or load_report(today) is not None:
        # plans/23 §2.5 V2：把"跳过"原因说清楚，并区分两种情形，便于排查自动链路是否空转：
        #   ① data_not_advanced：日线还没推进到新交易日（典型：采集时点过早，17:30 前）
        #      → 这是"等数据"，不是"已扫过"，定时任务会在数据就绪后重试/由 21:00 兜底
        #   ② already_scanned：该交易日已产出结果（真幂等命中，无需重扫）
        # 修复前只有一句笼统提示，导致"连续多日 skipped"看起来像正常工作，实际是空转。
        try:
            from app.backtest.loader import get_latest_daily_date as _gl
            _latest = _gl()
            latest_s = _latest.strftime("%Y%m%d") if _latest is not None else ""
        except Exception:  # noqa: BLE001
            latest_s = ""
        reason = "already_scanned" if (latest_s and latest_s == today) else "data_not_advanced"
        if reason == "data_not_advanced":
            msg = (
                f"数据日期 {today} 已有结果，但日线最新仅为 {latest_s or '未知'}："
                f"属『数据未推进』（多为采集时点过早，Tushare 日频尚未就绪），"
                f"非『已扫描无需重扫』；定时链路会在数据就绪后自动重试（17:30 主任务 + 30 分钟重试 + 21:00 兜底），"
                f"如需立即重扫请手动 POST /scan?force=true"
            )
        else:
            msg = (
                f"数据日期 {today} 已存在推荐结果（真幂等命中：该交易日已扫描过）："
                f"跳过重复扫描/精筛，避免覆盖当日命中登记；如需强制重扫请手动 POST /scan?force=true"
            )
        return {"status": "skipped", "date": today, "reason": reason, "message": msg}
    return None


def _scan_worker(max_stocks: int | None) -> None:
    """后台执行每日全市场扫描（附带详细日志捕获）。

    force/代际机制：force 重扫时生成代际递增，旧扫描线程（若仍在运行）写完
    结果前校验代际，被新扫描接管则丢弃自身结果，避免"旧线程跑完覆盖新线程"。
    """
    my_gen = _SCAN_GENERATION["gen"]
    try:
        _SCAN_STATE["status"] = "running"
        _SCAN_STATE["message"] = "每日扫描进行中"
        _SCAN_STATE["logs"] = []
        # 附加日志捕获 handler：收集扫描期间所有模块日志到 _SCAN_STATE['logs']
        handler = _ListHandler(_SCAN_STATE["logs"])
        logger.addHandler(handler)
        try:
            report = run_daily_scan(
                max_stocks=max_stocks,
                progress_cb=lambda stage, pct: logger.info(f"[predictions/scan] {stage} {pct:.0%}"),
                should_stop=_SCAN_CANCEL.is_set,
            )
        finally:
            logger.removeHandler(handler)
        # 代际校验：期间若有 force 重扫接管，则本线程结果作废（不覆盖新线程状态）
        if _SCAN_GENERATION["gen"] != my_gen:
            logger.info("[predictions/scan] 本扫描已被新扫描接管，丢弃本次结果（旧线程）")
            return
        # ── 用户请求停止：明确落"cancelled"状态，且**不进入 Agent 精筛** ──
        if report.get("cancelled"):
            _SCAN_STATE["report"] = None
            _SCAN_STATE["status"] = "cancelled"
            _SCAN_STATE["message"] = report.get("message") or "扫描已停止"
            _SCAN_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            logger.warning(f"[predictions/scan] {_SCAN_STATE['message']}")
            return
        _SCAN_STATE["report"] = report
        _SCAN_STATE["status"] = "success"
        _SCAN_STATE["message"] = (
            f"扫描完成：扫描 {report.get('total_scanned', 0)} 只，"
            f"确认启动 {report.get('confirmed_count', 0)}，推荐 {len(report.get('top_picks', []))} 条"
        )
        _SCAN_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # ── 自动 Agent 精筛（三层漏斗，作为全市场扫描最后一环，无需单独触发）──
        if settings.agent_enabled and report.get("top_picks"):
            try:
                _AGENT_STATE["status"] = "running"
                _AGENT_STATE["message"] = "Agent 精筛进行中（扫描最后一环）"
                _AGENT_STATE["logs"] = []
                handler2 = _ListHandler(_AGENT_STATE["logs"])
                logger.addHandler(handler2)
                try:
                    agent_result = run_agent_refine(
                        report["top_picks"],
                        progress_cb=lambda stage, pct: logger.info(f"[agent/refine] {stage} {pct:.0%}"),
                        should_stop=_SCAN_CANCEL.is_set,
                    )
                finally:
                    logger.removeHandler(handler2)
                _AGENT_STATE["report"] = agent_result
                if agent_result.get("cancelled"):
                    # 用户中途停止：精筛不落盘（半成品榜单会污染次日自我验证/命中登记）
                    _AGENT_STATE["status"] = "cancelled"
                    _AGENT_STATE["message"] = agent_result.get("message") or "Agent 精筛已停止"
                else:
                    _AGENT_STATE["status"] = "success"
                    _AGENT_STATE["message"] = (
                        f"Agent 精筛完成：批量 {agent_result.get('batch_count', 0)}，"
                        f"深度 {agent_result.get('refined_count', 0)}，select {agent_result.get('select_count', 0)}，"
                        f"最终 {len(agent_result.get('top_picks', []))} 条"
                    )
                _AGENT_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                # 持久化 Agent 最终榜单（最后一环产物，刷新/重启后端不丢失）
                if not agent_result.get("cancelled"):
                    _save_agent_report(report, agent_result)
            except Exception as exc:  # noqa: BLE001
                _AGENT_STATE["status"] = "failed"
                _AGENT_STATE["message"] = f"Agent 精筛失败: {exc}"
                _AGENT_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                logger.exception("Agent 精筛失败")
    except Exception as exc:  # noqa: BLE001
        _SCAN_STATE["status"] = "failed"
        _SCAN_STATE["message"] = f"每日扫描失败: {exc}"
        _SCAN_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        logger.exception("每日扫描失败")


def _with_plans(report: dict | None, date: str = "") -> dict:
    """给榜单附加**价格化操作计划**（买入区间/回踩低吸/止损/止盈/持有期）。

    用户诉求："入场建议要标明什么价格买入、什么价格卖出"——故在读取榜单时按需生成，
    不改扫描主流程；生成失败时静默返回原榜单（不影响推荐展示）。
    """
    if not isinstance(report, dict):
        return report or {}
    try:
        from app.backtest import trade_plan
        return trade_plan.attach_plans(report, cache_key=f"{date or report.get('date', '')}")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[predictions] 生成操作计划失败（不影响榜单）: {exc}")
        return report


def _with_baseline_shadow(report: dict | None, date: str = "") -> dict:
    """给榜单附加**基准影子对照**（plans/24 §11.19 / todo 18）：B1/B2 名单 + 门槛判定。

    为什么是"影子"而不是"直接把权重换掉"：§11.18 已定量证明，
    同日同口径下"随机抽 30 只"比我们的打分选股每年多赚 6.8pp；
    但**换权重会改变持仓只数、行业暴露与回撤路径**，必须先有独立的 A/B 与
    样本外验证（项目纪律：先验证后生效）。故此处只**附加**对照信息，
    让"是否切权重"变成**有依据的决策**，而不是拍脑袋。

    三条硬保证（由 scripts/verify_baseline_shadow.py 的 A/C 组逐条验收）：

      - 参数 `daily_baseline_mode` 默认 `off` ⇒ **原样返回同一对象**（输出逐字节不变）；
      - 开启后也只**新增** `baseline_shadow` 一个键，`top_picks` 顺序与内容一字不动；
      - 任何异常静默降级（与 `_with_plans` 同一策略），不影响榜单展示。
    """
    if not isinstance(report, dict):
        return report or {}
    try:
        from app.backtest import baseline_shadow
        return baseline_shadow.attach_shadow(report, date)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[predictions] 生成影子对照失败（不影响榜单）: {exc}")
        return report


@router.get("/today")
async def get_today_predictions():
    """获取今日推荐榜单（综合上涨概率 + 命中条件 + **价格化操作计划**，可解释）。

    优先返回 Agent 精筛最终榜单（已持久化 daily_YYYYMMDD_agent.json），
    无 Agent 榜单时回退到规则扫描榜单。
    """
    today = _data_date()
    report = _load_agent_report(today) or load_report(today) or _SCAN_STATE.get("report")
    if report is None:
        return {
            "date": today,
            "message": "今日榜单尚未生成，请触发 POST /api/v1/predictions/scan（或等待每日 18:00 自动扫描）",
            "top_picks": [],
        }
    return _with_baseline_shadow(_with_plans(report, today), today)


@router.get("/date/{query_date}")
async def get_predictions_by_date(query_date: str):
    """获取指定日期历史榜单（优先 Agent 精筛最终榜单，附价格化操作计划）。"""
    report = _load_agent_report(query_date) or load_report(query_date)
    if report is None:
        return {"date": query_date, "message": "该日暂无榜单", "top_picks": []}
    return _with_baseline_shadow(_with_plans(report, query_date), query_date)


@router.get("/today/{ts_code}")
async def get_stock_prediction(ts_code: str):
    """获取某只股票的今日推荐详情（优先 Agent 精筛最终榜单，附价格化操作计划）。"""
    today = _data_date()
    report = _load_agent_report(today) or load_report(today) or _SCAN_STATE.get("report") or {}
    report = _with_plans(report, today)
    pick = next((p for p in report.get("top_picks", []) if p.get("ts_code") == ts_code), None)
    if pick is None:
        return {"ts_code": ts_code, "message": "今日不在推荐榜中"}
    return {"ts_code": ts_code, "detail": pick}


def _scan_running_stuck() -> str | None:
    """判断当前 running 扫描是否疑似卡死（超过阈值未结束）。

    全市场扫描（5535 只）正常需数小时，但若超 SCAN_STUCK_THRESHOLD（默认 3h）
    仍未结束，提示用户可 force 强制重扫。返回提示文本或 None。
    """
    if _SCAN_STATE["status"] != "running" or not _SCAN_STATE.get("started_at"):
        return None
    try:
        started = time.mktime(time.strptime(_SCAN_STATE["started_at"], "%Y-%m-%d %H:%M:%S"))
    except Exception:  # noqa: BLE001
        return None
    if time.time() - started > SCAN_STUCK_THRESHOLD:
        return (
            f"扫描已运行超过 {SCAN_STUCK_THRESHOLD // 3600} 小时未结束（自 {_SCAN_STATE['started_at']}），"
            f"疑似卡死或全市场扫描过慢；如确认异常可调用 POST /scan?force=true 强制重扫"
        )
    return None


def start_scan_task(source: str = "manual", max_stocks: int | None = None, force: bool = False) -> dict:
    """统一每日全市场扫描入口（手动/定时/采集完成自动三条路径共用）。

    用户要求：定时任务在前端也要有反应（按钮/日志/"执行中"状态），不能静默执行。
    统一走 _SCAN_STATE + _scan_worker（带日志捕获 + Agent 精筛 + 落盘），
    前端 GET /scan/status 轮询即可看到所有来源的执行进度与日志。

    Args:
        source: 触发来源（manual 手动 / schedule 定时任务 / auto_data 采集完成自动）
        max_stocks: 限制扫描股票数（调试用）
        force: 强制重扫（即使上次仍在 running）

    Returns:
        {status, message, ...}
    """
    if _SCAN_STATE["status"] == "running":
        if not force:
            stuck = _scan_running_stuck()
            msg = f"每日扫描已在进行中（自 {_SCAN_STATE['started_at']}，来源 {_SCAN_STATE.get('source') or 'unknown'}）"
            if stuck:
                msg += f"；{stuck}"
            return {"status": "running", "message": msg, "force_available": bool(stuck)}
        logger.info(
            f"[predictions/scan] 强制重扫接管旧扫描（来源 {source}，"
            f"代际 {_SCAN_GENERATION['gen']} → {_SCAN_GENERATION['gen'] + 1}）"
        )
    # 幂等守卫（自动路径）：最新数据日期当日已生成过结果时，不重复扫描/精筛/覆盖命中登记
    if not force and source in ("auto_data", "schedule"):
        _guard = _existing_report_guard(_data_date())
        if _guard is not None:
            logger.info(f"[predictions/scan] {_guard['message']}")
            return _guard
    _SCAN_CANCEL.clear()   # 每次启动清空停止信号（防跨扫描残留 → 新扫描立刻自我中断）
    _SCAN_GENERATION["gen"] += 1
    _SCAN_STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _SCAN_STATE["finished_at"] = ""
    _SCAN_STATE["status"] = "pending"
    _SCAN_STATE["source"] = source
    _SCAN_STATE["message"] = f"每日扫描已提交（来源：{source}）" + ("（强制重扫）" if force else "")
    t = threading.Thread(target=_scan_worker, args=(max_stocks,), daemon=True)
    t.start()
    logger.info(f"[predictions/scan] 已启动每日扫描（来源 {source}）")
    return {"status": "pending", "message": "每日扫描已提交，请稍后查询 GET /today"}


# ── 采集完成后的"待确认"（用户要求：不要每次都自动扫，弹窗让我决定）──────────────
# 模式由可进化参数 auto_scan_after_collect 控制：
#   confirm（默认）：采集完成 → 只标记"待确认"，前端弹窗询问；用户点"是"才扫
#   auto          ：旧行为（采集完成直接自动扫）
#   off           ：完全不自动扫（纯手动）
_SCAN_CONFIRM = {
    "pending": False,
    "data_date": "",
    "source": "",
    "created_at": "",
    "note": "",
    "last_action": "",       # confirmed / dismissed
    "last_action_at": "",
}


def auto_scan_mode() -> str:
    """读 auto_scan_after_collect（非法值一律回退 confirm：安全默认=不擅自跑）。"""
    try:
        from app.agents import evolution_config
        v = str(evolution_config.get_param("auto_scan_after_collect", "confirm") or "").strip().lower()
    except Exception:  # noqa: BLE001
        v = "confirm"
    return v if v in ("confirm", "auto", "off") else "confirm"


def _clear_confirm(action: str, note: str = "") -> None:
    _SCAN_CONFIRM.update({
        "pending": False,
        "last_action": action,
        "last_action_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note or _SCAN_CONFIRM.get("note", ""),
    })


def handle_after_collection(source: str = "auto_data", data_date: str = "") -> dict:
    """采集完成后的**唯一入口**（手动采集 / 定时采集共用）。

    取代原先"采集完成直接 start_scan_task"：
      - confirm → 只标记待确认（前端弹窗），**不启动扫描**
      - auto    → 老行为（直接启动扫描）
      - off     → 不处理
    """
    mode = auto_scan_mode()
    if mode == "auto":
        return start_scan_task(source=source, max_stocks=None)
    if mode == "off":
        logger.info("[predictions] 采集完成：auto_scan_after_collect=off → 不自动扫描（可手动触发）")
        return {"status": "off", "message": "采集完成（已配置为不自动扫描，可手动触发）"}
    # 已在扫描中就不再问（避免弹一个"要不要跑"但点了也跑不了的窗）
    if _SCAN_STATE["status"] in ("running", "pending"):
        logger.info(
            f"[predictions] 采集完成：扫描已在运行（来源 {_SCAN_STATE.get('source') or 'unknown'}）→ 不再询问"
        )
        return {"status": "running", "message": "扫描已在进行中，无需确认（沿用正在跑的这次）"}
    _SCAN_CONFIRM.update({
        "pending": True,
        "data_date": data_date or _data_date(),
        "source": source,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "数据采集已完成，是否立即运行每日推荐扫描？",
        "last_action": "",
        "last_action_at": "",
    })
    logger.info(
        f"[predictions] 采集完成（来源 {source}，数据日期 {_SCAN_CONFIRM['data_date']}）→ "
        f"等待用户确认是否运行每日推荐扫描（auto_scan_after_collect=confirm）"
    )
    return {"status": "confirm_pending", "message": "采集完成：等待确认是否运行每日推荐扫描",
            "data_date": _SCAN_CONFIRM["data_date"]}


@router.post("/scan/confirm")
async def confirm_scan(max_stocks: int | None = None, force: bool = False):
    """用户点「是」：确认运行每日推荐扫描（弹窗路径入口）。"""
    if _SCAN_STATE["status"] in ("running", "pending") and not force:
        _clear_confirm("confirmed", "扫描已在运行，确认请求被忽略（沿用正在跑的这次）")
        return {"status": "running", "confirmed": True,
                "message": f"扫描已在进行中（自 {_SCAN_STATE['started_at']}），无需重复触发"}
    _clear_confirm("confirmed", "用户确认运行每日推荐扫描")
    res = start_scan_task(source="confirm", max_stocks=max_stocks, force=force)
    return {**res, "confirmed": True}


@router.post("/scan/dismiss")
async def dismiss_scan(reason: str = "user_skip"):
    """用户点「否/稍后」：本次不扫（可随时手动触发）。"""
    _clear_confirm("dismissed", f"用户跳过本次扫描（{reason}）")
    logger.info(f"[predictions] 用户跳过采集后的每日推荐扫描（{reason}）")
    return {"status": "dismissed", "message": "已跳过本次每日推荐扫描，可随时手动触发"}


@router.post("/scan")
async def trigger_scan(max_stocks: int | None = None, force: bool = False):
    """手动触发每日全市场扫描（后台执行，可传 max_stocks 调试限流）。

    force=true：即使上次扫描仍在 running（疑似卡死/未结束）也强制重扫；
    新扫描代际接管，旧线程结果作废。统一走 start_scan_task（前端可见状态/日志）。
    """
    return start_scan_task(source="manual", max_stocks=max_stocks, force=force)


@router.post("/scan/stop")
async def stop_scan():
    """请求停止正在运行的每日扫描（协作式取消，用户点「停止扫描」）。

    语义说明：不是"立刻 kill 线程"，而是在 daily_scan 逐股循环的**下一次迭代前**退出
    （单只股票处理通常 1~3 秒，所以实际响应是秒级）。停止后：
      - 状态置为 cancelled（前端可区分"停止"与"失败"）
      - **不落盘榜单、不登记命中**（避免半成品污染 monitor_state / T+5 回填）
      - 若扫描已进入 Agent 精筛阶段，本信号同样会让精筛在批间退出
    """
    # 扫描与「手动 Agent 精筛」共用同一停止信号：任一在跑都能被停
    scan_running = _SCAN_STATE["status"] in ("running", "pending")
    agent_running = _AGENT_STATE["status"] == "running"
    if not scan_running and not agent_running:
        return {
            "status": _SCAN_STATE["status"],
            "message": f"当前没有正在运行的扫描/精筛（扫描：{_SCAN_STATE['status']}，"
                       f"精筛：{_AGENT_STATE['status']}）",
            "cancelled": False,
        }
    if _SCAN_CANCEL.is_set():
        return {"status": "stopping", "message": "停止请求已发送，正在中断…", "cancelled": False}
    _SCAN_CANCEL.set()
    _SCAN_STATE["message"] = "已请求停止扫描（将在当前股票处理完后中断，不产出榜单）"
    logger.warning("[predictions/scan] 收到停止扫描请求")
    return {"status": "stopping", "message": "已请求停止扫描，正在中断…", "cancelled": True}


@router.get("/scan/status")
async def scan_status():
    """每日扫描任务状态 + 详细日志（后端在做什么）。"""
    return {
        "status": _SCAN_STATE["status"],
        "message": _SCAN_STATE["message"],
        "started_at": _SCAN_STATE["started_at"],
        "finished_at": _SCAN_STATE["finished_at"],
        "source": _SCAN_STATE.get("source") or "",
        "cancel_requested": _SCAN_CANCEL.is_set(),
        "confirm_pending": dict(_SCAN_CONFIRM),
        "auto_scan_mode": auto_scan_mode(),
        "logs": list(_SCAN_STATE.get("logs", []))[-200:],
        "stuck_hint": _scan_running_stuck(),
    }


# ── 多 Agent 精筛（每日推荐最后一环，对齐 plans/03-Agent系统.md）──
_AGENT_STATE = {
    "status": "idle",          # idle / running / success / failed
    "message": "",
    "started_at": "",
    "finished_at": "",
    "report": None,
    "logs": [],                # 精筛详细日志（实时）
}


def _latest_report_file() -> dict | None:
    """读取最近一期落盘的每日榜单（候选池回退用）。

    返回"数据日期最新、同日期版本最新"的榜单；兼容旧无版本文件。
    """
    import glob
    import os
    import re
    from app.backtest.daily_scan import REPORT_DIR
    files = glob.glob(os.path.join(REPORT_DIR, "daily_*.json"))
    parsed = []
    for f in files:
        base = os.path.basename(f)
        m = re.match(r"^daily_(\d{8})(?:_agent)?(?:_v(\d+))?\.json$", base)
        if m:
            parsed.append((m.group(1), int(m.group(2) or 0), f))
    # 按 数据日期 → 版本号 升序，取最大的（最新日期 + 最新版本）
    parsed.sort(key=lambda x: (x[0], x[1]))
    for _d, _v, f in reversed(parsed):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                dd = json.load(fp)
            if dd.get("top_picks"):
                return dd
        except Exception:  # noqa: BLE001
            continue
    return None


def _save_agent_report(scan_report: dict, agent_result: dict) -> str:
    """把 Agent 精筛最终榜单持久化到 daily_recommend/daily_YYYYMMDD_agent.json。

    合并扫描统计 + Agent 最终 top_picks（含可解释理由），作为每日推荐**最终榜单**落盘，
    /today、/date 优先读取该文件返回，保证刷新/重启后端后最终榜单不丢失。
    失败返回 ''（不影响主流程）。
    """
    import os
    from app.backtest.daily_scan import REPORT_DIR, next_report_version
    # 落盘/命中登记用扫描产物的数据日期（scan_report['date']），而非运行时重查最新日线日期，
    # 避免扫描与落盘之间数据日期跳变（如扫描期间新数据写入）导致榜单/命中登记错位到错误日期
    today = str(scan_report.get("date") or "") or _data_date()
    version = next_report_version(f"daily_{today}_agent")
    path = os.path.join(REPORT_DIR, f"daily_{today}_agent_v{version}.json")
    try:
        payload = {
            "date": today,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "agent_refine",
            "total_scanned": scan_report.get("total_scanned", 0),
            "confirmed_count": scan_report.get("confirmed_count", 0),
            "candidate_pool": scan_report.get("candidate_pool", 0),
            "agent": {
                "total": agent_result.get("total", 0),
                "batch_count": agent_result.get("batch_count", 0),
                "refined_count": agent_result.get("refined_count", 0),
                "select_count": agent_result.get("select_count", 0),
                "elapsed": agent_result.get("elapsed", 0),
            },
            "top_picks": agent_result.get("top_picks", []),
        }
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Agent 最终榜单已落盘: {path}（{len(payload['top_picks'])} 条）")
        # 反馈对齐（优化）：Agent 最终榜单即用户实际看到的最终推荐，覆盖登记命中记录，
        # 使次日验证/进化反馈基于 Agent 最终榜单（而非规则版），避免进化目标脱节。
        try:
            from app.backtest.monitor import record_agent_hits
            if payload["top_picks"]:
                record_agent_hits(today, payload["top_picks"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Agent 最终榜单命中登记失败: {exc}")
        # 交易纪律台账（plans/22 P0）：当日榜单的操作计划落 kb_plan → 后续用日线触价回放评估纪律
        try:
            from app.backtest import plan_tracker
            _n = plan_tracker.save_plans(today, payload)
            logger.info(f"操作计划台账写入 {_n} 条（{today}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"操作计划台账写入失败（不影响榜单）: {exc}")
        # 进化事件流（plans/11 §15.2）：榜单摘要事件（复用落盘 + 1 行钩子）
        try:
            from app.agents import evolution_events
            evolution_events.record_event(
                "INFO", "daily_recommend",
                f"每日榜单 {today}: 扫描 {payload.get('total_scanned')} 只, "
                f"精筛 {payload.get('agent', {}).get('select_count', 0)} 只, 候选池 {payload.get('candidate_pool', 0)}",
            )
        except Exception:  # noqa: BLE001
            pass  # 事件上报失败不阻塞主流程
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Agent 最终榜单落盘失败: {exc}")
        return ""


def _load_agent_report(query_date: str) -> dict | None:
    """读取某日的 Agent 精筛最终榜单（同一天多版本取版本最新者）。无则 None。

    兼容旧无版本文件 daily_{query_date}_agent.json（视为最低版本）。
    """
    import glob
    import os
    import re
    from app.backtest.daily_scan import REPORT_DIR
    files = glob.glob(os.path.join(REPORT_DIR, f"daily_{query_date}_agent_v*.json"))
    candidates = []
    for f in files:
        m = re.match(rf"^daily_{query_date}_agent_v(\d+)\.json$", os.path.basename(f))
        if m:
            candidates.append((int(m.group(1)), f))
    # 旧无版本文件视为 v0（优先级最低），仅当无版本文件时使用
    candidates.append((0, os.path.join(REPORT_DIR, f"daily_{query_date}_agent.json")))
    candidates.sort(key=lambda x: x[0])
    for _v, path in reversed(candidates):
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("top_picks"):
                return d
        except Exception:  # noqa: BLE001
            continue
    return None


def _agent_worker(top_k: int | None, top_n: int | None) -> None:
    """后台执行多 Agent 精筛（手动触发路径；同样支持「停止扫描」中断）。"""
    try:
        _AGENT_STATE["status"] = "running"
        _AGENT_STATE["message"] = "Agent 精筛进行中"
        _AGENT_STATE["logs"] = []
        handler = _ListHandler(_AGENT_STATE["logs"])
        logger.addHandler(handler)
        try:
            # 读取候选池：当日（数据最新日）优先，否则回退最近一期落盘的每日榜单
            today = _data_date()
            report = load_report(today) or _SCAN_STATE.get("report") or _latest_report_file()
            if not report or not report.get("top_picks"):
                _AGENT_STATE["status"] = "failed"
                _AGENT_STATE["message"] = "无候选池，请先执行每日扫描（POST /scan）"
                return
            result = run_agent_refine(
                report["top_picks"],
                top_k=top_k,
                top_n=top_n,
                progress_cb=lambda stage, pct: logger.info(f"[agent/refine] {stage} {pct:.0%}"),
                should_stop=_SCAN_CANCEL.is_set,
            )
        finally:
            logger.removeHandler(handler)
        _AGENT_STATE["report"] = result
        if result.get("cancelled"):
            _AGENT_STATE["status"] = "cancelled"
            _AGENT_STATE["message"] = result.get("message") or "Agent 精筛已停止"
            _AGENT_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return
        _AGENT_STATE["status"] = "success"
        _AGENT_STATE["message"] = (
            f"Agent 精筛完成：精筛 {result.get('refined_count', 0)} 只，"
            f"select {result.get('select_count', 0)}，最终榜单 {len(result.get('top_picks', []))} 条"
        )
        # 反馈对齐（优化）：手动精筛同样落盘 Agent 最终榜单 + 覆盖登记命中记录
        try:
            if result.get("top_picks"):
                _save_agent_report(report, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Agent 手动精筛落盘失败: {exc}")
        _AGENT_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as exc:  # noqa: BLE001
        _AGENT_STATE["status"] = "failed"
        _AGENT_STATE["message"] = f"Agent 精筛失败: {exc}"
        _AGENT_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        logger.exception("Agent 精筛失败")


@router.post("/agent/refine")
async def trigger_agent_refine(top_k: int | None = None, top_n: int | None = None):
    """触发多 Agent 精筛（后台，读取当日候选池 → 精筛 → 最终榜单）。"""
    if not settings.agent_enabled:
        return {"status": "disabled", "message": "Agent 精筛已关闭（AGENT_ENABLED=false）"}
    if _AGENT_STATE["status"] == "running":
        return {"status": "running", "message": "Agent 精筛已在进行中"}
    _AGENT_STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _AGENT_STATE["finished_at"] = ""
    _AGENT_STATE["status"] = "pending"
    _AGENT_STATE["message"] = "Agent 精筛已提交"
    t = threading.Thread(target=_agent_worker, args=(top_k, top_n), daemon=True)
    t.start()
    return {"status": "pending", "message": "Agent 精筛已提交，请查询 GET /agent/status"}


@router.get("/agent/status")
async def agent_refine_status():
    """多 Agent 精筛任务状态 + 详细日志。"""
    return {
        "status": _AGENT_STATE["status"],
        "message": _AGENT_STATE["message"],
        "started_at": _AGENT_STATE["started_at"],
        "finished_at": _AGENT_STATE["finished_at"],
        "report": _AGENT_STATE.get("report"),
        "logs": list(_AGENT_STATE.get("logs", []))[-200:],
    }


# ── 反思层（§十五 "要越来越聪明"）─────────────────────────────────
_REFLECT_STATE = {
    "status": "idle",          # idle / running / success / failed
    "message": "",
    "started_at": "",
    "finished_at": "",
    "report": None,
    "logs": [],
}


def _reflect_worker(limit: int | None) -> None:
    """后台执行反思任务：回填 T+5 结果 → 反思失败推荐 → 固化教训。"""
    from app.agents.reflect_agent import backfill_outcomes, run_reflection
    try:
        _REFLECT_STATE["status"] = "running"
        _REFLECT_STATE["message"] = "反思任务进行中（回填结果→分析失败原因）"
        _REFLECT_STATE["logs"] = []
        handler = _ListHandler(_REFLECT_STATE["logs"])
        logger.addHandler(handler)
        try:
            bf = backfill_outcomes()
            rr = run_reflection(limit=limit)
        finally:
            logger.removeHandler(handler)
        _REFLECT_STATE["report"] = {"backfill": bf, "reflection": rr}
        _REFLECT_STATE["status"] = "success"
        _REFLECT_STATE["message"] = (
            f"反思完成：回填 {bf.get('filled', 0)}，反思 {rr.get('reflected', 0)}，"
            f"累计教训 {rr.get('total_lessons', 0)} 条"
        )
        _REFLECT_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as exc:  # noqa: BLE001
        _REFLECT_STATE["status"] = "failed"
        _REFLECT_STATE["message"] = f"反思任务失败: {exc}"
        _REFLECT_STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        logger.exception("反思任务失败")


@router.post("/reflect/run")
async def trigger_reflection(limit: int | None = None):
    """手动触发反思任务（后台：回填结果 → 反思失败推荐 → 固化教训）。"""
    if _REFLECT_STATE["status"] == "running":
        return {"status": "running", "message": "反思任务已在进行中"}
    _REFLECT_STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _REFLECT_STATE["finished_at"] = ""
    _REFLECT_STATE["status"] = "pending"
    _REFLECT_STATE["message"] = "反思任务已提交"
    t = threading.Thread(target=_reflect_worker, args=(limit,), daemon=True)
    t.start()
    return {"status": "pending", "message": "反思任务已提交，请查询 GET /reflect/status"}


@router.get("/reflect/status")
async def reflect_status():
    """反思任务状态 + 详细日志。"""
    return {
        "status": _REFLECT_STATE["status"],
        "message": _REFLECT_STATE["message"],
        "started_at": _REFLECT_STATE["started_at"],
        "finished_at": _REFLECT_STATE["finished_at"],
        "report": _REFLECT_STATE.get("report"),
        "logs": list(_REFLECT_STATE.get("logs", []))[-200:],
    }


@router.get("/reflect/report")
async def reflect_report():
    """反思报告：推荐命中统计 + 失败教训知识库 + 反馈精筛文本（"越来越聪明"的证据）。"""
    from app.agents.reflect_agent import load_reflect_kb, build_reflect_txt
    from app.backtest.monitor import _load_state as _load_monitor_state
    kb = load_reflect_kb()
    state = _load_monitor_state()
    hits = state.get("hits", [])
    filled = [h for h in hits if h.get("t5_ret") is not None]
    hit_n = sum(1 for h in filled if h.get("hit"))
    failed_n = len(filled) - hit_n
    return {
        "stats": {
            "total_recommended": len(hits),
            "has_outcome": len(filled),
            "hit_n": hit_n,
            "failed_n": failed_n,
            "hit_rate": round(hit_n / len(filled), 4) if filled else None,
        },
        "reflections": kb.get("reflections", []),
        "feedback_txt": build_reflect_txt(kb),
    }
