"""分类层：启动形态分类（form_classifier）

对齐规划：plans/04-回测引擎.md 3.2
  形态体系（按判定优先级 E→C→B→A→D，先判最特殊形态）：
    E 连板型     ：连续涨停/一字板启动（难买入，单独管理）
    C 中继加速型 ：第一波上涨后平台整理再加速
    B V 型反转型 ：快速下跌后 V 型反转（无平台）
    A 平台突破型 ：底部横盘 20~60 日后放量突破（核心形态）
    D 直接拉升型 ：兜底形态，无明显平台/下跌/前涨，放量大阳拉升

  关键口径：涨幅从"启动点 T0"算起；负样本必须与正样本同形态。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logger import logger

# 形态类型
FORM_A = "A"  # 平台突破
FORM_B = "B"  # V 型反转
FORM_C = "C"  # 中继加速
FORM_D = "D"  # 直接拉升
FORM_E = "E"  # 连板
FORM_U = "U"  # 无法归类（unknown）：仍进入样本库与结局分流，但不参与同形态归因/聚类

# 判定阈值（可配置，用验证集校准）
PLATFORM_WIN_MIN, PLATFORM_WIN_MAX = 20, 60   # 平台窗口
PLATFORM_VOLATILITY = 0.25                     # 平台波动 <25%
PLATFORM_SHRINK = 0.80                         # 平台缩量 <80% 均量
BREAKOUT_PCT = 1.05                            # 有效突破：close > 平台高点*1.03
BREAKOUT_VOL_RATIO = 1.5                       # 放量：vol > 近20日均量*1.5
PRIOR_RISE = 0.50                              # 前一波上涨 >50%（中继）
PRIOR_DROP = 0.25                              # 前 20 日快速下跌 >25%（V 反转）
DIRECT_RISE_10D = 0.30                         # 直接拉升：10 日内涨幅 >30%
LIMIT_UP_RATIO = 0.50                          # 连板：涨停日占比 >50%

# ── T5（plans/23 §十三）：平台突破型的**结构性**修复 ──────────────────────────
# 实测（scripts/diagnose_unclassified.py，220 只）：无法归类（U）段占全部候选段 21/44 = 48%，
# 其中 6/12 的 U 段被否原因是 "no_platform(A)"。原因不是"没有平台"，而是**探测口径错位**：
# 旧口径要求平台在 low 之前结束（`_detect_platform(df, low_idx-1)`），而 3 倍段的低点往往
# **就在平台内部**（横盘的最后一根/最低一根），于是真实平台永远探测不到 → 判 U。
# 修复：① 平台窗口允许包含 low 本身（先试 low-1，失败再试 low）
#      ② 突破确认窗口 5 → 10 日（低点后第 6~10 日才放量突破在实盘中很常见）
# 注意：这是"修探测器"而不是"调阈值凑样本"——阈值一个都没动，所以不会把失败样本硬拉进 A。
A_BREAKOUT_WIN = 10                            # 突破确认窗口（交易日，原为 5）
# U 占比警戒线（超出即视为"形态层解释力不足"，由进化大脑 L0 健康神经元报警）
U_RATIO_WARN = 0.35


@dataclass
class ClassifiedSegment:
    """已分类的行情段。"""
    segment: object                 # SurgeSegment
    form_type: str                  # A/B/C/D/E
    confidence: float               # 0~1 置信度
    t0_idx: int                     # 启动点在序列中的下标（主升浪起涨点）
    start_price: float              # 启动价
    peak_price: float               # 最高价
    peak_gain: float                # 启动价到最高价涨幅
    peak_idx: int                   # 最高点下标
    detail: dict = field(default_factory=dict)


def _calc_volatility(close: np.ndarray) -> float:
    """区间波动率：max/min - 1。"""
    if len(close) == 0:
        return 0.0
    return float(close.max() / close.min() - 1)


def _detect_platform(df: pd.DataFrame, end_idx: int) -> tuple[bool, float, float, int]:
    """检测 end_idx 之前是否存在横盘平台。

    Returns:
        (found, platform_high, platform_volatility, platform_len)
    """
    close = df["adj_close"].to_numpy()
    vol = df["vol"].to_numpy()
    n = len(close)
    # 搜索窗口 20~60 个交易日
    for win in range(PLATFORM_WIN_MIN, PLATFORM_WIN_MAX + 1):
        start = end_idx - win + 1
        if start < 0:
            break
        w_close = close[start : end_idx + 1]
        w_vol = vol[start : end_idx + 1]
        if len(w_close) < 5:
            continue
        vol_60 = np.nanmean(vol[max(0, end_idx - 60) : end_idx + 1]) or 1.0
        if _calc_volatility(w_close) < PLATFORM_VOLATILITY and np.nanmean(w_vol) < PLATFORM_SHRINK * vol_60:
            return True, float(w_close.max()), _calc_volatility(w_close), win
    return False, 0.0, 0.0, 0


def classify_segment(df: pd.DataFrame, seg, min_data: int = 90,
                     diag: dict | None = None) -> ClassifiedSegment | None:
    """对单个候选行情段识别启动形态。

    Args:
        df: 该股票 prepare_stock_data 输出（含 adj_close / vol / is_sealed_up）
        seg: SurgeSegment
        min_data: 启动前至少需要的数据量（用于判断平台/前涨/前跌）
        diag: 可选出参。填入各形态的**判定中间量**（含被否原因），
            供 U（无法归类）段留痕（plans/23 §十三 T5：不加规则的"可解释性"补强）

    Returns:
        ClassifiedSegment 或 None（无法分类/数据不足）
    """
    if df is None or df.empty:
        return None
    close = df["adj_close"].to_numpy()
    vol = df["vol"].to_numpy()
    n = len(close)

    # 找到实际主升浪起涨点：low_idx 之后第一个放量阳线突破点，或 low_idx 本身
    low_idx = seg.low_idx
    t0_idx = low_idx

    # ── 判定连板型 E（最特殊）──
    win = df.iloc[low_idx : min(seg.high_idx + 1, n)]
    if not win.empty and "is_sealed_up" in win.columns:
        seal_ratio = float(win["is_sealed_up"].mean())
        if diag is not None:
            diag["E_seal_ratio"] = round(seal_ratio, 4)
        if seal_ratio >= LIMIT_UP_RATIO:
            return ClassifiedSegment(
                segment=seg, form_type=FORM_E, confidence=0.9,
                t0_idx=low_idx, start_price=float(close[low_idx]),
                peak_price=float(close[seg.high_idx]), peak_gain=seg.gain,
                peak_idx=seg.high_idx, detail={"seal_ratio": seal_ratio},
            )

    # 启动前结构：low_idx 之前的窗口
    pre = df.iloc[max(0, low_idx - 60) : low_idx]

    # ── 判定中继加速型 C（前有 >50% 上涨后平台整理再加速）──
    if len(pre) >= PLATFORM_WIN_MIN:
        pre_close = pre["adj_close"].to_numpy()
        pre_peak = pre_close.max()
        pre_base = pre_close[0]
        prior_rise = pre_peak / pre_base - 1 if pre_base else 0
        if diag is not None:
            diag["C_prior_rise"] = round(float(prior_rise), 4)
        if prior_rise > PRIOR_RISE:
            # 低点后放量上行确认加速
            after = df.iloc[low_idx : min(low_idx + 10, n)]
            if len(after) >= 3:
                return ClassifiedSegment(
                    segment=seg, form_type=FORM_C, confidence=0.75,
                    t0_idx=low_idx, start_price=float(close[low_idx]),
                    peak_price=float(close[seg.high_idx]), peak_gain=seg.gain,
                    peak_idx=seg.high_idx,
                    detail={"prior_rise": prior_rise},
                )

    # ── 判定 V 型反转型 B（前 20 日快速下跌 >25%，低点后放量反转）──
    if len(pre) >= 21:
        pre20 = pre.tail(20)["adj_close"].to_numpy()
        pre20_drop = pre20[0] / pre20[-1] - 1 if pre20[-1] else 0
        if diag is not None:
            diag["B_pre20_drop"] = round(float(pre20_drop), 4)
        if pre20_drop > PRIOR_DROP:
            after = df.iloc[low_idx : min(low_idx + 5, n)]
            if len(after) >= 3:
                after_rise = after["adj_close"].iloc[-1] / after["adj_close"].iloc[0] - 1
                if after_rise > 0:
                    return ClassifiedSegment(
                        segment=seg, form_type=FORM_B, confidence=0.75,
                        t0_idx=low_idx, start_price=float(close[low_idx]),
                        peak_price=float(close[seg.high_idx]), peak_gain=seg.gain,
                        peak_idx=seg.high_idx,
                        detail={"pre20_drop": pre20_drop},
                    )

    # ── 判定平台突破型 A（底部横盘 20~60 日 + 放量突破）──
    # T5 结构性修复：平台窗口允许包含 low 本身，突破确认窗口 5 → A_BREAKOUT_WIN(10)。
    if low_idx >= PLATFORM_WIN_MIN:
        found, ph, pv, pwin = _detect_platform(df, low_idx - 1)
        plat_scope = "before_low"
        if not found:
            found, ph, pv, pwin = _detect_platform(df, low_idx)
            plat_scope = "incl_low"
        if diag is not None:
            diag["A_platform"] = bool(found)
            diag["A_platform_scope"] = plat_scope if found else ""
        if found:
            # 突破确认：低点后 A_BREAKOUT_WIN 日内 close 突破平台高点且放量
            after = df.iloc[low_idx : min(low_idx + A_BREAKOUT_WIN, n)]
            if len(after) >= 3:
                vol20 = float(np.nanmean(vol[max(0, low_idx - 20) : low_idx])) or 1.0
                brk = bool(after["adj_close"].max() > ph * BREAKOUT_PCT)
                vsurge = bool(after["vol"].max() > BREAKOUT_VOL_RATIO * vol20)
                if diag is not None:
                    diag["A_breakout"] = brk
                    diag["A_vol_surge"] = vsurge
                if brk and vsurge:
                    return ClassifiedSegment(
                        segment=seg, form_type=FORM_A, confidence=0.85,
                        t0_idx=low_idx, start_price=float(close[low_idx]),
                        peak_price=float(close[seg.high_idx]), peak_gain=seg.gain,
                        peak_idx=seg.high_idx,
                        detail={"platform_high": ph, "platform_vol": pv, "win": pwin,
                                "platform_scope": plat_scope},
                    )

    # ── 兜底：直接拉升型 D（无平台/急跌/前涨 → 放量大阳启动确认）──
    # 精准度控制（对齐 plans/04 3.10 ①超跌反弹 ④假突破）：D 不是"垃圾桶"。
    # A/B/C 已判定失败（无平台突破/无V反转/无前涨中继），此处要求"直接拉升"语义：
    #   ① low 后 5 日强放量上行：涨幅 >5% 且 量 >1.5 倍 近20日均量
    #   ② 或 10 日加速涨幅 >30%（DIRECT_RISE_10D，与每日推荐 classify_current 对齐）
    # 不满足 → None（不进入样本库，宁缺毋滥），避免慢涨/弱启动/反抽混入。
    after = df.iloc[low_idx : min(low_idx + 5, n)]
    if len(after) >= 3:
        after_rise = float(after["adj_close"].iloc[-1] / after["adj_close"].iloc[0] - 1)
        vol20 = float(np.nanmean(vol[max(0, low_idx - 20) : low_idx])) or 1.0
        vol_surge = bool(float(after["vol"].max()) > 1.5 * vol20)
        d10 = close[low_idx : min(low_idx + 10, n)]
        d10_rise = float(d10[-1] / d10[0] - 1) if len(d10) >= 5 and d10[0] else 0.0
        if diag is not None:
            diag["D_after_rise"] = round(after_rise, 4)
            diag["D_vol_surge"] = vol_surge
            diag["D_d10_rise"] = round(d10_rise, 4)
        if (after_rise > 0.05 and vol_surge) or d10_rise > DIRECT_RISE_10D:
            return ClassifiedSegment(
                segment=seg, form_type=FORM_D, confidence=0.6,
                t0_idx=low_idx, start_price=float(close[low_idx]),
                peak_price=float(close[seg.high_idx]), peak_gain=seg.gain,
                peak_idx=seg.high_idx,
                detail={"after_rise": after_rise, "vol_surge": vol_surge, "d10_rise": d10_rise},
            )

    # 无法明确归类（不进入样本库，保持纯净）——diag 留痕，便于量化"形态层解释力不足"
    if diag is not None:
        diag["reason"] = _diag_reason(diag)
    return None


def _diag_reason(diag: dict) -> str:
    """把诊断量翻译成"被否原因链"（供 U 段统计与进化大脑观察）。"""
    rs: list[str] = []
    if diag.get("E_seal_ratio") is not None and diag["E_seal_ratio"] < LIMIT_UP_RATIO:
        rs.append("no_seal(E)")
    if diag.get("C_prior_rise") is None:
        rs.append("pre_short_data(C)")
    elif diag["C_prior_rise"] <= PRIOR_RISE:
        rs.append("no_prior_rise(C)")
    if diag.get("B_pre20_drop") is None:
        rs.append("pre_short_data(B)")
    elif diag["B_pre20_drop"] <= PRIOR_DROP:
        rs.append("no_pre_drop(B)")
    if not diag.get("A_platform"):
        rs.append("no_platform(A)")
    elif not (diag.get("A_breakout") and diag.get("A_vol_surge")):
        rs.append("platform_no_breakout(A)")
    if not ((diag.get("D_after_rise") or 0) > 0.05 and diag.get("D_vol_surge")):
        if not ((diag.get("D_d10_rise") or 0) > DIRECT_RISE_10D):
            rs.append("no_fast_rise(D)")
    return "|".join(rs) or "unknown"


def classify_batch(df: pd.DataFrame, segments: list, keep_unknown: bool = True) -> list[ClassifiedSegment]:
    """批量分类：对同一股票的候选行情段逐一分类。

    Args:
        df: prepare_stock_data 输出
        segments: SurgeSegment 列表
        keep_unknown: 是否保留无法归类的段（默认 True）。为 True 时，无法归类的段
            以 form_type=FORM_U（"U"，unknown）保留——仍进入结局分流与样本库，
            保证"能不能进样本库只认涨幅、不认形态"，避免误伤真实 3 倍段；
            U 样本在聚类/归因中单独成组，不混入 A/B/C/D。

    Returns:
        ClassifiedSegment 列表（keep_unknown=False 时仅含分类成功的段）
    """
    result: list[ClassifiedSegment] = []
    for seg in segments:
        diag: dict = {}
        cls = classify_segment(df, seg, diag=diag)
        if cls is not None:
            result.append(cls)
        elif keep_unknown:
            # 无法明确归类：以 U 保留，t0 用低点（后续 outcome 分流仍会验证是否真 3 倍）。
            # T5：detail 记录"被否原因链 + 判定中间量"，供回测统计 U 占比/原因分布
            # （不加规则、不调阈值 —— 实测 U 的成功/失败样本结构特征几乎相同，
            #   硬加形态只会制造"没有信息量的标签"）。
            result.append(ClassifiedSegment(
                segment=seg,
                form_type=FORM_U,
                confidence=0.0,
                t0_idx=seg.low_idx,
                start_price=float(seg.low_price),
                peak_price=float(seg.high_price),
                peak_gain=seg.gain,
                peak_idx=seg.high_idx,
                detail={"reason": diag.get("reason", "unclassified"), "diag": diag},
            ))
    return result


@dataclass
class CurrentPattern:
    """当前时点形态识别结果（每日推荐用，不依赖历史行情段）。"""
    ts_code: str
    form_type: str            # A/B/C/D/E；未确认 = ""
    confirmed: bool           # 是否通过"启动确认"（是推荐入口的前提）
    confidence: float
    end_idx: int              # 疑似启动点（当前日）下标
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ts_code": self.ts_code,
            "form_type": self.form_type,
            "confirmed": self.confirmed,
            "confidence": round(self.confidence, 3),
            "end_idx": self.end_idx,
            "detail": self.detail,
        }


def classify_current(df: pd.DataFrame, end_idx: int | None = None, ts_code: str = "") -> CurrentPattern | None:
    """识别"当前时点"是否处于某形态的启动确认状态（每日推荐第零层）。

    与 classify_segment 的区别：classify_segment 是对"历史行情段"分类（依赖 low/high 的
    SurgeSegment）；本函数以 end_idx（当前日）为疑似启动点，前瞻判断当前是否刚启动，
    并做"启动确认"（防止把普通横盘/下跌当推荐）。

    判定（复用本文件阈值，优先级 E→A→B→C→D）：
      E 连板   ：近 5 日涨停封板占比 ≥50%
      A 平台突破：end_idx 前有平台，近 5 日放量突破平台高点 1.03 倍
      B V反    ：近 20 日快速下跌 >25%，近 3 日放量反转
      C 中继   ：前一波涨 >50%，近 5 日放量加速上行
      D 直接拉升：近 10 日涨幅 >30%
    """
    if df is None or df.empty:
        return None
    end_idx = int(end_idx) if end_idx is not None else len(df) - 1
    n = len(df)
    if end_idx < 0 or end_idx >= n:
        return None
    close = df["adj_close"].to_numpy()
    vol = df["vol"].to_numpy() if "vol" in df.columns else np.zeros(n)

    # ── E 连板 ──
    if "is_sealed_up" in df.columns:
        win = df["is_sealed_up"].iloc[max(0, end_idx - 4) : end_idx + 1]
        if len(win) >= 3:
            seal_ratio = float(win.mean())
            if seal_ratio >= LIMIT_UP_RATIO:
                return CurrentPattern(ts_code, FORM_E, True, 0.9, end_idx,
                                      {"seal_ratio": round(seal_ratio, 3)})

    # ── A 平台突破（启动确认：放量突破平台高点）──
    if end_idx >= PLATFORM_WIN_MIN:
        found, ph, pv, pwin = _detect_platform(df, end_idx - 1)
        if found:
            after = df.iloc[max(0, end_idx - 4) : end_idx + 1]
            if len(after) >= 3 and float(after["adj_close"].max()) > ph * BREAKOUT_PCT:
                vol20 = float(np.nanmean(vol[max(0, end_idx - 20) : end_idx + 1])) or 1.0
                if float(after["vol"].max()) > BREAKOUT_VOL_RATIO * vol20:
                    return CurrentPattern(ts_code, FORM_A, True, 0.85, end_idx,
                                          {"platform_high": round(float(ph), 3), "win": pwin})

    # ── B V 型反转（启动确认：急跌后放量反转）──
    if end_idx >= 21:
        pre20 = close[max(0, end_idx - 19) : end_idx + 1]
        if len(pre20) >= 10:
            pre20_drop = float(pre20[0] / pre20[-1] - 1) if pre20[-1] else 0.0
            if pre20_drop > PRIOR_DROP:
                after = df.iloc[max(0, end_idx - 2) : end_idx + 1]
                if len(after) >= 3:
                    after_rise = float(after["adj_close"].iloc[-1] / after["adj_close"].iloc[0] - 1)
                    if after_rise > 0:
                        return CurrentPattern(ts_code, FORM_B, True, 0.75, end_idx,
                                              {"pre20_drop": round(pre20_drop, 3)})

    # ── C 中继加速（启动确认：前涨后平台再放量加速）──
    if end_idx >= PLATFORM_WIN_MIN:
        pre = df.iloc[max(0, end_idx - 60) : end_idx + 1]
        pre_close = pre["adj_close"].to_numpy()
        prior_rise = float(pre_close.max() / pre_close[0] - 1) if pre_close[0] else 0.0
        if prior_rise > PRIOR_RISE:
            after = df.iloc[max(0, end_idx - 4) : end_idx + 1]
            if len(after) >= 3:
                after_rise = float(after["adj_close"].iloc[-1] / after["adj_close"].iloc[0] - 1)
                if after_rise > 0.03:
                    return CurrentPattern(ts_code, FORM_C, True, 0.7, end_idx,
                                          {"prior_rise": round(prior_rise, 3)})

    # ── D 直接拉升（启动确认：近 10 日涨幅 >30%）──
    if end_idx >= 10:
        d10 = close[max(0, end_idx - 9) : end_idx + 1]
        if len(d10) >= 5:
            d10_rise = float(d10[-1] / d10[0] - 1) if d10[0] else 0.0
            if d10_rise > DIRECT_RISE_10D:
                return CurrentPattern(ts_code, FORM_D, True, 0.6, end_idx,
                                      {"d10_rise": round(d10_rise, 3)})

    return CurrentPattern(ts_code, "", False, 0.0, end_idx, {"reason": "未确认启动形态"})
