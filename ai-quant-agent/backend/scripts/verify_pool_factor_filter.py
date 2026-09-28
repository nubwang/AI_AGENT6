"""验证：用「已证有效」的截面因子筛选候选池（plans/24 §11.10 的下一步）

## 背景

§11.10 用底线测试证明了：**标签可用、截面 alpha 存在**，且找到了**唯一经过
FDR + 扣成本 + 超额三重检验**的有效因子（8 因子 × 5 标签，m=40，通过 10 个）：

| 因子 | 最佳标签 | spread(Q5−Q1) | q | 方向 |
| ---- | -------- | ------------- | - | ---- |
| `mom20`（20 日动量） | fwd10 | **−0.917%** | 0.0000 | **反向**（涨多的后续差 = 反转） |
| `illiq`（非流动性） | fwd10 | **+0.612%** | 0.0022 | 正向 |
| `bias20`（乖离 MA20） | fwd10 | **−0.574%** | 0.0045 | **反向**（超买差） |
| `turnover`（换手率） | fwd10 | −0.515% | 0.0367 | 反向 |
| `rev5`（5 日反转） | fwd5/fwd10 | +0.33/+0.36% | *（幅度不足） |

## 本脚本要回答的问题

把这些因子用到**我们自己的候选池**上，有用吗？

**但先要过一道前置关（否则会白做）**：
候选池每天只有 30 只（且都是"形态确认"过的票），**它们在这些因子上的分布可能已经
高度同质化**（都是刚启动的、涨幅相近的）。若池内横截面离散度远小于全市场，
那么"在池内分档/剔除"**天然没有区分度** —— 这会先被 A 组测出来。

## 分组

    A 池内分布 vs 全市场分布（是否已同质化？）
    B 池内按因子分 3 档（低/中/高）的 fwd5（日等权 + 扣成本）
    C 剔除模拟：剔除池内该因子最高 1/3，看剩余池能否改善（含组合剔除）
    D 与同期全市场基线对照

## 口径（沿用 §11.8 定下的红线）

    - 一律**按日截面**（每天都要选票）；
    - 一律给**扣成本后**（双边 0.50%）；
    - 收益口径 = H2 的 `build_panel`（T+1 开盘买入持 5 日），**禁用全局分位**。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_pool_factor_filter.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.backtest import sentiment_gate as SG  # noqa: E402
from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICK_DIR = os.path.join(BACKEND, "data", "daily_recommend")
H = 5
MAX_ABS = 1.5
COST_RT = 0.005
LOOKBACK_FROM = "20260401"          # 因子需要 ~60 天回溯
FACTORS = ("mom20", "bias20", "turnover", "illiq")


def load_picks() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(PICK_DIR, "daily_*.json")))
    latest: dict[tuple[str, bool], str] = {}
    for p in files:
        m = re.match(r"daily_(\d{8})(_agent)?(_v\d+)?\.json$", os.path.basename(p))
        if m:
            latest[(m.group(1), bool(m.group(2)))] = p
    rows: list[dict] = []
    for (date, is_agent), p in sorted(latest.items()):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for it in (d.get("top_picks") or []):
            rows.append({
                "date": str(d.get("date") or date).replace("-", "")[:8],
                "is_agent": is_agent,
                "ts_code": str(it.get("ts_code") or ""),
                "form_type": str(it.get("form_type") or ""),
            })
    return pd.DataFrame(rows)


def build_factors() -> pd.DataFrame:
    """4 个候选因子的全市场面板（只用 ≤T 数据）。"""
    d0, d1 = LOOKBACK_FROM, ""
    with SessionLocal() as db:
        mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
    d1 = str(mx).replace("-", "")[:8] if mx else ""
    sql_d = ("SELECT trade_date, ts_code, close, amount FROM daily "
             "WHERE trade_date >= :a AND trade_date <= :b")
    sql_b = ("SELECT trade_date, ts_code, turnover_rate FROM daily_basic "
             "WHERE trade_date >= :a AND trade_date <= :b")
    parts_d, parts_b = [], []
    with SessionLocal() as db:
        conn = db.connection()
        for y0, y1 in SG.ST._year_spans(d0, d1):
            a = pd.read_sql(text(sql_d), conn, params={"a": y0, "b": y1})
            b = pd.read_sql(text(sql_b), conn, params={"a": y0, "b": y1})
            if a is not None and not a.empty:
                parts_d.append(a)
            if b is not None and not b.empty:
                parts_b.append(b)
    df = pd.concat(parts_d, ignore_index=True) if parts_d else pd.DataFrame()
    if df.empty:
        return df
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "", regex=False)
    df["ts_code"] = df["ts_code"].astype(str)
    for c in ("close", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if parts_b:
        b = pd.concat(parts_b, ignore_index=True)
        b["trade_date"] = b["trade_date"].astype(str).str.replace("-", "", regex=False)
        b["ts_code"] = b["ts_code"].astype(str)
        b["turnover_rate"] = pd.to_numeric(b["turnover_rate"], errors="coerce")
        df = df.merge(b, on=["trade_date", "ts_code"], how="left")
    else:
        df["turnover_rate"] = np.nan
    df = df[df["close"] > 0].copy()
    df = df.sort_values(["ts_code", "trade_date"], kind="mergesort")
    g = df.groupby("ts_code", sort=False)
    ret = g["close"].pct_change()
    df["mom20"] = df["close"] / g["close"].shift(20) - 1.0
    ma20 = g["close"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    df["bias20"] = df["close"] / ma20 - 1.0
    df["turnover"] = df["turnover_rate"]
    ill = ret.abs() / df["amount"].where(df["amount"] > 0)
    df["illiq"] = ill.groupby(df["ts_code"]).transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    keep = ["trade_date", "ts_code"] + list(FACTORS)
    out = df[keep].copy()
    for f in FACTORS:
        out[f] = pd.to_numeric(out[f], errors="coerce").astype("float64")
    return out.reset_index(drop=True)


def attach(df: pd.DataFrame, factors: pd.DataFrame) -> pd.DataFrame:
    d0, d1 = str(df["date"].min()), str(df["date"].max())
    fwd = SG.build_panel(SG.HORIZONS, d0, d1, keep_stock=True)
    col = f"fwd{H}"
    out = df.merge(fwd[["trade_date", "ts_code", col]], how="left",
                   left_on=["date", "ts_code"], right_on=["trade_date", "ts_code"])
    out = out.rename(columns={col: "fwd5"})
    out.loc[out["fwd5"].abs() > MAX_ABS, "fwd5"] = np.nan
    out["fwd5_net"] = out["fwd5"] - COST_RT
    out = out.merge(factors, how="left", left_on=["date", "ts_code"],
                    right_on=["trade_date", "ts_code"], suffixes=("", "_f"))
    return out


def dmean(df: pd.DataFrame, col: str) -> float | None:
    v = pd.to_numeric(df[col], errors="coerce")
    k = v.notna()
    if not k.any():
        return None
    byd = v[k].groupby(df["date"][k]).mean()
    return round(float(byd.mean()), 5) if len(byd) else None


def _line(name: str, df: pd.DataFrame, base: float | None) -> None:
    m = dmean(df, "fwd5")
    n = dmean(df, "fwd5_net")
    c1 = f"{m:+.3%}" if m is not None else "—"
    c2 = f"{n:+.3%}" if n is not None else "—"
    dd = f"{m - base:+.3%}" if (m is not None and base is not None) else "—"
    print(f"{name:<34}{len(df):>6}{c1:>13}{c2:>13}{dd:>12}")


def _hdr() -> None:
    h = (f"{'方案':<34}{'n':>6}{'均值(日等权)':>13}{'扣成本后':>13}{'相对基线':>12}")
    print(h)
    print("-" * len(h))


def main() -> int:
    print("=" * 100)
    print("候选池 × 有效因子筛选（plans/24 §11.10 后续）")
    print("=" * 100)
    picks = load_picks()
    print(f"  构建因子面板（{LOOKBACK_FROM} 起）...")
    fac = build_factors()
    if fac.empty:
        print("因子面板为空")
        return 1
    ev = attach(picks, fac).dropna(subset=["fwd5"])
    print(f"候选池 {len(ev)} 条 / {ev['date'].nunique()} 个可评估交易日"
          f"｜口径：按日截面 + 扣成本 {COST_RT:.2%}")

    # ── A 池内分布 vs 全市场分布 ──
    print()
    print("=== A 池内横截面离散度 vs 全市场（判断是否已同质化）===")
    print(f"  {'因子':<10}{'池内日内std均值':>18}{'全市场日内std均值':>20}{'比值':>10}")
    for f in FACTORS:
        pool_std, mkt_std = [], []
        for d, g in ev.groupby("date"):
            v = g[f].dropna()
            if len(v) >= 5:
                pool_std.append(float(v.std()))
            gm = fac[fac["trade_date"] == d][f].dropna()
            if len(gm) >= 100:
                mkt_std.append(float(gm.std()))
        ps = float(np.mean(pool_std)) if pool_std else None
        ms = float(np.mean(mkt_std)) if mkt_std else None
        r = f"{ps / ms:.2f}" if (ps and ms) else "—"
        a = f"{ps:.4f}" if ps is not None else "—"
        b = f"{ms:.4f}" if ms is not None else "—"
        print(f"  {f:<10}{a:>18}{b:>20}{r:>10}")
    print("  判读：比值 ≪1 → 池内已同质化，**池内分档/剔除天然没有区分度**；")

    # ── B 池内分 3 档 ──
    base = dmean(ev, "fwd5")
    print()
    print("=== B 池内按因子分 3 档（低/中/高）===")
    for f in FACTORS:
        d = ev.dropna(subset=[f, "fwd5"]).copy()
        if len(d) < 100:
            continue
        d["_q"] = d.groupby("date")[f].transform(
            lambda s: pd.qcut(s.rank(method="first"), 3, labels=False)
            if len(s) >= 6 else np.nan)
        d = d.dropna(subset=["_q"])
        print(f"  --- {f} ---")
        _hdr()
        for q, nm in ((0, "低档"), (1, "中档"), (2, "高档")):
            sub = d[d["_q"] == q]
            if not sub.empty:
                _line(f"  {f} {nm}", sub, base)

    # ── C 剔除模拟 ──
    print()
    print("=== C 剔除模拟（剔除池内该因子最高 1/3）===")
    _hdr()
    _line("现状（全池基线）", ev, base)

    def drop_top(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.DataFrame:
        """按日剔除这些因子都处于最高 1/3 的记录（用任一因子=并集更狠；这里用**全部满足**）。"""
        d = df.copy()
        masks = []
        for c in cols:
            th = d.groupby("date")[c].transform(lambda s: s.quantile(2 / 3))
            masks.append(d[c] >= th)
        m = masks[0]
        for x in masks[1:]:
            m = m & x
        return d[~m.fillna(False)]

    def drop_top_union(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.DataFrame:
        """按日剔除"**任一**因子处于最高 1/3"的记录（并集，剔得更多）。"""
        d = df.copy()
        m = pd.Series(False, index=d.index)
        for c in cols:
            th = d.groupby("date")[c].transform(lambda s: s.quantile(2 / 3))
            m = m | (d[c] >= th).fillna(False)
        return d[~m]

    for cols, nm in ((("mom20",), "剔除 mom20 最高 1/3"),
                     (("bias20",), "剔除 bias20 最高 1/3"),
                     (("turnover",), "剔除 turnover 最高 1/3"),
                     (("mom20", "bias20"), "剔除 mom20∩bias20 最高 1/3（交集）"),
                     (("turnover", "bias20"), "剔除 turnover∪bias20 最高 1/3（并集）"),
                     (("turnover", "bias20", "mom20"),
                      "剔除 turnover∪bias20∪mom20（并集）")):
        sub = drop_top(ev, cols) if "交集" in nm else drop_top_union(ev, cols)
        _line(nm, sub, base)

    # 正向因子：只保留 illiq 最高 1/3
    th = ev.groupby("date")["illiq"].transform(lambda s: s.quantile(2 / 3))
    _line("只保留 illiq 最高 1/3", ev[ev["illiq"] >= th], base)

    # ── D 与同期市场对照 ──
    print()
    print("=== D 与同期全市场基线对照 ===")
    fwd_mkt = SG.build_panel(SG.HORIZONS, str(ev["date"].min()), str(ev["date"].max()))
    mkt = float(fwd_mkt[f"fwd{H}"].mean())
    print(f"  同期全市场等权 fwd{H}（22 日窗口）        = {mkt:+.3%}")
    print(f"  候选池现状                                = {base:+.3%}")
    best = None
    for cols, nm in ((("mom20",), "剔除 mom20 最高 1/3"),
                     (("bias20",), "剔除 bias20 最高 1/3")):
        m = dmean(drop_top(ev, cols), "fwd5")
        if m is not None and (best is None or m > best[1]):
            best = (nm, m)
    if best:
        dv1 = f"{best[1] - base:+.3%}" if base is not None else "—"
        dv2 = f"{best[1] - mkt:+.3%}"
        print(f"  最佳剔除方案：{best[0]} → {best[1]:+.3%}"
              f"（相对候选池 {dv1}；相对市场 {dv2}）")

    # ── C2 逐日分解（防"只靠少数几天"）──
    print()
    print("=== C2 逐日分解：改善是不是只来自少数几天？===")
    for cols, nm in ((("turnover",), "剔除 turnover 最高 1/3"),
                     (("turnover", "bias20", "mom20"), "剔除三因子并集")):
        sub = drop_top_union(ev, cols)
        byd = sub.groupby("date")["fwd5"].mean()
        n_pos = int((byd > 0).sum())
        tot = float(byd.sum())
        top1 = (float(byd.max()) / tot) if abs(tot) > 1e-12 else float("nan")
        print(f"  {nm}: 剩余 {len(sub)} 条 / {len(byd)} 天，"
              f"正收益 {n_pos}/{len(byd)} 天（{n_pos / max(1, len(byd)):.0%}），"
              f"最大单日贡献 {top1:.0%}")

    print()
    print("=" * 100)
    print("判读：")
    print("  1) A 组比值 ≪1 → 池内同质化，B/C 的'无差异'是**结构性**的，不是因子的问题；")
    print("  2) C 组改善必须同时满足：扣成本后为正 **且** 相对候选池基线有实质提升（≥0.5pp）；")
    print("  3) 即便改善，也要看是否只靠少数几天（本脚本未做逐日分解 —— 若进入下一步必须补）；")
    print("  4) 本窗口仅 22 个可评估交易日、样本 ~1900 → **只看方向与量级**。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
