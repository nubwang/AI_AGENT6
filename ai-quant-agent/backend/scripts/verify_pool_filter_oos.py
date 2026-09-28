"""C2 —— 把 §11.11 的 +1.56pp 升级为**样本外**（plans/24 §11.32）

## 为什么这一步不能省（动机）

§11.11 的结论是：

    剔除 turnover∪bias20∪mom20 最高 1/3 → 相对候选池基线 **+1.563%**

但它有一个我当时**没有说透**的缺陷：那 7 个方案是我在**看完全样本结果之后**
才挑出「三因子并集」这一条的。这正是 §11.14 那条教训的推广：

    「方向不能用全样本定」  →  「**方案也不能用全样本定**」

所以本节做一次真正的样本外检验。

## 三层结构（哪一层能判定，先写清楚）

    PART 0  复现 §11.11 全样本数字（确认口径没漂，防「换口径换结论」）
    PART 1  **池内**样本外：前半段定方向 + 选方案 → 后半段只评估
            ⚠️ 必然功效不足：池内只有 ~22 个可评估交易日，切半 ≈ 11 天
    PART 2  **全市场层**样本外：把同一条规则搬到 ~660 天的全市场面板
            ✅ 这才是能判定的那一层（n ≈ 330 天，HAC(lag=4) 才有意义）
    PART 3  选择偏差量化：全样本最优方案，在前半 / 后半各自的改善

## ★ 一个必须先讲的方法学事实（本节才发现的）

    「样本外评估」与基准门槛的 **条件 d（四段全正）** 在**定义上互斥**：
    样本外窗口按时点切在后半段，**必然缺前面几段** ⇒ d 永远只能报
    `not_decidable`（见 `baseline_gate.coverage()`），而不是 `reject`。

    ⇒ 本层真正**能判定**的证据 = 条件 a（扣成本年化）+ **b / b_hac（两档 t）**；
      要连 d 一起判，必须改用**滚动前推（walk-forward）**，不是一次性 50/50 切分。
      （此项登记为 §11.32 的遗留项，本轮不实现。）

## 口径（与 §11.11 / §11.12 对齐）

    H = 5（T+1 开盘买入持 5 日）⇒ Newey-West lag = H − 1 = **4**
    扣双边 0.50%（`fwd5_net = fwd5 − 0.005`）；**逐日截面**；按日等权
    全市场层额外套 §11.12 的可交易性红线：`pct_chg < 9.5`（涨停买不到）、
    `close ≥ 2`（仙股）、`amount ≥ 1000`（流动性极差）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_pool_filter_oos.py
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

from app.backtest import baseline_gate as BG  # noqa: E402
from app.backtest import sentiment_gate as SG  # noqa: E402
from app.backtest.stats_correction import newey_west_t, plain_t  # noqa: E402
from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICK_DIR = os.path.join(BACKEND, "data", "daily_recommend")
OUT = os.path.join(BACKEND, "data", "pool_filter_oos.json")

H = 5
HAC_LAG = H - 1                   # 重叠修正的自由度：持有 5 日 ⇒ lag 4
MAX_ABS = 1.5
COST_RT = 0.005
POOL_LOOKBACK_FROM = "20260401"   # 与 §11.11 同口径（池子 20260808 起，因子需 ~60 天回溯）
MKT_FROM = "20231215"             # 全市场层：留 ~20 天回溯，使因子自 2024-01 起可用
FACTORS = ("mom20", "bias20", "turnover", "illiq")

# 7 个候选方案 —— 与 §11.11 C 组**同一批**（用于「前半段选方案」，防止我事后挑）
SCHEMES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("剔除 mom20 最高 1/3", "union", ("mom20",)),
    ("剔除 bias20 最高 1/3", "union", ("bias20",)),
    ("剔除 turnover 最高 1/3", "union", ("turnover",)),
    ("剔除 mom20∩bias20（交集）", "inter", ("mom20", "bias20")),
    ("剔除 turnover∪bias20（并集）", "union", ("turnover", "bias20")),
    ("剔除 turnover∪bias20∪mom20（并集）", "union", ("turnover", "bias20", "mom20")),
    ("只保留 illiq 最高 1/3", "keep", ("illiq",)),
)
# §11.11 自己选出的那一条（用于 PART 3 单独追踪）
SCHEME_11_11 = "剔除 turnover∪bias20∪mom20（并集）"


# ────────────────────────── 数据加载 ──────────────────────────

def load_picks() -> pd.DataFrame:
    """读候选池（同一日期取最新版本；agent 与普通榜单都计入，与 §11.11 一致）。"""
    files = sorted(glob.glob(os.path.join(PICK_DIR, "daily_*.json")))
    latest: dict[tuple[str, bool], str] = {}
    for p in files:
        m = re.match(r"daily_(\d{8})(_agent)?(_v\d+)?\.json$", os.path.basename(p))
        if m:
            latest[(m.group(1), bool(m.group(2)))] = p
    rows: list[dict] = []
    for (date, is_agent), p in sorted(latest.items()):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        for it in (d.get("top_picks") or []):
            rows.append({
                "date": str(d.get("date") or date).replace("-", "")[:8],
                "is_agent": is_agent,
                "ts_code": str(it.get("ts_code") or ""),
            })
    return pd.DataFrame(rows)


def build_factors(d0: str, tradable: bool) -> pd.DataFrame:
    """4 个候选因子的面板（**只用 ≤T 数据**，无未来函数）。

    tradable=True 时套 §11.12 的可交易性红线（涨停 / 仙股 / 极差流动性）。
    """
    with SessionLocal() as db:
        mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
    d1 = str(mx).replace("-", "")[:8] if mx else ""
    sql_d = ("SELECT trade_date, ts_code, close, amount, pct_chg FROM daily "
             "WHERE trade_date >= :a AND trade_date <= :b")
    sql_b = ("SELECT trade_date, ts_code, turnover_rate FROM daily_basic "
             "WHERE trade_date >= :a AND trade_date <= :b")
    parts_d: list[pd.DataFrame] = []
    parts_b: list[pd.DataFrame] = []
    with SessionLocal() as db:
        conn = db.connection()
        for a, b in SG.ST._year_spans(d0, d1):
            x = pd.read_sql(text(sql_d), conn, params={"a": a, "b": b})
            y = pd.read_sql(text(sql_b), conn, params={"a": a, "b": b})
            if x is not None and not x.empty:
                parts_d.append(x)
            if y is not None and not y.empty:
                parts_b.append(y)
    if not parts_d:
        return pd.DataFrame()
    df = pd.concat(parts_d, ignore_index=True)
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "", regex=False)
    df["ts_code"] = df["ts_code"].astype(str)
    for c in ("close", "amount", "pct_chg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if parts_b:
        b = pd.concat(parts_b, ignore_index=True)
        b["trade_date"] = b["trade_date"].astype(str).str.replace("-", "", regex=False)
        b["ts_code"] = b["ts_code"].astype(str)
        b["turnover_rate"] = pd.to_numeric(b["turnover_rate"], errors="coerce")
        df = df.merge(b[["trade_date", "ts_code", "turnover_rate"]],
                      on=["trade_date", "ts_code"], how="left")
    else:
        df["turnover_rate"] = np.nan
    if tradable:
        df = df[(df["close"] >= 2.0) & (df["amount"] >= 1000.0)
                & (df["pct_chg"].isna() | (df["pct_chg"] < 9.5))].copy()
    else:
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


def build_pool() -> pd.DataFrame:
    """候选池 + 因子 + fwd5（口径与 §11.11 完全一致）。"""
    picks = load_picks()
    if picks.empty:
        return pd.DataFrame()
    fac = build_factors(POOL_LOOKBACK_FROM, tradable=False)
    if fac.empty:
        return pd.DataFrame()
    d0, d1 = str(picks["date"].min()), str(picks["date"].max())
    fwd = SG.build_panel(SG.HORIZONS, d0, d1, keep_stock=True)
    col = f"fwd{H}"
    out = picks.merge(fwd[["trade_date", "ts_code", col]], how="left",
                      left_on=["date", "ts_code"], right_on=["trade_date", "ts_code"])
    out = out.rename(columns={col: "fwd5"})
    out["fwd5"] = pd.to_numeric(out["fwd5"], errors="coerce")
    out.loc[out["fwd5"].abs() > MAX_ABS, "fwd5"] = np.nan
    out["fwd5_net"] = out["fwd5"] - COST_RT
    out = out.merge(fac, how="left", on=["trade_date", "ts_code"])
    out["d"] = out["date"]
    return out.dropna(subset=["fwd5"]).reset_index(drop=True)


def build_market() -> pd.DataFrame:
    """全市场面板 + 因子 + fwd5（含可交易性过滤）。"""
    fac = build_factors(MKT_FROM, tradable=True)
    if fac.empty:
        return pd.DataFrame()
    d0, d1 = str(fac["trade_date"].min()), str(fac["trade_date"].max())
    fwd = SG.build_panel(SG.HORIZONS, d0, d1, keep_stock=True)
    fwd = fwd.rename(columns={f"fwd{H}": "fwd5"})[["trade_date", "ts_code", "fwd5"]]
    m = fac.merge(fwd, on=["trade_date", "ts_code"], how="inner")
    m["fwd5"] = pd.to_numeric(m["fwd5"], errors="coerce")
    m.loc[m["fwd5"].abs() > MAX_ABS, "fwd5"] = np.nan
    m["fwd5_net"] = m["fwd5"] - COST_RT
    m["d"] = m["trade_date"]
    return m.dropna(subset=["fwd5"]).reset_index(drop=True)


# ────────────────────────── 方案 / 统计 ──────────────────────────

def _th(df: pd.DataFrame, col: str) -> pd.Series:
    """逐日 2/3 分位（截面）。"""
    return df.groupby("d")[col].transform(lambda s: s.quantile(2.0 / 3.0))


def scheme_mask(df: pd.DataFrame, kind: str, cols: tuple[str, ...]) -> pd.Series:
    """返回「应剔除」的布尔掩码（True = 剔除）。

    NaN 一律**不剔除**（与 §11.11 的 `.fillna(False)` 一致）；`keep` 方案例外：
    低于 2/3 分位（含 NaN）都剔除 —— 因为它的语义就是「只留最高 1/3」。
    """
    if kind == "keep":
        th = _th(df, cols[0])
        return (df[cols[0]] < th).fillna(True)
    if kind == "union":
        m = pd.Series(False, index=df.index)
        for c in cols:
            m = m | (df[c] >= _th(df, c)).fillna(False)
        return m
    m = pd.Series(True, index=df.index)          # inter
    for c in cols:
        m = m & (df[c] >= _th(df, c)).fillna(False)
    return m


def daily_mean(df: pd.DataFrame, col: str = "fwd5_net") -> pd.Series:
    return df.groupby("d")[col].mean()


def diff_series(base: pd.Series, sub: pd.Series) -> pd.Series:
    """配对（只在**双方都有值的同一天**比较）后的日差序列。"""
    j = pd.concat([base.rename("b"), sub.rename("k")], axis=1).dropna()
    return j["k"] - j["b"]


def stats(s: pd.Series) -> dict:
    x = [float(v) for v in pd.to_numeric(s, errors="coerce").dropna()]
    if len(x) < 3:
        return {"n": len(x), "mean": float("nan"), "t": float("nan"),
                "t_hac": float("nan"), "sd": float("nan")}
    return {"n": len(x), "mean": float(np.mean(x)), "sd": float(np.std(x, ddof=1)),
            "t": float(plain_t(x)),
            "t_hac": (float(newey_west_t(x, HAC_LAG)) if len(x) >= 30 else float("nan"))}


def factor_dirs(df: pd.DataFrame) -> dict:
    """逐因子：高档 − 低档 的日等权 spread（用于**只用前半段定方向**）。"""
    out: dict[str, dict] = {}
    for f in FACTORS:
        d = df.dropna(subset=[f, "fwd5_net"]).copy()
        d["_q"] = d.groupby("d")[f].transform(
            lambda s: pd.qcut(s.rank(method="first"), 3, labels=False)
            if len(s) >= 6 else np.nan)
        d = d.dropna(subset=["_q"])
        sp = pd.Series(dtype="float64")
        if not d.empty:
            hi = d[d["_q"] == 2].groupby("d")["fwd5_net"].mean()
            lo = d[d["_q"] == 0].groupby("d")["fwd5_net"].mean()
            sp = (hi - lo).dropna()
        m = float(sp.mean()) if len(sp) else float("nan")
        out[f] = {"n_days": int(len(sp)), "spread": m,
                  "dir": ("反向(剔高)" if m < 0 else "正向(留高)") if len(sp) else "不可判定"}
    return out


def eval_schemes(base: pd.Series, df: pd.DataFrame) -> list[dict]:
    """在给定期上，对 7 个方案各算「改善 = 子集日等权 − 基线日等权」。"""
    rows: list[dict] = []
    for nm, kind, cols in SCHEMES:
        kept = df[~scheme_mask(df, kind, cols)]
        st = stats(diff_series(base, daily_mean(kept)))
        rows.append({"scheme": nm, "kind": kind, "cols": list(cols),
                     "n_kept": int(len(kept)), **st})
    return rows


def _bucket(dt: str) -> str:
    y, md = str(dt)[:4], str(dt)[4:6]
    if y == "2024":
        return "2024"
    if y == "2025":
        return "2025H1" if md < "07" else "2025H2"
    return y if y >= "2026" else y


def seg_of(daily: pd.Series) -> dict:
    acc: dict[str, list[float]] = {}
    for dt, v in daily.items():
        if v == v:                      # 非 NaN
            acc.setdefault(_bucket(str(dt)), []).append(float(v))
    return {k: float(np.mean(v)) for k, v in sorted(acc.items())}


def judge_oos(name: str, daily: pd.Series, base_daily: pd.Series | None = None) -> dict:
    """**样本外口径**判定：a / b / b_hac 可判；d / c **按 `coverage()` 报 not_decidable**。

    ⚠️ **口径关键点（第一版我自己就写错了，必须记在代码里）**：
       传入的 `daily` 已经是 `fwd5_net = fwd5 − COST_RT`，**成本已经扣过**。
       所以这里必须用 `fee=0, slip_bp=0`；若用 `judge_default()`（0.5% + 40bp），
       成本会被**再扣一次**，把市场层「判定 A」的 `a` 从 ✔ 误判成 ✘
       （实测：−40.7%/年 vs 正确的 +4.6%/年）。

    （另外不要用 `baseline_gate.judge()` 的 `d`/`c` 去判样本外序列 —— 它会把「缺段」
      与「段为负」**混为一谈**，正是 `coverage()` 当初被加进来要解决的问题。）
    """
    st = stats(daily)
    cand = BG.Candidate(name=name, gross=float(st["mean"]), t=float(st["t"]),
                        t_hac=float(st["t_hac"]), per_year=252.0 / H,
                        by_seg=seg_of(daily), n=int(st["n"]))
    j = BG.judge(cand, 0.0, None, 0.0)               # ★ 序列已扣成本 ⇒ 不重复扣
    cov = BG.coverage(cand)
    j["coverage"] = cov
    if cov["missing"]:
        j["verdict"] = "not_decidable"
        j["reasons"] = [r for r in j["reasons"] if not r.startswith(("c 不过", "d 不过"))]
        j["reasons"].append(
            f"d 不可判定：评估窗只覆盖 {cov['n_have']}/{cov['n_total']} 段"
            f"（缺 {'、'.join(cov['missing'])}）—— 样本外窗口与「四段全正」定义上互斥")
    if base_daily is not None:
        j["excess_vs_base"] = float(st["mean"]) - float(stats(base_daily)["mean"])
    return j


# ────────────────────────── 打印 ──────────────────────────

def _pct(v, nd: int = 3) -> str:
    try:
        f = float(v)
        return "—" if f != f else f"{f:+.{nd}%}"
    except Exception:  # noqa: BLE001
        return "—"


def _num(v, nd: int = 2) -> str:
    try:
        f = float(v)
        return "—" if f != f else f"{f:+.{nd}f}"
    except Exception:  # noqa: BLE001
        return "—"


def _scheme_table(rows: list[dict], mark: str = "") -> None:
    h = (f"  {'方案':<38}{'剩余n':>7}{'改善':>11}{'t':>8}{'t_HAC':>8}{'天数':>6}")
    print(h)
    print("  " + "-" * (len(h) - 2))
    for r in rows:
        flag = " ★" if (mark and r["scheme"] == mark) else ""
        print(f"  {r['scheme'][:36]:<38}{r['n_kept']:>7}{_pct(r['mean']):>11}"
              f"{_num(r['t']):>8}{_num(r['t_hac']):>8}{r['n']:>6}{flag}")


def _dir_table(dirs: dict) -> None:
    h = f"  {'因子':<10}{'高档−低档':>12}{'天数':>7}  方向"
    print(h)
    print("  " + "-" * (len(h) - 2))
    for f, v in dirs.items():
        print(f"  {f:<10}{_pct(v['spread']):>12}{v['n_days']:>7}  {v['dir']}")


def _verdict_line(j: dict) -> None:
    m = lambda x: "✔" if x is True else ("—" if x is None else "✘")  # noqa: E731
    # ★ 序列本身已扣成本（`fwd5_net`）⇒ 判定用 fee=0，故此处的年化**就是**「已扣成本的年化」
    print(f"    a(已扣成本年化 {_pct(j['net'], 1)})={m(j['a'])}  "
          f"b(naive t={_num(j['t'])})={m(j['b'])}  "
          f"b_hac(HAC t={_num(j['t_hac'])})={m(j['b_hac'])}  "
          f"d={m(j['d'])} ← {j['coverage']['n_have']}/{j['coverage']['n_total']} 段")
    print(f"    verdict = {j['verdict']}")
    for r in j.get("reasons", []):
        print(f"      · {r}")


# ────────────────────────── 主体 ──────────────────────────

def run_layer(tag: str, df: pd.DataFrame, out: dict, frac: float = 0.5) -> None:
    """对一个数据层跑 PART 1/2/3（同一套逻辑，池内与全市场共用）。"""
    print()
    print("=" * 104)
    print(f"【{tag}】n={len(df)} 条 / {df['d'].nunique()} 个可评估交易日")
    print("=" * 104)

    dates = sorted(df["d"].unique())
    k = max(1, int(len(dates) * frac))
    d_a, d_b = dates[:k], dates[k:]
    fa = df[df["d"].isin(d_a)]
    fb = df[df["d"].isin(d_b)]
    print(f"  切分：前半 {len(d_a)} 天（{d_a[0]}~{d_a[-1]}）"
          f"｜后半 {len(d_b)} 天（{d_b[0]}~{d_b[-1]}）")

    # ── PART 0：全样本复现（口径核对）──
    base_all = daily_mean(df)
    all_rows = eval_schemes(base_all, df)
    print()
    print(f"  PART 0 · 全样本（{len(dates)} 天）—— 复现 §11.11，确认口径没漂")
    print(f"    基线（本层全样本）：毛 {_pct(daily_mean(df, 'fwd5').mean())}"
          f"｜净 {_pct(base_all.mean())}（双边 {COST_RT:.2%}）")
    if len(dates) < 60:
        print(f"    ⚠ 本层只有 {len(dates)} 天 ⇒ 项目统计模块的硬门槛是 **n ≥ 30**"
              f"（见 `plain_t` / `newey_west_t`），切半后必然**不报 t**（表里显示 —）。"
              f"这不是算错，是「样本不够就不给数」。")
    _scheme_table(all_rows, SCHEME_11_11)
    all_best = max(all_rows, key=lambda r: (r["mean"] if r["mean"] == r["mean"] else -9))
    print(f"    全样本最优 = {all_best['scheme']}（{_pct(all_best['mean'])}）")

    # ── PART 1/2：前半定方向 + 选方案 → 后半只评估 ──
    print()
    print("  【前半段】定方向（4 因子高档−低档）")
    dirs_a = factor_dirs(fa)
    _dir_table(dirs_a)

    base_a = daily_mean(fa)
    rows_a = eval_schemes(base_a, fa)
    print(f"  【前半段】7 方案改善（用于**选方案**，不看后半）")
    _scheme_table(rows_a)
    picked = max(rows_a, key=lambda r: (r["mean"] if r["mean"] == r["mean"] else -9))
    print(f"    → 前半段选出的方案 = **{picked['scheme']}**（{_pct(picked['mean'])}，"
          f"t={_num(picked['t'])}，t_HAC={_num(picked['t_hac'])}）")

    print()
    print(f"  【后半段】**只评估**前半段选出的那一条（这是真正的样本外）")
    base_b = daily_mean(fb)
    rows_b = eval_schemes(base_b, fb)
    _scheme_table(rows_b, picked["scheme"])
    pit = next((r for r in rows_b if r["scheme"] == picked["scheme"]), None)
    if pit:
        print(f"    样本外结果：{picked['scheme']} → 改善 {_pct(pit['mean'])}"
              f"（t={_num(pit['t'])}，t_HAC={_num(pit['t_hac'])}，{pit['n']} 天）")
    # §11.11 那条，在后半段还剩多少
    ref = next((r for r in rows_b if r["scheme"] == SCHEME_11_11), None)
    if ref:
        print(f"    对照：§11.11 自己那条（{SCHEME_11_11}）在后半段 = {_pct(ref['mean'])}"
              f"（t={_num(ref['t'])}，t_HAC={_num(ref['t_hac'])}）")

    # 后半段：判定（超额序列）
    kind, cols = next((s[1], s[2]) for s in SCHEMES if s[0] == picked["scheme"])
    kept_b = fb[~scheme_mask(fb, kind, cols)]
    imp_b = diff_series(base_b, daily_mean(kept_b))
    j_oos = judge_oos(f"{tag}·样本外超额({picked['scheme']})", imp_b)

    # 后半段：被选方案自身（绝对收益）也判一次
    j_kept = judge_oos(f"{tag}·样本外组合({picked['scheme']})", daily_mean(kept_b), base_b)

    print()
    print("  判定 A —— 样本外**超额**序列（能否稳定跑赢本层基线）：")
    _verdict_line(j_oos)
    print("  判定 B —— 样本外**组合自身**（绝对收益，对照本层基线）：")
    _verdict_line(j_kept)

    # ── PART 3：选择偏差量化 ──
    print()
    print("  PART 3 · 选择偏差量化（全样本最优方案，在前/后半各自的改善）")
    h = f"    {'方案':<38}{'前半':>11}{'后半':>11}{'全样本':>11}"
    print(h)
    print("    " + "-" * (len(h) - 4))
    seen: list[str] = []
    for nm in (all_best["scheme"], SCHEME_11_11):
        if nm in seen:                      # 两者相同（池内就是这样）时只打一行
            continue
        seen.append(nm)
        ra = next((r for r in rows_a if r["scheme"] == nm), None)
        rb = next((r for r in rows_b if r["scheme"] == nm), None)
        r0 = next((r for r in all_rows if r["scheme"] == nm), None)
        print(f"    {nm[:36]:<38}{_pct(ra['mean']) if ra else '—':>11}"
              f"{_pct(rb['mean']) if rb else '—':>11}{_pct(r0['mean']) if r0 else '—':>11}")
    print("    判读：若「全样本」明显优于「前半」与「后半」两者 → "
          "**全样本那个数是被挑出来的**，不是可持续的规则。")

    out[tag] = {
        "n_records": int(len(df)), "n_days": int(len(dates)),
        "split": {"half_a": [d_a[0], d_a[-1]], "half_b": [d_b[0], d_b[-1]],
                  "n_a": len(d_a), "n_b": len(d_b)},
        "full_sample": {"base_net": float(base_all.mean()), "schemes": all_rows,
                        "best": all_best["scheme"]},
        "dir_first_half": dirs_a,
        "first_half_rows": rows_a,
        "picked_scheme": picked["scheme"],
        "second_half_rows": rows_b,
        "oos_excess": {**pit} if pit else {},
        "verdict_oos_excess": j_oos,
        "verdict_oos_kept": j_kept,
        "selection_bias": {nm: {"a": next((r["mean"] for r in rows_a if r["scheme"] == nm), None),
                                "b": next((r["mean"] for r in rows_b if r["scheme"] == nm), None),
                                "all": next((r["mean"] for r in all_rows if r["scheme"] == nm), None)}
                           for nm in {all_best["scheme"], SCHEME_11_11}},
    }


def main() -> int:
    t0 = time.time()
    print("=" * 104)
    print("C2 —— §11.11 的 +1.56pp 到底能不能样本外复现？（plans/24 §11.32）")
    print("=" * 104)
    print(f"  H={H} ⇒ HAC lag={HAC_LAG}｜扣双边 {COST_RT:.2%}｜逐日截面 + 按日等权")

    out: dict = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "h": H, "hac_lag": HAC_LAG, "cost_rt": COST_RT,
                 "schemes": [s[0] for s in SCHEMES],
                 "scheme_11_11": SCHEME_11_11}

    print()
    print("  加载候选池 ...")
    pool = build_pool()
    if pool.empty:
        print("  ✘ 候选池为空")
        return 1
    run_layer("池内", pool, out)

    print()
    print(f"  加载全市场面板（{MKT_FROM} 起，含可交易性过滤）... 这步较慢")
    mkt = build_market()
    if mkt.empty:
        print("  ✘ 全市场面板为空")
        return 1
    run_layer("全市场", mkt, out)

    try:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, default=str)
        print()
        print(f"  已落盘：{os.path.relpath(OUT, os.path.dirname(BACKEND))}")
    except Exception as e:  # noqa: BLE001
        print(f"  ! 落盘失败：{e}")

    print()
    print("=" * 104)
    print("判读（三条，写进 §11.32）：")
    print("  1) **池内样本外不可判定**：只有 ~22 个可评估交易日，切半 ≈ 11 天，")
    print("     HAC(lag=4) 连 30 个样本都凑不满 ⇒ 它只报 nan，不报数（诚实）。")
    print("  2) **全市场层才是能判定的那一层**：看样本外那一行的 t 与 t_HAC。")
    print("  3) **样本外与「四段全正」定义上互斥** ⇒ d 一律 not_decidable；")
    print("     要连 d 一起判必须换 **滚动前推（walk-forward）**（登记为遗留项）。")
    print(f"  用时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
