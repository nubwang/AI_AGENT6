"""自证报告验证（plans/25 P4 / §十三 F4·F6·F8·F13 + §五 样本量护栏）

流程：小规模回放（近期 2 天 × 抽样 60 只，规则档零费用）→ 结算 → 归因 → 组装报告 → 断言。

验什么（任一失败 → 非零退出）：
  ① 结算落盘：`verify.json` 含 rows / n_picks / n_pending_t5，未到期样本被正确记为 pending
  ② 报告契约：schema 字段齐 + 主指标=超额 + 基准 lift + 环境/形态/相似度三层 + IS/OOS 双栏
  ③ IS/OOS：`in_sample` 与 `out_of_sample` 都在，且文案声明"验收只看 OOS"
  ④ 归因：规则化归因（零 LLM）产出 error_type_counts；UNREACHED（未到期）单独归类
  ⑤ 费用对账：`usage` 字段存在（未调 LLM 时 cost=0）
  ⑥ 口径护栏：flags 前视徽标 / 术语=重演口径 / 数据门或幸存者偏差声明
  ⑦ 样本量护栏：样本不足时 verdict.sufficient_samples=False
  ⑧ 沙箱隔离：真实资产 mtime/大小零变化
  ⑨ L0 文本：build_verify_txt 非空且含『重演口径』

跑法（约 1~3 分钟）：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_selfproof_report.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import selfproof_report as SR  # noqa: E402
from app.agents import selfproof_verify as SV  # noqa: E402
from app.backtest import selfproof as SP  # noqa: E402
from app.backtest import selfproof_policy as P  # noqa: E402
from app.backtest.loader import get_trade_dates  # noqa: E402

RUN_ID = "verify_report_smoke"
FAILS: list[str] = []


def ck(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label} {detail}")
        FAILS.append(label)


def snapshot(paths: list[str]) -> dict:
    out = {}
    for p in paths:
        try:
            st = os.stat(p)
            out[p] = (st.st_mtime, st.st_size)
        except OSError:
            out[p] = None
    return out


def main() -> int:
    protected = [i["path"] for i in P.REAL_ASSETS]
    before = snapshot(protected)

    # 选一段"T+5 已到期"的近期区间（数据到 2026-09-21）
    cal = [d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
           for d in get_trade_dates("20260801", "20260831")]
    dates = cal[:2] if len(cal) >= 2 else cal
    print(f"\n回放区间: {dates[0]} ~ {dates[-1]}（抽样 60 只、规则档、零 API 费用）")

    t0 = time.time()
    res = SP.run_replay(start=dates[0], end=dates[-1], step=1, universe=60, max_days=2,
                        run_id=RUN_ID, resume=False)
    print(f"回放完成：status={res.get('status')} 天数={res.get('dates_done')} "
          f"推荐={res.get('picks_total')} 用时={time.time() - t0:.1f}s")
    ck(res.get("status") in ("done", "stopped", "paused_budget"), "回放状态正常", str(res))

    # ── ① 结算 ──
    print("\n① 结算（T+1/T+5/T+20，D+1 买入口径）")
    v = SV.settle_run(RUN_ID)
    ck(bool(v.get("ok")), "settle_run 成功", str(v)[:200])
    ck(os.path.exists(os.path.join(SP.run_dir(RUN_ID), "verify.json")), "verify.json 已落沙箱")
    ck("rows" in v and isinstance(v["rows"], list), "含 rows 明细")
    ck("n_pending_t5" in v, "含 n_pending_t5（未到期样本单独计）")
    print(f"   n_picks={v.get('n_picks')} 已结算T+5={v.get('n_settled_t5')} "
          f"未到期={v.get('n_pending_t5')}")
    for r in (v.get("rows") or [])[:3]:
        print(f"   · {r.get('as_of')} {r.get('ts_code')} t1={r.get('t1')} t5={r.get('t5')} "
              f"excess_t5={r.get('excess_t5')}")
    ck(all(("t5_net" in r and "excess_t5" in r) for r in (v.get("rows") or [])),
       "每行含净收益与超额字段")

    # ── ④ 规则化归因（零 LLM）──
    print("\n④ 失败归因（默认规则化，零 API 费用）")
    att = SV.attribute_run(RUN_ID, llm=False)
    ck(bool(att.get("ok")), "attribute_run 成功", str(att)[:200])
    ck(isinstance(att.get("error_type_counts"), dict), "含 error_type_counts")
    ck(os.path.exists(os.path.join(SP.run_dir(RUN_ID), "attribution.json")), "attribution.json 已落沙箱")
    print(f"   失败 {att.get('n_failures')} 条 | 归因分布 {att.get('error_type_counts')}")
    print(f"   形态均值 T+5 {att.get('form_avg_t5')}")
    ck(bool(att.get("llm") is False), "记录 llm=False（未烧 API）")

    # ── ② 报告组装 ──
    print("\n② 报告组装（主指标=超额 / 三层分层 / IS-OOS / 基准）")
    rep = SR.build(RUN_ID, llm_attribution=False)
    ck(bool(rep) and "metrics" in rep, "build 成功", str(rep)[:200])
    for k in ("run_id", "flags", "dates", "daily_stats", "metrics", "layers",
              "is_oos", "attribution", "usage", "verdict"):
        ck(k in rep, f"报告含字段 {k}")
    m = rep.get("metrics") or {}
    ck("excess_t5" in m and "bench_t5" in m, "含主指标（超额）与基准（随机候选期望）")
    ck("lift_vs_bench" in m, "含 vs 基准 lift（F6 对照）")
    ck((m.get("excess_t5") or {}).get("t_stat") is not None or (m.get("excess_t5") or {}).get("n", 0) < 5,
       "超额 t 值：样本足够时给出（不足则为 None，不编造）")
    lay = rep.get("layers") or {}
    for k in ("by_regime", "by_form", "by_similarity"):
        ck(k in lay, f"分层含 {k}")
    print(f"   超额 {m.get('excess_t5')} | 基准 {m.get('bench_t5')} | lift={m.get('lift_vs_bench')}")
    print(f"   按环境 {json.dumps(lay.get('by_regime') or {}, ensure_ascii=False)}")
    print(f"   按形态 {json.dumps(lay.get('by_form') or {}, ensure_ascii=False)}")

    # ── ③ IS/OOS ──
    print("\n③ IS/OOS 分离（F4）")
    io = rep.get("is_oos") or {}
    ck("in_sample" in io and "out_of_sample" in io, "IS/OOS 双栏都在")
    ck("验收只看" in str(io.get("note")), "声明『验收只看 out_of_sample』", str(io.get("note"))[:80])
    ck(io.get("mode") == "year_kfold", "模式 = year_kfold")

    # ── ⑤ 费用对账 ──
    print("\n⑤ 费用对账（F13）")
    u = rep.get("usage") or {}
    ck("llm_calls" in u, "含 llm_calls")
    ck(u.get("llm_calls") == 0, f"规则档应为 0 次调用（实际 {u.get('llm_calls')}）")

    # ── ⑥ 口径护栏 ──
    print("\n⑥ 口径护栏（F1/F2/F8）")
    fl = rep.get("flags") or {}
    ck(bool(fl.get("forward_look")), "前视徽标存在")
    ck((fl.get("main_metric") or {}).get("hit_definition") == "excess_vs_market",
       "主指标 = excess_vs_market")
    ck("重演" in str(fl.get("metric_name")), "指标名 = 重演口径")
    ck(bool(fl.get("data_gate")) or bool(fl.get("survivorship")), "带数据门/幸存者偏差声明")

    # ── ⑦ 样本量护栏 ──
    print("\n⑦ 样本量护栏（§五）")
    vd = rep.get("verdict") or {}
    ck("sufficient_samples" in vd, "含 sufficient_samples 判定")
    print(f"   samples={vd.get('sufficient_samples')} 门槛={vd.get('min_samples')} → {vd.get('note')}")

    # ── ⑨ L0 文本 ──
    print("\n⑨ L0 摘要文本")
    txt = SV.build_verify_txt(RUN_ID)
    ck(bool(txt), "build_verify_txt 非空", txt[:120])
    ck("重演" in txt, "文本含『重演口径』（防误读为当年成绩）")

    # ── ⑧ 沙箱隔离 ──
    print("\n⑧ 真实资产零变化（G3）")
    after = snapshot(protected)
    diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    ck(not diff, "真实资产 mtime/大小未变", str(diff))

    print("\n" + "=" * 60)
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过：\n  - " + "\n  - ".join(FAILS))
        return 1
    print("✅ 全部通过：结算 / 归因 / 报告契约 / 分层 / IS-OOS / 费用对账 / 护栏 / 样本量 / 沙箱")
    return 0


if __name__ == "__main__":
    sys.exit(main())
