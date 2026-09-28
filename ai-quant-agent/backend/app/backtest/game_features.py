"""个股博弈特征（game_features）— plans/24 人心博弈层 H3

## 要回答的问题

H2 已把瓶颈定量锁定在**选股**：同期（近 30 日）全市场等权 fwd5 = **−0.353%**，
而我们的推荐样本 = **−5.55%**，缺口 **5.2 个百分点** —— 择时解释不了它。

H3 的问题：**"主力 vs 散户"这类博弈特征，能不能预测个股的前向收益？**

## 特征清单（全部只用 T 日及之前的数据；"计谋"只是命名，计算是严格的）

| 特征 | 计谋 | 含义 |
| ---- | ---- | ---- |
| `main_ratio` | — | 主力净额（大单+特大单）占当日成交额的比例 |
| `main_ratio_5` | 反客为主 | 主力净额占比的 5 日均值（持续性） |
| `pain_dist` | **笑里藏刀** | 价涨（pct_chg>0）**而**主力在卖（main_ratio<0）= 拉高派发 |
| `kuru_ji` | **苦肉计** | 价跌**而**主力在买 = 砸盘吸筹 |
| `chenhuo` | **趁火打劫** | 单日跌 >5% **而**主力在买 = 接恐慌盘 |
| `kongcheng` | **空城计** | 一字板（全天未打开 = 无人愿卖） |
| `dacao` | **打草惊蛇** | 长上影 >3% = 试盘 |
| `shushang` | **树上开花** | 量比 >2 **而** |涨跌| <1% = 量增价平（虚张声势/对倒） |
| `andu` | **暗度陈仓** | 20 日振幅 <15% **且** 20 日累计主力净买 >0 **且** 换手处低位 |
| `upper_shadow` | 调虎离山 | 上影线长度 / 前收 |
| `lower_shadow` | 以逸待劳 | 下影线长度 / 前收（恐慌后收复） |
| `gap` | — | 开盘缺口 open/pre_close − 1 |
| `churn` | **偷梁换柱** | 换手率 / (|涨跌|+0.5) = 换手高而价格不动（对倒代理） |
| `turnover_pos60` | — | 换手率在近 60 日区间中的位置（低位高换手=换手，高位高换手=派发） |
| `pos60` | — | 收盘价在近 60 日区间中的位置（高位/低位） |
| `vol_ratio20` | — | 当日量 / 20 日均量 |

## ★ 统计方法（本模块最关键的设计）

**不能**把 350 万个 (交易日, 股票) 当独立样本 —— 同一天的股票高度相关（共同市场因子），
那会把显著性抬高一个数量级（plans/23 §14.9 已踩过同类坑）。

改用 **Fama-MacBeth 式两步法**：

    第一步：**每日截面**内按特征分 k 组，算每组的当日等权 fwd5；
    第二步：得到每日的"高组 − 低组"差值序列（长度 = 交易日数，约 659），
            对这条差值序列做**单样本 t 检验**。

于是有效样本量是**天数**（659），而不是 350 万 —— 这才是诚实的自由度。

## 判据（提前声明）

  - 显著性：日度差值序列的 t 检验 `p < 0.05`，**且** BH-FDR 校正后 `q < 0.05`；
  - 经济意义：|高组 − 低组| ≥ `MIN_SPREAD`（默认 **0.5%**）——
    双边成本约 0.2~0.5%，低于此的"优势"会被成本吃掉；
  - 两者同时满足才算"有依据"。

## 用法

    cd ai-quant-agent/backend && ./venv/bin/python -m app.backtest.game_features
"""
from __future__ import annotations

import json
import math
import os
import time

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.backtest import sentiment as ST
from app.backtest import sentiment_gate as SG
from app.backtest.stats_correction import bh_fdr, t_compare
from app.core.logger import logger
from app.models import SessionLocal

GAME_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "game_features.json",
)
# ★ 日度「高减低」差值序列（plans/24 §11.28 A2）：**另存**，不动 GAME_FILE 的结构
#   （GAME_FILE 里只有汇总 t / p，所以 HAC 复核一直只能把它列为「不可复核」）。
GAME_SERIES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "game_features_series.json",
)

MIN_SPREAD = 0.005        # 有经济意义的最小高减低差（0.5%）
ALPHA = 0.05
MIN_STOCKS_PER_DAY = 200  # 当日截面股票数下限（低于此不做分组）
MIN_DAYS = 60             # 日度差值序列的最少天数

# 连续特征（按日截面等频分 5 组）
CONT_FEATURES = ("main_ratio", "main_ratio_5", "upper_shadow", "lower_shadow",
                 "gap", "churn", "turnover_pos60", "pos60", "vol_ratio20")
# 事件特征（0/1，比较"1 组 vs 0 组"）
EVENT_FEATURES = ("pain_dist", "kuru_ji", "chenhuo", "kongcheng",
                  "dacao", "shushang", "andu")

PLAN_NAME = {
    "pain_dist": "笑里藏刀", "kuru_ji": "苦肉计", "chenhuo": "趁火打劫",
    "kongcheng": "空城计", "dacao": "打草惊蛇", "shushang": "树上开花",
    "andu": "暗度陈仓", "main_ratio_5": "反客为主", "churn": "偷梁换柱",
    "upper_shadow": "调虎离山", "lower_shadow": "以逸待劳",
}


def _limit_pct(code: str) -> float:
    return ST._limit_pct(code)


# ─────────────────────────────────────────────────────────────────────────────
# 特征计算
# ─────────────────────────────────────────────────────────────────────────────
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


def _num_col(df: pd.DataFrame, name: str) -> pd.Series:
    """取数值列；列不存在时返回全 NaN（保持索引对齐，避免下游 KeyError）。"""
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype="float64")


def build_features(d0: str, d1: str) -> pd.DataFrame:
    """个股级博弈特征（每行 = 一只股票的一个交易日，特征只用 ≤T 的数据）。"""
    daily = _load(
        "SELECT trade_date, ts_code, open, high, low, close, pre_close, pct_chg, "
        "vol, amount FROM daily WHERE trade_date >= :a AND trade_date <= :b", d0, d1)
    dbb = _load(
        "SELECT trade_date, ts_code, turnover_rate, turnover_rate_f, volume_ratio, "
        "circ_mv FROM daily_basic WHERE trade_date >= :a AND trade_date <= :b", d0, d1)
    mf = _load(
        "SELECT trade_date, ts_code, buy_sm_amount, sell_sm_amount, buy_md_amount, "
        "sell_md_amount, buy_lg_amount, sell_lg_amount, buy_elg_amount, sell_elg_amount "
        "FROM moneyflow WHERE trade_date >= :a AND trade_date <= :b", d0, d1)
    if daily.empty:
        return pd.DataFrame()

    num = ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]
    for c in num:
        daily[c] = pd.to_numeric(daily[c], errors="coerce").astype("float64")
    for c in ("turnover_rate", "turnover_rate_f", "volume_ratio", "circ_mv"):
        if not dbb.empty:
            dbb[c] = pd.to_numeric(dbb[c], errors="coerce").astype("float64")
    for c in ("buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
              "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount"):
        if not mf.empty:
            mf[c] = pd.to_numeric(mf[c], errors="coerce").astype("float64")

    d = daily
    if not dbb.empty:
        d = d.merge(dbb, on=["trade_date", "ts_code"], how="left")
    if not mf.empty:
        d = d.merge(mf, on=["trade_date", "ts_code"], how="left")
    d = d[d["pre_close"] > 0].copy()
    # 这几列可能因 daily_basic 缺失而不存在 → 统一补成全 NaN，保证后续不炸
    for _c in ("turnover_rate", "volume_ratio", "turnover_rate_f"):
        d[_c] = _num_col(d, _c)
    d["vol"] = _num_col(d, "vol")

    # ── 主力/散户净额（同一张表内做，**避免跨表单位问题**）──
    # ⚠️ daily.amount 单位是千元，moneyflow 是万元 → 不能混算。
    #    因此分母用 moneyflow 自己的"买卖双边合计" tot（同源同单位）。
    if "buy_lg_amount" in d.columns:
        buy = (d["buy_sm_amount"].fillna(0) + d["buy_md_amount"].fillna(0)
               + d["buy_lg_amount"].fillna(0) + d["buy_elg_amount"].fillna(0))
        sell = (d["sell_sm_amount"].fillna(0) + d["sell_md_amount"].fillna(0)
                + d["sell_lg_amount"].fillna(0) + d["sell_elg_amount"].fillna(0))
        main_amt = ((d["buy_lg_amount"].fillna(0) + d["buy_elg_amount"].fillna(0))
                    - (d["sell_lg_amount"].fillna(0) + d["sell_elg_amount"].fillna(0)))
        tot = buy + sell
        d["main_amt"] = main_amt
        d["main_ratio"] = np.where(tot > 0, main_amt / tot, np.nan)   # ∈ [-0.5, 0.5]
    else:
        d["main_amt"] = np.nan
        d["main_ratio"] = np.nan

    # ── 价量派生（全部只用当日及之前）──
    hi, lo, op, cl, pre = d["high"], d["low"], d["open"], d["close"], d["pre_close"]
    body_hi = np.maximum(op, cl)
    body_lo = np.minimum(op, cl)
    d["upper_shadow"] = (hi - body_hi) / pre
    d["lower_shadow"] = (body_lo - lo) / pre
    d["gap"] = op / pre - 1.0
    tr = d["turnover_rate"]
    d["churn"] = tr / (d["pct_chg"].abs() + 0.5)      # 换手高而价格不动（对倒代理）

    # ── 需要跨日的滚动特征（按股票分组）──
    d = d.sort_values(["ts_code", "trade_date"], kind="mergesort")
    g = d.groupby("ts_code", sort=False)
    d["main_ratio_5"] = g["main_ratio"].transform(lambda s: s.rolling(5, min_periods=3).mean())
    d["main_sum20"] = g["main_amt"].transform(lambda s: s.rolling(20, min_periods=10).sum())
    rmax_h = g["high"].transform(lambda s: s.rolling(20, min_periods=10).max())
    rmin_l = g["low"].transform(lambda s: s.rolling(20, min_periods=10).min())
    d["amp20"] = (rmax_h - rmin_l) / cl
    rmax_c = g["close"].transform(lambda s: s.rolling(60, min_periods=20).max())
    rmin_c = g["close"].transform(lambda s: s.rolling(60, min_periods=20).min())
    rng = (rmax_c - rmin_c)
    d["pos60"] = np.where(rng > 0, (cl - rmin_c) / rng, np.nan)
    rmax_t = g["turnover_rate"].transform(lambda s: s.rolling(60, min_periods=20).max())
    rmin_t = g["turnover_rate"].transform(lambda s: s.rolling(60, min_periods=20).min())
    rng_t = (rmax_t - rmin_t)
    d["turnover_pos60"] = np.where(rng_t > 0, (tr - rmin_t) / rng_t, np.nan)
    vol_ma20 = g["vol"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    d["vol_ratio20"] = np.where(vol_ma20 > 0, d["vol"] / vol_ma20, np.nan)

    # ── 事件特征（"计谋"的二分实现）──
    lim = d["ts_code"].map(_limit_pct).to_numpy(dtype="float64")
    thr = lim * 100.0 - 0.3
    pc = d["pct_chg"].to_numpy(dtype="float64")
    lo_pct = (lo.to_numpy(dtype="float64") / pre.to_numpy(dtype="float64") - 1.0) * 100.0
    mr = d["main_ratio"].to_numpy(dtype="float64")

    d["pain_dist"] = ((pc > 0) & (mr < 0)).astype("float64")          # 笑里藏刀
    d["kuru_ji"] = ((pc < 0) & (mr > 0)).astype("float64")            # 苦肉计
    d["chenhuo"] = ((pc <= -5.0) & (mr > 0)).astype("float64")        # 趁火打劫
    d["kongcheng"] = ((pc >= thr) & (lo_pct >= thr - 1.0)).astype("float64")   # 空城计
    d["dacao"] = (d["upper_shadow"] > 0.03).astype("float64")         # 打草惊蛇
    d["shushang"] = ((d["volume_ratio"] > 2.0)
                     & (np.abs(pc) < 1.0)).astype("float64")          # 树上开花
    d["andu"] = ((d["amp20"] < 0.15) & (d["main_sum20"] > 0)
                 & (d["turnover_pos60"] < 0.4)).astype("float64")     # 暗度陈仓

    keep = (["trade_date", "ts_code", "close", "pct_chg"]
            + list(CONT_FEATURES) + list(EVENT_FEATURES))
    out = d[[c for c in keep if c in d.columns]].copy()
    for c in list(CONT_FEATURES):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").astype("float32")
    return out.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Fama-MacBeth 式筛查
# ─────────────────────────────────────────────────────────────────────────────
def _tstat(x: np.ndarray) -> tuple[float, float, int]:
    """单样本 t 检验（H0: 均值 = 0）。返回 (t, p, n)。"""
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


def screen_feature(feat: str, df: pd.DataFrame, horizon: int = 5,
                   k: int = 5, series_out: dict | None = None) -> dict:
    """按日截面分组 → 日度高低差 → 时序 t 检验（含 Newey-West HAC，§11.28）。

    传入 `series_out`（dict）时，额外写入本特征的**日度差值序列**，仅供 HAC 复核。
    """
    col = f"fwd{horizon}"
    if feat not in df.columns or col not in df.columns:
        return {"feature": feat, "verdict": "字段缺失"}
    d = df[["trade_date", "ts_code", feat, col]].copy()
    d = d.dropna(subset=[feat, col])
    if d.empty:
        return {"feature": feat, "verdict": "无有效样本"}
    d[feat] = pd.to_numeric(d[feat], errors="coerce")
    d = d.dropna(subset=[feat])
    # 当日截面股票数下限
    cnt = d.groupby("trade_date")["ts_code"].transform("size")
    d = d[cnt >= MIN_STOCKS_PER_DAY]
    if d.empty:
        return {"feature": feat, "verdict": "当日截面样本不足"}

    is_event = feat in EVENT_FEATURES
    if is_event:
        # 事件特征：1 组 vs 0 组（不做分位分组）
        d["_g"] = (d[feat] > 0.5).astype("int8")
        if d["_g"].nunique() < 2:
            return {"feature": feat, "verdict": "事件无变异（全 0 或全 1）"}
        grp = d.groupby(["trade_date", "_g"])[col].mean().unstack()
        if 1 not in grp.columns or 0 not in grp.columns:
            return {"feature": feat, "verdict": "事件分组不完整"}
        diff = (grp[1] - grp[0]).dropna()
        hi_col, lo_col = 1, 0
        groups = [{"q": 0, "n_days": int(grp[0].notna().sum()),
                   "fwd_mean": round(float(grp[0].mean()), 5)},
                  {"q": 1, "n_days": int(grp[1].notna().sum()),
                   "fwd_mean": round(float(grp[1].mean()), 5)}]
        hit_rate = float((d[d["_g"] == 1][col] > 0).mean()) if (d["_g"] == 1).any() else None
    else:
        # 连续特征：按日截面等频 k 组（重复值多时用 rank 兜底）
        def _lab(s: pd.Series) -> pd.Series:
            try:
                return pd.qcut(s.rank(method="first"), k, labels=False)
            except Exception:  # noqa: BLE001
                return pd.Series([np.nan] * len(s), index=s.index)

        d["_g"] = d.groupby("trade_date")[feat].transform(_lab)
        d = d.dropna(subset=["_g"])
        if d.empty:
            return {"feature": feat, "verdict": "分组失败"}
        d["_g"] = d["_g"].astype("int8")
        grp = d.groupby(["trade_date", "_g"])[col].mean().unstack()
        if k - 1 not in grp.columns or 0 not in grp.columns:
            return {"feature": feat, "verdict": "分组列缺失"}
        diff = (grp[k - 1] - grp[0]).dropna()
        hi_col, lo_col = k - 1, 0
        groups = [{"q": int(q), "n_days": int(grp[q].notna().sum()),
                   "fwd_mean": round(float(grp[q].mean()), 5)}
                  for q in sorted(grp.columns)]
        # 单调性：组均值序列的 Spearman 相关（越接近 ±1 越单调）
        gm = [g["fwd_mean"] for g in groups if g["fwd_mean"] is not None]
        hit_rate = None

    t, p, n = _tstat(diff.to_numpy())
    # ★ Newey-West HAC（plans/24 §11.28）—— 修正**重叠窗口**导致的 t 膨胀：
    #   fwd_h = T+1 开盘买入持 h 日 ⇒ 相邻交易日的持有窗口重叠 h−1 天
    #   ⇒ 日度差值序列本身有序列相关 ⇒ naive t 被高估。
    #   **判定用的 `t` 保持不变**（仍是 naive）；`t_hac` 只作口径披露。
    tc = t_compare(diff.to_numpy(), max(0, horizon - 1))
    if series_out is not None:
        series_out[feat] = {str(d): float(v) for d, v in diff.items()}
    spread = float(diff.mean()) if len(diff) else None
    # 单调性
    mono = None
    if not is_event and len(groups) >= 3:
        gm = np.array([g["fwd_mean"] for g in groups], dtype="float64")
        gm = gm[np.isfinite(gm)]
        if len(gm) >= 3:
            r = pd.Series(gm).corr(pd.Series(np.arange(len(gm))), method="spearman")
            mono = None if pd.isna(r) else round(float(r), 3)
    return {
        "feature": feat, "plan": PLAN_NAME.get(feat, ""), "horizon": horizon,
        "type": "event" if is_event else "cont",
        "n_days": n, "spread": None if spread is None else round(spread, 5),
        "t": round(t, 3), "p": round(p, 6),
        # ★ HAC 披露（§11.28）：判定仍用上面的 naive `t`
        "t_hac": (None if tc["t_hac"] != tc["t_hac"] else round(float(tc["t_hac"]), 3)),
        "hac_lag": int(tc["lag"]),
        "t_shrink": (None if tc["shrink"] != tc["shrink"] else round(float(tc["shrink"]), 3)),
        "monotonic_spearman": mono, "hit_rate_when_on": hit_rate,
        "groups": groups, "verdict": "待校正",
    }


def run_screen(d0: str = "", d1: str = "", horizon: int = 5,
               save: bool = True) -> dict:
    t0 = time.time()
    sent = ST.load_sentiment()
    if not sent or not sent.get("days"):
        return {"ok": False, "msg": "缺少 data/sentiment_daily.json"}
    rng = sent.get("range") or [min(sent["days"]), max(sent["days"])]
    if not d0:
        d0, d1 = str(rng[0]), str(rng[1])

    logger.info(f"[game] 计算个股博弈特征 {d0}~{d1} ...")
    feats = build_features(d0, d1)
    if feats.empty:
        return {"ok": False, "msg": "特征为空"}
    # ★ 前向收益复用 H2 的**同一份口径**（build_panel(keep_stock=True)）
    fwd = SG.build_panel(SG.HORIZONS, d0, d1, keep_stock=True)
    if fwd.empty:
        return {"ok": False, "msg": "前向收益为空"}
    df = feats.merge(fwd, on=["trade_date", "ts_code"], how="inner")
    logger.info(f"[game] 合并后样本 {len(df):,} 行，{df['trade_date'].nunique()} 个交易日")

    results = []
    series: dict[str, dict[str, float]] = {}   # ★ 日度差值序列（另存，见 GAME_SERIES_FILE）
    for feat in list(CONT_FEATURES) + list(EVENT_FEATURES):
        r = screen_feature(feat, df, horizon=horizon, series_out=series)
        results.append(r)

    # ── BH-FDR 校正（所有特征一起校正）──
    idx = [i for i, r in enumerate(results) if r.get("p") is not None]
    fdr = bh_fdr([results[i]["p"] for i in idx], alpha=ALPHA)
    q_map = dict(zip(fdr.get("idx") or [], fdr.get("q") or []))
    for j, i in enumerate(idx):
        q = q_map.get(j)
        r = results[i]
        r["q"] = q
        r["fdr_pass"] = bool(q is not None and q <= ALPHA)
        sp = r.get("spread")
        r["meaningful"] = sp is not None and abs(sp) >= MIN_SPREAD
        r["usable"] = bool(r["fdr_pass"] and r["meaningful"])
        th = r.get("t_hac")
        r["hac_pass"] = bool(th is not None and abs(float(th)) >= 2.0)
        if r["usable"]:
            r["verdict"] = "有依据"
        elif r["fdr_pass"] and not r["meaningful"]:
            r["verdict"] = "显著但幅度不足（会被成本吃掉）"
        elif not r["fdr_pass"]:
            r["verdict"] = "FDR 后不显著"
    usable = [r for r in results if r.get("usable")]
    raw = [r for r in results if r.get("p") is not None and r["p"] < ALPHA
           and r.get("meaningful")]

    out = {
        "ok": True,
        "range": [d0, d1],
        "horizon": horizon,
        "n_rows": int(len(df)),
        "n_days": int(df["trade_date"].nunique()),
        "min_spread": MIN_SPREAD,
        "alpha": ALPHA,
        "fdr": {"m": fdr.get("m"), "n_raw": len(raw), "n_usable": len(usable),
                "threshold": fdr.get("threshold")},
        "results": results,
        "usable": usable,
        # ★ HAC 口径披露（§11.28）：判定用的 `t` 未修正重叠窗口；`t_hac` 仅作对照
        "t_basis": ("`t` = naive（日度差值序列直接 t 检验，**判定用**）；"
                    "`t_hac` = Newey-West（lag = 持有期 − 1，**仅披露、不参与判定**）"),
        "n_hac_robust": int(sum(1 for r in results if r.get("hac_pass"))),
        "verdict": ("存在可用博弈特征" if usable else "无可采纳的博弈特征（FDR 后无依据）"),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "method": ("Fama-MacBeth 式：按日截面分组 → 日度高减低差 → 时序 t 检验"
                   "（有效样本量 = 天数，不是 (日,股) 个数）"),
        "entry": "T+1 开盘买入（与 H2 同口径，天然无未来函数）",
    }
    if save:
        try:
            os.makedirs(os.path.dirname(GAME_FILE), exist_ok=True)
            with open(GAME_FILE, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[game] 落盘失败：{exc}")
        # ★ 日度差值序列**另存**（plans/24 §11.28）：不动 GAME_FILE 的结构，
        #   仅供 HAC 复核脚本（verify_t_overlap_recheck.py）读取。
        try:
            with open(GAME_SERIES_FILE, "w", encoding="utf-8") as f:
                json.dump({"range": [d0, d1], "horizon": int(horizon),
                           "lag": max(0, int(horizon) - 1),
                           "n_series": len(series), "series": series},
                          f, ensure_ascii=False, indent=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[game] 序列落盘失败：{exc}")
    logger.info(f"[game] 完成：{out['verdict']}（可用 {len(usable)} 个），"
                f"耗时 {out['elapsed_sec']}s")
    return out


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="个股博弈特征筛查（plans/24 H3）")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    print("=" * 92)
    print("个股博弈特征筛查（plans/24 H3）—— Fama-MacBeth 式（按日截面分组）")
    print("=" * 92)
    r = run_screen(args.start, args.end, horizon=args.horizon, save=not args.no_save)
    if not r.get("ok"):
        print(f"失败：{r.get('msg')}")
        return 1

    print(f"样本：{r['n_rows']:,} 行 / {r['n_days']} 个交易日  区间 {r['range'][0]}~{r['range'][1]}")
    print(f"持仓 {r['horizon']} 日（T+1 开盘买入）｜{r['method']}")
    print()
    hdr = (f"{'特征':<16}{'计谋':<8}{'类型':<6}{'天数':>6}{'高减低':>10}"
           f"{'t':>8}{'t_HAC':>8}{'p':>9}{'q':>9}{'单调':>7}  判定")
    print(hdr)
    print("-" * len(hdr))
    rows = sorted(r["results"], key=lambda x: (x.get("spread") is None,
                                               -(abs(x.get("spread") or 0))))
    for x in rows:
        sp = x.get("spread")
        mono = x.get("monotonic_spearman")
        th = x.get("t_hac")
        th_s = f"{th:>8.2f}" if th is not None else f"{'—':>8}"
        print(f"{x['feature']:<16}{(x.get('plan') or '—'):<8}{x.get('type', '—'):<6}"
              f"{x.get('n_days', 0):>6}"
              f"{(f'{sp:+.3%}' if sp is not None else '—'):>10}"
              f"{(x.get('t') if x.get('t') is not None else 0):>8.2f}"
              f"{th_s}"
              f"{(x.get('p') if x.get('p') is not None else 1):>9.4f}"
              f"{(x.get('q') if x.get('q') is not None else 1):>9.4f}"
              f"{(f'{mono:+.2f}' if mono is not None else '—'):>7}  {x.get('verdict')}")
    print()
    print(f"多重检验：m={r['fdr']['m']}，未校正显著(且|差|≥{r['min_spread']:.1%}) "
          f"{r['fdr']['n_raw']} 个 → BH-FDR 后 {r['fdr']['n_usable']} 个")
    if r.get("usable"):
        print("★ 可用（唯一能当依据的）：")
        for x in r["usable"]:
            print(f"    · {x['feature']}（{x.get('plan') or '—'}）"
                  f" 高减低={x['spread']:+.3%} q={x['q']} 天数={x['n_days']}")
    print()
    print(f"结论：{r['verdict']}")
    print(f"HAC 复核（§11.28）：{r.get('n_hac_robust', 0)} / {len(r['results'])} 个特征 "
          f"|t_hac| ≥ 2（lag = {max(0, r['horizon'] - 1)}）—— 判定仍用 naive t")
    print(f"（hint：H2 已定基线 —— 同期全市场 fwd5 = −0.353%，推荐样本 = −5.55%）")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
