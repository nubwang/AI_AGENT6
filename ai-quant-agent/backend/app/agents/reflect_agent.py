"""反思层 Agent（reflect_agent）

对齐 plans/03-Agent系统.md §十五：推荐→结果→反思→教训→反馈 闭环（"要越来越聪明"）。

  ① backfill_outcomes(): 回填 monitor_state.hits 的 T+1/T+5 实际涨幅（daily 表前复权）
  ② run_reflection():    对 T+5 未兑现的失败推荐逐只 LLM 反思，固化 reflect_kb
  ③ build_reflect_txt(): 构造"历史失败教训"文本（供精筛 Prompt 参考，高频教训才生效）

知识边界：反思只服务 Agent 判断（参考历史教训），不直接改规则/概率表（防参数漂移）。
"""
from __future__ import annotations

import json
import os
import time

from app.core.logger import logger
from app.backtest.loader import prepare_stock_data
from app.backtest.monitor import STATE_FILE as MONITOR_STATE_FILE, HIT_RET_THRESHOLD
from app.agents import prompts
from app.agents.llm_client import deepseek_chat
from app.agents import evolution_config  # F2：命中阈值/教训次数可进化热生效
from app.agents import failure_context as FCTX  # 外部证据包（大盘/个股日线/个股资料）

# 反思知识库路径：backend/data/reflect_kb.json
REFLECT_KB_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "reflect_kb.json",
)
# 每日榜单目录：backend/data/daily_recommend/
DAILY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "daily_recommend",
)
MIN_COUNT_TO_FEEDBACK = 2   # 教训出现 >= N 次才进入反馈（避免单次偶然）
MAX_REFLECT_LESSONS = 8     # 反馈精筛最多携带的教训条数（控制 token）


# ── 基础状态读写 ─────────────────────────────────────────────
def _load_monitor_state() -> dict:
    try:
        with open(MONITOR_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"hits": []}


def _save_monitor_state(state: dict) -> None:
    try:
        with open(MONITOR_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"监控状态保存失败: {exc}")


def _new_kb() -> dict:
    return {"version": 1, "reflections": [], "stats": {"total_reflected": 0}}


def load_reflect_kb(path: str = REFLECT_KB_FILE) -> dict:
    """加载反思知识库。无/损坏返回空库（不影响主流程）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and "reflections" in d:
            return d
    except Exception:  # noqa: BLE001
        pass
    return _new_kb()


def save_reflect_kb(kb: dict, path: str = REFLECT_KB_FILE) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"反思知识保存失败: {exc}")
        return False


# ── ① 结果回填 ───────────────────────────────────────────────
def _calc_outcome(ts_code: str, rec_date: str) -> dict | None:
    """计算推荐之后的 T+1/T+5 前复权涨幅（**信号滞后修正：D+1 买入口径**）。

    推荐基于推荐日(D)收盘晚上生成，D 日收盘后无法买入，实际 D+1 日买入；
    基准=**D+1 收盘**（用户买入价），t1_ret=买入后持有 1 日、t5_ret=持有 5 日。
    数据不足（停牌/未到）返回 None。
    """
    try:
        df = prepare_stock_data(ts_code)
        if df is None or df.empty:
            return None
        # trade_date 统一为 YYYYMMDD 字符串再比较
        dates = df["trade_date"].dt.strftime("%Y%m%d").tolist()
        idx = None
        for i, d in enumerate(dates):      # 推荐日最近交易日作为 T0
            if d >= rec_date:
                idx = i
                break
        if idx is None:
            return None
        close = df["adj_close"].to_numpy()
        # 信号滞后修正：实际买入日 = D+1
        buy_idx = idx + 1
        if buy_idx >= len(close):
            return None
        base = float(close[buy_idx])
        if base <= 0:
            return None

        def _ret(offset: int) -> float | None:
            j = buy_idx + offset
            if j < len(close):
                return float(close[j] / base - 1.0)
            return None

        return {"t1_ret": _ret(1), "t5_ret": _ret(5)}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"结果回填失败 {ts_code}@{rec_date}: {exc}")
        return None


def backfill_outcomes() -> dict:
    """回填 monitor_state.hits 中 t5_ret 为 None 的推荐（已到 T+5 才回填）。"""
    state = _load_monitor_state()
    hits = state.get("hits", [])
    filled, pending, skipped = 0, 0, 0
    for h in hits:
        if h.get("t5_ret") is not None:
            continue
        oc = _calc_outcome(str(h.get("ts_code", "")), str(h.get("date", "")))
        if oc is None:
            skipped += 1
            continue
        t5 = oc.get("t5_ret")
        if t5 is None:
            pending += 1
            continue                        # 未到 T+5，下次再回填
        h["t1_ret"] = oc.get("t1_ret")
        h["t5_ret"] = round(float(t5), 4)
        hit_thr = float(evolution_config.get_param("hit_threshold", HIT_RET_THRESHOLD)
                        or HIT_RET_THRESHOLD)  # F2 热生效
        h["hit"] = bool(t5 > hit_thr)
        filled += 1
    _save_monitor_state(state)
    logger.info(f"反思回填完成: 回填 {filled} 条，未到T+5 {pending} 条，无数据 {skipped} 条")
    return {"filled": filled, "pending": pending, "skipped": skipped}


# ── ② 失败反思 ───────────────────────────────────────────────
def _load_recommend(rec_date: str, ts_code: str) -> dict:
    """读取某股在某日的推荐记录（Agent 榜单优先，回退规则榜单）。"""
    for fname in (f"daily_{rec_date}_agent.json", f"daily_{rec_date}.json"):
        path = os.path.join(DAILY_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            for p in d.get("top_picks", []):
                if p.get("ts_code") == ts_code:
                    return p
        except Exception:  # noqa: BLE001
            continue
    return {}


def _format_signals(pick: dict) -> str:
    """把命中特征格式化为可读文本。"""
    if not pick:
        return ""
    items = []
    for hc in (pick.get("hit_conditions") or [])[:10]:
        feat = hc.get("feature", "")
        prob = hc.get("up_prob")
        prob_txt = f"{prob * 100:.0f}%" if prob is not None else "?"
        items.append(f"{feat}(条件概率{prob_txt})")
    return "、".join(items) if items else ""


def _norm(s: str) -> str:
    return "".join(str(s).split())


def _upsert_lesson(kb: dict, result: dict, ts_code: str, rec_date: str) -> dict:
    """把一条反思结果并入知识库（同错误类型+同教训去重合并 count++）。"""
    error_type = str(result.get("error_type", "其他"))
    lesson = str(result.get("lesson", "")).strip()
    for r in kb.get("reflections", []):
        if r.get("error_type") == error_type and _norm(r.get("lesson")) == _norm(lesson):
            r["count"] = int(r.get("count", 0)) + 1
            r["last_date"] = rec_date
            r["samples"] = (r.get("samples") or []) + [f"{ts_code}@{rec_date}"]
            return r
    new = {
        "error_type": error_type,
        "lesson": lesson or "未总结",
        "fix": str(result.get("fix", "")).strip(),
        "signal_hint": str(result.get("signal_hint", "")).strip(),
        # 外因/内因分离（来自外部证据包）：外因教训不该被当成"改形态/阈值"的依据
        "external_cause": str(result.get("external_cause", "")).strip(),
        "evidence_basis": str(result.get("evidence_basis", "")).strip(),
        "count": 1,
        "last_date": rec_date,
        "samples": [f"{ts_code}@{rec_date}"],
    }
    kb.setdefault("reflections", []).append(new)
    return new


def run_reflection(limit: int | None = None, progress_cb=None) -> dict:
    """对 T+5 未兑现的失败推荐逐只 LLM 反思，固化 reflect_kb。

    Args:
        limit: 最多反思条数（默认全部失败；可传小值调试降成本）
        progress_cb: 进度回调 fn(idx, total)

    Returns:
        {reflected, failed, skipped, lessons, total_lessons}
    """
    state = _load_monitor_state()
    hits = state.get("hits", [])
    failures = [h for h in hits if h.get("t5_ret") is not None and not h.get("hit")]
    if limit:
        failures = failures[-int(limit):]

    kb = load_reflect_kb()
    reflected = 0
    skipped = 0
    lessons = []
    total = len(failures)
    for i, h in enumerate(failures):
        ts_code = str(h.get("ts_code", ""))
        rec_date = str(h.get("date", ""))
        pick = _load_recommend(rec_date, ts_code)
        name = pick.get("name", "") or ts_code
        reason = pick.get("agent_reason") or "规则推荐（Agent 未精筛该股）"
        signals = _format_signals(pick) or "无"
        t1 = h.get("t1_ret")
        t5 = h.get("t5_ret")
        # 外部证据包（大盘/个股日线/个股资料）——与次日快反馈同一套证据，口径一致
        ev: dict = {}
        try:
            ev = FCTX.build_evidence(ts_code, rec_date)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[reflect] 证据包构建失败 {ts_code}@{rec_date}: {exc}")
        try:
            user = prompts.REFLECT_USER_TMPL.format(
                name=name, ts_code=ts_code, date=rec_date,
                form_type=h.get("form_type", ""),
                prob=round(float(h.get("pred_prob", 0) or 0) * 100, 1),
                reason=reason, signals=signals,
                t1_ret=f"{t1 * 100:.2f}" if t1 is not None else "N/A",
                t5_ret=f"{t5 * 100:.2f}" if t5 is not None else "N/A",
                evidence=FCTX.to_text(ev),
            )
            result = deepseek_chat([
                {"role": "system", "content": prompts.REFLECT_SYSTEM},
                {"role": "user", "content": user},
            ])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"反思调用失败 {ts_code}@{rec_date}: {exc}")
            result = None
        if result is None:
            skipped += 1
        else:
            entry = _upsert_lesson(kb, result, ts_code, rec_date)
            lessons.append({
                "ts_code": ts_code, "date": rec_date,
                "error_type": entry["error_type"], "lesson": entry["lesson"],
                "confidence": str(result.get("confidence", "mid")),
                "external_cause": entry.get("external_cause", ""),
                "evidence_basis": entry.get("evidence_basis", ""),
                "cause_prior": ev.get("cause_prior", ""),
                "evidence": FCTX.summarize(ev),
            })
            reflected += 1
        if progress_cb:
            progress_cb(i + 1, total)
        if (i + 1) % 10 == 0 or i == total - 1:
            logger.info(f"[reflect] 反思进度 {i + 1}/{total}，已反思 {reflected}")

    kb["stats"]["total_reflected"] = int(kb.get("stats", {}).get("total_reflected", 0)) + reflected
    save_reflect_kb(kb)
    # plans/21 §七：T+5 慢反思并入私有域知识库（教训层，供进化大脑/对话综合分析检索）
    try:
        from app.kb import kb_distiller
        kb_distiller.absorb_reflections(kb.get("reflections", []))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[reflect] 知识库吸收反思跳过（不影响反思）: {exc}")
    logger.info(f"反思完成: 失败 {total}，反思 {reflected}，跳过 {skipped}，累计教训 {len(kb['reflections'])} 条")
    return {
        "reflected": reflected, "failed": total, "skipped": skipped,
        "lessons": lessons, "total_lessons": len(kb["reflections"]),
    }


# ── ③ 教训文本（反馈精筛）───────────────────────────────────
def build_reflect_txt(kb: dict | None = None) -> str:
    """构造"历史失败教训"文本（供精筛 Prompt 参考，只取高频教训控制 token）。

    只取 count >= MIN_COUNT_TO_FEEDBACK 的教训（反复出现才值得参考），按次数降序。
    无高频教训返回 ''（精筛降级为仅参考归因/语义）。
    """
    kb = kb or load_reflect_kb()
    min_count = int(evolution_config.get_param("lesson_min_count", MIN_COUNT_TO_FEEDBACK)
                    or MIN_COUNT_TO_FEEDBACK)  # F2 热生效
    refls = [
        r for r in kb.get("reflections", [])
        if int(r.get("count", 0)) >= min_count
    ]
    refls.sort(key=lambda r: -int(r.get("count", 0)))
    if not refls:
        return ""
    lines = []
    for r in refls[:MAX_REFLECT_LESSONS]:
        hint = f"，预警：{r['signal_hint']}" if r.get("signal_hint") else ""
        fix = f"，修正：{r['fix']}" if r.get("fix") else ""
        ec = str(r.get("external_cause") or "")
        ext = f"，外因：{ec}" if ec and "无（内因）" not in ec and "证据不足" not in ec else ""
        lines.append(f"- [{r['count']}次] {r['error_type']}：{r['lesson']}{ext}{hint}{fix}")
    # 外因占比：提醒进化大脑"有多少失败根本不是选股逻辑的错"
    ext_n = sum(1 for r in refls[:MAX_REFLECT_LESSONS]
                if str(r.get("external_cause") or "").strip()
                and "无（内因）" not in str(r.get("external_cause"))
                and "证据不足" not in str(r.get("external_cause")))
    head = f"历史失败教训（反思层，仅参考，不代表未来；其中 {ext_n}/{len(refls[:MAX_REFLECT_LESSONS])} 条属外因/部分外因，不应作为改形态/阈值的依据）：\n"
    return head + "\n".join(lines)


__all__ = [
    "backfill_outcomes", "run_reflection", "build_reflect_txt",
    "load_reflect_kb", "save_reflect_kb", "REFLECT_KB_FILE",
]
