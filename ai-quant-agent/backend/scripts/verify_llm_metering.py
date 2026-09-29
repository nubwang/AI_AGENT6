"""LLM 计量与预算闸门冒烟验证（plans/25 §十 P1）

验什么（每条都有断言，任一失败 → 非零退出）：
  ① 费用计算：缓存命中 / 未命中 / 输出 三段分开计价，结果精确到分位
  ② 缺字段兜底：usage 未给缓存拆分时，保守按"全部未命中"（高估不低估）
  ③ usage 落盘：`llm_usage.jsonl` 一行一次，含 token 明细 + 费用 + source/run_id
  ④ 预算三闸门：单任务 / 单日 / 累计 任一破裂 → `ensure_budget()` False + 熔断标志
  ⑤ **闸门与 LLM 路径已解耦**（2026-09-29 用户要求"每日推荐不要有预算熔断、不要设置上限"）：
     超预算时 `ensure_budget()` **仍返回 False**（能力保留，供自证内核等**显式**自查），
     但 `deepseek_chat()` **不再拦截、照常发请求并返回结果** ——
     否则每日推荐会被整体降级为"按规则概率保留"（这正是要修的那个现象）。
     注：原断言是"熔断时不发 HTTP 请求（不烧钱）"，它约束的正是本次要求移除的行为，故已改写。
  ⑥ 正常路径计量：请求成功 → 解析 JSON 正常返回 + usage 落盘 + 台账累加（互不影响）
  ⑦ 费用预估器：有价 → 低/中/高三档且 low<mid<high；无价 → priced=False + 明确 warning
  ⑧ 汇总：`summary()` 按天/按来源聚合，并给出平均单次 token 与费用

跑法（不接触真实 data/ 目录，全部用临时目录；不消耗任何真实 API 额度）：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_llm_metering.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.agents.llm_client as LC  # noqa: E402
import app.agents.llm_metering as M  # noqa: E402

FAILS: list[str] = []


def ck(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label} {detail}")
        FAILS.append(label)


class FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def patch_paths(tmp: str) -> None:
    M.USAGE_FILE = os.path.join(tmp, "llm_usage.jsonl")
    M.PRICING_FILE = os.path.join(tmp, "llm_pricing.json")
    M.BUDGET_FILE = os.path.join(tmp, "llm_budget.json")
    M.BALANCE_FILE = os.path.join(tmp, "llm_balance_snapshots.json")


def write_pricing(prices: tuple | None) -> None:
    """prices=(hit, miss, out) 元/百万 token；None = 未填价。"""
    row = {"input_cache_hit_per_m": None, "input_cache_miss_per_m": None, "output_per_m": None}
    if prices is not None:
        row = {"input_cache_hit_per_m": prices[0], "input_cache_miss_per_m": prices[1],
               "output_per_m": prices[2]}
    M.save_pricing({
        "version": 1, "currency": "CNY", "unit": "per_1m_tokens",
        "verified": prices is not None, "as_of_date": "2026-01-01",
        "source": "unit-test", "default_model": "deepseek-chat",
        "models": {"deepseek-chat": row},
    })


def reset_budget(per_task: float, per_day: float, total: float, enabled: bool = True) -> None:
    M.save_budget({
        "version": 1, "currency": "CNY", "enabled": enabled,
        "limits": {"per_task_cny": per_task, "per_day_cny": per_day, "total_cny": total},
        "spent": {"total_cny": 0.0, "day": {}, "task": {}}, "ledger": [],
    })
    M.clear_budget_block()


def read_usage() -> list:
    if not os.path.exists(M.USAGE_FILE):
        return []
    with open(M.USAGE_FILE, "r", encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="verify_llm_metering_")
    patch_paths(tmp)
    # 令牌：仅用于通过"未配置 key 直接返回 None"的守卫；HTTP 全程被替换，不会真的出网
    try:
        setattr(LC.settings, "deepseek_api_key", "sk-unit-test")
    except Exception:  # noqa: BLE001
        object.__setattr__(LC.settings, "deepseek_api_key", "sk-unit-test")

    real_post = LC.requests.post
    calls = {"n": 0}

    try:
        # ── ① 费用计算（prices = hit 4 / miss 2 / out 3 元每百万 token）──
        print("\n① 费用计算")
        write_pricing((4.0, 2.0, 3.0))
        cost = M.cost_of({"prompt_tokens": 2_000_000, "completion_tokens": 1_000_000,
                          "prompt_cache_hit_tokens": 1_000_000,
                          "prompt_cache_miss_tokens": 1_000_000}, "deepseek-chat")
        ck(cost == 9.0, "1M命中(4)+1M未命中(2)+1M输出(3) = 9.0 元", f"实得 {cost}")

        # ── ② 缺字段兜底 ──
        print("\n② 缺缓存拆分字段时的保守兜底")
        cost2 = M.cost_of({"prompt_tokens": 1_000_000, "completion_tokens": 0}, "deepseek-chat")
        ck(cost2 == 2.0, "无缓存字段 → 全按未命中计价(2.0 元)", f"实得 {cost2}")
        hit, miss = M.split_prompt_tokens({"prompt_tokens": 100, "prompt_cache_hit_tokens": 30})
        ck((hit, miss) == (30, 70), "拆分补齐差额 (30,70)", f"实得 {(hit, miss)}")

        # ── ③ usage 落盘（含未计价模型 → cost None 不报假数）──
        print("\n③ usage 落盘")
        reset_budget(50.0, 30.0, 300.0)
        M.begin_task("t_ok")
        rec = M.record_usage(source="unit/refine", model="deepseek-chat", run_id="t_ok",
                             usage={"prompt_tokens": 1000, "completion_tokens": 500,
                                    "prompt_cache_hit_tokens": 400,
                                    "prompt_cache_miss_tokens": 600})
        rows = read_usage()
        ck(len(rows) == 1, "jsonl 落 1 行", f"实得 {len(rows)}")
        ck(bool(rows) and rows[0]["source"] == "unit/refine" and rows[0]["run_id"] == "t_ok",
           "source/run_id 已记录", f"实得 {rows[0] if rows else None}")
        exp = round(400 / 1e6 * 4.0 + 600 / 1e6 * 2.0 + 500 / 1e6 * 3.0, 8)
        ck(rec["cost_cny"] == exp, f"费用精确 = {exp}", f"实得 {rec['cost_cny']}")
        snap = M.spend_snapshot(run_id="t_ok")
        ck(abs(snap["task_cny"] - exp) < 1e-9 and abs(snap["total_cny"] - exp) < 1e-9,
           "台账累加（task/total）", f"实得 {snap}")

        # ── ④ 三闸门 ──
        print("\n④ 预算三闸门")
        reset_budget(0.001, 30.0, 300.0)          # 单任务上限故意极小
        M.begin_task("t_broke")
        M.add_spend(0.002, run_id="t_broke", source="unit")
        ck(M.ensure_budget("t_broke") is False, "单任务闸门破裂 → ensure_budget False")
        ck(M.budget_blocked() is True, "熔断标志已置", M.budget_blocked_reason())
        ck(M.check_budget("t_broke")["gate"] == "per_task", "闸门名 = per_task")

        reset_budget(50.0, 0.001, 300.0)          # 单日上限故意极小
        M.clear_budget_block()
        M.add_spend(0.002, run_id="t_day", source="unit")
        ck(M.ensure_budget("t_day") is False and M.check_budget("t_day")["gate"] == "per_day",
           "单日闸门破裂 → per_day")

        reset_budget(50.0, 30.0, 0.001)           # 累计上限故意极小
        M.clear_budget_block()
        M.add_spend(0.002, run_id="t_tot", source="unit")
        ck(M.ensure_budget("t_tot") is False and M.check_budget("t_tot")["gate"] == "total",
           "累计闸门破裂 → total")

        # ── ⑤ 闸门与 LLM 路径**已解耦** ──
        print("\n⑤ 闸门仍在，但**不再挂在 LLM 路径上**（每日推荐因此不会被降级）")
        reset_budget(50.0, 30.0, 0.001)
        M.clear_budget_block()
        M.add_spend(0.002, run_id="t_blk", source="unit")

        # (a) 闸门**自身**照常判定：能力保留，供需要它的调用方显式自查
        #     （自证回放：app/backtest/selfproof.py::budget_blocked(run_id)）
        ck(M.ensure_budget("t_blk") is False,
           "超预算 ⇒ ensure_budget 仍返回 False（闸门能力保留）")
        ck(M.budget_blocked() is True, "熔断标志对外可见（供显式自查者读原因）")

        # (b) LLM 路径**不再被预算阻断** —— 每日推荐正是靠这一点才能跑完
        def _post_records(*a, **k):
            calls["n"] += 1
            return FakeResp({"choices": [{"message": {"content": "{\"ok\": 1}"}}],
                             "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                       "total_tokens": 110,
                                       "prompt_cache_hit_tokens": 0,
                                       "prompt_cache_miss_tokens": 100}})

        LC.requests.post = _post_records
        out = LC.deepseek_chat([{"role": "user", "content": "hi"}], source="unit/over_budget",
                               run_id="t_blk")
        ck(out == {"ok": 1}, "超预算 ⇒ deepseek_chat **照常返回结果**（不再降级为 None）",
           f"实得 {out}")
        ck(calls["n"] == 1, "超预算 ⇒ 确实发出了 HTTP 请求（闸门已从该路径摘除）")

        # ── ⑥ 正常路径：解析返回值 + 计量 ──
        print("\n⑥ 正常路径（熔断解除后）")
        reset_budget(50.0, 30.0, 300.0)
        M.begin_task("t_live", reset=True)
        before = len(read_usage())
        LC.requests.post = lambda *a, **k: FakeResp({
            "choices": [{"message": {"content": "{\"ok\": 1}"}}],
            "usage": {"prompt_tokens": 2000, "completion_tokens": 200,
                      "total_tokens": 2200,
                      "prompt_cache_hit_tokens": 0,
                      "prompt_cache_miss_tokens": 2000},
        })
        parsed = LC.deepseek_chat([{"role": "user", "content": "hi"}],
                                  source="unit/live", run_id="t_live")
        ck(parsed == {"ok": 1}, "返回值仍被正常解析", f"实得 {parsed}")
        rows = read_usage()
        ck(len(rows) == before + 1, "本次调用已计量落盘", f"{before} → {len(rows)}")
        live_cost = round(2000 / 1e6 * 2.0 + 200 / 1e6 * 3.0, 8)
        ck(rows[-1]["cost_cny"] == live_cost, f"费用 = {live_cost}", f"实得 {rows[-1]['cost_cny']}")
        ck(M.spend_snapshot(run_id="t_live")["task_cny"] == live_cost,
           "任务台账按 run_id 归集")

        # ── ⑦ 预估器（有价 / 无价）──
        print("\n⑦ 运行前预估")
        est = M.estimate_task(days=5, candidates_per_day=150, deep_top_n=50, n_sectors=5)
        ck(est["priced"] is True, "有价 → priced=True")
        ck(est["ranges"]["low"]["cost_cny"] < est["ranges"]["mid"]["cost_cny"]
           < est["ranges"]["high"]["cost_cny"], "低<中<高 三档", str(est["ranges"]))
        ck(est["calls_total"] == est["calls_per_day"]["total"] * 5, "调用次数 = 单日模型 × 天数")
        ck(est["calls_per_day"]["total"] > 50, "单日调用次数模型已计入 L2 逐只精筛",
           str(est["calls_per_day"]))
        ck(est["estimated_hours"] > 0, "给出预计耗时", str(est["estimated_hours"]))

        write_pricing(None)
        est2 = M.estimate_task(days=5)
        ck(est2["priced"] is False and est2["cost_mid_cny"] is None,
           "无价 → priced=False 且不给金额（不猜价）", str(est2["cost_mid_cny"]))
        ck(any("价格" in w for w in est2["warnings"]), "无价 → 明确 warning", str(est2["warnings"]))

        # ── ⑧ 汇总 ──
        print("\n⑧ 用量汇总")
        write_pricing((4.0, 2.0, 3.0))
        s = M.summary(days=30)
        # 汇总数必须等于明细行数（熔断那次不该留下任何记录 → 明细正好 2 行）
        ck(s["calls"] == len(read_usage()), "调用次数聚合 == 明细行数", f"实得 {s['calls']}")
        ck(bool(s["by_day"]) and bool(s["by_source"]), "按天/按来源聚合", json.dumps(
            {"by_day": s["by_day"], "by_source": s["by_source"]}, ensure_ascii=False))
        ck(s["avg_prompt_tokens"] is not None, "给出平均单次 token（供预估校准）",
           str(s["avg_prompt_tokens"]))
        ck("by_period" in s and bool(s["by_period"]), "按峰谷时段聚合（省了多少钱可核对）",
           json.dumps(s.get("by_period") or {}, ensure_ascii=False))

        # ── ⑨ 模型别名 + 峰谷定价 + 思考模式 token（2026-09-22 官方实测对齐）──
        print("\n⑨ 别名解析 / 峰谷时段 / reasoning_tokens")
        # 用官方真实结构的价格表（peak/off_peak + aliases），验证新逻辑
        M.save_pricing({
            "version": 2, "currency": "CNY", "unit": "per_1m_tokens", "verified": True,
            "as_of_date": "2026-09-22", "source": "unit-test",
            "peak_windows_bj": [["09:00", "12:00"], ["14:00", "18:00"]],
            "peak_weekdays_only": True,
            "default_model": "deepseek-flash",
            "aliases": {"deepseek-chat": "deepseek-flash"},
            "models": {
                "deepseek-flash": {
                    "off_peak": {"input_cache_hit_per_m": 0.02, "input_cache_miss_per_m": 1.0,
                                 "output_per_m": 4.0},
                    "peak": {"input_cache_hit_per_m": 0.04, "input_cache_miss_per_m": 2.0,
                             "output_per_m": 8.0},
                },
            },
        })
        ck(M.resolve_model("deepseek-chat") == "deepseek-flash",
           "别名 deepseek-chat → deepseek-flash（实测服务端就是这么路由的）",
           M.resolve_model("deepseek-chat"))
        ck(M.resolve_model("") == "deepseek-flash", "空模型名 → 价格表 default_model")

        mon_10 = time.mktime((2026, 9, 14, 10, 0, 0, 0, 0, -1))     # 周一 10:00 北京
        mon_13 = time.mktime((2026, 9, 14, 13, 0, 0, 0, 0, -1))     # 周一 13:00（午休）
        mon_20 = time.mktime((2026, 9, 14, 20, 0, 0, 0, 0, -1))     # 周一 20:00
        sat_10 = time.mktime((2026, 9, 19, 10, 0, 0, 0, 0, -1))     # 周六 10:00
        ck(time.localtime(mon_10).tm_wday == 0 and time.localtime(sat_10).tm_wday == 5,
           "基准时间戳的星期正确（周一 09-14 / 周六 09-19）",
           f"{time.localtime(mon_10).tm_wday} / {time.localtime(sat_10).tm_wday}")
        ck(M.current_period(mon_10) == "peak", "周一 10:00 → 高峰", M.current_period(mon_10))
        ck(M.current_period(mon_13) == "off_peak", "周一 13:00（午休）→ 空闲", M.current_period(mon_13))
        ck(M.current_period(mon_20) == "off_peak", "周一 20:00 → 空闲", M.current_period(mon_20))
        ck(M.current_period(sat_10) == "off_peak", "周六全天 → 空闲", M.current_period(sat_10))

        p_peak = M.price_for("deepseek-chat", "peak")
        p_off = M.price_for("deepseek-chat", "off_peak")
        ck(p_peak is not None and p_peak["output_per_m"] == 8.0,
           "峰值价取对（别名 + peak 时段）", str(p_peak))
        ck(p_off is not None and p_off["output_per_m"] == 4.0, "空闲价取对（= 峰值的一半）", str(p_off))

        # 同一 usage 在两个时段应恰好差 2 倍（峰谷定价的硬约束）
        u = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000,
             "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1_000_000}
        c_peak = M.cost_of(u, "deepseek-chat", "peak")
        c_off = M.cost_of(u, "deepseek-chat", "off_peak")
        ck(c_peak == 10.0 and c_off == 5.0, "1M未命中输入+1M输出：峰值 10 元 / 空闲 5 元",
           f"peak={c_peak} off={c_off}")

        rec2 = M.record_usage(source="unit/think", model="deepseek-chat", run_id="t_think",
                              usage={"prompt_tokens": 31, "completion_tokens": 5, "total_tokens": 36,
                                     "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 31,
                                     "completion_tokens_details": {"reasoning_tokens": 5}})
        ck(rec2["reasoning_tokens"] == 5, "reasoning_tokens 已单独记录（思考模式成本可解释）",
           str(rec2["reasoning_tokens"]))
        ck(rec2["model"] == "deepseek-flash" and rec2["model_requested"] == "deepseek-chat",
           "记录里同时保留计费模型与请求模型名",
           f"{rec2['model']} / {rec2['model_requested']}")
        ck(rec2["period"] in ("peak", "off_peak"), "记录里带计费时段", rec2["period"])

        # 预估器：分时段报价（高峰为头条、空闲单独给出，且高峰 = 2×空闲）
        est3 = M.estimate_task(days=1, candidates_per_day=150, model="deepseek-chat")
        ck(est3["cost_mid_peak_cny"] is not None and est3["cost_mid_off_peak_cny"] is not None,
           "预估器同时给出高峰/空闲两套报价",
           f"peak={est3['cost_mid_peak_cny']} off={est3['cost_mid_off_peak_cny']}")
        ck(abs(float(est3["cost_mid_peak_cny"] or 0.0)
               - 2 * float(est3["cost_mid_off_peak_cny"] or 0.0)) < 1e-9,
           "高峰金额 = 空闲金额 × 2")
        ck(est3["headline_period"] == "peak", "头条金额用高峰（保守上限，不低估）",
           est3["headline_period"])
        ck(est3["model"] == "deepseek-flash", "预估器报出真实计费模型", est3["model"])

        # ── ⑩ 思考模式显式声明（F15）──
        # 用户口径（2026-09-22）：**deepseek-flash 就用思考模式** ⇒ 默认 enabled（且始终显式写入，
        # 不依赖官方默认值）；需要 temperature 生效的调用方可显式 thinking=False。
        print("\n⑩ 思考模式开关（thinking.type）")
        reset_budget(50.0, 30.0, 300.0)
        M.begin_task("t_think2", reset=True)
        captured: list[dict] = []

        def _capture_post(url, json=None, headers=None, timeout=None):   # noqa: A002
            captured.append(json or {})
            return FakeResp({
                "model": "deepseek-flash",
                "choices": [{"message": {"content": "{\"ok\": 1}",
                                         "reasoning_content": "…"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                          "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10},
            })

        def _set_thinking(v: bool) -> None:
            try:
                setattr(LC.settings, "deepseek_thinking", v)
            except Exception:  # noqa: BLE001
                object.__setattr__(LC.settings, "deepseek_thinking", v)

        LC.requests.post = _capture_post
        _set_thinking(True)
        LC.deepseek_chat([{"role": "user", "content": "hi"}], source="unit/think/default",
                         run_id="t_think2")
        p0 = captured[-1]
        ck(p0.get("thinking") == {"type": "enabled"},
           "默认按用户口径走**思考模式**（且显式写入，不依赖官方默认值）", str(p0.get("thinking")))
        ck(str(p0.get("reasoning_effort")) in ("low", "high", "max"),
           "思考模式带 reasoning_effort", str(p0.get("reasoning_effort")))
        ck("temperature" not in p0,
           "思考模式**不传 temperature**（官方明确其不生效，避免'以为设了'）")

        LC.deepseek_chat([{"role": "user", "content": "hi"}], source="unit/think/off",
                         run_id="t_think2", thinking=False)
        p1 = captured[-1]
        ck(p1.get("thinking") == {"type": "disabled"}, "可显式 thinking=False 关闭",
           str(p1.get("thinking")))
        ck("temperature" in p1, "关闭思考后 temperature 才真正传入生效",
           str(p1.get("temperature")))
        ck("reasoning_effort" not in p1, "关闭思考时不传 reasoning_effort")

        recs = read_usage()
        ext = recs[-1].get("extra") or {}
        ck(ext.get("thinking_requested") == "disabled"
           and ext.get("reasoning_content_present") is True,
           "usage 明细同时记录'请求模式'与'是否真的产出思维链'（费用异常时可解释）",
           json.dumps(ext, ensure_ascii=False))
        LC.settings.deepseek_thinking = True
    finally:
        LC.requests.post = real_post
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过：\n  - " + "\n  - ".join(FAILS))
        return 1
    print("✅ 全部通过：费用计算 / usage 落盘 / 三闸门判定 / 闸门与LLM路径解耦 / 预估器 / 汇总")
    return 0


if __name__ == "__main__":
    sys.exit(main())
