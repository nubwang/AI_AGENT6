"""诊断：博弈特征作为"排除器"的可交易性（plans/24 H3 补充）

## 为什么需要这一步

H3 的筛查发现：有预测力的博弈特征**全部是负超额**（高组表现更差），
即它们是**排除器（negative screen）**，不是选股增强：
    - `chenhuo`（趁火打劫：单日跌>5% 且主力在买）→ 高组比低组 **−0.750%**（q=0.0002, 659 天）
    - `kongcheng`（空城计：一字板）            → 高组比低组 **−4.357%**（q=0.007, 但仅 57 天）

"高减低显著" **不等于** "能赚钱"：做空不可行、且一字板本来就买不到。
真正要问的是 —— **把这类票从候选池里剔掉，池子的期望能不能提升？**

## 本脚本做什么

在**与推荐样本同期**的窗口（20260801~20260918）上，用全市场口径算：
    - 全体基线 fwd5
    - 剔除各"排除器"命中票后的 fwd5
    - 每日被剔除的比例（**剔太多就没票可选了**，必须一起看）

并给出 H2 定义的参照：同期全市场 fwd5 = −0.353%、推荐样本 = −5.55%。

## ⚠️ 局限（必须一起读）

1. 窗口只有约 30 个交易日 → **均值差不可靠**，本脚本只看**方向与量级**，不下显著性结论；
2. 这是**全市场口径**：剔除效果取决于"该特征在候选池里的占比"，
   而我们的候选池（几十只 top_picks）未必与全市场同分布 → **需在候选池上再验一遍**（下一步）；
3. 一字板命中极少（全市场每天几只），剔除它对本池影响有限。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/diagnose_game_exclusions.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from app.backtest import game_features as GF  # noqa: E402
from app.backtest import sentiment_gate as SG  # noqa: E402

D0, D1 = "20260801", "20260918"      # 与推荐样本同期
H = 5
# H2 已测定的同期参照（用于对照）
MARKET_SAME_PERIOD = -0.00353
PICK_SAMPLE = -0.0555

# 排除器候选：H3 全区间筛查里"高组为负"且通过 FDR 的（含"幅度不足"的作对照）
EXCLUDERS = ("chenhuo", "kongcheng", "dacao", "shushang", "churn")


def _daily_mean(df: pd.DataFrame, col: str) -> float | None:
    """日度均值再对日度序列求平均（避免"某天股票多"的权重偏置）。"""
    if df.empty:
        return None
    d = df.dropna(subset=[col])
    if d.empty:
        return None
    return float(d.groupby("trade_date")[col].mean().mean())


def main() -> int:
    print("=" * 92)
    print(f"排除器可交易性诊断（plans/24 H3 补充）— 窗口 {D0}~{D1}（与推荐样本同期）")
    print("=" * 92)
    feats = GF.build_features(D0, D1)
    if feats.empty:
        print("特征为空，退出")
        return 1
    fwd = SG.build_panel(SG.HORIZONS, D0, D1, keep_stock=True)
    df = feats.merge(fwd, on=["trade_date", "ts_code"], how="inner")
    col = f"fwd{H}"
    df = df.dropna(subset=[col])
    n_days = df["trade_date"].nunique()
    print(f"样本：{len(df):,} 行 / {n_days} 个交易日")

    base = _daily_mean(df, col)
    print()
    print(f"全市场等权 fwd{H}（本窗口实测）        = {base:.3%}"
          if base is not None else "基线不可算")
    print(f"（H2 用近 30 日算的同期值           = {MARKET_SAME_PERIOD:.3%}）")
    print(f"（我们的推荐样本，plans/23 §15.6    = {PICK_SAMPLE:.3%}）")

    print()
    hdr = (f"{'排除器(计谋)':<22}{'命中占比':>10}{'剔除后 fwd5':>14}"
           f"{'相对基线':>12}{'判定':>10}")
    print(hdr)
    print("-" * len(hdr))
    rows = []
    for f in EXCLUDERS:
        if f not in df.columns:
            continue
        hit = df[f] > 0.5
        n_hit = int(hit.sum())
        ratio = n_hit / max(1, len(df))
        rest = df[~hit]
        m = _daily_mean(rest, col)
        delta = (m - base) if (m is not None and base is not None) else None
        # 判定：剔除后必须**变好**且幅度有实盘意义（≥0.3%），且不能剔掉太多
        ok = (delta is not None and delta >= 0.003 and ratio <= 0.10)
        rows.append((f, ratio, m, delta, ok))
        nm = f"{f}（{GF.PLAN_NAME.get(f, '—')}）"
        print(f"{nm:<22}{ratio:>10.3%}"
              f"{(f'{m:.3%}' if m is not None else '—'):>14}"
              f"{(f'{delta:+.3%}' if delta is not None else '—'):>12}"
              f"{('✅ 有效' if ok else '⚠️ 不达标'):>10}")

    # 组合剔除
    comb = df.copy()
    for f in EXCLUDERS:
        if f in comb.columns:
            comb = comb[comb[f] <= 0.5]
    mc = _daily_mean(comb, col)
    print()
    if mc is not None and base is not None:
        print(f"全部排除器一起剔除：剩 {len(comb):,} 行（{len(comb)/max(1,len(df)):.1%}），"
              f"fwd{H} = {mc:.3%}（相对基线 {mc - base:+.3%}）")

    print()
    print("=" * 92)
    print("判读（以数据为准）")
    print("  1) 若某排除器『剔除后基线上升 ≥0.3% 且剔除比例 ≤10%』→ 值得进 H4 做候选池回测；")
    print("  2) **不要**把『高减低显著』直接当成可交易 —— 做空不可行、一字板买不到；")
    print("  3) 本窗口仅约 30 个交易日，均值差不稳：只看方向与量级，**不下显著性结论**；")
    print("  4) 真正验收必须回到**候选池口径**（几十只 top_picks），全市场口径只能算初筛。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
