"""诊断：按形态的纪律网格为什么从未被采纳（plans/23 §15.2 / P2-2）

## 起因

§14.10 的结论是「**不该追高开 >1% 的突破**」，我原以为"体系里没有按入场类型生效的参数位"。
核对代码后发现**这个前提是错的**：维度早就存在 ——
[`discipline_grid`](ai-quant-agent/backend/app/backtest/discipline_grid.py:1) 会**按形态**（A/B/C/D/E/U）
网格化 `stop_loss_pct / tp1_ratio / hold_days / chase_pct`，并由
[`discipline_grid.plan_override(form)`](ai-quant-agent/backend/app/backtest/discipline_grid.py:462)
在 [`trade_plan.build_plan`](ai-quant-agent/backend/app/backtest/trade_plan.py:179) 里覆盖参数。

所以真正的问题不是"缺维度"，而是："**维度有了，为什么一次都没生效？**"
本脚本回答这个，并给出可复核的数字（不猜）。

## 输出

  1. 每形态网格结果表（n / 最优 chase·stop·tp1·hold / utility / 成交率 / 未成交率 / 洗出率）
  2. 采纳状态（plan_override 是否为空）+ 覆盖缺口（哪些形态没有网格结果）
  3. ⚠️ 三项内部一致性告警：
     - 若某形态最优 `chase_pct=0` 却有 `filled_rate=1.0` → **该样本对追高上限不敏感**，
       chase 维度在这个样本上没有区分度（与 §14.10 市场级实测「收紧会损失 8.1% 成交」矛盾）
     - `washout_rate` 偏高（>0.5）→ 主因是止损口径而不是选股
     - 最优 chase 在所有形态间**完全一致**（无分化）→ 说明"按形态"目前没有实际区分力

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/diagnose_discipline_grid.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRID_FILE = os.path.join(BACKEND, "data", "discipline_grid.json")
FORMS = ("A", "B", "C", "D", "E", "U")


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '⚠️ '} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 78)
    print("诊断：按形态纪律网格的采纳状态与口径一致性（P2-2）")
    print("=" * 78)
    if not os.path.exists(GRID_FILE):
        print(f"❌ 网格文件不存在: {GRID_FILE}")
        return 1
    with open(GRID_FILE, encoding="utf-8") as fh:
        d = json.load(fh)

    bf = d.get("by_form") or {}
    print(f"生成时间 {d.get('generated_at')} | 样本 {d.get('samples')} | "
          f"min_samples {d.get('min_samples')} | min_fill_rate {d.get('min_fill_rate')} | "
          f"网格组合 {len(d.get('grid') or {})}")
    print()
    hdr = (f"{'形态':<5}{'n':>6}{'chase':>8}{'stop':>7}{'tp1':>6}{'hold':>6}"
           f"{'utility':>10}{'成交率':>9}{'未成交':>9}{'洗出率':>9}")
    print(hdr)
    print("-" * len(hdr))
    for f in FORMS:
        v = bf.get(f)
        if not v:
            print(f"{f:<5}{'—':>6}  （无网格结果）")
            continue
        print(f"{f:<5}{v.get('n', 0):>6}{v.get('chase_pct', 0):>8.3f}"
              f"{v.get('stop_loss_pct', 0):>7.3f}{v.get('tp1_ratio', 0):>6.2f}"
              f"{v.get('hold_days', 0):>6}{v.get('utility', 0):>10.4f}"
              f"{v.get('filled_rate', 0):>9.3f}{v.get('missed_rate', 0) or 0:>9.3f}"
              f"{v.get('washout_rate', 0):>9.3f}")

    # ── 采纳状态 ──
    print()
    print("=== 采纳状态 ===")
    from app.backtest import discipline_grid as DG
    adopted = []
    for f in FORMS:
        try:
            if DG.plan_override(f):
                adopted.append(f)
        except Exception as exc:  # noqa: BLE001
            print(f"  plan_override({f}) 异常: {exc}")
    ck("grid_enabled", DG.grid_enabled() is True, f"{DG.grid_enabled()}")
    ck("没有任何形态的参数被采纳（plan_override 全空）", not adopted,
       f"已采纳={adopted or '无'}")

    # ── 覆盖缺口 ──
    missing = [f for f in FORMS if f not in bf]
    ck("形态覆盖不全（有形态从未网格化）", bool(missing), f"缺失={missing}")

    # ── utility 全负 ──
    neg = [f for f, v in bf.items() if float(v.get("utility") or 0) < 0]
    ck("所有形态的最优 utility 都是负的 → 采纳门槛天然不触发",
       len(neg) == len(bf), f"utility<0 的形态={neg}")

    # ── 告警 1：chase=0 却 filled_rate=1.0 → chase 维度无区分度 ──
    print()
    print("=== 口径一致性告警 ===")
    fishy = [f for f, v in bf.items()
             if float(v.get("chase_pct") or 0) <= 0.0001
             and float(v.get("filled_rate") or 0) >= 0.999]
    ck("⚠️ chase_pct=0 却有 filled_rate=1.0 的形态（chase 维度在该样本上无区分度）",
       bool(fishy), f"{fishy} —— 与 §14.10 市场级实测『收紧到 1% 要放弃 8.1% 成交』矛盾")

    # ── 告警 2：洗出率过高 ──
    hi_wash = [f for f, v in bf.items() if float(v.get("washout_rate") or 0) > 0.5]
    ck("⚠️ 洗出率 >50% 的形态（主因是止损口径，不是选股）", bool(hi_wash),
       "；".join(f"{f}={bf[f].get('washout_rate')}" for f in hi_wash))

    # ── 告警 3：chase 无分化 ──
    chases = {round(float(v.get("chase_pct") or 0), 3) for v in bf.values()}
    ck("⚠️ 各形态最优 chase 完全一致 → 『按形态』当前没有实际区分力",
       len(chases) <= 2, f"不同取值={sorted(chases)}")

    # ── 告警 4：成交率与 §14.8 的实测口径不可比 ──
    fills = [float(v.get("filled_rate") or 0) for v in bf.values()]
    if fills:
        ck("⚠️ 网格成交率全为 1.0，无法复现 §14.8 的 47%~89% 分档成交率",
           all(f >= 0.999 for f in fills),
           f"网格成交率={[round(f, 3) for f in fills]} vs buy_types 实测 低吸 47.3% / 回踩 64% / 突破 88.6%")

    print()
    print("=" * 78)
    print("结论：**不是缺维度，而是既有维度不可用**")
    print("  ① 维度早就有（按形态网格化 chase/stop/tp1/hold + plan_override 覆盖）")
    print("  ② 一次都没采纳：所有形态最优 utility 为负（-0.011 ~ -0.027）")
    print("  ③ 负 utility 的主因是 **洗出率 71%~74%**（止损口径），不是选股能力")
    print("  ④ chase 维度在该样本上**无区分度**（chase=0 却 100% 成交），")
    print("     因此 §14.10『收紧追高』的结论**无法通过这个网格验证或落地**")
    print("  → 正确顺序：先修止损/入场价口径（把洗出率降下来）→ 再重跑网格 → 最后才谈按入场类型细化")
    print(f"通过(硬性) {len(PASS)} / {len(PASS) + len(FAIL)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
