"""回测控制 + 回测健康诊断（backtest_ctl）— 进化大脑"控制回测"

用户核心诉求（plans/19）：
  1. 进化大脑要有控制回测的功能（触发 / 状态 / 读结果）；
  2. 要考虑"是不是回测的问题"——回测逻辑失效？回测产物与每日推荐脱节？
  3. 极致利用各种办法提高每日推荐股票的上涨概率。

能力：
  - is_running()          回测是否在跑
  - trigger_backtest()    触发一次回测（后台线程；复用 backtest API 状态机，限频 + 空闲保护）
  - status()              回测状态（复用 backtest API）
  - latest_result()       读最新回测报告（backtest_latest.json）
  - backtest_health()     回测健康诊断：对比"回测命中率 vs 每日推荐实盘命中率"，
                          判断是不是回测逻辑的问题（失效 / 脱节 / 系统性 / 正常）
"""
from __future__ import annotations

import json
import os
import time

from app.core.logger import logger
from app.agents import evolution_config

# 数据目录：backend/data/
DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
LATEST_FILE = os.path.join(DATA_DIR, "backtest_results", "backtest_latest.json")
VERIFY_KB_FILE = os.path.join(DATA_DIR, "verify_kb.json")
MONITOR_STATE_FILE = os.path.join(DATA_DIR, "monitor_state.json")

# 自动触发回测的冷却（秒）：进化大脑/每日诊断触发回测后，N 秒内不再重复触发（回测很重）
_BACKTEST_COOLDOWN = 6 * 3600
_last_trigger = {"ts": 0.0}


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def is_running() -> bool:
    """回测是否在跑（复用 backtest API 状态机）。"""
    try:
        from app.api import backtest as _bt
        return _bt._STATE.get("status") == "running"
    except Exception:  # noqa: BLE001
        return False


def status() -> dict:
    """回测状态（复用 backtest API）。"""
    try:
        from app.api import backtest as _bt
        return {
            "status": _bt._STATE.get("status", "idle"),
            "message": _bt._STATE.get("message", ""),
            "progress": _bt._STATE.get("progress", 0.0),
            "started_at": _bt._STATE.get("started_at", ""),
            "finished_at": _bt._STATE.get("finished_at", ""),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc)}


def latest_result() -> dict | None:
    """读最新回测报告（落盘）。无则 None。"""
    d = _read_json(LATEST_FILE, None)
    return d if isinstance(d, dict) else None


_LAST_INGEST: dict = {"mtime": None}


def ingest_latest(force: bool = False) -> dict:
    """把最新回测结果入库（plans/21 §七）：experiment 台账 + 口径指纹对比。

    以落盘文件 mtime 去重（同一份报告不重复入库）；失败静默，绝不影响回测/诊断主流程。
    """
    try:
        mtime = os.path.getmtime(LATEST_FILE)
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "无回测结果文件"}
    if not force and _LAST_INGEST["mtime"] == mtime:
        return {"ok": True, "skipped": "unchanged"}
    d = latest_result()
    if not d:
        return {"ok": False, "reason": "回测结果为空"}
    try:
        from app.kb import kb_ingest
        r = d.get("result", d) or {}
        res = kb_ingest.ingest_backtest(r, source="backtest_ctl",
                                        period=d.get("config") or r.get("config") or {})
        _LAST_INGEST["mtime"] = mtime
        return res
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[backtest_ctl] 回测结果入库跳过: {exc}")
        return {"ok": False, "reason": str(exc)}


def trigger_backtest(start_date: str = "20100101", end_date: str = "",
                     max_stocks: int | None = None, source: str = "evolution",
                     force: bool = False) -> dict:
    """触发一次回测（后台线程；复用 backtest API 状态机，保证状态/落盘/基线一致）。

    Args:
        start_date/end_date/max_stocks: 回测配置（默认全量）
        source: 触发来源（evolution 进化大脑 / manual 人工）
        force: 是否忽略冷却强制触发（默认 False 遵守冷却 + 不抢占已运行任务）

    Returns: {status, message, ...}
    """
    if is_running():
        return {"status": "running", "message": "回测已在运行，等待完成", "source": source}
    # BugFix：避免回测与每日扫描/Agent精筛/采集并发（回测 scan_market 与 daily_scan 都全市场拉数，
    # 无互斥时互相拖慢到近 2 小时像卡住）。force=True 时仍尊重并发保护（不抢占扫描/采集）。
    try:
        from app.agents import code_writer
        _idle = code_writer.is_idle()
        if not _idle["idle"]:
            blockers = [b for b in _idle.get("busy", [])
                        if b in ("daily_scan", "agent_refine", "reflection", "collection")]
            if blockers:
                return {"status": "busy",
                        "message": f"系统忙（{'/'.join(blockers)} 运行中），回测会与其并发争抢数据库被拖慢，请稍后重试",
                        "source": source}
    except Exception:  # noqa: BLE001
        pass

    # 冷却保护（回测全市场很重，避免进化大脑频繁触发）
    if not force:
        now = time.time()
        left = _BACKTEST_COOLDOWN - (now - _last_trigger["ts"])
        if left > 0:
            return {"status": "cooldown", "message": f"回测冷却中（{int(left // 60)} 分钟后可再触发）",
                    "cooldown_seconds": int(left), "source": source}

    try:
        from app.api import backtest as _bt
        import threading

        cfg = _bt.BacktestConfig(start_date=start_date, end_date=end_date or "",
                                 max_stocks=max_stocks)
        _last_trigger["ts"] = time.time()
        _bt._STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _bt._STATE["finished_at"] = ""
        _bt._STATE["status"] = "pending"
        _bt._STATE["progress"] = 0.0
        _bt._STATE["message"] = f"回测任务已提交（来源：{source}）"
        _bt._STATE["source"] = source
        t = threading.Thread(target=_bt._run_worker, args=(cfg,), daemon=True)
        t.start()
        logger.info(f"[backtest_ctl] 进化大脑触发回测（来源 {source}）：{start_date}~{end_date or '最新'}"
                    f" max_stocks={max_stocks}")
        return {"status": "pending", "message": "回测任务已提交（后台），请轮询 /status", "source": source}
    except Exception as exc:  # noqa: BLE001
        return {"status": "fail", "message": f"触发回测失败: {exc}", "source": source}


# 环节归因 TOP 持续观察（健康诊断 normal 分支建议"维持现有逻辑，持续观察环节归因 TOP 是否变化"的落地依据）
STAGE_TREND_TOP_N = 3      # 环节归因 TOP 观察条数
STAGE_TREND_PERIODS = 6    # 观察最近 N 期（含环节归因的验证期）
 
 
def _verify_verifications() -> list[dict]:
    """读 verify_kb.json 的 verifications（按落盘顺序，date 升序）。"""
    kb = _read_json(VERIFY_KB_FILE, {"verifications": []})
    verifs = kb.get("verifications", []) if isinstance(kb, dict) else []
    return verifs if isinstance(verifs, list) else []
 
 
def _stage_top(verification: dict, top_n: int) -> list[str]:
    """某期验证的环节归因 TOP（按 count 降序取 stage_name，最多 top_n 个）。"""
    report = verification.get("stage_report") or []
    rows = sorted(report, key=lambda r: -float(r.get("count", 0) or 0))
    return [str(r.get("stage_name") or r.get("stage") or "?") for r in rows[:top_n]]
 
 
def stage_attribution_trend(periods: int | None = None, top_n: int = STAGE_TREND_TOP_N) -> dict:
    """持续观察"环节归因 TOP 是否变化"（健康诊断 normal 分支建议的观察依据）。

    读取 verify_kb 多期次日验证的 stage_report（环节归因），取最近 periods 期，
    对比"最新一期"与"上一期"的环节归因 TOP（count 降序前 top_n）：
      - kept         ：最新 TOP 中也在上一期 TOP 的环节（保持）
      - new_in       ：最新 TOP 中不在上一期 TOP 的环节（新进）
      - gone         ：上一期 TOP 中不在最新 TOP 的环节（退出）
      - head_changed ：最新 TOP 首位 ≠ 上一期 TOP 首位
    changed=True 表示 TOP 漂移（新进/退出/首位变化任一），否则稳定。
    history 返回每期 date+top，供前端/诊断展示跨期变化。
    数据不足（<2 期含环节归因）返回 ok=False，待积累。
    """
    periods = periods or STAGE_TREND_PERIODS
    verifs = _verify_verifications()
    if len(verifs) < 2:
        return {"ok": False, "periods_available": len(verifs),
                "note": "环节归因历史不足（<2 期），待次日验证继续积累后再观察 TOP 变化"}
    recent = [v for v in verifs[-periods:] if (v.get("stage_report") or [])]
    per_period = [{"date": v.get("date", ""), "top": _stage_top(v, top_n)} for v in recent]
    if len(per_period) < 2:
        return {"ok": False, "periods_available": len(per_period),
                "note": "含环节归因的验证期不足（<2 期），待积累后再观察 TOP 变化"}
    latest, prev = per_period[-1], per_period[-2]
    lt, pt = set(latest["top"]), set(prev["top"])
    kept = [s for s in latest["top"] if s in pt]
    new_in = [s for s in latest["top"] if s not in pt]
    gone = [s for s in prev["top"] if s not in lt]
    head_changed = bool(latest["top"]) and (not prev["top"] or latest["top"][0] != prev["top"][0])
    changed = bool(new_in or gone or head_changed)
    if changed:
        note = ("环节归因TOP 漂移：新进 " + ("、".join(new_in) if new_in else "无") +
                "；退出 " + ("、".join(gone) if gone else "无") +
                ("；首位变化 → " + latest["top"][0] if head_changed else ""))
    else:
        note = f"环节归因TOP 稳定（最近两期前{top_n}环节保持一致）"
    return {
        "ok": True, "top_n": top_n,
        "latest_date": latest["date"], "latest_top": latest["top"],
        "prev_date": prev["date"], "prev_top": prev["top"],
        "kept": kept, "new_in": new_in, "gone": gone,
        "head_changed": head_changed, "changed": changed,
        "note": note, "history": per_period,
    }
 
 
def backtest_health() -> dict:
    """回测健康诊断：判断"是不是回测的问题"。

    对比回测命中率（accuracy/win_rate）与每日推荐实盘命中率（次日 T+1 / T+5），
    给出判定：
      - backtest_decouple : 回测准但实盘不准 → 回测产物与每日推荐脱节（加载口径/传导链路问题）
      - backtest_failure  : 回测命中率本身低 → 回测逻辑失效（特征/形态/结局分流/模式库）
      - market_issue      : 都低 → 系统性/市场环境问题（回测与推荐共同失效）
      - normal            : 实盘命中率达标 → 系统正常
      - insufficient      : 数据不足，待积累
    """
    # plans/21 §七：诊断前先确保最新回测结果已入库（含口径指纹），保证知识库回测事实不缺档
    try:
        ingest_latest()
    except Exception:  # noqa: BLE001
        pass
    bt = latest_result() or {}
    perf = bt.get("performance", {}) if isinstance(bt.get("performance"), dict) else {}
    has_backtest = bool(perf)

    # 回测命中率代表值（accuracy 更可信；win_rate/hit_rate 可能口径偏正样本）
    bt_accuracy = float(perf.get("accuracy") or 0.0)
    bt_win_rate = float(perf.get("win_rate") or 0.0)
    bt_avg_t5 = float(perf.get("avg_t5_return") or 0.0)
    bt_trades = int(perf.get("total_trades") or 0)

    # 每日推荐实盘：T+1/T+5 命中率（monitor_state hits，信号滞后修正后为 D+1 买入口径）
    # 用 t1_ret/t5_ret 直接统计（随 backfill_t1(force) 更新为新口径），不读 verify_kb 的历史旧口径。
    ms = _read_json(MONITOR_STATE_FILE, {"hits": []})
    hits = ms.get("hits", [])
    done1 = [h for h in hits if h.get("t1_ret") is not None]
    done5 = [h for h in hits if h.get("t5_ret") is not None]
    try:
        _t1_thr = float(evolution_config.get_param("t1_hit_threshold", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        _t1_thr = 0.0
    live_t1 = (sum(1 for h in done1 if float(h.get("t1_ret", 0) or 0) > _t1_thr) / len(done1)) if done1 else None
    live_t5 = (sum(1 for h in done5 if float(h.get("t5_ret", 0) or 0) > 0) / len(done5)) if done5 else None

    if not has_backtest and live_t1 is None:
        return {"ok": False, "verdict": "insufficient", "has_backtest": False,
                "message": "尚无回测报告与次日验证数据，待积累后诊断"}

    # 判定逻辑
    verdict, hypotheses, suggestions = "insufficient", [], []
    gap = None
    if has_backtest:
        bt_ref = bt_accuracy if bt_accuracy > 0 else bt_win_rate
        if live_t1 is not None:
            gap = round(bt_ref - live_t1, 3)
            if live_t1 >= 0.5:
                verdict = "normal"
                hypotheses = ["实盘次日命中率达标，系统整体健康"]
                suggestions = ["维持现有逻辑，持续观察环节归因 TOP 是否变化"]
            elif bt_ref >= 0.6 and gap > 0.1:
                verdict = "backtest_decouple"
                hypotheses = ["回测命中率高但实盘低 → 回测产物（模式库/条件表/归一化）未正确传导到每日推荐",
                              "每日推荐加载的回测产物口径与训练不一致，或样本偏差"]
                suggestions = [
                    "检查 daily_scan 加载 probability_table/condition_table/normalizer 的口径是否与回测一致",
                    "核对 vector_store 查询特征与回测训练特征是否同源（feature_extractor MATCH_FEATURES）",
                    "回看 backtest_product 环节归因；考虑在 daily_scan 用同一套归一化/条件表重建评分",
                ]
            elif bt_ref < 0.5:
                verdict = "backtest_failure"
                hypotheses = ["回测命中率本身低 → 回测逻辑失效（结局分流/特征拟合/形态识别）",
                              "回测训练样本或特征与目标（次日大涨）不相关"]
                suggestions = [
                    "检查 outcome_tracker 正负分流口径（OUTCOME_WIN/结局门槛）",
                    "检查 feature_extractor 特征与次日上涨的相关性（attribution 归因）",
                    "考虑重跑回测/扩充形态规则库（plans/16）",
                ]
            else:
                verdict = "market_issue"
                hypotheses = ["回测与实盘命中率都不高 → 系统性/市场环境问题或特征已漂移"]
                suggestions = [
                    "结合 market 环节归因判断是否市场环境拖累",
                    "检查 evolution_snapshots 最近命中率漂移（freshness）",
                ]
        else:
            verdict = "insufficient"
            hypotheses = ["暂无实盘次日验证数据，先观察"]
    return {
        "ok": True, "verdict": verdict, "has_backtest": has_backtest,
        "gap": gap,
        "backtest": {
            "accuracy": bt_accuracy, "win_rate": bt_win_rate,
            "avg_t5_return": bt_avg_t5, "total_trades": bt_trades,
            "elapsed": bt.get("elapsed"),
            "ran_at": (bt.get("config") or {}).get("end_date", "") if isinstance(bt.get("config"), dict) else "",
        },
        "note": "口径：每日推荐为 D+1 买入口径（D 日收盘出信号、次日买入，收益自 D+1 起计）；"
                "回测为 T0 收盘买入口径（未做信号滞后修正），回测-实盘差值部分含口径差",
        "stage_trend": stage_attribution_trend(),
        "live": {"t1_hit_rate": live_t1, "t1_samples": len(done1),
                 "t5_hit_rate": live_t5, "t5_samples": len(done5)},
        "hypotheses": hypotheses, "suggestions": suggestions,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def health_text() -> str:
    """回测健康诊断 → 一段文本（供进化大脑 L0/写代码 prompt）。"""
    h = backtest_health()
    bt = h.get("backtest", {})
    lv = h.get("live", {})
    head = (f"回测健康诊断({h.get('verdict')}): "
            f"回测 accuracy={bt.get('accuracy')} win={bt.get('win_rate')} "
            f"T+5均收益={bt.get('avg_t5_return')} 样本={bt.get('total_trades')}")
    if h.get("live", {}).get("t1_hit_rate") is not None:
        head += (f" | 实盘次日T+1命中={lv.get('t1_hit_rate')} "
                 f"T+5命中={lv.get('t5_hit_rate')}(n={lv.get('t5_samples')})")
    if h.get("gap") is not None:
        head += f" | 回测-实盘差值={h.get('gap')}"
    lines = [head]
    if h.get("hypotheses"):
        lines.append("假设: " + "；".join(h["hypotheses"]))
    if h.get("suggestions"):
        lines.append("建议: " + "；".join(h["suggestions"]))
    if h.get("note"):
        lines.append("口径: " + h["note"])
    st = h.get("stage_trend") or {}
    if st.get("ok"):
        lines.append(f"环节归因TOP观察({st.get('latest_date','')}): TOP={st.get('latest_top')} "
                     f"上一期({st.get('prev_date','')})={st.get('prev_top')} → {st.get('note')}")
    elif st.get("note"):
        lines.append(f"环节归因TOP观察: {st.get('note')}")
    return "\n".join(lines)


__all__ = ["is_running", "status", "latest_result", "trigger_backtest",
           "backtest_health", "health_text", "stage_attribution_trend"]
