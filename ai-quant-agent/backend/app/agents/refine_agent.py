"""个股语义精筛 Agent（refine_agent）

对齐 plans/03-Agent系统.md §五：
  综合候选画像（量化）+ 政策信号（板块催化）+ 消息面信号（个股新闻），
  判断 select/skip + score_adjust + reason。

失败降级：LLM 失败 → 保留规则分数（score_adjust=0，action=select 按规则概率参与）。

★ 降级文案的口径变更史（两次方向相反的踩坑）：
  - 2026-09-23：曾出现"预算熔断被写成 Agent 分析失败" —— 前端大面积显示
    "Agent 分析失败，按规则概率保留"，看起来像模型/网络故障，真因（当天 3.0 元/天预算
    已用 3.0017 元）只能靠翻日志猜。于是加了"区分预算熔断与调用失败"的文案。
  - 2026-09-29（用户要求"每日推荐不要有预算熔断、不要设置上限"）：
    闸门已从 `llm_client.deepseek_chat()` **整层摘除** ⇒ **本路径不可能因预算而返回 None**。
    此时再读全局的 `llm_metering.budget_blocked()` 去写"预算熔断"**只会制造误报**：
    该标志是进程级全局的，若自证回放先显式自查把标志置起、之后这里发生真实调用失败，
    就会被错写成"预算熔断"、把排查方向带偏。故文案回到如实陈述"调用失败"。
"""
from __future__ import annotations

from app.agents.llm_client import deepseek_chat, as_dict, as_rows
from app.agents import prompts
from app.agents.candidate_profiler import profile_to_text
from app.core.logger import logger


def _degrade_reason() -> str:
    """LLM 返回 None 时的降级原因（≤100 字）。

    ★ 2026-09-29：**本路径已不可能因预算熔断**（闸门已从 `deepseek_chat()` 摘除，
      见 app/agents/llm_client.py 的说明）⇒ 不再声称预算是原因。
      为什么必须把那段判定删掉而不是留着：`llm_metering.budget_blocked()` 是**全局**标志，
      自证回放显式自查预算时也会点亮它 —— 留着那段判定，就会把本路径后续的
      **真实调用失败**误报成"预算熔断"（把排查方向带偏，正是 2026-09-23 那条坑的反面）。
    """
    return "LLM 调用失败，按规则概率保留（每日推荐链路不受预算闸门约束）"[:100]


class RefineAgent:
    """个股语义精筛 Agent。"""

    def refine(
        self,
        profile: dict,
        policy_signal_text: str,
        news_signal_text: str,
        news_sig: dict | None = None,
        attribution_txt: str | None = None,
        reflect_txt: str | None = None,
        condition_txt: str | None = None,
    ) -> dict:
        """对单只候选做语义精筛。

        硬性兜底（防烟雾弹/防利好出货，不依赖 LLM 自觉）：
          - 消息面明确"利好出货风险" → 强制 skip（必排）
          - 消息面明确"烟雾弹/假利好" → 强制 score_adjust<=-5（降权）

        Returns:
            {ts_code, action, score_adjust, confidence, reason}
        """
        ts_code = profile.get("ts_code", "")
        dist = (news_sig or {}).get("distribution_risk")
        smoke = (news_sig or {}).get("smoke_risk")

        # 硬性排除：利好出货（股价已大涨+利好兑现）→ 必排，不调 LLM
        if dist and dist != "无":
            return {
                "ts_code": ts_code,
                "action": "skip",
                "score_adjust": -20,
                "confidence": "low",
                "reason": f"消息面利好出货风险[{dist}]，排除（防追高）",
            }

        user = prompts.REFINE_USER_TMPL.format(
            profile=profile_to_text(profile),
            attribution_kb=attribution_txt or "无历史成功规律数据",
            reflect_kb=reflect_txt or "无历史失败教训数据",
            condition_stat=condition_txt or "无回测条件统计数据",
            policy_signal=policy_signal_text or "无明确政策催化信号",
            news_signal=news_signal_text or "无消息面信号",
        )
        result = deepseek_chat([
            {"role": "system", "content": prompts.REFINE_SYSTEM},
            {"role": "user", "content": user},
        ])
        if result is None:
            return {
                "ts_code": ts_code,
                "action": "select", "score_adjust": 0,
                "confidence": "low",
                "reason": _degrade_reason(),
            }
        # ★ 形状规整（2026-09-29 事故：'list' object has no attribute 'get'）：
        #   LLM 偶尔把对象包成 `[ {...} ]` 或直接返回数组。原来下面的 `result.get("reason")`
        #   **不在 try 里** ⇒ AttributeError 逃出 refine()，被上层"逐只 try"记成
        #   "个股精筛失败，按规则保留" —— 表面是单只失败，实际**每一只都降级**（LLM 精筛整层失效）。
        d = as_dict(result)
        if not d:
            return {
                "ts_code": ts_code,
                "action": "select", "score_adjust": 0,
                "confidence": "low",
                "reason": "LLM 返回格式异常（非对象），按规则概率保留"[:100],
            }
        try:
            action = str(d.get("action", "select")).lower()
            score_adjust = int(d.get("score_adjust", 0) or 0)
            score_adjust = max(-20, min(10, score_adjust))
        except Exception:  # noqa: BLE001
            action, score_adjust = "select", 0
        reason = str(d.get("reason", ""))[:80]

        # 硬性降权：烟雾弹/假利好 → 不得上调，强制降权
        if smoke and smoke != "无":
            score_adjust = min(score_adjust, -5)
            reason = (reason + f"；烟雾弹[{smoke}]降权")[:80]

        return {
            "ts_code": ts_code,
            "action": action,
            "score_adjust": score_adjust,
            "confidence": str(d.get("confidence", "mid")),
            "reason": reason,
        }

    def batch_refine(
        self,
        profiles: list[dict],
        batch_size: int = 25,
        per_industry: int = 10,
        attribution_txt: str | None = None,
        reflect_txt: str | None = None,
        condition_txt: str | None = None,
        should_stop=None,
    ) -> list[dict]:
        """批量粗筛（三层漏斗 L1）：按行业分组统一调用 LLM，降低成本。

        同一行业的一批候选画像组装成一个 prompt，LLM 横向对比输出 select/skip；
        每行业最终保留规则概率前 per_industry 只。

        Args:
            profiles: 候选画像列表（dump_profiles 输出）
            batch_size: 每批候选数（默认 25）
            per_industry: 每行业最多保留数（默认 10）；**<=0 = 不截断**（保留该行业全部粗筛
                候选 —— 配合 run_agent_refine 的小池自适应：候选只有百来条时不必再砍）

        Returns:
            粗筛后的子集 profile 列表
        """
        from collections import defaultdict

        groups: dict[str, list] = defaultdict(list)
        for p in profiles:
            key = p.get("industry") or p.get("form_type") or "其他"
            groups[key].append(p)

        selected: list[dict] = []
        for key in sorted(groups):
            group = sorted(groups[key], key=lambda p: -p.get("up_probability", 0))
            for i in range(0, len(group), batch_size):
                # 协作式取消：每批 LLM 调用前检查（单批约数秒），命中即返回已选中的部分，
                # 由上层 run_agent_refine 标 cancelled（前端「停止扫描」同样能中断精筛阶段）。
                if should_stop is not None and should_stop():
                    logger.warning(
                        f"[agent_refine] 收到停止请求：批量粗筛中断（已选 {len(selected)} 只，行业 {key}）"
                    )
                    return selected
                batch = group[i:i + batch_size]
                cand_txt = "\n".join(profile_to_text(p) for p in batch)
                user = prompts.BATCH_REFINE_USER_TMPL.format(
                    sector=key,
                    candidates=cand_txt,
                    attribution_kb=attribution_txt or "无历史成功规律数据",
                    reflect_kb=reflect_txt or "无历史失败教训数据",
                    condition_stat=condition_txt or "无回测条件统计数据",
                )
                result = deepseek_chat([
                    {"role": "system", "content": prompts.BATCH_REFINE_SYSTEM},
                    {"role": "user", "content": user},
                ])
                if result is None:
                    # LLM 失败降级：该批全保留，由深度层/规则兜底
                    selected.extend(batch)
                    continue
                # ★ 形状规整：模型可能返回 `{"candidates": [...]}` 这类"对象里嵌列表"。
                #   原来只认 `isinstance(result, list)` ⇒ 该批被当成"解析异常"，
                #   **只保留规则概率最高的 1 只**（静默丢掉整批候选）⇒ 榜单规模莫名缩水。
                rows = as_rows(result)
                select_codes = {
                    str(r.get("ts_code")) for r in rows
                    if str(r.get("action", "")).lower() == "select"
                }
                if not select_codes:
                    # LLM 全 skip 或解析异常：保守保留该批规则概率最高 1 只
                    if batch:
                        selected.append(batch[0])
                else:
                    selected.extend(p for p in batch if p.get("ts_code") in select_codes)

        # 每行业最终最多 per_industry（按规则概率）。
        # per_industry <= 0 = **不截断**（小池自适应：候选本就不多时保留该行业全部候选，
        # 避免"只有百来条候选还被每行业砍到前 10"造成真实错失；见 agents/__init__.py
        # 的 agent_small_pool_threshold）。
        final: list[dict] = []
        by_ind: dict[str, list] = defaultdict(list)
        for p in selected:
            by_ind[p.get("industry") or p.get("form_type") or "其他"].append(p)
        for key, plist in by_ind.items():
            plist.sort(key=lambda p: -p.get("up_probability", 0))
            final.extend(plist if per_industry <= 0 else plist[:per_industry])
        return final
