"""验收：LLM 返回「形状不符」（数组而非对象）时，**精筛链路不再被打崩**（2026-09-29）

## 事故背景（用户报错原话）

    失败Agent 精筛失败: 'list' object has no attribute 'get'

根因**不是**模型/网络故障，而是**契约假设**错了：
[`deepseek_chat()`](ai-quant-agent/backend/app/agents/llm_client.py:222) 返回的是
「模型实际输出的 JSON」，类型由模型决定（内部 [`_extract_json()`](ai-quant-agent/backend/app/agents/llm_client.py:179)
**明确会返回 list**）。prompt 里写着"输出 JSON 对象"，模型偶尔仍输出 `[ {...} ]`
（把对象包一层数组）或直接输出数组 ⇒ 消费方直接 `result.get(...)` 就崩成
`'list' object has no attribute 'get'`。

## 崩点清单（本次修复）

  ① [`decision_agent.decide()`](ai-quant-agent/backend/app/agents/decision_agent.py:43) —— L3 最后一环，
     **外层没有 try** ⇒ 整轮精筛判 failed、**当日榜单一条都不落盘**（L1/L2 几十次 LLM 调用全白花）。
     这就是本次用户看到的那条报错（前端 `_AGENT_STATE["message"] = f"Agent 精筛失败: {exc}"`）。
  ② [`refine_agent.refine()`](ai-quant-agent/backend/app/agents/refine_agent.py:42) —— `reason`/`confidence`
     取值**在 try 之外** ⇒ AttributeError 逃出，被上层"逐只 try"记成"个股精筛失败"：表面单只失败，
     实际**每一只都降级**（LLM 精筛整层失效）。
  ③ [`news_agent.verify()`](ai-quant-agent/backend/app/agents/news_agent.py:48) —— 抛错被上层 try 吞掉
     ⇒ **防烟雾弹/防利好出货的硬性兜底静默失效**（比崩更危险：看起来一切正常，其实没有避雷能力）。
  ④ [`refine_agent.batch_refine()`](ai-quant-agent/backend/app/agents/refine_agent.py:115) —— 只认
     `isinstance(result, list)` ⇒ `{"candidates": [...]}` 被当"解析异常"，该批**只留 1 只**
     （榜单规模静默缩水）。
  ⑤ [`policy_agent.analyze_sector()`](ai-quant-agent/backend/app/agents/policy_agent.py:171) 子 Agent 结果
     被 `(grade or {}).get(...)` 消费 ⇒ 整块政策信号被丢弃（调用点 try 只会记"政策解读失败"）。
  ⑥ [`reflect_agent`](ai-quant-agent/backend/app/agents/reflect_agent.py:185) /
     [`learning_runner`](ai-quant-agent/backend/app/agents/learning_runner.py:180) 反思固化 ——
     一条坏返回会**打断整批**反思（后面的案例不再处理）。

## 修复口径（三条，别走回头路）

  1. 规整工具 [`as_dict()`](ai-quant-agent/backend/app/agents/llm_client.py:205) /
     [`as_rows()`](ai-quant-agent/backend/app/agents/llm_client.py:227) 放在**消费侧**，
     **不改 `deepseek_chat` 的返回契约** —— `batch_refine` / `evolution_agent` 确实需要"列表语义"，
     在 LLM 层统一归一化会把这些路径的语义吃掉。
  2. **多元素数组不折叠成单对象**：强行折叠 = 静默丢数据（N 条压成 1 条），比崩溃更糟。
  3. L3 仍然加一层 try/except 兜底：即使出现"没预料到的形状"，也退回确定性排序 ——
     保证「有榜单 > 榜单完美」。

## 方式

全部**打桩**（伪造 `deepseek_chat` / Tavily / 画像 / 落盘），
**不发任何真实 API 请求、不写任何真实数据文件**（`data/decision_records.json` 等一律 mock）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_llm_json_shape.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

import app.agents as AG  # noqa: E402
from app.agents import llm_client as LC  # noqa: E402
from app.agents import refine_agent as RA  # noqa: E402
from app.agents import decision_agent as DA  # noqa: E402
from app.agents import news_agent as NA  # noqa: E402
from app.agents import policy_agent as PA  # noqa: E402
from app.agents import reflect_agent as RF  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS: list[str] = []
FAIL: list[str] = []


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


ITEMS = [
    {"ts_code": "600001.SH", "action": "select", "score_adjust": 5,
     "confidence": "mid", "reason": "形态好", "up_probability": 66.0},
    {"ts_code": "600002.SH", "action": "select", "score_adjust": 0,
     "confidence": "low", "reason": "一般", "up_probability": 55.0},
]
FAKE_PROFILES = [
    {"ts_code": "600006.SH", "name": "A", "industry": "半导体",
     "sectors": ["半导体"], "up_probability": 70.0},
    {"ts_code": "600007.SH", "name": "B", "industry": "半导体",
     "sectors": ["半导体"], "up_probability": 60.0},
]


def main() -> int:
    print("=" * 100)
    print("LLM 返回形状不符（数组）⇒ 精筛链路不得被打崩（事故回归验收）")
    print("=" * 100)

    # ── ① 规整工具语义（确定性，先证明工具本身对）──
    print("\n① as_dict / as_rows 语义（多元素数组**不折叠**：折叠=静默丢数据）")
    print("-" * 100)
    ck("A1 dict 原样返回", LC.as_dict({"a": 1}) == {"a": 1})
    ck("A2 单元素数组（把对象包一层）⇒ 取出该对象", LC.as_dict([{"a": 1}]) == {"a": 1})
    ck("A3 多元素数组**不折叠**（返回 {}，交调用方按列表语义处理）",
       LC.as_dict([{"a": 1}, {"b": 2}]) == {})
    ck("A4 None / 标量 / 空 ⇒ {}", LC.as_dict(None) == {} and LC.as_dict("x") == {}
       and LC.as_dict(3) == {} and LC.as_dict([]) == {})
    ck("B1 list[dict] ⇒ 行列表（丢掉非 dict 元素）",
       LC.as_rows([{"ts_code": "1"}, "噪音", {"ts_code": "2"}]) == [{"ts_code": "1"}, {"ts_code": "2"}])
    ck("B2 对象里嵌列表 {\"candidates\": [...]} ⇒ 取出该列表",
       [r["ts_code"] for r in LC.as_rows({"candidates": [{"ts_code": "1"}, {"ts_code": "2"}]})]
       == ["1", "2"])
    ck("B3 单条结论被当对象返回 ⇒ [obj]", len(LC.as_rows({"ts_code": "1", "action": "select"})) == 1)
    ck("B4 无行可提取 ⇒ []", LC.as_rows({"reason": "x"}) == [] and LC.as_rows(None) == [])

    # ── ② L3 统一决策（本次崩溃点）──
    print("\n② L3 统一决策：LLM 返回数组不再崩（事故原报错点）")
    print("-" * 100)
    with mock.patch.object(DA, "deepseek_chat", return_value=[
            {"ts_code": "600002.SH", "final_score": 90},
            {"ts_code": "600001.SH", "final_score": 80}]):
        r1 = DA.DecisionAgent().decide(ITEMS, top_n=None)
    ck("C1 LLM 直接把 final_ranks 数组当结果返回 ⇒ 不再抛 'list' object has no attribute 'get'",
       [x["ts_code"] for x in r1] == ["600002.SH", "600001.SH"], str(r1))

    with mock.patch.object(DA, "deepseek_chat", return_value=[
            {"final_ranks": [{"ts_code": "600001.SH", "final_score": 70},
                             {"ts_code": "600002.SH", "final_score": 60}]}]):
        r2 = DA.DecisionAgent().decide(ITEMS)
    ck("C2 单元素数组包裹对象 ⇒ 正常解析（不被误判为列表语义）",
       [x["ts_code"] for x in r2] == ["600001.SH", "600002.SH"], str(r2))

    with mock.patch.object(DA, "deepseek_chat",
                           return_value=[{"ts_code": "600001.SH", "final_score": 70}, "垃圾行", 42]):
        r3 = DA.DecisionAgent().decide(ITEMS)
    ck("C3 数组里混入非 dict 元素 ⇒ 跳过而不是崩",
       len(r3) == 1 and r3[0]["ts_code"] == "600001.SH", str(r3))

    with mock.patch.object(DA, "deepseek_chat", return_value="完全不是 JSON 的一段话"):
        r4 = DA.DecisionAgent().decide(ITEMS)
    ck("C4 形状无法解释 ⇒ 退回确定性排序（榜单仍非空，绝不返回 []）",
       len(r4) == 2 and r4[0]["ts_code"] == "600001.SH", str(r4))

    # ── ③ L2 逐只精筛 / L1 批量粗筛 ──
    print("\n③ 个股精筛：包一层数组要能用；批量粗筛：对象里嵌列表不得再丢候选")
    print("-" * 100)
    prof = {"ts_code": "600003.SH", "up_probability": 60}
    with mock.patch.object(RA, "deepseek_chat",
                           return_value=[{"action": "select", "score_adjust": 3,
                                          "reason": "包了一层数组", "confidence": "mid"}]):
        rr = RA.RefineAgent().refine(prof, "", "")
    ck("D1 逐只精筛：LLM 返回 `[ {...} ]` ⇒ 规整后正常取字段（原报错点之一）",
       rr["ts_code"] == "600003.SH" and rr["action"] == "select"
       and rr["score_adjust"] == 3 and rr["confidence"] == "mid", str(rr))

    with mock.patch.object(RA, "deepseek_chat", return_value=[{"a": 1}, {"b": 2}]):
        rr2 = RA.RefineAgent().refine(prof, "", "")
    ck("D2 逐只精筛：形状无法解释 ⇒ 如实降级（按规则概率保留，不崩、不静默改分）",
       rr2["action"] == "select" and rr2["score_adjust"] == 0 and "格式异常" in rr2["reason"],
       str(rr2))

    plist = [{"ts_code": f"60010{i}.SH", "industry": "半导体", "up_probability": 50 + i}
             for i in range(4)]
    with mock.patch.object(RA, "deepseek_chat",
                           return_value={"candidates": [{"ts_code": p["ts_code"], "action": "select"}
                                                        for p in plist]}):
        kept_obj = RA.RefineAgent().batch_refine(list(plist), batch_size=10, per_industry=10)
    ck("E1 批量粗筛：`{\"candidates\": [...]}` ⇒ 不再被当异常（原来该批只留 1 只）",
       len(kept_obj) == 4, f"kept={len(kept_obj)}（修复前=1）")

    with mock.patch.object(RA, "deepseek_chat",
                           return_value=[{"ts_code": p["ts_code"], "action": "select"} for p in plist]):
        kept_arr = RA.RefineAgent().batch_refine(list(plist), batch_size=10, per_industry=10)
    ck("E2 批量粗筛：纯数组形态仍按列表语义处理（没把能力吃掉）",
       len(kept_arr) == 4, f"kept={len(kept_arr)}")

    # ── ④ 消息面 Agent（防烟雾弹硬兜底不得静默失效）──
    print("\n④ 消息面 Agent：返回形状不符 ⇒ 降级为「存疑」，绝不静默丢掉避雷能力")
    print("-" * 100)
    # ★ 打桩打在 `llm_client.deepseek_chat` 上（不是 news_agent 里的名字）：
    #   news_agent 现在用 `deepseek_chat_obj()` 包装，包装内部才调 `deepseek_chat`。
    #   这样测的正是**真实包装 + 真实 verify 路径**（若只桩掉 NA 的名字，等于绕过被测代码）。
    _patch_news = {
        "tavily_search": mock.patch.object(NA, "tavily_search",
                                           return_value=[{"title": "t", "content": "c"}]),
        "_recent_gain": mock.patch.object(NA, "_recent_gain", return_value=0.03),
        "record_news_verdict": mock.patch.object(NA.learning_runner, "record_news_verdict",
                                                 lambda *a, **k: None),
    }

    def _verify_with(llm_return):
        """用给定的 LLM 原始返回跑一次 NewsAgent.verify()（Tavily/DB/落盘全打桩）。"""
        with _patch_news["tavily_search"], _patch_news["_recent_gain"], \
                _patch_news["record_news_verdict"], \
                mock.patch.object(LC, "deepseek_chat", return_value=llm_return):
            return NA.NewsAgent().verify({"ts_code": "600004.SH", "name": "测试"})

    # F1a 单元素数组（把对象包一层）= **可救回**：规整后正常解析，信号不丢
    sig1 = _verify_with([{"verdict": "利好", "smoke_risk": "中", "distribution_risk": "无",
                          "reason": "包了一层数组", "key_points": ["公告"]}])
    ck("F1 verify + 单元素数组：规整后**正常解析**（不再把结论丢掉）",
       isinstance(sig1, dict) and sig1.get("verdict") == "利好"
       and sig1.get("smoke_risk") == "中" and bool(sig1.get("raw_news")),
       str(sig1)[:120])

    # F1b 多元素数组（无法当对象）= 不可救回：退回「存疑」而非抛异常
    sig2 = _verify_with([{"verdict": "利好"}, {"verdict": "利空"}])
    ck("F1b verify + 多元素数组：退回「存疑」+ 搜索摘要（不崩、也不漏判为无风险）",
       isinstance(sig2, dict) and sig2.get("verdict") == "存疑" and bool(sig2.get("raw_news")),
       str(sig2)[:120])
    # `# type: ignore[arg-type]`：本用例**故意**传非法形状（数组），
    # 类型标注写 dict|None 是"正常契约"，运行时兜底见 news_agent.signal_to_text。
    ck("F2 signal_to_text 收到非 dict ⇒ 按「无消息面信号」处理（不打断逐只循环）",
       NA.NewsAgent().signal_to_text([]) == "无消息面信号")  # type: ignore[arg-type]

    # ── ⑤ 政策 Agent ──
    print("\n⑤ 政策 Agent：子 Agent 返回数组 ⇒ 规整为 {}（不再整块丢弃政策信号）")
    print("-" * 100)
    ck("G1 signal_to_text 收到非 dict ⇒ 按「无明确政策催化信号」处理",
       PA.PolicyAgent().signal_to_text([]) == "无明确政策催化信号")  # type: ignore[arg-type]
    pa = PA.PolicyAgent()
    with mock.patch.object(PA.policy_kb, "retrieve", return_value=[]), \
            mock.patch.object(PA, "tavily_search", return_value=[{"title": "政策", "content": "支持半导体"}]), \
            mock.patch.object(pa, "extract", return_value=[{"x": 1}, {"y": 2}]), \
            mock.patch.object(pa, "industry_map", return_value=None), \
            mock.patch.object(pa, "grade", return_value=[{"a": 1}]), \
            mock.patch.object(pa, "benchmark", return_value=None), \
            mock.patch.object(pa, "validate", return_value=[{"b": 1}]), \
            mock.patch.object(pa, "deep_intent_analysis", return_value=None), \
            mock.patch.object(PA.policy_learning, "sector_recent_gain", return_value=None), \
            mock.patch.object(PA.policy_learning, "detect_intents", return_value=[]), \
            mock.patch.object(PA.policy_learning, "record_policy_interpretation", lambda *a, **k: None):
        psig = pa.analyze_sector("半导体")
    ck("G2 analyze_sector：子 Agent 返回数组 ⇒ 不抛 'list' object has no attribute 'get'"
       "（多元素规整为 {}，单元素数组拆包）",
       isinstance(psig, dict) and psig.get("elements") == {} and psig.get("grade") == {"a": 1},
       str(psig)[:160])

    # ── ⑥ 反思固化（慢路径：一条坏返回不得打断整批）──
    print("\n⑥ 反思固化：非 dict 结论不得抛异常 / 不得污染教训库")
    print("-" * 100)
    kb = {"reflections": [], "stats": {}}
    try:
        RF._upsert_lesson(kb, [1, 2, 3], "600005.SH", "20260901")  # type: ignore[arg-type]
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    ck("H1 reflect_agent：非 dict 结果 ⇒ 落一条「未总结」，而不是抛 AttributeError",
       (not raised) and bool(kb["reflections"])
       and kb["reflections"][0]["error_type"] == "其他",
       "raised" if raised else str(kb["reflections"][:1]))
    lr_src = open(os.path.join(BACKEND, "app", "agents", "learning_runner.py"), encoding="utf-8").read()
    ck("H2 learning_runner：消息面/决策反思都对结果做了 dict 形状判定（源码级防回归）",
       lr_src.count("if isinstance(result, dict):") >= 2,
       f"命中 {lr_src.count('if isinstance(result, dict):')} 处")

    # ── ⑦ 端到端：复现用户那句报错，确认整轮不再判 failed ──
    print("\n⑦ 端到端：L3 抛「'list' object has no attribute 'get'」时，整轮精筛不得判 failed")
    print("-" * 100)
    with mock.patch.object(AG, "dump_profiles", lambda cands, n: [dict(p) for p in FAKE_PROFILES]), \
            mock.patch.object(AG.RefineAgent, "batch_refine",
                              lambda self, *a, **k: [dict(p) for p in FAKE_PROFILES]), \
            mock.patch.object(AG.RefineAgent, "refine",
                              lambda self, p, *a, **k: {
                                  "ts_code": p["ts_code"], "action": "select", "score_adjust": 0,
                                  "confidence": "mid", "reason": "打桩", "up_probability": p["up_probability"]}), \
            mock.patch.object(AG.PolicyAgent, "analyze_sector", lambda self, s: None), \
            mock.patch.object(AG.NewsAgent, "verify", lambda self, p: None), \
            mock.patch.object(AG.DecisionAgent, "decide",
                              side_effect=AttributeError("'list' object has no attribute 'get'")), \
            mock.patch.object(AG, "record_decision_picks", lambda picks: None), \
            mock.patch.object(LC, "tavily_pending_size", lambda: 0), \
            mock.patch.object(LC, "tavily_drain_pending", lambda limit=5: {"tried": 0, "ok": 0, "left": 0}):
        res = AG.run_agent_refine([dict(p) for p in FAKE_PROFILES])
    ck("I1 整轮精筛 **status=ok**（事故前：status=failed + 「Agent 精筛失败: ...」）",
       res.get("status") == "ok", str(res.get("status")))
    ck("I2 榜单仍产出（退回确定性排序，而不是「一条都不落盘」）",
       len(res.get("top_picks") or []) == 2, f"top_picks={len(res.get('top_picks') or [])}")

    print("\n" + "=" * 100)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("全部通过：LLM 返回形状不符时，精筛链路（L1/L2/L3 + 消息面/政策 + 反思）都能安全降级。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
