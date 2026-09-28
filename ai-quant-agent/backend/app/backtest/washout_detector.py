"""洗盘识别（washout_detector）— plans/23 §3.2（P1 第 5 项）

## 业务定义（规划原文）

洗盘 = 主力在拉升前，用回调/横盘/急杀把浮筹震出，
**量能收缩、关键支撑不破、随后快速收复**。

## 本模块做什么

把 §3.2 的"九维判别矩阵"落成**可复现的 0~1 打分**，并给出：

    washout_score / stage / sub_type / evidence / counter_evidence / coverage

外加一个**事件**：`washout_end` 确认日（地量 + 收阳 + 站上 MA5），
供样本库（P1-6）与回测消费。

## 三个必须守住的工程原则

1. **缺失维度不计入分母**（`coverage` 会随之下降）——不允许用"中性分"顶替缺数据，
   否则缺数据的股票会拿到一个"看起来正常"的分数（这就是假信号）。
2. **无未来函数**：只看 `idx` 及之前的数据；`washout_end` 的确认也只用当日及之前。
3. **必须能与"出货"区分**：`counter_evidence` 显式记录反向证据（放量下跌/破位/高位），
   stage 会给出「疑似出货」——不能只报"像洗盘"。

## ⚠️ 命名陷阱（避免后人误解）

`discipline_grid._washout()` 是**被止损洗出率**（策略副作用），
与本模块的"洗盘识别"**完全无关**，不要互相引用。

## 与每日推荐的关系（本步只做识别，不改推荐逻辑）

P1-5 只产出 `washout_score`/`stage`（供历史回填与后续接入），
**不参与** 打分与排序——接入要等 P1-6 的 A/B 证明它有统计价值（规划 §九 的护栏）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logger import logger

# ── 九维权重（和 = 1.0；缺失维度会剔除并重新归一化）────────────────
DIMS: tuple[tuple[str, float], ...] = (
    ("vol_shrink", 0.18),        # ① 量能收缩（回调期量能萎缩）
    ("retrace", 0.14),           # ② 回撤幅度（占前一波涨幅的比例）
    ("support", 0.14),           # ③ 关键支撑不破（MA5/10/20 + 前波起点）
    ("converge", 0.10),          # ④ 振幅收敛（高低点区间收窄）
    ("shadow_recover", 0.10),    # ⑤ 下影/收复（盘中急杀后收回）
    ("moneyflow", 0.12),         # ⑥ 资金（大单净额流出收窄/转正）
    ("turnover", 0.08),          # ⑦ 换手衰减
    ("position", 0.07),          # ⑧ 位置（底部/平台中，距前高有空间）
    ("time", 0.07),              # ⑨ 时间（3~15 日，过短不叫洗、过长易变盘）
)

# ── 默认阈值（可被 evolution_config 覆盖，Tier1 热生效）─────────────
SCORE_WASHOUT = 0.60       # ≥ 此分判为洗盘
SCORE_CALLBACK = 0.35      # ≥ 此分只是"回调中"（未确认）
COVERAGE_MIN = 0.60        # 有效维度权重占比低于此值 → 证据不足（不给结论）
# 硬否决（出货特征）：命中即压低分数并判"疑似出货"。
# 为什么必须否决而不是扣分：实测发现"放量 3 倍 + 8 天放量下跌 + 高位"的票
# 靠其它维度的高分（浅回撤/守住起点/换手下降）仍能拿到 0.67 分并被判成"末端地量待确认"——
# 洗盘与出货是**互斥**判断，不能让加权平均把致命信号平均掉（规划 §3.2 的出货特征就是红线）。
VETO_SCORE_CAP = 0.35
VETO_SURGE_DOWN_DAYS = 3   # 连续放量下跌 ≥3 天
VETO_PULLBACK_DAYS = 25    # 回调超过 25 日（远超洗盘 3~15 日 → 结构已变）
VOL_SHRINK_BEST = 0.50     # 回调期量能 / 起涨前量能：≤0.50 满分
VOL_SHRINK_WORST = 1.30    # ≥1.30 零分（放量 = 出货特征）
RETRACE_GOOD = 0.34        # 回撤 ≤ 前一波 1/3 满分
RETRACE_BAD = 0.67         # 回撤 ≥ 2/3 零分（规划：>2/3 且破前低 = 出货）
MIN_DAYS = 3               # 洗盘最短天数
MAX_DAYS = 15              # 洗盘最长天数
END_VOL_RATIO = 0.60       # 地量：当日量 ≤ 0.6 × 回调期均量
MA_SHORT = 5               # 确认用短均线
LOOKBACK = 60              # 找"前一波高低点"的回看窗口（交易日）
POSITION_LOW = 0.30        # 长窗口价格分位 ≤0.30 → 低位（满分）
POSITION_HIGH = 0.70       # ≥0.70 → 高位（零分）
POSITION_LOOKBACK = 250    # 位置判定窗口（≈1 年；见 _d_position 说明）
POSITION_WAVE_GAIN_GOOD = 0.60   # 前一波涨幅 ≤60%（还有空间）满分
POSITION_WAVE_GAIN_BAD = 2.00    # 涨幅 ≥200%（已透支）零分
TURNOVER_GOOD = 0.85       # 换手：后半段/前半段 ≤0.85 满分
TURNOVER_BAD = 1.15        # ≥1.15 零分（换手高企不降 = 对倒出货）

STAGE_CONFIRMED = "洗盘结束确认"
STAGE_END_WAIT = "末端地量待确认"
STAGE_WASHING = "洗盘中"
STAGE_CALLBACK = "回调中（未确认）"
STAGE_OUT = "疑似出货"
STAGE_TOO_LONG = "超窗口回调（非洗盘）"
STAGE_NONE = "无洗盘特征"


def _p(name: str, default):
    """读可进化参数（plans/23 §十二：阈值交给进化大脑统一调优）。"""
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param(name, default)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def _pf(name: str, default: float) -> float:
    try:
        return float(_p(name, default))
    except Exception:  # noqa: BLE001
        return float(default)


def _lin(v: float, best: float, worst: float) -> float:
    """线性打分：v=best → 1.0，v=worst → 0.0，中间线性插值并 clip 到 [0,1]。"""
    if v != v:  # NaN
        return np.nan
    if best == worst:
        return 1.0 if v == best else 0.0
    t = (v - worst) / (best - worst)
    return float(min(1.0, max(0.0, t)))


def _nanmean(a: np.ndarray) -> float:
    a = a[~np.isnan(a)]
    return float(a.mean()) if len(a) else np.nan


@dataclass
class WashoutResult:
    """洗盘识别结果（对齐 plans/23 §3.2 的产出物 JSON）。"""
    ts_code: str = ""
    date: str = ""
    washout_score: float = 0.0
    confirmed: bool = False            # 是否为 washout_end 确认日
    stage: str = STAGE_NONE
    sub_type: str = ""
    dims: dict = field(default_factory=dict)         # 各维得分（0~1）
    evidence: dict = field(default_factory=dict)     # 各维原始值（可审计）
    counter_evidence: list = field(default_factory=list)
    veto: list = field(default_factory=list)         # 硬否决原因（命中即判疑似出货）
    missing: list = field(default_factory=list)
    coverage: float = 0.0              # 有效维度权重占比

    def to_dict(self) -> dict:
        return {
            "ts_code": self.ts_code, "date": self.date,
            "washout_score": round(self.washout_score, 4),
            "confirmed": self.confirmed,
            "stage": self.stage, "sub_type": self.sub_type,
            "dims": {k: (None if v != v else round(float(v), 4)) for k, v in self.dims.items()},
            "evidence": self.evidence,
            "counter_evidence": self.counter_evidence,
            "veto": self.veto,
            "missing": self.missing,
            "coverage": round(self.coverage, 4),
        }


# ── 波段定位（前一波高低点 + 回调）────────────────────────────
def _locate_wave(adj: np.ndarray, idx: int, lookback: int = LOOKBACK) -> dict | None:
    """定位 idx 之前的"前一波"：低点 → 高点 → 回调至 idx。

    Returns: {hi_idx, lo_idx, wave_gain, pullback_days, retrace} 或 None（数据不足）。
    """
    lo_start = max(0, idx - lookback)
    if idx - lo_start < 10:
        return None
    seg = adj[lo_start: idx + 1]
    if np.isnan(seg).all():
        return None
    # 前一波高点：窗口内最高收盘（允许就是当日 → 回调 0 天，随后会被 time 维度压低）
    hi_rel = int(np.nanargmax(seg))
    hi_idx = lo_start + hi_rel
    if hi_idx - lo_start < 2:
        return None
    # 前一波起点：高点之前的最低收盘
    lo_rel = int(np.nanargmin(adj[lo_start: hi_idx + 1]))
    lo_idx = lo_start + lo_rel
    hi_v, lo_v = float(adj[hi_idx]), float(adj[lo_idx])
    if not (lo_v > 0 and hi_v > 0) or hi_v <= lo_v:
        return None
    drop_den = hi_v - lo_v
    cur = float(adj[idx])
    if cur != cur:
        return None
    retrace = (hi_v - cur) / drop_den if drop_den > 0 else 0.0
    return {
        "hi_idx": hi_idx, "lo_idx": lo_idx,
        "wave_gain": hi_v / lo_v - 1.0,
        "pullback_days": int(idx - hi_idx),
        "retrace": float(retrace),
        "hi_price": hi_v, "lo_price": lo_v, "cur_price": cur,
    }


# ── 九个维度 ────────────────────────────────────────────────
def _d_vol_shrink(vol: np.ndarray, w: dict, base_win: int = 5) -> tuple[float, dict]:
    hi = w["hi_idx"]
    idx = w["hi_idx"] + w["pullback_days"]
    base = _nanmean(vol[max(0, hi - base_win + 1): hi + 1])
    pull_seg = vol[hi + 1: idx + 1]
    pull = _nanmean(pull_seg)
    last = float(vol[idx]) if vol[idx] == vol[idx] else np.nan
    if base != base or base <= 0 or pull != pull:
        return np.nan, {}
    ratio = pull / base
    ev = {"vol_base": round(base, 1), "vol_pullback": round(pull, 1),
          "vol_shrink_ratio": round(ratio, 3)}
    # 放量下跌天数（出货特征）
    pct = w.get("_pct")
    if pct is not None:
        seg = slice(hi + 1, idx + 1)
        surge_dn = int(np.nansum((pct[seg] < 0) & (vol[seg] > VOL_SHRINK_WORST * base)))
        ev["vol_surge_down_days"] = surge_dn
    if last == last and last > 0:
        ev["last_vol_ratio"] = round(last / base, 3)
    best = _pf("washout_vol_shrink_best", VOL_SHRINK_BEST)
    worst = _pf("washout_vol_shrink_worst", VOL_SHRINK_WORST)
    return _lin(ratio, best, worst), ev


def _d_retrace(w: dict) -> tuple[float, dict]:
    r = float(w["retrace"])
    ev = {"retrace_ratio": round(r, 3),
          "wave_gain": round(w["wave_gain"], 4),
          "pullback_days": w["pullback_days"]}
    good = _pf("washout_retrace_good", RETRACE_GOOD)
    bad = _pf("washout_retrace_bad", RETRACE_BAD)
    return _lin(r, good, bad), ev


def _d_support(adj: np.ndarray, w: dict) -> tuple[float, dict]:
    """支撑：MA5/MA10/MA20 收盘不破 + 不破前一波起点（四条腿，各 0.25）。"""
    idx = w["hi_idx"] + w["pullback_days"]
    cur = float(adj[idx])
    held, detail = 0, {}
    for n in (MA_SHORT, 10, 20):
        if idx + 1 >= n:
            ma = _nanmean(adj[idx + 1 - n: idx + 1])
            if ma == ma and cur >= ma:
                held += 1
            detail[f"above_ma{n}"] = bool(ma == ma and cur >= ma)
        else:
            detail[f"above_ma{n}"] = None
    above_lo = cur >= w["lo_price"]
    if above_lo:
        held += 1
    detail["above_wave_low"] = bool(above_lo)
    detail["cur_vs_ma20"] = round(cur / (_nanmean(adj[max(0, idx - 19): idx + 1]) or np.nan) - 1.0, 4) \
        if idx >= 19 else None
    return held / 4.0, detail


def _d_converge(high: np.ndarray, low: np.ndarray, adj: np.ndarray, w: dict) -> tuple[float, dict]:
    """振幅收敛：回调期后半段平均振幅 / 前半段。"""
    hi, idx = w["hi_idx"], w["hi_idx"] + w["pullback_days"]
    if idx - hi < 4:
        return np.nan, {}
    rng = (high[hi + 1: idx + 1] - low[hi + 1: idx + 1]) / np.where(adj[hi + 1: idx + 1] > 0,
                                                                   adj[hi + 1: idx + 1], np.nan)
    half = len(rng) // 2
    a, b = _nanmean(rng[:half]), _nanmean(rng[half:])
    if a != a or b != b or a <= 0:
        return np.nan, {}
    ratio = b / a
    return _lin(ratio, 0.70, 1.20), {"amplitude_ratio": round(ratio, 3)}


def _d_shadow_recover(df: pd.DataFrame, w: dict) -> tuple[float, dict]:
    """下影 + 收复：盘中急杀后收回（日线近似，规划 §5.3 代理指标）。"""
    hi, idx = w["hi_idx"], w["hi_idx"] + w["pullback_days"]
    if idx - hi < 3 or not {"open", "high", "low", "close"}.issubset(df.columns):
        return np.nan, {}
    o = df["open"].to_numpy(dtype=float)[hi + 1: idx + 1]
    h = df["high"].to_numpy(dtype=float)[hi + 1: idx + 1]
    lo = df["low"].to_numpy(dtype=float)[hi + 1: idx + 1]
    c = df["close"].to_numpy(dtype=float)[hi + 1: idx + 1]
    rng = h - lo
    ok = rng > 0
    if not ok.any():
        return np.nan, {}
    lower_shadow = np.where(ok, (np.minimum(o, c) - lo) / np.where(ok, rng, np.nan), np.nan)
    ls = _nanmean(lower_shadow)
    recover = np.where(ok, (c - lo) / np.where(ok, rng, np.nan), np.nan)
    rc = _nanmean(recover[-2:])
    if ls != ls or rc != rc:
        return np.nan, {}
    score = 0.5 * _lin(ls, 0.35, 0.05) + 0.5 * _lin(rc, 0.75, 0.35)
    return float(score), {"lower_shadow_mean": round(ls, 3), "recover_last2": round(rc, 3)}


def _d_moneyflow(series: dict | None, w: dict) -> tuple[float, dict]:
    """资金：大单净额（net_amount_ratio）流出收窄 / 转正。"""
    if not series:
        return np.nan, {}
    net = series.get("net_amount_ratio")
    if net is None or len(net) == 0:
        return np.nan, {}
    hi, idx = w["hi_idx"], w["hi_idx"] + w["pullback_days"]
    seg = net[hi + 1: idx + 1]
    seg = seg[~np.isnan(seg)]
    if len(seg) < 2:
        return np.nan, {}
    half = len(seg) // 2
    a, b = float(seg[:half].mean()), float(seg[half:].mean())
    improve = b - a                     # 后半段相对前半段改善
    last = float(net[idx]) if net[idx] == net[idx] else np.nan
    score = _lin(improve, 0.05, -0.05)
    if last == last and last > 0:
        score = min(1.0, score + 0.2)
    return float(score), {"mf_first_half": round(a, 4), "mf_second_half": round(b, 4),
                          "mf_improve": round(improve, 4),
                          "mf_last": None if last != last else round(last, 4)}


def _d_turnover(series: dict | None, w: dict) -> tuple[float, dict]:
    """换手：逐日下降（后半段/前半段）。"""
    if not series:
        return np.nan, {}
    tr = series.get("turnover_rate")
    if tr is None or len(tr) == 0:
        return np.nan, {}
    hi, idx = w["hi_idx"], w["hi_idx"] + w["pullback_days"]
    seg = tr[hi + 1: idx + 1]
    seg = seg[~np.isnan(seg)]
    if len(seg) < 4:
        return np.nan, {}
    half = len(seg) // 2
    a, b = float(seg[:half].mean()), float(seg[half:].mean())
    if a <= 0:
        return np.nan, {}
    ratio = b / a
    return _lin(ratio, TURNOVER_GOOD, TURNOVER_BAD), {"turnover_ratio": round(ratio, 3)}


def _d_position(adj: np.ndarray, w: dict, lookback: int = POSITION_LOOKBACK) -> tuple[float, dict]:
    """位置：底部/平台中（距前高有空间）——**长窗口分位 + 前一波涨幅**组合。

    为什么不用 60 日分位单独衡量：浅回调（回撤小 = 好）**必然**让价格仍在 60 日高位，
    两个维度会自相矛盾（回撤越小 → 位置维越差）。实测就是这样打架的。
    改用更贴切的代理：
      - 长窗口（默认 250 日 ≈ 1 年）价格分位 → 是不是"底部区域"；
      - 前一波涨幅（wave_gain）→ 涨了多少了（涨太多 = 位置高、易兑现）。
    """
    idx = w["hi_idx"] + w["pullback_days"]
    lo = max(0, idx - lookback)
    seq = adj[lo: idx + 1]
    seq = seq[~np.isnan(seq)]
    if len(seq) < 20:
        return np.nan, {}
    cur = float(adj[idx])
    pct = float((seq <= cur).mean())
    rank_score = _lin(pct, POSITION_LOW, POSITION_HIGH)
    gain = float(w["wave_gain"])
    gain_score = _lin(gain, POSITION_WAVE_GAIN_GOOD, POSITION_WAVE_GAIN_BAD)
    score = 0.5 * rank_score + 0.5 * gain_score
    return float(score), {"price_pct_rank": round(pct, 3), "wave_gain": round(gain, 4),
                          "position_rank_score": round(rank_score, 3),
                          "position_gain_score": round(gain_score, 3)}


def _d_time(w: dict) -> tuple[float, dict]:
    """时间：3~15 日最佳；过短不叫洗，过长易变盘。"""
    d = int(w["pullback_days"])
    if d == 0:
        return 0.0, {"pullback_days": 0}
    if MIN_DAYS <= d <= MAX_DAYS:
        return 1.0, {"pullback_days": d}
    if d < MIN_DAYS:
        return _lin(d, MIN_DAYS, 0), {"pullback_days": d}
    return _lin(d, MAX_DAYS, MAX_DAYS + 12), {"pullback_days": d}


# ── 主入口 ────────────────────────────────────────────────────
def score_washout(df: pd.DataFrame, idx: int, series: dict | None = None,
                  ts_code: str = "") -> WashoutResult:
    """对 df 的第 idx 日做洗盘打分（只用 idx 及之前的数据）。

    Args:
        df: prepare_stock_data 输出（需含 adj_close / vol；high/low/open/close 用于更细维度）
        idx: 分析日下标（0-based）
        series: 可选，与 df 按行对齐的资金/换手序列
                {"net_amount_ratio": ndarray, "turnover_rate": ndarray}
        ts_code: 股票代码（仅用于结果标注）

    Returns:
        WashoutResult（证据不足时 stage=无洗盘特征 且 missing 非空、coverage 偏低）
    """
    res = WashoutResult(ts_code=ts_code)
    if df is None or len(df) == 0 or idx < 0 or idx >= len(df):
        res.missing.append("数据为空或下标越界")
        return res
    res.date = str(df["trade_date"].iloc[idx])[:10].replace("-", "")

    adj = pd.to_numeric(df["adj_close"], errors="coerce").to_numpy(dtype=float)
    w = _locate_wave(adj, idx)
    if w is None:
        res.missing.append("无法定位前一波（数据不足 10 日或序列异常）")
        return res

    vol = pd.to_numeric(df["vol"], errors="coerce").to_numpy(dtype=float) \
        if "vol" in df.columns else np.full(len(df), np.nan)
    high = pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype=float) \
        if "high" in df.columns else np.full(len(df), np.nan)
    low = pd.to_numeric(df["low"], errors="coerce").to_numpy(dtype=float) \
        if "low" in df.columns else np.full(len(df), np.nan)
    w["_pct"] = (pd.to_numeric(df["pct_chg"], errors="coerce").to_numpy(dtype=float)
                 if "pct_chg" in df.columns else None)

    raw = {
        "vol_shrink": _d_vol_shrink(vol, w),
        "retrace": _d_retrace(w),
        "support": _d_support(adj, w),
        "converge": _d_converge(high, low, adj, w),
        "shadow_recover": _d_shadow_recover(df, w),
        "moneyflow": _d_moneyflow(series, w),
        "turnover": _d_turnover(series, w),
        "position": _d_position(adj, w),
        "time": _d_time(w),
    }
    num = den = 0.0
    for dim, wgt in DIMS:
        sc, ev = raw.get(dim, (np.nan, {}))
        if sc is not None and not (isinstance(sc, float) and sc != sc):
            res.dims[dim] = float(sc)
            num += wgt * float(sc)
            den += wgt
        else:
            res.missing.append(dim)
        if ev:
            res.evidence.update(ev)
    res.coverage = round(den / sum(w for _, w in DIMS), 4)
    res.washout_score = (num / den) if den > 0 else 0.0

    # ── 反向证据（出货特征）──────────────────────────────────
    if w["retrace"] > RETRACE_BAD:
        res.counter_evidence.append(f"回撤已达前一波 {w['retrace'] * 100:.0f}%（>2/3）")
    if res.evidence.get("vol_surge_down_days"):
        res.counter_evidence.append(f"放量下跌 {res.evidence['vol_surge_down_days']} 天")
    if res.evidence.get("above_ma20") is False:
        res.counter_evidence.append("跌破 MA20")
    if res.evidence.get("turnover_ratio") and res.evidence["turnover_ratio"] >= TURNOVER_BAD:
        res.counter_evidence.append("换手高企不降（对倒出货特征）")
    if res.evidence.get("price_pct_rank") and res.evidence["price_pct_rank"] >= POSITION_HIGH:
        res.counter_evidence.append("处于近 60 日高位（洗盘可疑）")

    # ── 硬否决（出货红线）────────────────────────────────────
    # 实测教训：000011.SZ 放量 2.9×、8 天放量下跌、近 60 日 92% 分位，
    # 靠"浅回撤 + 守住起点 + 换手下降"仍拿到 0.669 分并被判"末端地量待确认"。
    # 洗盘与出货是互斥判断 → 命中红线时**不接受加权平均的高分**。
    veto: list[str] = []
    surge_dn = int(res.evidence.get("vol_surge_down_days") or 0)
    if surge_dn >= VETO_SURGE_DOWN_DAYS:
        veto.append(f"连续放量下跌 {surge_dn} 天（≥{VETO_SURGE_DOWN_DAYS}）")
    if w["retrace"] > RETRACE_BAD:
        veto.append("回撤超 2/3（结构已破）")
    if res.evidence.get("above_ma20") is False and res.evidence.get("above_wave_low") is False:
        veto.append("跌破 MA20 且破前波起点（有效跌破）")
    if (res.evidence.get("vol_shrink_ratio") or 0) > 1.0 and \
            (res.evidence.get("price_pct_rank") or 0) >= POSITION_HIGH:
        veto.append("高位放量（量不缩）")
    # 超窗口**不算"出货"**：极缩量 + 回调 27 日更像"长期低迷/结构不成立"，
    # 与"放量下跌出货"是两回事。语义必须分开，否则归因会张冠李戴。
    over_window = w["pullback_days"] > VETO_PULLBACK_DAYS
    if veto or over_window:
        res.washout_score = min(res.washout_score, VETO_SCORE_CAP)
    if over_window:
        res.counter_evidence.append(f"回调 {w['pullback_days']} 日（远超洗盘窗口 {MIN_DAYS}~{MAX_DAYS}）")
    for v in veto:
        if v not in res.counter_evidence:
            res.counter_evidence.append(v)
    res.veto = veto

    # ── 确认日（washout_end）：地量 + 收阳 + 站上 MA5 ─────────
    # 被否决的样本不可能是"洗盘结束确认"（这是互斥的）
    res.confirmed = (not veto) and _is_end_day(df, idx, adj, vol, w, res.washout_score)

    # ── stage / sub_type ────────────────────────────────────
    confirm_score = _pf("washout_score_threshold", SCORE_WASHOUT)
    callback_score = _pf("washout_callback_threshold", SCORE_CALLBACK)
    if res.coverage < COVERAGE_MIN:
        res.stage = STAGE_NONE
        res.missing.append(f"有效维度不足（coverage={res.coverage}）")
    elif veto:
        res.stage = STAGE_OUT
    elif over_window:
        res.stage = STAGE_TOO_LONG
    elif res.washout_score >= confirm_score and res.confirmed:
        res.stage = STAGE_CONFIRMED
    elif res.washout_score >= confirm_score:
        res.stage = STAGE_END_WAIT
    elif res.washout_score >= callback_score:
        res.stage = STAGE_WASHING if w["pullback_days"] >= MIN_DAYS else STAGE_CALLBACK
    else:
        res.stage = STAGE_NONE
    res.sub_type = _sub_type(res)
    return res


def _is_end_day(df: pd.DataFrame, idx: int, adj: np.ndarray, vol: np.ndarray,
                w: dict, score: float) -> bool:
    """washout_end 确认日：地量 + 收阳 + 站上 MA5（且分数达阈值）。"""
    try:
        if score < _pf("washout_score_threshold", SCORE_WASHOUT):
            return False
        hi, pull_days = w["hi_idx"], w["pullback_days"]
        if pull_days < MIN_DAYS or pull_days > MAX_DAYS:
            return False
        # 地量：当日量 ≤ END_VOL_RATIO × 回调期均量
        pull_vol = _nanmean(vol[hi + 1: idx + 1])
        last_vol = float(vol[idx])
        if pull_vol != pull_vol or last_vol != last_vol or pull_vol <= 0:
            return False
        if last_vol > _pf("washout_end_vol_ratio", END_VOL_RATIO) * pull_vol:
            return False
        # 收阳：close >= open 且 close >= pre_close
        o = float(df["open"].iloc[idx])
        c = float(df["close"].iloc[idx])
        pc = float(df["pre_close"].iloc[idx]) if "pre_close" in df.columns else np.nan
        if not (c >= o):
            return False
        if pc == pc and c < pc:
            return False
        # 站上 MA5（用 >=：贴着均线横盘的"末端地量"很常见，
        # 用严格 > 会让这类样本永远无法确认；同时已要求地量+收阳，不会因此变假信号）
        if idx + 1 < MA_SHORT:
            return False
        ma5 = _nanmean(adj[idx + 1 - MA_SHORT: idx + 1])
        return bool(ma5 == ma5 and float(adj[idx]) >= ma5)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[washout] 确认日判定失败: {exc}")
        return False


_SUB_TYPES = ("缩量回踩均线", "横盘振幅收敛", "急杀长下影后收复", "末端地量待确认")


def _sub_type(res: WashoutResult) -> str:
    """按最强维度给子类型（可解释）。"""
    d = res.dims
    if not d:
        return ""
    if d.get("shadow_recover", 0) >= 0.7:
        return "急杀长下影后收复"
    if d.get("converge", 0) >= 0.7 and d.get("vol_shrink", 0) >= 0.5:
        return "横盘振幅收敛"
    if d.get("support", 0) >= 0.7 and d.get("vol_shrink", 0) >= 0.5:
        return "缩量回踩均线"
    if res.confirmed:
        return "末端地量待确认"
    return "回调观察中"


def washout_series(ts_code: str, df: pd.DataFrame) -> dict:
    """加载并**按 df 行对齐**的资金/换手序列（缺失 → NaN，不插值）。

    只读已采集的表（复用 extra_feature_loader 的镜像优先查询）。
    """
    out: dict = {"net_amount_ratio": None, "turnover_rate": None}
    try:
        from app.backtest.extra_feature_loader import _query, _parse_dates

        key = pd.Series(_parse_dates(df["trade_date"]))

        # 换手率（daily_basic）
        try:
            dbb = _query(ts_code, "daily_basic", ["trade_date", "turnover_rate"], "trade_date")
            out["turnover_rate"] = _align(dbb, "turnover_rate", key)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[washout] turnover 加载失败 {ts_code}: {exc}")

        # 大单净额占比（moneyflow）
        try:
            mf = _query(ts_code, "moneyflow",
                        ["trade_date", "buy_sm_amount", "buy_md_amount", "buy_lg_amount",
                         "buy_elg_amount", "sell_sm_amount", "sell_md_amount", "sell_lg_amount",
                         "sell_elg_amount", "net_mf_amount"], "trade_date")
            if mf is not None and not mf.empty:
                buy = mf[["buy_sm_amount", "buy_md_amount", "buy_lg_amount", "buy_elg_amount"]] \
                    .apply(pd.to_numeric, errors="coerce").sum(axis=1)
                sell = mf[["sell_sm_amount", "sell_md_amount", "sell_lg_amount", "sell_elg_amount"]] \
                    .apply(pd.to_numeric, errors="coerce").sum(axis=1)
                tot = (buy + sell).replace(0, np.nan)
                mf = mf.assign(_net_ratio=(pd.to_numeric(mf["net_mf_amount"], errors="coerce") / tot))
                out["net_amount_ratio"] = _align(mf, "_net_ratio", key)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[washout] moneyflow 加载失败 {ts_code}: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[washout] 序列加载失败 {ts_code}: {exc}")
    return out


def _align(tbl: pd.DataFrame, col: str, key: pd.Series) -> np.ndarray | None:
    """把 (trade_date, col) 对齐到 df 的日期序列（缺失 → NaN）。

    两个坑（都踩过）：
      1. **重复日期行**：本库部分表存在重复行（Q1 事故同源问题），
         重复索引会让 `reindex` 直接抛错 → 先按日期去重（保留最后一行）；
      2. 绝不用 `ffill` 填坑：缺数据的维度就该按"缺失"处理，
         填出来的是假证据（这正是洗盘识别最怕的东西）。
    """
    if tbl is None or tbl.empty or col not in tbl.columns:
        return None
    try:
        from app.backtest.extra_feature_loader import _parse_dates
        s = pd.Series(pd.to_numeric(tbl[col], errors="coerce").to_numpy(),
                      index=pd.Series(_parse_dates(tbl["trade_date"])))
        s = s[~s.index.duplicated(keep="last")]
        return s.reindex(key).to_numpy(dtype=float)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[washout] 对齐 {col} 失败: {exc}")
        return None


def current_washout(ts_code: str, df: pd.DataFrame | None = None,
                    idx: int | None = None, series: dict | None = None) -> WashoutResult:
    """每日推荐用的入口：对最新一日（或指定 idx）做洗盘识别。"""
    if df is None:
        from app.backtest.loader import prepare_stock_data
        df = prepare_stock_data(ts_code)
    if df is None or len(df) == 0:
        r = WashoutResult(ts_code=ts_code)
        r.missing.append("无日线数据")
        return r
    if idx is None:
        idx = len(df) - 1
    if series is None:
        series = washout_series(ts_code, df)
    return score_washout(df, idx, series=series, ts_code=ts_code)


__all__ = [
    "WashoutResult", "score_washout", "current_washout", "washout_series",
    "DIMS", "STAGE_CONFIRMED", "STAGE_END_WAIT", "STAGE_WASHING",
    "STAGE_CALLBACK", "STAGE_OUT", "STAGE_TOO_LONG", "STAGE_NONE",
]
