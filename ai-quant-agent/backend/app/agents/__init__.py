"""多 Agent 精筛编排入口（agents）

对齐 plans/03-Agent系统.md §七 端到端流水线（三层漏斗，降成本保质量）：
  候选池（过滤北交所/科创板）
  → L1 批量粗筛（按行业分组统一调用 LLM，每行业保留前 PER_INDUSTRY）
  → L2 深度精筛 Top-DEEP_TOP_N（政策行业驱动 + Tavily 消息面防烟雾弹/防出货 + 个股精筛）
  → L3 统一决策 Agent → 最终榜单（全部 select，含可解释理由）

对外主入口：run_agent_refine()
"""
from __future__ import annotations

import time
from collections import Counter

from app.core.logger import logger
from app.core.config import settings
from app.agents.candidate_profiler import dump_profiles, build_candidate_profile, to_json
from app.agents.policy_agent import PolicyAgent
from app.agents.news_agent import NewsAgent
from app.agents.refine_agent import RefineAgent
from app.agents.decision_agent import DecisionAgent, build_final_report, _deterministic_rank
from app.backtest.attribution import load_attribution_kb
from app.backtest.threshold_analyzer import load_condition_table
from app.agents.reflect_agent import build_reflect_txt
from app.agents.learning_runner import record_decision_picks

# 三层漏斗规模参数（**默认值=历史口径**；运行时可被 settings/.env 覆盖，见 `_scale()`）
MAX_POLICY_SECTORS = 5   # 政策解读只对出现频次最高的 N 个行业做（控制成本）
BATCH_SIZE = 25          # 批量粗筛每批候选数
PER_INDUSTRY = 10        # 批量粗筛每行业最多保留数（<=0 = 不截断）
DEEP_TOP_N = 50          # 深度精筛全局 Top-N（按规则概率取前 N）
# 小池自适应阈值：候选池 ≤ 该值时取消"每行业截断"与"全局 Top-N 截断"（智能判断，见 run_agent_refine）
SMALL_POOL_THRESHOLD = 100


def _scale(settings_name: str, default: int) -> int:
    """读漏斗规模参数（settings 优先；缺省/非法 → 默认值）。

    为什么改成可覆盖：这几个数原来硬编码，想调"每板块几只进深度精筛"只能改代码，
    而它直接决定 LLM 调用量与花费（每只深度 = 1 次 Tavily + 1 次 LLM）。
    """
    try:
        from app.core.config import settings
        v = getattr(settings, settings_name, None)
        return max(0, int(v)) if v is not None else default
    except Exception:  # noqa: BLE001
        return default


def _allow_stock(c: dict) -> bool:
    """过滤北交所(.BJ)与科创板(688 开头)，保留沪深主板 + 创业板。"""
    code = str(c.get("ts_code", ""))
    if code.endswith(".BJ"):
        return False
    if code.startswith("688"):
        return False
    return True


def _build_attribution_txt(kb: dict | None) -> str:
    """把回测归因知识库构造为供 LLM 参考的"历史成功规律"文本（控制 token）。

    只取 is_key（AUC 高）的关键特征 + success_profile 方向 + 少量组合规则。
    无 kb 返回 ''（精筛降级为纯语义判断）。
    """
    if not kb:
        return ""
    key_feats = (kb.get("key_features") or [])[:10]
    if not key_feats:
        return ""
    parts = []
    for f in key_feats:
        name = str(f.get("feature", ""))
        auc = float(f.get("auc", 0) or 0)
        direction = "高值" if f.get("direction") == "high" else "低值"
        parts.append(f"{name}(AUC {auc:.2f}，成功样本{direction}占优)")
    txt = "、".join(parts)
    profile = kb.get("success_profile") or {}
    if profile:
        prof_txt = "、".join(f"{k}≈{v}" for k, v in list(profile.items())[:6])
        txt += f"；成功画像：{prof_txt}"
    combos = (kb.get("combo_rules") or [])[:3]
    if combos:
        c_txt = "、".join(
            f"[{'+'.join(c.get('features', []))}]上涨概率{c.get('up_prob', 0) * 100:.0f}%"
            for c in combos
        )
        txt += f"；高胜率组合：{c_txt}"
    return f"历史成功样本共性：{txt}"


def _build_condition_txt() -> str:
    """把回测条件统计（condition_table.json）构造为供 LLM 参考的文本。

    用户架构：bps/人均持股/股东人数/筹码集中度等条件参数是"Agent 参考条件"——
    回测只统计这些参数对涨跌的影响，统计结果提供给 Agent 作为分析参考。
    无表返回 ''。
    """
    try:
        ct = load_condition_table()
    except Exception:  # noqa: BLE001
        return ""
    if ct is None or not ct.rules:
        return ""
    parts = []
    for r in ct.rules[:10]:
        feat = str(r.feature)
        # 方向：high=值越大上涨率越高；low=值越小上涨率越高
        if getattr(r, "direction", "high") == "low":
            cond = f"{feat}≤{r.threshold:.2f}"
        else:
            cond = f"{feat}≥{r.threshold:.2f}"
        parts.append(f"{cond}→T+5上涨率{r.up_prob * 100:.0f}%（样本{r.sample_count}）")
    return "回测条件统计（bps/筹码/资金等对上涨的影响，仅参考，不代表未来）：" + "；".join(parts)


def _cancelled_refine(t0: float, total: int, message: str = "", refined_count: int = 0,
                      select_count: int = 0, items: list | None = None) -> dict:
    """提前停止时的精筛结果（plans/09 前端「停止扫描」）。

    `cancelled=True` 让调用方（predictions._scan_worker）**不落盘**：
    半成品榜单若落盘，会被次日自我验证/命中登记当成正式推荐，污染进化反馈。
    """
    return {
        "status": "cancelled",
        "cancelled": True,
        "message": message or "已按请求停止",
        "total": total,
        "batch_count": refined_count,
        "refined_count": refined_count,
        "select_count": select_count,
        "top_picks": [],
        "partial_items": items or [],
        "elapsed": round(time.time() - t0, 2),
    }


def run_agent_refine(
    candidates: list[dict],
    top_k: int | None = None,
    top_n: int | None = None,
    progress_cb=None,
    should_stop=None,
) -> dict:
    """多 Agent 三层漏斗精筛主流程。

    Args:
        candidates: 每日推荐候选池（daily_recommend 的 top_picks 列表）
        top_k: 保留兼容（默认全部候选池，不限制）
        top_n: 保留兼容（默认全部 select）
        progress_cb: 进度回调 fn(stage, pct)
        should_stop: 可选取消回调（前端「停止扫描」共用同一信号）。
            在"批量粗筛每批调用前"与"深度精筛每只前"检查，命中即返回部分结果
            并带 `cancelled=True`（由调用方决定不落盘），保证点停止后几秒内真的停下。

    Returns:
        {status, total, batch_count, refined_count, select_count, top_picks, elapsed}
        （提前停止时额外带 cancelled=True 与 message）
    """
    t0 = time.time()

    def _progress(stage: str, pct: float):
        if progress_cb:
            progress_cb(stage, pct)
        logger.info(f"[agent_refine] {stage} {pct:.0%}")

    # ── 0.5 归因知识（回测固化 attribution_kb → 参考历史成功规律）──
    attribution_txt = _build_attribution_txt(load_attribution_kb())
    if attribution_txt:
        logger.info(f"[agent_refine] 已加载归因知识: {attribution_txt[:120]}...")
    else:
        logger.info("[agent_refine] 无归因知识（回测未产出 attribution_kb），精筛降级为纯语义判断")

    # ── 0.5b 条件统计参考（回测 condition_table → bps/筹码/资金等对上涨的影响，Agent 分析参考）──
    condition_txt = _build_condition_txt()
    if condition_txt:
        logger.info(f"[agent_refine] 已加载条件统计参考: {condition_txt[:120]}...")
    else:
        logger.info("[agent_refine] 无条件统计参考（回测未产出 condition_table），精筛仅参考个股条件参数原始值")

    # ── 0.6 反思知识（reflect_kb → 参考历史失败教训，"越来越聪明"）──
    reflect_txt = build_reflect_txt()
    if reflect_txt:
        logger.info(f"[agent_refine] 已加载反思教训: {reflect_txt[:120]}...")
    else:
        logger.info("[agent_refine] 暂无高频失败教训（样本不足），精筛仅参考归因/语义")

    if not candidates:
        return {"status": "ok", "total": 0, "batch_count": 0, "refined_count": 0,
                "select_count": 0, "top_picks": [], "elapsed": 0.0}

    # ── 0. 过滤北交所/科创板 ──
    candidates = [c for c in candidates if _allow_stock(c)]
    logger.info(f"[agent_refine] 过滤北交所/科创板后候选 {len(candidates)} 只")

    # ── 1. 候选画像 Agent（全部候选）──
    _progress("候选画像", 0.05)
    profiles = dump_profiles(candidates, None)  # None = 全部
    logger.info(f"[agent_refine] 候选画像组装完成: {len(profiles)} 只")

    # 漏斗规模参数（settings/.env 可覆盖，默认=历史口径）
    batch_size = _scale("agent_l1_batch_size", BATCH_SIZE) or BATCH_SIZE
    per_industry = _scale("agent_l1_per_industry", PER_INDUSTRY)
    deep_top_n = _scale("agent_l2_deep_top_n", DEEP_TOP_N)
    floor_per_industry = _scale("agent_l2_per_industry_floor", 0)
    l2_max_total = _scale("agent_l2_max_total", 120) or 120
    max_policy_sectors = _scale("agent_policy_max_sectors", MAX_POLICY_SECTORS)
    small_pool_threshold = _scale("agent_small_pool_threshold", SMALL_POOL_THRESHOLD)

    # ── 1.5 小池自适应（"智能判断"）──
    # 候选池本来就小（≤ 阈值；典型如行情清淡/确认启动少的交易日，全市场只筛出百来条）时，
    # 再按"每行业前 N / 全局 Top-N"激进裁剪只会造成**真实错失**，省下的成本却有限。
    # 此时改为：① L1 不做每行业截断（该行业候选全保留）② L2 不做全局 Top-N 截断（候选全量
    # 进深度精筛）—— 把取舍交给更"聪明"的深度层（Tavily 消息面 + LLM 逐只精筛）。
    # 阈值可调（.env: AGENT_SMALL_POOL_THRESHOLD）；设 0 = 关闭该自适应，恒按大池口径。
    small_pool = small_pool_threshold > 0 and len(profiles) <= small_pool_threshold
    if small_pool:
        logger.info(
            f"[agent_refine] 小池自适应生效：候选 {len(profiles)} ≤ {small_pool_threshold} → "
            f"取消每行业截断（原每行业前 {per_industry}）与全局 Top-N 截断（原 {deep_top_n}），"
            f"全部候选进深度精筛"
        )
        per_industry = 0             # 0 = 不截断（保留该行业全部粗筛候选）
        floor_per_industry = 0       # 已全量进 L2，"每行业保底"无意义
        deep_top_n = len(profiles)   # 放宽到全量（sub_profiles ⊆ profiles ⇒ 实际全保留）

    # ── 2. L1 批量粗筛（按行业分组统一调用 LLM，降成本）──
    _progress("批量粗筛", 0.2)
    if should_stop is not None and should_stop():
        return _cancelled_refine(t0, len(candidates), message="已按请求停止（批量粗筛前）")
    refine_agent = RefineAgent()
    sub_profiles = refine_agent.batch_refine(
        profiles, batch_size=batch_size, per_industry=per_industry,
        attribution_txt=attribution_txt,
        condition_txt=condition_txt,
        reflect_txt=reflect_txt,
        should_stop=should_stop,
    )
    if should_stop is not None and should_stop():
        return _cancelled_refine(t0, len(candidates), message="已按请求停止（批量粗筛阶段）",
                                refined_count=len(sub_profiles))
    per_ind_desc = "每行业不截断（小池自适应）" if per_industry <= 0 else f"每行业前 {per_industry}"
    logger.info(
        f"[agent_refine] 批量粗筛完成: {len(profiles)} → {len(sub_profiles)} 只"
        f"（{per_ind_desc}）"
    )
    _progress("批量粗筛", 0.3)

    # ── 3. L2 深度精筛范围（Top-DEEP_TOP_N，按 20 天向量 up_probability）──
    # 形态规则召回增强（规划 15）：命中已验证形态规则的候选强制保送进 L2，
    # 即使 up_probability 排不进 Top-50 也保证被 LLM 分析/搜索，弥补候选未被 LLM 覆盖的缺陷。
    ranked = sorted(sub_profiles, key=lambda p: -p.get("up_probability", 0))
    core = ranked[:deep_top_n]                       # ① 全局 Top-N（主口径）
    core_codes = {p.get("ts_code") for p in core}

    # ② 形态规则召回增强（规划 15）：命中已验证形态规则的候选强制保送进 L2（硬承诺，不被上限裁）
    rule_locked = [p for p in ranked
                   if p.get("hit_rules") and p.get("ts_code") not in core_codes]
    for _p in rule_locked:
        core_codes.add(_p.get("ts_code"))

    # ③ 每行业保底（`agent_l2_per_industry_floor>0` 才启用）：
    # ★ 口径差异就在这 —— L1 的"每行业前 10"只是**候选**，L2 若只取**全局** Top-N，
    #   候选/行业一多，小板块的头部会被大板块挤掉（用户口径：每个板块最好的那几只也要被 LLM 看到）。
    #   打开后每个板块按 up_probability 的前 K 只保证进 L2（按板块分组，不跨板块竞争）。
    floor_extra: list[dict] = []
    if floor_per_industry > 0:
        groups: dict[str, list] = {}
        for p in sub_profiles:
            groups.setdefault(p.get("industry") or p.get("form_type") or "其他", []).append(p)
        for _g in groups.values():
            _g.sort(key=lambda p: -p.get("up_probability", 0))
            for p in _g[:floor_per_industry]:
                if p.get("ts_code") not in core_codes:
                    core_codes.add(p.get("ts_code"))
                    floor_extra.append(p)
        logger.info(
            f"[agent_refine] 每行业保底深度精筛: 每行业前 {floor_per_industry} 只"
            f"（行业 {len(groups)} 个 → 保底新增 {len(floor_extra)} 只）"
        )

    # 规模上限：**只裁"每行业保底"**（可选增强最不刚性）；全局 Top-N 与形态规则保送不裁，
    # 故极端情况下总数可能略超上限 —— 但绝不静默丢掉"必须被 LLM 看到"的形态规则命中。
    _room = max(0, l2_max_total - len(core) - len(rule_locked))
    _capped = len(floor_extra) > _room
    deep_profiles = core + rule_locked + floor_extra[:_room]
    logger.info(
        f"[agent_refine] 深度精筛范围: {len(deep_profiles)} 只"
        f"（全局 Top-{deep_top_n}={len(core)} + 形态规则保送 {len(rule_locked)}"
        f" + 每行业保底 {min(len(floor_extra), _room)}"
        f"{'（保底按上限 %d 截断）' % l2_max_total if _capped else ''}）"
    )

    # ── 4. 政策解读 Agent（4 子 Agent，按行业去重）──
    if should_stop is not None and should_stop():
        return _cancelled_refine(t0, len(candidates), message="已按请求停止（深度精筛前）",
                                refined_count=len(sub_profiles))
    _progress("政策解读", 0.35)
    policy_agent = PolicyAgent()
    sector_counter: Counter = Counter()
    for p in deep_profiles:
        for c in p.get("sectors") or p.get("concepts") or []:
            sector_counter[c] += 1
    top_sectors = [s for s, _ in sector_counter.most_common(max_policy_sectors)]
    policy_signals: dict[str, dict | None] = {}
    for i, sector in enumerate(top_sectors):
        logger.info(f"[agent_refine] 政策解读 {i + 1}/{len(top_sectors)}: {sector}")
        try:
            policy_signals[sector] = policy_agent.analyze_sector(sector)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[agent_refine] 政策解读失败 {sector}: {exc}")
            policy_signals[sector] = None
        _progress("政策解读", 0.35 + 0.1 * (i + 1) / max(len(top_sectors), 1))

    # ── 5. L2 深度逐只（Tavily 消息面 + 个股精筛，防烟雾弹/防出货）──
    _progress("消息面+个股精筛", 0.5)
    news_agent = NewsAgent()
    policy_cache: dict[str, str] = {}
    items: list[dict] = []
    for i, p in enumerate(deep_profiles):
        # 协作式取消：逐只深度精筛前检查（单只含 Tavily 搜索 + LLM，数秒级）
        if should_stop is not None and should_stop():
            logger.warning(f"[agent_refine] 收到停止请求：深度精筛中断（已处理 {i}/{len(deep_profiles)} 只）")
            return _cancelled_refine(
                t0, len(candidates), message=f"已按请求停止（深度精筛 {i}/{len(deep_profiles)}）",
                refined_count=len(sub_profiles), select_count=len(items), items=items,
            )
        ts_code = p.get("ts_code", "")
        sector_hit = next(
            (c for c in (p.get("sectors") or p.get("concepts") or []) if c in policy_signals),
            None,
        )
        if sector_hit is not None and sector_hit not in policy_cache:
            sig = policy_signals.get(sector_hit) or {}
            txt = policy_agent.signal_to_text(sig)
            if sig.get("signal_direction") == "reverse":
                # 政策反向信号（利好出尽/政策本身利空）：硬性避雷，优先 skip 或大幅降权
                txt = f"【政策反向信号-硬性】该板块政策利好已兑现或属利空，个股精筛应优先 skip 或大幅降权（避雷）。{txt}"
            policy_cache[sector_hit] = txt
        policy_txt = policy_cache.get(sector_hit, "") if sector_hit else ""
        try:
            news_sig = news_agent.verify(p)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[agent_refine] 消息面验证失败 {ts_code}: {exc}")
            news_sig = None
        news_txt = news_agent.signal_to_text(news_sig)
        try:
            refine = refine_agent.refine(
                p, policy_txt, news_txt, news_sig=news_sig,
                attribution_txt=attribution_txt, reflect_txt=reflect_txt,
                condition_txt=condition_txt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[agent_refine] 个股精筛失败 {ts_code}: {exc}")
            refine = {"ts_code": ts_code, "action": "select", "score_adjust": 0,
                      "confidence": "low", "reason": "精筛失败，按规则保留"}
        refine["up_probability"] = p.get("up_probability", 0)
        items.append(refine)
        if (i + 1) % 10 == 0 or i == len(deep_profiles) - 1:
            _progress("消息面+个股精筛", 0.5 + 0.35 * (i + 1) / max(len(deep_profiles), 1))
            logger.info(
                f"[agent_refine] 深度精筛 {i + 1}/{len(deep_profiles)}，"
                f"select {sum(1 for x in items if x.get('action') == 'select')}"
            )

    select_n = sum(1 for x in items if x.get("action") == "select")
    logger.info(f"[agent_refine] 深度精筛完成: {len(items)} 只，select {select_n}")

    # ── 6. L3 统一决策 Agent（全部 select）──
    _progress("统一决策", 0.95)
    decision_agent = DecisionAgent()
    # ★ 2026-09-29 事故加固：L3 是最后一环，原来**没有 try** ⇒ 任何异常（如 LLM 返回形状异常
    #   —— 实测报错「Agent 精筛失败: 'list' object has no attribute 'get'」、模型/网络抖动）
    #   都会让**整轮精筛判 failed、当日榜单一条都不落盘**（前面 L1/L2 的几十次调用全白花）。
    #   兜底成确定性排序（规则概率 × (1+score_adjust%)，与 LLM 失败时同口径）：
    #   宁可"排名次优但有榜单"，也不要"整轮失败且无榜单"。
    try:
        ranks = decision_agent.decide(items, top_n=None)  # None = 全部 select
        final_picks = build_final_report(candidates, ranks, deep_profiles)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[agent_refine] 统一决策异常，退回确定性排序（榜单仍会产出）: {exc}")
        final_picks = build_final_report(candidates, _deterministic_rank(items, None), deep_profiles)
    logger.info(f"[agent_refine] 最终榜单 {len(final_picks)} 条")
    # 记录决策榜单 → T+5 后反思决策质量（§20 统一学习能力）
    try:
        record_decision_picks(final_picks)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[agent_refine] 记录决策榜单失败: {exc}")

    # ── 7. 消息面欠账补做（"消息面必需"的还账动作）──
    # L2 精筛期间若 Tavily 抖动，失败 query 会进欠账队列（llm_client）。这里顺手补做：
    # 补做成功的结果会落消息面缓存 → 当天同 query（行业政策/盘前兜底）即可命中，
    # 欠账才不是"记着不用"；再失败也不阻塞榜单（llm_client 会重新入队 + 开进化大脑工单）。
    try:
        from app.agents.llm_client import tavily_drain_pending, tavily_pending_size
        from app.core.config import settings as _settings
        _drain_limit = int(_settings.tavily_drain_limit or 0)
        _left = tavily_pending_size()
        if _left and _drain_limit > 0 and (should_stop is None or not should_stop()):
            logger.info(f"[agent_refine] 消息面欠账 {_left} 条，补做最多 {_drain_limit} 条")
            logger.info(f"[agent_refine] 消息面欠账补做结果: {tavily_drain_pending(limit=_drain_limit)}")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[agent_refine] 消息面欠账补做失败（不影响榜单）: {exc}")

    elapsed = round(time.time() - t0, 2)
    _progress("完成", 1.0)
    return {
        "status": "ok",
        "total": len(candidates),
        "batch_count": len(sub_profiles),
        "refined_count": len(deep_profiles),
        "select_count": select_n,
        "top_picks": final_picks,
        "elapsed": elapsed,
    }


__all__ = ["run_agent_refine", "build_candidate_profile", "to_json"]
