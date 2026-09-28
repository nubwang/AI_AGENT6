"""知识蒸馏层（kb_distiller）— 对齐 plans/21 §5.4 教训/规律 + §九 防退化

职责：把零散案例蒸馏为**可检索、可复用、可信度可更新**的教训（lesson）。

规则（对齐规划）：
  - 升级门槛：support.n ≥ lesson.promote_min_n（默认 30）才允许 status=active（高置信、可影响提案）；
    否则 status=candidate（仅作提示，防把小样本噪声当规律，宪法 C3）。
  - 置信度：按样本量 + 高置信归因占比 + 近期性计算（0~1）。
  - 降权/退役：教训被实验否证（verdict=worse/no_change）达 retire_after_neg_verdicts 次 → retired；
    长期无新样本 → 按 half_life 衰减（保留历史，不删除，防重复踩坑）。
  - 成功侧与失败侧教训分开记账（success_pattern / failure_pattern），支持"成功-失败对照"。

读写说明：蒸馏是"读-改-写"，必须同步执行，故直接使用 kb_store（不走异步 kb_writer）。
"""
from __future__ import annotations

import math
import re
import time

from app.core.logger import logger
from app.kb import kb_store

MAX_SUPPORT_CASES = 60      # 单条教训保留的支持案例 id 上限（滚动）
MAX_SUPPORT_DATES = 40      # 保留样本日期上限
_TXT_MAX = 120              # 教训文本截断


# ── 配置 ─────────────────────────────────────────────────────
def _cfg() -> dict:
    try:
        from app.agents import evolution_config
        return evolution_config.get("knowledge_base", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _promote_min_n() -> int:
    try:
        return int((_cfg().get("lesson", {}) or {}).get("promote_min_n", 30))
    except Exception:  # noqa: BLE001
        return 30


def _retire_after_neg() -> int:
    try:
        return int((_cfg().get("lesson", {}) or {}).get("retire_after_neg_verdicts", 2))
    except Exception:  # noqa: BLE001
        return 2


def _half_life() -> float:
    try:
        return float((_cfg().get("lesson", {}) or {}).get("half_life_days", 120))
    except Exception:  # noqa: BLE001
        return 120.0


# ── 置信度模型 ───────────────────────────────────────────────
def confidence_for(n: int, high_ratio: float = 0.0, fresh_days: float = 0.0) -> float:
    """置信度：样本量为主，高置信归因占比与时效为辅。

    n<10 → ≤0.45（仅提示）；n≥30 → ≥0.70（可影响提案）。
    """
    n = max(0, int(n or 0))
    if n <= 0:
        return 0.0
    if n < 10:
        base = 0.30 + 0.015 * n                 # 0.30 ~ 0.435
    elif n < 30:
        base = 0.45 + 0.008 * (n - 10)          # 0.45 ~ 0.61
    else:
        base = 0.70 + min(0.20, (n - 30) / 400.0)  # 0.70 ~ 0.90
    base += 0.08 * max(0.0, min(1.0, float(high_ratio or 0.0)))
    if fresh_days > 0:
        hl = max(1.0, _half_life())
        base *= max(0.4, math.exp(-fresh_days / hl))
    return round(max(0.0, min(0.98, base)), 3)


def _norm_key(text: str) -> str:
    """教训文本归一化键（去空白/标点/数字，用于跨期合并同类教训）。"""
    # 保留下划线：stage 名（如 backtest_condition）需保持可读、可关联
    t = re.sub(r"[\s，。；、,!.;:：（）()\[\]【】\-—/]", "", str(text or ""))
    t = re.sub(r"\d+", "", t)
    return t[:40]


def lesson_id(type_: str, key: str) -> str:
    """教训 id（确定性；同 key 跨期合并）。"""
    return f"lesson:{type_}:{_norm_key(key)[:48]}"


# ── 案例 → 教训（失败侧）─────────────────────────────────────
def absorb_cases(cases: list[dict]) -> int:
    """把失败案例聚合进教训库（按 环节×错误类型 合并，跨期累积样本量）。

    Returns: 受影响教训条数
    """
    grouped: dict[str, list[dict]] = {}
    for c in cases or []:
        if c.get("side") != "failure":
            continue
        cause = c.get("cause") or {}
        stage = str(cause.get("stage") or c.get("stage") or "other")
        etype = str(cause.get("error_type") or "其他")
        grouped.setdefault(f"{stage}|{etype}", []).append(c)
    touched = 0
    for gkey, items in grouped.items():
        try:
            if _merge_lesson("failure_pattern", gkey, items, side="failure"):
                touched += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[kb_distiller] 合并失败教训异常 {gkey}: {exc}")
    return touched


def absorb_success_cases(cases: list[dict]) -> int:
    """把成功案例聚合进教训库（成功侧；key 用 success_type）。"""
    grouped: dict[str, list[dict]] = {}
    for c in cases or []:
        if c.get("side") != "success":
            continue
        cause = c.get("cause") or {}
        stype = str(cause.get("success_type") or cause.get("error_type") or "其他成功模式")
        grouped.setdefault(stype, []).append(c)
    touched = 0
    for gkey, items in grouped.items():
        try:
            if _merge_lesson("success_pattern", gkey, items, side="success"):
                touched += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[kb_distiller] 合并成功教训异常 {gkey}: {exc}")
    return touched


def absorb_reflections(reflections: list[dict], max_items: int = 800,
                       recent_days: int = 180) -> int:
    """把 reflect_agent 的 T+5 慢反思并入教训库（plans/21 §七：反思同样是知识来源）。

    reflect_kb 条目本身是"累积型"（同教训 count++），故样本量取 **max(已有, count)**，
    重复入库幂等；仅吸收最近 N 天有更新的条目以控成本。
    """
    if not reflections:
        return 0
    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - max(1, recent_days) * 86400))
    items = [r for r in reflections
             if str(r.get("last_date") or "")[:8] >= cutoff][-max_items:]
    touched = 0
    for r in items:
        lesson_text = str(r.get("lesson") or "").strip()
        if not lesson_text:
            continue
        lid = lesson_id("reflection", lesson_text)
        try:
            existing = kb_store.get("lesson", lid) or {}
            support = dict(existing.get("support") or {})
            n = max(int(support.get("n") or 0), int(r.get("count") or 1))
            samples = list(support.get("samples") or [])
            for s in (r.get("samples") or [])[-20:]:
                if s not in samples:
                    samples.append(s)
            support.update({"n": n, "last_date": r.get("last_date"),
                            "samples": samples[-40:],
                            "reflect_error_type": r.get("error_type")})
            conf = confidence_for(n)
            prev_status = str(existing.get("status") or "")
            status = "active" if n >= _promote_min_n() else (prev_status or "candidate")
            if kb_store.upsert("lesson", {
                "id": lid, "ts": existing.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S"),
                "text": lesson_text[:_TXT_MAX], "type": "reflection",
                "scope": {"side": "failure", "error_type": r.get("error_type"),
                          "source": "reflect_agent"},
                "actionable": {"hint": str(r.get("fix") or "")[:120],
                               "signal_hint": str(r.get("signal_hint") or "")[:60]},
                "support": support, "confidence": conf,
                "half_life_days": _half_life(),
                "applied_experiments": existing.get("applied_experiments") or [],
                "effect_verdict": existing.get("effect_verdict"),
                "status": status,
                "last_verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }):
                touched += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[kb_distiller] 吸收反思教训失败 {lid}: {exc}")
    return touched


def _merge_lesson(type_: str, gkey: str, items: list[dict], side: str) -> bool:
    """合并一类案例到一条教训（读取现有 → 累积样本/案例/日期 → 重算置信度与状态）。"""
    lid = lesson_id(type_, gkey)
    existing = kb_store.get("lesson", lid) or {}
    support = dict(existing.get("support") or {})
    case_ids = list(support.get("cases") or [])
    dates = list(support.get("dates") or [])
    seen = set(case_ids)
    new_n = 0
    high = 0
    form_types: list[str] = []
    texts: list[str] = []
    actions: list[str] = []
    for it in items:
        cid = it.get("id")
        if cid and cid not in seen:
            seen.add(cid)
            case_ids.append(cid)
            new_n += 1
        d = str(it.get("date") or "")[:8]
        if d and d not in dates:
            dates.append(d)
        cause = it.get("cause") or {}
        if str(cause.get("confidence") or "").lower() == "high":
            high += 1
        ft = it.get("form_type")
        if ft and ft not in form_types:
            form_types.append(ft)
        for k in ("lesson", "success_lesson", "stage_hint"):
            v = str(cause.get(k) or "").strip()
            if v:
                texts.append(v)
        for k in ("fix", "optimize", "param_hint", "key_signal"):
            v = str(cause.get(k) or "").strip()
            if v and v != "无需调参":
                actions.append(v)

    prev_n = int(support.get("n") or 0)
    n = prev_n + new_n
    if n <= 0:
        return False
    high_ratio = (high + int(support.get("high_n") or 0)) / float(n)
    newest = max(dates) if dates else str(existing.get("ts") or "")[:8]
    fresh_days = _days_since(newest)
    conf = confidence_for(n, high_ratio, fresh_days)
    min_n = _promote_min_n()
    status = "active" if n >= min_n else "candidate"
    # 已被否证退役的教训：有新样本时回到 candidate 重新观察（不直接复活为 active）
    if existing.get("status") == "retired":
        status = "candidate"
    # 教训文本：优先已有（占位文本除外）→ 归因 lesson → 可操作 fix → 明确标注"待补齐归因"
    prev_text = str(existing.get("text") or "").strip()
    is_placeholder = (not prev_text or prev_text == gkey
                      or "[待归因]" in prev_text
                      or prev_text.replace("|", "") == gkey.replace("|", ""))
    if prev_text and not is_placeholder:
        text = prev_text[:_TXT_MAX]
    elif texts:
        text = texts[0][:_TXT_MAX]
    elif actions:
        text = f"{gkey}：{actions[0]}"[:_TXT_MAX]
    else:
        text = (f"{gkey}：历史案例未归因（样本 {n}），需补齐失效归因后方可用于进化决策"
                f"[待归因]")[:_TXT_MAX]
    scope = dict(existing.get("scope") or {})
    scope.setdefault("side", side)
    if type_ == "failure_pattern":
        stage, etype = (gkey.split("|", 1) + [""])[:2]
        scope["stage"] = stage or scope.get("stage")
        scope["error_type"] = etype or scope.get("error_type")
    else:
        scope["success_type"] = gkey
    scope["form_types"] = sorted(set((scope.get("form_types") or []) + form_types))
    actionable = dict(existing.get("actionable") or {})
    if actions and not actionable.get("hint"):
        actionable["hint"] = actions[0][:120]
    rec = {
        "id": lid,
        "ts": existing.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "text": text,
        "type": type_,
        "scope": scope,
        "actionable": actionable,
        "support": {
            "cases": case_ids[-MAX_SUPPORT_CASES:],
            "dates": dates[-MAX_SUPPORT_DATES:],
            "n": n, "high_n": high + int(support.get("high_n") or 0),
            "last_date": newest,
        },
        "confidence": conf,
        "half_life_days": _half_life(),
        "applied_experiments": existing.get("applied_experiments") or [],
        "effect_verdict": existing.get("effect_verdict"),
        "status": status,
        "last_verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return kb_store.upsert("lesson", rec)


def archive_stale(days: int = 365) -> int:
    """归档长期未被复现的**单例候选**教训（plans/21 §六：压缩不删事实）。

    条件：status=candidate + 样本 n=1 + 最后出现时间早于 days 天前 → status=archived。
    归档后默认不出现在检索与 L0（避免历史噪声稀释当前认知），但记录仍在库中可查；
    有新样本时 `absorb_cases` 会把它重新变回 candidate（自动复活）。
    """
    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - max(1, days) * 86400))
    n = 0
    for l in kb_store.query("lesson", {"status": "candidate"}, limit=3000):
        support = l.get("support") or {}
        last = str(support.get("last_date") or "")[:8]
        if not last or last >= cutoff:
            continue
        if int(support.get("n") or 0) > 1:
            continue
        if kb_store.upsert("lesson", {**l, "status": "archived",
                                      "last_verified_at": time.strftime("%Y-%m-%d %H:%M:%S")}):
            n += 1
    if n:
        logger.info(f"[kb_distiller] 归档长期单例教训 {n} 条（>={days} 天未复现）")
    return n


# ── 实验结论回馈教训（升权/降权/退役）────────────────────────
def record_verdict(lesson_ids: list[str], experiment_id: str, verdict: str) -> int:
    """把实验结论回写教训：supported（升权）/ refuted（降权，达阈值退役）。"""
    if not lesson_ids:
        return 0
    touched = 0
    for lid in lesson_ids:
        try:
            l = kb_store.get("lesson", lid)
            if not l:
                continue
            applied = list(l.get("applied_experiments") or [])
            if experiment_id and experiment_id not in applied:
                applied.append(experiment_id)
            conf = float(l.get("confidence") or 0.0)
            neg = int((l.get("support") or {}).get("neg_n") or 0)
            if verdict == "improved":
                conf = min(0.98, conf + 0.05)
                status = "active"
                eff = "improved"
            elif verdict in ("worse", "no_change"):
                conf = max(0.0, conf - 0.12)
                neg += 1
                eff = verdict
                status = "retired" if neg >= _retire_after_neg() else "weakened"
            else:
                eff = verdict or l.get("effect_verdict")
                status = l.get("status") or "candidate"
            support = dict(l.get("support") or {})
            support["neg_n"] = neg
            ok = kb_store.upsert("lesson", {
                "id": lid, "ts": l.get("ts"), "text": l.get("text"), "type": l.get("type"),
                "scope": l.get("scope"), "actionable": l.get("actionable"), "support": support,
                "confidence": round(conf, 3), "half_life_days": l.get("half_life_days"),
                "applied_experiments": applied, "effect_verdict": eff, "status": status,
                "last_verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            touched += 1 if ok else 0
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[kb_distiller] 回写教训结论失败 {lid}: {exc}")
    return touched


def decay_all() -> int:
    """时效衰减：长期无新样本的教训按 half_life 降权；过低 → weakened。

    建议低频调用（如每周一次），避免频繁全表写。
    """
    touched = 0
    for l in kb_store.query("lesson", {"status": "active"}, limit=1000):
        try:
            last = str(((l.get("support") or {}).get("last_date")) or "")[:8]
            if not last:
                continue
            fresh = _days_since(last)
            if fresh <= _half_life() / 2:
                continue
            conf = confidence_for(int((l.get("support") or {}).get("n") or 0),
                                  ((l.get("support") or {}).get("high_n") or 0) /
                                  max(1, int((l.get("support") or {}).get("n") or 1)),
                                  fresh)
            if conf >= float(l.get("confidence") or 0):
                continue
            status = "active" if conf >= 0.7 else ("weakened" if conf < 0.5 else "active")
            if kb_store.upsert("lesson", {**l, "confidence": conf, "status": status,
                                          "last_verified_at": time.strftime("%Y-%m-%d %H:%M:%S")}):
                touched += 1
        except Exception:  # noqa: BLE001
            continue
    return touched


def _days_since(date_str: str) -> float:
    try:
        t = time.strptime(date_str, "%Y%m%d")
        return max(0.0, (time.time() - time.mktime(t)) / 86400.0)
    except Exception:  # noqa: BLE001
        return 0.0


def stats() -> dict:
    """教训库总览。"""
    out = {
        "n": kb_store.count("lesson"),
        "by_status": {r["k"]: r["n"] for r in kb_store.group_count("lesson", "status")},
        "by_type": {r["k"]: r["n"] for r in kb_store.group_count("lesson", "type")},
        "promote_min_n": _promote_min_n(),
        "top": [
            {"id": l.get("id"), "text": l.get("text"), "n": (l.get("support") or {}).get("n"),
             "confidence": l.get("confidence"), "status": l.get("status")}
            for l in sorted(kb_store.query("lesson", limit=200),
                            key=lambda x: (-float(x.get("confidence") or 0),))
            [:10]
        ],
    }
    return out


__all__ = ["absorb_cases", "absorb_success_cases", "absorb_reflections",
           "record_verdict", "decay_all", "archive_stale",
           "confidence_for", "lesson_id", "stats"]
