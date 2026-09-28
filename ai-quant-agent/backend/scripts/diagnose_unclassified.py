"""U（无法归类）段诊断（plans/23 §十三 T5）

**问题（实测 backtest_20260917_153645.json）**
```
by_form: A{success:7}  B{success:23}  C{success:36}  D{success:110}  E{success:6}  U{success:134, failure_A:43, near:76}
```
U 占 success 的 **42%**：形态层里"最能解释真实爆发"的一类居然是"无法归类"，
导致按形态分层统计/同形态配对失去意义（A 类只有 7 个 success 样本）。

**本脚本先诊断、再动手**：对每个 U 段统计启动前后结构特征，并按 success / failure 分组对比，
回答"U 到底是什么、能不能拆出一个有区分度的新形态"。

输出：
  1. U 段被否原因分布（A/B/C/D/E 各卡在哪一步）
  2. U-success vs U-failure 的结构特征差异（哪些特征有区分度）
  3. 候选新形态「F 底部吸筹/窄幅缩量后启动」的覆盖率与组内成功率（探索性）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/diagnose_unclassified.py --stocks 200
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from app.backtest.loader import load_stock_pool, prepare_stock_data, is_st
from app.backtest.surge_scanner import scan_single_stock, dedupe_segments
from app.backtest.form_classifier import (
    classify_batch, FORM_U, _detect_platform,
    PLATFORM_WIN_MIN, PLATFORM_VOLATILITY,
)
from app.backtest.outcome_tracker import (
    batch_classify_outcomes, filter_noisy_samples,
    LABEL_SUCCESS, LABEL_FAILURE_A, LABEL_FAILURE_B,
)

# 候选新形态「F 底部吸筹/窄幅缩量后启动」的探索阈值（仅诊断用，验证通过才进分类器）
F_PRE_RANGE_MAX = 0.50      # 启动前 60 日振幅上限（窄幅）
F_VOL_SHRINK_MAX = 0.90     # 近 20 日均量 / 前 40 日均量 上限（缩量）
F_POST20_RISE_MIN = 0.15    # 启动后 20 日涨幅下限（确认真启动）


def _features(df, low_idx: int) -> dict:
    close = df["adj_close"].to_numpy()
    vol = df["vol"].to_numpy()
    n = len(close)
    out: dict = {}
    pre = close[max(0, low_idx - 60): low_idx]
    if len(pre) >= 20:
        out["pre_range"] = float(pre.max() / pre.min() - 1)
        out["pre_slope"] = float(pre[-1] / pre[0] - 1) if pre[0] else 0.0
        out["prior_rise"] = float(pre.max() / pre[0] - 1) if pre[0] else 0.0
        p20 = pre[-20:]
        out["pre20_drop"] = float(p20[0] / p20[-1] - 1) if p20[-1] else 0.0
        v_prev = float(np.nanmean(vol[max(0, low_idx - 60): max(0, low_idx - 20)])) or 1.0
        v_last = float(np.nanmean(vol[max(0, low_idx - 20): low_idx])) or 1.0
        out["vol_shrink"] = v_last / v_prev
    else:
        out.update({"pre_range": np.nan, "pre_slope": np.nan, "prior_rise": np.nan,
                    "pre20_drop": np.nan, "vol_shrink": np.nan})
    post5 = close[low_idx: min(low_idx + 5, n)]
    post10 = close[low_idx: min(low_idx + 10, n)]
    post20 = close[low_idx: min(low_idx + 20, n)]
    out["post5_rise"] = float(post5[-1] / post5[0] - 1) if len(post5) >= 3 and post5[0] else np.nan
    out["post10_rise"] = float(post10[-1] / post10[0] - 1) if len(post10) >= 5 and post10[0] else np.nan
    out["post20_rise"] = float(post20[-1] / post20[0] - 1) if len(post20) >= 10 and post20[0] else np.nan
    v20 = float(np.nanmean(vol[max(0, low_idx - 20): low_idx])) or 1.0
    out["post5_vol_ratio"] = (float(np.nanmax(vol[low_idx: min(low_idx + 5, n)])) / v20
                              if n > low_idx else np.nan)
    # 平台检测（两种口径）
    found_before, _, pv_before, _ = _detect_platform(df, low_idx - 1)
    found_off, _, _, _ = _detect_platform(df, low_idx)
    out["plat_before"] = float(found_before)
    out["plat_before_flat"] = float(found_before and pv_before < PLATFORM_VOLATILITY * 1.6)
    out["plat_at_low"] = float(found_off)
    return out


def _reason(f: dict) -> str:
    """U 段被否原因（按分类器判定顺序推测）。"""
    rs = []
    if not (f.get("prior_rise") is not None and not np.isnan(f.get("prior_rise", np.nan))
            and f["prior_rise"] > 0.5):
        rs.append("no_prior_rise(C)")
    if not (not np.isnan(f.get("pre20_drop", np.nan)) and f["pre20_drop"] > 0.25):
        rs.append("no_pre_drop(B)")
    if not f.get("plat_before"):
        rs.append("no_platform(A)")
    if not (not np.isnan(f.get("post10_rise", np.nan)) and f["post10_rise"] > 0.30):
        rs.append("no_fast_rise(D)")
    if not (not np.isnan(f.get("post5_rise", np.nan)) and f["post5_rise"] > 0.05):
        rs.append("no_post5_pop(detail)")
    return "|".join(rs[:3]) or "other"


def _mean(vals) -> float:
    a = [v for v in vals if v is not None and not (isinstance(v, float) and np.isnan(v))]
    return float(np.mean(a)) if a else float("nan")


def diagnose(max_stocks: int, start_date: str, end_date: str) -> dict:
    pool = load_stock_pool()
    if pool.empty:
        return {"error": "股票池为空"}
    codes = pool["ts_code"].tolist()[:max_stocks]
    u_feats: dict[str, list[dict]] = defaultdict(list)   # label -> [features]
    reason_counter: Counter = Counter()
    rule_hits: dict[str, Counter] = defaultdict(Counter)
    total_u = 0
    total_seg = 0
    form_counter: Counter = Counter()

    for i, code in enumerate(codes):
        try:
            if is_st(code, pool):
                continue
            df = prepare_stock_data(code, start_date, end_date)
            if df.empty:
                continue
            segs = dedupe_segments(scan_single_stock(df, code))
            if not segs:
                continue
            classified = classify_batch(df, segs)
            outcomes = filter_noisy_samples(df, batch_classify_outcomes(df, classified))
            total_seg += len(classified)
            form_counter.update(c.form_type for c in classified)
            for o in outcomes:
                if o.form_type != FORM_U:
                    continue
                low_idx = int(getattr(o, "t0_idx", -1))
                if low_idx < PLATFORM_WIN_MIN:
                    continue
                total_u += 1
                f = _features(df, low_idx)
                f["label"] = o.label
                f["ts_code"] = code
                u_feats[o.label].append(f)
                reason_counter[_reason(f)] += 1
                hit = (not np.isnan(f.get("pre_range", np.nan)) and f["pre_range"] <= F_PRE_RANGE_MAX
                       and not np.isnan(f.get("vol_shrink", np.nan)) and f["vol_shrink"] <= F_VOL_SHRINK_MAX
                       and not np.isnan(f.get("post20_rise", np.nan)) and f["post20_rise"] >= F_POST20_RISE_MIN)
                rule_hits["F_candidate"][bool(hit)] += 1
                rule_hits["F_by_label"][(bool(hit), o.label)] += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  {code} 失败: {exc}")
            continue
        if (i + 1) % 50 == 0:
            print(f"  进度 {i + 1}/{len(codes)}，U 段累计 {total_u}")

    metrics = ["pre_range", "pre_slope", "prior_rise", "pre20_drop", "vol_shrink",
               "post5_rise", "post10_rise", "post20_rise", "post5_vol_ratio",
               "plat_before", "plat_before_flat", "plat_at_low"]
    by_label = {}
    for label in (LABEL_SUCCESS, LABEL_FAILURE_A, LABEL_FAILURE_B):
        rows = u_feats.get(label, [])
        by_label[label] = {
            "n": len(rows),
            "features": {m: round(_mean([r.get(m) for r in rows]), 4) for m in metrics},
        }

    succ = u_feats.get(LABEL_SUCCESS, [])
    fail = u_feats.get(LABEL_FAILURE_A, []) + u_feats.get(LABEL_FAILURE_B, [])
    diff = {m: round(_mean([r.get(m) for r in succ]) - _mean([r.get(m) for r in fail]), 4)
            for m in metrics}

    fh = rule_hits["F_candidate"]
    hit_n = int(fh.get(True, 0))
    miss_n = int(fh.get(False, 0))
    fl = rule_hits["F_by_label"]
    succ_in_rule = int(fl.get((True, LABEL_SUCCESS), 0))
    return {
        "sampled_stocks": len(codes),
        "segments": total_seg,
        "U_segments": total_u,
        "form_dist": dict(form_counter),
        "U_by_label": {k: len(v) for k, v in u_feats.items()},
        "reason_top": reason_counter.most_common(12),
        "features_by_label": by_label,
        "success_minus_failure": diff,
        "F_candidate": {
            "thresholds": {"pre_range_max": F_PRE_RANGE_MAX,
                           "vol_shrink_max": F_VOL_SHRINK_MAX,
                           "post20_rise_min": F_POST20_RISE_MIN},
            "hit": hit_n, "miss": miss_n,
            "coverage_of_U": round(hit_n / total_u, 3) if total_u else 0.0,
            "success_in_rule": succ_in_rule,
            "success_rate_in_rule": round(succ_in_rule / hit_n, 3) if hit_n else 0.0,
            "success_rate_outside": round((len(succ) - succ_in_rule) / miss_n, 3) if miss_n else 0.0,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=200)
    ap.add_argument("--start", default="20100101")
    ap.add_argument("--end", default="")
    ap.add_argument("--out", default="data/unclassified_diag.json")
    args = ap.parse_args()
    res = diagnose(args.stocks, args.start, args.end)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    try:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"已写入 {args.out}")
    except Exception as exc:  # noqa: BLE001
        print(f"写入失败: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
