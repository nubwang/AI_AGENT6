"""规则形态统一框架（rule_forms）

对齐规划：plans/12-网络经典形态规则库.md v4.0
  - 统一"规则形态"概念：网络经典形态（老鸭头/仙人指路…）与系统自总结规则形态，
    本质相同——都是"最近 N 天（3~10 日）的 K 线形态 → 次日（T+1）上涨概率"的可编码规则。
  - RuleForm 统一 schema：key/name/source/form_type/lookback_days/description/
    hit_rate_t1/samples/verified + detect(df, idx)
  - form_type 字段用于映射 entry_guidance.json（按 A/B/C/D 固化最佳入场）

铁律：所有 detect 只用 df.iloc[:idx+1]（idx 及之前数据），严禁未来函数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from app.core.logger import logger

# ──────────────────────────────────────────────
# 形态工具（共享，避免每个规则重复算）
# ──────────────────────────────────────────────


def _ma(arr: np.ndarray, n: int, idx: int) -> float | None:
    """最近 n 日均值（含 idx）。数据不足返回 None。"""
    if idx < 0 or n <= 0:
        return None
    win = arr[max(0, idx - n + 1): idx + 1]
    if len(win) < n:
        return None
    return float(win.mean())


def _ma_series(arr: np.ndarray, n: int) -> np.ndarray:
    """滚动 n 日均线（全序列，长度同 arr；头部不足为 NaN）。"""
    if n <= 0 or len(arr) == 0:
        return np.full(len(arr), np.nan)
    out = np.full(len(arr), np.nan)
    csum = np.cumsum(np.insert(arr, 0, 0.0))
    out[n - 1:] = (csum[n:] - csum[:-n]) / n
    return out


def _rsi(closes: np.ndarray, period: int = 14) -> float | None:
    """RSI（Wilder 简化）。数据不足返回 None。"""
    if len(closes) < period + 1:
        return None
    diff = np.diff(closes[-(period + 1):])
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def _candle(df: pd.DataFrame, idx: int) -> dict | None:
    """单根 K 线要素（前复权）。越界返回 None。"""
    if idx < 0 or idx >= len(df):
        return None
    def _g(col: str, alt: str | None = None) -> float:
        if col in df.columns:
            v = df[col].iloc[idx]
            if v == v:  # 非 NaN
                return float(v)
        if alt and alt in df.columns:
            v = df[alt].iloc[idx]
            if v == v:
                return float(v)
        return float(df["adj_close"].iloc[idx])

    close = _g("adj_close", "close")
    open_ = _g("adj_open", "open")
    high = _g("adj_high", "high")
    low = _g("adj_low", "low")
    body = close - open_
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    return {"close": close, "open": open_, "high": high, "low": low,
            "body": body, "upper": upper, "lower": lower,
            "is_up": close >= open_}


def _candles3(df: pd.DataFrame, i0: int, i1: int, i2: int) -> tuple[dict, dict, dict] | None:
    """取 3 根 K 线；任一越界返回 None（类型守卫，方便 Pylance 收窄）。"""
    c0 = _candle(df, i0)
    c1 = _candle(df, i1)
    c2 = _candle(df, i2)
    if c0 is None or c1 is None or c2 is None:
        return None
    return c0, c1, c2


def _vol_ratio(df: pd.DataFrame, idx: int, base: int = 20) -> float | None:
    """当前日量 / 近 base 日均量（不含当日）。"""
    if "vol" not in df.columns or idx <= 0:
        return None
    cur = df["vol"].iloc[idx]
    win = df["vol"].iloc[max(0, idx - base): idx]
    if len(win) == 0 or cur != cur:
        return None
    base_mean = float(win.mean())
    if base_mean <= 0:
        return None
    return float(cur) / base_mean


def _vol_ma(df: pd.DataFrame, idx: int, n: int) -> float | None:
    """近 n 日均量。"""
    if "vol" not in df.columns:
        return None
    return _ma(df["vol"].to_numpy(dtype=float), n, idx)


def _pct_change(df: pd.DataFrame, idx: int, back: int) -> float | None:
    """当前日相对 back 个交易日前（含当日）的收盘涨跌幅。"""
    if idx - back < 0:
        return None
    closes = df["adj_close"].to_numpy(dtype=float)
    base = closes[idx - back]
    if base == 0 or base != base:
        return None
    return float(closes[idx] / base - 1.0)


# ──────────────────────────────────────────────
# 特征快照（auto 规则的条件评估用）
# ──────────────────────────────────────────────

# 候选特征定义（系统自总结规则挖掘的可解释特征集）
# 每个特征 = (名称, 提取函数 fn(df, idx) -> float|None, 方向偏好说明)
CANDIDATE_FEATURES: dict[str, Callable] = {}


def _feat_vol_ratio(df, idx): return _vol_ratio(df, idx, 20)
def _feat_vol_ma_ratio(df, idx):
    """当前量 / 5 日均量。"""
    cur = df["vol"].iloc[idx] if "vol" in df.columns else None
    v5 = _vol_ma(df, idx, 5)
    if cur is None or cur != cur or not v5 or v5 <= 0:
        return None
    return float(cur) / v5


def _feat_rsi(df, idx):
    closes = df["adj_close"].to_numpy(dtype=float)
    return _rsi(closes[max(0, idx - 30): idx + 1])


def _feat_break_5d_high(df, idx):
    """当前 close 相对前 5 日最高价突破幅度（>0 即创新高）。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 5:
        return None
    prev_high = float(np.max(closes[idx - 5: idx]))
    if prev_high <= 0:
        return None
    return float(closes[idx] / prev_high - 1.0)


def _feat_ret_3d(df, idx): return _pct_change(df, idx, 3)
def _feat_ret_5d(df, idx): return _pct_change(df, idx, 5)
def _feat_ret_10d(df, idx): return _pct_change(df, idx, 10)


def _feat_body_ratio(df, idx):
    """当前 K 线实体占振幅比例（0~1）。"""
    c = _candle(df, idx)
    if c is None:
        return None
    rng = c["high"] - c["low"]
    if rng <= 1e-9:
        return None
    return float(abs(c["body"]) / rng)


def _feat_upper_shadow(df, idx):
    """上影线相对实体比例。"""
    c = _candle(df, idx)
    if c is None or abs(c["body"]) <= 1e-9:
        return None
    return float(c["upper"] / abs(c["body"]))


def _feat_ma5_above_ma10(df, idx):
    closes = df["adj_close"].to_numpy(dtype=float)
    ma5 = _ma(closes, 5, idx)
    ma10 = _ma(closes, 10, idx)
    if ma5 is None or ma10 is None:
        return None
    return float(ma5 - ma10)


def _feat_close_above_ma20(df, idx):
    closes = df["adj_close"].to_numpy(dtype=float)
    ma20 = _ma(closes, 20, idx)
    if ma20 is None:
        return None
    return float(closes[idx] / ma20 - 1.0)


CANDIDATE_FEATURES = {
    "vol_ratio_20": _feat_vol_ratio,
    "vol_ratio_5": _feat_vol_ma_ratio,
    "rsi_14": _feat_rsi,
    "break_5d_high": _feat_break_5d_high,
    "ret_3d": _feat_ret_3d,
    "ret_5d": _feat_ret_5d,
    "ret_10d": _feat_ret_10d,
    "body_ratio": _feat_body_ratio,
    "upper_shadow": _feat_upper_shadow,
    "ma5_minus_ma10": _feat_ma5_above_ma10,
    "close_above_ma20": _feat_close_above_ma20,
}


def extract_feature_snapshot(df: pd.DataFrame, idx: int) -> dict:
    """提取 idx 处可解释特征快照（auto 规则条件评估用，只用 idx 及之前数据）。"""
    snap: dict = {}
    for name, fn in CANDIDATE_FEATURES.items():
        try:
            v = fn(df, int(idx))
            snap[name] = float(v) if v is not None else None
        except Exception:  # noqa: BLE001
            snap[name] = None
    return snap


# ──────────────────────────────────────────────
# RuleForm 统一 schema
# ──────────────────────────────────────────────


@dataclass
class ThresholdCondition:
    """阈值条件（auto 规则用）：feature >=/<= threshold。"""
    feature: str            # 特征名（见 extract_feature_snapshot）
    threshold: float
    direction: str = "gte"  # "gte"（>=）或 "lte"（<=）

    def eval(self, snapshot: dict) -> bool:
        v = snapshot.get(self.feature)
        if v is None or v != v:  # 缺失/NaN
            return False
        return float(v) >= self.threshold if self.direction == "gte" else float(v) <= self.threshold

    def to_dict(self) -> dict:
        return {"feature": self.feature, "threshold": self.threshold, "direction": self.direction}


@dataclass
class RuleForm:
    """规则形态（统一框架）。"""
    key: str                      # 唯一标识：网络经典 "laoyatou" / 自总结 "auto_0001"
    name: str                     # 可读名："老鸭头"
    source: str                   # "network"（网络经典）/ "auto"（系统自总结）
    form_type: str                # 所属启动形态 A/B/C/D/U，用于映射 entry_guidance
    lookback_days: int            # 判定窗口（最近 N 天）
    description: str              # 人类可读描述（可解释性）
    bullish: bool = True          # True=看涨（进正信号）；False=看跌（反向排除）
    detect_fn: Callable | None = None   # fn(df, idx) -> bool（network 规则用）
    conditions: list[ThresholdCondition] = field(default_factory=list)  # auto 规则用
    # 回测验证结果（运行时填充，不参与 detect）
    hit_rate_t1: float = 0.0      # 次日上涨率
    samples: int = 0              # 可交易样本量
    verified: bool = False        # 通过 45% 门槛 + 基线 + 显著性 + 样本外
    tradeable_ratio: float = 0.0  # 可交易比例
    avg_ret_t1: float = 0.0       # 平均次日收益
    # 运行时缓存（防未来函数检查）
    _cache: dict = field(default_factory=dict, repr=False)

    def detect(self, df: pd.DataFrame, idx: int) -> bool:
        """判定当前 idx 是否命中该形态（只用 idx 及之前数据）。"""
        try:
            if self.conditions:
                # auto 规则：提取特征快照 → 全部阈值条件满足即命中
                snap = extract_feature_snapshot(df, int(idx))
                return all(c.eval(snap) for c in self.conditions)
            if self.detect_fn is None:
                return False
            return bool(self.detect_fn(df, int(idx)))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"RuleForm[{self.key}].detect 失败 idx={idx}: {exc}")
            return False

    def to_dict(self) -> dict:
        d = {
            "key": self.key,
            "name": self.name,
            "source": self.source,
            "form_type": self.form_type,
            "lookback_days": self.lookback_days,
            "description": self.description,
            "bullish": self.bullish,
            "hit_rate_t1": round(self.hit_rate_t1, 4),
            "samples": self.samples,
            "verified": self.verified,
            "tradeable_ratio": round(self.tradeable_ratio, 4),
            "avg_ret_t1": round(self.avg_ret_t1, 4),
        }
        if self.conditions:
            d["conditions"] = [c.to_dict() for c in self.conditions]
        return d

# ──────────────────────────────────────────────
# 网络经典形态 detect 实现（P1 首批 15 个）
# ──────────────────────────────────────────────

# 判定阈值
_MA5, _MA10, _MA20 = 5, 10, 20
_DUCK_CROSS_LOOKBACK = 8       # 老鸭头：金叉回看窗口
_DUCK_BODY_UP = 0.02           # 老鸭头：再上攻日涨幅
_VOL_BOOST = 1.2               # 老鸭头：量能放大倍数


def _detect_laoyatou(df, idx):
    """老鸭头：5 日线上穿 10 日线 → 回踩 10/20 日线不破 → 5 日线再上拐 + 放量。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < _MA20:
        return False
    ma5 = _ma_series(closes, _MA5)
    ma10 = _ma_series(closes, _MA10)
    ma20 = _ma_series(closes, _MA20)
    if np.isnan(ma5[idx]) or np.isnan(ma10[idx]) or np.isnan(ma20[idx]):
        return False
    # 1. 近 DUCK_CROSS_LOOKBACK 日内存在 5 上穿 10（金叉）
    crossed = False
    for i in range(max(_MA10, idx - _DUCK_CROSS_LOOKBACK), idx + 1):
        if i >= _MA10 and not np.isnan(ma5[i]) and not np.isnan(ma10[i]):
            if ma5[i - 1] <= ma10[i - 1] and ma5[i] > ma10[i]:
                crossed = True
                break
    if not crossed:
        return False
    # 2. 金叉后回踩：当前 5 日线 >= 10 日线 且 10 日线 > 20 日线（多头）
    if ma5[idx] < ma10[idx] or ma10[idx] <= ma20[idx]:
        return False
    # 3. 再上攻：当前日 5 日线拐头向上且 close 站上 5 日线
    if idx >= 1 and not np.isnan(ma5[idx - 1]):
        if ma5[idx] <= ma5[idx - 1]:
            return False
    if closes[idx] <= ma5[idx]:
        return False
    # 4. 量能放大
    vr = _vol_ratio(df, idx)
    if vr is not None and vr < _VOL_BOOST:
        return False
    return True


def _detect_xianren(df, idx):
    """仙人指路：前一日长上影试盘（上影≥实体2倍），当前日不破前日实体且收阳。"""
    if idx < 2:
        return False
    prev = _candle(df, idx - 1)
    cur = _candle(df, idx)
    if prev is None or cur is None:
        return False
    body_abs = abs(prev["body"])
    if body_abs <= 1e-9:
        return False
    if prev["upper"] < 2 * body_abs:      # 上影 ≥ 实体 2 倍
        return False
    if not prev["is_up"]:                  # 前日应收阳（试盘）
        return False
    # 当前日不破前日实体 1/2，且收阳
    prev_mid = (prev["close"] + prev["open"]) / 2
    if cur["low"] < prev_mid:
        return False
    if not cur["is_up"]:
        return False
    # 上升趋势背景（简化：close 站上 10 日线）
    closes = df["adj_close"].to_numpy(dtype=float)
    ma10 = _ma(closes, _MA10, idx)
    if ma10 is None or closes[idx] < ma10:
        return False
    return True


def _detect_chushuifurong(df, idx):
    """出水芙蓉：一根放量大阳一举突破 5/10/20 日均线（一阳穿三线）。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < _MA20:
        return False
    ma5 = _ma(closes, _MA5, idx)
    ma10 = _ma(closes, _MA10, idx)
    ma20 = _ma(closes, _MA20, idx)
    if None in (ma5, ma10, ma20):
        return False
    c = _candle(df, idx)
    if c is None or not c["is_up"]:
        return False
    if c["close"] <= ma5 or c["close"] <= ma10 or c["close"] <= ma20:
        return False
    # 前一日应低于 5/10 日线（突破前）
    if idx >= 1:
        prev_close = closes[idx - 1]
        if prev_close >= _ma(closes, _MA5, idx - 1) or prev_close >= _ma(closes, _MA10, idx - 1):
            return False
    vr = _vol_ratio(df, idx)
    if vr is not None and vr < 1.5:
        return False
    return True


def _detect_hongsanbing(df, idx):
    """红三兵：连续 3 根阳线，依次收高，量能温和放大。"""
    if idx < 3:
        return False
    cs = _candles3(df, idx - 2, idx - 1, idx)
    if cs is None:
        return False
    c0, c1, c2 = cs
    for c in (c0, c1, c2):
        if not c["is_up"] or c["body"] <= 0:
            return False
    if not (c2["close"] > c1["close"] > c0["close"]):
        return False
    # 量能温和放大（第三日量 ≥ 第一日量）
    vol = df["vol"].to_numpy(dtype=float)
    if vol[idx] < vol[idx - 2]:
        return False
    return True


def _detect_duofangpao(df, idx):
    """多方炮：两阳夹一阴（中间阴线缩量），后阳放量收复。"""
    if idx < 3:
        return False
    cs = _candles3(df, idx - 2, idx - 1, idx)
    if cs is None:
        return False
    c0, c1, c2 = cs  # 阳 / 阴 / 阳
    if not c0["is_up"] or c1["is_up"] or not c2["is_up"]:
        return False
    if c2["close"] <= c0["close"]:
        return False
    vol = df["vol"].to_numpy(dtype=float)
    if vol[idx - 1] >= vol[idx - 2]:     # 中间阴线应缩量
        return False
    if vol[idx] <= vol[idx - 1]:          # 后阳应放量
        return False
    return True


def _detect_zaochen(df, idx):
    """早晨之星：长阴 → 小实体（十字）→ 长阳收复前阴 1/2。"""
    if idx < 3:
        return False
    cs = _candles3(df, idx - 2, idx - 1, idx)
    if cs is None:
        return False
    c0, c1, c2 = cs
    if c0["is_up"] or abs(c0["body"]) <= 0:
        return False
    if abs(c1["body"]) > 0.5 * abs(c0["body"]):   # 中间应小实体
        return False
    if not c2["is_up"]:
        return False
    if c2["close"] < (c0["open"] + c0["close"]) / 2:  # 收复前阴 1/2
        return False
    return True


def _detect_jinzhen(df, idx):
    """金针探底/锤头线：下影 ≥ 实体 2 倍，出现在下跌末端，实体收高。"""
    if idx < 5:
        return False
    c = _candle(df, idx)
    if c is None or not c["is_up"]:
        return False
    body_abs = abs(c["body"])
    if body_abs <= 1e-9:
        return False
    if c["lower"] < 2 * body_abs:
        return False
    # 下跌末端：前 5 日累计下跌
    closes = df["adj_close"].to_numpy(dtype=float)
    if closes[idx - 5] <= closes[idx - 1]:
        return False
    return True


def _detect_shuangdi(df, idx):
    """双底/W 底（简化）：近 20 日两度探底（两低点相近），当前日放量突破两底之间颈线。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 20:
        return False
    win = closes[idx - 20: idx + 1]
    # 找两低点：最低点 + 次低点
    if len(win) < 10:
        return False
    m1 = float(np.min(win))
    # 排除最低点附近，找第二个低
    i1 = int(np.argmin(win))
    masked = win.copy()
    masked[max(0, i1 - 3): i1 + 4] = np.inf
    if np.isinf(masked).all():
        return False
    i2 = int(np.argmin(masked))
    m2 = float(masked[i2])
    # 两低点相近（相差 < 5%）
    if m1 <= 0 or abs(m1 - m2) / m1 > 0.05:
        return False
    neck = min(m1, m2) * 1.03
    # 当前日突破颈线且放量
    if closes[idx] < neck:
        return False
    vr = _vol_ratio(df, idx)
    if vr is not None and vr < 1.2:
        return False
    return True


def _detect_toujianbe(df, idx):
    """头肩底（简化）：左肩→更低头→右肩（不高出头），当前日突破颈线。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 25:
        return False
    win = closes[idx - 25: idx + 1]
    if len(win) < 15:
        return False
    head_i = int(np.argmin(win))
    head = float(win[head_i])
    left = win[: head_i]
    right = win[head_i + 1:]
    if len(left) < 4 or len(right) < 4:
        return False
    left_shoulder = float(np.min(left[-4:]))
    right_shoulder = float(np.min(right[:4]))
    # 头更低，两肩相近且高于头
    if head >= left_shoulder or head >= right_shoulder:
        return False
    if abs(left_shoulder - right_shoulder) / max(left_shoulder, 1e-9) > 0.08:
        return False
    neck = max(left_shoulder, right_shoulder)
    if closes[idx] < neck:
        return False
    return True


def _detect_duotoupailie(df, idx):
    """均线多头排列：5 > 10 > 20 > 60 日线，且 close 站上 5 日线。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 60:
        return False
    ma5 = _ma(closes, 5, idx)
    ma10 = _ma(closes, 10, idx)
    ma20 = _ma(closes, 20, idx)
    ma60 = _ma(closes, 60, idx)
    if ma5 is None or ma10 is None or ma20 is None or ma60 is None:
        return False
    if not (ma5 > ma10 > ma20 > ma60):
        return False
    if closes[idx] < ma5:
        return False
    return True


def _detect_suolianghuicai(df, idx):
    """缩量回踩：上升趋势中回调缩量回踩 5/10 日线获支撑再收阳。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 15:
        return False
    ma5 = _ma(closes, 5, idx)
    ma10 = _ma(closes, 10, idx)
    if ma5 is None or ma10 is None:
        return False
    c = _candle(df, idx)
    if c is None or not c["is_up"]:
        return False
    # 上升趋势：20 日线向上（20 日均值 > 10 日均值 > 5 日均值 或 close 高于 20 日线）
    ma20 = _ma(closes, 20, idx)
    if ma20 is None or closes[idx] < ma20:
        return False
    # 回踩支撑：近 3 日最低触及 5/10 日线附近（-2% 容差）
    near_support = False
    for i in range(max(0, idx - 2), idx + 1):
        m5i = _ma(closes, 5, i)
        m10i = _ma(closes, 10, i)
        low_i = df["adj_low"].iloc[i] if "adj_low" in df.columns else closes[i]
        if m5i is not None and low_i <= m5i * 1.02:
            near_support = True
            break
        if m10i is not None and low_i <= m10i * 1.02:
            near_support = True
            break
    if not near_support:
        return False
    # 缩量：当前量 < 近 20 日均量
    vma = _vol_ma(df, idx, 20)
    vr = _vol_ratio(df, idx)
    if vma is not None and vr is not None and vr >= 1.0:
        return False
    return True


# ── 看跌形态（反向排除用） ──


def _detect_hunhuang(df, idx):
    """黄昏之星：长阳 → 小实体 → 长阴跌破前阳 1/2。"""
    if idx < 3:
        return False
    cs = _candles3(df, idx - 2, idx - 1, idx)
    if cs is None:
        return False
    c0, c1, c2 = cs
    if not c0["is_up"] or abs(c0["body"]) <= 0:
        return False
    if abs(c1["body"]) > 0.5 * abs(c0["body"]):
        return False
    if c2["is_up"]:
        return False
    if c2["close"] > (c0["open"] + c0["close"]) / 2:
        return False
    return True


def _detect_sanzhiwuya(df, idx):
    """三只乌鸦：连续 3 根阴线，依次收低。"""
    if idx < 3:
        return False
    cs = _candles3(df, idx - 2, idx - 1, idx)
    if cs is None:
        return False
    c0, c1, c2 = cs
    for c in (c0, c1, c2):
        if c["is_up"] or c["body"] >= 0:
            return False
    if not (c2["close"] < c1["close"] < c0["close"]):
        return False
    return True


def _detect_wuyungaiding(df, idx):
    """乌云盖顶：长阳后次日高开低走，阴线实体吞没前阳 1/2 以上。"""
    if idx < 2:
        return False
    c0 = _candle(df, idx - 1)
    c1 = _candle(df, idx)
    if c0 is None or c1 is None:
        return False
    if not c0["is_up"] or c1["is_up"] or abs(c0["body"]) <= 0:
        return False
    if c1["open"] < c0["close"]:       # 高开
        return False
    if c1["close"] > (c0["open"] + c0["close"]) / 2:  # 未吞没前阳 1/2
        return False
    return True


# ── 扩展形态（P6：补齐塔形底/三重底/圆弧底/放量突破等，扩充到 30+） ──


def _detect_taxingdi(df, idx):
    """塔形底：长阴急跌 → 小实体筑底 → 长阳拉起。"""
    if idx < 8:
        return False
    cs0 = _candle(df, idx - 6)
    cs1 = _candle(df, idx - 3)
    cs2 = _candle(df, idx)
    if cs0 is None or cs1 is None or cs2 is None:
        return False
    c0, c3, c6 = cs0, cs1, cs2
    if c0["is_up"] or abs(c0["body"]) <= 0:
        return False
    # 中间小实体筑底
    for i in range(idx - 5, idx):
        c = _candle(df, i)
        if c is None:
            return False
        if abs(c["body"]) > 0.5 * abs(c0["body"]):
            return False
    if not c6["is_up"] or c6["close"] < (c0["open"] + c0["close"]) / 2:
        return False
    return True


def _detect_sanchongdi(df, idx):
    """三重底：三次探底同水平（±3%），当前日突破颈线。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 25:
        return False
    win = closes[idx - 25: idx + 1]
    # 三次探底：取 3 个局部极小值
    lows = []
    for i in range(1, len(win) - 1):
        if win[i] <= win[i - 1] and win[i] <= win[i + 1]:
            lows.append(float(win[i]))
    if len(lows) < 3:
        return False
    lows = sorted(lows)[:3]
    spread = (max(lows) - min(lows)) / max(min(lows), 1e-9)
    if spread > 0.03:
        return False
    neck = max(lows) * 1.03
    if closes[idx] < neck:
        return False
    return True


def _detect_yuanhudi(df, idx):
    """圆弧底（简化）：近 20 日形态呈 U 形（中部最低、两端较高），当前日突破右端。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 20:
        return False
    win = closes[idx - 20: idx + 1]
    mid_i = int(np.argmin(win))
    if mid_i < 4 or mid_i > len(win) - 5:
        return False
    left = win[:mid_i + 1]
    right = win[mid_i:]
    if len(left) < 4 or len(right) < 4:
        return False
    # 两端明显高于中部
    if win[mid_i] >= left[0] * 0.97 or win[mid_i] >= right[-1] * 0.97:
        return False
    if closes[idx] < right[0]:
        return False
    return True


def _detect_fangliangtupo(df, idx):
    """放量突破：close 创 20 日新高 + 量能 ≥1.5 倍 20 日均量。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 20:
        return False
    prev_high = float(np.max(closes[idx - 20: idx]))
    if closes[idx] <= prev_high:
        return False
    vr = _vol_ratio(df, idx, 20)
    if vr is None or vr < 1.5:
        return False
    c = _candle(df, idx)
    if c is None or not c["is_up"]:
        return False
    return True


def _detect_liangjiaqisheng(df, idx):
    """量价齐升：连续 3 日价升 + 量增。"""
    if idx < 3:
        return False
    cs = _candles3(df, idx - 2, idx - 1, idx)
    if cs is None:
        return False
    c0, c1, c2 = cs
    closes = [c0["close"], c1["close"], c2["close"]]
    if not (closes[2] > closes[1] > closes[0]):
        return False
    vol = df["vol"].to_numpy(dtype=float)
    if not (vol[idx] > vol[idx - 1] > vol[idx - 2]):
        return False
    return True


def _detect_jinshangu(df, idx):
    """金山谷：20 日线上穿 60 日线（二次金叉更可靠），当前站上 20 日线。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 60:
        return False
    ma20 = _ma_series(closes, 20)
    ma60 = _ma_series(closes, 60)
    if np.isnan(ma20[idx]) or np.isnan(ma60[idx]):
        return False
    # 近 10 日内出现 20 上穿 60
    crossed = False
    for i in range(max(60, idx - 10), idx + 1):
        if not np.isnan(ma20[i - 1]) and not np.isnan(ma60[i - 1]) and not np.isnan(ma20[i]) and not np.isnan(ma60[i]):
            if ma20[i - 1] <= ma60[i - 1] and ma20[i] > ma60[i]:
                crossed = True
                break
    if not crossed:
        return False
    if closes[idx] < ma20[idx]:
        return False
    return True


def _detect_haidilaoyue(df, idx):
    """海底捞月/均线粘合：5/10/20 日均线粘合后向上发散，放量突破。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 20:
        return False
    ma5 = _ma(closes, 5, idx)
    ma10 = _ma(closes, 10, idx)
    ma20 = _ma(closes, 20, idx)
    if ma5 is None or ma10 is None or ma20 is None:
        return False
    # 前 5 日粘合（三条均线接近），当前发散向上
    spread_prev = 0.0
    for i in range(max(5, idx - 4), idx + 1):
        m5 = _ma(closes, 5, i)
        m10 = _ma(closes, 10, i)
        m20 = _ma(closes, 20, i)
        if m5 is None or m10 is None or m20 is None:
            continue
        spread_prev = max(spread_prev, (max(m5, m10, m20) - min(m5, m10, m20)) / max(m20, 1e-9))
    if spread_prev > 0.05:
        return False
    if not (ma5 > ma10 > ma20):
        return False
    vr = _vol_ratio(df, idx, 20)
    if vr is None or vr < 1.3:
        return False
    return True


def _detect_daochuizhui(df, idx):
    """倒锤头线：上影长、实体小、出现在底部，次日高开确认。"""
    if idx < 6:
        return False
    c = _candle(df, idx - 1)  # 倒锤头
    nxt = _candle(df, idx)    # 次日确认
    if c is None or nxt is None:
        return False
    body_abs = abs(c["body"])
    if body_abs <= 1e-9:
        return False
    if c["upper"] < 2 * body_abs:
        return False
    if not nxt["is_up"] or nxt["open"] < c["close"]:
        return False
    closes = df["adj_close"].to_numpy(dtype=float)
    if closes[idx - 5] <= closes[idx - 1]:
        return False
    return True


def _detect_shuguang(df, idx):
    """曙光初现：长阴后次日低开高走，阳线收复前阴实体 1/2 以上。"""
    if idx < 2:
        return False
    cs = _candles3(df, idx - 1, idx, idx)
    if cs is None:
        return False
    c0, c1, _ = cs
    if c0["is_up"] or abs(c0["body"]) <= 0:
        return False
    if not c1["is_up"] or c1["open"] >= c0["close"]:
        return False
    if c1["close"] < (c0["open"] + c0["close"]) / 2:
        return False
    return True


def _detect_kantunmo(df, idx):
    """看涨吞没：阴线后阳线实体完全吞没前阴实体。"""
    if idx < 2:
        return False
    cs = _candles3(df, idx - 1, idx, idx)
    if cs is None:
        return False
    c0, c1, _ = cs
    if c0["is_up"] or c1["is_up"] is False:
        return False
    if abs(c1["body"]) <= 1e-9 or c1["body"] <= 0:
        return False
    if c1["open"] > c0["close"] or c1["close"] < c0["open"]:
        return False
    if abs(c1["body"]) <= abs(c0["body"]):
        return False
    return True


def _detect_sanyangguan(df, idx):
    """三阳开泰：底部区域连续 3 根阳线逐步抬高，量能递增。"""
    if idx < 3:
        return False
    cs = _candles3(df, idx - 2, idx - 1, idx)
    if cs is None:
        return False
    c0, c1, c2 = cs
    for c in (c0, c1, c2):
        if not c["is_up"] or c["body"] <= 0:
            return False
    if not (c2["close"] > c1["close"] > c0["close"]):
        return False
    vol = df["vol"].to_numpy(dtype=float)
    if not (vol[idx] > vol[idx - 1] > vol[idx - 2]):
        return False
    return True


def _detect_shangsanshi(df, idx):
    """上升三法：阳线后 3 根小阴线缩量回调不破前阳低点，再一根阳线收复。"""
    if idx < 5:
        return False
    c0 = _candle(df, idx - 4)  # 首阳
    c1 = _candle(df, idx - 3)  # 小阴
    c2 = _candle(df, idx - 2)
    c3 = _candle(df, idx - 1)
    c4 = _candle(df, idx)      # 末阳
    if c0 is None or c1 is None or c2 is None or c3 is None or c4 is None:
        return False
    if not c0["is_up"] or c4["is_up"] is False or c4["body"] <= 0:
        return False
    for c in (c1, c2, c3):
        if c["is_up"]:
            return False
    # 回调不破首阳低点
    if min(c1["low"], c2["low"], c3["low"]) < c0["low"]:
        return False
    if c4["close"] < c0["close"]:
        return False
    return True


# ── 扩展看跌形态 ──


def _detect_shejixing(df, idx):
    """射击之星：长上影、小实体，出现在高位。"""
    if idx < 10:
        return False
    c = _candle(df, idx)
    if c is None:
        return False
    body_abs = abs(c["body"])
    if body_abs <= 1e-9:
        return False
    if c["upper"] < 2 * body_abs:
        return False
    closes = df["adj_close"].to_numpy(dtype=float)
    if closes[idx] < float(np.max(closes[idx - 10: idx])):
        return False
    return True


def _detect_emutou(df, idx):
    """M 头（双顶）：两次冲高相近（±3%），当前跌破颈线。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 20:
        return False
    win = closes[idx - 20: idx + 1]
    highs = []
    for i in range(1, len(win) - 1):
        if win[i] >= win[i - 1] and win[i] >= win[i + 1]:
            highs.append(float(win[i]))
    if len(highs) < 2:
        return False
    highs = sorted(highs, reverse=True)[:2]
    if (highs[0] - highs[1]) / max(highs[1], 1e-9) > 0.03:
        return False
    neck = max(highs) * 0.97
    if closes[idx] > neck:
        return False
    return True


def _detect_toujianding(df, idx):
    """头肩顶：左肩→头→右肩（不高过左肩太多），当前跌破颈线。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 25:
        return False
    win = closes[idx - 25: idx + 1]
    head_i = int(np.argmax(win))
    head = float(win[head_i])
    left = win[:head_i]
    right = win[head_i + 1:]
    if len(left) < 4 or len(right) < 4:
        return False
    left_shoulder = float(np.max(left[-4:]))
    right_shoulder = float(np.max(right[:4]))
    if head <= left_shoulder or head <= right_shoulder:
        return False
    if abs(left_shoulder - right_shoulder) / max(left_shoulder, 1e-9) > 0.08:
        return False
    neck = min(left_shoulder, right_shoulder)
    if closes[idx] > neck:
        return False
    return True


def _detect_yuanhuding(df, idx):
    """圆弧顶（简化）：近 20 日形态呈倒 U 形（中部最高、两端较低），当前跌破。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 20:
        return False
    win = closes[idx - 20: idx + 1]
    mid_i = int(np.argmax(win))
    if mid_i < 4 or mid_i > len(win) - 5:
        return False
    if win[mid_i] <= win[0] * 1.03 or win[mid_i] <= win[-1] * 1.03:
        return False
    if closes[idx] > win[0]:
        return False
    return True


def _detect_junxiansicha(df, idx):
    """均线死叉：5 日线下穿 10 日线。"""
    closes = df["adj_close"].to_numpy(dtype=float)
    if idx < 10:
        return False
    ma5 = _ma_series(closes, 5)
    ma10 = _ma_series(closes, 10)
    if np.isnan(ma5[idx]) or np.isnan(ma10[idx]):
        return False
    if ma5[idx - 1] >= ma10[idx - 1] and ma5[idx] < ma10[idx]:
        return True
    return False


def _detect_kantunmoyin(df, idx):
    """看跌吞没：阳线后阴线实体完全吞没前阳实体。"""
    if idx < 2:
        return False
    cs = _candles3(df, idx - 1, idx, idx)
    if cs is None:
        return False
    c0, c1, _ = cs
    if not c0["is_up"] or c1["is_up"]:
        return False
    if abs(c1["body"]) <= 1e-9:
        return False
    if c1["open"] < c0["close"] or c1["close"] > c0["open"]:
        return False
    if abs(c1["body"]) <= abs(c0["body"]):
        return False
    return True


# ──────────────────────────────────────────────
# RULE_FORMS 注册表
# ──────────────────────────────────────────────


def _build_network_forms() -> list[RuleForm]:
    """网络经典看涨形态（用户确认：去掉看跌形态，仅保留看涨，聚焦 T+1 上涨概率）。"""
    return [
        # 看涨（K 线 / 均线 / 量价）
        RuleForm("laoyatou", "老鸭头", "network", "A", 8,
                 "5日线上穿10日线后回踩不破再上攻+放量", True, _detect_laoyatou),
        RuleForm("xianren", "仙人指路", "network", "D", 3,
                 "长上影试盘+次日不破位收阳", True, _detect_xianren),
        RuleForm("chushuifurong", "出水芙蓉", "network", "A", 3,
                 "一阳穿三线(5/10/20)+放量", True, _detect_chushuifurong),
        RuleForm("hongsanbing", "红三兵", "network", "D", 3,
                 "连续3根阳线依次收高", True, _detect_hongsanbing),
        RuleForm("duofangpao", "多方炮", "network", "D", 3,
                 "两阳夹一阴+中间缩量后阳放量", True, _detect_duofangpao),
        RuleForm("zaochenzhixing", "早晨之星", "network", "B", 3,
                 "长阴→小实体→长阳收复前阴1/2", True, _detect_zaochen),
        RuleForm("jinzhentandi", "金针探底", "network", "B", 5,
                 "下影≥实体2倍+下跌末端", True, _detect_jinzhen),
        RuleForm("shuangdi", "双底/W底", "network", "A", 20,
                 "两度探底相近+放量突破颈线", True, _detect_shuangdi),
        RuleForm("toujianbe", "头肩底", "network", "A", 25,
                 "左肩→头→右肩+突破颈线", True, _detect_toujianbe),
        RuleForm("duotoupailie", "均线多头排列", "network", "C", 60,
                 "5>10>20>60日线+站上5日线", True, _detect_duotoupailie),
        RuleForm("suolianghuicai", "缩量回踩", "network", "A", 15,
                 "上升趋势缩量回踩5/10日线获支撑", True, _detect_suolianghuicai),
        # P6 扩充：看涨（塔形底/三重底/圆弧底/放量突破/量价齐升/金山谷/海底捞月/
        #          倒锤头线/曙光初现/看涨吞没/三阳开泰/上升三法）
        RuleForm("taxingdi", "塔形底", "network", "B", 7,
                 "长阴急跌→小实体筑底→长阳拉起", True, _detect_taxingdi),
        RuleForm("sanchongdi", "三重底", "network", "A", 25,
                 "三次探底同水平+突破颈线", True, _detect_sanchongdi),
        RuleForm("yuanhudi", "圆弧底", "network", "A", 20,
                 "U形底部+突破右端", True, _detect_yuanhudi),
        RuleForm("fangliangtupo", "放量突破", "network", "A", 20,
                 "创20日新高+量≥1.5倍", True, _detect_fangliangtupo),
        RuleForm("liangjiaqisheng", "量价齐升", "network", "D", 3,
                 "连续3日价升量增", True, _detect_liangjiaqisheng),
        RuleForm("jinshangu", "金山谷", "network", "C", 60,
                 "20日线上穿60日线+站上20日线", True, _detect_jinshangu),
        RuleForm("haidilaoyue", "海底捞月", "network", "C", 20,
                 "均线粘合后向上发散+放量", True, _detect_haidilaoyue),
        RuleForm("daochuizhui", "倒锤头线", "network", "B", 6,
                 "上影长小实体+次日高开确认", True, _detect_daochuizhui),
        RuleForm("shuguang", "曙光初现", "network", "B", 2,
                 "长阴后低开高走收复1/2", True, _detect_shuguang),
        RuleForm("kantunmo", "看涨吞没", "network", "D", 2,
                 "阳线实体吞没前阴", True, _detect_kantunmo),
        RuleForm("sanyangguan", "三阳开泰", "network", "D", 3,
                 "底部3阳线抬高+量增", True, _detect_sanyangguan),
        RuleForm("shangsanshi", "上升三法", "network", "D", 5,
                 "阳后3小阴不破低点再收复", True, _detect_shangsanshi),
    ]


# 注册表（回测/每日推荐共用）
RULE_FORMS: list[RuleForm] = _build_network_forms()

# key 索引
RULE_FORMS_BY_KEY: dict[str, RuleForm] = {r.key: r for r in RULE_FORMS}


def get_rule_form(key: str) -> RuleForm | None:
    """按 key 取规则。"""
    return RULE_FORMS_BY_KEY.get(key)


def detect_all(df: pd.DataFrame, idx: int, verified_only: bool = True) -> list[RuleForm]:
    """在 idx 处跑全部规则，返回命中的规则列表（按 verified 过滤可选）。

    Args:
        df: prepare_stock_data 输出
        idx: 判定位置（当前日）
        verified_only: 只返回已验证规则（每日推荐用）；False 返回全部命中（回测用）

    Returns:
        命中规则列表
    """
    hits: list[RuleForm] = []
    for r in RULE_FORMS:
        if verified_only and not r.verified:
            continue
        if r.detect(df, idx):
            hits.append(r)
    return hits
