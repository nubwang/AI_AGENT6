"""政策解读 Agent（policy_agent）— 4 子 Agent 协作

对齐 plans/03-Agent系统.md §三（用户指定设计）：
  ① 政策要素抽取 Agent  ② 产业链映射 Agent
  ③ 利好程度分级 Agent  ④ 历史对标分析 Agent

定位：为候选股"所属板块/题材"提供政策催化信号，供个股精筛 Agent 使用。
政策文本来源：Tavily 搜索该板块近期政策（无完整 RSS 采集时的即时方案；
            后续可接入 news_collector.py 的 RSS + 政策知识库 RAG）。

所有子 Agent 失败均降级返回 None，不阻断主流程。
"""
from __future__ import annotations

import json
import os

from app.core.logger import logger
from app.agents.llm_client import deepseek_chat, tavily_search
from app.agents import prompts
from app.agents import policy_learning
from app.agents import evolution_config
from app.data import policy_kb

# 历史政策→板块案例库（第 2 层记忆知识层，可持久化扩充）
# 文件不存在时用内置种子兜底；复盘/反思可追加真实案例
POLICY_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "policy_history.json",
)
_SEED_HISTORY = [
    {"title": "2023年3月 国务院机构改革（组建国家数据局）", "date": "2023-03", "sector": "数据要素/数字经济",
     "ret_1d": 1.2, "ret_3d": 3.5, "ret_7d": 5.1, "ret_30d": 8.4},
    {"title": "2023年5月 国常会支持新能源汽车下乡", "date": "2023-05", "sector": "新能源汽车/充电桩",
     "ret_1d": 0.8, "ret_3d": 2.2, "ret_7d": 3.8, "ret_30d": 6.1},
    {"title": "2024年3月 政府工作报告'人工智能+'行动", "date": "2024-03", "sector": "AI/算力",
     "ret_1d": 1.5, "ret_3d": 4.2, "ret_7d": 6.8, "ret_30d": 12.5},
    {"title": "2024年9月 央行降准降息+存量房贷利率下调", "date": "2024-09", "sector": "大金融/地产",
     "ret_1d": 2.1, "ret_3d": 5.0, "ret_7d": 7.4, "ret_30d": 9.8},
    {"title": "2025年4月 半导体设备材料国产替代政策", "date": "2025-04", "sector": "半导体/设备材料",
     "ret_1d": 1.0, "ret_3d": 3.0, "ret_7d": 5.5, "ret_30d": 9.0},
]


def _load_policy_history() -> list[dict]:
    """加载历史政策→板块案例库（第 2 层记忆知识层）。文件缺失/损坏回退内置种子。"""
    try:
        if os.path.exists(POLICY_HISTORY_FILE):
            with open(POLICY_HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
    except Exception:  # noqa: BLE001
        pass
    return list(_SEED_HISTORY)


def _save_policy_history(history: list[dict]) -> bool:
    try:
        os.makedirs(os.path.dirname(POLICY_HISTORY_FILE), exist_ok=True)
        with open(POLICY_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"政策历史库保存失败: {exc}")
        return False


class PolicyAgent:
    """政策解读 4 子 Agent 编排器。"""

    # ── ① 政策要素抽取 ──
    def extract(self, text: str) -> dict | None:
        user = prompts.POLICY_EXTRACT_USER_TMPL.format(text=text[:4000])
        return deepseek_chat([
            {"role": "system", "content": prompts.POLICY_EXTRACT_SYSTEM},
            {"role": "user", "content": user},
        ])

    # ── ② 产业链映射 ──
    def industry_map(self, elements: dict | None, text: str) -> dict | None:
        supported = (elements or {}).get("support_areas", "")
        if not supported:
            return None
        user = prompts.POLICY_MAP_USER_TMPL.format(
            supported_areas=supported, text=text[:3000],
        )
        return deepseek_chat([
            {"role": "system", "content": prompts.POLICY_MAP_SYSTEM},
            {"role": "user", "content": user},
        ])

    # ── ③ 利好程度分级 ──
    def grade(self, elements: dict | None, mapping: dict | None = None) -> dict | None:
        targets = (elements or {}).get("targets", "")
        measures = (elements or {}).get("measures", "")
        summary = f"目标指标:{targets}；配套措施:{measures}"
        user = prompts.POLICY_GRADE_USER_TMPL.format(summary=summary[:2000])
        return deepseek_chat([
            {"role": "system", "content": prompts.POLICY_GRADE_SYSTEM},
            {"role": "user", "content": user},
        ])

    # ── ④ 历史对标（第 2 层记忆知识层：持久化历史库）──
    def benchmark(self, elements: dict | None) -> dict | None:
        summary = (elements or {}).get("support_areas", "")
        history = "\n".join(
            f"- {h['title']}（{h['date']}）板块{h['sector']} 发布后1/3/7/30天涨幅 "
            f"{h['ret_1d']}/{h['ret_3d']}/{h['ret_7d']}/{h['ret_30d']}%"
            for h in _load_policy_history()
        )
        user = prompts.POLICY_BENCHMARK_USER_TMPL.format(
            summary=summary[:1500], history=history,
        )
        return deepseek_chat([
            {"role": "system", "content": prompts.POLICY_BENCHMARK_SYSTEM},
            {"role": "user", "content": user},
        ])

    # ── ⑤ 校验对齐（第 6 层：防幻觉/防错误，强严谨性关键）──
    def validate(
        self,
        text: str,
        elements: dict | None,
        grade: dict | None,
        mapping: dict | None,
    ) -> dict | None:
        """校验解读结果：原文锚定 + 逻辑自洽 + 风险对齐。

        失败返回 None（不阻断主流程；无校验结论时精筛按原信号处理）。
        """
        try:
            user = prompts.POLICY_VALIDATE_USER_TMPL.format(
                text=text[:3000],
                elements=json.dumps(elements or {}, ensure_ascii=False)[:1200],
                grade=json.dumps(grade or {}, ensure_ascii=False)[:800],
                mapping=json.dumps(mapping or {}, ensure_ascii=False)[:1000],
            )
            return deepseek_chat([
                {"role": "system", "content": prompts.POLICY_VALIDATE_SYSTEM},
                {"role": "user", "content": user},
            ])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[policy_agent] 校验失败: {exc}")
            return None

    # ── ⑥ 潜在意图研判（§19 政策自学层：理解官方书面潜在意思 + 实证参考）──
    def deep_intent_analysis(
        self, sector: str, text: str, elements: dict | None, grade: dict | None,
    ) -> dict | None:
        """基于原文 + 意图库信号 + 行业实证，研判官方书面潜在意图。失败返回 None。"""
        try:
            intent_signals = policy_learning.build_intent_txt(text) or "无命中措辞"
            empirical = policy_learning.build_empirical_txt(sector) or "暂无该行业实证"
            user = prompts.POLICY_DEEP_INTENT_USER_TMPL.format(
                text=text[:2500],
                intent_signals=intent_signals[:400],
                empirical=empirical[:400],
                grade=(grade or {}).get("grade", "未知"),
                support_areas=((elements or {}).get("support_areas", "") or "")[:200],
            )
            return deepseek_chat([
                {"role": "system", "content": prompts.POLICY_DEEP_INTENT_SYSTEM},
                {"role": "user", "content": user},
            ])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[policy_agent] 潜在意图研判失败 {sector}: {exc}")
            return None

    # ── 板块级政策信号（对外主入口）──
    def analyze_sector(self, sector: str) -> dict | None:
        """对某个板块/题材做政策解读，输出催化信号。失败返回 None。

        政策文本来源：RAG 知识库优先（policy_kb，已建库时），回退 Tavily 即时搜索。
        """
        # 1. 获取板块近期政策：RAG 知识库优先（feature-flag 可关），回退 Tavily 即时搜索
        from app.core.config import settings
        rag_enabled = getattr(settings, "policy_rag_enabled", True)
        policy_text = ""
        if rag_enabled:
            try:
                # 优先按板块标签精确过滤 + 时效窗口（可进化 F2/E4）；板块标签未命中时回退语义检索
                max_age = int(evolution_config.get_param("policy_max_age_days", 1095) or 1095)
                hits = policy_kb.retrieve(sector, top_k=3, meta_filter=sector, max_age_days=max_age)
                if not hits:
                    hits = policy_kb.retrieve(sector, top_k=3, max_age_days=max_age)
                if hits:
                    policy_text = "\n".join(
                        f"[{h['doc']}] {h['text'][:500]}" for h in hits[:3]
                    )
                    logger.info(f"[policy_agent] {sector} RAG 检索命中 {len(hits)} 条")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[policy_agent] {sector} RAG 检索失败（回退 Tavily）: {exc}")
        if not policy_text:
            news = tavily_search(f"{sector} 政策 最新 支持 利好", max_results=5)
            if not news:
                logger.info(f"[policy_agent] {sector} 未搜到政策新闻，跳过")
                return None
            policy_text = "\n".join(
                f"[{n.get('title','')}] {n.get('content','')[:500]}" for n in news[:4]
            )
        # 2. 4 子 Agent 串行分析
        elements = self.extract(policy_text)
        mapping = self.industry_map(elements, policy_text)
        grade = self.grade(elements, mapping)
        benchmark = self.benchmark(elements)
        # 3. 校验对齐（第 6 层：原文锚定 + 逻辑自洽 + 风险对齐，防幻觉）
        validation = self.validate(policy_text, elements, grade, mapping)
        # 4. 潜在意图研判（§19 政策自学层：理解官方书面潜在意思 + 实证参考）
        deep = self.deep_intent_analysis(sector, policy_text, elements, grade)
        # 政策兑现度：板块近期涨幅（政策利好是否已被市场反应 / 已涨过 = 政策"过期"）
        market_priced = {}
        try:
            gain = policy_learning.sector_recent_gain(sector, n=10)
            if gain is not None:
                # 兑现度阈值从进化配置读取（Tier1 热生效；J1 读失败回退默认值）
                priced_th = evolution_config.get_param("policy_priced_threshold", 0.15) or 0.15
                partial_th = evolution_config.get_param("policy_partial_threshold", 0.05) or 0.05
                reversal_th = evolution_config.get_param("policy_reversal_threshold", -0.03) or -0.03
                gain_r = round(gain, 4)
                if gain_r >= priced_th:
                    verdict = "利好已兑现（板块近10日已大涨，追高风险）"
                elif gain_r >= partial_th:
                    verdict = "部分反应（板块已涨，注意追高）"
                elif gain_r <= reversal_th:
                    verdict = "板块回调（政策催化尚未被反应）"
                else:
                    verdict = "板块尚未明显反应（催化仍在发酵）"
                market_priced = {
                    "sector": sector,
                    "gain_10d": gain_r,
                    "priced_in": gain_r >= priced_th,
                    "verdict": verdict,
                }
                # 影子实验旁路记录（F1：新阈值判定不改变主流程，供 settle 裁决）
                # Bug12 修复：通用化遍历所有 running 影子（含 partial/reversal），而非仅 priced
                try:
                    import time as _t
                    from app.agents import evolution_shadow
                    for sp, rec in evolution_shadow.shadow_status().items():
                        if not (rec and rec.get("status") == "running"):
                            continue
                        # 计算该参数旧判定（当前 config 阈值）与新判定（影子新阈值）
                        old_th = evolution_config.get_param(sp)
                        if sp == "policy_reversal_threshold":
                            # 回调型：涨幅≤阈值判回调（看多/超跌反弹，方向 rebound）
                            old_priced = gain_r <= (float(old_th) if old_th is not None else -0.03)
                            new_priced = gain_r <= float(rec.get("new"))
                            direction = "rebound"
                        else:  # priced / partial 属"涨幅达阈值"型（避雷，方向 avoid）
                            old_priced = gain_r >= (float(old_th) if old_th is not None else 0.05)
                            new_priced = gain_r >= float(rec.get("new"))
                            direction = "avoid"
                        evolution_shadow.record_verdict(
                            sp, sector, gain_r, old_priced, new_priced, _t.strftime("%Y%m%d"), direction)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[policy_agent] {sector} 兑现度计算失败: {exc}")
        # 信号方向（三态：正面催化 / 中性 / 反向避雷）：
        # 结合政策本身方向（grade: S/A/B/C=利好, 利空）+ 市场兑现度（板块是否已涨过 = 政策"过期"）
        g_txt = str((grade or {}).get("grade", ""))
        gain = market_priced.get("gain_10d", 0.0)
        priced = market_priced.get("priced_in", False)
        direction, dlabel, dreason = "neutral", "中性", "政策方向或市场反应不明"
        if "利空" in g_txt:
            direction, dlabel = "reverse", "反向（政策本身利空）"
            dreason = "政策属监管收紧/限制性/补贴退坡等，本身构成利空"
        elif priced:
            direction, dlabel = "reverse", "反向（利好出尽）"
            dreason = f"政策利好但板块近10日已涨{gain * 100:+.0f}%，利好兑现，出货/回调风险高"
        elif gain >= 0.05:
            if g_txt in ("S", "A"):
                direction, dlabel = "positive", "正面（部分兑现）"
                dreason = f"板块近10日{gain * 100:+.0f}%已部分反应，注意追高"
            else:
                direction, dlabel = "neutral", "中性（部分兑现）"
                dreason = f"板块近10日{gain * 100:+.0f}%已部分反应，政策力度一般"
        elif g_txt in ("S", "A", "B", "C", "利好"):
            direction, dlabel = "positive", "正面（催化未兑现）"
            dreason = f"板块近10日{gain * 100:+.0f}%尚未明显反应，催化仍在发酵"
        result = {
            "sector": sector,
            "policy_text_snippet": policy_text[:800],
            "elements": elements or {},
            "mapping": mapping or {},
            "grade": grade or {},
            "benchmark": benchmark or {},
            "validation": validation or {},
            "deep_intent": deep or {},
            "market_priced": market_priced,
            "signal_direction": direction,
            "direction_label": dlabel,
            "direction_reason": dreason,
            "intent_signals": policy_learning.detect_intents(policy_text),
        }
        grade_txt = (grade or {}).get("grade", "未知")
        v_txt = (validation or {}).get("verdict", "未校验")
        logger.info(f"[policy_agent] {sector} 政策解读完成: 等级 {grade_txt}，校验 {v_txt}")
        # 记录解读事件 → 板块跟踪自学（§19：解读后跟踪板块表现，越用越聪明）
        policy_learning.record_policy_interpretation(
            sector, grade_txt,
            str((mapping or {}).get("direct", "") or ""),
            policy_learning.build_intent_txt(policy_text),
            policy_text,
        )
        return result

    def signal_to_text(self, sig: dict | None) -> str:
        """催化信号 → prompt 用文本。无信号返回'无明确政策催化信号'。"""
        if not sig:
            return "无明确政策催化信号"
        g = sig.get("grade") or {}
        e = sig.get("elements") or {}
        b = sig.get("benchmark") or {}
        v = sig.get("validation") or {}
        d = sig.get("deep_intent") or {}
        parts = [
            f"政策等级 {g.get('grade', '未知')}（{g.get('summary', '')}）",
            f"支持领域 {e.get('support_areas', '未明确')[:120]}",
            f"历史对标 {b.get('observation', '暂无')}",
        ]
        # 潜在意图研判标注（§19：官方书面潜在意思）
        if d:
            if d.get("official_intent"):
                parts.append(f"潜在意图 {d.get('official_intent')}")
            if d.get("expectation_gap"):
                parts.append(f"预期差{d.get('expectation_gap')}")
            if d.get("risk_hint"):
                parts.append(f"风险 {d.get('risk_hint')}")
        # 校验对齐标注：需修正/存在脑补/漏风险时给精筛明确降权提示
        if v:
            flags = []
            if v.get("unfounded"):
                flags.append("含脑补:" + "、".join(str(x) for x in v["unfounded"][:2]))
            if v.get("missed_risks"):
                flags.append("漏利空:" + "、".join(str(x) for x in v["missed_risks"][:2]))
            if v.get("verdict") == "需修正":
                flags.append("需修正:" + str(v.get("suggestion", "")))
            if flags:
                parts.append("⚠️校验:" + "；".join(flags))
            elif v.get("verdict") == "通过":
                parts.append("校验通过")
        # 信号方向（三态）：反向信号最前置，作为"反面消息"明确指示精筛避雷
        sd = sig.get("signal_direction")
        if sd == "reverse":
            parts.insert(0, f"⚠️反面信号:{sig.get('direction_label', '反向')}；"
                            f"{sig.get('direction_reason', '利好已兑现或政策利空，建议规避')}")
        elif sd == "positive":
            parts.insert(0, f"正向催化:{sig.get('direction_label', '正面')}")
        # 市场兑现度（政策是否已被板块上涨 price in；已涨过 → 利好过期/追高风险）
        mp = sig.get("market_priced") or {}
        if mp and mp.get("verdict"):
            g = mp.get("gain_10d")
            tag = "⚠️兑现:" if mp.get("priced_in") else "兑现度:"
            if isinstance(g, (int, float)):
                sign = "+" if g >= 0 else ""
                parts.append(f"{tag}{mp.get('verdict')}（近10日板块{sign}{g * 100:.0f}%）")
            else:
                parts.append(f"{tag}{mp.get('verdict')}")
        return "；".join(p for p in parts if p)
