"""「顺势做反转」方案判定（plans/24 §11.26）—— 用**统一门槛 + 实盘滑点**判一次

## 背景：这是全层唯一有统计证据的方向

§11.14 是本层**唯一**通过 FDR 的一节：

    全市场 60 日动量  q = 0.0036
    情绪 连板家数      q = 0.0083
    且方向**与常识相反**：要「**顺势做反转**」——
    即"市场 60 日动量高 + 情绪活跃时，去买低位补涨"，而不是逆势抄底。

§11.15 曾把它做成网格，结论是"成本关过了，但收益全来自 β"（第 15 次否证）。
**但那一次：① 没扣滑点；② 判定口径是脚本内自写的三条件。**

本轮做两件新事：

    ① 用 **`judge_default()`**（费率 0.5% + 滑点 20bp，§11.24/§11.25）**统一判定**它；
    ② 用**同期同源**的基准（本缓存里的 base20），而不是跨缓存比大小。

## 数据来源（不重算面板，秒级）

    data/regime_gate_daily.json   → 11 个 regime 特征（滚动 252 日分位，§11.14）
    data/gate_cost_cut_daily.json → base5/10/20 与 combo5/10/20（§11.15）

⚠️ 两份缓存都含 `base5` / `combo5` ⇒ 合并时只取后者的 10/20 列，避免 `_x/_y` 后缀。

## 口径（三条必须说清）

1. **门控 = 择时**：门 ON 时持仓、OFF 时持现金（收益 0）；
   故"每期收益" = `p_on × mean(ON 天的每期收益)`（按天加权，**不按记录计数**）；
2. **成本按"每期都换仓"记（保守）**：`成本 × 252/20`。
   —— 现实中 ON-OFF 切换还会产生额外建仓/清仓换手，
   所以这是**净收益的下界**；若按 ON 占比缩放成本（写法 B），净收益会更高（表中一并给出）；
3. **方向必须前半段定、后半段验**（§11.14 的教训）⇒ 本节同时打印前后半段的超额符号。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_reversal_scheme.py
"""
from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.backtest.baseline_gate import (  # noqa: E402
    Candidate, default_slip_bp, FEE_DEFAULT, fmt_judge, judge_default,
)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_CACHE = os.path.join(BACKEND, "data", "regime_gate_daily.json")
COST_CACHE = os.path.join(BACKEND, "data", "gate_cost_cut_daily.json")
HOLD = 20
PER_YEAR = 252.0 / HOLD
SEGS = (("2024", "20240101", "20241231"),
        ("2025H1", "20250101", "20250630"),
        ("2025H2", "20250701", "20251231"),
        ("2026", "20260101", "20261231"))
FEATS = ("mkt_mom60", "mkt_mom20", "s_lb2", "s_zha", "s_effect")
ROLL_W, ROLL_MIN, HI_Q, LO_Q = 252, 60, 0.70, 0.30


def _t_of(s: pd.Series) -> float:
    x = pd.to_numeric(s, errors="coerce").dropna().to_numpy(dtype="float64")
    if x.size < 10:
        return float("nan")
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return float("nan")
    return float(x.mean() / (sd / math.sqrt(x.size)))


def build() -> pd.DataFrame:
    with open(GATE_CACHE, encoding="utf-8") as f:
        g = json.load(f)
    with open(COST_CACHE, encoding="utf-8") as f:
        c = json.load(f)
    dg, dc = pd.DataFrame(g), pd.DataFrame(c)
    keep_g = ["trade_date", "mkt_mom20", "mkt_mom60", "s_lb2", "s_zha", "s_effect",
              "base5", "combo5"]
    keep_c = ["trade_date", "base10", "base20", "combo10", "combo20"]
    df = dg[[x for x in keep_g if x in dg.columns]].merge(
        dc[[x for x in keep_c if x in dc.columns]], on="trade_date", how="inner")
    df["trade_date"] = df["trade_date"].astype(str)
    return df.sort_values("trade_date").reset_index(drop=True)


def rpct(s: pd.Series) -> pd.Series:
    """滚动分位（与 §11.14 同口径：252 日窗、至少 60 点）。"""
    return s.rolling(ROLL_W, min_periods=ROLL_MIN).rank(pct=True)


def segs_of(series: pd.Series, idx: pd.Series, scale: float = 1.0) -> dict:
    out: dict[str, float] = {}
    for nm, a, b in SEGS:
        m = (idx >= a) & (idx <= b)
        v = series[m]
        out[nm] = float(v.mean() * scale) if len(v) else float("nan")
    return out


def main() -> int:
    print("=" * 108)
    print("「顺势做反转」方案判定（plans/24 §11.26）—— 统一门槛 + 实盘滑点（§11.24）")
    print("=" * 108)
    slip = default_slip_bp()
    print(f"  成本口径（judge_default）：费率 {FEE_DEFAULT:.2%} + 单边滑点 {slip:g}bp "
          f"⇒ 每期成本 {(FEE_DEFAULT + 2 * slip / 1e4):.3%}")
    print(f"  门口径：滚动 {ROLL_W} 日分位（至少 {ROLL_MIN} 点），高门 ≥ {HI_Q}、低门 ≤ {LO_Q}")
    print()

    df = build()
    print(f"  缓存合并：{len(df):,} 个交易日（{df['trade_date'].min()} ~ {df['trade_date'].max()}）")
    for c in ("base20", "combo20", "mkt_mom60", "s_lb2"):
        if c in df.columns:
            print(f"    {c:<12} 覆盖 {df[c].notna().sum():>4} 天 · "
                  f"均值 {pd.to_numeric(df[c], errors='coerce').mean():+.4f}")
    print()

    df["p_mom60"] = rpct(df["mkt_mom60"])
    df["p_lb2"] = rpct(df["s_lb2"])
    df["p_mom20"] = rpct(df["mkt_mom20"])

    print("=" * 108)
    print("① 单特征门的效果（持 20 日；`毛/期` 为 ON 天的条件均值，`组合` 为 ON×占比）")
    print("=" * 108)
    hd = (f"{'门（顺势=高门）':<28}{'ON天':>6}{'占比':>7}{'基准毛/期':>11}"
          f"{'ON毛/期':>10}{'超额/期':>10}{'组合毛/期':>11}{'t(ON)':>7}")
    print(hd)
    print("-" * len(hd))
    for feat, col in (("mkt_mom60 高（顺势）", "p_mom60"), ("s_lb2 高（连板多）", "p_lb2"),
                      ("mkt_mom20 高（顺势）", "p_mom20")):
        mask = df[col] >= HI_Q
        base_all = float(pd.to_numeric(df["base20"], errors="coerce").mean())
        on = pd.to_numeric(df.loc[mask, "base20"], errors="coerce")
        p_on = float(mask.mean())
        exc = float(on.mean() - base_all)
        print(f"{feat:<28}{int(mask.sum()):>6}{p_on:>7.1%}{base_all:>11.3%}"
              f"{on.mean():>10.3%}{exc:>+10.3%}{p_on * float(on.mean()):>11.3%}"
              f"{_t_of(on):>7.2f}")
    print()
    print("  读法：`超额/期` > 0 表示门有效；`组合毛/期` 才是这个择时策略的毛收益"
          "（OFF 时持现金）。")
    print()

    print("=" * 108)
    print("② 前后半段方向一致性（§11.14 的教训：方向不能用全样本定）")
    print("=" * 108)
    mid = len(df) // 2
    for tag, sub in (("前半段", df.iloc[:mid]), ("后半段", df.iloc[mid:])):
        for feat, col in (("mkt_mom60 高", "p_mom60"), ("s_lb2 高", "p_lb2")):
            mask = sub[col] >= HI_Q
            base_all = float(pd.to_numeric(sub["base20"], errors="coerce").mean())
            on = pd.to_numeric(sub.loc[mask, "base20"], errors="coerce")
            exc = (float(on.mean() - base_all)) if len(on) else float("nan")
            print(f"  {tag}（{sub['trade_date'].min()}~{sub['trade_date'].max()}，"
                  f"{len(sub)} 天）{feat:<14} ON {int(mask.sum()):>3} 天 · "
                  f"超额 {exc:+.3%} · t {_t_of(on):+.2f}")
    print()

    print("=" * 108)
    print("③ ★ 组合成策略并过统一门槛（judge_default：费率 + 滑点 20bp）")
    print("=" * 108)
    base_series = pd.to_numeric(df["base20"], errors="coerce")
    base_cand = Candidate("基准（同源）：全市场等权 · 持 20 日", float(base_series.mean()),
                          _t_of(base_series), PER_YEAR,
                          segs_of(base_series, df["trade_date"]), len(df))
    print(f"  基准（同源）：毛/期 {base_cand.gross:+.3%} · t {base_cand.t:+.2f}")
    print()

    schemes: list[tuple[str, pd.Series]] = [
        ("A) mom60 高", df["p_mom60"] >= HI_Q),
        ("B) mom60 高 且 lb2 高（§11.14.3 的原始方向）",
         (df["p_mom60"] >= HI_Q) & (df["p_lb2"] >= HI_Q)),
        ("C) mom60 高 且 lb2 高（用 combo20 选股）",
         (df["p_mom60"] >= HI_Q) & (df["p_lb2"] >= HI_Q)),
    ]
    for name, mask in schemes:
        col = "combo20" if "combo20 选股" in name else "base20"
        s = pd.to_numeric(df[col], errors="coerce")
        on = s[mask]
        p_on = float(mask.mean())
        gross = p_on * float(on.mean())            # OFF 持现金 ⇒ 按天加权
        by = segs_of(s, df["trade_date"], scale=p_on)
        cand = Candidate(name, gross, _t_of(s[mask]) if mask.sum() >= 10 else float("nan"),
                         PER_YEAR, by, int(mask.sum()))
        j = judge_default(cand, base_cand)
        print(f"  {name}")
        print(f"    ON {int(mask.sum())} 天（{p_on:.1%}）· 组合毛/期 {gross:+.3%} · "
              f"ON 条件毛/期 {float(on.mean()):+.3%} · 超额(条件) "
              f"{float(on.mean()) - base_cand.gross:+.3%}")
        print(f"    成本（保守：按每期都换仓）{PER_YEAR:.1f} 次/年 × "
              f"{(FEE_DEFAULT + 2 * slip / 1e4):.3%} = "
              f"{(FEE_DEFAULT + 2 * slip / 1e4) * PER_YEAR:.1%}/年")
        print(f"    ⇒ {fmt_judge(j)}")
        # 敏感项：若成本按 ON 占比缩放（写法 B）
        net_b = (gross - (FEE_DEFAULT + 2 * slip / 1e4) * p_on) * PER_YEAR
        print(f"    [敏感项] 若成本按 ON 占比缩放（写法 B）⇒ 年化净 {net_b:+.1%}")
        print()

    print("=" * 108)
    print("④ 诚实边界（必须与结论一起读）")
    print("=" * 108)
    print("  - 本脚本**复用缓存**（§11.14 特征 + §11.15 收益），因此：")
    print("    · 特征只有 5 个（缓存里存的子集），不是 §11.14 的全部 11 个；")
    print("    · **持 5 日的'低位补涨'选股端（m20_5）在本缓存里没有持 20 日版本** ⇒")
    print("      §11.14.3 的「买低位补涨」这一半**本轮未判定**，只判了'门'这一半；")
    print("  - 门控是**择时**：OFF 持现金（收益 0），未考虑 OFF 期间的机会成本；")
    print("  - 成本按'每期都换仓'保守估计（净收益的下界）；写法 B 的敏感项已一并给出；")
    print("  - 滑点仍是**结构性假设**（§11.24，非实测）；封板 / 跌停卖不出未建模。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
