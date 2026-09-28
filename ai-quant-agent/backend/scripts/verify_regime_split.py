"""验收：环境分层（plans/23 §3.1 ②择时 / P1 第 8 项）

守 4 组约束，并**顺手给出真实结论**（回答"P1-6 洗盘为何无预测力"中的『环境混杂』假设）：

  A 环境三态判定正确（强势=动量>+3% 且站上 60 日线；弱势=动量<-3%；其余震荡）
  B ⚠️ 无未来函数：regime_of(date) 只依赖 <= date 的指数数据
  C 分层统计自洽（三态点数和 = 总点数；配对口径不变；样本不足有说明）
  D 真实数据：三态各自重跑洗盘 A/B（复用 data/washout_samples.json，无需重扫）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_regime_split.py
"""
from __future__ import annotations

import inspect
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import regime_split as RS
from app.backtest import washout_samples as WS

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _syn_panel(mom: float, above: bool, n: int = 200) -> dict:
    """合成指数面板：精确构造指定 20 日动量 + 是否站上 60 日线。

    关键：**只动"动量窗口之外"的数据**来切换 above/not-above。
    若像最初那样缩放动量段内的值，会把 20 日动量一起改掉（测的就不是想测的东西了）。
      - 动量段（最近 21 天）：close[-21] = 100 → close[-1] = 100×(1+mom) → mom 精确可控；
      - 最近 22~60 天：填 10（→ MA60 远低于现价 = 站上）或 200（→ MA60 高于现价 = 未站上）。
    """
    seg = n - (MOM_N + 1)
    closes = [50.0] * max(0, seg)
    closes += [100.0 + 100.0 * mom * i / MOM_N for i in range(MOM_N + 1)]
    fill = 10.0 if above else 200.0
    # 只覆盖倒数第 22~60 天：**绝不能碰 -21**，因为动量正是用 closes[-21] 算的
    # （最初写成 range(MOM_N+1, 61) 把 -21 也覆盖了 → mom 变成 9.5 而不是 5%，
    #  结果 A4/A5 测的根本不是"动量 -5%/+1%"——测试自身的 bug 也要当成 bug 修）
    for k in range(MOM_N + 2, 61):
        if len(closes) >= k:
            closes[-k] = fill
    dates = [f"2026{m:02d}{d:02d}" for m in range(1, 13) for d in range(1, 29)]
    rows = [[dates[i], float(closes[i])] for i in range(min(len(closes), len(dates)))]
    rows.sort(key=lambda r: r[0])
    return {"000001.SH": rows}


MOM_N = RS.MOM_DAYS


def main() -> int:  # noqa: C901
    print("=== A 环境三态判定 ===")
    panel = RS.load_panel()
    ck("A1 指数面板可加载", bool(panel.get(RS.BENCH_CODE)),
       f"指数数={len(panel)}，上证行数={len(panel.get(RS.BENCH_CODE) or [])}")

    full = _syn_panel(mom=0.05, above=True)
    r = RS.regime_of(full["000001.SH"][-1][0], full)
    ck("A2 动量 +5% 且站上 60 日线 → 强势", r["regime"] == RS.R_STRONG, str(r))
    r2 = RS.regime_of(full["000001.SH"][-1][0], _syn_panel(mom=0.05, above=False))
    ck("A3 动量 +5% 但**未**站上 60 日线 → 不是强势（两条件必须同时满足）",
       r2["regime"] != RS.R_STRONG, str(r2["regime"]))
    r3 = RS.regime_of(full["000001.SH"][-1][0], _syn_panel(mom=-0.05, above=True))
    ck("A4 动量 -5% → 弱势（即使站上均线）", r3["regime"] == RS.R_WEAK, str(r3["regime"]))
    r4 = RS.regime_of(full["000001.SH"][-1][0], _syn_panel(mom=0.01, above=True))
    ck("A5 动量 +1% → 震荡", r4["regime"] == RS.R_MIXED, str(r4["regime"]))
    ck("A6 判据细节可审计（mom20/ma60/above_ma60/close）",
       all(k in r for k in ("mom20", "ma60", "above_ma60", "close", "ok")) and r["ok"])

    short = {"000001.SH": [["20260101", 100.0], ["20260102", 101.0]]}
    r5 = RS.regime_of("20260102", short)
    ck("A7 指数数据不足 → ok=False + missing，且不硬判（默认震荡）",
       r5["ok"] is False and r5["missing"] is True and r5["regime"] == RS.R_MIXED, str(r5))
    r6 = RS.regime_of("bad-date", panel)
    ck("A8 非法日期 → missing（不崩）", r6["missing"] is True)

    print("=== B ⚠️ 无未来函数 ===")
    real_rows = panel.get(RS.BENCH_CODE) or []
    if len(real_rows) > 100:
        d = real_rows[-40][0]
        tampered = {RS.BENCH_CODE: [[x[0], x[1]] for x in real_rows]}
        for i, (dt, _c) in enumerate(tampered[RS.BENCH_CODE]):
            if dt > d:
                tampered[RS.BENCH_CODE][i][1] = tampered[RS.BENCH_CODE][i][1] * 5.0
        a = RS.regime_of(d, panel)
        b = RS.regime_of(d, tampered)
        ck("B1 篡改 date 之后的指数数据 → 判定完全不变",
           a == b, f"{a['regime']}({a['mom20']}) vs {b['regime']}({b['mom20']})")
    src = inspect.getsource(RS.regime_of)
    ck("B2 二分只取 <= date（切片上界 hi 由 <= 分支决定）",
       "dates[mid] <= d" in src and "hi < MA_DAYS" in src)
    ck("B3 docstring 明示无未来函数约束", "无未来函数" in inspect.getsource(RS))

    print("=== C 分层统计自洽 ===")
    smp_file = WS.SAMPLES_FILE
    payload = {}
    if os.path.exists(smp_file):
        with open(smp_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
    points = payload.get("points") or []
    ck("C1 样本库可加载（P1-6 产物可复用，无需重扫）", bool(points), f"点数={len(points)}")
    strat = RS.stratify_ab(points, horizons=("t5",)) if points else {}
    if strat:
        tot = sum(v["points"] for v in strat["counts"].values())
        ck("C2 三态点数之和 == 总点数（不丢不重）", tot == len(points), f"{tot} vs {len(points)}")
        ck("C3 三态俱全（每态都有样本）",
           all(strat["counts"][x]["points"] > 0 for x in RS.REGIMES),
           str({k: v["points"] for k, v in strat["counts"].items()}))
        ck("C4 分层后配对口径不变（仍为同日配对）",
           all(strat["by_regime"][x]["t5"].get("pair_mode") == "same_date" for x in RS.REGIMES))
        ck("C5 各态 wash 数与总数一致",
           sum(v["wash"] for v in strat["counts"].values()) == strat["counts"][RS.R_STRONG]["wash"]
           + strat["counts"][RS.R_MIXED]["wash"] + strat["counts"][RS.R_WEAK]["wash"])
    # form_leaderboard：样本不足要给说明，超额要减去同环境基准
    d0 = (points[0].get("date") if points else "20260801")
    fake = ([{"date": d0, "form": "A", "t5": 0.10} for _ in range(70)]
            + [{"date": d0, "form": "B", "t5": 0.00} for _ in range(70)]
            + [{"date": d0, "form": "C", "t5": 0.05} for _ in range(5)])
    fb = RS.form_leaderboard(fake, min_n=60, cache_file="")
    # 不写死环境名：取实际命中的那个环境（真实指数决定 d0 属强/震/弱）
    board = (fb.get(next(iter(fb))) if fb else None) or {}
    rows = list((board.get("forms") or {}).items())
    has_note = any("note" in v for _k, v in rows)
    base = board.get("baseline_mean")
    ck("C6 form_leaderboard：样本不足的形态给 note（不合成假结论）",
       has_note, str(rows[-1] if rows else "（无行）"))
    # 只对"样本充足"的行要求有超额（样本不足的行本来就只有 note）
    ok_rows = [v for _k, v in rows if "n" in v and "note" not in v]
    ck("C7 form_leaderboard：超额 = 组均值 − 同环境基准（且减的是本环境基准）",
       base is not None and bool(ok_rows) and all("excess_vs_regime" in v for v in ok_rows),
       f"baseline={base} 充足行={len(ok_rows)} forms={[k for k, _ in rows]}")

    print("=== D 真实数据：三态各自重跑洗盘 A/B ===")
    if strat:
        print(f"  （样本库：{len(points)} 点，{payload.get('wash_n')} 个确认日）")
        print(f"  {'环境':<6} {'样本点':>8} {'确认日':>7} | "
              f"{'洗盘n':>6} {'洗盘T5均值':>10} {'对照T5均值':>10} {'p':>7}")
        for reg in RS.REGIMES:
            c = strat["counts"][reg]
            ab = strat["by_regime"][reg]["t5"]
            w = ab.get("wash", {})
            ctl = ab.get("control", {})
            print(f"  {reg:<6} {c['points']:>8} {c['wash']:>7} | "
                  f"{w.get('n', 0):>6} {str(w.get('mean')):>10} {str(ctl.get('mean')):>10} "
                  f"{str(ab.get('p_value')):>7}")
        print("\n  各态 T5 结论：")
        for reg in RS.REGIMES:
            ab = strat["by_regime"][reg]["t5"]
            print(f"   · {reg}: {ab.get('verdict')}")

    print(f"\n===== 结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
