"""消息面验证 Agent（news_agent）

对齐 plans/03-Agent系统.md §四 + 用户完善要求（防烟雾弹 + 防利好出货）：
  用 Tavily 实时搜索候选股近期新闻/公告/题材，结合**当前涨幅**识别：
    ① 利好落地出货（股价已大涨 + 利好 = 利好兑现，追高风险）
    ② 烟雾弹/假利好（蹭热点、与主营无关、传闻低可信、利好伴随减持/定增）
    ③ 超跌反抽/题材垃圾（图形相似但无实质驱动）

失败降级：未搜到新闻 → verdict=存疑，不阻断；LLM 判断失败 → 返回搜索摘要级信号。
"""
from __future__ import annotations

from sqlalchemy import text

from app.models import SessionLocal
from app.agents.llm_client import deepseek_chat, tavily_search
from app.agents import prompts
from app.agents import learning_runner
from app.agents import evolution_config


def _recent_gain(ts_code: str, n: int | None = None) -> float | None:
    """近 n 个交易日累计涨幅（用于判断'利好出货'：涨幅越大+利好→出货风险越高）。

    窗口可进化（F2/E4）：读 evolution_config 的 news_gain_window，缺省 10。
    """
    if n is None:
        n = int(evolution_config.get_param("news_gain_window", 10) or 10)
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT close FROM daily WHERE ts_code=:c ORDER BY trade_date DESC LIMIT :n"),
            {"c": ts_code, "n": n + 1},
        ).fetchall()
        closes = [float(r[0]) for r in rows if r[0] is not None]
        if len(closes) >= 2 and closes[-1]:
            return closes[0] / closes[-1] - 1.0
    except Exception:  # noqa: BLE001
        pass
    finally:
        db.close()
    return None


class NewsAgent:
    """消息面验证 Agent（防烟雾弹 + 防利好出货）。"""

    def verify(self, profile: dict) -> dict | None:
        """对单只候选做消息面验证。

        Args:
            profile: candidate_profiler.build_candidate_profile 输出

        Returns:
            {verdict, reason, key_points, smoke_risk, distribution_risk, confidence, raw_news}
        """
        ts_code = profile.get("ts_code", "")
        name = profile.get("name", "")
        sectors = ",".join(profile.get("sectors") or profile.get("concepts") or [])
        query = f"{name} {ts_code} 最新消息 公告"
        if sectors:
            query += f" {sectors[:30]}"
        news = tavily_search(query, max_results=5)
        if not news:
            return {
                "verdict": "存疑", "reason": "未获取到消息面信息",
                "key_points": [], "smoke_risk": "无", "distribution_risk": "无",
                "confidence": "low", "raw_news": [],
            }
        # 量化状态：近期涨幅（判断利好出货的关键）+ 综合概率/形态
        gain = _recent_gain(ts_code)
        quant = (
            f"综合上涨概率{profile.get('up_probability', 0)}%，形态{profile.get('form_type', '')}，"
            f"近10日涨幅{'%.1f%%' % (gain * 100) if gain is not None else '未知'}"
        )
        news_txt = "\n".join(
            f"- [{n.get('title', '')}] {n.get('content', '')[:300]}" for n in news[:5]
        )
        user = prompts.NEWS_USER_TMPL.format(
            name=name or ts_code, ts_code=ts_code, concepts=sectors or "无",
            quant=quant, news=news_txt[:3000],
            news_lessons=learning_runner.build_news_txt() or "无历史消息面教训",
        )
        result = deepseek_chat([
            {"role": "system", "content": prompts.NEWS_SYSTEM},
            {"role": "user", "content": user},
        ])
        if result is None:
            # LLM 失败：退回搜索摘要，标记存疑
            return {
                "verdict": "存疑",
                "reason": "消息面判断失败，仅提供搜索摘要供参考",
                "key_points": [n.get("title", "") for n in news[:3]],
                "smoke_risk": "无", "distribution_risk": "无",
                "confidence": "low", "raw_news": news,
            }
        result["raw_news"] = news
        # 记录消息面判定 → T+5 后反思（§20 统一学习能力）
        learning_runner.record_news_verdict(
            ts_code, name, str(result.get("verdict", "存疑")),
            str(result.get("smoke_risk", "无")), str(result.get("distribution_risk", "无")),
            str(result.get("reason", "")),
        )
        return result

    def signal_to_text(self, sig: dict | None) -> str:
        """消息面信号 → prompt 用文本（含烟雾弹/出货风险标注）。"""
        if not sig:
            return "无消息面信号"
        pts = sig.get("key_points") or []
        risks = []
        smoke = sig.get("smoke_risk")
        if smoke and smoke != "无":
            risks.append(f"烟雾弹[{smoke}]")
        dist = sig.get("distribution_risk")
        if dist and dist != "无":
            risks.append(f"出货风险[{dist}]")
        risk_txt = "；".join(risks)
        base = (
            f"判定 {sig.get('verdict', '存疑')}（{sig.get('reason', '')}）"
            f" 要点 {'；'.join(pts[:3]) if pts else '无'}"
        )
        return base + (f" ⚠️{risk_txt}" if risk_txt else "")
