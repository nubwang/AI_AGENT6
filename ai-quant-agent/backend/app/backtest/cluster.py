"""同形态配对聚类（cluster）

对齐规划：plans/04-回测引擎.md 3.5 与第十四章第 7 项
  目的：确保成功/失败样本"可比"。仅按启动形态（A/B/C/D/E）分组还不够——
  同一形态内部起爆前的图形/量能/资金结构差异巨大。因此：
    1. 按 form_type 分组
    2. 组内用"起爆前特征"做 KMeans 聚类（纯 numpy 实现，无 sklearn 依赖）
    3. 每个簇 = 更细的"同构环境"，簇内同时含正负样本时归因才公平

  聚类特征说明：
    - 使用 zscore 归一化后的特征矩阵（NaN 已置 0）
    - 剔除"全样本方差≈0"的列（如资金/财务表未采集时大量为 0，无区分度）
    - 聚类维度偏重"起爆前形态"（价格图形 + 量能 + 技术面）

  KMeans 为自研 Lloyd 迭代：kmeans++ 初始化 + 空簇重初始化，稳定可复现。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logger import logger

DEFAULT_N_CLUSTERS = 5
MIN_CLUSTER_SAMPLES = 4        # 少于该样本数的簇直接合并到最近簇
MAX_ITERS = 60
SEED = 42


@dataclass
class ClusterInfo:
    """单个簇的信息。"""
    cluster_id: int
    form_type: str
    size: int
    success: int
    failure: int
    success_ratio: float        # 簇内成功占比
    comparable: bool            # 是否同时含正负样本（可做簇内归因）
    center: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "form_type": self.form_type,
            "size": self.size,
            "success": self.success,
            "failure": self.failure,
            "success_ratio": round(self.success_ratio, 4),
            "comparable": self.comparable,
        }


@dataclass
class ClusterResult:
    """同形态聚类结果。"""
    cluster_ids: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))
    n_clusters: int = 0
    clusters: list[ClusterInfo] = field(default_factory=list)
    by_form: dict = field(default_factory=dict)     # {form_type: n_clusters}

    def to_dict(self) -> dict:
        return {
            "n_clusters": self.n_clusters,
            "by_form": self.by_form,
            "clusters": [c.to_dict() for c in self.clusters],
        }


def _kmeans(X: np.ndarray, k: int, iters: int = MAX_ITERS, seed: int = SEED) -> tuple[np.ndarray, np.ndarray]:
    """自研 KMeans（kmeans++ 初始化 + Lloyd 迭代）。

    Args:
        X: (N, D) 特征矩阵
        k: 簇数（<= N）
    Returns:
        (labels, centers)：labels 为 (N,) 簇编号，centers 为 (k, D)
    """
    n, d = X.shape
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)

    # kmeans++ 初始化
    centers = np.zeros((k, d))
    first = int(rng.integers(0, n))
    centers[0] = X[first]
    for ci in range(1, k):
        dist = np.min(((X[:, None, :] - centers[None, :ci, :]) ** 2).sum(axis=2), axis=1)
        dist = np.maximum(dist, 1e-12)
        prob = dist / dist.sum()
        idx = int(rng.choice(n, p=prob))
        centers[ci] = X[idx]

    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        # 分配
        dists = ((X[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        new_labels = np.argmin(dists, axis=1)
        # 更新中心
        new_centers = centers.copy()
        for ci in range(k):
            member = new_labels == ci
            if member.sum() > 0:
                new_centers[ci] = X[member].mean(axis=0)
            else:
                # 空簇：从离它最远的点重新初始化
                far_idx = int(np.argmax(dists[:, ci]))
                new_centers[ci] = X[far_idx]
        if np.array_equal(new_labels, labels) and np.allclose(new_centers, centers, atol=1e-12):
            centers, labels = new_centers, new_labels
            break
        centers, labels = new_centers, new_labels

    # 将小簇（样本数 < MIN_CLUSTER_SAMPLES）合并到最近的大簇
    for ci in range(k):
        if int((labels == ci).sum()) < MIN_CLUSTER_SAMPLES and k > 1:
            member = labels == ci
            idxs = np.where(member)[0]
            # 把该簇每个点重新分配给最近的其它簇
            for pi in idxs:
                dists_p = ((X[pi] - centers) ** 2).sum(axis=1)
                dists_p[ci] = np.inf
                labels[pi] = int(np.argmin(dists_p))
    return labels, centers


def _usable_columns(feat_df: pd.DataFrame, names: list[str] | None = None) -> list[str]:
    """筛选可用于聚类的列：剔除全 NaN / 方差≈0 的列（无区分度）。"""
    cols = names or list(feat_df.columns)
    usable = []
    for c in cols:
        if c not in feat_df.columns:
            continue
        vals = feat_df[c].to_numpy(dtype=float)
        if len(vals) == 0:
            continue
        valid = vals[~np.isnan(vals)]
        if len(valid) < 2:
            continue
        if np.nanstd(valid) < 1e-9:
            continue
        usable.append(c)
    return usable


def cluster_by_form(
    feat_df: pd.DataFrame,
    meta: list[dict],
    n_clusters: int = DEFAULT_N_CLUSTERS,
    seed: int = SEED,
    feature_names: list[str] | None = None,
) -> ClusterResult:
    """按 form_type 分组做 KMeans 聚类，确保正负样本同构可比。

    Args:
        feat_df: 归一化特征矩阵（行=样本，列=特征）
        meta: 与 feat_df 对齐的元信息列表（需含 form_type / label）
        n_clusters: 每个形态的目标簇数（实际按样本量自适应）
        seed: 随机种子
        feature_names: 参与聚类的特征名（默认剔除无区分度列后的全部）

    Returns:
        ClusterResult（cluster_ids 与 feat_df 行对齐）
    """
    n = len(feat_df)
    if n == 0 or len(meta) == 0:
        return ClusterResult(cluster_ids=np.array([], dtype=int))

    usable = _usable_columns(feat_df, feature_names)
    if not usable:
        logger.warning("cluster: 无可用于聚类的特征列，退化为全量单簇")
        return ClusterResult(
            cluster_ids=np.zeros(n, dtype=int),
            n_clusters=1,
            clusters=[ClusterInfo(0, "ALL", n, 0, 0, 0.0, False)],
        )

    X_all = feat_df[usable].to_numpy(dtype=float)
    X_all = np.nan_to_num(X_all, nan=0.0)

    # 按 form 分组
    form_ids: dict[str, list[int]] = {}
    for i, m in enumerate(meta):
        form_ids.setdefault(str(m.get("form_type", "?")), []).append(i)

    global_labels = np.full(n, -1, dtype=int)
    clusters: list[ClusterInfo] = []
    by_form: dict[str, int] = {}
    cluster_counter = 0

    for form, idxs in form_ids.items():
        idxs_arr = np.array(idxs, dtype=int)
        sub_n = len(idxs_arr)
        if sub_n < 2:
            # 单样本形态：独立簇（不可比）
            gid = cluster_counter
            cluster_counter += 1
            global_labels[idxs_arr] = gid
            lbl = meta[idxs_arr[0]].get("label", "")
            succ = 1 if lbl == "success" else 0
            clusters.append(ClusterInfo(gid, form, 1, succ, 1 - succ,
                                        float(succ), False))
            by_form[form] = 1
            continue

        k = max(1, min(n_clusters, sub_n))
        X_sub = X_all[idxs_arr]
        labels, centers = _kmeans(X_sub, k, seed=seed)

        # 全局簇编号 + 统计
        for ci in range(k):
            member_mask = labels == ci
            member_local = np.where(member_mask)[0]
            member_global = idxs_arr[member_local]
            gid = cluster_counter
            cluster_counter += 1
            for gi in member_global:
                global_labels[gi] = gid

            succ = sum(1 for gi in member_global if meta[gi].get("label") == "success")
            fail = len(member_global) - succ
            clusters.append(
                ClusterInfo(
                    cluster_id=gid, form_type=form, size=len(member_global),
                    success=succ, failure=fail,
                    success_ratio=succ / len(member_global) if member_global.size else 0.0,
                    comparable=bool(succ > 0 and fail > 0),
                    center=centers[ci].tolist(),
                )
            )
        by_form[form] = k

    return ClusterResult(
        cluster_ids=global_labels,
        n_clusters=len(clusters),
        clusters=clusters,
        by_form=by_form,
    )


def cluster_attribution(
    feat_df: pd.DataFrame,
    meta: list[dict],
    cluster_ids: np.ndarray,
    min_comparable: int = 2,
    feature_names: list[str] | None = None,
) -> dict:
    """同簇内成功/失败归因汇总。

    对每个"同时含正负样本"的簇分别跑归因，再把各簇的关键特征合并：
      - 每个特征在多少个可比簇中被判定为关键（关键频次）
      - 各簇关键特征的平均 AUC（方向归一化）
      - 组合规则：优先取"跨簇稳定"的关键特征

    返回 dict：{cluster_attributions: [...], aggregated: {...}, comparable_clusters: n}
    """
    from app.backtest.attribution import analyze_attribution
    from app.backtest.outcome_tracker import LABEL_SUCCESS

    n = len(feat_df)
    if n == 0 or len(meta) == 0 or len(cluster_ids) != n:
        return {"cluster_attributions": [], "aggregated": {}, "comparable_clusters": 0}

    # 组内按簇分组
    cluster_groups: dict[int, list[int]] = {}
    for i in range(n):
        cluster_groups.setdefault(int(cluster_ids[i]), []).append(i)

    cluster_attributions: list[dict] = []
    comparable_count = 0
    # 特征 → [auc, ...]（跨簇收集关键特征的表现）
    feat_aucs: dict[str, list[float]] = {}

    for cid, idxs in cluster_groups.items():
        if len(idxs) < min_comparable * 2:
            continue
        sub = feat_df.iloc[idxs].copy()
        sub_meta = [meta[i] for i in idxs]
        labels = np.array([1 if m.get("label") == LABEL_SUCCESS else 0 for m in sub_meta], dtype=float)
        n_succ = int(labels.sum())
        n_fail = int(len(labels) - n_succ)
        if n_succ < 2 or n_fail < 2:
            continue

        att = analyze_attribution(sub, labels, feature_names=feature_names)
        if not att.rankings:
            continue
        comparable_count += 1
        for r in att.rankings:
            if r.is_key:
                feat_aucs.setdefault(r.feature, []).append(r.auc)

        cluster_attributions.append(
            {
                "cluster_id": int(cid),
                "size": len(idxs),
                "success": n_succ,
                "failure": n_fail,
                "success_ratio": round(n_succ / len(idxs), 4),
                "top_features": [r.feature for r in att.rankings[:5]],
                "rankings": [r.__dict__ for r in att.rankings[:10]],
            }
        )

    # 汇总：跨簇稳定的关键特征
    aggregated = {"stable_key_features": [], "feature_frequency": {}}
    freq = {f: len(v) for f, v in feat_aucs.items()}
    aggregated["feature_frequency"] = dict(
        sorted(freq.items(), key=lambda kv: (-kv[1], -np.mean(feat_aucs[kv[0]])))
    )
    if comparable_count > 0:
        stable = [
            {
                "feature": f,
                "clusters_hit": cnt,
                "clusters_total": comparable_count,
                "avg_auc": round(float(np.mean(feat_aucs[f])), 4),
            }
            for f, cnt in freq.items()
            if cnt >= max(1, comparable_count // 2)   # 在至少一半可比簇中稳定出现
        ]
        stable.sort(key=lambda x: (-x["clusters_hit"], -x["avg_auc"]))
        aggregated["stable_key_features"] = stable

    return {
        "cluster_attributions": cluster_attributions,
        "aggregated": aggregated,
        "comparable_clusters": comparable_count,
    }
