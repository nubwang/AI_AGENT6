"""市值分层与基准定义（plans/24 §11.15.4 的下一步：把基准落地）

## 先纠正我自己上一轮的一处不严谨

§11.15 我把 `always-base` 称作「小盘等权」，但它的真实定义其实是：

    **「过滤后全市场等权」**（剔除当日涨停 / close<2 / amount<1000 千元）

A 股里小盘股数量多，所以"全市场等权"在权重上**天然偏向小盘** ——
但这是**间接推断**，不是事实。要把它当作"基准策略"落地，必须先回答两件事：

    1. **收益到底来自哪个市值层？**（最小 20% 组是不是真的最强？）
    2. **该用"等权"还是"市值加权"？**

## 本脚本做什么

    ① 逐日截面按 `ln(circ_mv)` 分成 5 组（Q1 最小 ~ Q5 最大），等权持有，持 h ∈ {5,10,20} 日；
    ② 同时算「全市场等权」与「市值加权」两个基准；
    ③ 一律：T+1 开盘买入（`sentiment_gate.build_panel`）、日等权、扣双边 0.50%、
       `|fwd| > 1.5` 剔除；
    ④ **输出必须分段**（2024 / 2025H1 / 2025H2 / 2026）—— §11.13/§11.15 的教训：
       全区间年化会把"某一年的强势"伪装成"长期有效"；
    ⑤ 结果落盘 `data/size_baseline_daily.json`，供今后所有验证脚本当**永久对照基线**。

## 诚实登记（重要）

    - 若 `daily` 表**不含已退市股票**，则本表存在**幸存者偏差**，对小市值组影响最大
      （小盘股退市率最高）→ **小市值组的历史收益会被系统性高估**。
      这一点必须在任何"小盘等权"结论前先说清楚；
    - 未扣滑点；小市值组滑点最高，**实盘折损最重**；
    - 未考虑"买入日一字板不可成交"。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_size_baseline.py
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.backtest import sentiment_gate as SG  # noqa: E402
from app.backtest.stats_correction import t_compare  # noqa: E402
from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BACKEND, "data", "size_baseline_daily.json")
HORIZONS = (5, 10, 20)
FEE = 0.005
MAX_ABS = 1.5
D0 = "20240101"
NQ = 5
MIN_PRICE = 2.0
MIN_AMOUNT = 1000.0
LIMIT_UP_PCT = 9.5
SEGS = (("2024", "20240101", "20241231"),
        ("2025H1", "20250101", "20250630"),
        ("2025H2", "20250701", "20251231"),
        ("2026", "20260101", "20261231"))
QN = ("Q1 最小", "Q2", "Q3", "Q4", "Q5 最大")


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


def _t_of(s: pd.Series) -> float:
    x = pd.to_numeric(s, errors="coerce").dropna().to_numpy(dtype="float64")
    if x.size < 10:
        return float("nan")
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return float("nan")
    return float(x.mean() / (sd / math.sqrt(x.size)))


def _lab(s: pd.Series) -> pd.Series:
    """当日截面按 ln(circ_mv) 等频分 5 组（0=最小）；重复值多时用 rank 兜底。"""
    try:
        return pd.qcut(s.rank(method="first"), NQ, labels=False)
    except ValueError:
        return pd.Series(np.nan, index=s.index)


# 各层日序列（只用于 §11.28 的 HAC 复核；**不写进 size_baseline_daily.json**，
# 因为 baseline_gate.load_market_baseline() 依赖后者的 list 结构 —— 不动它就是不动生产）
_SERIES: dict[str, dict[str, float]] = {}
SERIES_OUT = os.path.join(BACKEND, "data", "size_baseline_series.json")


def build() -> list[dict]:
    with SessionLocal() as db:
        mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
        mn = db.execute(text("SELECT MIN(trade_date) FROM daily")).scalar()
    d1 = str(mx).replace("-", "")[:8] if mx else ""
    print(f"  daily 表覆盖：{str(mn)[:8]} ~ {d1}")
    daily = _load("SELECT trade_date, ts_code, close, amount, pct_chg FROM daily "
                  "WHERE trade_date >= :a AND trade_date <= :b", D0, d1)
    dbb = _load("SELECT trade_date, ts_code, circ_mv, turnover_rate FROM daily_basic "
                "WHERE trade_date >= :a AND trade_date <= :b", D0, d1)
    if daily.empty or dbb.empty:
        raise RuntimeError("daily / daily_basic 为空")
    for c in ("close", "amount", "pct_chg"):
        daily[c] = pd.to_numeric(daily[c], errors="coerce")
    dbb["circ_mv"] = pd.to_numeric(dbb["circ_mv"], errors="coerce")
    d = daily.merge(dbb[["trade_date", "ts_code", "circ_mv"]],
                    on=["trade_date", "ts_code"], how="inner")
    d = d[(d["close"] >= MIN_PRICE) & (d["amount"] >= MIN_AMOUNT)
          & (d["pct_chg"].fillna(0) < LIMIT_UP_PCT)
          & (d["circ_mv"] > 0)].copy()
    d["lnmv"] = np.log(d["circ_mv"])
    print(f"     过滤后 {len(d):,} 行")

    panel = SG.build_panel(HORIZONS, D0, d1, keep_stock=True)
    if panel is None or panel.empty:
        raise RuntimeError("前向收益面板为空")

    recs: list[dict] = []
    for h in HORIZONS:
        col = f"fwd{h}"
        if col not in panel.columns:
            continue
        p = panel[["trade_date", "ts_code", col]].rename(columns={col: "y"})
        m = d.merge(p, how="inner", on=["trade_date", "ts_code"])
        m["y"] = pd.to_numeric(m["y"], errors="coerce")
        m = m[m["y"].abs() <= MAX_ABS].dropna(subset=["y"])
        if m.empty:
            continue
        # 逐日截面 5 等分（同日内的相对市值，无未来函数）
        m["_q"] = m.groupby("trade_date")["lnmv"].transform(_lab)
        m["_wy"] = m["y"] * m["circ_mv"]
        g = m.groupby(["trade_date", "_q"])["y"].mean().unstack()
        mkt = m.groupby("trade_date")["y"].mean()
        capw = (m.groupby("trade_date")["_wy"].sum()
                / m.groupby("trade_date")["circ_mv"].sum())
        for q in range(NQ):
            if q not in g.columns:
                continue
            s = g[q].dropna()
            tc = t_compare(s.to_numpy(dtype="float64"), h - 1)
            _SERIES[f"h{h}_size{q}"] = {str(k): float(v) for k, v in s.items()}
            recs.append({"h": h, "q": q, "kind": "size",
                         "gross": float(s.mean()), "n": int(len(s)),
                         "t": tc["t_naive"], "t_hac": tc["t_hac"],
                         "by_seg": {nm: float(s[(s.index >= a) & (s.index <= b)].mean())
                                    for nm, a, b in SEGS}})
        for kind, s in (("market_ew", mkt), ("market_capw", capw)):
            s = s.dropna()
            tc = t_compare(s.to_numpy(dtype="float64"), h - 1)
            _SERIES[f"h{h}_{kind}"] = {str(k): float(v) for k, v in s.items()}
            recs.append({"h": h, "q": -1, "kind": kind,
                         "gross": float(s.mean()), "n": int(len(s)),
                         "t": tc["t_naive"], "t_hac": tc["t_hac"],
                         "by_seg": {nm: float(s[(s.index >= a) & (s.index <= b)].mean())
                                    for nm, a, b in SEGS}})
        print(f"     h={h:>2} 完成（{len(mkt)} 天）")
    return recs


def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-cache", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    print("=" * 118)
    print("市值分层与基准定义（plans/24 §11.16）")
    print("=" * 118)

    if args.from_cache and os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            recs = json.load(f)
        print(f"  复用缓存 {os.path.relpath(CACHE, BACKEND)}")
    else:
        recs = build()
        try:
            with open(CACHE, "w", encoding="utf-8") as f:
                json.dump(recs, f, ensure_ascii=False)
            print(f"  已缓存 → {os.path.relpath(CACHE, BACKEND)}")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 缓存失败：{e}")

    if _SERIES:
        stats: dict = {}
        for r in recs:
            k = f"h{r['h']}_size{r['q']}" if r["q"] >= 0 else f"h{r['h']}_{r['kind']}"
            stats[k] = {"naive_t": r.get("t"), "hac_t": r.get("t_hac"), "n": r.get("n")}
        try:
            with open(SERIES_OUT, "w", encoding="utf-8") as f:
                json.dump({"series": _SERIES, "stats": stats}, f, ensure_ascii=False)
            print(f"  已落盘日序列 → {os.path.relpath(SERIES_OUT, BACKEND)}"
                  f"（{len(_SERIES)} 条；供 §11.28 的 HAC 复核复用）")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 日序列落盘失败：{e}")
    else:
        print("  ⚠️ 本次未生成日序列（`--from-cache` 复用旧缓存）⇒ "
              "如需 HAC 复核，请不带 `--from-cache` 重跑一次")

    for h in HORIZONS:
        rs = [r for r in recs if r["h"] == h]
        if not rs:
            continue
        per = 252.0 / h
        print()
        print(f"=== 持有 {h} 日（年约 {per:.1f} 次换仓，双边 {FEE:.2%}；"
              f"各段为**年化净**）===")
        hd = (f"{'组':<10}{'每期毛':>10}{'年化毛':>10}{'全扣年化':>11}"
              f"{'t':>7}{'t_HAC':>8}{'天数':>6}" + "".join(f"{s[0]:>11}" for s in SEGS))
        print(hd)
        print("-" * len(hd))
        order = [("size", q) for q in range(NQ)] + [("market_ew", -1), ("market_capw", -1)]
        for kind, q in order:
            hit = [r for r in rs if r["kind"] == kind and r["q"] == q]
            if not hit:
                continue
            r = hit[0]
            lab = QN[q] if kind == "size" else ("全市场等权" if kind == "market_ew" else "市值加权")
            c1 = f"{r['gross']:+.3%}"
            c2 = f"{r['gross'] * per:+.1%}"
            c3 = f"{(r['gross'] - FEE) * per:+.1%}"
            c4 = "—" if math.isnan(r["t"]) else f"{r['t']:+.2f}"
            th = r.get("t_hac")
            c5 = "—" if th is None or math.isnan(th) else f"{th:+.2f}"
            segs_s = "".join(
                f"{(r['by_seg'].get(s[0], float('nan')) - FEE) * per:>+11.1%}"
                if not math.isnan(r["by_seg"].get(s[0], float("nan"))) else f"{'—':>11}"
                for s in SEGS)
            print(f"{lab:<10}{c1:>10}{c2:>10}{c3:>11}{c4:>7}{c5:>8}{r['n']:>6}{segs_s}")

    print()
    print("=" * 118)
    print("判读（先定好，不许事后改）：")
    print("  1) 若 Q1（最小市值）并非最强 ⇒ 「收益来自小盘」这个说法**不成立**，")
    print("     那 §11.15 的 always-base 就该叫「全市场等权」，不能叫「小盘等权」；")
    print("  2) 若某组的**四个分段全为正** ⇒ 它才是可信的基准；只要有一段为负，就不许当基准；")
    print("  2b) ⚠️ **`t` 未修正重叠**：持 h 日的逐日滚动收益相邻样本共享 h−1 天 ⇒")
    print("      `t` 被系统性放大；**判读请以 `t_HAC`（Newey-West, lag=h−1）为准**（§11.28）。")
    print("  3) ⚠️ 先看 `daily` 是否含退市股（本脚本打印了覆盖区间）——")
    print("     **若不含退市股，则小市值组被系统性高估**，任何小盘结论都要打折扣；")
    print("  4) 本表数字将作为**永久对照基线**：今后任何选股/择时改动，")
    print("     必须在「β 之上还有稳定正超额」（逐段检验）才允许上线。")
    print(f"耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
