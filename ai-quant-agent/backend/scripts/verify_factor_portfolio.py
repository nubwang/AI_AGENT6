"""对照实验：我们的候选池 vs 纯经典因子选股（plans/24 §11.11 的下一步）

## 为什么这是最关键的一步

到 §11.11 为止已经知道：

    - **全市场**用 4 个简单因子就能拿到 0.5~0.9% 的 spread（§11.10，FDR 通过）；
    - 我们的候选池 1972 条，**无论怎么筛都是负的**（−2.434%；剔除后最好 −0.871%）；
    - 剔掉 55% 后剩下的 45% **仍然亏** → 问题是"**整体选不出好票**"，不是"混入差票"。

于是必须直接回答那个沉重的问题：

    **我们的形态 + 条件概率打分体系，打得过 4 个简单的经典因子吗？**

## 两个窗口（都必须看）

    A **全区间 659 天** —— 统计功效高，用于判断"因子组合本身能不能赚钱"；
    B **与候选池同期的 22 天** —— 与 §11.11 的候选池数字**严格同窗口**，才可比。

## 策略（每天从过滤后的全市场里选 N=30 只，与推荐榜同规模）

    base          全市场等权（基准）
    m20_low       20 日动量**最低** 30（涨最少的 = 反转）
    to_low        换手率**最低** 30
    illiq_high    非流动性**最高** 30
    bias_low      乖离 MA20 **最低** 30
    combo         4 因子日度 z-score 等权（mom20/bias20/turnover 反向 + illiq 正向）前 30

## 实盘可行性过滤（否则结果不可交易）

    - 剔除当日**涨停**（`pct_chg >= 9.5`）—— 次日多半一字板，**买不进**；
    - 剔除收盘价 < 2 元；
    - 剔除当日成交额 < 1000 千元（= 100 万）。

## 口径

    - 收益 = H2 的 `build_panel`（**T+1 开盘买入**，持有 5 日）；
    - 一律**日等权**（每天组合收益再对天平均）；
    - 一律给**扣成本后**（双边 0.50%）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_factor_portfolio.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.backtest import sentiment_gate as SG  # noqa: E402
from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICK_DIR = os.path.join(BACKEND, "data", "daily_recommend")
H = 5
N = 30
COST_RT = 0.005
MAX_ABS = 1.5
D0_ALL = "20240101"
LIMIT_UP_PCT = 9.5          # 近似涨停（买不进）
MIN_PRICE = 2.0
MIN_AMOUNT = 1000.0         # 千元 → 100 万
STRATEGIES = ("base", "m20_low", "to_low", "illiq_high", "bias_low", "combo")
LABEL = {
    "base": "全市场等权（基准）", "m20_low": "mom20 最低 30",
    "to_low": "turnover 最低 30", "illiq_high": "illiq 最高 30",
    "bias_low": "bias20 最低 30", "combo": "4 因子 z 等权 前 30",
}


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


def build(d0: str, d1: str) -> pd.DataFrame:
    daily = _load("SELECT trade_date, ts_code, close, amount, pct_chg FROM daily "
                  "WHERE trade_date >= :a AND trade_date <= :b", d0, d1)
    dbb = _load("SELECT trade_date, ts_code, turnover_rate FROM daily_basic "
                "WHERE trade_date >= :a AND trade_date <= :b", d0, d1)
    if daily.empty:
        return pd.DataFrame()
    for c in ("close", "amount", "pct_chg"):
        daily[c] = pd.to_numeric(daily[c], errors="coerce")
    if not dbb.empty:
        dbb["turnover_rate"] = pd.to_numeric(dbb["turnover_rate"], errors="coerce")
        daily = daily.merge(dbb, on=["trade_date", "ts_code"], how="left")
    if "turnover_rate" not in daily.columns:
        daily["turnover_rate"] = np.nan
    daily = daily[daily["close"] > 0].copy()
    daily = daily.sort_values(["ts_code", "trade_date"], kind="mergesort")
    g = daily.groupby("ts_code", sort=False)
    ret = g["close"].pct_change()
    daily["mom20"] = daily["close"] / g["close"].shift(20) - 1.0
    ma20 = g["close"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    daily["bias20"] = daily["close"] / ma20 - 1.0
    daily["turnover"] = daily["turnover_rate"]
    ill = ret.abs() / daily["amount"].where(daily["amount"] > 0)
    daily["illiq"] = ill.groupby(daily["ts_code"]).transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    # 实盘可行性过滤
    daily = daily[(daily["close"] >= MIN_PRICE)
                  & (daily["amount"] >= MIN_AMOUNT)
                  & (daily["pct_chg"].fillna(0) < LIMIT_UP_PCT)]
    return daily


def _z(df: pd.DataFrame, col: str, sign: float) -> pd.Series:
    """按日截面 z-score（sign=-1 表示越小越好）。"""
    grp = df.groupby("trade_date")[col]
    mu = grp.transform("mean")
    sd = grp.transform("std")
    z = (df[col] - mu) / sd.replace(0, np.nan)
    return (sign * z).astype("float64")


def run_window(df: pd.DataFrame, tag: str, d_from: str = "", d_to: str = "") -> list[dict]:
    d = df if not d_from else df[(df["trade_date"] >= d_from) & (df["trade_date"] <= d_to)]
    if d.empty:
        return []
    d = d.dropna(subset=["fwd5"]).copy()
    if d.empty:
        return []
    # 逐日排序键（越大越好）
    d["_s_m20"] = -d["mom20"]
    d["_s_to"] = -d["turnover"]
    d["_s_illiq"] = d["illiq"]
    d["_s_bias"] = -d["bias20"]
    d["_s_combo"] = (_z(d, "mom20", -1) + _z(d, "turnover", -1)
                     + _z(d, "illiq", 1.0) + _z(d, "bias20", -1))
    key = {"m20_low": "_s_m20", "to_low": "_s_to",
           "illiq_high": "_s_illiq", "bias_low": "_s_bias", "combo": "_s_combo"}

    rows = []
    # 基准：全市场等权
    b = d.groupby("trade_date")["fwd5"].mean()
    rows.append({"tag": tag, "strategy": "base", "n_days": int(len(b)),
                 "hold_n": round(float(d.groupby("trade_date").size().mean()), 0),
                 "mean": round(float(b.mean()), 5),
                 "net": round(float(b.mean()) - COST_RT, 5),
                 "win": round(float((b > 0).mean()), 4)})
    for st in STRATEGIES:
        if st == "base":
            continue
        col = key[st]
        t = d.dropna(subset=[col]).sort_values(["trade_date", col], ascending=[True, False])
        t = t.groupby("trade_date").head(N)
        if t.empty:
            continue
        byd = t.groupby("trade_date")["fwd5"].mean()
        rows.append({"tag": tag, "strategy": st, "n_days": int(len(byd)),
                     "hold_n": round(float(t.groupby("trade_date").size().mean()), 0),
                     "mean": round(float(byd.mean()), 5),
                     "net": round(float(byd.mean()) - COST_RT, 5),
                     "win": round(float((byd > 0).mean()), 4)})
    return rows


def load_picks_same_window() -> tuple[float | None, int]:
    """候选池在同期的日等权 fwd5（用于严格同窗对照）。"""
    files = sorted(glob.glob(os.path.join(PICK_DIR, "daily_*.json")))
    latest: dict[tuple[str, bool], str] = {}
    for p in files:
        m = re.match(r"daily_(\d{8})(_agent)?(_v\d+)?\.json$", os.path.basename(p))
        if m:
            latest[(m.group(1), bool(m.group(2)))] = p
    rows = []
    for (date, _ia), p in sorted(latest.items()):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for it in (d.get("top_picks") or []):
            rows.append({"date": str(d.get("date") or date).replace("-", "")[:8],
                         "ts_code": str(it.get("ts_code") or "")})
    if not rows:
        return None, 0
    pk = pd.DataFrame(rows)
    d0, d1 = str(pk["date"].min()), str(pk["date"].max())
    fwd = SG.build_panel(SG.HORIZONS, d0, d1, keep_stock=True)
    col = f"fwd{H}"
    m = pk.merge(fwd[["trade_date", "ts_code", col]], how="left",
                 left_on=["date", "ts_code"], right_on=["trade_date", "ts_code"])
    m = m.rename(columns={col: "fwd5"})
    m.loc[m["fwd5"].abs() > MAX_ABS, "fwd5"] = np.nan
    m = m.dropna(subset=["fwd5"])
    if m.empty:
        return None, 0
    byd = m.groupby("date")["fwd5"].mean()
    return round(float(byd.mean()), 5), int(len(byd))


def main() -> int:
    t0 = time.time()
    print("=" * 100)
    print("对照实验：我们的候选池 vs 纯经典因子选股（同口径 + 实盘可行性过滤）")
    print("=" * 100)
    with SessionLocal() as db:
        mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
    d1 = str(mx).replace("-", "")[:8] if mx else ""
    print(f"  构建因子面板 {D0_ALL}~{d1} ...")
    df = build(D0_ALL, d1)
    if df.empty:
        print("因子面板为空")
        return 1
    print(f"  过滤后 {len(df):,} 行；拉取前向收益 ...")
    fwd = SG.build_panel(SG.HORIZONS, D0_ALL, d1, keep_stock=True)
    col = f"fwd{H}"
    df = df.merge(fwd[["trade_date", "ts_code", col]], how="inner",
                  on=["trade_date", "ts_code"])
    df = df.rename(columns={col: "fwd5"})
    df.loc[df["fwd5"].abs() > MAX_ABS, "fwd5"] = np.nan

    # 候选池同期窗口
    pk_mean, pk_days = load_picks_same_window()
    pk_from, pk_to = "", ""
    if pk_days:
        files = sorted(glob.glob(os.path.join(PICK_DIR, "daily_*.json")))
        ds = sorted({re.match(r"daily_(\d{8})", os.path.basename(p)).group(1)  # type: ignore[union-attr]
                     for p in files if re.match(r"daily_(\d{8})", os.path.basename(p))})
        pk_from, pk_to = ds[0], ds[-1]

    out: list[dict] = []
    out += run_window(df, "A 全区间(659d)")
    if pk_from:
        out += run_window(df, f"B 同期({pk_from}~{pk_to})", pk_from, pk_to)

    for tag in sorted({r["tag"] for r in out}):
        print()
        print(f"=== 窗口 {tag} ===")
        h = (f"{'策略':<24}{'持有数/天':>10}{'天数':>6}{'均值(日等权)':>14}"
             f"{'扣成本后':>13}{'正收益天':>10}")
        print(h)
        print("-" * len(h))
        for r in [x for x in out if x["tag"] == tag]:
            c1 = f"{r['mean']:+.3%}"
            c2 = f"{r['net']:+.3%}"
            c3 = f"{r['win']:.0%}"
            print(f"{LABEL[r['strategy']]:<24}{r['hold_n']:>10.0f}{r['n_days']:>6}"
                  f"{c1:>14}{c2:>13}{c3:>10}")
        if tag.startswith("B") and pk_mean is not None:
            c1 = f"{pk_mean:+.3%}"
            c2 = f"{pk_mean - COST_RT:+.3%}"
            print(f"{'我们的候选池（同窗口）':<24}{'~31':>10}{pk_days:>6}"
                  f"{c1:>14}{c2:>13}{'36%':>10}")

    print()
    print("=" * 100)
    print("判读：")
    print("  1) 若某个纯因子组合**扣成本后为正**，而我们的候选池为负 → ")
    print("     **我们的形态/打分体系不如 4 个简单因子**（资源应转向因子化选股）；")
    print("  2) 若**所有**纯因子组合也为负 → 问题在**持仓期/仓位管理**，而非选股；")
    print("  3) 窗口 B 只有 22 天，**只看方向与量级**；窗口 A 才有统计意义；")
    print("  4) 已剔除涨停/低价/低流动，但**未扣滑点**（illiq 策略对滑点最敏感）。")
    print(f"耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
