"""统一决策 Agent（decision_agent）

对齐 plans/03-Agent系统.md §六：
  汇总所有个股精筛决策 + 规则综合概率，做最终 Top-N 排序决定 + 最终理由。

设计：
  - 先按规则综合概率降序取 Top-K（已由上层控制），全部喂给 LLM 做统一精筛排序
  - LLM 输出 final_ranks（按 final_score 降序），由本模块截取 Top-N
  - 失败降级：LLM 失败 → 按规则综合概率 + score_adjust 确定性公式排序（select 优先）
"""
from __future__ import annotations

import json

from app.core.logger import logger
from app.agents.llm_client import deepseek_chat
from app.agents import prompts
from app.agents import learning_runner


def _deterministic_rank(items: list[dict], top_n: int | None = None) -> list[dict]:
    """确定性排序（LLM 失败时的降级）：规则概率 × (1 + score_adjust%)。top_n=None → 全部。"""
    scored = []
    for it in items:
        if it.get("action") != "select":
            continue
        prob = float(it.get("up_probability", 0) or 0)
        adj = int(it.get("score_adjust", 0) or 0)
        final = prob * (1 + adj / 100.0)
        scored.append({
            "ts_code": it["ts_code"],
            "final_score": round(min(final, 99.0), 1),
            "final_reason": it.get("reason", "") or "规则精筛保留",
            "confidence": it.get("confidence", "mid"),
        })
    scored.sort(key=lambda x: -x["final_score"])
    return scored[:top_n]


class DecisionAgent:
    """统一决策 Agent（最终决定）。"""

    def decide(self, items: list[dict], top_n: int | None = None) -> list[dict]:
        """items: 个股精筛结果（含 ts_code/action/score_adjust/reason/confidence/up_probability）。
        top_n: None → 全部 select；指定则截取前 top_n。"""
        if not items:
            return []
        selects = [it for it in items if it.get("action") == "select"]
        if not selects:
            logger.info("[decision_agent] 无 select 候选，退回确定性排序")
            return _deterministic_rank(items, top_n)

        # 组装候选列表给 LLM
        cand_lines = []
        for it in selects:
            cand_lines.append(
                f"{it['ts_code']}: 规则概率{it.get('up_probability', 0)}% "
                f"精筛action={it['action']} adjust={it['score_adjust']} "
                f"confidence={it['confidence']} reason={it.get('reason', '')}"
            )
        user = prompts.DECISION_USER_TMPL.format(
            candidates="\n".join(cand_lines)[:6000],
            reflect_kb=learning_runner.build_decision_txt() or "无历史决策教训",
        )
        result = deepseek_chat([
            {"role": "system", "content": prompts.DECISION_SYSTEM},
            {"role": "user", "content": user},
        ])
        if result is None or not result.get("final_ranks"):
            logger.info("[decision_agent] LLM 决策失败，退回确定性排序")
            return _deterministic_rank(selects, top_n)

        ranks = []
        seen = set()
        for r in result.get("final_ranks", []):
            code = str(r.get("ts_code", ""))
            if not code or code in seen:
                continue
            seen.add(code)
            try:
                score = float(r.get("final_score", 0) or 0)
            except Exception:  # noqa: BLE001
                score = 0.0
            ranks.append({
                "ts_code": code,
                "final_score": round(score, 1),
                "final_reason": str(r.get("final_reason", ""))[:80],
                "confidence": str(r.get("confidence", "mid")),
            })
        ranks.sort(key=lambda x: -x["final_score"])
        logger.info(f"[decision_agent] 统一决策完成: {len(ranks)} 条" + (f"（截取 Top-{top_n}）" if top_n else "（全部）"))
        return ranks if not top_n else ranks[:top_n]


def build_final_report(candidates: list[dict], ranks: list[dict], profiles: list[dict]) -> list[dict]:
    """把最终排序结果合并回候选原始字段，生成前端可展示的榜单条目。

    Args:
        candidates: 原始候选池条目
        ranks: decision_agent 输出
        profiles: profile 列表（candidate_profiler 输出）
    """
    cand_by_code = {c.get("ts_code"): c for c in candidates}
    prof_by_code = {p.get("ts_code"): p for p in profiles}
    out = []
    for r in ranks:
        code = r["ts_code"]
        cand = cand_by_code.get(code, {})
        prof = prof_by_code.get(code, {})
        out.append({
            **cand,
            "agent_reason": r.get("final_reason", ""),
            "agent_confidence": r.get("confidence", ""),
            "agent_score": r.get("final_score", cand.get("up_probability", 0) * 100),
            "concepts": prof.get("concepts", []),
        })
    # 重新编号 rank
    for i, item in enumerate(out):
        item["rank"] = i + 1
    return out
