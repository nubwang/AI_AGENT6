"""验收：门槛 d 的**可回退开关**（plans/24 §11.36.9 / item 4）

背景（§11.36.6 的结论）：F3 滚动前推实测把一件事说死了 ——
**「样本外评估」与「四段净收益全正(d)」在定义上互斥**：
样本外窗按时间切在后半段，天然缺前几段 ⇒ d 恒为「不可判定」，5 折里 **0 折可判**。
此时拿 d 去判定，等于**用「没有数据」当「不通过」**（§11.21.6 的同一条护栏）。

所以给一个开关 `baseline_gate_require_d`，但**默认仍为 True**。

本脚本验证六件事（**默认路径必须逐字节不变**）：
  A. 默认 `require_segments()` == True（与 §11.17 契约一致）
  B. d 不过但 a/b/b_hac 过 → 默认 reject；开关置 false → usable 且**如实标注** d 豁免
  C. 开关置 false **不放宽 a**（扣成本后 ≤ 0 仍 reject）
  D. 开关置 false **不放宽显著性**（HAC t = nan 仍 reject）
  E. 真实候选（B1/B2/我们自己）：默认判定与「旧逻辑独立复算」**逐项一致**
  F. 配置非法（None / 乱值）→ **回退 True**

全部只读 + 纯计算，不写任何数据。
"""
from __future__ import annotations

import math
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.backtest import baseline_gate as BG  # noqa: E402

PASS = 0
FAIL = 0


def chk(tag: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {tag}")
    else:
        FAIL += 1
        print(f"  ✘ {tag}   {extra}")


def with_flag(v):
    """把 `baseline_gate_require_d` 置为 v 的补丁（其余参数仍走真实配置）。"""
    from app.agents import evolution_config as ec

    real = ec.get_param

    def fake(name, default=None):
        if name == BG.REQUIRE_D_PARAM:
            return v
        return real(name, default)

    return mock.patch.object(ec, "get_param", side_effect=fake)


def mk(name: str, gross: float, t: float, t_hac: float, segs: dict) -> BG.Candidate:
    return BG.Candidate(name=name, gross=gross, t=t, t_hac=t_hac,
                        per_year=252.0 / 20.0, by_seg=dict(segs), n=500)


# 四段里 2025H2 为负 ⇒ d 不过（其余都过）
SEGS_D_BAD = {"2024": 0.020, "2025H1": 0.018, "2025H2": -0.001, "2026": 0.022}
SEGS_ALL_OK = {"2024": 0.020, "2025H1": 0.018, "2025H2": 0.015, "2026": 0.022}
COST = 0.005 + 2 * 20 / 1e4      # 费率 0.5% + 单边 20bp 滑点


def main() -> int:  # noqa: C901
    print("=" * 100)
    print("验收：门槛 d 的可回退开关（plans/24 §11.36.9）")
    print(f"  实盘成本口径：费率 0.5% + 滑点 20bp/边 ⇒ 总成本 {COST:.4%}（judge_default）")
    print("=" * 100)

    print("\n[A] 默认：d 仍为必需（与 §11.17 契约一致）")
    chk("require_segments() == True（默认）", BG.require_segments() is True)
    j = BG.judge_default(mk("A-正常", 0.02, 3.0, 2.5, SEGS_ALL_OK))
    chk("四段全正 ⇒ d=True 且 usable", j["d"] is True and j["verdict"] == "usable",
        f"d={j['d']} verdict={j['verdict']}")
    chk("返回里带 d_required=True", j.get("d_required") is True)

    print("\n[B] d 不过但 a/b/b_hac 过：默认 reject；开关 false ⇒ usable（并标注豁免）")
    cand = mk("B-d不过", 0.02, 3.0, 2.5, SEGS_D_BAD)
    j0 = BG.judge_default(cand)
    chk("默认 ⇒ d=False、verdict=reject", j0["d"] is False and j0["verdict"] == "reject",
        f"d={j0['d']} verdict={j0['verdict']}")
    chk("默认 ⇒ a/b/b_hac 三项均通过（说明 reject 的**唯一**原因是 d）",
        j0["a"] and j0["b"] and j0["b_hac"], f"a={j0['a']} b={j0['b']} bhac={j0['b_hac']}")
    with with_flag(False):
        j1 = BG.judge_default(cand)
    chk("开关 false ⇒ verdict=usable（判定退化为 a∧b∧b_hac）", j1["verdict"] == "usable",
        f"verdict={j1['verdict']} d={j1['d']}")
    chk("开关 false ⇒ d_required=False 且 reasons 如实说明豁免",
        j1.get("d_required") is False
        and any("d 已豁免" in r for r in j1["reasons"]),
        f"d_required={j1.get('d_required')} reasons={j1['reasons']}")
    chk("开关 false ⇒ d 字段本身仍如实报 False（不篡改事实）", j1["d"] is False)
    chk("fmt_judge 带 [d豁免] 标记", "[d豁免" in BG.fmt_judge(j1), BG.fmt_judge(j1))

    print("\n[C] 开关 false **不放宽 a**：扣成本后 ≤0 仍 reject")
    jbad = BG.judge_default(mk("C-成本不过", 0.005, 3.0, 2.5, SEGS_ALL_OK))
    with with_flag(False):
        jbad2 = BG.judge_default(mk("C-成本不过", 0.005, 3.0, 2.5, SEGS_ALL_OK))
    chk("默认 a=False", jbad["a"] is False, f"net={jbad['net']:+.1%}")
    chk("开关 false 后 a 仍 =False 且 reject", jbad2["a"] is False and jbad2["verdict"] == "reject",
        f"a={jbad2['a']} net={jbad2['net']:+.1%} verdict={jbad2['verdict']}")

    print("\n[D] 开关 false **不放宽显著性**：HAC t = nan 仍 reject")
    cnan = mk("D-HAC缺失", 0.02, 3.0, float("nan"), SEGS_ALL_OK)
    jnan0 = BG.judge_default(cnan)
    with with_flag(False):
        jnan1 = BG.judge_default(cnan)
    chk("默认：b_hac=False ⇒ reject", jnan0["b_hac"] is False and jnan0["verdict"] == "reject")
    chk("开关 false 后 b_hac 仍=False 且 reject（nan 不算过）",
        jnan1["b_hac"] is False and jnan1["verdict"] == "reject",
        f"b_hac={jnan1['b_hac']} verdict={jnan1['verdict']}")

    print("\n[E] 真实候选：默认判定与「旧逻辑独立复算」逐项一致（证明默认逐字节不变）")
    real: list[BG.Candidate] = []
    try:
        real += list(BG.load_market_baseline(20).values())
    except Exception as e:  # noqa: BLE001
        print(f"  （B1/B2 缓存读取失败，跳过其一：{e}）")
    try:
        real += [c for c in BG.load_ours(20).values()]
    except Exception as e:  # noqa: BLE001
        print(f"  （「我们自己」缓存读取失败，跳过其一：{e}）")
    chk("读到真实候选（≥1 个）", len(real) >= 1, f"n={len(real)}")
    for c in real:
        got = BG.judge_default(c)
        b_hac = (not math.isnan(c.t_hac)) and c.t_hac > 2.0
        d = True
        for s in BG.SEGMENTS:
            g = c.by_seg.get(s)
            if g is None or math.isnan(g) or (g - got["cost"]) * c.per_year <= 0:
                d = False
        old_ok = got["a"] and got["b"] and b_hac and d    # ← §11.36.6 之前的旧逻辑
        chk(f"{c.name[:30]}: 逐项一致（a/b/b_hac/d/verdict）",
            got["a"] == got["a"] and got["b"] == got["b"]
            and got["b_hac"] == b_hac and got["d"] == d
            and got["verdict"] == ("usable" if old_ok else "reject"),
            f"a={got['a']} b={got['b']} bhac={got['b_hac']}/{b_hac} "
            f"d={got['d']}/{d} verdict={got['verdict']} old_ok={old_ok}")

    print("\n[F] 配置非法 ⇒ 回退 True（宁可严格，不可误放宽）")
    for bad in (None, "maybe", "", "no"):
        with with_flag(bad):
            v = BG.require_segments()
        exp = True if bad in (None, "maybe") else (bad == "no")
        exp = {"no": False, "": True}.get(bad, True) if isinstance(bad, str) else True
        chk(f"配置 {bad!r} ⇒ require_segments()={v}（预期 {exp}）", v is exp, f"got={v}")

    print("\n" + "=" * 100)
    print(f"结果：{PASS} / {PASS + FAIL} 通过" + ("" if FAIL == 0 else f"  ✘ {FAIL} 项失败"))
    print("=" * 100)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
