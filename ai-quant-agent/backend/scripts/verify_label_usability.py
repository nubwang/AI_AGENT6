"""标签可用性底线测试：在「T+1 开盘 + 5 日持有」这个标签下，到底有没有可提取的截面 alpha？

## 为什么要做这一步（这是"元层面"的一步）

到这一步为止，已经有两个**互相独立**的特征族在同一标签口径下**全部无效**：

    - 个股博弈特征（§十）：16 个，FDR 后全部不显著
    - 榜单打分键（§11.9）：6 个，按日截面 + 扣成本后全部为负

这强烈提示：问题**可能不在"我们的打分"，而在"我们问的问题（标签本身）"**。

## 本脚本要回答的唯一问题

**用最朴素的经典因子（不带任何"我们的"色彩），在同一口径下测同一个标签 —— 有效吗？**

    - 若**连经典因子也全部无效** ⇒ 病在**标签/窗口**：
      应换标签（更长持有期？超额收益？）或换窗口，而**不是**继续调打分、加特征；
    - 若**其中有效** ⇒ "截面 alpha 存在，只是我们的体系没用上"，那就把有效因子纳入打分。

## 因子（8 个，全部只用 ≤T 的数据；均为经典文献中的常见截面因子）

| 因子        | 含义                       | 常见方向       |
| ----------- | -------------------------- | -------------- |
| `rev1`      | −(昨收→今收) 日收益        | 短期反转（+）  |
| `rev5`      | −(过去 5 日收益)           | 短期反转（+）  |
| `mom20`     | 过去 20 日收益             | 动量（+）      |
| `vol20`     | 20 日日收益标准差          | 低波动异象（−）|
| `turnover`  | 换手率                     | 高换手差（−）  |
| `ln_mv`     | ln(流通市值)               | 小市值异象（−）|
| `bias20`    | 收盘 / MA20 − 1            | 乖离反转（−）  |
| `illiq`     | 20 日均 |日收益| / 成交额    | Amihud 非流动性|

## 标签（5 个）

    fwd1 / fwd5 / fwd10           —— 绝对收益（T+1 开盘买入持有 1/5/10 日）
    fwd5_ex / fwd10_ex            —— **超额**（减去当日全市场等权均值）；
                                     剥掉市场 beta，判断"是真没 alpha 还是被 beta 掩盖"

## 口径（把本轮学到的纪律全用上）

    - **按日截面**分 5 组（每天都要选票的可交易口径），**禁用全局分位**；
    - 检验对象是**逐日 (Q5 − Q1) 差值序列**的**时序 t 检验**（有效样本量 = 天数）；
    - 报告**扣成本后**（双边 0.50%）的 Q5 均值 —— 但 Q5−Q1 的多空 spread 不扣成本
      （A 股不能做空，spread 只作"因子有没有信息"的判据）；
    - **BH-FDR 校正**（8 因子 × 5 标签 = 40 次检验）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_label_usability.py
"""
from __future__ import annotations

import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.backtest import sentiment as ST  # noqa: E402
from app.backtest import sentiment_gate as SG  # noqa: E402
from app.backtest.stats_correction import bh_fdr  # noqa: E402
from app.models import SessionLocal  # noqa: E402

HORIZONS = (1, 5, 10)
COST_RT = 0.005
ALPHA = 0.05
MIN_STOCKS_PER_DAY = 300
MAX_ABS_FWD = 1.5

FACTORS = ("rev1", "rev5", "mom20", "vol20", "turnover", "ln_mv", "bias20", "illiq")
LABELS = ("fwd1", "fwd5", "fwd10", "fwd5_ex", "fwd10_ex")
FACTOR_DESC = {
    "rev1": "1 日反转", "rev5": "5 日反转", "mom20": "20 日动量",
    "vol20": "20 日波动", "turnover": "换手率", "ln_mv": "ln流通市值",
    "bias20": "乖离 MA20", "illiq": "Amihud 非流动性",
}


def _load(sql: str, d0: str, d1: str) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    with SessionLocal() as db:
        conn = db.connection()
        for a, b in ST._year_spans(d0, d1):
            df = pd.read_sql(text(sql), conn, params={"a": a, "b": b})
            if df is not None and not df.empty:
                parts.append(df)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    out["trade_date"] = out["trade_date"].astype(str).str.replace("-", "", regex=False)
    out["ts_code"] = out["ts_code"].astype(str)
    return out


def build_factors(d0: str, d1: str) -> pd.DataFrame:
    daily = _load("SELECT trade_date, ts_code, close, amount FROM daily "
                  "WHERE trade_date >= :a AND trade_date <= :b", d0, d1)
    dbb = _load("SELECT trade_date, ts_code, turnover_rate, circ_mv FROM daily_basic "
                "WHERE trade_date >= :a AND trade_date <= :b", d0, d1)
    if daily.empty:
        return pd.DataFrame()
    for c in ("close", "amount"):
        daily[c] = pd.to_numeric(daily[c], errors="coerce")
    if not dbb.empty:
        for c in ("turnover_rate", "circ_mv"):
            dbb[c] = pd.to_numeric(dbb[c], errors="coerce")
        daily = daily.merge(dbb, on=["trade_date", "ts_code"], how="left")
    for c in ("turnover_rate", "circ_mv"):
        if c not in daily.columns:
            daily[c] = np.nan
    daily = daily[daily["close"] > 0].copy()
    daily = daily.sort_values(["ts_code", "trade_date"], kind="mergesort")
    g = daily.groupby("ts_code", sort=False)
    ret = g["close"].pct_change()
    daily["rev1"] = -ret
    daily["rev5"] = -(g["close"].shift(0) / g["close"].shift(5) - 1.0)
    daily["mom20"] = g["close"].shift(0) / g["close"].shift(20) - 1.0
    daily["vol20"] = ret.groupby(daily["ts_code"]).transform(
        lambda s: s.rolling(20, min_periods=10).std())
    ma20 = g["close"].shift(0).groupby(daily["ts_code"]).transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    daily["bias20"] = daily["close"] / ma20 - 1.0
    daily["ln_mv"] = np.log(daily["circ_mv"].where(daily["circ_mv"] > 0))
    ill = ret.abs() / daily["amount"].where(daily["amount"] > 0)
    daily["illiq"] = ill.groupby(daily["ts_code"]).transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    # daily_basic 的列名是 turnover_rate，因子名统一用 turnover
    daily["turnover"] = daily["turnover_rate"]
    keep = ["trade_date", "ts_code"] + list(FACTORS)
    out = daily[keep].copy()
    for f in FACTORS:
        out[f] = pd.to_numeric(out[f], errors="coerce").astype("float32")
    return out.reset_index(drop=True)


def _tstat(x: np.ndarray) -> tuple[float, float, int]:
    x = np.asarray([v for v in x if np.isfinite(v)], dtype="float64")
    n = len(x)
    if n < 5:
        return 0.0, 1.0, n
    sd = float(x.std(ddof=1))
    if not (sd > 1e-12):
        return 0.0, 1.0, n
    t = float(x.mean()) / (sd / math.sqrt(n))
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return t, p, n


def test_one(df: pd.DataFrame, factor: str, label: str) -> dict:
    """按日截面分 5 组 → 逐日 (Q5−Q1) 差值 → 时序 t 检验。"""
    d = df[[ "trade_date", "ts_code", factor, label]].dropna()
    if d.empty:
        return {"factor": factor, "label": label, "n_days": 0, "p": None}
    cnt = d.groupby("trade_date")["ts_code"].transform("size")
    d = d[cnt >= MIN_STOCKS_PER_DAY]
    if d.empty:
        return {"factor": factor, "label": label, "n_days": 0, "p": None}
    d["_q"] = d.groupby("trade_date")[factor].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False)
        if len(s) >= 5 else np.nan)
    d = d.dropna(subset=["_q"])
    if d.empty:
        return {"factor": factor, "label": label, "n_days": 0, "p": None}
    d["_q"] = d["_q"].astype("int8")
    grp = d.groupby(["trade_date", "_q"])[label].mean().unstack()
    if 4 not in grp.columns or 0 not in grp.columns:
        return {"factor": factor, "label": label, "n_days": 0, "p": None}
    diff = (grp[4] - grp[0]).dropna()
    t, p, n = _tstat(diff.to_numpy())
    # Q5 的日等权均值（用于报"扣成本后"）
    q5 = d[d["_q"] == 4]
    byd5 = q5.groupby("trade_date")[label].mean()
    byd_all = d.groupby("trade_date")[label].mean()
    return {
        "factor": factor, "label": label,
        "n_rows": int(len(d)), "n_days": int(n),
        "q1": round(float(grp[0].mean()), 5),
        "q5": round(float(grp[4].mean()), 5),
        "spread": round(float(diff.mean()), 5),
        "t": round(t, 3), "p": round(p, 6),
        "q5_daily": round(float(byd5.mean()), 5),
        "all_daily": round(float(byd_all.mean()), 5),
        "mono": None,
    }


def run(d0: str = "", d1: str = "") -> dict:
    t0 = time.time()
    sent = ST.load_sentiment()
    rng = (sent or {}).get("range") or []
    if not d0:
        d0 = str(rng[0]) if rng else "20240101"
        d1 = str(rng[1]) if rng else ""
    if not d1:
        return {"ok": False, "msg": "需要 --end 或先构建 sentiment_daily.json"}

    print(f"  构建因子 {d0}~{d1} ...")
    feats = build_factors(d0, d1)
    if feats.empty:
        return {"ok": False, "msg": "因子为空"}
    print(f"  因子表 {len(feats):,} 行；拉取前向收益 ...")
    fwd = SG.build_panel(SG.HORIZONS, d0, d1, keep_stock=True)
    if fwd.empty:
        return {"ok": False, "msg": "前向收益为空"}
    df = feats.merge(fwd, on=["trade_date", "ts_code"], how="inner")
    for h in HORIZONS:
        c = f"fwd{h}"
        df.loc[df[c].abs() > MAX_ABS_FWD, c] = np.nan
    # **超额**标签：减去当日全市场等权均值（剥掉市场 beta）
    for h in HORIZONS:
        c = f"fwd{h}"
        df[f"{c}_ex"] = df[c] - df.groupby("trade_date")[c].transform("mean")
    print(f"  合并后 {len(df):,} 行 / {df['trade_date'].nunique()} 个交易日")

    tests = []
    for label in LABELS:
        for f in FACTORS:
            r = test_one(df, f, label)
            r["factor"] = f
            tests.append(r)

    idx = [i for i, r in enumerate(tests) if r.get("p") is not None]
    fdr = bh_fdr([tests[i]["p"] for i in idx], alpha=ALPHA)
    qmap = dict(zip(fdr.get("idx") or [], fdr.get("q") or []))
    usable = []
    for j, i in enumerate(idx):
        qv = qmap.get(j)
        tests[i]["q"] = qv
        # 可用 = FDR 通过 **且** |spread| 有经济意义（0.5%，与既有门槛一致）
        tests[i]["usable"] = bool(qv is not None and qv <= ALPHA
                                 and abs(tests[i].get("spread") or 0) >= 0.005)
        if tests[i]["usable"]:
            usable.append(tests[i])

    out = {
        "ok": True, "range": [d0, d1], "n_rows": int(len(df)),
        "n_days": int(df["trade_date"].nunique()),
        "fdr": {"m": fdr.get("m"), "n_usable": len(usable)},
        "usable": usable, "tests": tests,
        "verdict": ("存在有效的经典因子" if usable else
                    "★ 连经典因子也全部无效 → 问题在【标签/窗口】，不在打分"),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    return out


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="标签可用性底线测试")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    args = ap.parse_args()

    print("=" * 104)
    print("标签可用性底线测试：经典因子在「T+1 开盘 + 1/5/10 日」标签下有效吗？")
    print("=" * 104)
    r = run(args.start, args.end)
    if not r.get("ok"):
        print(f"失败：{r.get('msg')}")
        return 1

    print(f"样本 {r['n_rows']:,} 行 / {r['n_days']} 个交易日｜"
          f"口径：按日截面分 5 组 + 时序 t 检验 + BH-FDR（m={r['fdr']['m']}）")
    print()
    # 矩阵：行=因子，列=标签，值=Q5−Q1 spread（附显著性标记）
    hdr = f"{'因子':<10}{'说明':<14}" + "".join(f"{lb:>16}" for lb in LABELS)
    print(hdr)
    print("-" * len(hdr))
    for f in FACTORS:
        row = f"{f:<10}{FACTOR_DESC[f]:<14}"
        for lb in LABELS:
            hit = next((x for x in r["tests"]
                        if x["factor"] == f and x["label"] == lb), None)
            if not hit or hit.get("spread") is None:
                row += f"{'—':>16}"
            else:
                mark = "★" if hit.get("usable") else ("*" if (hit.get("q") or 1) <= ALPHA else "")
                txt = f"{hit['spread']:+.2%}{mark}"
                row += f"{txt:>16}"
        print(row)
    print("  标记：★ = FDR 通过 **且** |spread| ≥ 0.5% ｜ * = 仅 FDR 通过")

    print()
    print("--- 各标签的『Q5 日等权均值』与扣成本后（判断能不能只做多头）---")
    for lb in LABELS:
        rows = [x for x in r["tests"] if x["label"] == lb and x.get("q5_daily") is not None]
        if not rows:
            continue
        best = max(rows, key=lambda x: x["q5_daily"])
        # 超额标签本身没有成本含义，不给"扣成本"
        net = best["q5_daily"] - COST_RT if not lb.endswith("_ex") else None
        detail = f"{best['q5_daily']:+.3%}"
        if net is not None:
            detail += f"（扣成本 {net:+.3%}）"
        print(f"  {lb:<10} 最佳因子 {best['factor']:<9} "
              f"Q5={detail}  全样本={best['all_daily']:+.3%}")

    print()
    print(f"FDR 后可用：{r['fdr']['n_usable']} 个（m={r['fdr']['m']}）")
    if r["usable"]:
        for x in r["usable"]:
            print(f"    · {x['factor']}（{FACTOR_DESC.get(x['factor'], '')}）on {x['label']}"
                  f"  spread={x['spread']:+.3%} q={x['q']} 天数={x['n_days']}")
    print()
    print(f"结论：{r['verdict']}")
    print(f"耗时 {r['elapsed_sec']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
