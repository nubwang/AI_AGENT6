"""基准组合生成器（baseline_portfolio）— plans/24 §11.19：把 B1 做成"可对照"的形态

## 它是什么（以及**不是什么**）

它**不是选股器**。它是**索引化组合**：按代码排序后**等距取样**，
所以它对每只票**没有任何偏好**（这一点是关键 ——
§11.18 已证明：我们的"打分选股"比"随机抽 30 只"每年少赚 6.8pp）。

它把 §11.16 定义的基准变成**每天可生成的一份名单**：

    mode="market_ew" ：全市场等权（过滤后）—— **B1，保守基准**
    mode="size_q1"   ：Q1 最小市值 20% 等权     —— **B2，上限参考**

## 实测口径（来自 §11.16 / §11.18，含退市股）

    持有 20 日、T+1 开盘买入、日等权、扣双边 0.50%
    B1 = +11.1%/年（全区间）/ +9.3%/年（样本外 2025-02 起）；抽样 30 只的长期误差仅 ±1.2%/年
    B2 = +17.7%/年（全区间）/ +13.6%/年（样本外）
    ⚠️ 2026 段 B1 是 −21.3% ⇒ **必须带 regime 状态使用**（§11.17 的门槛连 B1 自己都 reject）

## 过滤条件（与 §11.16 逐条一致）

    `pct_chg < 9.5`（当日涨停买不到）、`close ≥ 2`、`amount ≥ 1000` 千元、`circ_mv > 0`

## 设计上的两个自觉约束

1. **等距取样而非随机**：完全确定、可解释、**重复调用结果一致**（便于对照与复盘）；
   需要随机版时可传 `mode="market_ew_random"` + `seed`（用于验证"无信息选择"的分布）；
2. **本模块只读数据库、只返回数据**：不写任何生产文件、不改任何推荐决策 ——
   接入与否由使用方决定（项目纪律：先验证后生效）。

用法：
    from app.backtest.baseline_portfolio import build_baseline_picks
    r = build_baseline_picks("20260918", n=50, mode="market_ew")
    r["picks"]      # [{"ts_code": ..., "close": ...}, ...]
    r["hold_days"]  # 20
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.models import SessionLocal

N_DEFAULT = 50
HOLD_DAYS = 20
MIN_PRICE = 2.0
MIN_AMOUNT = 1000.0          # 千元
LIMIT_UP_PCT = 9.5
Q1_QUANTILE = 0.20
MODES = ("market_ew", "size_q1", "market_ew_random", "size_q1_random")
LABEL = {
    "market_ew": "B1 全市场等权（索引化等距取样）",
    "size_q1": "B2 Q1 最小市值 20% 等权（索引化等距取样）",
    "market_ew_random": "B1 全市场等权（随机取样）",
    "size_q1_random": "B2 Q1 最小市值 20% 等权（随机取样）",
}


def _load_pool(date: str) -> pd.DataFrame:
    """取某交易日的可买池（已过滤），返回 ts_code / close / amount / circ_mv / ln_mv。"""
    sql = ("SELECT d.ts_code, d.close, d.amount, d.pct_chg, b.circ_mv "
           "FROM daily d JOIN daily_basic b "
           "  ON d.ts_code = b.ts_code AND d.trade_date = b.trade_date "
           "WHERE d.trade_date = :d")
    with SessionLocal() as db:
        df = pd.read_sql(text(sql), db.connection(), params={"d": str(date)})
    if df is None or df.empty:
        return pd.DataFrame()
    for c in ("close", "amount", "pct_chg", "circ_mv"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[(df["close"] >= MIN_PRICE) & (df["amount"] >= MIN_AMOUNT)
            & (df["pct_chg"].fillna(0) < LIMIT_UP_PCT) & (df["circ_mv"] > 0)].copy()
    if df.empty:
        return df
    df["ln_mv"] = np.log(df["circ_mv"])
    return df.sort_values("ts_code", kind="mergesort").reset_index(drop=True)


def _equidistant(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """索引化等距取样：**先按 ln(circ_mv) 排序**，再等间距取 n 只。

    ⚠️ 为什么不是"按 ts_code 排序"（这是本模块第一版的写法，已被实测否掉）：
    按代码排序等距取样会**带来小市值偏差** —— 实测（20260918，池 5371 只、取 50 只）：
    ln_mv 中位差 **−0.142（≈ −13% 市值）**，而小市值组收益更高（§11.16），
    于是这份"基准"里**偷偷藏了选股**（正好是要避免的事）。
    改成按市值排序后，市值的分位分布与全池严格一致 ⇒ 才是真正的"无信息选择"。
    （板块占比偏差无论哪种排法都在 2pp 内。）
    """
    m = len(df)
    if m <= n:
        return df
    s = df.sort_values("ln_mv", kind="mergesort").reset_index(drop=True)
    idx = np.unique(np.linspace(0, m - 1, num=n).round().astype(int))
    return s.iloc[idx].sort_values("ts_code", kind="mergesort")


def _random(df: pd.DataFrame, n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    m = len(df)
    if m <= n:
        return df
    idx = rng.choice(m, size=n, replace=False)
    return df.iloc[np.sort(idx)]


def build_baseline_picks(date: str, n: int = N_DEFAULT, mode: str = "market_ew",
                         seed: int = 20260921) -> dict:
    """生成某交易日的基准组合（**只读，不写任何文件**）。

    返回 dict：date / mode / label / hold_days / pool_size / n_picked / picks / fee_rt
    """
    if mode not in MODES:
        raise ValueError(f"mode 必须是 {MODES} 之一")
    pool = _load_pool(date)
    out: dict = {
        "date": str(date), "mode": mode, "label": LABEL[mode],
        "hold_days": HOLD_DAYS, "fee_rt": 0.005,
        "pool_size": int(len(pool)), "n_picks": int(n),
        "n_picked": 0, "picks": [], "note": "",
    }
    if pool.empty:
        out["note"] = "该日无可买标的（未采集 / 全部被过滤）"
        return out
    q1 = mode.startswith("size_q1")
    if q1:
        thr = float(pool["ln_mv"].quantile(Q1_QUANTILE))
        pool = pool[pool["ln_mv"] <= thr].copy()
        out["pool_size"] = int(len(pool))
        if pool.empty:
            out["note"] = "Q1 最小市值池为空"
            return out
    sel = _random(pool, n, seed) if mode.endswith("_random") else _equidistant(pool, n)
    out["n_picked"] = int(len(sel))
    out["picks"] = [
        {"ts_code": str(r["ts_code"]), "close": float(r["close"]),
         "circ_mv": float(r["circ_mv"])}
        for _, r in sel.iterrows()
    ]
    if len(sel) < n:
        out["note"] = f"池内仅 {len(sel)} 只，少于请求的 {n} 只"
    return out


def baseline_txt(rec: dict) -> str:
    """把基准组合渲染成文本（供日志 / 前端展示，不写盘）。"""
    codes = "、".join(p["ts_code"] for p in rec.get("picks", [])[:20])
    more = "…" if len(rec.get("picks", [])) > 20 else ""
    return (f"[基准 {rec['label']}] {rec['date']}  组合 {rec['n_picked']} 只 · "
            f"持 {rec['hold_days']} 日 · 池内 {rec['pool_size']} 只\n"
            f"  样本：{codes}{more}\n"
            f"  用途：**对照基准**（不是推荐）。§11.18 实测：同日同口径下，"
            f"随机抽 30 只比我们的打分选股每年多 6.8pp。")
