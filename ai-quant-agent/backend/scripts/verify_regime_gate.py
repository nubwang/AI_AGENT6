"""regime 门验证（plans/24 §11.13.7 第 1 条，当前最高优先级）

## 为什么这是下一步

§11.13 用年度/月度分解证明了：

    - "拉长持有期"这条出路**不可用**（全区间 +27.4%/年，但 2026 整年全负）；
    - 2026-01~06 **连续 6 个月** 因子超额为负（04 月单月 −12.1pp）→ **regime 切换**；
    - 连"全市场等权"基准在 2026 都是负的 ⇒ 不是因子独有的病，是**环境问题**。

所以真正的问题变成：

    **能不能事前判断"当前是否适合做多小盘反转"？**

## 两类候选 regime 特征（都只用 T 日收盘前可知的信息）

    A 市场自身（从日线面板算）
      mkt_mom20 / mkt_mom60   全市场等权 20/60 日动量
      vol20                   全市场等权日收益 20 日波动
      xsr                     横截面离散度 / 其 20 日均值（>1 = 主题行情放大）
      xs_disp                 横截面收益标准差
      ud_ratio                当日上涨家数 / 下跌家数
    B 人心/情绪层（plans/24 §八 的 659 日序列，经 `sentiment_asof` 取"开盘前可用"的那一行）
      s_lb2 / s_zha / s_effect / s_seal / s_ud

## ★ 两条必须遵守的口径红线

    1. **分档阈值用「滚动分位」（rolling 252 日 30%/70%），绝不用全样本分位** ——
       全样本分位 = 未来函数（§11.8 就是被这个坑过：Q5 从 −1.629% 被说成 +0.636%）；
       代价是**前 60 天无法分档**（诚实损失样本）；
    2. 所有检验（特征 × 组）一律过 **BH-FDR**；`excess = combo − base` 是**同一天内**的差，
       已消掉市场 beta。

## 判读标准（事先定好，不许事后改）

    - 若某特征 high/low 两组超额**反向且 FDR 后显著** → 有资格做门；
    - 再做"门策略"回测：ON 持 4 因子组合、OFF 持全市场等权，**扣双边 0.5%**；
    - 最后**逐年分解**（§11.13 的教训）：只有"每年都不差于 always-combo"才算通过；
    - 若门只是在"事后避开 2026H1" → 看 ON 天数按年的分布就能拆穿。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_regime_gate.py            # 重建+分析
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_regime_gate.py --from-cache  # 只重跑分析
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

import verify_factor_portfolio as VFP  # noqa: E402
from app.backtest import sentiment_gate as SG  # noqa: E402
from app.backtest.sentiment import sentiment_asof  # noqa: E402
from app.backtest.validator import _t_sf  # noqa: E402
from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BACKEND, "data", "regime_gate_daily.json")
N = 30
REBAL = 50.4              # 一年换仓次数（252/5）
COST_RT = 0.005
MAX_ABS = 1.5
D0 = "20240101"
ROLL_W = 252
ROLL_MIN = 60
LO_Q, HI_Q = 0.30, 0.70

FEATS = ("mkt_mom20", "mkt_mom60", "vol20", "xsr", "xs_disp", "ud_ratio",
         "s_lb2", "s_zha", "s_effect", "s_seal", "s_ud")
LABEL = {
    "mkt_mom20": "全市场 20 日动量",
    "mkt_mom60": "全市场 60 日动量",
    "vol20": "市场 20 日波动",
    "xsr": "离散度/近20日均值",
    "xs_disp": "横截面收益标准差",
    "ud_ratio": "上涨/下跌家数比",
    "s_lb2": "情绪·连板家数",
    "s_zha": "情绪·炸板率",
    "s_effect": "情绪·赚钱效应",
    "s_seal": "情绪·封板率",
    "s_ud": "情绪·涨跌家数比",
}


# ---------------- 统计小工具 ----------------

def _t1(s: pd.Series) -> tuple[float, int, float]:
    """单样本 t 检验（H0: 均值 = 0）→ (均值, 样本数, 双侧 p)。"""
    x = s.dropna().to_numpy(dtype="float64")
    n = int(x.size)
    if n < 10:
        return (float("nan"), n, float("nan"))
    m = float(x.mean())
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return (m, n, float("nan"))
    t = m / (sd / math.sqrt(n))
    return (m, n, float(_t_sf(t, n - 1)))


def _welch(a: pd.Series, b: pd.Series) -> tuple[float, float]:
    """Welch 两样本 t 检验 → (t, 双侧 p)。"""
    x = a.dropna().to_numpy(dtype="float64")
    y = b.dropna().to_numpy(dtype="float64")
    if x.size < 10 or y.size < 10:
        return (float("nan"), float("nan"))
    v1 = float(x.var(ddof=1)) / x.size
    v2 = float(y.var(ddof=1)) / y.size
    if v1 + v2 <= 0:
        return (float("nan"), float("nan"))
    t = (float(x.mean()) - float(y.mean())) / math.sqrt(v1 + v2)
    df = (v1 + v2) ** 2 / (v1 ** 2 / (x.size - 1) + v2 ** 2 / (y.size - 1))
    return (float(t), float(_t_sf(t, max(int(df), 1))))


def _bh(pvals: list[float], alpha: float = 0.05) -> list[float]:
    """BH-FDR（本地实现，与 stats_correction.bh_fdr 同算法；返回每个 p 对应的 q）。"""
    m = len(pvals)
    out = [float("nan")] * m
    idx = sorted(range(m), key=lambda i: (math.isnan(pvals[i]), pvals[i]))
    prev = 1.0
    for rank, i in enumerate(reversed(idx), start=1):
        p = pvals[i]
        if math.isnan(p):
            continue
        k = m - rank + 1
        q = min(prev, p * m / k)
        out[i] = min(1.0, q)
        prev = q
    # 保持单调（从大到小已处理），再回填 NaN 之前的位置
    for i in range(m):
        if math.isnan(out[i]):
            out[i] = 1.0
    return out


def _fmt(v: float, pct: bool = True, nd: int = 3) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:+.{nd}%}" if pct else f"{v:+.2f}"


# ---------------- 第一步：构建每日序列（可缓存） ----------------

def build_daily() -> pd.DataFrame:
    with SessionLocal() as db:
        mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
    d1 = str(mx).replace("-", "")[:8] if mx else ""
    print(f"  ① 因子面板 {D0}~{d1}（复用 verify_factor_portfolio.build）...")
    df = VFP.build(D0, d1)
    if df.empty:
        raise RuntimeError("因子面板为空")
    df["_s_combo"] = (VFP._z(df, "mom20", -1) + VFP._z(df, "turnover", -1)
                      + VFP._z(df, "illiq", 1.0) + VFP._z(df, "bias20", -1))
    df["_s_m20"] = -df["mom20"]
    print(f"     过滤后 {len(df):,} 行；拉取 5 日前向收益 ...")
    panel = SG.build_panel((5,), D0, d1, keep_stock=True)
    if panel is None or panel.empty:
        raise RuntimeError("前向收益面板为空")
    p = panel[["trade_date", "ts_code", "fwd5"]].rename(columns={"fwd5": "y"})
    d = df.merge(p, how="inner", on=["trade_date", "ts_code"])
    d["y"] = pd.to_numeric(d["y"], errors="coerce")
    d = d[d["y"].abs() <= MAX_ABS].dropna(subset=["y"])
    if d.empty:
        raise RuntimeError("合并后无有效样本")

    base = d.groupby("trade_date")["y"].mean()
    t = d.dropna(subset=["_s_combo"]).sort_values(
        ["trade_date", "_s_combo"], ascending=[True, False], kind="mergesort")
    t = t.groupby("trade_date").head(N)
    combo = t.groupby("trade_date")["y"].mean()
    t2 = d.dropna(subset=["_s_m20"]).sort_values(
        ["trade_date", "_s_m20"], ascending=[True, False], kind="mergesort")
    t2 = t2.groupby("trade_date").head(N)
    m20 = t2.groupby("trade_date")["y"].mean()

    print("  ② 市场级 regime 特征 ...")
    mk = d.groupby("trade_date").agg(
        mkt_ret=("pct_chg", "mean"),
        xs_disp=("pct_chg", "std"),
        up_n=("pct_chg", lambda s: float((s > 0).sum())),
        dn_n=("pct_chg", lambda s: float((s < 0).sum())),
    )
    mk["ud_ratio"] = mk["up_n"] / mk["dn_n"].replace(0, np.nan)
    r = mk["mkt_ret"] / 100.0
    mk["vol20"] = r.rolling(20, min_periods=10).std()
    mk["mkt_mom20"] = (1.0 + r).rolling(20, min_periods=10).apply(
        lambda a: float(np.prod(a)) - 1.0, raw=True)
    mk["mkt_mom60"] = (1.0 + r).rolling(60, min_periods=30).apply(
        lambda a: float(np.prod(a)) - 1.0, raw=True)
    mk["xsr"] = mk["xs_disp"] / mk["xs_disp"].rolling(20, min_periods=10).mean()

    print("  ③ 情绪读数（sentiment_asof：只取「开盘前可合法使用」的那一行）...")
    srows = []
    for dt in mk.index:
        s = sentiment_asof(str(dt)) or {}
        srows.append({
            "trade_date": str(dt),
            "s_lb2": s.get("n_lb2"), "s_zha": s.get("zha_rate"),
            "s_effect": s.get("money_effect"), "s_seal": s.get("seal_ratio"),
            "s_ud": s.get("up_down_ratio"),
        })
    sent = pd.DataFrame(srows).set_index("trade_date")
    for c in sent.columns:
        sent[c] = pd.to_numeric(sent[c], errors="coerce")

    out = pd.DataFrame({
        "base5": base, "combo5": combo, "m20_5": m20,
        "mkt_ret": mk["mkt_ret"], "xs_disp": mk["xs_disp"], "ud_ratio": mk["ud_ratio"],
        "vol20": mk["vol20"], "mkt_mom20": mk["mkt_mom20"], "mkt_mom60": mk["mkt_mom60"],
        "xsr": mk["xsr"],
    })
    out.index = out.index.astype(str)
    out = out.join(sent, how="left")
    out = out.reset_index().rename(columns={"index": "trade_date"})
    if "trade_date" not in out.columns:
        out = out.rename(columns={out.columns[0]: "trade_date"})
    out["excess"] = out["combo5"] - out["base5"]
    return out


def load_cache() -> pd.DataFrame:
    with open(CACHE, encoding="utf-8") as f:
        recs = json.load(f)
    df = pd.DataFrame(recs).sort_values("trade_date").reset_index(drop=True)
    return df


def save_cache(df: pd.DataFrame) -> None:
    try:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(df.to_dict("records"), f, ensure_ascii=False)
        print(f"  已缓存 → {os.path.relpath(CACHE, BACKEND)}")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 缓存失败：{e}")


# ---------------- 第二步：分档 + FDR ----------------

def analyze(df: pd.DataFrame) -> list[dict]:
    ex = df["excess"]
    rows: list[dict] = []
    for ft in FEATS:
        if ft not in df.columns:
            continue
        f = pd.to_numeric(df[ft], errors="coerce")
        ql = f.rolling(ROLL_W, min_periods=ROLL_MIN).quantile(LO_Q)
        qh = f.rolling(ROLL_W, min_periods=ROLL_MIN).quantile(HI_Q)
        lo = f < ql
        hi = f > qh
        m_lo, n_lo, p_lo = _t1(ex[lo])
        m_hi, n_hi, p_hi = _t1(ex[hi])
        t_d, p_d = _welch(ex[hi], ex[lo])
        rows.append({"feat": ft, "m_lo": m_lo, "n_lo": n_lo, "p_lo": p_lo,
                     "m_hi": m_hi, "n_hi": n_hi, "p_hi": p_hi,
                     "diff": m_hi - m_lo, "t_d": t_d, "p_d": p_d,
                     "usable": int(lo.sum() + hi.sum())})
    qs = _bh([r["p_d"] for r in rows])
    for r, q in zip(rows, qs):
        r["q_d"] = q
    return rows


# ---------------- 第三步：门策略回测 ----------------

def _yearly(d: pd.DataFrame, col: str) -> list[dict]:
    out = []
    yr = d["trade_date"].astype(str).str[:4]
    for y, g in d.groupby(yr):
        out.append({"year": str(y), "n": int(len(g)),
                    "mean": float(g[col].mean()),
                    "on": int(g["_on"].sum()) if "_on" in g.columns else 0})
    return out


def gate_backtest(df: pd.DataFrame, rows: list[dict]) -> list[dict]:
    """对 FDR 后仍显著的特征各做一遍门策略。"""
    out: list[dict] = []
    sig = [r for r in rows if not math.isnan(r["q_d"]) and r["q_d"] <= 0.10]
    for r in sig:
        ft = r["feat"]
        f = pd.to_numeric(df[ft], errors="coerce")
        ql = f.rolling(ROLL_W, min_periods=ROLL_MIN).quantile(LO_Q)
        qh = f.rolling(ROLL_W, min_periods=ROLL_MIN).quantile(HI_Q)
        # 方向：哪一组超额更高，就在那一组 ON
        on = (f > qh) if (r["m_hi"] >= r["m_lo"]) else (f < ql)
        d = df.copy()
        # ⚠️ 掩码必须**在 subset 之前**算好并按原索引落列，否则会错位
        d["_on"] = on.fillna(False).to_numpy()
        d["_ok"] = ql.notna().to_numpy() & d["base5"].notna().to_numpy() \
            & d["combo5"].notna().to_numpy()
        d = d[d["_ok"]]
        if d.empty:
            continue
        d["_gate"] = np.where(d["_on"].to_numpy(), d["combo5"].to_numpy(), d["base5"].to_numpy())
        m_g, n_g, p_g = _t1(d["_gate"])
        m_c, n_c, p_c = _t1(d["combo5"])
        m_b, n_b, p_b = _t1(d["base5"])
        out.append({
            "feat": ft, "side": "high" if r["m_hi"] >= r["m_lo"] else "low",
            "q_d": r["q_d"], "n": n_g, "on_n": int(d["_on"].sum()),
            "gate": m_g, "gate_net": m_g - COST_RT, "gate_t": _t_of(d["_gate"]),
            "combo": m_c, "combo_net": m_c - COST_RT,
            "base": m_b, "base_net": m_b - COST_RT,
            "yearly": _yearly(d, "_gate"),
            "yearly_combo": _yearly(d.assign(_on=True), "combo5"),
        })
    return out


def oos_split(df: pd.DataFrame, cut_frac: float = 0.4) -> list[dict]:
    """★ 样本外切分：**前半定方向，后半评估**。

    为什么必须做：上面 `gate_backtest` 的"方向"（哪一组超额高）是**用全样本**定的 ——
    这是一个**隐蔽的未来函数**（等价于先用答案挑方向，再回头算收益）。
    正确做法：方向与"是否启用"都用前半段决定，后半段只看结果。
    （分档阈值本来就是滚动分位，不含未来信息，无需改。）
    """
    ds = sorted(str(x) for x in df["trade_date"].unique())
    if len(ds) < 200:
        return []
    cut = ds[int(len(ds) * cut_frac)]
    out: list[dict] = []
    for ft in FEATS:
        if ft not in df.columns:
            continue
        f = pd.to_numeric(df[ft], errors="coerce")
        ql = f.rolling(ROLL_W, min_periods=ROLL_MIN).quantile(LO_Q)
        qh = f.rolling(ROLL_W, min_periods=ROLL_MIN).quantile(HI_Q)
        hi, lo = f > qh, f < ql
        ex = df["excess"]
        head = df["trade_date"].astype(str) < cut
        # ① 只用前半段决定方向
        m_hi = float(ex[hi & head].mean())
        m_lo = float(ex[lo & head].mean())
        if math.isnan(m_hi) or math.isnan(m_lo):
            continue
        up = m_hi >= m_lo
        on = hi if up else lo
        # ② 只用后半段评估
        d = df.copy()
        d["_on"] = on.fillna(False).to_numpy()
        d["_ok"] = ql.notna().to_numpy() & d["base5"].notna().to_numpy() \
            & d["combo5"].notna().to_numpy()
        tail = d[d["_ok"] & (d["trade_date"].astype(str) >= cut)]
        if len(tail) < 60:
            continue
        on_arr = tail["_on"].to_numpy()
        combo_arr = tail["combo5"].to_numpy()
        base_arr = tail["base5"].to_numpy()
        tail = tail.assign(
            _gate=np.where(on_arr, combo_arr, base_arr),
            # 第三臂：OFF 持现金，且 OFF 期**仍按全额成本**扣（保守）
            _cash=np.where(on_arr, combo_arr, 0.0),
            # ★ 第四臂：OFF 持现金，且 OFF 期**不换手、不扣成本**（最宽松）
            _free=np.where(on_arr, combo_arr - COST_RT, 0.0),
        )
        g = _t1(tail["_gate"])
        gc = _t1(tail["_cash"])
        gf = _t1(tail["_free"])
        c = _t1(tail["combo5"])
        b = _t1(tail["base5"])
        out.append({
            "feat": ft, "cut": cut, "dir": "high" if up else "low",
            "head_hi": m_hi, "head_lo": m_lo,
            "n": g[1], "on_n": int(tail["_on"].sum()),
            "gate": g[0], "gate_net": g[0] - COST_RT, "gate_t": _t_of(tail["_gate"]),
            "cash": gc[0], "cash_net": gc[0] - COST_RT, "cash_t": _t_of(tail["_cash"]),
            "free": gf[0], "free_net": gf[0], "free_t": _t_of(tail["_free"]),
            "combo": c[0], "combo_net": c[0] - COST_RT,
            "base": b[0], "base_net": b[0] - COST_RT,
        })
    return out


def _t_of(s: pd.Series) -> float:
    x = s.dropna().to_numpy(dtype="float64")
    if x.size < 10:
        return float("nan")
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return float("nan")
    return float(x.mean() / (sd / math.sqrt(x.size)))


def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-cache", action="store_true", help="复用已缓存的每日序列")
    ap.add_argument("--from", dest="d0", default="",
                    help="起始日 YYYYMMDD（缺省用脚本常量 D0 ⇒ **默认行为不变**）；"
                         "用于扩样本重跑，如 --from 20100101")
    ap.add_argument("--no-cache", action="store_true",
                    help="**不写回缓存**（扩样本重跑时保护原缓存；结果只出在 stdout）")
    args = ap.parse_args()
    t0 = time.time()
    print("=" * 112)
    print("regime 门验证（plans/24 §11.13.7）")
    print("=" * 112)

    global D0
    if args.d0:
        D0 = str(args.d0).replace("-", "")[:8]
        print(f"  ★ 起始日覆盖为 {D0}（来自 --from；脚本常量本身不变）")

    if args.from_cache and os.path.exists(CACHE):
        print(f"  复用缓存 {os.path.relpath(CACHE, BACKEND)}")
        df = load_cache()
    else:
        df = build_daily()
        if args.no_cache:
            print(f"  （--no-cache：本次**不写回** {os.path.basename(CACHE)}，保护原缓存）")
        else:
            save_cache(df)
    if df.empty:
        print("  数据为空")
        return 1
    print(f"  每日序列 {len(df)} 天（{df['trade_date'].iloc[0]} ~ {df['trade_date'].iloc[-1]}）")

    rows = analyze(df)
    print()
    print("=== 分档检验（滚动 252 日 30%/70% 分位；excess = combo − base，同日相减已消 beta）===")
    hd = (f"{'regime 特征':<20}{'low组超额':>11}{'天数':>6}"
          f"{'high组超额':>12}{'天数':>6}{'high−low':>11}{'Welch t':>9}"
          f"{'p':>8}{'q(FDR)':>9}")
    print(hd)
    print("-" * len(hd))
    for r in sorted(rows, key=lambda x: (math.isnan(x["p_d"]), x["p_d"])):
        c1 = _fmt(r["m_lo"])
        c2 = _fmt(r["m_hi"])
        c3 = _fmt(r["diff"])
        c4 = "—" if math.isnan(r["t_d"]) else f"{r['t_d']:+.2f}"
        c5 = "—" if math.isnan(r["p_d"]) else f"{r['p_d']:.4f}"
        c6 = "—" if math.isnan(r["q_d"]) else f"{r['q_d']:.4f}"
        print(f"{LABEL[r['feat']]:<20}{c1:>11}{r['n_lo']:>6}{c2:>12}{r['n_hi']:>6}"
              f"{c3:>11}{c4:>9}{c5:>8}{c6:>9}")

    n_ok = sum(1 for r in rows if not math.isnan(r["q_d"]) and r["q_d"] <= 0.10)
    print()
    print(f"  FDR(q≤0.10) 通过：{n_ok} / {len(rows)}")

    gs = gate_backtest(df, rows)
    if not gs:
        print()
        print("  ❌ **没有任何 regime 特征通过 FDR** → 门策略无需回测（假设被否证）")
    else:
        print()
        print("=== 门策略回测（ON 持 4 因子组合 / OFF 持全市场等权；双边 0.5%）===")
        hd = (f"{'门（特征/方向）':<26}{'ON天':>7}{'总天':>6}{'每期毛':>10}"
              f"{'扣成本':>10}{'年化净':>10}{'t':>8}{'always组合':>12}")
        print(hd)
        print("-" * len(hd))
        for g in gs:
            lab = f"{LABEL[g['feat']]}/{'高' if g['side'] == 'high' else '低'}"
            c1 = _fmt(g["gate"])
            c2 = _fmt(g["gate_net"])
            c3 = f"{g['gate_net'] * REBAL:+.1%}"
            c4 = "—" if math.isnan(g["gate_t"]) else f"{g['gate_t']:+.2f}"
            c5 = _fmt(g["combo_net"])
            print(f"{lab:<26}{g['on_n']:>7}{g['n']:>6}{c1:>10}{c2:>10}{c3:>10}{c4:>8}{c5:>12}")
        print()
        print("  逐年分解（门策略每期毛 / ON 天数）—— 门是否只是在「事后避开 2026H1」：")
        for g in gs:
            lab = f"{LABEL[g['feat']]}/{'高' if g['side'] == 'high' else '低'}"
            print(f"    [{lab}]")
            for y in g["yearly"]:
                c1 = _fmt(y["mean"])
                print(f"      {y['year']}  {y['n']:>4} 天  每期毛 {c1}  ON {y['on']:>3} 天")

    oos = oos_split(df)
    if oos:
        cut = oos[0]["cut"]
        print()
        print(f"=== ★★ 样本外切分（前半 < {cut} 定方向；后半 ≥ {cut} 只用结果）===")
        b_net = float(np.mean([r["base_net"] for r in oos]))
        c_net = float(np.mean([r["combo_net"] for r in oos]))
        print(f"  参考（同一后半窗口）：always-市场等权 {b_net * REBAL:+.1%}/年 | "
              f"always-4因子组合 {c_net * REBAL:+.1%}/年 | "
              f"两者的差（=该窗口的因子超额） {(c_net - b_net) * REBAL:+.1%}/年")
        print()
        hd = (f"{'特征/方向':<24}{'ON天':>6}{'门(持市场)':>11}"
              f"{'门(持现金·全扣)':>15}{'门(持现金·OFF免成本)':>19}"
              f"{'t':>7}{'always组合':>12}")
        print(hd)
        print("-" * len(hd))
        for r in sorted(oos, key=lambda x: -x["free_net"]):
            lab = f"{LABEL[r['feat']]}/{'高' if r['dir'] == 'high' else '低'}"
            c1 = f"{r['gate_net'] * REBAL:+.1%}"
            c2 = f"{r['cash_net'] * REBAL:+.1%}"
            c3 = f"{r['free_net'] * REBAL:+.1%}"
            c4 = "—" if math.isnan(r["free_t"]) else f"{r['free_t']:+.2f}"
            c5 = f"{r['combo_net'] * REBAL:+.1%}"
            print(f"{lab:<24}{r['on_n']:>6}{c1:>11}{c2:>15}{c3:>19}{c4:>7}{c5:>12}")
        print()
        print("  （表中全部为**年化净收益**；ON/OFF 天数据后半天数 "
              f"{oos[0]['n']}，ON 占比约 {oos[0]['on_n'] / max(oos[0]['n'], 1):.0%}）")
        n_beat = sum(1 for r in oos if r["gate_net"] > r["combo_net"])
        n_free = sum(1 for r in oos if r["free_net"] > 0)
        n_cash = sum(1 for r in oos if r["cash_net"] > 0)
        print(f"  样本外：门(持市场)跑赢 always-combo = {n_beat} / {len(oos)}")
        print(f"  样本外：门(持现金·OFF免成本)年化净为正 = {n_free} / {len(oos)}"
              f"（最宽松口径；若连它都不为正 → 结论不依赖成本假设）")
        print(f"  样本外：门(持现金·全扣)年化净为正 = {n_cash} / {len(oos)}")

        # ★ 费率敏感性：纯重算（不重跑面板）。
        #   gate/cash 的"毛"= 净 + 0.005；free 的毛按 ON 占比折算。
        print()
        print("=== ★★ 费率敏感性（样本外；门的正负可能完全取决于费率）===")
        print(f"  注：本表为**年化净收益**，按不同双边费率重算；ON 占比见上表。")
        hd = (f"{'特征/方向':<24}{'ON占比':>8}"
              f"{'0.10%·持市场':>15}{'0.10%·持现金':>15}"
              f"{'0.25%·持市场':>15}{'0.25%·持现金':>15}"
              f"{'0.50%·持现金':>15}")
        print(hd)
        print("-" * len(hd))
        for r in sorted(oos, key=lambda x: -x["free_net"])[:4]:
            frac = r["on_n"] / max(r["n"], 1)
            lab = f"{LABEL[r['feat']]}/{'高' if r['dir'] == 'high' else '低'}"
            g_gross = r["gate_net"] + COST_RT
            c_gross = r["cash_net"] + COST_RT
            f_gross = r["free_net"] + COST_RT * frac      # ON 天毛收益（按占比折算）
            a = (g_gross - 0.001) * REBAL
            b = (c_gross - 0.001) * REBAL
            c = (g_gross - 0.0025) * REBAL
            d = (c_gross - 0.0025) * REBAL
            e = (f_gross - 0.005 * frac) * REBAL
            print(f"{lab:<24}{frac:>8.0%}{a:>15.1%}{b:>15.1%}{c:>15.1%}{d:>15.1%}{e:>15.1%}")
        print("  → 若「持现金」臂在 0.10% 费率下转正、在 0.50% 下为负 ⇒")
        print("     结论不是「策略无效」，而是「**该策略的边际太薄，活不过交易成本**」。")
        print()
        print("  ★ 把最好的门拆成「毛收益 vs 成本」两项（口径已核对，避免把平均量误标）：")
        r0 = sorted(oos, key=lambda x: -x["free_net"])[0]
        frac0 = r0["on_n"] / max(r0["n"], 1)
        # free_net 已是"全期（含 OFF 的天）平均"，加回成本即得全期平均毛收益
        gross_all = r0["free_net"] + COST_RT * frac0
        gross_on = gross_all / frac0 if frac0 > 0 else float("nan")
        c05 = COST_RT * frac0
        c01 = 0.001 * frac0
        print(f"     [{LABEL[r0['feat']]}/{'高' if r0['dir'] == 'high' else '低'}]")
        print(f"       ON 天平均毛 = {gross_on:+.3%}/期   全期平均毛 = {gross_all:+.3%}/期"
              f"  → 年化毛 {gross_all * REBAL:+.1%}")
        print(f"       成本(0.50% 双边 × ON占比 {frac0:.0%}) = {c05:.3%}/期"
              f" → 年化 {c05 * REBAL:.1%}"
              f" ⇒ 净 {(gross_all - c05) * REBAL:+.1%}/年")
        print(f"       成本(0.10% 双边)             = {c01:.3%}/期"
              f" → 年化 {c01 * REBAL:.1%}"
              f" ⇒ 净 {(gross_all - c01) * REBAL:+.1%}/年")

    print()
    print("=" * 112)
    print("判读（事先定好，不许事后改）：")
    print("  1) 若 0 个特征过 FDR → **regime 门这条路也被否证**（第 14 次），")
    print("     那么「事后看得见的 regime」与「事前可用的 regime」是两回事；")
    print("  2) 若过了 FDR，还必须满足：**逐年分解中每一年都不差于 always-combo** ——")
    print("     只在某些年份有效的门，等价于「事后避开已知的坏年份」，不可用；")
    print("  3) 分档阈值一律滚动分位（无未来函数），代价是前 60 天不可用；")
    print("  4) excess 是同日相减，已消掉市场 beta —— 但**成本仍按每期 0.5% 全额扣**（保守）。")
    print(f"耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
