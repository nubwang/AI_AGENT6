"""多 Agent 统一学习能力（learning_runner）

对齐 plans/03-Agent系统.md §20：让每个 Agent 都能自学与反思，统一机制、统一调度。

  - 通用 kb 工具：load_kb / save_kb / upsert_lesson / build_txt（同构知识库）
  - news_lesson_kb：消息面验证 verdict vs T+5 实际 → 烟雾弹/出货误判教训
  - decision_kb：统一决策榜单 vs T+5 命中 → 排序/理由质量教训
  - run_all()：每日统一学习调度（个股反思 + 政策自学 + 消息面反思 + 决策反思）

知识边界：所有教训仅影响判断/风险提示，不改规则概率（防参数漂移）。
"""
from __future__ import annotations

import json
import os
import time

from app.core.logger import logger
from app.agents.llm_client import deepseek_chat
from app.agents.reflect_agent import _calc_outcome
from app.agents import evolution_config  # F2：命中阈值/教训次数可进化热生效

NEWS_LESSON_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "news_lesson_kb.json",
)
NEWS_VERDICTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "news_verdicts.json",
)
DECISION_KB_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "decision_kb.json",
)
DECISION_RECORDS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "decision_records.json",
)

HIT_THRESHOLD = 0.05      # T+5 >5% = 兑现
MIN_COUNT_TO_FEEDBACK = 2  # 教训出现 >= N 次才进入反馈
MAX_LESSONS = 8            # 反馈最多携带教训条数

# ── 反思 Prompt（消息面 / 决策）──
_NEWS_REFLECT_SYSTEM = """你是消息面复盘分析师。某股被消息面验证 Agent 判定为【verdict】，但事后走势与判定相反，说明当时判断有误。
反思为什么误判（烟雾弹漏判？利好出货漏判？消息面本就中性被高估？），得出可复用教训。
输出 JSON 对象（不要任何其他文字）。"""
_NEWS_REFLECT_USER_TMPL = """消息面误判案例：
股票：{name}（{ts_code}）{date} 消息面判定【{verdict}】（烟雾弹:{smoke} 出货:{dist}）
判定理由：{reason}
实际走势：T+5 = {t5_ret}%（预期与判定方向应一致，本次相反）
请输出 JSON：{{"error_type": "烟雾弹漏判/利好出货漏判/消息面误判/其他", "lesson": "一句话教训(≤40字)", "fix": "下次修正(≤50字)", "signal_hint": "预警信号(≤20字)", "confidence": "high/mid/low"}}"""

_DECISION_REFLECT_SYSTEM = """你是量化决策复盘分析师。某股被统一决策 Agent 高分推荐但 T+5 未上涨，说明决策理由有误导。
反思：决策时哪些信号被高估/低估？得出可复用教训。
输出 JSON 对象（不要任何其他文字）。"""
_DECISION_REFLECT_USER_TMPL = """决策失误案例：
股票：{name}（{ts_code}）{date} 决策分 {score} 理由：{reason}
实际走势：T+5 = {t5_ret}%
请输出 JSON：{{"error_type": "高估利好/低估风险/理由不实/排序偏差/其他", "lesson": "一句话教训(≤40字)", "fix": "下次修正(≤50字)", "signal_hint": "预警信号(≤20字)", "confidence": "high/mid/low"}}"""


# ── 通用知识库工具 ─────────────────────────────────────────────
def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"保存失败 {path}: {exc}")


def _norm(s: str) -> str:
    return "".join(str(s).split())


def load_lessons(path: str) -> list[dict]:
    d = _read_json(path, {})
    return d.get("lessons", []) if isinstance(d, dict) else []


def save_lessons(path: str, lessons: list[dict]) -> None:
    _write_json(path, {"version": 1, "lessons": lessons, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})


def upsert_lesson(path: str, agent: str, error_type: str, lesson: str,
                  fix: str = "", signal_hint: str = "", sample: str = "") -> dict:
    """统一教训固化：同 agent+error_type+lesson 去重合并 count++，否则新增。"""
    lessons = load_lessons(path)
    for r in lessons:
        if r.get("agent") == agent and r.get("error_type") == error_type \
                and _norm(r.get("lesson") or "") == _norm(lesson or ""):
            r["count"] = int(r.get("count", 0)) + 1
            r["last_sample"] = sample
            r["samples"] = (r.get("samples") or []) + [sample]
            save_lessons(path, lessons)
            return r
    new = {"agent": agent, "error_type": error_type or "其他", "lesson": lesson or "未总结",
           "fix": fix, "signal_hint": signal_hint, "count": 1, "last_sample": sample,
           "samples": [sample] if sample else []}
    lessons.append(new)
    save_lessons(path, lessons)
    return new


def build_txt(path: str, min_count: int | None = None, max_items: int = MAX_LESSONS) -> str:
    """通用教训反馈文本：只取高频（count>=min_count），按次数降序。

    教训触发次数可进化（F2/E4）：读 evolution_config 的 lesson_min_count，缺省 2。
    """
    if min_count is None:
        min_count = int(evolution_config.get_param("lesson_min_count", MIN_COUNT_TO_FEEDBACK)
                        or MIN_COUNT_TO_FEEDBACK)
    lessons = [r for r in load_lessons(path) if int(r.get("count", 0)) >= min_count]
    lessons.sort(key=lambda r: -int(r.get("count", 0)))
    if not lessons:
        return ""
    lines = []
    for r in lessons[:max_items]:
        hint = f"，预警：{r['signal_hint']}" if r.get("signal_hint") else ""
        fix = f"，修正：{r['fix']}" if r.get("fix") else ""
        lines.append(f"- [{r['count']}次] {r.get('error_type', '')}：{r['lesson']}{hint}{fix}")
    return "历史失败教训（反思层，仅参考，不代表未来）：\n" + "\n".join(lines)


# ── 消息面反思（news_lesson_kb）─────────────────────────────────
def record_news_verdict(ts_code: str, name: str, verdict: str,
                        smoke: str = "无", dist: str = "无", reason: str = "") -> None:
    """消息面验证时记录判断（供 T+5 后反思验证）。"""
    recs = _read_json(NEWS_VERDICTS_FILE, {"records": []})
    recs.setdefault("records", []).append({
        "ts_code": ts_code, "name": name, "date": time.strftime("%Y%m%d"),
        "verdict": verdict, "smoke_risk": smoke, "distribution_risk": dist,
        "reason": reason, "reflected": False,
    })
    if len(recs["records"]) > 3000:
        recs["records"] = recs["records"][-3000:]
    _write_json(NEWS_VERDICTS_FILE, recs)


def backfill_news_reflection(limit: int | None = None) -> dict:
    """T+5 后验证消息面 verdict vs 实际走势 → 对误判案例 LLM 反思 → 固化 news_lesson_kb。"""
    recs = _read_json(NEWS_VERDICTS_FILE, {"records": []})["records"]
    pending = [r for r in recs if not r.get("reflected") and r.get("verdict") in ("利好", "利空")]
    if limit:
        pending = pending[-int(limit):]
    reflected, skipped = 0, 0
    for r in pending:
        oc = _calc_outcome(str(r.get("ts_code", "")), str(r.get("date", "")))
        if oc is None or oc.get("t5_ret") is None:
            skipped += 1
            continue  # 未到 T+5 / 无数据
        t5 = float(oc["t5_ret"])
        verdict = r.get("verdict", "")
        conflict = (verdict == "利好" and t5 < -0.02) or (verdict == "利空" and t5 > 0.02)
        if not conflict:
            r["reflected"] = True
            r["t5_ret"] = round(t5, 4)
            skipped += 1
            continue  # 判定与走势一致，无需反思
        user = _NEWS_REFLECT_USER_TMPL.format(
            name=r.get("name", "") or r.get("ts_code", ""), ts_code=r.get("ts_code", ""),
            date=r.get("date", ""), verdict=verdict,
            smoke=r.get("smoke_risk", "无"), dist=r.get("distribution_risk", "无"),
            reason=r.get("reason", "")[:120], t5_ret=f"{t5 * 100:.2f}",
        )
        result = deepseek_chat([{"role": "system", "content": _NEWS_REFLECT_SYSTEM},
                                {"role": "user", "content": user}])
        r["reflected"] = True
        r["t5_ret"] = round(t5, 4)
        if result:
            upsert_lesson(NEWS_LESSON_FILE, "news", str(result.get("error_type", "")),
                          str(result.get("lesson", "")), str(result.get("fix", "")),
                          str(result.get("signal_hint", "")), f"{r.get('ts_code')}@{r.get('date')}")
            reflected += 1
    _write_json(NEWS_VERDICTS_FILE, {"records": recs})
    logger.info(f"[learning/news] 消息面反思: 误判反思 {reflected}，其余 {skipped}")
    return {"reflected": reflected, "skipped": skipped,
            "total_lessons": len(load_lessons(NEWS_LESSON_FILE))}


def build_news_txt() -> str:
    return build_txt(NEWS_LESSON_FILE)


# ── 决策反思（decision_kb）─────────────────────────────────────
def record_decision_picks(picks: list[dict]) -> None:
    """统一决策最终榜单记录（供 T+5 后反思决策质量）。"""
    if not picks:
        return
    recs = _read_json(DECISION_RECORDS_FILE, {"records": []})
    date = time.strftime("%Y%m%d")
    for p in picks:
        recs.setdefault("records", []).append({
            "ts_code": p.get("ts_code", ""), "name": p.get("name", ""),
            "date": date, "score": float(p.get("agent_score", 0) or 0),
            "reason": str(p.get("agent_reason", ""))[:120], "reflected": False,
        })
    if len(recs["records"]) > 3000:
        recs["records"] = recs["records"][-3000:]
    _write_json(DECISION_RECORDS_FILE, recs)


def backfill_decision_reflection(limit: int | None = None) -> dict:
    """T+5 后验证最终榜单命中 → 对失败的高分推荐 LLM 反思 → 固化 decision_kb。"""
    recs = _read_json(DECISION_RECORDS_FILE, {"records": []})["records"]
    pending = [r for r in recs if not r.get("reflected")]
    if limit:
        pending = pending[-int(limit):]
    reflected, skipped, hit_n, fail_n = 0, 0, 0, 0
    for r in pending:
        oc = _calc_outcome(str(r.get("ts_code", "")), str(r.get("date", "")))
        if oc is None or oc.get("t5_ret") is None:
            skipped += 1
            continue
        t5 = float(oc["t5_ret"])
        r["reflected"] = True
        r["t5_ret"] = round(t5, 4)
        hit_thr = float(evolution_config.get_param("hit_threshold", HIT_THRESHOLD) or HIT_THRESHOLD)  # F2
        r["hit"] = bool(t5 > hit_thr)
        if r["hit"]:
            hit_n += 1
            continue
        fail_n += 1
        # 只反思高分（>=80）且失败（未兑现）的推荐，聚焦决策质量
        if float(r.get("score", 0) or 0) < 80:
            continue
        user = _DECISION_REFLECT_USER_TMPL.format(
            name=r.get("name", "") or r.get("ts_code", ""), ts_code=r.get("ts_code", ""),
            date=r.get("date", ""), score=r.get("score", 0), reason=r.get("reason", ""),
            t5_ret=f"{t5 * 100:.2f}",
        )
        result = deepseek_chat([{"role": "system", "content": _DECISION_REFLECT_SYSTEM},
                                {"role": "user", "content": user}])
        if result:
            upsert_lesson(DECISION_KB_FILE, "decision", str(result.get("error_type", "")),
                          str(result.get("lesson", "")), str(result.get("fix", "")),
                          str(result.get("signal_hint", "")), f"{r.get('ts_code')}@{r.get('date')}")
            reflected += 1
    _write_json(DECISION_RECORDS_FILE, {"records": recs})
    logger.info(f"[learning/decision] 决策反思: 命中 {hit_n}，失败 {fail_n}，反思 {reflected}")
    return {"hit_n": hit_n, "fail_n": fail_n, "reflected": reflected, "skipped": skipped,
            "total_lessons": len(load_lessons(DECISION_KB_FILE))}


def build_decision_txt() -> str:
    return build_txt(DECISION_KB_FILE)


# ── 统一调度 ────────────────────────────────────────────────────
def run_all() -> dict:
    """每日统一学习调度：个股反思 + 政策自学 + 消息面反思 + 决策反思。"""
    from app.agents.reflect_agent import backfill_outcomes, run_reflection
    from app.agents import policy_learning
    results: dict = {}
    try:
        results["reflect"] = {"backfill": backfill_outcomes(), "reflection": run_reflection()}
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"个股反思失败: {exc}")
    try:
        results["policy"] = policy_learning.backfill_sector_impact()
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"政策自学失败: {exc}")
    try:
        results["news"] = backfill_news_reflection()
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"消息面反思失败: {exc}")
    try:
        results["decision"] = backfill_decision_reflection()
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"决策反思失败: {exc}")
    logger.info("[learning] 每日统一学习完成: " + json.dumps(results, ensure_ascii=False, default=str))

    # 进化事件流 + 合并调用（plans/11 §9/I6 + §15.2）：反思结论摘要入事件流，
    # 并惰性触发进化评估（消费反思结果，不重复 LLM 分析；守护暂停时跳过）
    try:
        from app.agents import evolution_events
        summary = {
            "reflect": results.get("reflect", {}),
            "policy": results.get("policy", {}),
            "news": results.get("news", {}),
            "decision": results.get("decision", {}),
        }
        evolution_events.record_event(
            "INFO", "learning",
            "每日统一学习完成: " + json.dumps(summary, ensure_ascii=False, default=str)[:400],
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.agents import evolution_agent, evolution_guard
        if not evolution_guard.is_paused():
            evolution_agent.evaluate()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[learning] 进化评估（合并调用 I6）失败: {exc}")
    return results


__all__ = [
    "record_news_verdict", "backfill_news_reflection", "build_news_txt",
    "record_decision_picks", "backfill_decision_reflection", "build_decision_txt",
    "run_all", "upsert_lesson", "build_txt",
    "NEWS_LESSON_FILE", "DECISION_KB_FILE",
]
