"""全维度特征提取（feature_extractor）

对齐规划：plans/04-回测引擎.md 4.4 + 4.4.1
  8 大维度 60+ 维特征，正负样本共用同一套特征体系。

  全特征归一化（4.4.1）：
    - 比率类（涨跌幅/换手/净流入率/ROE）→ 价格/规模无关，直接用
    - 绝对数值（成交额/市值）→ 转占比或分位
    - 有界指标（RSI/KDJ）→ 直接用（0~100）
    - 无界指标（MACD/均线斜率）→ Z-score
    - 最终拼接统一 Z-score，确保各维度对相似度贡献均衡

  说明：特征基于 daily/daily_basic/moneyflow 等可加载字段计算；
        未加载到关联表数据时对应特征置 NaN（后续可归一化/填充）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logger import logger

FEATURE_WINDOW = 20  # 启动前 20 个交易日

# 特征 schema：记录每个特征的归一化方式
# type: ratio / percentile / zscore / bounded / category
FEATURE_SCHEMA: dict[str, str] = {}


def register_feature(name: str, ftype: str) -> None:
    FEATURE_SCHEMA[name] = ftype


# 价格图形（20 维，百分比序列，价格无关）
for i in range(1, FEATURE_WINDOW + 1):
    register_feature(f"pct_{i}", "ratio")

# 技术面
register_feature("ma5_above_ma10", "category")
register_feature("ma10_above_ma20", "category")
register_feature("macd_hist", "zscore")
register_feature("rsi_14", "bounded")
register_feature("kdj_k", "bounded")
register_feature("boll_pos", "ratio")
register_feature("atr_pct", "ratio")
register_feature("adx", "bounded")

# 量能面
register_feature("volume_ratio", "ratio")
register_feature("vol_ma_change", "ratio")
register_feature("vol_surge_days", "ratio")
register_feature("turnover_rate", "ratio")
register_feature("vol_pct_20", "ratio")

# 资金面
register_feature("net_amount_ratio", "ratio")
register_feature("large_order_ratio", "ratio")
register_feature("amount_ratio", "ratio")

# 基本面
register_feature("roe", "ratio")
register_feature("gross_margin", "ratio")
register_feature("profit_growth", "ratio")
register_feature("pe_percentile", "percentile")
register_feature("pb_percentile", "percentile")
register_feature("revenue_growth", "ratio")        # 营收同比（income）
register_feature("debt_ratio", "ratio")            # 资产负债率（balancesheet）
register_feature("ocf_ps", "ratio")                # 每股经营现金流（cashflow）
register_feature("mainbz_concentration", "ratio")  # 主营集中度（fina_mainbz）

# 筹码面
register_feature("holder_change", "ratio")
register_feature("top10_ratio", "ratio")           # 前十大股东持股占比（top10_holders）
register_feature("pledge_ratio", "ratio")          # 股权质押比例（pledge_stat）
# 价值/筹码增强（用户补充：每股净资产 / 人均持股金额 / 筹码集中度）
register_feature("bps", "ratio")                   # 每股净资产（fina_indicator.bps）
register_feature("avg_hold_amount", "ratio")       # 人均持股金额（流通市值/股东人数，万元/人）
register_feature("holder_concentration", "ratio")  # 筹码集中度（股东人数倒数，越大越集中）
register_feature("st_risk_flag", "category")       # 即将*ST高风险预判 0/1（净资产<0 或 连续两期亏损）

# 事件面（来自低频事件表）
register_feature("event_forecast", "category")     # 业绩预告方向 +1/-1/0
register_feature("event_express", "category")      # 业绩快报方向 +1/-1/0
register_feature("event_dividend", "category")     # 近180日是否分红 0/1
register_feature("event_repurchase", "category")   # 近180日是否回购 0/1
register_feature("event_rewards", "category")      # 近180日是否股权激励 0/1
register_feature("event_holdertrade", "category")  # 近90日股东增减持方向 +1/-1/0
register_feature("event_block", "ratio")           # 近90日大宗交易折溢价率

# 市场环境
register_feature("market_env", "category")
register_feature("wk_trend", "ratio")              # 周线 4 周动量（weekly）
register_feature("mo_trend", "ratio")              # 月线 3 月动量（monthly）
register_feature("is_ever_st", "category")         # 历史是否 ST（namechange）

FEATURE_NAMES = list(FEATURE_SCHEMA.keys())

# ── 特征体系拆分（用户架构设计）──
# 用户明确：基本面/筹码/资金/事件等"状态"指标不应放进向量特征（不适合归一化、
# 不适合相似度匹配），应作为"采样条件参数"独立分析——研究"越来越大/越来越小对上涨的影响"。
# 因此特征分为两组：
#   1. VECTOR_FEATURES（形态 33 维）：价格图形 + 技术 + 量能 —— 归一化 + 相似度匹配
#   2. CONDITION_FEATURES（条件 30 维）：资金/基本/筹码/事件/环境/ST风险 —— 不归一化，
#      在采样时按原始值分位切分统计"条件→T+5上涨率"，固化 condition_table 供每日推荐打分。

# 形态核心特征（向量相似度用）：避免被资金/财务/筹码/事件/大盘等"状态"维度稀释相似度。
MATCH_FEATURES: list[str] = (
    [f"pct_{i}" for i in range(1, FEATURE_WINDOW + 1)]
    + ["ma5_above_ma10", "ma10_above_ma20", "macd_hist", "rsi_14", "kdj_k",
       "boll_pos", "atr_pct", "adx"]
    + ["volume_ratio", "vol_ma_change", "vol_surge_days", "turnover_rate", "vol_pct_20"]
)

# 向量特征 = 形态（价格+技术+量能）
VECTOR_FEATURES: list[str] = list(MATCH_FEATURES)

# 条件参数 = 除形态外的全部状态维度（资金/基本/筹码/事件/环境/ST风险）
CONDITION_FEATURES: list[str] = [n for n in FEATURE_NAMES if n not in VECTOR_FEATURES]


@dataclass
class FeatureVector:
    """单个样本的特征向量。"""
    ts_code: str
    form_type: str
    label: str
    t0_idx: int
    features: dict = field(default_factory=dict)

    def to_array(self, names: list[str] | None = None) -> np.ndarray:
        names = names or FEATURE_NAMES
        return np.array([self.features.get(k, np.nan) for k in names], dtype=float)

    def to_dict(self) -> dict:
        return self.features.copy()


def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    if len(arr) == 0:
        return arr
    out = np.empty_like(arr)
    out[0] = arr[0]
    alpha = 2 / (span + 1)
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(close: np.ndarray, period: int = 14) -> float:
    if len(close) < period + 1:
        return np.nan
    diff = np.diff(close)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_gain = gains[-period:].mean()
    avg_loss = losses[-period:].mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def extract_features(
    df: pd.DataFrame,
    t0_idx: int,
    ts_code: str = "",
    form_type: str = "",
    label: str = "",
    extra: dict | None = None,
) -> FeatureVector:
    """提取启动点前 FEATURE_WINDOW 个交易日的特征。

    Args:
        df: prepare_stock_data 输出（含 adj_close/vol/amount/high/low 等）
        t0_idx: 启动点下标（在 df 中）
        ts_code/form_type/label: 元信息
        extra: 额外特征（如资金面/基本面来自关联表）

    Returns:
        FeatureVector
    """
    fv: dict[str, float] = {}

    # 需要至少 FEATURE_WINDOW+1 个交易日数据
    start = max(0, t0_idx - FEATURE_WINDOW)
    win = df.iloc[start : t0_idx + 1]
    if len(win) < FEATURE_WINDOW + 1:
        # 数据不足，用可用的
        win = df.iloc[max(0, t0_idx - len(df) + 1) : t0_idx + 1]

    close = win["adj_close"].to_numpy(dtype=float) if "adj_close" in win else win["close"].to_numpy(dtype=float)
    # high/low 必须与 close 同口径复权（adj_high/adj_low），否则除权日技术指标（ATR/KDJ/布林带）失真
    high = (
        win["adj_high"].to_numpy(dtype=float) if "adj_high" in win
        else (win["high"].to_numpy(dtype=float) if "high" in win else close)
    )
    low = (
        win["adj_low"].to_numpy(dtype=float) if "adj_low" in win
        else (win["low"].to_numpy(dtype=float) if "low" in win else close)
    )
    vol = win["vol"].to_numpy(dtype=float) if "vol" in win else np.zeros_like(close)

    # ── 价格图形（20 维 涨跌幅%）──
    pct = np.diff(close) / np.maximum(close[:-1], 1e-9) * 100
    for i in range(1, FEATURE_WINDOW + 1):
        fv[f"pct_{i}"] = float(pct[-i]) if i <= len(pct) else np.nan

    # ── 技术面 ──
    ma5 = close[-5:].mean() if len(close) >= 5 else np.nan
    ma10 = close[-10:].mean() if len(close) >= 10 else np.nan
    ma20 = close[-20:].mean() if len(close) >= 20 else np.nan
    fv["ma5_above_ma10"] = float(1 if (ma5 > ma10) else 0)
    fv["ma10_above_ma20"] = float(1 if (ma10 > ma20) else 0)

    ema12 = _ema(close, 12) if len(close) else close
    ema26 = _ema(close, 26) if len(close) else close
    macd_line = ema12 - ema26
    signal = _ema(macd_line, 9) if len(macd_line) else macd_line
    fv["macd_hist"] = float(macd_line[-1] - signal[-1]) if len(macd_line) else np.nan

    fv["rsi_14"] = _rsi(close, 14)

    # KDJ（简化）
    if len(low) and len(high):
        low14 = low[-14:].min() if len(low) >= 14 else low.min()
        high14 = high[-14:].max() if len(high) >= 14 else high.max()
        rsv = (close[-1] - low14) / (high14 - low14 + 1e-9) * 100
        fv["kdj_k"] = float(rsv)
    else:
        fv["kdj_k"] = np.nan

    # 布林带位置 %B
    ma20v = close[-20:].mean() if len(close) >= 20 else np.nan
    std20 = close[-20:].std() if len(close) >= 20 else np.nan
    if ma20v == ma20v and std20 == std20 and std20 > 0:
        fv["boll_pos"] = float((close[-1] - (ma20v - 2 * std20)) / (4 * std20))
    else:
        fv["boll_pos"] = np.nan

    # ATR%
    if len(high) > 1 and len(low) > 1 and len(close) > 1:
        tr = np.maximum(high[1:] - low[1:], np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1])))
        fv["atr_pct"] = float(tr[-14:].mean() / close[-1] * 100) if len(tr) else np.nan
    else:
        fv["atr_pct"] = np.nan

    # ADX（简化：用价格方向强度）
    if len(close) > 1:
        direction = np.mean(np.sign(np.diff(close[-15:]))) if len(close) > 15 else np.nan
        fv["adx"] = float(abs(direction) * 100)
    else:
        fv["adx"] = np.nan

    # ── 量能面 ──
    if len(vol) >= 5:
        vol20 = vol[-20:].mean() if len(vol) >= 20 else vol.mean()
        fv["volume_ratio"] = float(vol[-1] / vol20) if vol20 else np.nan
        fv["vol_ma_change"] = float((vol[-5:].mean() - vol20) / vol20) if vol20 else np.nan
        surge = int((vol[-5:] > vol20 * 1.5).sum())
        fv["vol_surge_days"] = float(surge / 5)
        fv["vol_pct_20"] = float(vol[-20:].sum() / max(vol.sum(), 1e-9))
    else:
        fv["volume_ratio"] = np.nan
        fv["vol_ma_change"] = np.nan
        fv["vol_surge_days"] = np.nan
        fv["vol_pct_20"] = np.nan

    # 换手率（若 daily_basic 已 join 进来）
    if "turnover_rate" in df.columns:
        tr = win["turnover_rate"].to_numpy(dtype=float)
        fv["turnover_rate"] = float(tr[-1]) if len(tr) else np.nan
    else:
        fv["turnover_rate"] = np.nan

    # ── 资金面（来自 extra，moneyflow 等）──
    fv["net_amount_ratio"] = float(extra.get("net_amount_ratio", np.nan)) if extra else np.nan
    fv["large_order_ratio"] = float(extra.get("large_order_ratio", np.nan)) if extra else np.nan
    fv["amount_ratio"] = float(extra.get("amount_ratio", np.nan)) if extra else np.nan

    # ── 基本面（来自 extra，fina_indicator/income/balancesheet/cashflow/fina_mainbz 等）──
    fv["roe"] = float(extra.get("roe", np.nan)) if extra else np.nan
    fv["gross_margin"] = float(extra.get("gross_margin", np.nan)) if extra else np.nan
    fv["profit_growth"] = float(extra.get("profit_growth", np.nan)) if extra else np.nan
    fv["pe_percentile"] = float(extra.get("pe_percentile", np.nan)) if extra else np.nan
    fv["pb_percentile"] = float(extra.get("pb_percentile", np.nan)) if extra else np.nan
    fv["revenue_growth"] = float(extra.get("revenue_growth", np.nan)) if extra else np.nan
    fv["debt_ratio"] = float(extra.get("debt_ratio", np.nan)) if extra else np.nan
    fv["ocf_ps"] = float(extra.get("ocf_ps", np.nan)) if extra else np.nan
    fv["mainbz_concentration"] = float(extra.get("mainbz_concentration", np.nan)) if extra else np.nan

    # ── 筹码面（来自 extra，stk_holdernumber/top10_holders/pledge_stat 等）──
    fv["holder_change"] = float(extra.get("holder_change", np.nan)) if extra else np.nan
    fv["top10_ratio"] = float(extra.get("top10_ratio", np.nan)) if extra else np.nan
    fv["pledge_ratio"] = float(extra.get("pledge_ratio", np.nan)) if extra else np.nan
    # 价值/筹码增强（用户补充）
    fv["bps"] = float(extra.get("bps", np.nan)) if extra else np.nan
    fv["avg_hold_amount"] = float(extra.get("avg_hold_amount", np.nan)) if extra else np.nan
    fv["holder_concentration"] = float(extra.get("holder_concentration", np.nan)) if extra else np.nan
    fv["st_risk_flag"] = float(extra.get("st_risk_flag", 0.0) or 0.0) if extra else 0.0

    # ── 事件面（来自 extra，低频事件表）──
    fv["event_forecast"] = float(extra.get("event_forecast", np.nan)) if extra else np.nan
    fv["event_express"] = float(extra.get("event_express", np.nan)) if extra else np.nan
    fv["event_dividend"] = float(extra.get("event_dividend", np.nan)) if extra else np.nan
    fv["event_repurchase"] = float(extra.get("event_repurchase", np.nan)) if extra else np.nan
    fv["event_rewards"] = float(extra.get("event_rewards", np.nan)) if extra else np.nan
    fv["event_holdertrade"] = float(extra.get("event_holdertrade", np.nan)) if extra else np.nan
    fv["event_block"] = float(extra.get("event_block", np.nan)) if extra else np.nan

    # ── 市场环境（来自 extra，index_daily/weekly/monthly/namechange 等）──
    fv["market_env"] = float(extra.get("market_env", np.nan)) if extra else np.nan
    fv["wk_trend"] = float(extra.get("wk_trend", np.nan)) if extra else np.nan
    fv["mo_trend"] = float(extra.get("mo_trend", np.nan)) if extra else np.nan
    fv["is_ever_st"] = float(extra.get("is_ever_st", np.nan)) if extra else np.nan

    return FeatureVector(
        ts_code=ts_code, form_type=form_type, label=label, t0_idx=t0_idx, features=fv,
    )


def zscore_normalize(features: list[FeatureVector], names: list[str] | None = None) -> np.ndarray:
    """将一批特征向量归一化为统一矩阵（逐特征 Z-score）。

    Returns:
        (N, D) ndarray；单值特征列若 std=0 则置 0
    """
    names = names or FEATURE_NAMES
    if not features:
        return np.empty((0, len(names)))
    mat = np.array([f.to_array(names) for f in features], dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.nanmean(mat, axis=0)
        std = np.nanstd(mat, axis=0)
        std_safe = np.where(std < 1e-9, 1.0, std)
        norm = (mat - mean) / std_safe
        norm = np.where(np.isnan(norm), 0.0, norm)
    return norm


# ──────────────────────────────────────────────
# 按类型差异化的特征归一化器（fit 训练参数 → transform 固定应用；参数固化可落盘，供每日推荐复用）
# ──────────────────────────────────────────────
NORMALIZER_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "feature_normalizer.json",
)
MIN_VALID_RATIO = 0.10      # 特征有效样本比例低于此 → 视为无信息（transform 置 0，不稀释相似度）
WIN_LO, WIN_HI = 1.0, 99.0  # winsorize 截断分位（抗极端值）


@dataclass
class FeatureNormalizer:
    """按特征类型差异化的归一化器。

    类型处理（对齐 FEATURE_SCHEMA）：
      category : 保持 0/1（缺失 → 0）
      bounded  : 按理论边界缩放到 [0,1]（RSI/KDJ/ADX 0~100 → /100）
      ratio/zscore/percentile : winsorize(1%~99%) + 中位数填充缺失 + Z-score
         （抗极端值，绝对量如 PE/PB 也可跨期可比）
    关键：fit 学到的参数（mean/std/中位数/分位）固化保存，transform 时对任意新样本用同一套参数，
          避免"回测 vs 每日推荐"归一化不一致。
    """
    names: list = field(default_factory=list)
    types: dict = field(default_factory=dict)
    means: np.ndarray = field(default_factory=lambda: np.array([], dtype=float))
    stds: np.ndarray = field(default_factory=lambda: np.array([], dtype=float))
    medians: np.ndarray = field(default_factory=lambda: np.array([], dtype=float))
    lo_vals: np.ndarray = field(default_factory=lambda: np.array([], dtype=float))
    hi_vals: np.ndarray = field(default_factory=lambda: np.array([], dtype=float))
    valid_ratio: np.ndarray = field(default_factory=lambda: np.array([], dtype=float))
    bounds: dict = field(default_factory=dict)

    @classmethod
    def fit(cls, mat: np.ndarray, names: list[str] | None = None) -> "FeatureNormalizer":
        """从训练样本学习归一化参数（仅用有效值）。

        架构：只对形态向量（VECTOR_FEATURES）归一化——基本面/筹码/资金等条件参数
        不进向量、不归一化（由 condition_table 原始值分位切分独立统计）。
        """
        names = list(names or VECTOR_FEATURES)
        D = len(names)
        means = np.zeros(D)
        stds = np.ones(D)
        medians = np.zeros(D)
        lo_vals = np.full(D, np.nan)
        hi_vals = np.full(D, np.nan)
        vr = np.zeros(D)
        types = {n: FEATURE_SCHEMA.get(n, "ratio") for n in names}
        for j, name in enumerate(names):
            col = mat[:, j].astype(float)
            valid = col[~np.isnan(col)]
            vr[j] = len(valid) / len(col) if len(col) else 0.0
            if len(valid) == 0:
                continue
            med = float(np.nanmedian(col))
            medians[j] = med if np.isfinite(med) else 0.0
            p_lo, p_hi = np.percentile(valid, [WIN_LO, WIN_HI])
            lo_vals[j], hi_vals[j] = float(p_lo), float(p_hi)
            s = float(np.nanstd(col))
            if s > 1e-9:
                means[j] = float(np.nanmean(col))
                stds[j] = s
        bounds = {n: (0.0, 100.0) for n in names if types.get(n) == "bounded"}
        return cls(names=names, types=types, means=means, stds=stds, medians=medians,
                   lo_vals=lo_vals, hi_vals=hi_vals, valid_ratio=vr, bounds=bounds)

    def transform(self, mat: np.ndarray) -> np.ndarray:
        """用固定参数归一化（可作用于训练样本或任意新样本）。"""
        if mat is None or len(mat) == 0:
            return np.empty((0, len(self.names)))
        D = len(self.names)
        out = np.zeros((mat.shape[0], D), dtype=float)
        for j, name in enumerate(self.names):
            col = mat[:, j].astype(float)
            t = self.types.get(name, "ratio")
            if t == "category":
                out[:, j] = np.nan_to_num(col, nan=0.0)
            elif t == "bounded":
                lo_b, hi_b = self.bounds.get(name, (0.0, 100.0))
                fill = self.medians[j] if j < len(self.medians) else 0.0
                c = np.clip(np.nan_to_num(col, nan=fill), lo_b, hi_b)
                out[:, j] = (c - lo_b) / (hi_b - lo_b) if hi_b > lo_b else 0.0
            else:
                if j < len(self.valid_ratio) and self.valid_ratio[j] < MIN_VALID_RATIO:
                    out[:, j] = 0.0   # 无信息特征：不参与相似度
                    continue
                fill = self.medians[j] if j < len(self.medians) else 0.0
                c = np.where(np.isnan(col), fill, col)
                if j < len(self.lo_vals) and j < len(self.hi_vals):
                    c = np.clip(c, self.lo_vals[j], self.hi_vals[j])
                mean = self.means[j] if j < len(self.means) else 0.0
                std = self.stds[j] if j < len(self.stds) and self.stds[j] > 1e-9 else 1.0
                out[:, j] = (c - mean) / std
        return out

    def to_dict(self) -> dict:
        return {
            "names": self.names,
            "types": self.types,
            "means": self.means.tolist(),
            "stds": self.stds.tolist(),
            "medians": self.medians.tolist(),
            "lo_vals": np.nan_to_num(self.lo_vals).tolist(),
            "hi_vals": np.nan_to_num(self.hi_vals).tolist(),
            "valid_ratio": self.valid_ratio.tolist(),
            "bounds": {k: list(v) for k, v in self.bounds.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FeatureNormalizer":
        return cls(
            names=list(d.get("names", [])),
            types=dict(d.get("types", {})),
            means=np.array(d.get("means", []), dtype=float),
            stds=np.array(d.get("stds", []), dtype=float),
            medians=np.array(d.get("medians", []), dtype=float),
            lo_vals=np.array(d.get("lo_vals", []), dtype=float),
            hi_vals=np.array(d.get("hi_vals", []), dtype=float),
            valid_ratio=np.array(d.get("valid_ratio", []), dtype=float),
            bounds={k: tuple(v) for k, v in d.get("bounds", {}).items()},
        )


def save_normalizer(normalizer: FeatureNormalizer, path: str = NORMALIZER_FILE) -> None:
    """固化归一化参数到磁盘（每日推荐复用同一套参数）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(normalizer.to_dict(), f, ensure_ascii=False)
        logger.info(f"归一化参数已保存: {path}（{len(normalizer.names)} 维）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"保存归一化参数失败: {exc}")


def load_normalizer(path: str = NORMALIZER_FILE) -> FeatureNormalizer | None:
    """加载已固化的归一化参数。"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return FeatureNormalizer.from_dict(json.load(f))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"加载归一化参数失败: {exc}")
    return None
