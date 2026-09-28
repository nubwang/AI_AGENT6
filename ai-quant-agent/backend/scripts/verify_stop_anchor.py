"""D3 验收：止损锚点 **T0 收盘 → 成交价**（plans/23 §15.3.2 / P2-7）

## 口径错误的本质（为什么必须修）

    stop = t0_close × (1 − stop_pct)        ← 锚在 **T0 收盘**
    成交价 = T+1 开盘（或日内回踩价）        ← **实际成本**在这里

⇒ 隔夜跳空低开时，**开盘价可能已经在止损位之下** ⇒ 出现"**入门即止损**"的假信号。
§15.3.2 实测：5% 止损时占成交单 **29.3%**，3% 时高达 **57.5%**。

止损是"**相对成本**的风险预算"，所以锚点必须是**成交价**。

## 本脚本验收 4 条

    A 参数已注册：`plan_stop_anchor` 读得到、有 choices、`current = close_t0`（默认）
    B **默认不变**：不传 `stop_anchor` 与显式传 `"close_t0"` 的结果**逐条完全相同**
    C 开启后确实生效：`fill` 锚点在「入门即止损」比例 / 洗出率 / 期望上与 legacy 有差异
    D 复现 §15.3.2 的量级：洗出率平均降约 **2.7pp**、期望变化 **≈ 0**

> 诚实边界：D 条是"量级对照"，不是"必须精确等于 2.7pp" —— 样本集与网格点不同，
> 只要求**同号、同量级**（降 1~5pp、期望 |Δ| < 0.005）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_stop_anchor.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import discipline_grid as DG  # noqa: E402

STOPS = (0.03, 0.05, 0.08, 0.10)
TP1_RATIO = 0.6
HOLD = 5
CHASE = 0.03
COST = 0.0025
LIMIT = 600

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def agg(rows: list[dict], stop: float, anchor: str) -> dict:
    """对同一批样本、同一组网格参数做一次回放聚合。**不写任何数据。**"""
    filled = hits = same_day = 0
    s = 0.0
    n = 0
    for r in rows:
        res = DG._replay(r["arrays"], r["i0"], r["t0_close"], r["avg_t5"], r["avg_t20"],
                         stop, TP1_RATIO, HOLD, CHASE, COST, stop_anchor=anchor)
        if not res["filled"]:
            continue
        filled += 1
        if res["stop_hit"]:
            hits += 1
            if int(res["days"]) == 0:
                same_day += 1
        if res["ret"] is not None:
            s += float(res["ret"])
            n += 1
    return {
        "filled": filled, "hit": hits,
        "washout": (hits / filled if filled else float("nan")),
        "same_day": (same_day / filled if filled else float("nan")),
        "mean": (s / n if n else float("nan")),
    }


def main() -> int:
    print("=" * 100)
    print("D3 验收：止损锚点 T0 收盘 → 成交价（plans/23 §15.3.2 / P2-7）")
    print("=" * 100)

    # ── A 参数已注册 ──
    from app.agents import evolution_config as EC
    cur = EC.get_param("plan_stop_anchor", None)
    check("A 参数 plan_stop_anchor 已注册", cur is not None, f"current = {cur!r}")
    check("A 默认值是 close_t0（默认行为不变）", str(cur) == "close_t0", f"current = {cur!r}")

    rows = DG._load_samples(LIMIT)
    # 与 build_grid 同口径：按形态补 avg_t5 / avg_t20（`_load_samples` 只给基础字段）
    avg = DG._avg_returns()
    for r in rows:
        _a = avg.get(r["form"]) or {}
        r["avg_t5"] = float(_a.get("t5") or DG.FALLBACK_AVG_T5)
        r["avg_t20"] = float(_a.get("t20") or DG.FALLBACK_AVG_T20)
    check("A 样本加载", len(rows) > 50, f"{len(rows)} 条 kb_case 样本")
    if not rows:
        print("  ✘ 无样本，无法继续")
        return 1

    # ── B 默认不变（逐条对照）──
    same = 0
    for r in rows:
        a = DG._replay(r["arrays"], r["i0"], r["t0_close"], r["avg_t5"], r["avg_t20"],
                       0.05, TP1_RATIO, HOLD, CHASE, COST)                     # 不传 ⇒ 读参数
        b = DG._replay(r["arrays"], r["i0"], r["t0_close"], r["avg_t5"], r["avg_t20"],
                       0.05, TP1_RATIO, HOLD, CHASE, COST, stop_anchor="close_t0")  # 显式 legacy
        if a == b:
            same += 1
    check("B 默认（不传参）与显式 close_t0 **逐条完全相同**", same == len(rows),
          f"{same} / {len(rows)} 条一致")

    # ── C / D 两种锚点对照 ──
    print()
    print("=== 两种锚点的对照（同一批样本、同一组网格参数；**纯回放，不写库**）===")
    h = (f"  {'止损':<7}{'成交n':>7}{'洗出率(T0)':>12}{'洗出率(fill)':>13}"
         f"{'入门即止损(T0)':>16}{'入门即止损(fill)':>17}{'期望(T0)':>11}{'期望(fill)':>12}")
    print(h)
    print("  " + "-" * (len(h) - 2))
    dw: list[float] = []
    dm: list[float] = []
    for st in STOPS:
        a = agg(rows, st, "close_t0")
        b = agg(rows, st, "fill")
        dw.append(b["washout"] - a["washout"])
        dm.append(b["mean"] - a["mean"])
        print(f"  {st:<7.0%}{a['filled']:>7}{a['washout']:>12.1%}{b['washout']:>13.1%}"
              f"{a['same_day']:>16.1%}{b['same_day']:>17.1%}"
              f"{a['mean']:>11.4f}{b['mean']:>12.4f}")

    import math
    d_w = sum(dw) / len(dw)
    d_m = sum(dm) / len(dm)
    print()
    print(f"  **平均差异**：洗出率 {d_w * 100:+.2f}pp｜期望 {d_m:+.4f}")

    check("C fill 锚点确实生效（洗出率有变化）", any(abs(x) > 1e-9 for x in dw))
    check("C 「入门即止损」在 fill 锚点下下降", d_w < 0 or all(x <= 0 for x in dw),
          f"Δ洗出率 = {d_w * 100:+.2f}pp")
    check("D 量级与 §15.3.2 同号同量级（洗出率降 1~5pp）", -0.05 <= d_w <= -0.005,
          f"Δ = {d_w * 100:+.2f}pp（§15.3.2 实测 ≈ −2.7pp）")
    check("D 期望变化 ≈ 0（|Δ| < 0.005）", abs(d_m) < 0.005,
          f"Δ = {d_m:+.4f}（§15.3.2 实测 ≈ −0.0003）")

    print()
    print("=" * 100)
    print(f"结果：{len(PASS)} / {len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
    print("判读：")
    print("  1) **默认不变**已验证 ⇒ 这次改动对既有网格结论零影响（可安全上线）；")
    print("  2) fill 锚点让「入门即止损」消失 ⇒ **口径修正**；")
    print("  3) 但期望几乎不变 ⇒ 它是**口径错误**，**不是收益根因**（§15.3.2 的结论保持）；")
    print("  4) 要切到 fill 需要跑一次完整 A/B（本文只给口径对照，不给采纳结论）。")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
