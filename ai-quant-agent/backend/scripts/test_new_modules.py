"""新增回测模块验证脚本（合成数据，不依赖数据库）

覆盖：cluster（同形态 KMeans + 簇内归因）、pattern_miner/failure_miner、
      entry_analyzer（最佳入场点）。

运行：cd ai-quant-agent/backend && python scripts/test_new_modules.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from app.backtest.feature_extractor import FeatureVector
from app.backtest.outcome_tracker import (
    OutcomeSample, LABEL_SUCCESS, LABEL_FAILURE_A, LABEL_FAILURE_B, LABEL_DISCARD,
)
from app.backtest.cluster import cluster_by_form, cluster_attribution, _kmeans
from app.backtest.pattern_miner import mine_positive_patterns
from app.backtest.failure_miner import mine_failure_patterns, split_by_type
from app.backtest.entry_analyzer import analyze_entry, aggregate_entry_results


def _mk_df(n=160, seed=3):
    """构造合成 K 线 df（prepare_stock_data 输出结构）。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2022-01-03", periods=n, freq="B")
    base = 10.0 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    close = base.copy()
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    open_ = np.concatenate([[close[0]], close[:-1] * (1 + rng.normal(0, 0.005, n - 1))])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.01, n)))
    amount = vol * close
    df = pd.DataFrame({
        "trade_date": dates,
        "open": open_, "high": high, "low": low, "close": close,
        "pre_close": np.concatenate([[close[0]], close[:-1]]),
        "pct_chg": np.concatenate([[0], np.diff(close) / close[:-1] * 100]),
        "vol": vol, "amount": amount,
        "adj_close": close,
        "adj_factor": np.ones(n),
        "is_sealed_up": np.zeros(n, dtype=bool),
        "is_sealed_down": np.zeros(n, dtype=bool),
    })
    return df


def _mk_sample(df, t0, label, form="A"):
    return OutcomeSample(
        ts_code="TEST.SZ", form_type=form, label=label,
        t0_idx=t0, t0_date=df["trade_date"].iloc[t0],
        start_price=float(df["adj_close"].iloc[t0]),
        peak_price=float(df["adj_close"].iloc[t0 + 30]),
        peak_gain=0.5, peak_idx=t0 + 30, peak_date=df["trade_date"].iloc[t0 + 30],
        max_drawdown=-0.1,
        outcome_t1=0.01, outcome_t5=0.06, outcome_t20=0.15, outcome_t90=0.5,
    )


def test_kmeans():
    """KMeans 应把两个明显分离的簇正确分开。"""
    rng = np.random.default_rng(1)
    c0 = rng.normal(0, 0.3, (50, 3))
    c1 = rng.normal(10, 0.3, (50, 3))
    X = np.vstack([c0, c1])
    labels, centers = _kmeans(X, 2, seed=1)
    assert len(labels) == 100
    # 两个簇中心应远大于簇内散布
    d = float(np.linalg.norm(centers[0] - centers[1]))
    assert d > 5.0, f"簇中心距离过小: {d}"
    # 每簇应恰好对应一个合成簇
    for ci in range(2):
        n0 = int((labels == ci).sum())
        assert 40 <= n0 <= 60, f"簇 {ci} 规模异常: {n0}"
    print(f"[PASS] _kmeans: 2 簇正确分离，中心距离 {d:.2f}")


def test_cluster_by_form():
    """同 form 内：按"形态特征"聚成 2 簇（每簇正负混合），"判别特征"可被簇内归因识别。"""
    rng = np.random.default_rng(2)
    n = 120
    # 形态簇（价格图形）：簇0 平缓 / 簇1 陡峭 —— 只影响形态，不直接决定结局
    form_cluster = np.array([0] * (n // 2) + [1] * (n - n // 2), dtype=int)
    pct_1 = np.where(form_cluster == 0, rng.normal(0, 0.1, n), rng.normal(0.5, 0.1, n))
    pct_2 = np.where(form_cluster == 0, rng.normal(0, 0.1, n), rng.normal(0.5, 0.1, n))
    pct_3 = np.where(form_cluster == 0, rng.normal(0, 0.1, n), rng.normal(0.5, 0.1, n))
    # 判别特征：net_amount_ratio > 0 → success（与形态簇独立，保证簇内正负混合）
    net = rng.normal(0, 1, n)
    label_success = net > 0
    volume = rng.normal(1, 0.2, n)  # 与标签无关

    feat_rows = []
    meta = []
    for i in range(n):
        label = LABEL_SUCCESS if label_success[i] else LABEL_FAILURE_A
        feat_rows.append(FeatureVector("X.SZ", "A", label, i, features={
            "volume_ratio": float(volume[i]),
            "net_amount_ratio": float(net[i]),
            "pct_1": float(pct_1[i]),
            "pct_2": float(pct_2[i]),
            "pct_3": float(pct_3[i]),
        }))
        meta.append({"form_type": "A", "label": label})

    cols = ["volume_ratio", "net_amount_ratio", "pct_1", "pct_2", "pct_3"]
    feat_df = pd.DataFrame([f.to_array(cols) for f in feat_rows], columns=cols)

    # 聚类只用"形态特征"（避免把标签/判别特征混入聚类导致泄漏）
    cres = cluster_by_form(feat_df, meta, n_clusters=2, seed=2,
                           feature_names=["pct_1", "pct_2", "pct_3", "volume_ratio"])
    assert cres.n_clusters >= 2, "应至少分出 2 簇"
    # 应存在正负同簇的可比簇，且簇内 success_ratio 接近 0.5（正负混合）
    comparables = [c for c in cres.clusters if c.comparable]
    assert len(comparables) >= 1, f"无可比簇: {[c.to_dict() for c in cres.clusters]}"
    for c in comparables:
        assert 0.3 < c.success_ratio < 0.7, f"簇 {c.cluster_id} 应为正负混合: {c.to_dict()}"
    print(f"[PASS] cluster_by_form: {cres.n_clusters} 簇, 可比簇 {len(comparables)}, "
          f"success_ratio {[round(c.success_ratio,2) for c in cres.clusters]}")

    # 簇内归因：应识别 net_amount_ratio 为稳定关键特征（判别特征）
    catt = cluster_attribution(feat_df, meta, cres.cluster_ids)
    assert catt["comparable_clusters"] >= 1
    stable = {s["feature"] for s in catt["aggregated"].get("stable_key_features", [])}
    assert "net_amount_ratio" in stable, f"未识别稳定判别特征: {stable}"
    print(f"[PASS] cluster_attribution: 可比簇 {catt['comparable_clusters']}, 稳定关键特征 {sorted(stable)}")


def test_miners():
    """pattern_miner/failure_miner 应按标签精确筛选。"""
    df = _mk_df()
    t0 = 60
    samples = [
        _mk_sample(df, t0, LABEL_SUCCESS),
        _mk_sample(df, t0 + 5, LABEL_SUCCESS),
        _mk_sample(df, t0 + 10, LABEL_FAILURE_A),
        _mk_sample(df, t0 + 15, LABEL_FAILURE_B),
        _mk_sample(df, t0 + 20, LABEL_DISCARD),
        _mk_sample(df, t0 + 25, LABEL_SUCCESS),
    ]
    pos = mine_positive_patterns(df, samples)
    neg = mine_failure_patterns(df, samples)
    assert len(pos) == 3 and all(f.label == LABEL_SUCCESS for f in pos), f"正样本筛选错误: {len(pos)}"
    assert len(neg) == 2 and all(f.label in (LABEL_FAILURE_A, LABEL_FAILURE_B) for f in neg), f"负样本筛选错误: {len(neg)}"
    groups = split_by_type(neg)
    assert len(groups.get(LABEL_FAILURE_A, [])) == 1 and len(groups.get(LABEL_FAILURE_B, [])) == 1
    # 内部键应携带 outcome
    assert "_outcome_t5" in pos[0].features and "_outcome_t20" in pos[0].features
    print(f"[PASS] pattern_miner: {len(pos)} 正 / {len(neg)} 负（含 failure_A/B 各 {len(groups.get(LABEL_FAILURE_A,[]))}/{len(groups.get(LABEL_FAILURE_B,[]))}）")


def test_entry_analyzer():
    """入场策略：回踩低吸应优于追高；封板日不可交易。"""
    rng = np.random.default_rng(5)
    df = _mk_df(n=160, seed=5)
    # 构造启动点 t0=60：T0 后先回踩启动价附近再大涨
    start = float(df["adj_close"].iloc[60])
    close = df["adj_close"].to_numpy()
    close[61] = start * 1.06          # T+1 冲高 6%（超出回踩容差 3%，不触发回踩）
    close[62] = start * 1.00          # T+2 回踩到启动价
    close[63] = start * 1.01
    close[64] = start * 1.30          # 之后大涨
    close[65] = start * 1.60
    close[66] = start * 2.00
    for i in range(67, 85):
        close[i] = start * 2.00 + (i - 66) * 0.05 * start
    df.loc[61:, "close"] = close[61:]
    # 重算 open：T+1 open 高于 T0 close（追高入场），后续 open 用前收盘
    opens = close.copy()
    opens[61:] = close[60:-1]
    opens[61] = start * 1.06
    df["open"] = opens
    df["high"] = np.maximum(df["open"], df["close"]) * 1.005
    df["low"] = np.minimum(df["open"], df["close"]) * 0.995

    sample = _mk_sample(df, 60, LABEL_SUCCESS)
    res = analyze_entry(df, sample)
    assert res is not None
    st = res.strategies

    # t1_open 入场价应 ≈ start*1.06（追高）
    assert abs(st["t1_open"]["entry_price"] / start - 1.06) < 0.02, st["t1_open"]
    # pullback_t0 应在 T+2（回踩日）入场，entry_gain ≈ 0
    assert abs(st["pullback_t0"]["entry_gain"]) < 0.05, f"pullback 未回踩到启动价: {st['pullback_t0']}"
    # pullback 的 T+20 收益应显著为正（从启动价起算，后面涨到 2 倍）
    assert st["pullback_t0"]["ret_t20"] is not None and st["pullback_t0"]["ret_t20"] > 0.5, st["pullback_t0"]
    # 追高 t1_open 的 entry_gain 应 > pullback（追得更高）
    assert st["t1_open"]["entry_gain"] > st["pullback_t0"]["entry_gain"]

    # 封板日不可交易：把 T+1 设为涨停封死
    df["is_sealed_up"].iloc[61] = True
    res2 = analyze_entry(df, sample)
    assert res2 is not None
    assert res2.strategies["t1_open"]["tradeable"] is False, "T+1 封板应不可交易"
    assert res2.strategies["pullback_t0"]["tradeable"] is True
    print("[PASS] entry_analyzer: 回踩低吸优于追高, 封板日正确标记不可交易")

    # 汇总
    agg = aggregate_entry_results([res2])
    assert agg["total_analyzed"] == 1
    assert "success" in agg["by_label"] and "t1_open" in agg["by_label"]["success"]
    print(f"[PASS] aggregate_entry_results: 成功组最佳入场 = {agg['best_by_label']}")


if __name__ == "__main__":
    print("=== 新增模块合成数据验证 ===")
    test_kmeans()
    test_cluster_by_form()
    test_miners()
    test_entry_analyzer()
    print("\n全部通过 ✅")
