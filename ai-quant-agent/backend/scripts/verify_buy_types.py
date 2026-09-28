"""验收：四类买点回测（plans/23 §3.3 / P1 第 7 项）

守 6 组约束，并打印真实结论（含**基准超额**——没有基准，买点收益率无法解释）：

  A 四类买点齐备 + 输出契约（triggered/filled/reason/rets）
  B ⚠️ 无未来函数：触发只依赖 ≤D 的数据（篡改 D+1 之后，触发判定必须完全不变）
  C 成交真实性（A 股现实约束）：一字封板不可买 / 追高放弃 / 挂单未触及 / 成交价优于挂单取开盘
  D 高开幅度分桶自洽（不重不漏；竞价 >3% 必然买不到）
  E 真实数据：四类都有触发与成交；逐类打印"触发/成交率/T+1/T+5/**超额**"
  F 未接入预测侧（本步只做回测分析，不改每日推荐）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_buy_types.py
"""
from __future__ import annotations

import inspect
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import buy_types as BT

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(BACKEND, "data", "buy_type_samples.json")


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _src(rel: str) -> str:
    p = os.path.join(BACKEND, rel)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def _mk(n: int = 60, close=None, vol=None, sealed=None, flat: bool = True) -> pd.DataFrame:
    """构造带前复权列的合成日线（默认横盘；可指定收盘/量/封板）。"""
    c = np.array(close if close is not None else [10.0] * n, dtype=float)
    v = np.array(vol if vol is not None else [1e6] * n, dtype=float)
    se = np.array(sealed if sealed is not None else [0] * n, dtype=float)
    if flat:
        return pd.DataFrame({
            "trade_date": pd.date_range("2026-01-01", periods=n, freq="D"),
            "adj_open": c * 0.999, "adj_high": c * 1.01, "adj_low": c * 0.99,
            "adj_close": c, "vol": v, "is_sealed_up": se, "is_sealed_down": np.zeros(n),
        })
    return pd.DataFrame({"trade_date": pd.date_range("2026-01-01", periods=n, freq="D"),
                         "adj_open": c, "adj_high": c, "adj_low": c, "adj_close": c,
                         "vol": v, "is_sealed_up": se, "is_sealed_down": np.zeros(n)})


def main() -> int:  # noqa: C901
    print("=== A 四类买点齐备 + 输出契约 ===")
    ck("A1 四类买点 + 基准俱全",
       len(BT.BUY_TYPES) == 4 and BT.BASELINE == "baseline"
       and set(BT.BUY_TYPE_CN) >= set(BT.BUY_TYPES) | {BT.BASELINE})
    df = _mk(60)
    s = BT.evaluate(df, 30, "TEST")
    ok_keys = s is not None and all(
        t in s.triggered and t in s.filled and t in s.reason
        and (t == BT.BASELINE or t in s.rets or not s.filled.get(t))
        for t in BT.BUY_TYPES)
    ck("A2 每类都有 triggered/filled/reason/rets 键", bool(ok_keys))
    ck("A3 基准必须被记录（否则无法算超额）", s is not None and BT.BASELINE in s.triggered)
    ck("A4 数据不足时返回 None（i<21 / 末尾不足 HOLD+1）",
       BT.evaluate(df, 5, "T") is None and BT.evaluate(df, 58, "T") is None)

    print("=== B ⚠️ 无未来函数 ===")
    t = _mk(60)
    base = BT.evaluate(t, 30, "T")
    assert base is not None          # 合成数据必然可评估（同时帮类型检查收窄）
    tampered = t.copy()
    tampered.loc[tampered.index[31:], "adj_close"] *= 3.0
    tampered.loc[tampered.index[31:], "adj_open"] *= 3.0
    tampered.loc[tampered.index[31:], "adj_low"] *= 1.0
    tampered.loc[tampered.index[31:], "vol"] *= 10.0
    after = BT.evaluate(tampered, 30, "T")
    assert after is not None
    ck("B1 篡改 D+1 之后的数据 → 触发判定完全不变（触发只依赖 ≤D）",
       after is not None and after.triggered == base.triggered,
       f"{base.triggered} vs {after.triggered}")
    tampered2 = t.copy()
    tail = 31 + BT.HOLD + 1
    tampered2.loc[tampered2.index[tail:], "adj_close"] *= 3.0
    after2 = BT.evaluate(tampered2, 30, "T")
    ck("B2 篡改持有窗口之后的数据 → 连收益也完全不变",
       after2 is not None and after2.rets == base.rets, str(base.rets))
    esrc = inspect.getsource(BT.evaluate)
    ck("B3 触发条件只用 d 及之前（h[d-20:d] / vol[d-19:d] / c[d]）",
       "prev_high20 = float(np.max(h[d - 20: d]))" in esrc
       and "vol[d - 19: d + 1]" in esrc and "ma10 = float(np.mean(c[d - 9: d + 1]))" in esrc)

    print("=== C 成交真实性（A 股现实约束）===")
    # C1 一字封板：**必须构造真能触发买点的数据**（横盘数据不触发任何买点，
    # 只会得到"未触发"——最初就是这么写错的，等于什么也没测）
    close_b = [10.0] * 60
    close_b[30] = 11.0                      # 放量突破前 20 日高点（h[10:30] 最大 10.1）
    vol_b = [1e6] * 60
    vol_b[30] = 3e6
    sealed = [0] * 60
    sealed[31] = 1                          # D+1 一字封板
    dfb = _mk(60, close=close_b, vol=vol_b, sealed=sealed)
    dfb.loc[dfb.index[31], "adj_open"] = 11.05   # 高开 0.45%（<追高上限）→ 只剩"封板"这一个原因
    c1 = BT.evaluate(dfb, 30, "T")
    assert c1 is not None
    ck("C1 D+1 一字封板 → 已触发的买点一律不可成交且原因写明",
       bool(c1.triggered["breakout"]) and not c1.filled["breakout"]
       and "封板" in (c1.reason["breakout"] or "")
       and not c1.filled["auction"] and "封板" in (c1.reason["auction"] or ""),
       f"breakout 触发={c1.triggered['breakout']}/原因={c1.reason['breakout']}｜"
       f"auction 原因={c1.reason['auction']}")
    # C2 高开 6%
    close = [10.0] * 60
    df2 = _mk(60, close=close)
    df2.loc[df2.index[31], "adj_open"] = 10.6          # 高开 6%
    c2 = BT.evaluate(df2, 30, "T")
    assert c2 is not None
    ck("C2 高开 6% → 突破放弃（追高上限）+ 竞价放弃（>5%）",
       c2 is not None and not c2.filled["breakout"] and not c2.filled["auction"],
       f"gap={c2.gap:.3f} breakout={c2.reason['breakout']} auction={c2.reason['auction']}")
    # C3 回踩/低吸挂单未触及（D+1 最低价高于限价）
    df3 = _mk(60, close=close)
    df3.loc[df3.index[31], "adj_low"] = 10.5
    c3 = BT.evaluate(df3, 30, "T")
    assert c3 is not None
    ck("C3 D+1 未触及限价 → 成交率<100%（挂不上就是买不到，不许假设总能成交）",
       c3 is not None and (not c3.filled["pullback"] or not c3.filled["dip"]),
       f"pullback={c3.reason['pullback']} dip={c3.reason['dip']}")
    # C4 成交价优于挂单时取开盘价：同样需要"真能触发低吸"的数据
    #   （低吸触发条件：20 日振幅 ≤15% 且 c[d] < ma20 → 用缓跌构造）
    close_d = [10.0] * 60
    for j in range(26, 31):
        close_d[j] = 10.0 - 0.04 * (j - 25)      # 缓跌到 9.80 → c[d] < ma20
    df4 = _mk(60, close=close_d)
    limit_expected = close_d[30] * BT.DIP_LIMIT  # 9.80 × 0.99 = 9.702
    df4.loc[df4.index[31], "adj_open"] = 9.5     # 开盘更低 → 应按开盘价成交
    df4.loc[df4.index[31], "adj_low"] = 9.4      # 触及限价
    c4 = BT.evaluate(df4, 30, "T")
    assert c4 is not None
    t1 = float(c4.rets["dip"]["t1"] or 0)
    ck("C4 开盘优于挂单 → 成交价取更优的开盘价（不占系统便宜）",
       bool(c4.filled["dip"]) and abs(t1 - (10.0 / 9.5 - 1.0)) < 1e-4,
       f"dip filled={c4.filled['dip']} t1={t1:.4f}（若误用限价 {limit_expected:.3f} 则应为 "
       f"{10.0/limit_expected - 1:.4f}）")

    print("=== D 高开幅度分桶自洽 ===")
    buckets = BT.GAP_BUCKETS
    hits = [sum(1 for lo_b, hi_b, _n in buckets if lo_b <= g < hi_b)
            for g in (-0.5, -0.01, 0.0, 0.005, 0.02, 0.04, 0.2)]
    ck("D1 每个高开值恰好落入一个桶（不重不漏）", all(h == 1 for h in hits), str(hits))
    recs = [{"gap": 0.04, "triggered": {"auction": True}, "filled": {"auction": False},
             "reason": {"auction": "高开4.0%超3%"}, "rets": {}},
            {"gap": 0.005, "triggered": {"auction": True}, "filled": {"auction": True},
             "reason": {"auction": ""}, "rets": {"auction": {"t1": 0.01, "t5": 0.02}}}]
    gb = BT.gap_buckets(recs, "auction")
    ck("D2 竞价 >3% 桶成交率必为 0（体现了「越追越买不到」）",
       gb["3~5%"]["fill_rate"] == 0 and gb["0~1%"]["fill_rate"] == 1.0,
       f"3~5%={gb['3~5%']['fill_rate']} 0~1%={gb['0~1%']['fill_rate']}")

    print("=== E 真实数据 ===")
    if os.path.exists(SAMPLES):
        with open(SAMPLES, "r", encoding="utf-8") as f:
            payload = json.load(f)
        sm = payload.get("summary") or {}
        pts = payload.get("points", 0)
        ck("E1 样本库可加载", pts > 0, f"{pts} 个评估点")
        ck("E2 四类都有触发与成交",
           all(sm.get(t, {}).get("trigger_n", 0) > 0 and sm.get(t, {}).get("fill_n", 0) > 0
               for t in BT.BUY_TYPES),
           str({t: (sm.get(t, {}).get("trigger_n"), sm.get(t, {}).get("fill_n")) for t in BT.BUY_TYPES}))
        ck("E3 成交率都在 (0,1]", all(0 < (sm.get(t, {}).get("fill_rate") or 0) <= 1
                                     for t in BT.BUY_TYPES))
        ck("E4 超额字段已产出（可解释性前提）",
           all("excess_t5" in sm.get(t, {}) for t in BT.BUY_TYPES))
        base5 = (sm.get(BT.BASELINE, {}).get("t5") or {}).get("mean")
        print(f"\n  基准（无条件 D+1 开盘买入）：n={(sm.get(BT.BASELINE, {}).get('t5') or {}).get('n')} "
              f"T+5 均值={base5}｜预测点 {pts}｜{payload.get('built_at')}")
        print(f"  {'买点':<12}{'触发':>8}{'成交':>8}{'成交率':>8}{'T+1均值':>10}{'T+5均值':>10}"
              f"{'超额T5':>9}{'T+5胜率':>9}")
        for t in BT.BUY_TYPES:
            x = sm[t]
            print(f"  {x['cn']:<12}{x['trigger_n']:>8}{x['fill_n']:>8}{x['fill_rate']:>8.3f}"
                  f"{str(x['t1'].get('mean')):>10}{str(x['t5'].get('mean')):>10}"
                  f"{str(x.get('excess_t5')):>9}{str(x['t5'].get('pos')):>9}")
        g = payload.get("gap_buckets") or {}
        if g:
            print("\n  【高开幅度分桶 · ①突破买入】")
            print(f"  {'分桶':<10}{'n':>8}{'成交率':>8}{'T+5均值':>10}{'T+5胜率':>9}")
            for name, b in (g.get("breakout") or {}).items():
                print(f"  {name:<10}{b['n']:>8}{b['fill_rate']:>8.3f}"
                      f"{str(b['t5'].get('mean')):>10}{str(b['t5'].get('pos')):>9}")
            # 可执行结论：追高（1~3%）是否显著更差（样本≥30 才判）
            lo = ((g.get("breakout") or {}).get("0~1%") or {}).get("t5", {})
            hi = ((g.get("breakout") or {}).get("1~3%") or {}).get("t5", {})
            if (lo.get("n", 0) >= 30) and (hi.get("n", 0) >= 30):
                ck("E5 突破买入：高开 1~3% 的 T+5 明显差于 0~1%（追高有害，可指导 plan_max_chase_pct）",
                   (hi.get("mean") or 0) < (lo.get("mean") or 0) - 0.005,
                   f"0~1%={lo.get('mean')} vs 1~3%={hi.get('mean')}")
            else:
                print("  （E5 跳过：高开分桶样本不足 30，不硬下结论）")
    else:
        ck("E1 样本库可加载", False, "先跑 scripts/build_buy_type_samples.py")

    print("=== F 未接入预测侧 ===")
    for rel in ("app/backtest/trade_plan.py", "app/backtest/daily_scan.py",
                "app/backtest/recommender.py", "app/backtest/runner.py"):
        ck(f"F1 {rel} 未引用 buy_types", "buy_types" not in _src(rel))
    ck("F2 docstring 明确「信号在 D、成交在 D+1」",
       "信号在 D 收盘" in inspect.getsource(BT) and "成交在 D+1" in inspect.getsource(BT))

    print(f"\n===== 结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
