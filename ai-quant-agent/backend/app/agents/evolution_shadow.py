"""进化影子实验 + 回测模拟 + 效果追踪（evolution_shadow）— 对齐 plans/11 §7.5/§12/F1/F3

MVP 补齐（第 1 阶段完整）：
  - 简化影子实验（F1：先验证后生效）：start_shadow（提案进影子期，不立即生效）
    → policy_agent 旁路记录新旧判定（record_verdict）
    → settle_shadow（对比新旧判定命中，显著性裁决 → applied / rolled_back / running）
  - 回测模拟 F3：simulate_threshold（基于 monitor 命中基线 + 参数方向估算影响面，供宪法校验）
  - 效果追踪：record_baseline / track_effect（应用后对比 monitor 命中率）

适用范围（G1）：仅确定性参数（Tier1）可走影子实验。
"""
from __future__ import annotations

import json
import os
import threading
import time

from app.core.logger import logger
from app.agents import evolution_config
from app.agents import evolution_summarizer

# Bug7 修复：record_verdict 读-改-写加锁，避免并发丢更新
_verdict_lock = threading.Lock()

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
SHADOW_FILE = os.path.join(DATA_DIR, "evolution_shadow.json")
from app.agents.improve_effect import EFFECT_FILE, record_baseline, track_effect  # 复用独立模块（F7）
MAX_PARALLEL_SHADOWS = 3   # 并行影子实验上限（G6）
MAX_SHADOW_DAYS = 10       # 影子期长上限（天，G6/§11）
MAX_BUY_VERDICTS = 5000    # 纪律类影子最多留多少条买点观察（防文件膨胀）
DISCIPLINE_MIN_SAMPLES = 60  # 纪律类影子每组最少样本（被放弃组/保留组各自要求）


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: str, data) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution_shadow] 写入失败 {path}: {exc}")


def _future_t5(sector: str, date: str) -> float | None:
    """板块在 date 之后的 T+5 涨幅（用政策自学层的板块指数未来涨幅）。"""
    try:
        from app.agents import policy_learning
        ts_code = policy_learning.resolve_sector_index(sector)
        out = policy_learning._index_outcomes(ts_code, str(date).replace("-", ""))
        v = out.get("5")
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


# ── 影子实验 ─────────────────────────────────────────────────
def _running_count(st: dict) -> int:
    return sum(1 for r in st.values() if r.get("status") == "running")


def start_shadow(param: str, old, new, kind: str = "policy") -> dict:
    """把提案进入影子期（不立即生效）。含并行上限（G6）+ 参数冻结（H3）。

    Args:
        kind: 评估器类型，**必须选对**：
            - `"policy"`（默认）：板块/消息类参数，比较"新旧判定对板块 T+5 方向判对没有"；
            - `"discipline"`：纪律/买点阈值类参数（`plan_*` 等），比较
              "**被新阈值放弃的机会** vs **保留的机会**"的个股收益（Welch t）。

        为什么必须区分（实测发现的静默失败）：policy 评估器用 `_future_t5(板块)` 取未来收益，
        纪律参数没有"板块"这一列，verdict 会被全部跳过 → 样本永远不足 →
        10 天后被"影子超期"强制回滚，看起来"跑了实验其实什么都没测"。
    """
    st = _read_json(SHADOW_FILE, {})
    # G6：并行上限 —— 同时最多 MAX_PARALLEL_SHADOWS 个影子实验
    if _running_count(st) >= MAX_PARALLEL_SHADOWS:
        return {"ok": False, "status": "rejected", "reason": "并行影子实验已达上限"}
    # H3：参数冻结 —— 该参数在影子期锁定，禁止其他改动触碰（含人工）
    st[param] = {
        "old": old, "new": new, "kind": kind,
        "start_date": time.strftime("%Y-%m-%d"),
        "status": "running",
        "frozen": True,
        "verdicts": [],
        "buys": [],
    }
    _write_json(SHADOW_FILE, st)
    logger.info(f"[evolution_shadow] {param} 进入影子期: {old} -> {new}（主流程仍旧，已冻结 H3）")
    return {"ok": True, "status": "running", "param": param}


def is_frozen(param: str) -> bool:
    """参数是否在影子期冻结（H3：禁止其他改动触碰）。"""
    rec = _read_json(SHADOW_FILE, {}).get(param)
    return bool(rec and rec.get("status") == "running" and rec.get("frozen"))


def record_verdict(param: str, sector: str, gain: float,
                   old_priced: bool, new_priced: bool, date: str,
                   direction: str = "avoid") -> None:
    """policy_agent 旁路记录一次新旧阈值判定（主流程不改变）。

    Bug22：direction 表示该参数判定的市场含义 —— "avoid"= 判避雷（利好出尽，预期 T+5 弱）
    或 "rebound"= 判回调（超跌反弹，预期 T+5 强）。settle 按方向判定正确性。
    """
    with _verdict_lock:  # Bug7：并发读-改-写保护
        st = _read_json(SHADOW_FILE, {})
        rec = st.get(param)
        if not rec or rec.get("status") != "running":
            return
        rec.setdefault("verdicts", []).append({
            "date": date, "sector": sector, "gain": round(float(gain), 4),
            "old_priced": bool(old_priced), "new_priced": bool(new_priced),
            "direction": direction,
        })
        st[param] = rec
        _write_json(SHADOW_FILE, st)


def record_buy_verdict(param: str, ts_code: str, date: str, gap: float, t5: float) -> None:
    """纪律类影子：记录一次"该股 D+1 高开 gap、按 D+1 开盘买入的 T+5"观察。

    只记**原始观测量**（gap 与收益），不在记录时判新旧是否成交 ——
    因为阈值可能被人工微调，结算时按当时的 old/new 重算才不会出现"记录与判定不一致"。

    调用方：[`plan_tracker.replay_pending`](ai-quant-agent/backend/app/backtest/plan_tracker.py:246)
    （每条回放计划贡献一个观察点）。
    """
    if t5 is None or gap is None:
        return
    with _verdict_lock:  # 与 record_verdict 共用锁，避免并发丢更新
        st = _read_json(SHADOW_FILE, {})
        rec = st.get(param)
        if not rec or rec.get("status") != "running" or rec.get("kind") != "discipline":
            return
        buys = rec.setdefault("buys", [])
        if len(buys) >= MAX_BUY_VERDICTS:
            return
        buys.append({"ts_code": ts_code, "date": date,
                     "gap": round(float(gap), 5), "t5": round(float(t5), 5)})
        st[param] = rec
        _write_json(SHADOW_FILE, st)


def _sync_proposal_status(param: str, status: str) -> None:
    """影子结算后同步候选池提案状态（Bug33：decisions 里 shadowing 提案与影子结果一致）。"""
    try:
        from app.agents import evolution_agent
        recs = evolution_agent._read_decisions()
        changed = False
        for r in recs:
            if r.get("param") == param and r.get("status") == "shadowing":
                r["status"] = status
                changed = True
        if changed:
            evolution_agent._save_decisions(recs)
    except Exception:  # noqa: BLE001
        pass


def _buf_reason(param: str, reason: str, rec: dict, st: dict) -> dict:
    """纪律类影子的强制回滚（期长上限到但样本不足）—— 释放冻结与并行名额。"""
    rec["status"] = "rolled_back"
    rec["frozen"] = False
    rec["settled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    rec["close_reason"] = reason
    _write_json(SHADOW_FILE, st)
    _sync_proposal_status(param, "rolled_back")
    try:
        from app.agents import evolution_guard
        evolution_guard.record_apply(param, "failed")
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"[evolution_shadow] {param} {reason}")
    return {"ok": False, "status": "rolled_back", "reason": reason}


def _settle_discipline(param: str, st: dict, rec: dict) -> dict:
    """纪律/买点类参数的影子结算：**被新阈值放弃的机会 vs 保留的机会**。

    为什么不能复用 policy 的结算：policy 看的是"板块 T+5 方向有没有判对"，
    而纪律参数的决策问题是"被放弃的这批机会是不是**明显更差**"，
    这是完全不同的统计问题（一个是分类正确率，一个是两组收益均值差异）。

    判定：被放弃组均值**更低** 且 Welch t 双侧 p < 0.05 → 收紧有依据 → applied；否则 rolled_back。
    只有"被放弃的更差"才支持收紧；反过来（被放弃的更好）一律回滚，不做"反向投机"。
    """
    try:
        from app.backtest.param_sweep import welch_t
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution_shadow] 纪律结算依赖缺失: {exc}")
        return {"ok": False, "status": "running", "reason": "缺少 param_sweep 依赖"}
    try:
        min_sample = int((evolution_config.get("thresholds", {}) or {}).get(
            "shadow_discipline_min_samples", DISCIPLINE_MIN_SAMPLES)
            or DISCIPLINE_MIN_SAMPLES)
    except Exception:  # noqa: BLE001
        min_sample = DISCIPLINE_MIN_SAMPLES

    old_v, new_v = rec.get("old"), rec.get("new")
    if old_v is None or new_v is None:
        return _buf_reason(param, "影子记录缺少 old/new", rec, st)

    dropped: list[float] = []
    kept: list[float] = []
    for b in (rec.get("buys") or []):
        t5 = b.get("t5")
        gap = b.get("gap")
        if t5 is None or gap is None:
            continue
        g = float(gap)
        if g <= float(new_v):
            kept.append(float(t5))
        elif g <= float(old_v):
            dropped.append(float(t5))

    if len(dropped) < min_sample or len(kept) < min_sample:
        try:
            from datetime import datetime
            elapsed = (datetime.now() - datetime.strptime(
                rec.get("start_date", ""), "%Y-%m-%d")).days
        except Exception:  # noqa: BLE001
            elapsed = 0
        if elapsed >= MAX_SHADOW_DAYS:
            return _buf_reason(
                param, f"纪律影子超 {MAX_SHADOW_DAYS} 天样本仍不足"
                       f"（放弃组 {len(dropped)} / 保留组 {len(kept)}，各需 {min_sample}），强制回滚",
                rec, st)
        return {"ok": False, "status": "running",
                "reason": (f"样本不足（放弃组 {len(dropped)} / 保留组 {len(kept)}，"
                           f"各需 {min_sample}），影子继续（主流程仍旧）")}

    t = welch_t(dropped, kept)
    evidence = (f"放弃组 n={len(dropped)} mean={t['mean_a']} vs 保留组 n={len(kept)} "
                f"mean={t['mean_b']}，Welch t={t['t']} p={t['p']}")
    improved = bool(t["p"] is not None and t["p"] < 0.05
                    and (t["mean_a"] or 0.0) < (t["mean_b"] or 0.0))
    if not improved:
        rec["status"] = "rolled_back"
        rec["frozen"] = False
        rec["settled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rec["close_reason"] = f"被放弃的机会没有显著更差，收紧无依据（{evidence}）"
        rec["result"] = {"t": t}
        _write_json(SHADOW_FILE, st)
        _sync_proposal_status(param, "rolled_back")
        try:
            from app.agents import evolution_guard, evolution_ledger
            evolution_ledger.close_entry(param, "shadow_rolled_back")
            evolution_guard.record_apply(param, "failed")
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"[evolution_shadow] {param} 纪律影子回滚（{evidence}）")
        return {"ok": False, "status": "rolled_back", "n_dropped": len(dropped),
                "n_kept": len(kept), "p": t["p"], "reason": rec["close_reason"]}

    # 生效：与 policy 路径走同一套"备份 + 审计 + 记账 + 守护"
    try:
        from app.agents import self_improver
        bak = self_improver._backup_versions(evolution_config.CONFIG_FILE) \
            if os.path.exists(evolution_config.CONFIG_FILE) else ""
        self_improver._audit({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "param": param,
            "old": old_v, "new": new_v,
            "reason": "纪律影子实验显著更优，正式生效",
            "evidence": evidence, "backup": bak,
            "action": "apply_config", "status": "ok", "via": "shadow_settle_discipline",
        })
    except Exception:  # noqa: BLE001
        pass
    evolution_config.set_param(param, new_v)
    rec["status"] = "applied"
    rec["frozen"] = False
    rec["settled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    rec["result"] = {"t": t}
    _write_json(SHADOW_FILE, st)
    _sync_proposal_status(param, "applied")
    record_baseline(param, new_v)
    try:
        from app.agents import evolution_ledger, evolution_guard
        evolution_ledger.open_entry(param, old_v, new_v)
        evolution_guard.record_apply(param, "ok")
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"[evolution_shadow] {param} 纪律影子显著更优（{evidence}），正式生效")
    return {"ok": True, "status": "applied", "n_dropped": len(dropped),
            "n_kept": len(kept), "p": t["p"], "evidence": evidence}


def settle_shadow(param: str) -> dict:
    """影子期结算：对比新旧判定命中，显著性裁决 → applied / rolled_back / running。

    按 `kind` 分派：policy（板块判定正确率）/ discipline（买点阈值：放弃组 vs 保留组收益）。
    """
    st = _read_json(SHADOW_FILE, {})
    rec = st.get(param)
    if not rec:
        return {"ok": False, "reason": f"无 {param} 影子实验"}
    if rec.get("status") != "running":
        return {"ok": False, "reason": f"{param} 状态 {rec.get('status')}"}

    # 纪律/买点类参数走**另一套评估器**（见 start_shadow 的 kind 说明）
    if rec.get("kind") == "discipline":
        return _settle_discipline(param, st, rec)

    # Bug8 修复：thresholds 可能缺失/None 时回退默认 150
    try:
        min_sample = (evolution_config.get("thresholds", {}) or {}).get("shadow_min_samples", 150)
    except Exception:  # noqa: BLE001
        min_sample = 150
    verdicts = rec.get("verdicts", [])
    evals = []
    for v in verdicts:
        if v.get("old_priced") == v.get("new_priced"):
            continue  # 判定未变化，无评估价值
        t5 = _future_t5(v.get("sector", ""), v.get("date", ""))
        if t5 is None:
            continue
        # Bug22 修复：按方向判定正确性 —— avoid 判避雷应弱；rebound 判回调应强
        direction = v.get("direction", "avoid")
        if direction == "rebound":
            new_correct = (v["new_priced"] == (t5 > 0))
            old_correct = (v["old_priced"] == (t5 > 0))
        else:  # avoid（默认）：判避雷(True) 应 T+5 弱
            new_correct = (v["new_priced"] == (t5 < 0))
            old_correct = (v["old_priced"] == (t5 < 0))
        evals.append({"new_correct": new_correct, "old_correct": old_correct, "t5": t5})

    if len(evals) < min_sample:
        # Bug11：期长上限落地 —— 超过 MAX_SHADOW_DAYS 仍未达样本 → 强制结算（回滚释放冻结/并行名额）
        try:
            from datetime import datetime
            start = datetime.strptime(rec.get("start_date", ""), "%Y-%m-%d")
            elapsed_days = (datetime.now() - start).days
        except Exception:  # noqa: BLE001
            elapsed_days = 0
        if elapsed_days >= MAX_SHADOW_DAYS:
            rec["status"] = "rolled_back"
            rec["frozen"] = False
            rec["settled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            rec["close_reason"] = f"影子期超 {MAX_SHADOW_DAYS} 天样本仍不足，强制回滚（G6 期长上限）"
            _write_json(SHADOW_FILE, st)
            _sync_proposal_status(param, "rolled_back")
            try:
                from app.agents import evolution_guard
                evolution_guard.record_apply(param, "failed")
            except Exception:  # noqa: BLE001
                pass
            logger.info(f"[evolution_shadow] {param} 影子超期（{elapsed_days}天）样本不足，强制回滚释放冻结")
            return {"ok": False, "status": "rolled_back", "reason": rec["close_reason"]}
        return {"ok": False, "status": "running",
                "reason": f"样本 {len(evals)} < {min_sample}，影子继续（主流程仍旧）"}

    n = len(evals)
    new_acc = sum(1 for e in evals if e["new_correct"]) / n
    old_acc = sum(1 for e in evals if e["old_correct"]) / n
    if new_acc - old_acc >= 0.05:  # 提升 ≥5pp（D4）
        # Bug25/Bug30：影子生效也走"版本化备份 + 审计"（对齐 §8.5，每次进化留独立版本可回滚）
        try:
            from app.agents import self_improver
            bak = self_improver._backup_versions(evolution_config.CONFIG_FILE) \
                if os.path.exists(evolution_config.CONFIG_FILE) else ""
            self_improver._audit({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "param": param,
                "old": rec.get("old"), "new": rec["new"],
                "reason": "影子实验显著更优，正式生效",
                "evidence": f"new_acc={new_acc:.3f} vs old_acc={old_acc:.3f}, n={n}",
                "backup": bak, "action": "apply_config", "status": "ok", "via": "shadow_settle",
            })
        except Exception:  # noqa: BLE001
            pass
        evolution_config.set_param(param, rec["new"])
        rec["status"] = "applied"
        rec["frozen"] = False  # H3：结算完成，释放冻结
        rec["settled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(SHADOW_FILE, st)
        _sync_proposal_status(param, "applied")
        record_baseline(param, rec["new"])
        # Bug3/Bug4：终身记账开账 + 守护记录（改进生效）
        try:
            from app.agents import evolution_ledger, evolution_guard
            evolution_ledger.open_entry(param, rec.get("old"), rec["new"])
            evolution_guard.record_apply(param, "ok")
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"[evolution_shadow] {param} 影子显著更优（{new_acc:.3f} vs {old_acc:.3f}），正式生效")
        return {"ok": True, "status": "applied", "new_acc": round(new_acc, 4), "old_acc": round(old_acc, 4), "n": n}
    rec["status"] = "rolled_back"
    rec["frozen"] = False  # H3：结算完成，释放冻结
    rec["settled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(SHADOW_FILE, st)
    _sync_proposal_status(param, "rolled_back")
    # Bug3：影子回滚 → 终身记账结账（若此前有记账则结清）
    try:
        from app.agents import evolution_ledger, evolution_guard
        evolution_ledger.close_entry(param, "shadow_rolled_back")
        evolution_guard.record_apply(param, "failed")
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"[evolution_shadow] {param} 影子未显著更优（{new_acc:.3f} vs {old_acc:.3f}），回滚")
    return {"ok": False, "status": "rolled_back", "new_acc": round(new_acc, 4), "old_acc": round(old_acc, 4), "n": n}


def shadow_status() -> dict:
    """当前影子实验状态。"""
    return _read_json(SHADOW_FILE, {})


# ── 回测模拟（F3）────────────────────────────────────────────
def simulate_threshold(param: str, new) -> dict:
    """F3 回测模拟：估算参数调整的影响面（基于 monitor 命中基线 + 参数方向），供宪法校验作强证据。"""
    s = evolution_summarizer.build_l0_summary()
    m = s.get("monitor", {})
    current = evolution_config.get_param(param)
    direction = "下调" if (new or 0) < (current or 0) else "上调"
    return {
        "baseline_hit_rate": m.get("hit_rate", 0.0),
        "baseline_samples": m.get("samples", 0),
        "direction": direction,
        "delta": round(float(new) - float(current), 4) if current is not None else None,
        "impact_note": f"{param} 从 {current} 调至 {new}（{direction}），判定翻转面随阈值移动而变化",
        "estimated": "需影子实验验证实际命中变化",
    }


# ── 效果追踪 ─────────────────────────────────────────────────
__all__ = ["start_shadow", "record_verdict", "record_buy_verdict", "settle_shadow",
           "shadow_status", "is_frozen",
           "simulate_threshold", "record_baseline", "track_effect", "EFFECT_FILE",
           "MAX_PARALLEL_SHADOWS", "MAX_SHADOW_DAYS", "MAX_BUY_VERDICTS",
           "DISCIPLINE_MIN_SAMPLES"]
