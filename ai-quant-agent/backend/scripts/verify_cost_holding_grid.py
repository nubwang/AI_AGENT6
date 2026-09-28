"""成本 × 持有期敏感性网格（plans/24 §11.12.6）

## 为什么做这个（而不是继续加特征）

§11.12 已经证明了两件事：

    1) 我们的形态 + 条件概率打分体系**不如 4 个简单因子**
       （同期同口径差 2.8~2.9pp；全区间我们的池是全表最差）；
    2) 更底层：**5 日持有这个频率可能结构性不可行** ——
       年约 49 次全换仓 × 双边 0.5% ≈ **24%/年成本**，
       而全市场 `fwd5` 均值只有 **+0.342%**（年化毛收益约 17%）。

于是真正该问的不再是"再加什么特征"，而是：

    **在哪个 (费率, 持有期) 格子里，扣成本后仍然为正？**

## 网格

    持有期 h ∈ {5, 10, 20} 交易日
    双边费率 f ∈ {0.10%, 0.25%, 0.50%}

## 策略（与 §11.12 完全同口径：**直接复用** verify_factor_portfolio 的因子与过滤）

    base       全市场等权（基准）
    combo      4 因子（mom20 / turnover / illiq / bias20）日度 z-score 等权，取前 30
    m20_low    mom20 最低 30（单因子最强）
    to_low     turnover 最低 30

## 口径（沿用 §11.11 / §11.12 的红线）

    - 收益 = `sentiment_gate.build_panel`：**T+1 开盘买入、持 h 日**（天然无未来函数）；
    - **逐日**取前 N，再对天做**日等权**平均（不是按记录计数）；
    - 实盘可行性过滤：剔除当日涨停（`pct_chg >= 9.5`）、`close < 2`、`amount < 1000` 千元；
    - `|fwd| > 1.5` 视为脏数据剔除；
    - 净收益 = 每期毛收益 − 双边费率；**年化 = 每期 × (252 / h)**。

## 诚实边界

    - 未扣**滑点与冲击成本**（`illiq` 策略对此最敏感，见 §11.12 局限）；
    - 年化是"简单线性外推"，不等于复利，仅用于**比较量级**；
    - 未考虑**涨跌停不可成交**的成交价修正（已过滤当日涨停，但买入日一字板仍无法成交）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_cost_holding_grid.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

import verify_factor_portfolio as VFP  # noqa: E402  同口径复用（不重写因子）
from app.backtest import sentiment_gate as SG  # noqa: E402
from app.models import SessionLocal  # noqa: E402

HORIZONS = (5, 10, 20)
FEES = (0.001, 0.0025, 0.005)
N = 30
MAX_ABS = 1.5
TRADING_DAYS = 252.0
D0 = "20240101"
KEYS = (("combo", "_s_combo"), ("m20_low", "_s_m20"), ("to_low", "_s_to"))
LABEL = {
    "base": "全市场等权（基准）",
    "combo": "4因子 z 等权 前30",
    "m20_low": "mom20 最低 30",
    "to_low": "turnover 最低 30",
}


def _pick(d: pd.DataFrame, col: str) -> pd.DataFrame:
    """逐日按 col 降序取前 N（col 越大越好，方向已在构造排序键时统一）。"""
    t = d.dropna(subset=[col])
    if t.empty:
        return t
    t = t.sort_values(["trade_date", col], ascending=[True, False], kind="mergesort")
    return t.groupby("trade_date").head(N)


def _by_year(h: int, tag: str, d: pd.DataFrame) -> list[dict]:
    """按年拆解：日等权「每期毛收益」。

    ★ 这是本脚本最关键的一道检验（实测发现）：
      只看全区间会把 2024~2025 的正收益与 2026 的崩溃平均掉，
      从而得出"某个格子可用"的**假结论**。
    """
    out: list[dict] = []
    if d.empty:
        return out
    yr = d["trade_date"].astype(str).str[:4]
    for y, g in d.groupby(yr):
        byd = g.groupby("trade_date")["y"].mean()
        if byd.empty:
            continue
        out.append({"h": h, "tag": tag, "year": str(y),
                    "n_days": int(len(byd)), "gross": float(byd.mean())})
    return out


def _by_month(h: int, tag: str, d: pd.DataFrame) -> list[dict]:
    """按月拆解（只用于最近年份）：判断"某年翻负"是**全年衰退**还是**某几个月崩**。"""
    out: list[dict] = []
    if d.empty:
        return out
    ym = d["trade_date"].astype(str).str[:6]
    for k, g in d.groupby(ym):
        byd = g.groupby("trade_date")["y"].mean()
        if byd.empty:
            continue
        out.append({"h": h, "tag": tag, "month": str(k),
                    "n_days": int(len(byd)), "gross": float(byd.mean())})
    return out


def _row(h: int, per_year: float, tag: str, s: pd.Series) -> dict:
    """把一个策略的逐日收益序列汇总成一行（含 t 值 / 各费率下的年化净收益）。"""
    m = float(s.mean())
    n = int(len(s))
    sd = float(s.std(ddof=1)) if n > 1 else float("nan")
    if n > 1 and sd > 0:
        tstat = m / (sd / float(np.sqrt(n)))
    else:
        tstat = float("nan")
    return {
        "h": h, "per_year": per_year, "tag": tag, "n_days": n,
        "gross": m, "ann_gross": m * per_year, "t": tstat,
        "net": {f: m - f for f in FEES},
        "ann_net": {f: (m - f) * per_year for f in FEES},
    }


def main() -> int:  # noqa: C901
    import argparse

    ap = argparse.ArgumentParser(description="成本 × 持有期网格（plans/24 §11.12.6 / §11.36.16）")
    ap.add_argument("--from", dest="d0", default="",
                    help="起始日 YYYYMMDD（缺省用脚本常量 D0 ⇒ **默认行为不变**）；"
                         "用于扩样本重跑，如 --from 20100101")
    args = ap.parse_args()

    global D0
    if args.d0:
        D0 = str(args.d0).replace("-", "")[:8]

    t0 = time.time()
    print("=" * 118)
    print("成本 × 持有期敏感性网格（plans/24 §11.12.6）")
    print("=" * 118)
    if args.d0:
        print(f"  ★ 起始日覆盖为 {D0}（来自 --from；脚本常量本身不变）")
    with SessionLocal() as db:
        mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
    d1 = str(mx).replace("-", "")[:8] if mx else ""
    print(f"  ① 构建因子面板 {D0}~{d1}（复用 verify_factor_portfolio.build，口径一致）...")
    df = VFP.build(D0, d1)
    if df.empty:
        print("  因子面板为空，退出")
        return 1
    # 排序键（越大越好）：与 §11.12 逐字一致
    df["_s_combo"] = (VFP._z(df, "mom20", -1) + VFP._z(df, "turnover", -1)
                      + VFP._z(df, "illiq", 1.0) + VFP._z(df, "bias20", -1))
    df["_s_m20"] = -df["mom20"]
    df["_s_to"] = -df["turnover"]
    print(f"     过滤后 {len(df):,} 行")
    print(f"  ② 拉取 {HORIZONS} 日前向收益（T+1 开盘买入）...")
    panel = SG.build_panel(HORIZONS, D0, d1, keep_stock=True)
    if panel is None or panel.empty:
        print("  面板为空，退出")
        return 1
    print(f"     {len(panel):,} 行")

    rows: list[dict] = []
    yearly: list[dict] = []
    monthly: list[dict] = []
    for h in HORIZONS:
        col = f"fwd{h}"
        if col not in panel.columns:
            print(f"  ⚠️ 面板缺少 {col}，跳过持有 {h} 日")
            continue
        p = panel[["trade_date", "ts_code", col]].rename(columns={col: "y"})
        d = df.merge(p, how="inner", on=["trade_date", "ts_code"])
        d["y"] = pd.to_numeric(d["y"], errors="coerce")
        d = d[d["y"].abs() <= MAX_ABS].dropna(subset=["y"])
        if d.empty:
            print(f"  ⚠️ 持有 {h} 日无有效样本，跳过")
            continue
        per_year = TRADING_DAYS / float(h)
        # 基准
        b = d.groupby("trade_date")["y"].mean()
        rows.append(_row(h, per_year, "base", b))
        yearly += _by_year(h, "base", d)
        if h in (5, 10):
            monthly += _by_month(h, "base", d)
        for tag, c in KEYS:
            t = _pick(d, c)
            if t.empty:
                continue
            s = t.groupby("trade_date")["y"].mean()
            rows.append(_row(h, per_year, tag, s))
            yearly += _by_year(h, tag, t)
            if h in (5, 10) and tag == "combo":
                monthly += _by_month(h, tag, t)

    for h in HORIZONS:
        rs = [r for r in rows if r["h"] == h]
        if not rs:
            continue
        per = float(rs[0]["per_year"])
        print()
        print(f"=== 持有 {h} 日（年约 {per:.1f} 次全换仓 = 252/{h}）===")
        hd = (f"{'策略':<22}{'每期毛':>10}{'年化毛':>10}"
              f"{'净@0.10%':>11}{'年化净':>10}"
              f"{'净@0.25%':>11}{'年化净':>10}"
              f"{'净@0.50%':>11}{'年化净':>10}{'t(毛)':>8}{'天数':>6}")
        print(hd)
        print("-" * len(hd))
        for r in rs:
            lab = LABEL[r["tag"]]
            g = f"{r['gross']:+.3%}"
            ag = f"{r['ann_gross']:+.1%}"
            n1 = f"{r['net'][FEES[0]]:+.3%}"
            a1 = f"{r['ann_net'][FEES[0]]:+.1%}"
            n2 = f"{r['net'][FEES[1]]:+.3%}"
            a2 = f"{r['ann_net'][FEES[1]]:+.1%}"
            n3 = f"{r['net'][FEES[2]]:+.3%}"
            a3 = f"{r['ann_net'][FEES[2]]:+.1%}"
            tv = f"{r['t']:+.2f}"
            print(f"{lab:<22}{g:>10}{ag:>10}{n1:>11}{a1:>10}"
                  f"{n2:>11}{a2:>10}{n3:>11}{a3:>10}{tv:>8}{r['n_days']:>6}")

    # ---- 判定：哪些格子年化净收益 > 0 ----
    print()
    print("=" * 118)
    print("★ 判定：年化净收益 > 0 的格子")
    pos: list[tuple[dict, float]] = []
    for r in rows:
        for f in FEES:
            if r["ann_net"][f] > 0:
                pos.append((r, f))
    if not pos:
        print("  ❌ 全部为负 → 在当前费率与持有期下**没有任何可用格子**（不应上生产）")
        print("     下一步应转向：更低的费率假设？更长的持有期（20 日以上）？还是干脆放弃该频率？")
    else:
        for r, f in sorted(pos, key=lambda x: -x[0]["ann_net"][x[1]]):
            lab = LABEL[r["tag"]]
            af = f"{f:.2%}"
            an = f"{r['ann_net'][f]:+.1%}"
            gp = f"{r['gross']:+.3%}"
            tv = f"{r['t']:+.2f}"
            print(f"  ✅ 持 {r['h']:>2} 日 | {lab:<18} | 费率 {af} | "
                  f"每期毛 {gp} | 年化净 {an} | t(毛) {tv} | {r['n_days']} 天")

    if yearly:
        print()
        print("=== ★★ 年度 × 策略（每期毛收益；用于区分「市场 regime」与「因子失效」）===")
        years = sorted({r["year"] for r in yearly})
        for h in HORIZONS:
            rs = [r for r in yearly if r["h"] == h]
            if not rs:
                continue
            per = TRADING_DAYS / float(h)
            print()
            print(f"--- 持有 {h} 日（年约 {per:.1f} 次换仓）---")
            hd = f"{'策略':<22}" + "".join(f"{y:>12}" for y in years)
            print(hd)
            print("-" * len(hd))
            for tg in ("base",) + tuple(k for k, _ in KEYS):
                cells = []
                for y in years:
                    hit = [r for r in rs if r["tag"] == tg and r["year"] == y]
                    cells.append(f"{hit[0]['gross']:+.3%}" if hit else "—")
                print(f"{LABEL[tg]:<22}" + "".join(f"{c:>12}" for c in cells))
        print()
        print("  判读：")
        print("    - **每一年**都为正 → 才可能是真效应；")
        print("    - 只有部分年份为正 → **regime 依赖**，必须按当前年份状态判断能否用；")
        print("    - **最新一年翻负** → 严格讲**不可上生产**，无论全区间年化多好看。")

    if monthly:
        print()
        print("=== ★★★ 月度拆解（最近年份）：判断「某年翻负」是全年衰退还是某几个月崩 ===")
        months = sorted({r["month"] for r in monthly})
        months = [m for m in months if m[:4] >= "2026"] or months[-9:]
        for h in (5, 10):
            rs = [r for r in monthly if r["h"] == h]
            if not rs:
                continue
            per = TRADING_DAYS / float(h)
            print()
            print(f"--- 持有 {h} 日（年约 {per:.1f} 次换仓）---")
            hd = (f"{'月份':<10}{'天数':>6}{'基准(毛)':>11}{'combo(毛)':>12}"
                  f"{'超额':>10}{'combo 年化':>12}")
            print(hd)
            print("-" * len(hd))
            for m in months:
                bb = [r for r in rs if r["tag"] == "base" and r["month"] == m]
                cc = [r for r in rs if r["tag"] == "combo" and r["month"] == m]
                if not bb and not cc:
                    continue
                gb = bb[0]["gross"] if bb else float("nan")
                gc = cc[0]["gross"] if cc else float("nan")
                nd = cc[0]["n_days"] if cc else (bb[0]["n_days"] if bb else 0)
                s1 = f"{gb:+.3%}" if bb else "—"
                s2 = f"{gc:+.3%}" if cc else "—"
                s3 = f"{gc - gb:+.3%}" if (bb and cc) else "—"
                s4 = f"{gc * per:+.1%}" if cc else "—"
                print(f"{m:<10}{nd:>6}{s1:>11}{s2:>12}{s3:>10}{s4:>12}")
        print()
        print("  判读：")
        print("    - 若只有个别月份崩 → 可能是**事件性冲击**（可做风控规避）；")
        print("    - 若连续多月为负且超额为负 → **因子已失效**（regime 切换，不是运气问题）；")
        print("    - 若近月已恢复为正 → 不能断言「永久失效」，但**样本不足以证明可用**。")

    print()
    print("=" * 118)
    print("读数（不要把任何一格当成「可以上生产」）：")
    print("  1) 若某格年化净为正，先看 **t 值是否 > 2** —— 否则可能只是噪声；")
    print("  2) 再看 **年度分解是否每年都为正** —— 若只有某一年为正，就是「单年运气」；")
    print("  3) 最后扣**滑点**：`illiq` 类策略的滑点会显著吃掉优势；")
    print("  4) 年化是线性外推（非复利），仅用于比较不同 (费率, 持有期) 的量级。")
    print(f"耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
