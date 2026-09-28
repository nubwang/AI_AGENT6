"""规则形态回测验证引擎（rule_verifier）

对齐规划：plans/12-网络经典形态规则库.md v4.0 第五章
  职责：
    1. 全市场逐日扫描所有 RuleForm，采集命中样本（含 T+1 结局、可交易性）
    2. 每个规则统计 hit_rate_t1（次日上涨率）+ avg_ret_t1 + 样本量
    3. 应用 45% 硬门槛 + 基线对比 + 显著性 + 样本外验证
    4. 无规则幸存时回退"仅 20 天向量"模式（由调用方处理）
    5. 固化 form_leaderboard.json + 加载函数

口径：
  - 结局唯一口径 T+1：hit_t1 = T+1 收盘 > 当日收盘
  - 可交易性：命中日一字封死涨停（is_sealed_up）→ 不可交易，不计入胜率统计
  - 防未来函数：detect 只用 idx 及之前；结局只用 idx 之后
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict

import numpy as np
import pandas as pd

from app.core.logger import logger
from app.backtest.loader import load_stock_pool, prepare_stock_data, is_st, filter_liquidity
from app.backtest.rule_forms import RuleForm, RULE_FORMS, detect_all
from app.backtest.stats_correction import bh_fdr

# 门槛（默认，可配置）
MIN_HIT_RATE = 0.45          # 45% 硬门槛（用户明确）
MIN_SAMPLES = 30             # 可交易样本量下限
MIN_LIFT = 0.0               # 相对基线提升下限（不劣于随机）
MIN_LIFT_BEARISH = 0.03      # 看跌规则：T+1 上涨率至少低于基线 3pp 才算"预示回调"（反向排除用）
P_VALUE_ALPHA = 0.05         # 显著性水平（默认 0.05；方向 A 扩充规则库可放宽到 0.10）
WALK_FORWARD_SPLIT = 0.8     # 训练/验证切分比例
# OOS（样本外）验证门槛：OOS 段 T+1 上涨率下限。
# 方向 A（扩充规则库）：OOS 与 45% 硬门槛解耦——OOS 段样本少、波动大，
# 用"不低于全量基线"代替"不低于 45%"判定，避免小样本波动误杀真实有效规则。
MIN_OOS_HIT_RATE = 0.0

# ── 多重检验校正（plans/23 §4.3）────────────────────────────────
# 问题：候选规则成百上千（网络 23 条 + auto 挖掘的 特征×阈值×组合），
# 单条 p<0.05 在多重检验下必然产生几十条"显著"噪声，而这些规则会进
# 条件概率表 → 每日推荐打分 → 进而驱动进化大脑调参。
#
# 三种模式（可进化参数 rule_fdr_mode）：
#   "off"     不做校正（历史行为）
#   "observe" **默认**：照常算 q 值并写进产物（可审计），但**不改动** verified 集合
#   "enforce" 真正把未过 FDR 的规则降级为 fdr_rejected
#
# 为什么默认 observe 而不是直接 enforce：实测当前只是 4 条看涨规则过 FDR 后剩 2 条，
# 而下游 daily_scan 有"verified_count < 3 → 回退仅 20 天向量"的兜底 ——
# 直接 enforce 会**静默降级每日推荐的规则信号**。所以先观测、拿到真实数字再决定。
FDR_ALPHA = 0.05
FDR_MODE = "observe"
FDR_MODES = ("off", "observe", "enforce")

# ★ HAC 复核（**重叠样本**修正；plans/24 §11.28 / D5）—— 与 FDR **同构**的三态：
#   FDR 管"多重比较"，HAC 管"重叠样本"；两件事必须分开做，否则会把两者混在一起。
HAC_MODE = "observe"
HAC_LAG = 4          # 持有期 5 日 ⇒ lag = 4（与 baseline_gate 的 b_hac 同档）

# 排行榜落盘
FORM_LEADERBOARD_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "form_leaderboard.json",
)


# ──────────────────────────────────────────────
# 单股票逐日扫描
# ──────────────────────────────────────────────


def scan_stock_rules(
    df: pd.DataFrame,
    ts_code: str,
    rules: list[RuleForm] | None = None,
    start_idx: int = 60,
    step: int = 1,
) -> dict[str, list[dict]]:
    """对单只股票逐日扫描所有规则，采集命中样本。

    Args:
        df: prepare_stock_data 输出
        ts_code: 股票代码
        rules: 要扫描的规则（默认全部 RULE_FORMS）
        start_idx: 起始下标（跳过形态工具数据不足的前段）
        step: 扫描步长（抽样用，1=逐日）

    Returns:
        {rule_key: [sample, ...]}，sample = {ts_code, date, hit_idx, ret_t1, hit_t1, tradeable}
    """
    rules = rules or RULE_FORMS
    if df is None or df.empty or len(df) < start_idx + 2:
        return {}
    closes = df["adj_close"].to_numpy(dtype=float)
    sealed = df["is_sealed_up"].to_numpy(dtype=bool) if "is_sealed_up" in df.columns else np.zeros(len(df), dtype=bool)
    dates = df["trade_date"].to_numpy()
    n = len(df)

    result: dict[str, list[dict]] = defaultdict(list)
    for idx in range(start_idx, n - 1, step):
        hits = detect_all(df, idx, verified_only=False)
        if not hits:
            continue
        # 结局：T+1 相对当日收盘
        ret_t1 = float(closes[idx + 1] / closes[idx] - 1.0) if closes[idx] else 0.0
        hit_t1 = bool(ret_t1 > 0)
        tradeable = not bool(sealed[idx])   # 命中日一字封死不可买
        sample = {
            "ts_code": ts_code,
            "date": str(pd.Timestamp(dates[idx]).date()),
            "hit_idx": idx,
            "ret_t1": round(ret_t1, 6),
            "hit_t1": hit_t1,
            "tradeable": tradeable,
        }
        for r in hits:
            result[r.key].append(sample)
    return result


# ──────────────────────────────────────────────
# 全市场扫描
# ──────────────────────────────────────────────


def scan_market_rules(
    max_stocks: int | None = None,
    start_date: str = "20150101",
    end_date: str = "",
    step: int = 1,
    progress_cb=None,
) -> dict[str, list[dict]]:
    """全市场逐日扫描所有规则，汇总命中样本。

    Args:
        max_stocks: 限制扫描股票数（调试用）
        start_date/end_date: 日期范围
        step: 扫描步长（1=逐日；调试可用 5 加速）
        progress_cb: fn(stage, pct)

    Returns:
        {rule_key: [sample, ...]}（全市场汇总）
    """
    pool = load_stock_pool()
    if pool.empty:
        logger.error("rule_verifier: 股票池为空")
        return {}
    codes = pool["ts_code"].tolist()
    if max_stocks:
        codes = codes[:max_stocks]

    all_hits: dict[str, list[dict]] = defaultdict(list)
    scanned = 0
    _scan_t0 = time.time()
    for i, ts_code in enumerate(codes):
        try:
            if is_st(ts_code, pool):
                continue
            df = prepare_stock_data(ts_code, start_date, end_date)
            if df.empty or len(df) < 100:
                continue
            df = filter_liquidity(df)
            if df.empty:
                continue
            hits = scan_stock_rules(df, ts_code, step=step)
            for k, samples in hits.items():
                all_hits[k].extend(samples)
            scanned += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"rule_verifier 处理 {ts_code} 失败: {exc}")
            continue

        if (i + 1) % 50 == 0 or (i + 1) == len(codes):
            if progress_cb:
                progress_cb("规则形态扫描", 0.3 * (i + 1) / max(len(codes), 1))
            elapsed = time.time() - _scan_t0
            logger.info(f"rule_verifier 进度 {i + 1}/{len(codes)} ({i + 1:5.1%}), 命中规则 {len(all_hits)}, 已用 {elapsed:.0f}s")

    if progress_cb:
        progress_cb("规则形态扫描", 0.35)
    logger.info(f"rule_verifier 完成: 扫描 {scanned} 只, 命中规则 {len(all_hits)}")
    return dict(all_hits)


def scan_market_rules_surge(
    max_stocks: int | None = None,
    start_date: str = "20150101",
    end_date: str = "",
    step: int = 1,
    progress_cb=None,
) -> dict[str, list[dict]]:
    """全市场 3 倍启动段内逐日扫描所有规则，采集命中样本（与每日推荐同源口径）。

    与 scan_market_rules（全市场普通日）的关键区别：
      - 只在 surge_scanner 识别的 3 倍启动段内逐日扫描。
        每日推荐（daily_scan）只在"确认启动形态"的时点应用规则，验证样本必须与之同源，
        否则普通日背景会把看涨规则命中率严重稀释（段内基线 T+1 上涨率约 63% vs 普通日 47%）。
      - 对齐规划 12 §5.1"主样本 = 3 倍股候选样本（与每日推荐同源）"的原始设计。

    Args:
        max_stocks: 限制股票数（调试用）
        start_date/end_date: 日期范围
        step: 扫描步长（段内抽样）
        progress_cb: fn(stage, pct)

    Returns:
        {rule_key: [sample, ...]}，sample 含 {ts_code, date, hit_idx, ret_t1, hit_t1, tradeable, in_surge}
    """
    from app.backtest.surge_scanner import scan_single_stock

    pool = load_stock_pool()
    if pool.empty:
        logger.error("rule_verifier(段内): 股票池为空")
        return {}
    codes = pool["ts_code"].tolist()
    if max_stocks:
        codes = codes[:max_stocks]

    all_hits: dict[str, list[dict]] = defaultdict(list)
    scanned = 0
    _scan_t0 = time.time()
    for i, ts_code in enumerate(codes):
        try:
            if is_st(ts_code, pool):
                continue
            df = prepare_stock_data(ts_code, start_date, end_date)
            if df.empty or len(df) < 100:
                continue
            df = filter_liquidity(df)
            if df.empty:
                continue
            closes = df["adj_close"].to_numpy(dtype=float)
            sealed = df["is_sealed_up"].to_numpy(dtype=bool) if "is_sealed_up" in df.columns else np.zeros(len(df), dtype=bool)
            dates = df["trade_date"].to_numpy()
            n = len(df)
            segs = scan_single_stock(df, ts_code)
            for seg in segs:
                lo = seg.low_idx
                hi = min(seg.high_idx, n - 2)
                if hi - lo < 3:
                    continue
                for idx in range(lo, hi + 1, step):
                    hits = detect_all(df, idx, verified_only=False)
                    if not hits:
                        continue
                    ret_t1 = float(closes[idx + 1] / closes[idx] - 1.0) if closes[idx] else 0.0
                    sample = {
                        "ts_code": ts_code,
                        "date": str(pd.Timestamp(dates[idx]).date()),
                        "hit_idx": idx,
                        "ret_t1": round(ret_t1, 6),
                        "hit_t1": bool(ret_t1 > 0),
                        "tradeable": not bool(sealed[idx]),
                        "in_surge": True,
                    }
                    for r in hits:
                        all_hits[r.key].append(sample)
            scanned += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"rule_verifier(段内) 处理 {ts_code} 失败: {exc}")
            continue

        if (i + 1) % 50 == 0 or (i + 1) == len(codes):
            if progress_cb:
                progress_cb("规则形态扫描(启动段)", 0.3 * (i + 1) / max(len(codes), 1))
            elapsed = time.time() - _scan_t0
            logger.info(f"rule_verifier(段内) 进度 {i + 1}/{len(codes)}, 命中规则 {len(all_hits)}, 已用 {elapsed:.0f}s")

    if progress_cb:
        progress_cb("规则形态扫描(启动段)", 0.35)
    logger.info(f"rule_verifier(段内) 完成: 扫描 {scanned} 只, 命中规则 {len(all_hits)}")
    return dict(all_hits)


# ──────────────────────────────────────────────
# 验证：45% 门槛 + 基线 + 显著性 + 样本外
# ──────────────────────────────────────────────


def _fdr_mode() -> str:
    """读多重检验校正模式（可进化参数 rule_fdr_mode；非法值回退 observe）。"""
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param("rule_fdr_mode", FDR_MODE)
        s = str(v or FDR_MODE).strip().lower()
        return s if s in FDR_MODES else FDR_MODE
    except Exception:  # noqa: BLE001
        return FDR_MODE


def _fdr_alpha() -> float:
    """读 FDR 目标（可进化参数 rule_fdr_alpha；≤0 视为关闭）。"""
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param("rule_fdr_alpha", FDR_ALPHA)
        return FDR_ALPHA if v is None else float(v)
    except Exception:  # noqa: BLE001
        return FDR_ALPHA


def _hac_mode() -> str:
    """读 HAC 复核模式（可进化参数 rule_fdr_hac；默认 observe，非法值回退）。"""
    try:
        from app.agents import evolution_config
        s = str(evolution_config.get_param("rule_fdr_hac", HAC_MODE) or HAC_MODE).strip().lower()
        return s if s in FDR_MODES else HAC_MODE
    except Exception:  # noqa: BLE001
        return HAC_MODE


def _hac_lag() -> int:
    """读 HAC 滞后阶数（可进化参数 rule_hac_lag；默认 4 = 持有期 5 日 − 1）。"""
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param("rule_hac_lag", HAC_LAG)
        return max(0, int(HAC_LAG if v is None else v))
    except Exception:  # noqa: BLE001
        return HAC_LAG


def hac_recheck(hits: dict[str, list[dict]], lag: int = HAC_LAG) -> dict:
    """★ HAC 复核（plans/24 §11.28 / D5）：修正**重叠样本**导致的显著性放大。

    为什么必须加：FDR 管的是**多重比较**，**不管重叠样本**。规则样本是**同一批日期**上的
    横截面（多条规则共享同一天）⇒ 日收益序列高度重叠，naive 显著性被系统性放大
    （§11.27/§11.28 实测 3.08→4.02；基准 B1 4.02→1.15）。

    口径：把每条规则的**可交易样本**按 `date` 聚合成「**日均 T+1 收益**」序列 →
    `Newey-West(lag)` 的 t；**只有 `t_hac > 2` 才算过**（与 baseline_gate 的 `b_hac` 同一档）。
    样本 < 30 天 ⇒ 不给 t（`pass=None`，即"不可判定"，**不是**"不通过"）。

    **只读入参，不改任何规则状态**（是否降级由调用方按 `rule_fdr_hac` 决定）。
    """
    out: dict[str, dict] = {}
    if not hits:
        return out
    try:
        from app.backtest.stats_correction import newey_west_t, plain_t
    except Exception:  # noqa: BLE001
        return out
    for key, samples in hits.items():
        tr = [s for s in (samples or [])
              if s.get("tradeable") and s.get("ret_t1") is not None]
        if not tr:
            continue
        byd: dict[str, list[float]] = {}
        for s in tr:
            byd.setdefault(str(s.get("date") or "")[:10], []).append(float(s["ret_t1"]))
        ser = [float(np.mean(v)) for k, v in sorted(byd.items()) if k and v]
        if len(ser) < 30:
            out[key] = {"n_days": len(ser), "mean": None, "t_naive": None,
                        "t_hac": None, "pass": None}
            continue
        tn = plain_t(ser)
        th = newey_west_t(ser, lag)
        out[key] = {
            "n_days": len(ser), "mean": round(float(np.mean(ser)), 6),
            "t_naive": (round(float(tn), 3) if tn == tn else None),
            "t_hac": (round(float(th), 3) if th == th else None),
            "pass": bool(th == th and th > 2.0),
        }
    return out


def _binomial_p(observed: float, n: int, p0: float) -> float:
    """单侧二项检验：H0: 真实率 <= p0。近似正态。"""
    if n <= 0:
        return 1.0
    se = math.sqrt(p0 * (1 - p0) / n)
    if se == 0:
        return 1.0
    z = (observed - p0) / se
    # 标准正态生存函数近似
    return 0.5 * math.erfc(z / math.sqrt(2))


def _baseline_t1(hits: dict[str, list[dict]]) -> float:
    """全市场基线：所有可交易样本的 T+1 上涨率。"""
    all_samples = [s for samples in hits.values() for s in samples if s["tradeable"]]
    if not all_samples:
        return 0.5
    return float(np.mean([s["hit_t1"] for s in all_samples]))


def verify_rule(
    rule: RuleForm,
    samples: list[dict],
    baseline: float,
    min_hit_rate: float = MIN_HIT_RATE,
    min_samples: int = MIN_SAMPLES,
    p_value_alpha: float = P_VALUE_ALPHA,
) -> dict:
    """对单个规则做验证，返回验证结果 dict（不直接改 rule 字段，由 verify_all 统一写回）。

    Args:
        p_value_alpha: 显著性水平（方向 A 可放宽到 0.10 以扩充规则库）
    """
    tradeable = [s for s in samples if s["tradeable"]]
    n = len(tradeable)
    if n < min_samples:
        return {"verified": False, "reason": "insufficient_samples", "samples": n}

    hit_t1 = [s["hit_t1"] for s in tradeable]
    ret_t1 = [s["ret_t1"] for s in tradeable]
    hit_rate = float(np.mean(hit_t1))
    avg_ret = float(np.mean(ret_t1))
    lift = hit_rate - baseline

    if rule.bullish:
        # ── 看涨规则：T+1 上涨率 >= 45% 硬门槛 + 不劣于基线 + 显著（主信号）──
        if hit_rate < min_hit_rate:
            return {"verified": False, "reason": "below_45pct", "samples": n,
                    "hit_rate_t1": hit_rate, "avg_ret_t1": avg_ret, "lift_t1": lift}
        if lift < MIN_LIFT:
            return {"verified": False, "reason": "below_baseline", "samples": n,
                    "hit_rate_t1": hit_rate, "avg_ret_t1": avg_ret, "lift_t1": lift}
        p = _binomial_p(hit_rate, n, baseline)
        if p >= p_value_alpha:
            return {"verified": False, "reason": "not_significant", "samples": n,
                    "hit_rate_t1": hit_rate, "avg_ret_t1": avg_ret, "lift_t1": lift, "p_value": round(p, 4)}
        return {"verified": True, "reason": "pass", "samples": n,
                "hit_rate_t1": hit_rate, "avg_ret_t1": avg_ret, "lift_t1": lift, "p_value": round(p, 4)}

    # ── 看跌规则（反面教材/反向排除）：T+1 上涨率显著低于基线 → 预示回调 ──
    # 在 3 倍启动段内：段内基线约 63%，看跌形态（乌云盖顶/射击之星等）出现后
    # T+1 上涨率降至 52%~59%，用于每日推荐对命中看跌形态的候选做负向降权/剔除。
    bearish_cap = baseline - MIN_LIFT_BEARISH
    if hit_rate > bearish_cap:
        return {"verified": False, "reason": "not_bearish_enough", "samples": n,
                "hit_rate_t1": hit_rate, "avg_ret_t1": avg_ret, "lift_t1": lift,
                "bearish_cap": round(bearish_cap, 4)}
    return {"verified": True, "reason": "pass_bearish", "samples": n,
            "hit_rate_t1": hit_rate, "avg_ret_t1": avg_ret, "lift_t1": lift,
            "fall_rate_t1": round(1.0 - hit_rate, 4)}


def verify_all(
    hits: dict[str, list[dict]],
    min_hit_rate: float = MIN_HIT_RATE,
    min_samples: int = MIN_SAMPLES,
    do_walk_forward: bool = True,
    p_value_alpha: float = P_VALUE_ALPHA,
    min_oos_hit_rate: float = MIN_OOS_HIT_RATE,
) -> dict:
    """验证全部规则，写回 RuleForm 字段，输出排行榜数据。

    Args:
        p_value_alpha: 显著性水平（方向 A 可放宽到 0.10 扩充规则库）
        min_oos_hit_rate: OOS 段 T+1 上涨率下限。默认 0.0 表示"不低于全量基线"
            （OOS 与 45% 硬门槛解耦，避免小样本波动误杀）；传 min_hit_rate 即恢复旧口径。

    Returns:
        {
          "baseline_t1": float,
          "rules": [RuleForm.to_dict() ...]（仅 verified；带 p_value / q_value 供审计），
          "deleted": [被删规则审计 ...（带 reason：含 fdr_rejected）]，
          "total_rules": n,
          "verified_count": n,
          "fdr": {mode, alpha, m, n_reject, n_fdr_rejected, ...},
        }

    多重检验校正（`rule_fdr_mode`）：off / **observe（默认）** / enforce。
    见文件头 FDR_MODES 的说明——默认只观测不降级，避免静默削弱每日推荐规则信号。
    """
    baseline = _baseline_t1(hits)
    entries: list[dict] = []

    for rule in RULE_FORMS:
        samples = hits.get(rule.key, [])
        res = verify_rule(rule, samples, baseline, min_hit_rate, min_samples,
                          p_value_alpha=p_value_alpha)

        # Walk-Forward 样本外验证（仅看涨规则；看跌规则按"低于基线"口径验证，OOS 逻辑不适用）
        if (
            do_walk_forward and rule.bullish and res["verified"]
            and len(samples) >= 20
        ):
            tradeable = [s for s in samples if s["tradeable"]]
            tradeable_sorted = sorted(tradeable, key=lambda s: s["date"])
            split = int(len(tradeable_sorted) * WALK_FORWARD_SPLIT)
            oos = tradeable_sorted[split:]
            if len(oos) >= 10:
                oos_hit = float(np.mean([s["hit_t1"] for s in oos]))
                # OOS 下限：默认不低于全量基线（方向 A）；可传 min_hit_rate 恢复旧口径
                oos_thr = min_oos_hit_rate if min_oos_hit_rate > 0 else baseline
                if oos_hit < oos_thr:
                    res = {"verified": False, "reason": "oos_below_thr", "samples": len(tradeable),
                           "hit_rate_t1": res["hit_rate_t1"], "avg_ret_t1": res["avg_ret_t1"],
                           "lift_t1": res["lift_t1"]}

        tradeable_n = sum(1 for s in samples if s["tradeable"])
        entries.append({
            "rule": rule, "res": res,
            "ratio": round(tradeable_n / len(samples), 4) if samples else 0.0,
        })

    # ── 多重检验校正 ────────────────────────────────────────────
    # 检验家族 = **所有走到了显著性检验的规则**（有 p_value 的那些）。
    # 未走到检验的（样本不足 / 没过 45% 硬门槛 / 看跌规则用另一套口径）不进 m：
    # 它们不是"可能的发现"，把它们算进 m 会人为放宽校正。
    # 代价：这是**偏宽松**的选择（m 小 → FDR 更易通过）；fdr 元数据里同时给出
    # n_bullish（全部看涨规则数），方便按更保守的口径复核。
    mode = _fdr_mode()
    alpha = _fdr_alpha()
    enabled = mode != "off" and alpha > 0
    testable = [(e["rule"].key, float(e["res"]["p_value"]))
                for e in entries if e["res"].get("p_value") is not None]
    qmap: dict[str, float] = {}
    fdr: dict = {
        "mode": mode, "method": "BH", "alpha": alpha, "enabled": bool(enabled),
        "m": 0, "n_reject": 0, "n_testable": len(testable),
        "n_bullish": sum(1 for e in entries if e["rule"].bullish),
        "n_fdr_rejected": 0,
        "note": ("observe：仅计算并记录 q 值，不改动 verified 集合" if mode == "observe"
                 else ("enforce：未过 FDR 的规则降级为 fdr_rejected" if mode == "enforce"
                       else "off：不做多重检验校正")),
    }
    if enabled and testable:
        rep = bh_fdr([p for _, p in testable], alpha)
        for pos, orig in enumerate(rep["idx"]):
            qmap[testable[orig][0]] = float(rep["q"][pos])
        fdr.update({"m": rep["m"], "n_reject": rep["n_reject"],
                    "threshold": rep["threshold"], "q_by_key": {
                        k: round(v, 5) for k, v in sorted(qmap.items())}})

    # ── ★ HAC 复核接入（plans/24 §11.28 / D5）─────────────────────
    #   `rule_fdr_hac`: off / **observe（默认）** / enforce —— 与 FDR 同构：
    #   observe 只记录 t_hac（可审计），**不改 verified 集合**；enforce 才真降级。
    hac_mode = _hac_mode()
    hac_map = hac_recheck(hits, _hac_lag()) if hac_mode != "off" else {}
    n_hac_rejected = 0

    verified_list: list[dict] = []
    deleted_list: list[dict] = []
    n_fdr_rejected = 0
    for e in entries:
        rule, res = e["rule"], e["res"]
        # 写回 RuleForm
        rule.hit_rate_t1 = float(res.get("hit_rate_t1", 0.0))
        rule.samples = int(res.get("samples", 0))
        rule.avg_ret_t1 = float(res.get("avg_ret_t1", 0.0))
        rule.tradeable_ratio = e["ratio"]

        ok = bool(res["verified"])
        reason = res.get("reason", "")
        q = qmap.get(rule.key)
        # 只有 enforce 模式才真正降级（observe 只记录，不动 verified 集合）
        if ok and mode == "enforce" and q is not None and q > alpha:
            ok = False
            reason = "fdr_rejected"
            n_fdr_rejected += 1
        # ★ HAC 第二档（D5）：同样只有 enforce 才降级；`pass=None`（样本不足）**不算过**。
        h = hac_map.get(rule.key)
        if ok and hac_mode == "enforce" and h is not None and h.get("pass") is not True:
            ok = False
            reason = "hac_rejected"
            n_hac_rejected += 1
        rule.verified = ok

        d = rule.to_dict()
        d["verified"] = ok
        d["reason"] = reason
        if res.get("p_value") is not None:
            d["p_value"] = res["p_value"]        # 落盘 p 值：否则事后无法复核校正结果
        if q is not None:
            d["q_value"] = round(q, 5)
            d["fdr_pass"] = bool(q <= alpha)
        if h is not None:                        # ★ 落盘 HAC 复核（可审计）
            d["hac_days"] = h["n_days"]
            d["t_naive_daily"] = h["t_naive"]
            d["t_hac"] = h["t_hac"]
            d["hac_pass"] = h["pass"]
        if ok:
            verified_list.append(d)
        else:
            deleted_list.append({
                "key": rule.key, "name": rule.name, "source": rule.source,
                "hit_rate_t1": rule.hit_rate_t1, "samples": rule.samples,
                "reason": reason,
                "p_value": res.get("p_value"),
                "q_value": None if q is None else round(q, 5),
            })

    fdr["n_fdr_rejected"] = n_fdr_rejected
    hac_meta = {
        "mode": hac_mode, "lag": _hac_lag(), "enabled": hac_mode != "off",
        "n_checked": len(hac_map),
        "n_pass": sum(1 for v in hac_map.values() if v.get("pass") is True),
        "n_undecidable": sum(1 for v in hac_map.values() if v.get("pass") is None),
        "n_hac_rejected": n_hac_rejected,
        "note": ("observe：仅计算并记录 HAC t（可用日序列 < 30 天的规则记 pass=None）"
                 if hac_mode == "observe" else
                 ("enforce：HAC t ≤ 2（或不可判定）的规则降级为 hac_rejected"
                  if hac_mode == "enforce" else "off：不做重叠样本复核")),
    }
    if hac_mode == "observe" and hac_map:
        would = sum(1 for e in entries if e["res"]["verified"]
                    and (hac_map.get(e["rule"].key) or {}).get("pass") is not True)
        logger.info(f"规则验证：HAC(observe) 已记录 t_hac；若改为 enforce，"
                    f"将有 {would} 条规则被降级")
    if mode == "enforce" and n_fdr_rejected:
        logger.info(f"规则验证：FDR(enforce) 降级 {n_fdr_rejected} 条未过校正的规则")
    elif mode == "observe" and qmap:
        would = sum(1 for e in entries if e["res"]["verified"]
                    and qmap.get(e["rule"].key, 1.0) > alpha)
        logger.info(f"规则验证：FDR(observe) 已记录 q 值；若改为 enforce，将有 {would} 条现存规则被降级")

    # ── ★ 统一门槛接线（plans/24 §11.17 / §11.31 / D4）───────────────
    #   把「这套规则能不能上生产」交给**同一个** `baseline_gate.judge_default()`，
    #   而不是每条链路各拍一套阈值。**只记录，不改 verified 集合**（observe 语义）。
    gate: dict | None = None
    try:
        from app.backtest import baseline_gate as BG
        from app.backtest.stats_correction import newey_west_t, plain_t
        all_tr = [s for samples in (hits or {}).values() for s in (samples or [])
                  if s.get("tradeable") and s.get("ret_t1") is not None]
        byd: dict[str, list[float]] = {}
        for s in all_tr:
            byd.setdefault(str(s.get("date") or "")[:10], []).append(float(s["ret_t1"]))
        ser = [float(np.mean(v)) for k, v in sorted(byd.items()) if k and v]
        if len(ser) >= 30:
            cand = BG.Candidate(
                name="规则集合（全部命中·可交易·T+1）",
                gross=float(np.mean(ser)), t=float(plain_t(ser)),
                t_hac=float(newey_west_t(ser, _hac_lag())),
                per_year=252.0, by_seg={}, n=len(ser))
            gate = BG.judge_default(cand)          # 实盘口径：0.5% 费率 + 20bp 滑点
            gate["coverage"] = BG.coverage(cand)
            gate["n_days"] = len(ser)
            if gate["coverage"]["missing"]:
                gate["verdict"] = "not_decidable"
        else:
            gate = {"verdict": "not_decidable", "n_days": len(ser),
                    "reason": f"组合日序列仅 {len(ser)} 天（< 30，功效不足）"}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"规则集合门槛判定失败（不影响验证结果）: {exc}")

    return {
        "baseline_t1": round(baseline, 4),
        "min_hit_rate": min_hit_rate,
        "rules": verified_list,
        "deleted": deleted_list,
        "total_rules": len(RULE_FORMS),
        "verified_count": len(verified_list),
        "fdr": fdr,
        "hac": hac_meta,
        "gate": gate,
    }


# ──────────────────────────────────────────────
# 固化 + 加载
# ──────────────────────────────────────────────


def save_form_leaderboard(leaderboard: dict, path: str = FORM_LEADERBOARD_FILE) -> None:
    """固化规则形态排行榜。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        leaderboard["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(leaderboard, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"规则形态排行榜已固化: {path}（verified {leaderboard.get('verified_count', 0)} 条）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"固化规则形态排行榜失败: {exc}")


def load_form_leaderboard(path: str = FORM_LEADERBOARD_FILE) -> dict | None:
    """加载已固化的规则形态排行榜。"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"加载规则形态排行榜失败: {exc}")
    return None


def load_verified_rule_dicts(path: str = FORM_LEADERBOARD_FILE) -> list[dict]:
    """加载已验证规则列表（每日推荐用）。"""
    lb = load_form_leaderboard(path)
    if not lb:
        return []
    return lb.get("rules", [])


def load_verified_rule_dicts_map(path: str = FORM_LEADERBOARD_FILE) -> dict[str, dict]:
    """加载已验证规则，按 key 建索引（每日推荐融合用）。"""
    return {r["key"]: r for r in load_verified_rule_dicts(path)}


def sync_rules_verified(leaderboard: dict | None = None) -> dict[str, dict]:
    """把已验证规则状态同步回 RULE_FORMS 对象（每日推荐进程启动时调用）。

    - 设置对应 RuleForm.verified / hit_rate_t1 / samples / form_type
    - 未验证规则 verified=False，每日推荐 detect_all(verified_only=True) 会跳过

    Returns:
        {key: leaderboard_dict}（已验证规则索引）
    """
    from app.backtest.rule_forms import RULE_FORMS_BY_KEY
    if leaderboard is None:
        leaderboard = load_form_leaderboard()
    verified_map: dict[str, dict] = {}
    rules = leaderboard.get("rules", []) if leaderboard else []
    for rd in rules:
        key = rd.get("key")
        if not key:
            continue
        verified_map[key] = rd
        rule = RULE_FORMS_BY_KEY.get(key)
        if rule is not None:
            rule.verified = True
            rule.hit_rate_t1 = float(rd.get("hit_rate_t1", 0.0))
            rule.samples = int(rd.get("samples", 0))
            rule.avg_ret_t1 = float(rd.get("avg_ret_t1", 0.0))
            rule.form_type = str(rd.get("form_type", rule.form_type))
    return verified_map


# ──────────────────────────────────────────────
# 一键流程
# ──────────────────────────────────────────────


def run_rule_verification(
    max_stocks: int | None = None,
    start_date: str = "20150101",
    end_date: str = "",
    step: int = 1,
    min_hit_rate: float = MIN_HIT_RATE,
    min_samples: int = MIN_SAMPLES,
    p_value_alpha: float = P_VALUE_ALPHA,
    min_oos_hit_rate: float = MIN_OOS_HIT_RATE,
    progress_cb=None,
    save: bool = True,
    mine_auto: bool = True,
    max_auto_rules: int = 12,
    sample_mode: str = "surge",
) -> dict:
    """完整执行规则形态回测验证：扫描 → 验证 network → 挖掘 auto → 统一验证 → 固化。

    样本口径（sample_mode）：
      - "surge"（默认）：在回测采样的 3 倍启动段内统计（与每日推荐同源）。
        段内基线 T+1 上涨率约 63%，看涨规则命中后普遍 60%~78%，
        看跌规则命中后显著低于基线（52%~59%）→ 作为反向排除"反面教材"。
        既省时（复用回测 3 倍段识别），又贴合每日推荐应用场景。
      - "market"：全市场普通日逐日统计（旧口径，基线约 47%，看涨规则易被稀释）。

    Args:
        max_stocks: 限制扫描股票数（调试用）
        mine_auto: 是否执行系统自总结规则挖掘（规划 P4b）
        max_auto_rules: auto 规则上限
        sample_mode: 样本口径 "surge"（默认）/ "market"
        p_value_alpha: 显著性水平（方向 A 扩充规则库可放宽到 0.10）
        min_oos_hit_rate: OOS 段下限（默认 0.0 = 不低于全量基线；传 min_hit_rate 恢复旧口径）

    Returns:
        排行榜 dict（含 baseline_t1 / rules / deleted / verified_count / auto_mined）
    """
    t0 = time.time()
    if progress_cb:
        progress_cb("规则形态全市场扫描", 0.05)
    if sample_mode == "surge":
        hits = scan_market_rules_surge(
            max_stocks=max_stocks, start_date=start_date, end_date=end_date,
            step=step, progress_cb=progress_cb,
        )
    else:
        hits = scan_market_rules(
            max_stocks=max_stocks, start_date=start_date, end_date=end_date,
            step=step, progress_cb=progress_cb,
        )
    if not hits:
        logger.error("规则形态扫描无命中样本，无法验证")
        return {"baseline_t1": 0.0, "rules": [], "deleted": [], "total_rules": len(RULE_FORMS),
                "verified_count": 0, "error": "无命中样本"}

    # 1) 验证 network 规则
    if progress_cb:
        progress_cb("网络经典规则验证", 0.5)
    leaderboard = verify_all(
        hits, min_hit_rate=min_hit_rate, min_samples=min_samples,
        p_value_alpha=p_value_alpha, min_oos_hit_rate=min_oos_hit_rate,
    )
    logger.info(f"network 规则验证完成：verified {leaderboard['verified_count']}")

    # 2) 系统自总结规则挖掘（auto 规则，规划 P4b）
    auto_mined: list[dict] = []
    if mine_auto:
        if progress_cb:
            progress_cb("系统自总结规则挖掘", 0.7)
        try:
            from app.backtest.rule_miner import run_auto_mining
            auto_rules = run_auto_mining(
                max_stocks=max_stocks, start_date=start_date, end_date=end_date,
                step=step, min_hit_rate=min_hit_rate, min_samples=min_samples,
                p_value_alpha=p_value_alpha, min_oos_hit_rate=min_oos_hit_rate,
                max_auto_rules=max_auto_rules,
                surge_only=(sample_mode == "surge"),
            )
            auto_mined = [r.to_dict() for r in auto_rules]
            if auto_rules:
                # 重新验证（auto 规则已加入 RULE_FORMS，需重新全量验证）
                leaderboard = verify_all(
                    hits, min_hit_rate=min_hit_rate, min_samples=min_samples,
                    p_value_alpha=p_value_alpha, min_oos_hit_rate=min_oos_hit_rate,
                )
                logger.info(f"auto 规则加入后重新验证：verified {leaderboard['verified_count']}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"系统自总结规则挖掘失败: {exc}")
    leaderboard["auto_mined"] = auto_mined
    leaderboard["elapsed"] = round(time.time() - t0, 2)

    # 无规则幸存兜底：verified_count < 3 → 标记回退
    leaderboard["fallback"] = leaderboard["verified_count"] < 3
    if leaderboard["fallback"]:
        logger.warning("规则形态验证：幸存规则 < 3，每日推荐将回退为'仅 20 天向量'模式")

    if save:
        save_form_leaderboard(leaderboard)
    if progress_cb:
        progress_cb("完成", 1.0)
    return leaderboard
