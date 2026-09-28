"""四类买点分别回测（buy_types）— plans/23 §3.3 / P1 第 7 项

## 规划要求

四类买点（突破/回踩/低吸/竞价）**必须分别回测**，各自产出：
触发率、成交率（挂不上 → 统计偏差）、当日封板率（不可成交占比）、T+1/T+5 胜率与期望收益、失效模式。
并特别要求一条最容易被忽略的现实约束：**竞价买入的高开幅度分桶胜率**。

## 本模块的口径（把"能成交"这件事当真）

- **信号在 D 收盘产生，成交在 D+1**（与项目既有 D+1 买入口径一致，也避免"用当日收盘信号当当日成交"的前视偏差）；
- 价格全部用**前复权**系列（`adj_open/high/low/close`），避免除权污染收益；
- **买不到就如实记为未成交**，绝不假设"总能按理想价成交"（否则回测必然虚高）；

| 买点 | D 日触发条件 | D+1 执行与成交判定 |
| --- | --- | --- |
| ① 突破 `breakout` | 收盘 > 前 20 日最高，且量 ≥ 1.5×20 日均量（放量突破） | 高开 > 追高上限(3%) → **放弃**；否则按 D+1 开盘 +0.4% 滑点买入 |
| ② 回踩 `pullback` | 盘中触及 MA10 且收回（low ≤ MA10 ≤ close）、收阳、缩量(≤0.8×20日均量) | D+1 挂限价 = MA10；`adj_low ≤ 限价` 才成交（开盘更优则按开盘价） |
| ③ 低吸 `dip` | 20 日振幅 ≤ 15%（横盘）且收盘在 20 日均值下方 | D+1 挂限价 = D 收盘 × 0.99；同上判定 |
| ④ 竞价 `auction` | 无形态要求（全部样本） | D+1 高开幅度 ∈ [0%, +3%] 才买；> +5% 放弃 |

另：D+1 为**一字涨停封板**（`is_sealed_up`）时一律视为不可成交（真实的"买不到"）。

## 产出

- 每个买点的：触发数 / 成交数 / 成交率 / 未成交原因分布 / 从**成交价**起算的 T+1、T+5 收益分布；
- **高开幅度分桶**（<0 / 0~1% / 1~3% / 3~5% / >5%）的成交率与 T+5 —— 回答"高开多少还能追"；
- 可按环境（`regime_split`）分层。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logger import logger

BUY_TYPES = ("breakout", "pullback", "dip", "auction")
BUY_TYPE_CN = {"breakout": "①突破买入", "pullback": "②回踩买入",
               "dip": "③低吸买入", "auction": "④竞价买入",
               "baseline": "基准(无条件D+1开盘买)"}
# 基准：不加任何条件、D+1 开盘就买 —— **所有买点都必须与它比超额**，
# 否则"某买点 T+5 +0.5%"这种数字毫无意义（同期随便买可能也是 +0.5%）。
BASELINE = "baseline"
HOLD = 5                     # 持有观察窗口（交易日）
BREAKOUT_VOL_RATIO = 1.5     # 放量倍数（突破需放量）
PULLBACK_VOL_RATIO = 0.8     # 回踩需缩量
CHASE_MAX = 0.03             # 追高上限（高开超过此值放弃）
SLIPPAGE = 0.004             # 突破买入滑点（规划建议 0.3%~0.5%）
AUCTION_MAX = 0.03           # 竞价可接受的高开上界
AUCTION_ABANDON = 0.05       # 高开超过此值视为放弃
DIP_LIMIT = 0.99             # 低吸限价 = D 收盘 × 此值
PLATFORM_RANGE = 0.15        # 横盘判定：20 日振幅上限

GAP_BUCKETS = ((-9.9, 0.0, "低开(<0)"), (0.0, 0.01, "0~1%"),
               (0.01, 0.03, "1~3%"), (0.03, 0.05, "3~5%"), (0.05, 9.9, ">5%"))


@dataclass
class BuySample:
    ts_code: str = ""
    date: str = ""
    gap: float = 0.0                 # D+1 高开幅度（相对 D 收盘）
    triggered: dict = field(default_factory=dict)   # {type: bool}
    filled: dict = field(default_factory=dict)      # {type: bool}
    reason: dict = field(default_factory=dict)      # {type: 未成交原因}
    rets: dict = field(default_factory=dict)        # {type: {"t1": x, "t5": y}}

    def to_dict(self) -> dict:
        return {"ts_code": self.ts_code, "date": self.date, "gap": round(self.gap, 5),
                "triggered": self.triggered, "filled": self.filled,
                "reason": self.reason, "rets": self.rets}


def evaluate(df: pd.DataFrame, i: int, ts_code: str = "") -> BuySample | None:
    """在 df 的第 i 日（D）评估四类买点，成交在 i+1。数据不足/无 D+1 返回 None。

    **只用 i 及之前的数据做判定**；成交与收益用 i+1 及之后（这是成交/结算，不是决策）。
    """
    n = len(df)
    if i < 21 or i + 1 + HOLD >= n:
        return None
    try:
        o = pd.to_numeric(df["adj_open"], errors="coerce").to_numpy(dtype=float)
        h = pd.to_numeric(df["adj_high"], errors="coerce").to_numpy(dtype=float)
        lo = pd.to_numeric(df["adj_low"], errors="coerce").to_numpy(dtype=float)
        c = pd.to_numeric(df["adj_close"], errors="coerce").to_numpy(dtype=float)
        vol = pd.to_numeric(df["vol"], errors="coerce").to_numpy(dtype=float)
        sealed_up = (pd.to_numeric(df["is_sealed_up"], errors="coerce").fillna(0).to_numpy()
                     if "is_sealed_up" in df.columns else np.zeros(n))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[buy_types] 价格列读取失败: {exc}")
        return None

    d, nxt = i, i + 1
    if not (c[d] == c[d] and o[nxt] == o[nxt] and c[d] > 0 and o[nxt] > 0):
        return None

    ma10 = float(np.mean(c[d - 9: d + 1]))
    ma20 = float(np.mean(c[d - 19: d + 1]))
    vol20 = float(np.mean(vol[d - 19: d + 1]))
    prev_high20 = float(np.max(h[d - 20: d]))      # 含当日（突破当日高点即"新高"）
    rng20 = (float(np.max(h[d - 19: d + 1])) - float(np.min(lo[d - 19: d + 1]))) / ma20

    s = BuySample(ts_code=ts_code, date=str(df["trade_date"].iloc[d])[:10].replace("-", ""))
    s.gap = float(o[nxt] / c[d] - 1.0)

    def _settle(t: str, price: float | None, why: str) -> None:
        s.filled[t] = price is not None
        if price is None:
            s.reason[t] = why
            return
        base = float(price)
        rets = {}
        for hh in (1, HOLD):
            j = nxt + hh
            rets[f"t{hh}"] = (round(float(c[j]) / base - 1.0, 5)
                              if j < n and c[j] == c[j] and base > 0 else None)
        s.rets[t] = rets

    sealed = bool(sealed_up[nxt] >= 1)

    # 基准：无条件 D+1 开盘买入（作为"超额"比较的分母）
    s.triggered[BASELINE] = True
    if sealed:
        _settle(BASELINE, None, "封板不可买")
    else:
        _settle(BASELINE, float(o[nxt]), "")

    # ① 突破买入
    trig = bool(c[d] > prev_high20 - 1e-9 and vol20 > 0 and vol[d] >= BREAKOUT_VOL_RATIO * vol20)
    s.triggered["breakout"] = trig
    if trig:
        if sealed:
            _settle("breakout", None, "封板不可买")
        elif s.gap > CHASE_MAX:
            _settle("breakout", None, f"高开{s.gap*100:.1f}%>追高上限")
        else:
            _settle("breakout", float(o[nxt]) * (1.0 + SLIPPAGE), "")
    else:
        _settle("breakout", None, "未触发")

    # ② 回踩买入（挂限价 = MA10）
    trig = bool(lo[d] <= ma10 <= c[d] and c[d] >= o[d] and vol20 > 0
                and vol[d] <= PULLBACK_VOL_RATIO * vol20)
    s.triggered["pullback"] = trig
    if trig:
        if sealed:
            _settle("pullback", None, "封板不可买")
        elif lo[nxt] <= ma10:
            _settle("pullback", min(ma10, float(o[nxt])), "")
        else:
            _settle("pullback", None, "挂单未触及")
    else:
        _settle("pullback", None, "未触发")

    # ③ 低吸买入（挂限价 = D 收盘 × 0.99）
    limit = float(c[d]) * DIP_LIMIT
    trig = bool(rng20 <= PLATFORM_RANGE and c[d] < ma20)
    s.triggered["dip"] = trig
    if trig:
        if sealed:
            _settle("dip", None, "封板不可买")
        elif lo[nxt] <= limit:
            _settle("dip", min(limit, float(o[nxt])), "")
        else:
            _settle("dip", None, "挂单未触及")
    else:
        _settle("dip", None, "未触发")

    # ④ 竞价买入（按高开幅度决定是否参与）
    s.triggered["auction"] = True
    if sealed:
        _settle("auction", None, "封板不可买")
    elif s.gap < 0:
        _settle("auction", None, "低开不买")
    elif s.gap > AUCTION_ABANDON:
        _settle("auction", None, f"高开{s.gap*100:.1f}%>5%放弃")
    elif s.gap <= AUCTION_MAX:
        _settle("auction", float(o[nxt]), "")
    else:
        _settle("auction", None, f"高开{s.gap*100:.1f}%超3%")

    return s


def scan_stock(df: pd.DataFrame, ts_code: str, stride: int = 1,
               min_idx: int = 22, max_idx: int | None = None) -> list[dict]:
    """扫一只股票的所有评估点（stride=1 才有意义：买点看的是"每一天"）。"""
    if df is None or len(df) < min_idx + HOLD + 3:
        return []
    end = (len(df) - HOLD - 2) if max_idx is None else min(max_idx, len(df) - HOLD - 2)
    out: list[dict] = []
    for i in range(min_idx, end + 1, max(1, stride)):
        s = evaluate(df, i, ts_code=ts_code)
        if s is not None:
            out.append(s.to_dict())
    return out


def _stats(rets: list) -> dict:
    """收益列表的分布统计（None 会被剔除——未成交/数据不足不参与，不补 0）。"""
    a = np.array([x for x in rets if x is not None], dtype=float)
    if len(a) == 0:
        return {"n": 0}
    return {"n": int(len(a)), "pos": round(float((a > 0).mean()), 4),
            "gt5": round(float((a > 0.05).mean()), 4),
            "mean": round(float(a.mean()), 4), "median": round(float(np.median(a)), 4)}


def summarize(records: list[dict]) -> dict:
    """四类买点汇总（含基准）：触发/成交/原因分布 + 从成交价起算的 T+1、T+5 + **超额**。"""
    out: dict = {"total_points": len(records)}
    for t in BUY_TYPES + (BASELINE,):
        trig = [r for r in records if (r.get("triggered") or {}).get(t)]
        fill = [r for r in trig if (r.get("filled") or {}).get(t)]
        reasons: dict = {}
        for r in trig:
            if not (r.get("filled") or {}).get(t):
                why = (r.get("reason") or {}).get(t) or "其它"
                key = ("封板不可买" if "封板" in why else
                       "追高/高开放弃" if ("追高" in why or "放弃" in why or "超3%" in why) else
                       "低开不买" if "低开" in why else
                       "挂单未触及" if "未触及" in why else "未触发")
                reasons[key] = reasons.get(key, 0) + 1
        out[t] = {
            "cn": BUY_TYPE_CN[t],
            "trigger_n": len(trig),
            "fill_n": len(fill),
            "fill_rate": round(len(fill) / max(1, len(trig)), 4),
            "trigger_rate": round(len(trig) / max(1, len(records)), 4),
            "reasons": reasons,
            "t1": _stats([(r.get("rets", {}).get(t) or {}).get("t1") for r in fill]),
            "t5": _stats([(r.get("rets", {}).get(t) or {}).get("t5") for r in fill]),
        }
    # 超额（相对基准均值）：没有这个数字，四类买点的收益率不可比较
    base_t5 = (out.get(BASELINE, {}).get("t5") or {}).get("mean")
    base_t1 = (out.get(BASELINE, {}).get("t1") or {}).get("mean")
    for t in BUY_TYPES:
        m5 = (out[t]["t5"] or {}).get("mean")
        m1 = (out[t]["t1"] or {}).get("mean")
        out[t]["excess_t5"] = (round(m5 - base_t5, 4)
                               if (m5 is not None and base_t5 is not None) else None)
        out[t]["excess_t1"] = (round(m1 - base_t1, 4)
                               if (m1 is not None and base_t1 is not None) else None)
    return out


def gap_buckets(records: list[dict], buy_type: str = "auction") -> dict:
    """**高开幅度分桶**（规划特别要求）：每桶的成交率与 T+5。

    对 ④竞价 尤其关键：它直接回答"高开多少还能追、超过多少就别追了"。
    注意桶内统计的是**该桶全部触发样本**（未成交的没有收益 → 只算成交率与样本数）。
    """
    res: dict = {}
    for lo_b, hi_b, name in GAP_BUCKETS:
        g = [r for r in records if lo_b <= float(r.get("gap") or 0) < hi_b
             and (r.get("triggered") or {}).get(buy_type)]
        fill = [r for r in g if (r.get("filled") or {}).get(buy_type)]
        res[name] = {
            "n": len(g),
            "fill_n": len(fill),
            "fill_rate": round(len(fill) / max(1, len(g)), 4),
            "t5": _stats([(r.get("rets", {}).get(buy_type) or {}).get("t5") for r in fill]),
            "t1": _stats([(r.get("rets", {}).get(buy_type) or {}).get("t1") for r in fill]),
        }
    return res


__all__ = ["BUY_TYPES", "BUY_TYPE_CN", "BASELINE", "evaluate", "scan_stock", "summarize",
           "gap_buckets", "GAP_BUCKETS", "HOLD"]
