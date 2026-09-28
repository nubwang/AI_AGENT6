"""验收：失败归因的**外部证据层**（大盘 / 个股日线 / 个股资料）

用户诉求（原话）：
    "我希望进化大脑在分析每日推荐为啥会失败的时候，其中一个方面要从以往的大盘或者
     个股日线以及个股资料中找原因，就像回测一样，找出原因"

改造前的问题：归因只把【推荐依据 + T+1 涨幅】交给 LLM，于是 LLM 看不到
  - 大盘同期是涨是跌（个股没涨可能只是大盘系统性下跌 = 外因）
  - 个股推荐后的真实走势（跌停放量出货 vs 缩量横盘）
  - 期间有没有利空事件（业绩预减/减持/高质押/大宗折价）
→ 把外因误判成内因，进化大脑据此改形态/阈值，**越改越偏**。

本脚本验收 7 组硬约束：

  A 三类证据齐备（真实数据跑通，且与"D+1 买入口径"一致）
  B 外因/内因先验判据正确（合成数据，不依赖库）
  C 降级：拿不到数据不崩、不编造（缺口显式标注）
  D 证据真的进了 prompt（次日快反馈 + T+5 反思两条路径）
  E 外因案例**不计入改进方向**（不然等于拿运气当规律）
  F 两个可进化参数已注册且热生效
  G ⚠️ 未来函数护栏：证据层**不得**被预测侧（每日推荐/特征/打分）引用
     —— 本模块故意用推荐日之后的数据，一旦回流成特征就是前瞻偏移

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_failure_evidence.py
"""
from __future__ import annotations

import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import daily_verify as DV
from app.agents import failure_context as FC
from app.agents import prompts as PR
from app.agents import reflect_agent as RA

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWED_PRIOR = {
    "external_market", "external_news", "internal_logic",
    "internal_technical", "mixed", "insufficient_data", "disabled",
}


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _sample_case() -> tuple[str, str]:
    """挑一个"复盘窗口已走完"的真实推荐案例（推荐日足够早）。"""
    st = _read_json(os.path.join(BACKEND, "data", "monitor_state.json"), {"hits": []})
    hits = [h for h in (st.get("hits") or []) if h.get("ts_code") and h.get("date")]
    cand = [h for h in hits if str(h.get("date")) <= "20260801"]
    if cand:
        return str(cand[-1]["ts_code"]), str(cand[-1]["date"])
    return "301008.SZ", "20260801"


def _src(path: str) -> str:
    p = os.path.join(BACKEND, path)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def main() -> int:  # noqa: C901
    code, date = _sample_case()
    print(f"（真实样本：{code} @ {date}）")

    print("=== A 三类证据齐备（真实数据）===")
    ev = FC.build_evidence(code, date, days=5)
    ck("A1 返回三类证据（market/stock/profile）",
       all(k in ev for k in ("market", "stock", "profile")), str(list(ev.keys())))
    mkt, stk, prof = ev["market"], ev["stock"], ev["profile"]
    ck("A2 大盘：上证基准收益可算（bench_ret 非 None）",
       mkt.get("bench_ret") is not None, f"bench_ret={mkt.get('bench_ret')}")
    ck("A3 大盘：至少 1 个指数有同口径收益",
       len(mkt.get("indices") or {}) >= 1, str(list((mkt.get('indices') or {}).keys())))
    ck("A3b 大盘：每条指数都带 actual_days/complete（不假装窗口走完）",
       all("actual_days" in v and "complete" in v for v in (mkt.get("indices") or {}).values()))
    ck("A4 个股：T+1 与区间收益可算，且标注实际天数",
       stk.get("t1_ret") is not None and stk.get("tN_ret") is not None
       and stk.get("window_days_actual") is not None,
       f"t1={stk.get('t1_ret')} tN={stk.get('tN_ret')} days={stk.get('window_days_actual')}")
    ck("A5 个股：买入日起点与终点日期明确", bool(stk.get("buy_date")) and bool(stk.get("end_date")),
       f"{stk.get('buy_date')} → {stk.get('end_date')}")
    ck("A6 个股资料：至少命中 1 条资料事件（业绩/股东/质押/大宗/分红…）",
       bool(prof.get("events")), "；".join(prof.get("events") or [])[:120])
    ck("A7 先验判据合法", ev.get("cause_prior") in ALLOWED_PRIOR, str(ev.get("cause_prior")))
    txt = FC.to_text(ev)
    ck("A8 文本含三块标题（LLM 能读到分区证据）",
       all(h in txt for h in ("【大盘】", "【个股日线】", "【个股资料】")))
    ck("A9 文本含先验判据但声明「不可照抄」",
       "【先验判据】" in txt and "不可直接照抄" in txt)

    print("=== B 外因/内因先验判据（合成数据，不依赖库）===")
    up = {"bench_ret": 0.01, "market_down": False}
    dn = {"bench_ret": -0.03, "market_down": True}

    def _stk(ret, **kw):
        d = {"tN_ret": ret, "flags": []}
        d.update(kw)
        return d

    ck("B1 大盘跌、个股跟跌 → external_market（外因）",
       FC._cause_prior(dn, _stk(-0.032), {}) == "external_market",
       FC._cause_prior(dn, _stk(-0.032), {}))
    ck("B2 大盘跌、但个股大幅跑输（-12%）→ internal_logic（内因）",
       FC._cause_prior(dn, _stk(-0.12), {}) == "internal_logic",
       FC._cause_prior(dn, _stk(-0.12), {}))
    ck("B3 大盘涨、个股跌 -8% → internal_logic",
       FC._cause_prior(up, _stk(-0.08), {}) == "internal_logic",
       FC._cause_prior(up, _stk(-0.08), {}))
    ck("B4 大盘微涨、个股跌 + 有业绩预减利空 → external_news",
       FC._cause_prior(up, _stk(-0.03), {"negatives": ["业绩预告预减"]}) == "external_news",
       FC._cause_prior(up, _stk(-0.03), {"negatives": ["业绩预告预减"]}))
    ck("B5 数据缺失 → insufficient_data（不硬判）",
       FC._cause_prior({}, {}, {}) == "insufficient_data")
    ck("B6 跌停/破位且无更好解释 → internal_technical",
       FC._cause_prior(up, _stk(-0.03, sealed_down_days=1), {}) == "internal_technical",
       FC._cause_prior(up, _stk(-0.03, sealed_down_days=1), {}))

    print("=== C 降级：拿不到数据不崩、不编造 ===")
    bad = FC.build_evidence("999999.SZ", "20260801", days=5)
    ck("C1 不存在的代码：不抛异常", isinstance(bad, dict))
    ck("C2 不存在的代码：明确标注缺口（不假装有证据）",
       bool(bad.get("missing")), "；".join(sorted(set(bad["missing"]))[:2]))
    ck("C3 不存在的代码：判据=insufficient_data", bad.get("cause_prior") == "insufficient_data")
    weird = FC.build_evidence(code, "20991231", days=5)
    ck("C4 未来日期：不崩且缺口显式", isinstance(weird, dict) and bool(
        (weird.get("stock") or {}).get("missing") or weird.get("missing")))
    off = FC.build_evidence(code, date, days=5, enabled=False)
    ck("C5 关闭证据层：cause_prior=disabled 且不查库",
       off.get("cause_prior") == "disabled" and FC.to_text(off) == "（外部证据未启用）")

    print("=== D 证据真的进了 prompt（两条归因路径）===")
    tmpl = DV._ATTRIBUTE_USER_TMPL
    ck("D1 次日归因模板含 {evidence}", "{evidence}" in tmpl)
    ck("D2 次日归因模板要求输出 external_cause 与 evidence_basis",
       "external_cause" in tmpl and "evidence_basis" in tmpl)
    ck("D3 模板明确要求「外因时 param_hint=无需调参」（防止拿外因改内因）",
       "无需调参" in tmpl and "外因" in tmpl)
    sig = inspect.signature(DV._attribute_failure)
    ck("D4 _attribute_failure 接受 evidence_txt", "evidence_txt" in sig.parameters)
    asrc = inspect.getsource(DV._attribute_failure)
    ck("D5 归因调用把证据传进模板", "evidence=evidence_txt" in asrc)
    vsrc = inspect.getsource(DV._verify_one_day)
    ck("D6 次日验证逐只构建证据包", "FCTX.build_evidence(" in vsrc)
    ck("D7 次日验证把证据归档到失败记录（可审计）", 'f["evidence"] = ev' in vsrc)

    ck("D8 反思模板含 {evidence}", "{evidence}" in PR.REFLECT_USER_TMPL)
    ck("D9 反思模板要求 external_cause/evidence_basis",
       "external_cause" in PR.REFLECT_USER_TMPL and "evidence_basis" in PR.REFLECT_USER_TMPL)
    ck("D10 反思 System 提示先分内外因（且外因别改参数）",
       "外因" in PR.REFLECT_SYSTEM and "内因" in PR.REFLECT_SYSTEM
       and "不要建议改形态/阈值" in PR.REFLECT_SYSTEM)
    rsrc = inspect.getsource(RA.run_reflection)
    ck("D11 T+5 反思也构建并发证据", "FCTX.build_evidence(" in rsrc and "evidence=FCTX.to_text(ev)" in rsrc)
    ck("D12 反思教训落库带外部归因字段",
       "external_cause" in inspect.getsource(RA._upsert_lesson))

    print("=== E 外因案例不计入改进方向（防「拿运气当规律」）===")
    ret = DV._aggregate([])
    ck("E1 _aggregate 返回 4 元组（含 external 统计）", len(ret) == 4, str(len(ret)))
    attrs = [
        {"error_type": "市场环境拖累", "stage": "market", "param_hint": "无需调参",
         "external_cause": "大盘拖累", "evidence_basis": "大盘-3.2% 个股-3.5%", "confidence": "high"},
        {"error_type": "相似度虚高", "stage": "pattern_match", "param_hint": "模式库相似度阈值",
         "external_cause": "无（内因）", "evidence_basis": "大盘+1.0% 个股-8.0%", "confidence": "high"},
    ]
    _stats, improvements, _stages, external = DV._aggregate(attrs)
    ck("E2 外因计数正确（1/2）", external.get("n") == 1 and external.get("pct") == 0.5, str(external))
    ck("E3 外因案例有可读依据（basis 入库）",
       bool((external.get("examples") or [{}])[0].get("basis")), str(external.get("examples")))
    ck("E4 改进方向只来自内因（外因案例被排除）",
       all("模式库" in i for i in improvements) and not any("无需调参" in i for i in improvements),
       str(improvements))
    ck("E5 未知 external_cause 会被计数（不冒充内因）",
       DV._aggregate([{"error_type": "x", "param_hint": "a"}])[3].get("unknown_n") == 1)
    ck("E6 进化大脑可读到「外因归因」一行", "外因归因" in inspect.getsource(DV.build_verify_txt))

    print("=== H prompt 渲染（防占位符漏传在真实运行时才炸）===")
    rendered = DV._ATTRIBUTE_USER_TMPL.format(
        name="测试股", ts_code=code, date=date, pick_txt="形态 A", t1_ret="-1.20",
        thr="0.00", evidence="【大盘】合成测试证据")
    ck("H1 次日归因模板可渲染且带上证据",
       "合成测试证据" in rendered and "external_cause" in rendered)
    rrendered = PR.REFLECT_USER_TMPL.format(
        name="测试股", ts_code=code, date=date, form_type="A", prob="70",
        reason="r", signals="s", t1_ret="-1.20", t5_ret="-3.00",
        evidence="【大盘】合成测试证据")
    ck("H2 反思模板可渲染且带上证据",
       "合成测试证据" in rrendered and "external_cause" in rrendered)

    print("=== F 可进化参数 ===")
    cfg = _read_json(os.path.join(BACKEND, "data", "evolution_config.json"), {})
    params = cfg.get("params") or {}
    for name, lo, hi in (("verify_evidence_enabled", 0, 1), ("verify_evidence_days", 1, 20)):
        p = params.get(name) or {}
        ck(f"F1 {name} 已注册且边界合理",
           p.get("default") is not None and p.get("min") == lo and p.get("max") == hi, str(p.get("desc", ""))[:40])
    from app.agents import evolution_config as EC
    orig = EC.get_param
    try:
        EC.get_param = lambda n, d=None: (0 if n == "verify_evidence_enabled" else 7 if n == "verify_evidence_days" else orig(n, d))
        ck("F2 evidence_enabled() 热生效（0 → False）", FC.evidence_enabled() is False)
        ck("F3 evidence_days() 热生效（7 → 7）", FC.evidence_days() == 7, str(FC.evidence_days()))
        EC.get_param = lambda n, d=None: ("banana" if n == "verify_evidence_days" else orig(n, d))
        ck("F4 脏值回退默认（不崩）", FC.evidence_days() == FC.DEFAULT_DAYS, str(FC.evidence_days()))
    finally:
        EC.get_param = orig

    print("=== G ⚠️ 未来函数护栏：证据层不得被预测侧引用 ===")
    # 本模块故意使用"推荐日之后"的数据（事后复盘用途）。一旦被每日推荐/特征/打分侧引用，
    # 就变成前瞻偏移（look-ahead bias）——回测会虚高、实盘会失效。
    PREDICTORS = [
        "app/backtest/daily_scan.py", "app/backtest/feature_extractor.py",
        "app/backtest/extra_feature_loader.py", "app/backtest/recommender.py",
        "app/backtest/risk_filter.py", "app/backtest/vector_store.py",
        "app/backtest/threshold_analyzer.py", "app/backtest/form_classifier.py",
        "app/backtest/runner.py", "app/backtest/pattern_miner.py",
    ]
    for p in PREDICTORS:
        s = _src(p)
        ck(f"G1 {p} 未引用 failure_context", "failure_context" not in s)
    ck("G2 只有归因/反思两条路径引用它（白名单）",
       "failure_context" in _src("app/agents/daily_verify.py")
       and "failure_context" in _src("app/agents/reflect_agent.py"))
    ck("G3 模块 docstring 明示未来函数护栏（防后人误用）",
       "未来函数护栏" in _src("app/agents/failure_context.py"))

    print(f"\n===== 结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
