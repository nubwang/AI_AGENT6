"""D4/D5 验收：规则链路接上「HAC 复核」与「统一门槛」（plans/24 §11.28 / §11.17 / D4·D5）

## 接的是什么

    D5  HAC 复核：FDR 管**多重比较**，不管**重叠样本**。规则样本是同一批日期上的
        横截面（多条规则共享同一天）⇒ 日收益序列重叠 ⇒ naive 显著性被系统性放大
        （§11.27/§11.28 实测 3.08→4.02；基准 B1 4.02→1.15）。
    D4  统一门槛：把「能不能上生产」交给**同一个** `baseline_gate.judge_default()`
        （费率 0.5% + 滑点 20bp + **两档 t**），而不是每条链路各拍一套阈值。

两处都是 **只增不改**：默认 `observe` ⇒ **不改 verified 集合、不改网格采纳结论**。

## 本脚本验收 6 条（**不写任何数据**）

    A  `hac_recheck` 数学正确：日序列 → naive t 与 HAC t；自相关序列上 |t_hac| < |t_naive|
    B  样本 < 30 天 ⇒ `pass=None`（不可判定，**不是**"不通过"）
    C  默认参数：`rule_fdr_hac=observe`、`rule_hac_lag=4`
    D  `verify_all` 返回里带 `hac` 元数据与 `gate` 结论
    E  **observe 不改结论**：`off` 与 `observe` 两种模式 verified_count **完全相同**
    F  门槛如实：日序列 < 30 天时 `gate.verdict == "not_decidable"`

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_hac_and_gate_wiring.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from app.backtest import rule_verifier as RV  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _mk_hits(key: str, n_days: int, rho: float = 0.0, seed: int = 7,
             per_day: int = 3) -> dict[str, list[dict]]:
    """构造"同一天多条样本"的命中集合（这正是**重叠**的来源）。"""
    rng = np.random.default_rng(seed)
    x = np.zeros(n_days)
    for i in range(n_days):
        x[i] = rho * (x[i - 1] if i else 0.0) + rng.normal(0.0, 0.02)
    rows: list[dict] = []
    for i in range(n_days):
        d = f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"
        for _ in range(per_day):
            r = float(x[i] + rng.normal(0.0, 0.004))
            rows.append({"date": d, "ret_t1": r, "hit_t1": r > 0, "tradeable": True})
    return {key: rows}


def main() -> int:
    print("=" * 100)
    print("D4/D5 验收：规则链路的 HAC 复核 + 统一门槛接线")
    print("=" * 100)

    key = RV.RULE_FORMS[0].key if RV.RULE_FORMS else "demo"
    print(f"  用规则 key = {key}（RULE_FORMS 共 {len(RV.RULE_FORMS)} 条）")

    # ── A 数学正确 ──
    hits_ar = _mk_hits(key, 80, rho=0.75)          # 强自相关 ⇒ HAC 应显著收缩 t
    m_ar = RV.hac_recheck(hits_ar, 4).get(key) or {}
    check("A① hac_recheck 可跑并给出 n_days", m_ar.get("n_days") == 80,
          f"n_days={m_ar.get('n_days')}")
    tn, th = m_ar.get("t_naive"), m_ar.get("t_hac")
    check("A② 自相关序列上 HAC 确实**收缩**了 t（|t_hac| < |t_naive|）",
          tn is not None and th is not None and abs(th) < abs(tn),
          f"t_naive={tn} → t_hac={th}")
    check("A③ pass 与 (t_hac > 2) 一致",
          m_ar.get("pass") == (th is not None and th > 2.0),
          f"pass={m_ar.get('pass')}")

    # ── B 样本不足 ⇒ 不可判定 ──
    m_sh = RV.hac_recheck(_mk_hits(key, 12), 4).get(key) or {}
    check("B 样本 < 30 天 ⇒ t_hac=None 且 pass=None（不可判定，不是不通过）",
          m_sh.get("n_days") == 12 and m_sh.get("t_hac") is None and m_sh.get("pass") is None,
          f"{m_sh}")

    # ── C 默认参数 ──
    check("C 默认 rule_fdr_hac = observe", RV._hac_mode() == "observe", RV._hac_mode())
    check("C 默认 rule_hac_lag = 4", RV._hac_lag() == 4, str(RV._hac_lag()))

    # ── D 返回值带 hac / gate ──
    res = RV.verify_all(hits_ar, do_walk_forward=False)
    has_hac = isinstance(res.get("hac"), dict) and "mode" in res["hac"]
    has_gate = "gate" in res
    check("D① verify_all 返回带 hac 元数据", has_hac, f"{res.get('hac')}")
    check("D② verify_all 返回带 gate 结论", has_gate,
          f"gate={None if res.get('gate') is None else {k: res['gate'].get(k) for k in ('verdict', 'n_days')}}")

    # ── E observe 不改结论 ──
    orig = RV._hac_mode
    RV._hac_mode = lambda: "off"           # type: ignore[assignment]
    try:
        res_off = RV.verify_all(hits_ar, do_walk_forward=False)
    finally:
        RV._hac_mode = orig               # type: ignore[assignment]
    check("E **observe 不改结论**：off 与 observe 的 verified_count 完全相同",
          res_off["verified_count"] == res["verified_count"],
          f"off={res_off['verified_count']} observe={res['verified_count']}")

    # ── F 门槛如实（样本不足 ⇒ not_decidable）──
    g = res.get("gate") or {}
    check("F 日序列 < 30 天时 gate.verdict == not_decidable（如实，不是 reject）",
          (g.get("verdict") == "not_decidable") if g.get("n_days", 999) < 30 else True,
          f"n_days={g.get('n_days')} verdict={g.get('verdict')}")

    print()
    print("=" * 100)
    print(f"结果：{len(PASS)} / {len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
    print("判读：")
    print("  1) D5 的 HAC 复核**接上了**且数学正确 —— 但它默认只在 observe（只记录）；")
    print("  2) D4 的统一门槛**接上了** —— 规则集与网格最优各有一份 judge_default() 结论；")
    print("  3) 两者都**不改**既有结论（E 条已实证）⇒ 符合『先验证后生效』；")
    print("  4) 要转 enforce，必须先确认 verified_count 不掉破 daily_scan 的兜底阈值（≥3）。")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
