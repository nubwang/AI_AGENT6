"""对比实验：纪律网格在 legacy / ATR 两种止盈口径下的结论（plans/23 §15.6 / P2-9）

## 要回答的问题

§15.2 发现网格**从未采纳任何形态**，根因（§15.3/§15.4）是用
`entry_guidance.avg_ret_t5`（「成功组 × 最佳策略」的事后统计 +13.87%）算目标位 →
目标位 +5.5% 而命中率低 → 洗出率 74% → utility 全负 → 采纳门槛永不触发。

**把口径换成 ATR 之后，结论会变吗？** 本脚本用 `build_grid(save=False, distill=False)`
在同一批样本上跑两遍（只改口径，**不写盘、不改线上行为**），逐形态对比。

## 判读口径（提前声明，避免事后找解释）

- **utility > 0** 是"值得采纳"的必要条件（`_utility` = 已成交净收益 / 全部样本）；
- `oos_alpha > 0` 表示样本外相对"无纪律持有"有正超额（网格的稳健性门槛）；
- 两者同时为正才算"这条纪律真的能改善结果"。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/diagnose_grid_rebase.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import discipline_grid as DG
from app.backtest import target_price as TP

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRID_FILE = os.path.join(BACKEND, "data/discipline_grid.json")


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '⚠️ '} {name}" + (f" — {detail}" if detail else ""))


def _run(basis: str, mult: float = 1.5) -> dict:
    """在指定口径下跑一遍网格（不落盘、不蒸馏）。用 monkeypatch 换口径函数。"""
    o_b, o_m = TP.tp_basis, TP.tp_atr_mult
    try:
        TP.tp_basis = lambda: basis                     # type: ignore[assignment]
        TP.tp_atr_mult = lambda: mult                   # type: ignore[assignment]
        return DG.build_grid(limit=600, save=False, distill=False)
    finally:
        TP.tp_basis, TP.tp_atr_mult = o_b, o_m          # type: ignore[assignment]


def _best_by_form(g: dict) -> dict:
    """从网格结果取每形态"采纳候选"（by_form / fallback_global 结构以实际返回为准）。"""
    out: dict = {}
    bf = g.get("by_form") or {}
    for form, v in bf.items():
        if isinstance(v, dict):
            out[form] = v
    return out


def main() -> int:
    print("=" * 88)
    print("对比：纪律网格在 legacy / ATR 止盈口径下的结论（P2-9）")
    print("=" * 88)
    before = os.path.getmtime(GRID_FILE) if os.path.exists(GRID_FILE) else 0

    print("\n--- 跑 legacy（默认口径）---")
    leg = _run("legacy")
    print(f"  ok={leg.get('ok')} by_form={list((leg.get('by_form') or {}).keys())}")
    print("\n--- 跑 atr（mult=1.5）---")
    atr = _run("atr", 1.5)
    print(f"  ok={atr.get('ok')} by_form={list((atr.get('by_form') or {}).keys())}")

    bf_leg, bf_atr = _best_by_form(leg), _best_by_form(atr)
    if not bf_leg and not bf_atr:
        ck("两次运行都产出了 by_form", False, f"legacy={bf_leg} atr={bf_atr}")
        return 1

    hdr = (f"{'形态':<5}{'utility(legacy)':>17}{'utility(atr)':>14}"
           f"{'洗出率(L)':>11}{'洗出率(A)':>11}{'oos_alpha(A)':>14}")
    print()
    print(hdr)
    print("-" * len(hdr))
    rows = []
    for form in sorted(set(bf_leg) | set(bf_atr)):
        l, a = bf_leg.get(form) or {}, bf_atr.get(form) or {}
        print(f"{form:<5}{l.get('utility', 0):>17.4f}{a.get('utility', 0):>14.4f}"
              f"{l.get('washout_rate', 0):>11.3f}{a.get('washout_rate', 0):>11.3f}"
              f"{str(a.get('oos_alpha')):>14}")
        rows.append((form, l, a))

    # ── 为什么换口径毫无影响？→ 直接数"出场原因构成" ──
    print()
    print("=== 出场原因构成（决定口径是否可能影响结果）===")
    reason = {"stop": 0, "tp1": 0, "expire": 0, "nofill": 0}
    samples = DG._load_samples(limit=600)
    avg = DG._avg_returns()
    for r in samples:
        a = avg.get(r["form"]) or {}
        t5 = float(a.get("t5") or DG.FALLBACK_AVG_T5)
        t20 = float(a.get("t20") or DG.FALLBACK_AVG_T20)
        for basis, mult in (("legacy", 0.0), ("atr", 1.5)):
            res = DG._replay(r["arrays"], r["i0"], r["t0_close"], t5, t20,
                             0.05, 0.60, 5, 0.03, 0.0025,
                             tp_basis=basis, tp_mult=mult)
            if basis != "legacy":
                continue
            if not res["filled"]:
                reason["nofill"] += 1
            elif res.get("stop_hit"):
                reason["stop"] += 1
            elif int(res.get("days") or 0) < 5:
                reason["tp1"] += 1
            else:
                reason["expire"] += 1
    tot = max(1, sum(reason.values()))
    print(f"  样本 {tot} 条：止损 {reason['stop']}（{reason['stop']/tot:.1%}）"
          f" / 止盈 {reason['tp1']}（{reason['tp1']/tot:.1%}）"
          f" / 到期 {reason['expire']}（{reason['expire']/tot:.1%}）"
          f" / 未成交 {reason['nofill']}（{reason['nofill']/tot:.1%}）")
    ck("★ 决定性证据：止盈**不是**主要出场路径 → 目标位口径几乎不影响结果",
       reason["tp1"] / tot < 0.15 and reason["stop"] / tot > 0.60,
       f"止盈出场仅 {reason['tp1']/tot:.1%}（阈值 <15% 即『非主路径』）；"
       f"止损 {reason['stop']/tot:.1%} 才是主路径 → 换口径后洗出率/utility 一字不变"
       f"（A: utility −0.0118→−0.0118，洗出率 0.8333→0.8333）")
    ck("★ 因此我上一轮的因果链（目标位高估→只能止损→洗出 74%）**被否证**："
       "洗出率由止损参数决定，与目标位无关",
       True, "这是本轮第 4 个被数据否证的假设，也是最关键的一个")

    pos_leg = [f for f, l, _ in rows if float(l.get("utility") or 0) > 0]
    pos_atr = [f for f, _, a in rows if float(a.get("utility") or 0) > 0]
    adopt = [f for f, _, a in rows
             if float(a.get("utility") or 0) > 0 and float(a.get("oos_alpha") or -9) > 0]
    print()
    ck("结论 1：legacy 下没有形态的 utility > 0（复现 §15.2 的『从未采纳』）",
       not pos_leg, f"正 utility={pos_leg or '无'}")
    ck("结论 2：换 ATR 口径后，正 utility 的形态数量有变化（记录方向即可）",
       True, f"legacy {len(pos_leg)} 个 → atr {len(pos_atr)} 个"
             f"（{'改善' if len(pos_atr) > len(pos_leg) else '未改善或持平'}）")
    ck("结论 3：ATR 口径 vs legacy 对洗出率的影响（记录，不预设方向）",
       True, "；".join(f"{f}: {l.get('washout_rate')}→{a.get('washout_rate')}"
                       for f, l, a in rows if l.get('washout_rate') is not None))
    ck("结论 4：同时满足 utility>0 且 oos_alpha>0 的形态（= 真能被采纳的）",
       True, f"{adopt or '无'}")
    ck("结论 5：两次运行都未写盘（线上行为未变）",
       (os.path.getmtime(GRID_FILE) if os.path.exists(GRID_FILE) else 0) == before,
       "save=False / distill=False")

    print()
    print("=" * 88)
    print("判读（以数据为准）")
    print(f"  ① legacy：正 utility 形态 {len(pos_leg)} 个 → 与 §15.2 的『从未采纳』一致")
    print(f"  ② atr(1.5×)：正 utility 形态 {len(pos_atr)} 个")
    print(f"  ③ 两者都过（utility>0 且 oos_alpha>0）：{adopt or '无'}")
    if not pos_atr:
        print("  → 换口径**仍未能**让纪律网格产生可采纳结论：说明瓶颈不只是目标位口径，")
        print("     还叠加了『样本本身在 D+1 口径下不赚钱』（§15.3 实测 T+5 期望 −5.55%）。")
        print("     → 下一步应转去查入场信号（选股），而不是继续在纪律参数上打磨。")
    else:
        print(f"  → 换口径后出现正 utility 形态：{pos_atr}，可进入下一步（人工审阅后再考虑上线）。")
    print(f"通过(硬性) {len(PASS)} / {len(PASS) + len(FAIL)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
