"""形态覆盖度分析脚本（analyze_form_coverage）

目标：用数据回答"5 种启动形态（A/B/C/D/E）是否够用、是否分类太少"：

  1. 召回-分类漏斗：召回层候选段 → 分类层成功/无法归类（衡量"分类层召回流失"）
  2. 各形态正负样本分布：success / failure_A / failure_B / near_success / discard
  3. D 兜底形态内部聚类纯度：D 是否"垃圾桶"（内部异构、需要细分）还是内部同质

与 runner 的口径对齐：
  - 同一套股票池/前复权/停牌/封板/流动性过滤（loader）
  - 同一套召回（surge_scanner）+ 形态分类（form_classifier）+ 分流（outcome_tracker）
  - 聚类复用 cluster.cluster_by_form（同形态 KMeans，纯 numpy）

运行：
  cd ai-quant-agent/backend
  python scripts/analyze_form_coverage.py --max-stocks 200
  python scripts/analyze_form_coverage.py --out data/form_coverage.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from app.core.logger import logger
from app.backtest.loader import load_stock_pool, prepare_stock_data, is_st, filter_liquidity
from app.backtest.surge_scanner import scan_single_stock, dedupe_segments, MIN_LIST_DAYS
from app.backtest.form_classifier import classify_segment, FORM_A, FORM_B, FORM_C, FORM_D, FORM_E
from app.backtest.outcome_tracker import (
    batch_classify_outcomes, filter_noisy_samples, sample_summary,
    LABEL_SUCCESS, LABEL_NEAR, LABEL_FAILURE_A, LABEL_FAILURE_B, LABEL_DISCARD,
)
from app.backtest.pattern_miner import extract_for_outcomes
from app.backtest.feature_extractor import FEATURE_NAMES, MATCH_FEATURES, FeatureVector
from app.backtest.cluster import cluster_by_form

FORMS = [FORM_A, FORM_B, FORM_C, FORM_D, FORM_E]
FORM_LABEL = {FORM_A: "平台突破", FORM_B: "V型反转", FORM_C: "中继加速", FORM_D: "直接拉升", FORM_E: "连板"}
OUTCOME_LABELS = [LABEL_SUCCESS, LABEL_NEAR, LABEL_FAILURE_A, LABEL_FAILURE_B, LABEL_DISCARD]


def _fmt_ratio(a: int, b: int) -> str:
    return f"{a}/{b} ({a / b:.1%})" if b else f"{a}/{b}"


def analyze(
    start_date: str,
    end_date: str,
    max_stocks: int | None,
    d_n_clusters: int = 4,
) -> dict:
    """逐股扫描 + 分类 + 分流 + D 形态聚类纯度，返回汇总统计。"""
    t0 = time.time()
    pool = load_stock_pool()
    if pool.empty:
        logger.error("股票池为空，请先采集 stock_basic")
        return {"error": "股票池为空"}

    codes = pool["ts_code"].tolist()
    if max_stocks:
        codes = codes[:max_stocks]

    # ── 累计统计 ──
    funnel = {"scanned_stocks": 0, "candidate_segments": 0, "classified_segments": 0}
    unclassified: list[dict] = []          # 无法归类的段（记录 gain/duration，判断漏掉的高涨幅段）
    form_count: Counter = Counter()        # 分类成功段的形态分布
    samples_by_form: dict[str, Counter] = {f: Counter() for f in FORMS}  # 分流后各形态标签分布
    d_samples: list[FeatureVector] = []    # D 形态 成功/失败 样本特征（供聚类纯度）

    for i, ts_code in enumerate(codes):
        try:
            if is_st(ts_code, pool):
                continue
            df = prepare_stock_data(ts_code, start_date, end_date)
            if df.empty or len(df) < MIN_LIST_DAYS:
                continue
            df = filter_liquidity(df)
            if df.empty:
                continue
            segs = dedupe_segments(scan_single_stock(df, ts_code))
            if not segs:
                continue
            funnel["scanned_stocks"] += 1
            funnel["candidate_segments"] += len(segs)

            classified = []
            for seg in segs:
                cls = classify_segment(df, seg)
                if cls is None:
                    unclassified.append({
                        "ts_code": ts_code,
                        "gain": round(seg.gain, 3),
                        "duration": seg.duration,
                        "low_date": str(seg.low_date.date()),
                    })
                else:
                    classified.append(cls)
                    form_count[cls.form_type] += 1
            funnel["classified_segments"] += len(classified)
            if not classified:
                continue

            # 分流 + 噪音过滤（与 runner 口径一致）
            outcomes = batch_classify_outcomes(df, classified)
            outcomes = filter_noisy_samples(df, outcomes)
            for o in outcomes:
                samples_by_form[o.form_type][o.label] += 1

            # 收集 D 形态正负样本特征（成功 + 失败A + 失败B）
            d_outcomes = [o for o in outcomes
                          if o.form_type == FORM_D and o.label in (LABEL_SUCCESS, LABEL_FAILURE_A, LABEL_FAILURE_B)]
            if d_outcomes:
                d_samples.extend(extract_for_outcomes(df, d_outcomes, (LABEL_SUCCESS, LABEL_FAILURE_A, LABEL_FAILURE_B)))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"analyze_form_coverage 处理 {ts_code} 失败: {exc}")
            continue

        if (i + 1) % 200 == 0:
            logger.info(f"进度 {i + 1}/{len(codes)}, 候选段 {funnel['candidate_segments']}, "
                        f"分类成功 {funnel['classified_segments']}, 无法归类 {len(unclassified)}")

    # ── 汇总 ──
    total_candidates = funnel["candidate_segments"]
    total_unclassified = len(unclassified)
    gain3_unclassified = sum(1 for u in unclassified if u["gain"] >= 3.0)

    # 无法归类段的高涨幅占比（>=3 倍但被分类层丢弃 → 真实召回流失）
    unclass_high_gain = {
        "count": gain3_unclassified,
        "ratio": round(gain3_unclassified / total_unclassified, 4) if total_unclassified else 0.0,
        "examples": [u for u in unclassified if u["gain"] >= 3.0][:5],
    }

    # D 兜底纯度：同形态 KMeans 聚类，看 D 内部能否分出成败可比的簇
    d_purity = _analyze_d_purity(d_samples, n_clusters=d_n_clusters)

    result = {
        "config": {
            "start_date": start_date,
            "end_date": end_date or "最新",
            "max_stocks": max_stocks,
        },
        "funnel": {
            **funnel,
            "unclassified_segments": total_unclassified,
            "unclassified_ratio": round(total_unclassified / total_candidates, 4) if total_candidates else 0.0,
            "classified_ratio": round(funnel["classified_segments"] / total_candidates, 4) if total_candidates else 0.0,
            "unclassified_high_gain": unclass_high_gain,
        },
        "form_distribution": {f: form_count.get(f, 0) for f in FORMS},
        "samples_by_form": {
            f: {lbl: samples_by_form[f].get(lbl, 0) for lbl in OUTCOME_LABELS}
            for f in FORMS
        },
        "d_purity": d_purity,
        "elapsed": round(time.time() - t0, 2),
    }
    return result


def _analyze_d_purity(d_samples: list[FeatureVector], n_clusters: int = 4) -> dict:
    """对 D 兜底形态样本做同形态聚类，判断内部是否异构（需要细分）。"""
    if len(d_samples) < 4:
        return {"error": f"D 形态样本不足 ({len(d_samples)} < 4)，无法聚类", "d_sample_count": len(d_samples)}

    # 构建特征矩阵 + 元信息（含 label）
    rows = [fv.to_array(FEATURE_NAMES) for fv in d_samples]
    mat = pd.DataFrame(rows, columns=FEATURE_NAMES)
    meta = [{"form_type": fv.form_type, "label": fv.label, "ts_code": fv.ts_code, "t0_idx": fv.t0_idx}
            for fv in d_samples]

    k = max(2, min(n_clusters, len(d_samples) // 2))
    cres = cluster_by_form(mat, meta, n_clusters=k, feature_names=MATCH_FEATURES)

    # 只取 D 形态的簇
    d_clusters = [c for c in cres.clusters if c.form_type == FORM_D]
    if not d_clusters:
        return {"error": "D 形态未产生簇", "d_sample_count": len(d_samples)}

    # 簇内正负是否可比（同簇内同时含 success 与 failure）
    comparable = [c for c in d_clusters if c.comparable]
    ratios = [c.success_ratio for c in d_clusters]
    return {
        "d_sample_count": len(d_samples),
        "n_clusters": len(d_clusters),
        "clusters": [
            {"cluster_id": c.cluster_id, "size": c.size,
             "success": c.success, "failure": c.failure,
             "success_ratio": round(c.success_ratio, 4), "comparable": c.comparable}
            for c in d_clusters
        ],
        "comparable_clusters": len(comparable),
        "comparable_ratio": round(len(comparable) / len(d_clusters), 4),
        # 簇间成功率的离散度：值越大说明 D 内部存在"可区分"的子形态（异构）
        "success_ratio_std": round(float(np.std(ratios)), 4),
        "success_ratio_range": [round(float(min(ratios)), 4), round(float(max(ratios)), 4)],
    }


def _print_report(r: dict) -> None:
    print("=" * 70)
    print("形态覆盖度分析报告")
    print("=" * 70)
    print(f"配置: {r['config']}")
    print(f"耗时: {r.get('elapsed')}s")
    if "error" in r:
        print(f"错误: {r['error']}")
        return

    print("\n── 1. 召回-分类漏斗（分类层召回流失） ──")
    f = r["funnel"]
    print(f"  扫描股票        : {f['scanned_stocks']}")
    print(f"  召回候选段      : {f['candidate_segments']}")
    print(f"  分类成功段      : {f['classified_segments']} ({f['classified_ratio']:.1%})")
    print(f"  无法归类段      : {f['unclassified_segments']} ({f['unclassified_ratio']:.1%})")
    uhg = f["unclassified_high_gain"]
    if uhg["count"]:
        print(f"  ⚠ 无法归类但≥3倍: {uhg['count']} ({uhg['ratio']:.1%}) —— 这些是【漏掉的真实 3 倍段】")
        for e in uhg["examples"]:
            print(f"      {e['ts_code']} {e['low_date']} 涨幅{e['gain']}x 跨度{e['duration']}日")
    else:
        print(f"  无法归类且≥3倍 : 0（分类层未漏掉高涨幅段）")

    print("\n── 2. 分类成功段的形态分布 ──")
    fd = r["form_distribution"]
    tot_fd = sum(fd.values()) or 1
    for frm in FORMS:
        n = fd[frm]
        bar = "#" * int(30 * n / tot_fd)
        print(f"  {frm} {FORM_LABEL[frm]:6s} : {n:5d} ({n / tot_fd:5.1%}) {bar}")

    print("\n── 3. 各形态正负样本分布（分流后） ──")
    hdr = "  形态      " + "".join(f"{lbl:>12s}" for lbl in OUTCOME_LABELS)
    print(hdr)
    for frm in FORMS:
        cnt = r["samples_by_form"][frm]
        row = "".join(f"{cnt[lbl]:>12d}" for lbl in OUTCOME_LABELS)
        print(f"  {frm} {FORM_LABEL[frm]:6s} {row}")

    print("\n── 4. D 兜底形态聚类纯度 ──")
    dp = r["d_purity"]
    if "error" in dp:
        print(f"  {dp['error']}")
    else:
        print(f"  D 形态样本数  : {dp['d_sample_count']}")
        print(f"  聚类数        : {dp['n_clusters']}")
        print(f"  可比簇(正负混合): {dp['comparable_clusters']}/{dp['n_clusters']} ({dp['comparable_ratio']:.1%})")
        print(f"  簇间成功率 std: {dp['success_ratio_std']}")
        print(f"  簇间成功率范围: {dp['success_ratio_range']}")
        print("  各簇明细:")
        for c in dp["clusters"]:
            mark = "✔可比" if c["comparable"] else "✘不可比"
            print(f"    簇{c['cluster_id']}: size={c['size']:4d} "
                  f"success={c['success']:4d} failure={c['failure']:4d} "
                  f"成功率={c['success_ratio']:.2f} {mark}")

    print("\n── 结论速读 ──")
    print("  · 漏斗：看「无法归类段」占比与其中≥3倍段数量，判断是否漏形态")
    print("  · D 纯度：簇间成功率差异大(>0.3)且可比簇多 → D 内部异构，需细分或增类")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="形态覆盖度分析")
    parser.add_argument("--start-date", default="20100101", help="数据起始 YYYYMMDD")
    parser.add_argument("--end-date", default="", help="数据截止 YYYYMMDD（空=最新）")
    parser.add_argument("--max-stocks", type=int, default=None, help="限制扫描股票数（调试/小规模验证）")
    parser.add_argument("--d-clusters", type=int, default=4, help="D 形态聚类数")
    parser.add_argument("--out", default="", help="结果落盘 JSON 路径")
    args = parser.parse_args()

    result = analyze(
        start_date=args.start_date,
        end_date=args.end_date,
        max_stocks=args.max_stocks,
        d_n_clusters=args.d_clusters,
    )
    _print_report(result)

    if args.out:
        out_path = args.out
    else:
        out_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "form_coverage_report.json",
        )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2, default=str)
    print(f"\n报告已落盘: {out_path}")


if __name__ == "__main__":
    main()
