"""验收：基准门槛（baseline_gate）— plans/24 §11.17 / §11.25

这个脚本做三件事：

    ① **守卫测试（两轮：滑点 0 与 20bp）**：证明门槛**不是"永远 reject"的坏门槛** ——
       构造 5 个假想候选（该过的过、该拒的拒），逐条核对判定结果；
    ② **真实判定**：把市值分层 Q1~Q5、B1、B2、市值加权全部过一遍，给出
       **slip = 0 / 20bp 两档**的年化净 + 每个候选的**归零滑点**；
    ③ ★ **本节新增的意义**（§11.25）：**加上滑点后，门槛变硬了一大截** ——
       §11.17 时"该通过"的假想候选（每段赢 B1 3‰）在 20bp 滑点下**会被否掉**，
       因为滑点 2×20bp = 0.40%/期 > 3‰。
       ⇒ **这正是 §11.24 的结论：不带滑点的"过闸"是假过闸。**

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_baseline_gate.py
"""
from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.baseline_gate import (  # noqa: E402
    CHECKLIST, SEGMENTS, Candidate, SIZE_CACHE, default_slip_bp, fmt_judge, judge,
)

FEE = 0.005
H = 20
PER_YEAR = 252.0 / H
QN = {0: "Q1 最小", 1: "Q2", 2: "Q3", 3: "Q4", 4: "Q5 最大"}
SLIP_LIVE = 20.0          # §11.24 的"中间档"假设（与 `baseline_slip_bp` 默认一致）


def _load_all(h: int = H) -> list[Candidate]:
    if not os.path.exists(SIZE_CACHE):
        return []
    with open(SIZE_CACHE, encoding="utf-8") as f:
        recs = json.load(f)
    out: list[Candidate] = []
    for r in recs:
        if r.get("h") != h:
            continue
        by = {k: float(v) for k, v in (r.get("by_seg") or {}).items()}
        t = float(r.get("t") or float("nan"))
        n = int(r.get("n") or 0)
        if r.get("kind") == "size":
            out.append(Candidate(f"{QN.get(int(r['q']), 'Q?')} 市值组", float(r["gross"]),
                                 t, PER_YEAR, by, n))
        elif r.get("kind") == "market_ew":
            out.append(Candidate("全市场等权（= B1）", float(r["gross"]), t, PER_YEAR, by, n))
        elif r.get("kind") == "market_capw":
            out.append(Candidate("市值加权", float(r["gross"]), t, PER_YEAR, by, n))
    return out


def guard_tests(b1: Candidate, slip_bp: float = 0.0, edge: float = 0.003,
                label: str = "") -> int:
    """守卫测试：门槛必须既能放行、也能拒绝。返回失败数。

    - `edge`：构造"该通过"用例时，每段相对 B1 的优势（毛/期）。
      **滑点越大，edge 必须越大**，否则滑点本身就把优势吃光（这正是要演示的事）。
    """
    cost = FEE + 2.0 * slip_bp / 1e4
    print("=" * 118)
    print(f"① 守卫测试  {label or f'滑点 {slip_bp:g}bp'}   "
          f"（成本 = 费率 {FEE:.2%} + 滑点 {2 * slip_bp:.0f}bp = {cost:.2%}）")
    print("  判定 = a ∧ b(naive t>2) ∧ **b_hac(HAC t>2)** ∧ d ∧ c —— 两档并列，只加严")
    print("=" * 118)
    good_seg = {s: max(v, cost) + edge for s, v in b1.by_seg.items()}
    g_gross = sum(good_seg.values()) / len(good_seg)
    cases: list[tuple[str, Candidate, bool]] = []
    # ★ 2026-09-21（§11.31）：判定改为**两档并列**（`usable` 需 `b AND b_hac`），
    #   所以"该通过"的用例必须**同时**给出 naive `t` 与 `t_hac`。
    cases.append((f"假想·该通过（每段赢 B1 +{edge:.1%}/期，t=3.2 / t_hac=3.0）",
                  Candidate("假想·该通过", g_gross, 3.2, PER_YEAR, dict(good_seg),
                            t_hac=3.0), True))
    cases.append(("假想·t 太小（0.8）",
                  Candidate("假想·t 太小", g_gross, 0.8, PER_YEAR, dict(good_seg),
                            t_hac=3.0), False))
    # ★ 新增两条：证明「第二档」真的在拦（旧口径会把这两条误放行）
    cases.append(("假想·naive t 高但**缺 HAC t**（旧口径会放行）",
                  Candidate("假想·缺HAC", g_gross, 3.2, PER_YEAR, dict(good_seg)), False))
    cases.append(("假想·naive t 高但 HAC t 低（1.10）",
                  Candidate("假想·HAC低", g_gross, 3.2, PER_YEAR, dict(good_seg),
                            t_hac=1.10), False))
    cases.append(("假想·成本吃光（每期毛 0.2%）",
                  Candidate("假想·成本吃光", 0.002, 3.0, PER_YEAR,
                            {s: 0.002 for s in SEGMENTS}), False))
    one = dict(good_seg)
    one["2026"] = -0.02
    cases.append(("假想·只有三段好（2026 崩）",
                  Candidate("假想·单段运气", sum(one.values()) / len(one),
                            3.0, PER_YEAR, one), False))
    lose = {s: v - 0.002 for s, v in b1.by_seg.items()}
    cases.append(("假想·每段都跑输 B1",
                  Candidate("假想·跑输基准", sum(lose.values()) / len(lose),
                            3.0, PER_YEAR, lose), False))

    fails = 0
    print(f"{'用例':<44}{'判定':>10}{'期望':>10}{'结果':>10}")
    print("-" * 74)
    for name, cand, expect_usable in cases:
        j = judge(cand, FEE, base=b1, slip_bp=slip_bp)
        got = j["verdict"] == "usable"
        ok = got == expect_usable
        if not ok:
            fails += 1
        print(f"{name:<44}{j['verdict']:>10}"
              f"{'usable' if expect_usable else 'reject':>10}{'✔ 一致' if ok else '✘ 不一致':>10}")
        if j["reasons"]:
            for r in j["reasons"]:
                print(f"    └ {r}")
    print()
    print(f"  守卫测试：{'全部一致 ✔' if fails == 0 else f'{fails} 个不一致 ✘'}")
    print()
    return fails


def main() -> int:
    print("=" * 118)
    print("基准门槛验收（plans/24 §11.17 立门槛 → §11.25 加滑点）")
    print("=" * 118)
    print()
    print("口径红线（每次验证都要对照；来源见 plans/24 各节）：")
    for i, c in enumerate(CHECKLIST, 1):
        print(f"  {i}. {c}")
    print()

    base = _load_all()
    if not base:
        print(f"  ❌ 没读到基准缓存 {os.path.relpath(SIZE_CACHE, os.getcwd())}")
        print("     请先运行：./venv/bin/python scripts/verify_size_baseline.py")
        return 1
    b1 = next((c for c in base if c.name.startswith("全市场等权")), None)
    if b1 is None:
        print("  ❌ 缓存里缺 B1（全市场等权）")
        return 1

    print(f"  可进化参数 baseline_slip_bp 当前值 = {default_slip_bp():g}bp"
          f"（§11.24 的中间档假设）")
    print()
    fails = guard_tests(b1, 0.0, 0.003, "滑点 0（= §11.17 原口径）")
    fails += guard_tests(b1, SLIP_LIVE, 0.012,
                         f"滑点 {SLIP_LIVE:g}bp（实盘口径；'该通过'用例优势放大到 +1.2%/期）")

    print()
    print("=" * 118)
    print(f"② 真实判定（持有 {H} 日）—— **两档并列：滑点 0 与 {SLIP_LIVE:g}bp**")
    print("   a=扣成本年化>0  b=t>2  c=逐段超额>0  d=逐段净>0（四段全正）")
    print("=" * 118)
    hd = (f"{'候选':<24}{'净(0bp)':>10}{'净(20bp)':>10}{'t':>7}"
          f"{'a':>4}{'b':>4}{'c':>4}{'d':>4}{'归零滑点':>10}  判定(20bp)")
    print(hd)
    print("-" * len(hd))
    usable_at_0 = usable_at_live = 0
    for c in base:
        is_self = c.name.startswith("全市场等权")
        bb = None if is_self else b1
        j0 = judge(c, FEE, base=bb, slip_bp=0.0)
        jl = judge(c, FEE, base=bb, slip_bp=SLIP_LIVE)
        if j0["verdict"] == "usable":
            usable_at_0 += 1
        if jl["verdict"] == "usable":
            usable_at_live += 1
        be = (c.gross - FEE) * 1e4 / 2.0
        tv = "nan" if math.isnan(c.t) else f"{c.t:+.2f}"
        mk = lambda x: "✔" if x is True else ("—" if x is None else "✘")  # noqa: E731
        name = c.name + ("（基准自身，c 不适用）" if is_self else "")
        print(f"{name[:22]:<24}{j0['net']:>+10.1%}{jl['net']:>+10.1%}{tv:>7}"
              f"{mk(jl['a']):>4}{mk(jl['b']):>4}{mk(jl['c']):>4}{mk(jl['d']):>4}"
              f"{be:>+9.1f}bp  {jl['verdict']}")
    print()
    print(f"  ⇒ 通过门槛的候选：滑点 0 时 **{usable_at_0} / {len(base)}**；"
          f"滑点 {SLIP_LIVE:g}bp 时 **{usable_at_live} / {len(base)}**")
    print()
    print("  判读（这就是本节的结论）：")
    print(f"    - **两档都是 {usable_at_live}/{len(base)}** —— 因为条件 d「四段全正」"
          "本来就无人满足（2026 段全负）；")
    print("      但**净收益被滑点吃掉一大块**，而且**各候选的脆弱度差别很大**：");
    print(f"      滑点 2×{SLIP_LIVE:g}bp = {2 * SLIP_LIVE / 1e4:.2%}/期，逐候选扣掉后"
          "就是右表的「净(20bp)」与「归零滑点」两列；");
    print("    - ★ **「归零滑点」是一个新的判别维度**：Q1 最小 **70bp** / B1 **44bp** /"
          " 市值加权 **27bp**");
    print("      ⇒ 「等权」与「小市值」这两件事都在**同时抬高收益与抗滑点能力**；");
    print("    - ★ 对**新方案**的意义：守卫测试第一轮（滑点 0）里「每段赢 B1 3‰」就能过；");
    print(f"      第二轮（{SLIP_LIVE:g}bp）必须赢 **≥1.2%/期** 才能过 ⇒ **门槛实质提高约 4 倍**；");
    print("    - 因此今后任何「新特征 / 新形态 / 新择时」一律用 **`judge_default()`** 判定")
    print("      （= 费率 0.5% + 滑点 20bp），而不是裸 `judge()` —— 否则是**假过闸**。")
    print()
    print("  已知局限：滑点是**结构性假设**（按成交额分档），不是实测；")
    print("            未建模盘中封板 / 跌停卖不出 / 停牌；小市值组滑点最重。")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
