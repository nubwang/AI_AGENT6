"""基准落地前的必答问题：**买 30 只能不能代表"全市场等权"？**（plans/24 §11.18）

## 为什么这是"落地 B1"的前置条件

§11.16 定义的基准是：

    B1 = **全市场等权**（过滤后）+ 持 20 日 + 扣 0.5% ⇒ +11.1%/年
    B2 = **Q1 最小市值 20% 等权** + 持 20 日      ⇒ +17.7%/年

但实盘不可能持有 1000+ 只（甚至 500 只）。**如果只买 30 只，误差有多大？**
如果抽样误差是每年 ±10pp，那"买 30 只代表 B1"就是**自欺欺人** ——
必须先知道误差量级，再决定"买多少只"或"干脆买 ETF"。

## 本脚本做什么

对每个交易日，在标的池里**随机抽 n 只**（不重复），算等权收益，重复 R 次；
把 R 条"日度序列"各自年化，看**分布**（不是只看均值）：

    n ∈ {30, 50, 100, 200, 500}    ×   池 ∈ {全市场（B1）, Q1 最小市值（B2）}
    R = 200 次重复（固定随机种子，可复现）

同时报告：全量基准年化、抽样均值、1σ、5%~95% 分位、以及"抽样误差（1σ）随 n 的下降"。

## 口径（沿用 §11.16）

    T+1 开盘买入、持 20 日；日等权；扣双边 0.50%；`|fwd|>1.5` 剔除；
    过滤：`pct_chg < 9.5`、`close ≥ 2`、`amount ≥ 1000` 千元、`circ_mv > 0`。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_baseline_sampling.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.backtest import sentiment_gate as SG  # noqa: E402
from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BACKEND, "data", "baseline_sampling_daily.json")
H = 20
FEE = 0.005
PER_YEAR = 252.0 / H
MAX_ABS = 1.5
D0 = "20240101"
N_LIST = (30, 50, 100, 200, 500)
REPS = 200
SEED = 20260921
OOS_FROM = "20250206"      # 与 §11.14/§11.15 的样本外切分点一致
OURS_OOS_NET = 0.025       # §11.15：我们的体系在样本外同口径下 +2.5%/年
MIN_PRICE = 2.0
MIN_AMOUNT = 1000.0
LIMIT_UP_PCT = 9.5


def _load(sql: str, d0: str, d1: str) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    with SessionLocal() as db:
        conn = db.connection()
        for a, b in SG.ST._year_spans(d0, d1):
            df = pd.read_sql(text(sql), conn, params={"a": a, "b": b})
            if df is not None and not df.empty:
                parts.append(df)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    out["trade_date"] = out["trade_date"].astype(str).str.replace("-", "", regex=False)
    out["ts_code"] = out["ts_code"].astype(str)
    return out


def build() -> dict:
    """返回 {date: {"all": ndarray, "q1": ndarray}}（已经是过滤 + 截尾后的 fwd20 数组）。"""
    with SessionLocal() as db:
        mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
    d1 = str(mx).replace("-", "")[:8] if mx else ""
    print(f"  ① 拉取日线 + daily_basic（{D0} ~ {d1}）...")
    daily = _load("SELECT trade_date, ts_code, close, amount, pct_chg FROM daily "
                  "WHERE trade_date >= :a AND trade_date <= :b", D0, d1)
    dbb = _load("SELECT trade_date, ts_code, circ_mv FROM daily_basic "
                "WHERE trade_date >= :a AND trade_date <= :b", D0, d1)
    if daily.empty or dbb.empty:
        raise RuntimeError("daily / daily_basic 为空")
    for c in ("close", "amount", "pct_chg"):
        daily[c] = pd.to_numeric(daily[c], errors="coerce")
    dbb["circ_mv"] = pd.to_numeric(dbb["circ_mv"], errors="coerce")
    d = daily.merge(dbb, on=["trade_date", "ts_code"], how="inner")
    d = d[(d["close"] >= MIN_PRICE) & (d["amount"] >= MIN_AMOUNT)
          & (d["pct_chg"].fillna(0) < LIMIT_UP_PCT) & (d["circ_mv"] > 0)].copy()
    d["lnmv"] = np.log(d["circ_mv"])
    print(f"     过滤后 {len(d):,} 行")

    print(f"  ② 拉取 {H} 日前向收益 ...")
    panel = SG.build_panel((H,), D0, d1, keep_stock=True)
    col = f"fwd{H}"
    if panel is None or panel.empty or col not in panel.columns:
        raise RuntimeError("前向收益面板不可用")
    p = panel[["trade_date", "ts_code", col]].rename(columns={col: "y"})
    m = d.merge(p, how="inner", on=["trade_date", "ts_code"])
    m["y"] = pd.to_numeric(m["y"], errors="coerce")
    m = m[m["y"].abs() <= MAX_ABS].dropna(subset=["y"])

    print("  ③ 按日切分（全市场 / Q1 最小市值 20%）...")
    out: dict[str, dict[str, np.ndarray]] = {"all": {}, "q1": {}}
    for dt, g in m.groupby("trade_date", sort=True):
        y = g["y"].to_numpy(dtype="float64")
        if y.size < 60:
            continue
        out["all"][str(dt)] = y
        thr = float(np.quantile(g["lnmv"].to_numpy(dtype="float64"), 0.20))
        yq = g.loc[g["lnmv"] <= thr, "y"].to_numpy(dtype="float64")
        if yq.size >= 30:
            out["q1"][str(dt)] = yq
    print(f"     全市场 {len(out['all'])} 天；Q1 {len(out['q1'])} 天")
    return out


def _stats(series: np.ndarray) -> dict:
    net = (float(series.mean()) - FEE) * PER_YEAR
    sd = float(series.std(ddof=1)) if series.size > 1 else float("nan")
    return {"net": net, "sd": sd, "n": int(series.size)}


def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-cache", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    print("=" * 118)
    print("基准落地前置检验：**买 n 只能不能代表「全市场等权」**（plans/24 §11.18）")
    print("=" * 118)

    if args.from_cache and os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            raw = json.load(f)
        data = {"all": {k: np.array(v) for k, v in raw["all"].items()},
                "q1": {k: np.array(v) for k, v in raw["q1"].items()}}
        print(f"  复用缓存 {os.path.relpath(CACHE, BACKEND)}")
    else:
        data = build()
        try:
            with open(CACHE, "w", encoding="utf-8") as f:
                json.dump({k: {d: v.tolist() for d, v in dd.items()} for k, dd in data.items()},
                          f, ensure_ascii=False)
            print(f"  已缓存 → {os.path.relpath(CACHE, BACKEND)}")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 缓存失败：{e}")

    for universe, lab in (("all", "B1 全市场等权"), ("q1", "B2 Q1 最小市值 20%")):
        dd = data[universe]
        dates = sorted(dd)
        if not dates:
            continue
        full = np.array([dd[d].mean() for d in dates])
        mask_oos = np.array([d >= OOS_FROM for d in dates])
        full_oos = _stats(full[mask_oos])
        f = _stats(full)
        print()
        print(f"=== {lab}（持 {H} 日、双边 {FEE:.2%}；全区间 {f['n']} 天）===")
        print(f"  全量基准（等权全部标的）：全区间 **{f['net']:+.1%}** / "
              f"样本外（≥{OOS_FROM}，{full_oos['n']} 天）**{full_oos['net']:+.1%}**")
        print()
        hd = (f"{'买入只数':>9}{'抽样均值':>10}{'年化1σ':>9}{'5%分位':>10}{'25%分位':>10}"
              f"{'中位':>9}{'75%分位':>10}{'95%分位':>10}{'5~95跨度':>11}"
              f"{'单期跟踪误差':>13}{'样本外均值':>12}")
        print(hd)
        print("-" * len(hd))
        rng = np.random.default_rng(SEED)
        sd_curve: list[tuple[int, float, float]] = []
        oos_best: float = float("nan")
        for n in N_LIST:
            if n > min(len(dd[d]) for d in dates) // 2:
                continue
            reps = np.empty(REPS)
            reps_oos = np.empty(REPS)      # ★ 与 §11.15「我们的体系 +2.5%」同窗口
            tes: list[float] = []          # 单期跟踪误差（相对全量基准的逐日偏离 std）
            for r in range(REPS):
                ser = np.empty(len(dates))
                for i, dt in enumerate(dates):
                    arr = dd[dt]
                    idx = rng.choice(arr.size, size=n, replace=False)
                    ser[i] = arr[idx].mean()
                reps[r] = (ser.mean() - FEE) * PER_YEAR
                reps_oos[r] = (ser[mask_oos].mean() - FEE) * PER_YEAR
                tes.append(float((ser - full).std(ddof=1)))
            q = np.percentile(reps, [5, 25, 50, 75, 95])
            sd = float(reps.std(ddof=1))
            te = float(np.mean(tes))
            sd_curve.append((n, sd, te))
            if n == 30:
                oos_best = float(reps_oos.mean())
            print(f"{n:>9}{reps.mean():>+10.1%}{sd:>9.1%}{q[0]:>+10.1%}{q[1]:>+10.1%}"
                  f"{q[2]:>+9.1%}{q[3]:>+10.1%}{q[4]:>+10.1%}"
                  f"{(q[4] - q[0]):>11.1%}{te:>13.2%}{reps_oos.mean():>+12.1%}")
        print()
        print(f"  ★★ 同窗口对比（样本外 ≥ {OOS_FROM}，{full_oos['n']} 天）—— 与 §11.15 严格可比：")
        print(f"     全量基准            ：{full_oos['net']:+.1%}/年")
        print(f"     随机抽 30 只        ：{oos_best:+.1%}/年（200 次重复的均值）")
        print(f"     **我们的体系（30 只）**：**{OURS_OOS_NET:+.1%}/年**（§11.15，同口径同窗口）")
        gap = oos_best - OURS_OOS_NET
        print(f"     ⇒ 都是 30 只、同窗口、同成本口径，差异 = **{gap:+.1%}/年**"
              f"（随机抽 − 我们的体系）")
        print("     ⇒ 即：**我们的「选股」比「随机抽 30 只」还差** ——"
              " 这不是「没效果」，是**负 alpha**。")
        print()
        print("  ★ 两个误差必须分开看（这是本节最重要的区分）：")
        for n, sd, te in sd_curve:
            print(f"    买 {n:>3} 只 → **长期（年化）误差 1σ = {sd:.1%}/年**，"
                  f"**单期跟踪误差 = {te:.2%}/期**")
        print("     （长期误差小是因为 638 天的平均把噪声平滑掉了；")
        print("       单期误差才是实盘每天要忍受的波动 —— 用它判断「能不能拿得住」）")

    print()
    print("=" * 118)
    print("判读（先定好）：")
    print("  1) 若买 30 只的 1σ 达到两位数（如 ≥10pp/年）⇒ **「买 30 只代表 B1」不成立**，")
    print("     落地时必须买足够多只（或改用 ETF / 指数）——这条直接决定落地形态；")
    print("  2) 若 5% 分位已经低于 0 ⇒ 即使基准长期为正，**任一个 30 只组合都可能亏**；")
    print("  3) 抽样只在「当期可买池」里抽（已过滤涨停/低价/低流动），所以这是**乐观口径**；")
    print("     真实还要扣滑点与买入日一字板（小市值组最重）。")
    print(f"耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
