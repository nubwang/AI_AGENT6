"""次日自我验证（daily_verify）— 对齐 plans/17 进化中心改造（用户核心想法）

用户想法：当天做每日推荐，第二天出现新的日线行情就"自我验证"，验证
"为啥每日推荐的股票没有涨"；进化中心据此修改回测与每日推荐逻辑，
进化成"高概率上涨的每日推荐的股票"。

本模块（MVP）：
  ① backfill_t1()         : 对 monitor_state.hits 回填 T+1 实际涨幅（次日快反馈，替代 T+5 慢反馈）
  ② run_daily_verify()    : 取最近 N 个交易日榜单 → 识别"推荐了但次日没涨"的失败案例
                             → 逐只 LLM 失效归因 → 聚合归因类型 → 提取改进方向
                             → 固化 verify_kb.json + 写进化事件（供进化大脑 L0 消费）
  ③ build_verify_txt()    : 构造"次日验证结论"文本（供进化大脑做每日推荐进化决策）

设计原则：
  - 只读不改推荐主流程，不阻塞每日推荐/采集（安全）
  - 归因必须"可参数化可操作"（error_type/lesson/fix/param_hint），供进化中心改逻辑
  - 与 reflect_agent（T+5 慢反思）互补：本模块次日快反馈，两者均入进化事件流
"""
from __future__ import annotations

import json
import os
import time

from app.core.logger import logger
from app.agents import evolution_config
from app.agents import failure_context as FCTX
from app.agents.llm_client import deepseek_chat
from app.backtest.monitor import STATE_FILE as MONITOR_STATE_FILE
from app.backtest.loader import prepare_stock_data

# 数据目录：backend/data/
DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
VERIFY_KB_FILE = os.path.join(DATA_DIR, "verify_kb.json")
DAILY_DIR = os.path.join(DATA_DIR, "daily_recommend")

# 默认值（均可被 evolution_config 覆盖）
T1_HIT_THRESHOLD = 0.0        # 次日上涨判定：D+1 买入后持有 1 日（T+1）前复权涨幅 > 0 视为次日上涨（可进化）
VERIFY_LOOKBACK_DAYS = 1      # 默认验证最近 1 个交易日榜单（可进化）
MAX_ATTRIBUTE_FAILURES = 20   # 单期最多归因失败案例数（控制 token，0=全部）
VERIFY_KB_MAX = 60            # verify_kb 保留期数（滚动）


# ── 基础读写 ─────────────────────────────────────────────────
def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _load_monitor_state() -> dict:
    return _read_json(MONITOR_STATE_FILE, {"hits": []})


def _save_monitor_state(state: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(MONITOR_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[daily_verify] monitor_state 保存失败: {exc}")


def _new_kb() -> dict:
    return {"version": 1, "verifications": [], "stats": {"verified_days": 0, "total_failures": 0}}


def load_verify_kb() -> dict:
    kb = _read_json(VERIFY_KB_FILE, None)
    if isinstance(kb, dict) and "verifications" in kb:
        return kb
    return _new_kb()


def save_verify_kb(kb: dict) -> bool:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(VERIFY_KB_FILE, "w", encoding="utf-8") as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[daily_verify] verify_kb 保存失败: {exc}")
        return False


def _t1_threshold() -> float:
    try:
        return float(evolution_config.get_param("t1_hit_threshold", T1_HIT_THRESHOLD) or T1_HIT_THRESHOLD)
    except Exception:  # noqa: BLE001
        return T1_HIT_THRESHOLD


def _lookback_days() -> int:
    try:
        return int(evolution_config.get_param("verify_lookback_days", VERIFY_LOOKBACK_DAYS) or VERIFY_LOOKBACK_DAYS)
    except Exception:  # noqa: BLE001
        return VERIFY_LOOKBACK_DAYS


# ── ① 多周期结果回填（T+1 次日大涨 / T+5、T+20 主升浪）─────────
def _calc_multi(ts_code: str, rec_date: str) -> dict | None:
    """计算推荐之后的 T+1/T+5/T+20 前复权涨幅（**信号滞后修正：D+1 买入口径**）。

    信息滞后（用户核心诉求）：每日推荐基于推荐日(D)收盘数据在晚上才生成，D 日收盘后
    已无法买入，实际只能 **D+1 日买入**。因此基准=**D+1 收盘**（用户买入价），
    收益为"买入后 N 个交易日"：
      - t1  = D+1 买入，持有 1 日（D+2 收盘 vs D+1 收盘）
      - t5  = 持有 5 日（D+6 收盘 vs D+1 收盘）
      - t20 = 持有 20 日（D+21 收盘 vs D+1 收盘）
    避免把"D→D+1"这段用户吃不到的涨幅误当成推荐成绩（前瞻偏移/look-ahead bias）。

    Returns: {"t1": float|None, "t5": float|None, "t20": float|None}
      - 数据不足/未到的周期为 None（后续轮次继续回填）
    """
    try:
        df = prepare_stock_data(ts_code)
        if df is None or df.empty:
            return None
        dates = df["trade_date"].dt.strftime("%Y%m%d").tolist()
        idx = None
        for i, d in enumerate(dates):
            if d >= rec_date:
                idx = i
                break
        if idx is None:
            return None
        close = df["adj_close"].to_numpy()
        # 信号滞后修正：实际买入日 = 推荐日 D 之后第 1 个交易日（D+1）
        buy_idx = idx + 1
        if buy_idx >= len(close):
            return None
        base = float(close[buy_idx])
        if base <= 0:
            return None

        def _ret(offset: int) -> float | None:
            j = buy_idx + offset
            return float(close[j] / base - 1.0) if j < len(close) else None

        return {"t1": _ret(1), "t5": _ret(5), "t20": _ret(20)}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[daily_verify] 多周期计算失败 {ts_code}@{rec_date}: {exc}")
        return None


def backfill_t1(force: bool = False) -> dict:
    """回填 monitor_state.hits 的 T+1/T+5/T+20 前复权涨幅（**信号滞后修正：D+1 买入口径**）。

    force=True：对已回填记录也重新计算（口径修正后全量对齐，避免新旧口径混合）。
    已到次日必回填 t1；T+5/T+20 数据够才回填（供"主升浪"验证）。
    """
    state = _load_monitor_state()
    hits = state.get("hits", [])
    filled, pending = 0, 0
    for h in hits:
        if not force and (h.get("t1_ret") is not None and h.get("t5_ret") is not None
                          and h.get("t20_ret") is not None):
            continue
        oc = _calc_multi(str(h.get("ts_code", "")), str(h.get("date", "")))
        if oc is None or oc.get("t1") is None:
            pending += 1   # 未到次日 / 无数据
            continue
        changed = False
        if (force or h.get("t1_ret") is None) and oc.get("t1") is not None:
            h["t1_ret"] = round(float(oc["t1"]), 4)
            changed = True
        if (force or h.get("t5_ret") is None) and oc.get("t5") is not None:
            h["t5_ret"] = round(float(oc["t5"]), 4)
            # BugFix：同步更新监控命中标记（T+5 命中 = 买入后 5 日涨幅 > hit_threshold），
            # 否则 monitor._monitor_stats / 监控基线用 hit 字段统计 T+5 命中率会失真（恒低）。
            try:
                _hit_thr = float(evolution_config.get_param("hit_threshold", 0.05) or 0.05)
            except Exception:  # noqa: BLE001
                _hit_thr = 0.05
            h["hit"] = bool(float(h["t5_ret"]) > _hit_thr)
            changed = True
        if (force or h.get("t20_ret") is None) and oc.get("t20") is not None:
            h["t20_ret"] = round(float(oc["t20"]), 4)
            changed = True
        if changed:
            h["t1_verified_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            filled += 1
    _save_monitor_state(state)
    if filled or pending:
        logger.info(f"[daily_verify] 多周期回填(force={force}): 回填 {filled} 条，未到/无数据 {pending} 条")
    return {"filled": filled, "pending": pending, "skipped": 0}


def _wave_threshold() -> float:
    """主升浪判定阈值：推荐后 T+5 前复权涨幅 ≥ 此值判走出主升浪（可进化 wave_hit_threshold）。"""
    try:
        return float(evolution_config.get_param("wave_hit_threshold", 0.05) or 0.05)
    except Exception:  # noqa: BLE001
        return 0.05


# ── 榜单加载（Agent 版优先，回退规则版，支持 v 版本）──────────
def _list_report_files() -> list[str]:
    """列出全部榜单文件路径（按日期升序）。"""
    import glob
    import re
    files = glob.glob(os.path.join(DAILY_DIR, "daily_*.json"))
    parsed = []
    for f in files:
        base = os.path.basename(f)
        m = re.match(r"^daily_(\d{8})(?:_agent)?(?:_v(\d+))?\.json$", base)
        if m:
            parsed.append((m.group(1), 1 if "_agent" in base else 0, int(m.group(2) or 0), f))
    parsed.sort(key=lambda x: (x[0], x[1], x[2]))
    return [p[3] for p in parsed]


def _load_report_detail(rec_date: str) -> dict:
    """加载某日榜单：{ts_code: pick}。

    规则扫描版为主（与 monitor_state.hits 的命中登记同源，覆盖 TOP-K 全量），
    Agent 精筛版合并补充（agent_reason/最终排序等 detail 字段）。
    """
    import glob
    import re

    def _latest_file(patterns: list[str]) -> str | None:
        files: list[str] = []
        for pat in patterns:
            files.extend(glob.glob(os.path.join(DAILY_DIR, pat)))
        if not files:
            return None

        def _ver(f: str) -> int:
            m = re.search(r"_v(\d+)\.json$", f)
            return int(m.group(1)) if m else 0

        files.sort(key=_ver)
        return files[-1]

    def _read_picks(path: str | None) -> dict:
        if not path:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:  # noqa: BLE001
            return {}
        return {str(p.get("ts_code", "")): p for p in d.get("top_picks", [])}

    rule_picks = _read_picks(_latest_file([f"daily_{rec_date}.json", f"daily_{rec_date}_v*.json"]))
    agent_picks = _read_picks(_latest_file([f"daily_{rec_date}_agent.json", f"daily_{rec_date}_agent_v*.json"]))
    detail: dict[str, dict] = dict(rule_picks)
    for code, pick in agent_picks.items():
        if code in detail:
            detail[code] = {**detail[code], **pick}   # Agent detail 补充（不覆盖规则主字段）
        else:
            detail[code] = pick
    return detail


def _format_pick(pick: dict) -> str:
    """把推荐依据格式化为可读文本（供 LLM 归因）。"""
    if not pick:
        return "（无该股榜单详情）"
    parts = [
        f"形态 {pick.get('form_type', '?')}",
        f"相似度 {pick.get('similarity', 0):.3f}",
        f"综合上涨概率 {float(pick.get('up_probability', 0) or 0) * 100:.1f}%",
        f"条件概率 {float(pick.get('positive_score', 0) or 0) * 100:.1f}%",
        f"负向抑制 {pick.get('negative_score', 0):.3f}",
    ]
    rules = pick.get("hit_rules") or []
    if rules:
        parts.append("规则命中: " + "、".join(f"{r.get('name','')}({r.get('hit_rate_t1',0)*100:.0f}%)" for r in rules[:3]))
    conds = pick.get("hit_conditions") or []
    if conds:
        parts.append("条件命中: " + "、".join(f"{c.get('feature','')}({float(c.get('up_prob',0) or 0)*100:.0f}%)" for c in conds[:6]))
    reason = pick.get("agent_reason")
    if reason:
        parts.append(f"Agent 理由: {str(reason)[:80]}")
    return "；".join(parts)


# ── ② 失效归因（环节级：定位回测/每日推荐流水线的哪个环节出问题）──
# 每日推荐流水线环节 → 对应文件/参数（供进化大脑据此优化逻辑）
STAGE_MAP = {
    "stock_pool": "股票池/数据",
    "form_identify": "形态识别（form_classifier/surge_scanner）",
    "feature_extract": "特征提取（feature_extractor）",
    "pattern_match": "正模式库相似度（vector_store）",
    "neg_exclude": "负向抑制（daily_lambda_exclude）",
    "condition_score": "条件概率打分（threshold_analyzer/condition_table）",
    "rule_signal": "规则信号（rule_verifier/form_leaderboard）",
    "risk_filter": "风险过滤（risk_filter）",
    "rank_select": "排序TOP-K（daily_top_k/daily_scan）",
    "agent_refine": "Agent精筛（refine_agent/decision_agent）",
    # 回测侧环节（plans/19：进化大脑要考虑"是不是回测的问题"）
    "backtest_outcome": "回测结局分流（outcome_tracker 正负判定/OUTCOME_WIN）",
    "backtest_feature": "回测特征拟合（feature_extractor 训练侧）",
    "backtest_pattern": "回测模式库构建（pattern_miner/vector_store 训练侧）",
    "backtest_condition": "回测条件概率表（threshold_analyzer 训练侧）",
    "backtest_normalizer": "回测归一化参数（FeatureNormalizer 训练侧）",
    "backtest_product": "回测产物与每日推荐脱节（normalizer/probability_table/模式库加载口径）",
    "market": "市场环境（系统性）",
    "other": "其他",
}

_ATTRIBUTE_SYSTEM = """你是一名A股量化每日推荐复盘分析师。任务：剖析"昨天被系统推荐、今天（次日）却没有上涨"的失败案例，
定位是**回测与每日推荐流水线的哪个环节**出了问题（环节见 STAGE_MAP），并给出该环节的可操作优化方向，
以便进化大脑改进回测/每日推荐逻辑，让推荐的股票更可能上涨。
必须具体、可操作，禁止事后诸葛式泛泛而谈。
输出 JSON 对象（不要任何其他文字）。"""

_ATTRIBUTE_USER_TMPL = """失败推荐案例（次日未上涨）：
股票：{name}（{ts_code}），推荐日 {date}，推荐依据：{pick_txt}

次日（T+1，信号滞后修正：D+1 买入后持有 1 个交易日）前复权涨幅：{t1_ret}%（预期应 > {thr}%，本次未达标）

外部证据（来自**历史大盘/个股日线/个股资料**的复盘证据，与回测同源；证据缺口已显式标注）：
{evidence}

归因要求（重要）：
1) 先判断本次失败主要是【外因】还是【内因】：
   - 外因：大盘系统性下跌（个股只是跟跌）、个股利空事件（业绩预减/减持/高质押/大宗折价）、技术破位；
   - 内因：形态识别误判、相似度虚高、条件概率与次日无关、排序/精筛偏差。
2) **外因主导时**：stage 填 market（个股利空事件填 other），external_cause 填对应外因，
   param_hint 必须填"无需调参"，且 optimize/fix **不要**建议改形态识别或阈值参数
   （否则会把外因当内因，越改越偏）。
3) 证据缺口（写"数据缺失"的项）不得用推测填补；证据不足时 external_cause 填"证据不足"。

请归因并输出 JSON：
{{
  "error_type": "形态识别误判/相似度虚高/条件概率与次日无关/市场环境拖累/消息面反转/排序偏差/数据缺失或停牌/其他",
  "external_cause": "外因判断（大盘拖累/个股利空/技术破位/无（内因）/证据不足，只选一个）",
  "evidence_basis": "你据以判断的关键外部证据（不超过40字，必须引用上面的证据，如'大盘-3.2%而个股-3.5%跟跌'）",
  "stage": "是哪个环节出的问题（从 stock_pool/form_identify/feature_extract/pattern_match/neg_exclude/condition_score/rule_signal/risk_filter/rank_select/agent_refine/backtest_outcome/backtest_feature/backtest_pattern/backtest_condition/backtest_normalizer/backtest_product/market 选一个，优先判断是不是回测侧环节的问题）",
  "stage_hint": "该环节具体哪里出了问题（不超过40字）",
  "optimize": "如何优化该环节/参数使上涨概率更高（不超过60字，指明文件或参数，如 daily_scan.py 的负向抑制/daily_lambda_exclude）",
  "lesson": "一句话具体教训（不超过40字）",
  "fix": "下次如何修正（不超过50字，可操作）",
  "signal_hint": "最关键的预警信号（不超过20字）",
  "param_hint": "建议进化中心调整的参数方向（如 daily_lambda_exclude 负向抑制/daily_top_k 榜单条数/排序权重/回测结局门槛/无需调参），只能从这些或"无需调参"中选择",
  "confidence": "high/mid/low"
}}"""


def _attribute_failure(pick: dict, hit: dict, thr: float, evidence_txt: str = "") -> dict | None:
    """对单只失败推荐做失效归因。失败返回 None。

    plans/21 §8.2 归因复用：先查私有域知识库是否已有**高置信 + 大样本**的同类失效规律
    （按形态匹配），命中则直接复用结论（不调 LLM → 省 token、且历史结论保持一致）。

    注意：复用路径来自历史结论，不含本次的 external_cause（聚合时按"未知"计），
    这是"省 token"与"证据新鲜度"之间的取舍；需要新鲜证据时把 reuse 关掉即可。
    """
    try:
        from app.kb import kb_context as _kctx
        form = str(pick.get("form_type") or hit.get("form_type") or "")
        reused = _kctx.reuse_attribution(form_type=form)
        if reused:
            logger.debug(f"[daily_verify] 复用历史归因 {hit.get('ts_code')} ← {reused.get('from_lesson')}"
                         f"（n={reused.get('support_n')}）")
            return reused
    except Exception:  # noqa: BLE001
        pass
    try:
        user = _ATTRIBUTE_USER_TMPL.format(
            name=pick.get("name", hit.get("ts_code", "")),
            ts_code=hit.get("ts_code", ""),
            date=hit.get("date", ""),
            pick_txt=_format_pick(pick),
            t1_ret=f"{float(hit.get('t1_ret', 0) or 0) * 100:.2f}",
            thr=f"{thr * 100:.2f}",
            evidence=evidence_txt or "（外部证据未启用或不可用）",
        )
        result = deepseek_chat([
            {"role": "system", "content": _ATTRIBUTE_SYSTEM},
            {"role": "user", "content": user},
        ])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[daily_verify] 归因调用失败 {hit.get('ts_code')}: {exc}")
        return None
    return result if isinstance(result, dict) else None


def _aggregate(attributions: list[dict]) -> tuple[list[dict], list[str], list[dict], dict]:
    """聚合归因 → 归因统计 + 改进方向 + 环节归因报告 + 外因/内因分离统计。"""
    from collections import Counter
    by_type: dict[str, list] = {}
    for a in attributions:
        et = str(a.get("error_type", "其他"))
        by_type.setdefault(et, []).append(a)
    stats = []
    for et, items in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        top = max(items, key=lambda x: int(x.get("confidence") in ("high", "mid")))
        stats.append({
            "error_type": et, "count": len(items),
            "pct": round(len(items) / max(len(attributions), 1), 4),
            "lesson": top.get("lesson", ""), "fix": top.get("fix", ""),
            "signal_hint": top.get("signal_hint", ""),
            "param_hint": top.get("param_hint", "无需调参"),
        })
    # 改进方向：从归因提取可参数化方向（频次降序、去重）
    # ── 外因 / 内因分离（用户核心诉求）──
    # 为什么必须分：外因案例（大盘系统性下跌、个股利空）不该驱动"改形态/改阈值"，
    # 否则进化大脑会拿运气当规律，把参数越调越偏。
    EXTERNAL_TAGS = ("大盘拖累", "大盘系统性下跌", "个股利空", "技术破位", "消息面反转")
    external_items = [a for a in attributions
                      if any(t in str(a.get("external_cause") or "") for t in EXTERNAL_TAGS)]
    ext_ids = {id(a) for a in external_items}
    external = {
        "n": len(external_items),
        "pct": round(len(external_items) / max(len(attributions), 1), 4),
        "unknown_n": sum(1 for a in attributions if not str(a.get("external_cause") or "").strip()),
        "examples": [{"ts_code": a.get("ts_code", ""), "cause": a.get("external_cause", ""),
                      "basis": a.get("evidence_basis", "")} for a in external_items[:5]],
    }
    # 改进方向**排除外因案例**（外因不需要调参）
    hints = Counter(a.get("param_hint", "无需调参") for a in attributions if id(a) not in ext_ids)
    improvements = []
    for hint, cnt in hints.most_common():
        if hint and hint != "无需调参" and cnt >= 1:
            improvements.append(f"{hint}（{cnt} 例）")
    # 环节归因报告：哪个环节导致失效最多 → 定位回测/每日推荐哪个环节出问题
    by_stage: dict[str, list] = {}
    for a in attributions:
        by_stage.setdefault(str(a.get("stage", "other")), []).append(a)
    stage_report = []
    for stage, items in sorted(by_stage.items(), key=lambda kv: -len(kv[1])):
        top = max(items, key=lambda x: int(x.get("confidence") in ("high", "mid")))
        stage_report.append({
            "stage": stage,
            "stage_name": STAGE_MAP.get(stage, stage),
            "count": len(items),
            "pct": round(len(items) / max(len(attributions), 1), 4),
            "stage_hint": top.get("stage_hint", ""),
            "optimize": top.get("optimize", ""),
        })
    return stats, improvements, stage_report, external


# ── 主入口：每日自我验证 ──────────────────────────────────────
def run_daily_verify(limit_days: int | None = None, max_failures: int = MAX_ATTRIBUTE_FAILURES,
                     progress_cb=None) -> dict:
    """次日自我验证：T+1 回填 → 识别失败 → 失效归因 → 固化知识库 → 写进化事件。

    Args:
        limit_days: 验证最近 N 个交易日榜单（None=evolution_config.verify_lookback_days）
        max_failures: 单期最多归因失败案例数（0=全部；控制 token）
        progress_cb: 进度回调 fn(stage, pct)

    Returns:
        {date, verified_at, total, up_n, flat_n, down_n, t1_hit_rate, avg_t1_ret,
         failures, attributions, improvements, ...}
    """
    # 增量回填（优化3）：日常只补未回填记录（快，秒级）；首次口径迁移由 backfill_t1(force=True) 完成
    bf = backfill_t1(force=False)
    thr = _t1_threshold()
    lookback = limit_days if limit_days is not None else _lookback_days()

    state = _load_monitor_state()
    hits = state.get("hits", [])
    # 已回填 T+1 的记录，按 date 分组，取最近 lookback 个日期
    done = [h for h in hits if h.get("t1_ret") is not None]
    by_date: dict[str, list] = {}
    for h in done:
        by_date.setdefault(str(h.get("date", "")), []).append(h)
    dates = sorted(by_date.keys())[-lookback:]
    if not dates:
        return {"ok": False, "reason": "暂无已回填 T+1 的推荐记录", "backfill": bf,
                "t1_hit_rate": None}

    kb = load_verify_kb()
    verified_dates = {v.get("date") for v in kb.get("verifications", [])}
    results = []
    total_failures = 0

    for d in dates:
        if d in verified_dates:
            continue  # 该期已验证，跳过（幂等）
        report = _verify_one_day(d, by_date[d], thr, max_failures)
        results.append(report)
        total_failures += len(report.get("failures", []))
        if progress_cb:
            progress_cb(d, len(results) / max(len(dates), 1))

    if not results:
        return {"ok": False, "reason": f"最近 {lookback} 个交易日榜单均已完成验证", "backfill": bf,
                "t1_hit_rate": None, "verified_dates": sorted(verified_dates)}

    # 固化知识库（滚动）
    kb.setdefault("verifications", []).extend(results)
    kb["verifications"] = kb["verifications"][-VERIFY_KB_MAX:]
    # 优化4：口径标注（信号滞后修正：D+1 买入口径），供进化大脑区分新旧口径
    kb["metric_basis"] = "D+1_buy"
    kb["stats"] = {"verified_days": int(kb.get("stats", {}).get("verified_days", 0)) + len(results),
                   "total_failures": int(kb.get("stats", {}).get("total_failures", 0)) + total_failures}
    save_verify_kb(kb)

    # ── plans/21 §七：私有域知识库入库（案例 + 失败侧教训 + 环境快照）──
    kb_summary = None
    try:
        from app.kb import kb_ingest
        for rep in results:
            r_s = kb_ingest.ingest_verify_period(
                rep, by_date.get(str(rep.get("date", "")), []))
            kb_summary = r_s if r_s.get("ok") else kb_summary
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[daily_verify] 知识库入库跳过（不影响验证）: {exc}")

    # ── plans/21 §5.6：成功侧归因（必须配对对照；采样上限受配置控制）──
    success_note = None
    try:
        from app.agents import success_attrib
        success_note = success_attrib.run_success_attrib(
            dates=[str(r.get("date", ""))[:8] for r in results])
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[daily_verify] 成功侧归因跳过（不影响验证）: {exc}")

    # 写进化事件（供进化大脑 L0）
    latest = results[-1]
    try:
        from app.agents import evolution_events
        evolution_events.record_event(
            "INFO", "daily_verify",
            f"次日验证 {latest['date']}: T+1命中率 {latest['t1_hit_rate']*100:.1f}% "
            f"(涨{latest['up_n']}/平{latest['flat_n']}/跌{latest['down_n']}，失败归因 {len(latest['failures'])} 只"
            f"，主升浪T+5命中率 {latest.get('wave_hit_rate') if latest.get('wave_hit_rate') is not None else 'N/A'})",
            1)
        if latest["t1_hit_rate"] < 0.5:
            evolution_events.record_event(
                "WARNING", "daily_verify",
                f"次日命中率偏低 {latest['t1_hit_rate']*100:.1f}%：{latest['improvements'] or '无明显方向'}",
                1)
    except Exception:  # noqa: BLE001
        pass

    logger.info(f"[daily_verify] 验证完成: 期数 {len(results)}，失败归因 {total_failures}，"
                f"最新 T+1 命中率 {latest['t1_hit_rate']*100:.1f}%")
    return {"ok": True, "backfill": bf, "results": results, "latest": latest,
            "t1_hit_threshold": thr,
            "knowledge": kb_summary, "success_attrib": success_note}


def _verify_one_day(date_str: str, hits: list[dict], thr: float, max_failures: int) -> dict:
    """验证单日榜单：统计 + 识别失败 + 归因 + 主升浪（T+5）验证。"""
    detail = _load_report_detail(date_str)
    wave_thr = _wave_threshold()
    up_n = flat_n = down_n = 0
    t1_rets = []
    failures: list[dict] = []
    wave_done: list[float] = []       # 已到 T+5 的涨幅
    wave_failures: list[dict] = []    # 已到 T+5 但未走出主升浪（t5 < wave_thr）
    for h in hits:
        t1 = float(h.get("t1_ret", 0) or 0)
        t1_rets.append(t1)
        if t1 > thr + 1e-9:
            up_n += 1
        elif t1 < thr - 1e-9:
            down_n += 1
        else:
            flat_n += 1
        # 主升浪（T+5）：数据已到位才统计
        t5 = h.get("t5_ret")
        if t5 is not None:
            t5 = float(t5 or 0)
            wave_done.append(t5)
            if t5 < wave_thr:
                pick = detail.get(str(h.get("ts_code", "")), {})
                wave_failures.append({
                    "ts_code": h.get("ts_code", ""),
                    "name": pick.get("name", ""),
                    "form_type": pick.get("form_type", h.get("form_type", "")),
                    "t5_ret": round(t5, 4),
                    "t1_ret": round(t1, 4),
                    "similarity": pick.get("similarity"),
                    "up_probability": pick.get("up_probability"),
                })
        # 失败（次日未涨）：未达到次日上涨阈值
        if t1 <= thr:
            pick = detail.get(str(h.get("ts_code", "")), {})
            failures.append({
                "ts_code": h.get("ts_code", ""),
                "name": pick.get("name", ""),
                "form_type": pick.get("form_type", h.get("form_type", "")),
                "t1_ret": round(t1, 4),
                "t5_ret": round(float(h.get("t5_ret", 0) or 0), 4) if h.get("t5_ret") is not None else None,
                "pred_prob": h.get("pred_prob"),
                "similarity": pick.get("similarity"),
                "negative_score": pick.get("negative_score"),
                "up_probability": pick.get("up_probability"),
                "verified_at": h.get("t1_verified_at", ""),
            })

    total = len(t1_rets)
    avg = sum(t1_rets) / total if total else 0.0
    t1_hit_rate = (up_n / total) if total else 0.0
    wave_n = len(wave_done)
    wave_hit_n = sum(1 for v in wave_done if v >= wave_thr)
    wave_hit_rate = (wave_hit_n / wave_n) if wave_n else None
    avg_wave = (sum(wave_done) / wave_n) if wave_n else None

    # 失效归因（只归因失败案例，限制条数控 token）
    attr_targets = failures if max_failures and max_failures > 0 else failures
    if max_failures and max_failures > 0:
        attr_targets = failures[:max_failures]
    attributions: list[dict] = []
    for f in attr_targets:
        pick = detail.get(str(f["ts_code"]), {})
        hit = next((x for x in hits if str(x.get("ts_code", "")) == str(f["ts_code"])), {})
        # 外部证据包（大盘/个股日线/个股资料）——"像回测一样去历史数据里找原因"
        ev: dict = {}
        try:
            ev = FCTX.build_evidence(str(f["ts_code"]), str(f.get("date") or date_str))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[daily_verify] 证据包构建失败 {f.get('ts_code')}: {exc}")
        a = _attribute_failure(pick, hit, thr, evidence_txt=FCTX.to_text(ev))
        if a:
            a["ts_code"] = str(f["ts_code"])
            a["cause_prior"] = ev.get("cause_prior", "")
            f["evidence"] = ev
            f["cause_prior"] = ev.get("cause_prior", "")
            f["external_cause"] = a.get("external_cause", "")
            f["evidence_basis"] = a.get("evidence_basis", "")
            f["error_type"] = a.get("error_type", "其他")
            f["stage"] = a.get("stage", "other")
            f["stage_hint"] = a.get("stage_hint", "")
            f["optimize"] = a.get("optimize", "")
            f["lesson"] = a.get("lesson", "")
            f["fix"] = a.get("fix", "")
            f["signal_hint"] = a.get("signal_hint", "")
            f["param_hint"] = a.get("param_hint", "无需调参")
            f["confidence"] = a.get("confidence", "mid")
            attributions.append(a)

    attrs, improvements, stage_report, external = _aggregate(attributions)
    # 综合目标（用户核心诉求"必须涨 + 主升浪"）：composite = t1×(1-w) + wave×w
    try:
        _w = float(evolution_config.get_param("daily_target_wave_w", 0.4) or 0.4)
    except Exception:  # noqa: BLE001
        _w = 0.4
    _w = max(0.0, min(1.0, _w))
    _wave_rt = wave_hit_rate if wave_hit_rate is not None else 0.0
    composite = round(t1_hit_rate * (1 - _w) + _wave_rt * _w, 4)
    return {
        "date": date_str,
        "basis": "D+1_buy",   # 优化4：D+1 买入口径（信号滞后修正后）
        "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": total, "up_n": up_n, "flat_n": flat_n, "down_n": down_n,
        "t1_hit_rate": round(t1_hit_rate, 4),
        "avg_t1_ret": round(avg, 4),
        "t1_hit_threshold": thr,
        # 主升浪（T+5）验证维度（plans/18：目标=次日大涨 + 后续主升浪）
        "wave_hit_threshold": wave_thr,
        "wave_n": wave_n, "wave_hit_n": wave_hit_n,
        "wave_hit_rate": round(wave_hit_rate, 4) if wave_hit_rate is not None else None,
        "avg_wave_ret": round(avg_wave, 4) if avg_wave is not None else None,
        "wave_failures": wave_failures,
        # 综合目标分（必须涨+主升浪）
        "composite": composite, "wave_w": _w,
        "failures": failures,
        "attributions": attrs,
        "improvements": improvements,
        "stage_report": stage_report,
        # 外因/内因分离统计（外因案例不计入改进方向）
        "external": external,
        "evidence_enabled": FCTX.evidence_enabled(),
    }


# ── ③ 验证结论文本（供进化大脑 L0）────────────────────────────
def build_verify_txt(kb: dict | None = None) -> str:
    """构造"次日验证结论"文本：最新一期 T+1 命中率 + 归因 TOP3 + 改进方向。"""
    kb = kb or load_verify_kb()
    verifs = kb.get("verifications", [])
    if not verifs:
        return ""
    latest = verifs[-1]
    lines = [
        f"次日验证 {latest.get('date','')}: T+1命中率 {latest.get('t1_hit_rate',0)*100:.1f}% "
        f"（涨{latest.get('up_n',0)}/平{latest.get('flat_n',0)}/跌{latest.get('down_n',0)}，"
        f"平均T+1 {latest.get('avg_t1_ret',0)*100:+.1f}%）",
    ]
    # plans/18：主升浪（T+5）验证维度 —— 目标"次日大涨 + 后续主升浪"
    if latest.get("wave_hit_rate") is not None:
        lines.append(
            f"主升浪T+5命中率 {latest.get('wave_hit_rate',0)*100:.1f}% "
            f"（样本 {latest.get('wave_n',0)}，平均T+5 {latest.get('avg_wave_ret',0)*100:+.1f}%，"
            f"主升浪失败 {len(latest.get('wave_failures',[]))} 只）"
        )
    # 综合目标分（用户核心诉求"必须涨 + 主升浪"）
    if latest.get("composite") is not None:
        lines.append(
            f"综合目标分 {latest.get('composite',0)*100:.1f}% "
            f"（T+1×{1 - latest.get('wave_w', 0.4):.1f}+主升浪×{latest.get('wave_w', 0.4):.1f}）"
        )
    attrs = latest.get("attributions") or []
    if attrs:
        top = attrs[:3]
        lines.append("失效归因: " + " | ".join(
            f"{a['error_type']}({a['count']}例){'→'+a['param_hint'] if a.get('param_hint') and a['param_hint']!='无需调参' else ''}"
            for a in top))
    # 外因/内因分离（用户诉求：先去大盘/个股日线/个股资料里找原因）
    # 外因案例**不计入改进方向**——否则会拿运气当规律，把形态/阈值越调越偏
    ext = latest.get("external") or {}
    if ext.get("n") or ext.get("unknown_n"):
        lines.append(
            f"外因归因: {ext.get('pct', 0) * 100:.0f}% 的失败属外因"
            f"（{ext.get('n', 0)} 例：大盘拖累/个股利空/技术破位，已排除在改进方向之外；"
            f"未判明 {ext.get('unknown_n', 0)} 例）"
        )
    # plans/18：环节归因（定位回测/每日推荐哪个环节出问题 → 优化方向）
    stages = latest.get("stage_report") or []
    if stages:
        top_s = stages[:3]
        lines.append("环节归因: " + " | ".join(
            f"{s.get('stage_name', s.get('stage',''))}({s.get('count',0)}例)→{s.get('optimize','')}"
            for s in top_s))
    imps = latest.get("improvements") or []
    if imps:
        lines.append("改进方向: " + "、".join(imps))
    return "\n".join(lines)


def verify_report() -> dict:
    """返回最近一期验证报告（前端/API 展示）。"""
    kb = load_verify_kb()
    verifs = kb.get("verifications", [])
    return {
        "latest": verifs[-1] if verifs else None,
        "stats": kb.get("stats", {}),
        "history": verifs[-10:],
    }


__all__ = ["backfill_t1", "run_daily_verify", "build_verify_txt", "verify_report",
           "load_verify_kb", "save_verify_kb", "VERIFY_KB_FILE"]
