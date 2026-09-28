"""F3 —— 滚动前推（walk-forward）验证（plans/24 §11.32.6 遗留项）

## 为什么必须做这一步

§11.32 用**一次性 50/50 切分**做样本外，并发现一个**结构性**问题：

    「样本外评估」与基准门槛的 **条件 d（四段全正）在定义上互斥**
    —— 评估窗按时间切在后半段，**必然缺前面几段** ⇒ `d` 永远只能报 not_decidable。

而且一次性切分只给出 **一个** 样本外点估计，**极易被"哪一天切"左右**。
本节改用**滚动前推（walk-forward）**：把区间切成 K 段，逐段用「**该段之前**的全部数据」
定方案，再在**该段**上评估 —— 得到 **K−1 个独立的样本外点**，
并且**每段都能回答"当时的四段覆盖情况"**。

## 口径（与 §11.32 完全一致，保证可比）

    样本 = 全市场面板（含 §11.12 可交易性过滤）；H = 5 ⇒ HAC lag = 4
    扣双边 0.50%；逐日截面 + 按日等权；方案集合 = §11.11 C 组那 7 条
    「方案」= 剔除某因子最高 1/3（或只保留 illiq 最高 1/3）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_walk_forward.py [K]
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.backtest import baseline_gate as BG  # noqa: E402
from app.backtest import sentiment_gate as SG  # noqa: E402
from app.backtest.stats_correction import newey_west_t, plain_t  # noqa: E402
from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BACKEND, "data", "walk_forward.json")

H = 5
HAC_LAG = H - 1
MAX_ABS = 1.5
COST_RT = 0.005
MKT_FROM = "20231215"
FACTORS = ("mom20", "bias20", "turnover", "illiq")
SCHEMES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("剔 mom20 高1/3", "union", ("mom20",)),
    ("剔 bias20 高1/3", "union", ("bias20",)),
    ("剔 turnover 高1/3", "union", ("turnover",)),
    ("剔 mom20∩bias20", "inter", ("mom20", "bias20")),
    ("剔 turnover∪bias20", "union", ("turnover", "bias20")),
    ("剔 turnover∪bias20∪mom20", "union", ("turnover", "bias20", "mom20")),
    ("只留 illiq 高1/3", "keep", ("illiq",)),
)


def build_market() -> pd.DataFrame:
    """全市场面板 + 因子 + fwd5（**与 §11.32 的 build_market 同一口径**）。"""
    with SessionLocal() as db:
        mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
    d1 = str(mx).replace("-", "")[:8] if mx else ""
    sql_d = ("SELECT trade_date, ts_code, close, amount, pct_chg FROM daily "
             "WHERE trade_date >= :a AND trade_date <= :b")
    sql_b = ("SELECT trade_date, ts_code, turnover_rate FROM daily_basic "
             "WHERE trade_date >= :a AND trade_date <= :b")
    pd_parts: list[pd.DataFrame] = []
    pb_parts: list[pd.DataFrame] = []
    with SessionLocal() as db:
        conn = db.connection()
        for a, b in SG.ST._year_spans(MKT_FROM, d1):
            x = pd.read_sql(text(sql_d), conn, params={"a": a, "b": b})
            y = pd.read_sql(text(sql_b), conn, params={"a": a, "b": b})
            if x is not None and not x.empty:
                pd_parts.append(x)
            if y is not None and not y.empty:
                pb_parts.append(y)
    if not pd_parts:
        return pd.DataFrame()
    df = pd.concat(pd_parts, ignore_index=True)
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "", regex=False)
    df["ts_code"] = df["ts_code"].astype(str)
    for c in ("close", "amount", "pct_chg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if pb_parts:
        b = pd.concat(pb_parts, ignore_index=True)
        b["trade_date"] = b["trade_date"].astype(str).str.replace("-", "", regex=False)
        b["ts_code"] = b["ts_code"].astype(str)
        b["turnover_rate"] = pd.to_numeric(b["turnover_rate"], errors="coerce")
        df = df.merge(b[["trade_date", "ts_code", "turnover_rate"]],
                      on=["trade_date", "ts_code"], how="left")
    else:
        df["turnover_rate"] = np.nan
    df = df[(df["close"] >= 2.0) & (df["amount"] >= 1000.0)
            & (df["pct_chg"].isna() | (df["pct_chg"] < 9.5))].copy()
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
    fac = df[["trade_date", "ts_code"] + list(FACTORS)].copy()
    for f in FACTORS:
        fac[f] = pd.to_numeric(fac[f], errors="coerce").astype("float64")
    fwd = SG.build_panel(SG.HORIZONS, str(fac["trade_date"].min()), d1, keep_stock=True)
    fwd = fwd.rename(columns={f"fwd{H}": "fwd5"})[["trade_date", "ts_code", "fwd5"]]
    m = fac.merge(fwd, on=["trade_date", "ts_code"], how="inner")
    m["fwd5"] = pd.to_numeric(m["fwd5"], errors="coerce")
    m.loc[m["fwd5"].abs() > MAX_ABS, "fwd5"] = np.nan
    m["fwd5_net"] = m["fwd5"] - COST_RT
    m["d"] = m["trade_date"]
    return m.dropna(subset=["fwd5"]).reset_index(drop=True)


def _th(df: pd.DataFrame, col: str) -> pd.Series:
    return df.groupby("d")[col].transform(lambda s: s.quantile(2.0 / 3.0))


def mask(df: pd.DataFrame, kind: str, cols: tuple[str, ...]) -> pd.Series:
    if kind == "keep":
        return (df[cols[0]] < _th(df, cols[0])).fillna(True)
    if kind == "union":
        m = pd.Series(False, index=df.index)
        for c in cols:
            m = m | (df[c] >= _th(df, c)).fillna(False)
        return m
    m = pd.Series(True, index=df.index)
    for c in cols:
        m = m & (df[c] >= _th(df, c)).fillna(False)
    return m


def stats(s: pd.Series) -> dict:
    x = [float(v) for v in pd.to_numeric(s, errors="coerce").dropna()]
    if len(x) < 3:
        return {"n": len(x), "mean": float("nan"), "t": float("nan"), "t_hac": float("nan")}
    return {"n": len(x), "mean": float(np.mean(x)),
            "t": (float(plain_t(x)) if len(x) >= 30 else float("nan")),
            "t_hac": (float(newey_west_t(x, HAC_LAG)) if len(x) >= 30 else float("nan"))}


def _bucket(dt: str) -> str:
    y, md = str(dt)[:4], str(dt)[4:6]
    if y == "2024":
        return "2024"
    if y == "2025":
        return "2025H1" if md < "07" else "2025H2"
    return y if y >= "2026" else y


def coverage_of(daily: pd.Series, ) -> dict:
    """评估窗覆盖了 SEGMENTS 中的哪几段（用来回答 d 是否可判）。"""
    have = sorted({_bucket(str(i)) for i in daily.index})
    miss = [s for s in BG.SEGMENTS if s not in have]
    return {"have": have, "missing": miss, "decidable_d": not miss}


def seg_of(daily: pd.Series) -> dict[str, float]:
    """把日序列按 SEGMENTS 分段聚合成 `by_seg`，供 `baseline_gate.judge` 判 d / c。

    缺段的情形**如实不填**（不伪造）⇒ `judge` 会把 d 记为 False（与 §11.21.6「不可判定 ≠ 判定为差」
    的护栏一致：调用方应先看 `coverage.decidable_d` 再解读 d）。
    """
    out: dict[str, float] = {}
    for k, g in daily.groupby(lambda i: _bucket(str(i))):
        out[str(k)] = float(g.mean())
    return out


def improv(base: pd.Series, sub: pd.Series) -> pd.Series:
    j = pd.concat([base.rename("b"), sub.rename("s")], axis=1).dropna()
    return j["s"] - j["b"]


def pick_best(train: pd.DataFrame) -> tuple[str, dict]:
    """在训练集上按「改善均值」挑方案（**只看训练集**）。"""
    base = train.groupby("d")["fwd5_net"].mean()
    best, rec = SCHEMES[0][0], {}
    bv = -1e9
    for nm, kind, cols in SCHEMES:
        kept = train[~mask(train, kind, cols)]
        st = stats(improv(base, kept.groupby("d")["fwd5_net"].mean()))
        rec[nm] = st
        if st["mean"] == st["mean"] and st["mean"] > bv:
            bv, best = st["mean"], nm
    return best, rec


def _pct(v, nd=3) -> str:
    try:
        f = float(v)
        return "—" if f != f else f"{f:+.{nd}%}"
    except Exception:  # noqa: BLE001
        return "—"


def _num(v, nd=2) -> str:
    try:
        f = float(v)
        return "—" if f != f else f"{f:+.{nd}f}"
    except Exception:  # noqa: BLE001
        return "—"


def main() -> int:
    t0 = time.time()
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    print("=" * 108)
    print(f"F3 —— 滚动前推（walk-forward, K={k}）（plans/24 §11.32.6 遗留项）")
    print("=" * 108)
    print(f"  H={H} ⇒ HAC lag={HAC_LAG}｜扣双边 {COST_RT:.2%}｜方案集合 {len(SCHEMES)} 条")

    print("  加载全市场面板 ...（较慢）")
    m = build_market()
    if m.empty:
        print("  ✘ 面板为空")
        return 1
    dates = sorted(m["d"].unique())
    n = len(dates)
    print(f"  样本 {len(m)} 条 / {n} 个交易日（{dates[0]}~{dates[-1]}）")

    # 折点：前 40% 作首段训练，其余均分（保证每折训练集越来越大）
    starts = [int(n * (0.4 + 0.6 * i / k)) for i in range(k)]
    folds: list[dict] = []
    print()
    h = (f"  {'折':<4}{'训练区间':<21}{'评估区间':<21}{'训练天数':>8}{'评估天数':>8}"
         f"{'选中的方案':<24}{'改善':>10}{'t':>8}{'t_HAC':>8}{'四段可判':>9}{'豁免d可过':>10}")
    print(h)
    print("  " + "-" * (len(h) - 2))
    for i in range(k):
        a0, a1 = starts[i], (starts[i + 1] if i + 1 < k else n)
        if a1 - a0 < 5 or a0 < 20:
            continue
        train = m[m["d"].isin(dates[:a0])]
        test = m[m["d"].isin(dates[a0:a1])]
        if train.empty or test.empty:
            continue
        nm, _rec = pick_best(train)
        kind, cols = next((s[1], s[2]) for s in SCHEMES if s[0] == nm)
        base_te = test.groupby("d")["fwd5_net"].mean()
        kept_te = test[~mask(test, kind, cols)]
        imp = improv(base_te, kept_te.groupby("d")["fwd5_net"].mean())
        st = stats(imp)
        cov = coverage_of(base_te)
        # ★ item 4 真正落地（plans/24 §11.36.10）：把每折结果**过一遍统一门槛**，
        #   从而量化"改用 a ∧ b ∧ b_hac 后，究竟有几折真能过"。
        #   ⚠️ `imp` 序列**已扣成本**（面板里 `fwd5_net = fwd5 − 0.5%`）⇒ 判定必须传 fee=0/slip=0，
        #   **不可**用 `judge_default()`（那会**重复扣成本**——§11.32.7 踩过同一个坑）。
        cand = BG.Candidate(name=nm, gross=float(st["mean"]), t=float(st["t"]),
                            t_hac=float(st["t_hac"]), per_year=252.0 / H,
                            by_seg=seg_of(imp), n=int(st["n"]))
        j = BG.judge(cand, 0.0, None, 0.0)
        usable_nod = bool(j["a"] and j["b"] and j["b_hac"])   # 豁免 d 后的判定（§11.36.6）
        folds.append({"fold": i + 1, "train": [dates[0], dates[a0 - 1]],
                      "test": [dates[a0], dates[a1 - 1]], "picked": nm,
                      **st, "coverage": cov, "judgment": j,
                      "usable_default_d": j["verdict"] == "usable",
                      "usable_nod": usable_nod})
        print(f"  {i + 1:<4}{dates[0]}~{dates[a0 - 1]:<11}{dates[a0]}~{dates[a1 - 1]:<11}"
              f"{a0:>8}{a1 - a0:>8}{nm[:22]:<24}{_pct(st['mean']):>10}"
              f"{_num(st['t']):>8}{_num(st['t_hac']):>8}"
              f"{('是' if cov['decidable_d'] else '否'):>9}"
              f"{('✔' if usable_nod else '✘'):>10}")

    if not folds:
        print("  ✘ 没有可评估的折")
        return 1

    mm = [f["mean"] for f in folds if f["mean"] == f["mean"]]
    ha = [f["t_hac"] for f in folds if f["t_hac"] == f["t_hac"]]
    n_pos = sum(1 for v in mm if v > 0)
    n_sig = sum(1 for v in ha if v > 2)
    n_dec = sum(1 for f in folds if f["coverage"]["decidable_d"])
    print()
    print("=" * 108)
    print(f"  **汇总（{len(folds)} 折独立样本外）**")
    print(f"    改善均值为正：{n_pos} / {len(mm)} 折"
          f"（均值 {_pct(np.mean(mm)) if mm else '—'}）")
    print(f"    HAC t > 2    ：{n_sig} / {len(ha)} 折"
          f"（均值 {_num(np.mean(ha)) if ha else '—'}）")
    print(f"    **四段（d）可判的折数：{n_dec} / {len(folds)}**"
          f"  ← 直接回答 §11.32.6：滚动前推**能否**让 d 可判")
    n_default = sum(1 for f in folds if f.get("usable_default_d"))
    n_nod = sum(1 for f in folds if f.get("usable_nod"))
    print(f"    **默认门槛（要求 d）：{n_default} / {len(folds)} 折 usable**"
          f"  ← d 恒不可判 ⇒ 预期为 0（结构性，不是策略差）")
    print(f"    **豁免 d 后（a ∧ b ∧ b_hac）：{n_nod} / {len(folds)} 折 usable**"
          f"  ← ★ §11.36.6 结论的量化：改用 a∧b∧b_hac 后**真能过**的折数")
    print()
    print("  判读：")
    print("  1) 若「改善为正的折数」远小于总折数 ⇒ 一次性 50/50 的正结论**不可推广**；")
    print("  2) 若每折选中的方案都不同 ⇒ 说明『选方案』本身极不稳定（§11.32 的担忧成立）；")
    print("  3) 若 d 仍不可判 ⇒ 说明**即使 walk-forward 也躲不开**『四段全正』的定义性冲突，")
    print("     必须改用**按段滚动**（每段都要求 4 段历史）或直接放弃 d 条件，改用 a/b/b_hac。")
    print(f"  用时 {time.time() - t0:.1f}s")

    try:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump({"k": k, "h": H, "hac_lag": HAC_LAG, "folds": folds,
                       "summary": {"n_pos": n_pos, "n_folds": len(folds),
                                   "n_hac_sig": n_sig, "n_decidable_d": n_dec,
                                   "n_usable_default_d": n_default, "n_usable_nod": n_nod}},
                      f, ensure_ascii=False, indent=1, default=str)
        print(f"  已落盘：{os.path.relpath(OUT, os.path.dirname(BACKEND))}")
    except Exception as e:  # noqa: BLE001
        print(f"  ! 落盘失败：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
