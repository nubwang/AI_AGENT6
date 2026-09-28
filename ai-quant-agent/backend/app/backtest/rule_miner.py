"""系统自总结规则挖掘（rule_miner）

对齐规划：plans/12-网络经典形态规则库.md v4.0 第四章 + 4.2
  目的：在回测中"模仿网络经典形态方式"，从数据自动总结出可编码、可解释的新规则（auto 规则）。
  与黑盒聚类不同：挖出的规则是"特征阈值条件"形式（如 "vol_ratio_20 >= 1.5 且 rsi_14 >= 60"），
  可读、可执行、可验证。

方法（规则学习）：
  1. 样本：全市场逐日扫描，每个交易日提取可解释特征快照（CANDIDATE_FEATURES）+ T+1 结局
  2. 单特征分位切分：对每个候选特征，在若干分位阈值切分，统计"命中侧"的 T+1 上涨率
  3. 规则生成：命中侧 hit_rate_t1 ≥ 45% 且样本量 ≥ MIN_SAMPLES 且 lift > 0 的特征/阈值 → 生成 auto 规则
  4. 组合（可选）：两两特征 AND 组合，进一步挖高概率规则
  5. 相关性去重（P6）：与已验证 network 规则、已选 auto 规则高度重叠的丢弃
  6. 输出：auto 规则列表（RuleForm(source='auto', conditions=[...])），加入 RULE_FORMS 注册表

依赖：
  - rule_forms.CANDIDATE_FEATURES / extract_feature_snapshot / RuleForm / ThresholdCondition
  - rule_verifier.scan_market_rules（或自建扫描）
"""
from __future__ import annotations

import time
from collections import defaultdict

import numpy as np
import pandas as pd

from app.core.logger import logger
from app.backtest.rule_forms import (
    RuleForm, ThresholdCondition, CANDIDATE_FEATURES, RULE_FORMS, RULE_FORMS_BY_KEY,
)
from app.backtest.rule_verifier import MIN_HIT_RATE, MIN_SAMPLES, scan_market_rules

# 挖掘参数
QUANTILES = (0.30, 0.50, 0.70, 0.85, 0.90)  # 尝试的单特征切分分位（取高分组；方向 A 增加 0.90 高分位）
MAX_AUTO_RULES = 12                     # 最多生成多少条 auto 规则
MIN_LIFT_AUTO = 0.0                     # auto 规则最小 lift（相对基线；方向 A 由 0.02 放宽到 0，≥基线即可）
MAX_RULES_PER_FEATURE = 2               # 方向 A：每特征最多保留的切分规则数（原为 1，扩候选）
MAX_OVERLAP = 0.85                      # 规则重叠上限（相关性去重）


# ──────────────────────────────────────────────
# 特征快照收集
# ──────────────────────────────────────────────


def collect_snapshots(hits_by_key: dict[str, list[dict]] | None = None,
                      **scan_kwargs) -> list[dict]:
    """收集全市场特征快照样本（含 T+1 结局与可交易性）。

    优先复用 scan_market_rules 的结果，但需要每个命中日的特征快照。
    简化实现：直接独立扫描一次，逐日提取快照 + T+1。

    Returns:
        [{"snapshot": {feat: val}, "ret_t1": float, "hit_t1": bool, "tradeable": bool, "date": str}]
    """
    from app.backtest.loader import load_stock_pool, prepare_stock_data, is_st, filter_liquidity
    from app.backtest.rule_forms import extract_feature_snapshot

    pool = load_stock_pool()
    if pool.empty:
        return []
    codes = pool["ts_code"].tolist()
    max_stocks = scan_kwargs.get("max_stocks")
    if max_stocks:
        codes = codes[:max_stocks]
    start_date = scan_kwargs.get("start_date", "20150101")
    end_date = scan_kwargs.get("end_date", "")
    step = scan_kwargs.get("step", 1)
    # surge_only：只在 3 倍启动段内收集快照（与每日推荐/scan_market_rules_surge 同源，
    # 段内 T+1 上涨率约 63% 而非普通日 47%，挖出的规则才匹配每日推荐应用场景，且省时）
    surge_only = scan_kwargs.get("surge_only", False)

    samples: list[dict] = []
    _scan_t0 = time.time()
    for _i, ts_code in enumerate(codes):
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
            if surge_only:
                # 只在 3 倍启动段内逐日采样（复用 surge_scanner 段识别，与每日推荐同源）
                from app.backtest.surge_scanner import scan_single_stock
                idxs: list[int] = []
                for sg in scan_single_stock(df, ts_code):
                    lo = sg.low_idx
                    hi = min(sg.high_idx, n - 2)
                    if hi - lo >= 3:
                        idxs.extend(range(lo, hi + 1, step))
            else:
                idxs = list(range(60, n - 1, step))
            for idx in idxs:
                snap = extract_feature_snapshot(df, idx)
                if not snap or all(v is None for v in snap.values()):
                    continue
                ret_t1 = float(closes[idx + 1] / closes[idx] - 1.0) if closes[idx] else 0.0
                samples.append({
                    "snapshot": snap,
                    "ret_t1": ret_t1,
                    "hit_t1": bool(ret_t1 > 0),
                    "tradeable": not bool(sealed[idx]),
                    "date": str(pd.Timestamp(dates[idx]).date()),
                })
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"rule_miner 收集 {ts_code} 失败: {exc}")
            continue
        # 进度日志：每 50 只 + 耗时提示（避免长时间"卡住"假象）
        if (_i + 1) % 50 == 0 or (_i + 1) == len(codes):
            _elapsed = time.time() - _scan_t0
            logger.info(
                f"rule_miner 收集进度: {_i + 1}/{len(codes)} ({(_i + 1) / len(codes):5.1%}), "
                f"样本 {len(samples)}, 已用 {_elapsed:.0f}s"
            )
    logger.info(f"rule_miner: 收集特征快照样本 {len(samples)} 条")
    return samples


# ──────────────────────────────────────────────
# 单特征分位切分挖掘
# ──────────────────────────────────────────────


def mine_single_feature_rules(
    samples: list[dict],
    baseline: float,
    min_hit_rate: float = MIN_HIT_RATE,
    min_samples: int = MIN_SAMPLES,
) -> list[RuleForm]:
    """对每个候选特征做分位切分，挖出高 T+1 概率规则。

    Returns:
        auto RuleForm 列表（conditions 单条件）
    """
    rules: list[RuleForm] = []
    for feat, fn in CANDIDATE_FEATURES.items():
        # 收集该特征有效样本
        vals: list[tuple[float, bool]] = []
        for s in samples:
            if not s["tradeable"]:
                continue
            v = s["snapshot"].get(feat)
            if v is None or v != v:
                continue
            vals.append((float(v), bool(s["hit_t1"])))
        if len(vals) < min_samples:
            continue
        arr = np.array([v for v, _ in vals])
        hits = np.array([h for _, h in vals])

        kept = 0
        for q in QUANTILES:
            thr = float(np.quantile(arr, q))
            if thr == float(np.quantile(arr, 1.0)):
                continue
            mask = arr >= thr
            n_hit = int(mask.sum())
            if n_hit < min_samples:
                continue
            hit_rate = float(hits[mask].mean())
            if hit_rate < min_hit_rate or hit_rate < baseline + MIN_LIFT_AUTO:
                continue
            # 生成规则
            rule = RuleForm(
                key=f"auto_{feat}_{int(thr * 1000)}",
                name=f"{_feat_name(feat)}≥{thr:.2f}",
                source="auto",
                form_type="D",
                lookback_days=20,
                description=f"系统自总结：{_feat_name(feat)} ≥ {thr:.2f} 时次日上涨率 {hit_rate:.1%}",
                bullish=True,
                conditions=[ThresholdCondition(feature=feat, threshold=thr, direction="gte")],
            )
            rule.hit_rate_t1 = hit_rate
            rule.samples = n_hit
            rules.append(rule)
            kept += 1
            # 方向 A：每特征保留多个高分位切分（原为只取 1 个最优切分，扩候选量）
            if kept >= MAX_RULES_PER_FEATURE:
                break
    return rules


def _feat_name(feat: str) -> str:
    names = {
        "vol_ratio_20": "量比(20日)", "vol_ratio_5": "量比(5日)", "rsi_14": "RSI14",
        "break_5d_high": "突破5日新高", "ret_3d": "3日涨幅", "ret_5d": "5日涨幅",
        "ret_10d": "10日涨幅", "body_ratio": "实体占比", "upper_shadow": "上影线",
        "ma5_minus_ma10": "MA5-MA10", "close_above_ma20": "站上MA20",
    }
    return names.get(feat, feat)


# ──────────────────────────────────────────────
# 组合规则（两两 AND）
# ──────────────────────────────────────────────


def mine_combo_rules(
    samples: list[dict],
    baseline: float,
    min_hit_rate: float = MIN_HIT_RATE,
    min_samples: int = MIN_SAMPLES,
) -> list[RuleForm]:
    """两两特征 AND 组合挖掘（提升概率）。"""
    rules: list[RuleForm] = []
    feats = list(CANDIDATE_FEATURES.keys())
    # 预提取有效样本的向量化数据
    rows: list[dict] = []
    for s in samples:
        if not s["tradeable"]:
            continue
        rows.append({"snap": s["snapshot"], "hit": bool(s["hit_t1"])})
    if len(rows) < min_samples:
        return rules

    for i, f1 in enumerate(feats):
        for f2 in feats[i + 1:]:
            # 收集两特征都有效的样本
            pair: list[tuple[float, float, bool]] = []
            for r in rows:
                v1 = r["snap"].get(f1)
                v2 = r["snap"].get(f2)
                if v1 is None or v1 != v1 or v2 is None or v2 != v2:
                    continue
                pair.append((float(v1), float(v2), r["hit"]))
            if len(pair) < min_samples * 2:
                continue
            a = np.array([p[0] for p in pair])
            b = np.array([p[1] for p in pair])
            h = np.array([p[2] for p in pair], dtype=bool)

            # 用各自 60 分位作阈值（高分组）
            ta = float(np.quantile(a, 0.60))
            tb = float(np.quantile(b, 0.60))
            mask = (a >= ta) & (b >= tb)
            n_hit = int(mask.sum())
            if n_hit < min_samples:
                continue
            hit_rate = float(h[mask].mean())
            if hit_rate < min_hit_rate or hit_rate < baseline + MIN_LIFT_AUTO:
                continue
            rule = RuleForm(
                key=f"auto_{f1}_{f2}",
                name=f"{_feat_name(f1)}≥{ta:.2f}&{_feat_name(f2)}≥{tb:.2f}",
                source="auto",
                form_type="D",
                lookback_days=20,
                description=f"系统自总结：{_feat_name(f1)}≥{ta:.2f} 且 {_feat_name(f2)}≥{tb:.2f} 时次日上涨率 {hit_rate:.1%}",
                bullish=True,
                conditions=[
                    ThresholdCondition(feature=f1, threshold=ta, direction="gte"),
                    ThresholdCondition(feature=f2, threshold=tb, direction="gte"),
                ],
            )
            rule.hit_rate_t1 = hit_rate
            rule.samples = n_hit
            rules.append(rule)
    return rules


# ──────────────────────────────────────────────
# 相关性去重（P6）
# ──────────────────────────────────────────────


def _rule_overlap(rule_a: RuleForm, rule_b: RuleForm) -> float:
    """两规则命中样本重叠度（基于条件特征交集近似，简化用特征重合度）。"""
    fa = {c.feature for c in rule_a.conditions}
    fb = {c.feature for c in rule_b.conditions}
    if not fa or not fb:
        return 0.0
    inter = len(fa & fb)
    union = len(fa | fb)
    return inter / union if union else 0.0


def dedupe_rules(candidates: list[RuleForm], existing: list[RuleForm], max_overlap: float = MAX_OVERLAP) -> list[RuleForm]:
    """相关性去重：与已验证规则（network/已选 auto）特征重叠过高的丢弃。"""
    keep: list[RuleForm] = []
    pool = [r for r in existing if r.verified] + keep
    for r in sorted(candidates, key=lambda x: -x.hit_rate_t1):
        if any(_rule_overlap(r, p) > max_overlap for p in pool):
            logger.info(f"rule_miner 去重丢弃 {r.key}（与已选规则重叠）")
            continue
        keep.append(r)
        pool.append(r)
    return keep


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────


def run_auto_mining(
    max_stocks: int | None = None,
    start_date: str = "20150101",
    end_date: str = "",
    step: int = 1,
    min_hit_rate: float = MIN_HIT_RATE,
    min_samples: int = MIN_SAMPLES,
    p_value_alpha: float = 0.05,
    min_oos_hit_rate: float = 0.0,
    max_auto_rules: int = MAX_AUTO_RULES,
    surge_only: bool = False,
) -> list[RuleForm]:
    """执行系统自总结规则挖掘：收集快照 → 单特征 + 组合挖掘 → 去重 → 加入 RULE_FORMS。

    Args:
        surge_only: 只在 3 倍启动段内收集样本（与每日推荐/scan_market_rules_surge 同源）。
        p_value_alpha / min_oos_hit_rate: 统一验证门槛（方向 A：挖出的 auto 规则
            由 run_rule_verification 的 verify_all 统一验证，这里透传记录供日志/展示）。

    Returns:
        挖掘出的 auto RuleForm 列表（已加入 RULE_FORMS 注册表，verified=False，待 verify_all 验证）
    """
    logger.info("rule_miner: 开始系统自总结规则挖掘")
    samples = collect_snapshots(
        max_stocks=max_stocks, start_date=start_date, end_date=end_date,
        step=step, surge_only=surge_only,
    )
    if not samples:
        logger.warning("rule_miner: 无快照样本")
        return []

    # 基线：全部可交易样本 T+1 上涨率
    tradeable_hits = [s["hit_t1"] for s in samples if s["tradeable"]]
    baseline = float(np.mean(tradeable_hits)) if tradeable_hits else 0.5
    logger.info(f"rule_miner: 基线 T+1 上涨率 {baseline:.3f}")

    # 1) 单特征 + 组合挖掘
    single = mine_single_feature_rules(samples, baseline, min_hit_rate, min_samples)
    combo = mine_combo_rules(samples, baseline, min_hit_rate, min_samples)
    candidates = single + combo
    candidates = sorted(candidates, key=lambda r: -r.hit_rate_t1)
    logger.info(f"rule_miner: 原始候选规则 {len(candidates)} 条（单特征 {len(single)} + 组合 {len(combo)}）")

    # 2) 相关性去重（相对已验证 network 规则 + 候选内部）
    existing_verified = [r for r in RULE_FORMS if r.verified]
    candidates = dedupe_rules(candidates, existing_verified)
    candidates = candidates[:max_auto_rules]

    # 3) 加入 RULE_FORMS 注册表（供后续 verify_all 统一验证 + 每日推荐使用）
    added = 0
    for r in candidates:
        if r.key in RULE_FORMS_BY_KEY:
            continue
        RULE_FORMS.append(r)
        RULE_FORMS_BY_KEY[r.key] = r
        added += 1
    logger.info(f"rule_miner: 新增 auto 规则 {added} 条，注册表现有 {len(RULE_FORMS)} 条")
    return candidates
