"""进化守护（evolution_guard）— 对齐 plans/11 进化Agent §8.2/H1/I2

独立监督模块（不受进化 Agent 控制，进化 Agent 无法禁用/修改本模块）：
  - 宪法哈希校验：evolution_config 的宪法部分（main_metric/thresholds/protected_files）哈希基线，防篡改
  - 越权拦截：进化 Agent 提案落在保护区 → 记录拦截
  - 频率监控：改进频率异常 / 连续失败 → 触发冷却
  - 主指标监控：命中率持续下降 → 建议暂停 + 回滚最近改进
  - 自身运维（I2）：进化系统健康（事件库容量 / 影子记录量 / 守护自身）
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from app.core.logger import logger
from app.agents import evolution_config
from app.agents import evolution_summarizer
from app.agents import evolution_events
from app.agents import evolution_shadow

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
GUARD_STATE = os.path.join(DATA_DIR, "evolution_guard_state.json")

# 宪法部分（不可被进化 Agent 修改；哈希基线防篡改）
# Bug38：approval_mode 是用户手动控制的运行模式开关（manual/auto 切换属正常操作），
#        不在宪法哈希内 —— 否则用户切换审批模式会被守护误判"篡改"而自动暂停进化
CONSTITUTION_KEYS = ("main_metric", "thresholds", "protected_files")

# 阈值（可配置，MVP 用常量）
MAX_APPLY_PER_WEEK = 3          # 每周最多应用改进数（频率监控）
MAX_CONSECUTIVE_FAIL = 3        # 连续失败阈值 → 冷却
HIT_RATE_DROP_PP = 5            # 命中率下降 5pp → 暂停建议
EVENTS_MAX_ROWS = 100000        # 事件库容量上限


def _read_state() -> dict:
    try:
        with open(GUARD_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"constitution_hash": None, "paused": False, "intercepts": 0,
                "applied_log": [], "fail_log": []}


def _write_state(st: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(GUARD_STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution_guard] 状态写入失败: {exc}")


def _constitution_digest() -> str:
    """宪法部分哈希（主指标/门槛/保护区/审批模式）。"""
    cfg = evolution_config._load()
    part = json.dumps({k: cfg.get(k) for k in CONSTITUTION_KEYS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(part.encode()).hexdigest()[:24]


def ensure_baseline() -> None:
    """初始化/更新宪法哈希基线（首次或人工确认后）。"""
    st = _read_state()
    st["constitution_hash"] = _constitution_digest()
    _write_state(st)


def check_constitution() -> dict:
    """宪法完整性校验：当前哈希 vs 基线。不一致 → 篡改风险，建议暂停。"""
    st = _read_state()
    base = st.get("constitution_hash")
    if not base:
        ensure_baseline()
        return {"ok": True, "note": "已初始化宪法基线"}
    cur = _constitution_digest()
    if cur != base:
        return {"ok": False, "tampered": True,
                "reason": "宪法部分（主指标/门槛/保护区）被改动，非人工确认"}
    return {"ok": True}


def record_apply(param: str, status: str) -> None:
    """记录一次改进应用/失败（频率 + 连续失败监控）。"""
    st = _read_state()
    st.setdefault("applied_log", []).append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "param": param, "status": status})
    if status == "failed":
        st.setdefault("fail_log", []).append(time.strftime("%Y-%m-%d %H:%M:%S"))
    st["applied_log"] = st["applied_log"][-200:]
    st["fail_log"] = st["fail_log"][-100:]
    _write_state(st)
    # plans/21 §七：应用失败 → 知识库实验台账标记 abandoned（留痕，防重复踩坑）
    if status != "ok":
        try:
            from app.kb import kb_ingest
            kb_ingest.finalize_param_experiments(
                param, "abandoned", f"应用未成功（守护记录 status={status}）")
        except Exception:  # noqa: BLE001
            pass


def record_intercept() -> None:
    st = _read_state()
    st["intercepts"] = st.get("intercepts", 0) + 1
    _write_state(st)


def frequency_check() -> dict:
    """频率监控：本周应用数 / 连续失败数。超限 → 冷却建议。"""
    st = _read_state()
    week_applies = sum(1 for r in st.get("applied_log", [])
                       if r.get("ts", "")[:10] >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400)))
    fails = st.get("fail_log", [])
    recent_fails = sum(1 for t in fails if t[:10] >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400)))
    cool = week_applies >= MAX_APPLY_PER_WEEK or recent_fails >= MAX_CONSECUTIVE_FAIL
    return {"week_applies": week_applies, "recent_fails": recent_fails,
            "cooldown_suggested": cool,
            "note": "改进频率/连续失败超限，建议冷却" if cool else "频率正常"}


def main_metric_check() -> dict:
    """主指标监控：综合目标分（次日大涨+主升浪）较基线下降超阈值 → 暂停 + 回滚建议。

    用户核心诉求"推荐必须涨、最好主升浪"→ 主指标用综合目标分 composite
    （monitor.composite = t1×（1-w）+wave×w），基线优先 composite，缺省回退旧 hit_rate。
    """
    try:
        m = evolution_summarizer.build_l0_summary().get("monitor", {})
        composite = m.get("composite", m.get("hit_rate", 0.0))
        hit_rate = m.get("hit_rate", 0.0)
        samples = m.get("samples", 0)
        eff = evolution_shadow._read_json(evolution_shadow.EFFECT_FILE, {"records": []})
        recs = [r for r in eff.get("records", []) if r.get("status") == "tracking"]
        if not recs or samples < 30:
            return {"ok": True, "note": "样本不足，暂不判定"}
        baseline = recs[-1].get("baseline_composite", recs[-1].get("baseline_hit_rate", 0.0))
        drop = baseline - composite
        pause_suggested = drop > HIT_RATE_DROP_PP / 100.0
        if pause_suggested:
            # plans/21 §七：主指标持续下降 → 把最近一次生效改动判为 worse（含未达预期原因）
            try:
                from app.kb import kb_ingest
                last_param = ""
                for r in reversed(_read_state().get("applied_log", [])):
                    if r.get("status") == "ok" and r.get("param"):
                        last_param = str(r["param"])
                        break
                if last_param:
                    kb_ingest.finalize_param_experiments(
                        last_param, "worse",
                        f"主指标（综合目标分）较基线下降 {round(drop * 100, 2)}pp，"
                        f"守护建议暂停并回滚（plans/21 未达预期判定）",
                        live={"current_composite": composite, "baseline": baseline,
                              "drop_pp": round(drop * 100, 2)})
            except Exception:  # noqa: BLE001
                pass
        return {"ok": not pause_suggested, "baseline": baseline, "current": composite,
                "hit_rate": hit_rate, "drop_pp": round(drop * 100, 2),
                "pause_suggested": pause_suggested}
    except Exception:  # noqa: BLE001
        return {"ok": True, "note": "主指标检查跳过"}


def health_check() -> dict:
    """自身运维（I2）：事件库/影子/守护自身健康。"""
    st = _read_state()
    try:
        db = evolution_events._conn()
        n = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        db.close()
    except Exception:  # noqa: BLE001
        n = 0
    shadow_n = len(evolution_shadow.shadow_status())
    return {"events_rows": n, "events_over_cap": n > EVENTS_MAX_ROWS,
            "shadow_running": shadow_n,
            "paused": st.get("paused", False),
            "intercepts": st.get("intercepts", 0)}


def freshness_check() -> dict:
    """规律新鲜度（H7）：对比 evolution_snapshots 最近两轮命中率漂移，给出重算建议。"""
    try:
        import os
        snaps = evolution_summarizer._read_json(evolution_summarizer.SNAPSHOT_FILE, [])
        if len(snaps) < 2:
            return {"ok": True, "note": "快照不足，暂不判定"}
        cur = snaps[-1].get("hit_rate", 0.0) or 0.0
        prev = snaps[-2].get("hit_rate", 0.0) or 0.0
        drift_pp = round((cur - prev) * 100, 2)
        stale = drift_pp < -HIT_RATE_DROP_PP
        return {"ok": not stale, "prev_hit_rate": prev, "current_hit_rate": cur,
                "drift_pp": drift_pp,
                "recalc_suggested": stale,
                "note": "命中率明显回落，建议重算 probability_table 验证规律新鲜度（H7）" if stale else "规律新鲜度正常"}
    except Exception:  # noqa: BLE001
        return {"ok": True, "note": "规律新鲜度检查跳过"}


def guard_scan() -> dict:
    """完整巡检报告（供定时任务/API）。"""
    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "constitution": check_constitution(),
        "frequency": frequency_check(),
        "main_metric": main_metric_check(),
        "freshness": freshness_check(),
        "health": health_check(),
    }


def pause(reason: str = "") -> None:
    st = _read_state()
    st["paused"] = True
    st["pause_reason"] = reason
    st["paused_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_state(st)
    logger.warning(f"[evolution_guard] 进化已暂停: {reason}")


def resume() -> None:
    st = _read_state()
    st["paused"] = False
    st["paused_at"] = None
    _write_state(st)


def is_paused() -> bool:
    return _read_state().get("paused", False)


__all__ = ["guard_scan", "check_constitution", "record_apply", "record_intercept",
           "frequency_check", "main_metric_check", "health_check", "pause", "resume", "is_paused"]
