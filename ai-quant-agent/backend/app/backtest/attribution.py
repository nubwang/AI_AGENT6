"""成功/失败归因分析（attribution）

对齐规划：plans/04-回测引擎.md 4.5
  - 在同形态簇内对比成功与失败样本，评估各特征判别力
  - 连续特征用 AUC / 均值差异；离散特征用信息增益
  - 输出：特征判别力排行、成功画像、失败画像、组合规则
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logger import logger

MIN_CLUSTER_SAMPLES = 10

# ── 关键特征准入标准（plans/23 §2.4 T6 修复）──────────────────────────────
# 修复前：is_key 只要求"归一化 AUC ≥ 0.6"，不看方差/样本量/显著性 →
# 实测把 st_risk_flag（两组均值都为 0、info_gain=0）、mainbz_concentration
# （两组均值都为 1.0）这类常数列/零方差列判为"关键特征"，而这份结果会被
# load_attribution_kb() 当作"历史成功规律"喂给 LLM 精筛 → 等于给 LLM 灌噪音。
# 修复后：必须同时满足 ①有效样本足够 ②方差非零 ③两组均值确有差异
# ④AUC ≥ 0.55 ⑤置换检验 p < 0.05（小样本下 AUC 虚高的主要来源）。
MIN_VALID_SAMPLES = 20      # 特征有效样本数下限
MIN_FEATURE_STD = 1e-9      # 方差下限（低于视为常数列）
KEY_AUC = 0.55              # 关键特征 AUC 下限（0.5 = 随机）
PERM_ALPHA = 0.05           # 置换检验显著性水平
N_PERM = 200                # 置换次数
MAX_KEY_FEATURES = 12       # 关键特征数量上限（防止"满屏都是关键特征"）

# 归因知识固化路径（回测步骤 7 产出 → Agent 精筛加载复用同一份）
ATTRIBUTION_KB_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "attribution_kb.json",
)


@dataclass
class FeatureImportance:
    """单个特征的判别力。"""
    feature: str
    auc: float            # ROC AUC（连续特征）
    info_gain: float      # 信息增益（离散特征）
    success_mean: float   # 成功样本该特征均值
    failure_mean: float   # 失败样本该特征均值
    is_key: bool          # 是否关键特征（须通过 T6 准入标准）
    p_value: float | None = None   # 置换检验 p 值（T6）
    valid_samples: int = 0         # 有效样本数（T6）
    discard_reason: str = ""       # 被剔除原因（空=保留）


@dataclass
class AttributionResult:
    """归因分析结果。"""
    rankings: list[FeatureImportance] = field(default_factory=list)
    success_profile: dict = field(default_factory=dict)
    failure_profile: dict = field(default_factory=dict)
    combo_rules: list[dict] = field(default_factory=list)
    cluster_stats: dict = field(default_factory=dict)
    discarded: list = field(default_factory=list)     # T6：被剔除特征 + 原因（审计用）
    filter_stats: dict = field(default_factory=dict)  # T6：过滤统计

    def to_dict(self) -> dict:
        return {
            "rankings": [r.__dict__ for r in self.rankings],
            "success_profile": self.success_profile,
            "failure_profile": self.failure_profile,
            "combo_rules": self.combo_rules,
            "cluster_stats": self.cluster_stats,
            "discarded": self.discarded,
            "filter_stats": self.filter_stats,
        }


def _perm_p_value(
    success_vals: np.ndarray,
    failure_vals: np.ndarray,
    n_perm: int = N_PERM,
    seed: int = 20260918,
) -> float | None:
    """置换检验 p 值（plans/23 T6）。

    随机打乱标签 n_perm 次，统计 |AUC-0.5| 不低于观测值的比例。
    用途：小样本下 AUC 很容易因偶然波动超过 0.6，仅凭 AUC 阈值会把噪音判成关键特征。
    """
    s = success_vals[~np.isnan(success_vals)]
    f = failure_vals[~np.isnan(failure_vals)]
    if len(s) < 5 or len(f) < 5:
        return None
    obs = abs(_auc(s, f) - 0.5)
    pool = np.concatenate([s, f])
    n1 = len(s)
    rng = np.random.default_rng(seed)
    ge = 0
    for _ in range(n_perm):
        rng.shuffle(pool)
        if abs(_auc(pool[:n1], pool[n1:]) - 0.5) >= obs - 1e-12:
            ge += 1
    return (ge + 1) / (n_perm + 1)


def _auc(success_vals: np.ndarray, failure_vals: np.ndarray) -> float:
    """用秩和统计近似 AUC（Mann-Whitney U）。"""
    if len(success_vals) == 0 or len(failure_vals) == 0:
        return 0.5
    s = success_vals[~np.isnan(success_vals)]
    f = failure_vals[~np.isnan(failure_vals)]
    if len(s) == 0 or len(f) == 0:
        return 0.5
    # 拼接排序，计算 rank
    all_vals = np.concatenate([s, f])
    order = np.argsort(np.argsort(all_vals))
    ranks = order + 1
    n1, n2 = len(s), len(f)
    r1 = ranks[:n1].sum()
    auc_val = (r1 - n1 * (n1 + 1) / 2) / (n1 * n2)
    return float(auc_val)


def _info_gain(labels: np.ndarray, values: np.ndarray) -> float:
    """连续特征按中位数二分后计算信息增益。"""
    valid = ~np.isnan(values)
    if valid.sum() < 2:
        return 0.0
    labels = labels[valid]
    values = values[valid]
    thr = np.median(values)
    groups = values >= thr
    n = len(labels)

    def _entropy(y: np.ndarray) -> float:
        if len(y) == 0:
            return 0.0
        p = y.mean()
        if p in (0.0, 1.0):
            return 0.0
        return -(p * np.log2(p) + (1 - p) * np.log2(1 - p))

    h_total = _entropy(labels)
    h_split = (groups.sum() / n) * _entropy(labels[groups]) + ((~groups).sum() / n) * _entropy(labels[~groups])
    return float(h_total - h_split)


def analyze_attribution(
    features: pd.DataFrame,
    labels: np.ndarray,
    feature_names: list[str] | None = None,
    key_auc: float | None = None,
) -> AttributionResult:
    """对一批样本做成功/失败归因分析。

    Args:
        features: 特征 DataFrame（行=样本）
        labels: 0/1 标签（1=成功 success，0=失败 failure）
        feature_names: 参与分析的特征名（默认全部）
        key_auc: AUC 阈值，>= 该值视为关键特征

    Returns:
        AttributionResult
    """
    # 关键特征 AUC 门槛可进化（plans/23 §十二）：未显式传参时读 evolution_config
    if key_auc is None:
        key_auc = KEY_AUC
        try:
            from app.agents import evolution_config
            key_auc = float(evolution_config.get_num("bt_key_auc", KEY_AUC))
        except Exception:  # noqa: BLE001
            pass

    result = AttributionResult()
    if features is None or features.empty or len(labels) == 0:
        return result

    names = feature_names or list(features.columns)
    succ_mask = labels == 1
    fail_mask = labels == 0
    n_succ, n_fail = int(succ_mask.sum()), int(fail_mask.sum())

    if n_succ < 2 or n_fail < 2:
        logger.warning("attribution: 成功或失败样本不足")
        return result

    rankings: list[FeatureImportance] = []
    discarded: list[dict] = []
    for col in names:
        if col not in features.columns:
            continue
        vals = features[col].to_numpy(dtype=float)
        n_valid = int(np.sum(~np.isnan(vals)))
        # T6 准入①：有效样本不足
        if n_valid < MIN_VALID_SAMPLES:
            discarded.append({"feature": col,
                              "reason": f"有效样本不足（{n_valid} < {MIN_VALID_SAMPLES}）"})
            continue
        # T6 准入②：常数列/零方差列（实测 st_risk_flag、mainbz_concentration 就是被它误判的）
        if float(np.nanstd(vals)) < MIN_FEATURE_STD:
            discarded.append({"feature": col, "reason": "方差≈0（常数列，无判别力）"})
            continue
        s_vals = vals[succ_mask]
        f_vals = vals[fail_mask]
        s_mean = float(np.nanmean(s_vals)) if len(s_vals) else 0.0
        f_mean = float(np.nanmean(f_vals)) if len(f_vals) else 0.0
        # T6 准入③：两组均值无差异
        if abs(s_mean - f_mean) < 1e-9:
            discarded.append({"feature": col, "reason": "成功/失败两组均值相同"})
            continue
        a = _auc(s_vals, f_vals)
        # 归一化 AUC 到 [0.5, 1] 方向（>0.5 表示成功样本偏高）
        auc_dir = a if a >= 0.5 else 1 - a
        ig = _info_gain(labels, vals)
        # T6 准入④⑤：AUC 门槛 + 置换检验显著（小样本 AUC 虚高是主要污染源）
        p = _perm_p_value(s_vals, f_vals)
        is_key = bool(auc_dir >= key_auc and p is not None and p < PERM_ALPHA)
        if not is_key:
            reason = (f"AUC {auc_dir:.3f} < {key_auc}" if auc_dir < key_auc
                      else f"置换检验不显著（p={p if p is None else round(p, 4)} ≥ {PERM_ALPHA}）")
            discarded.append({"feature": col, "reason": reason})
        rankings.append(
            FeatureImportance(
                feature=col, auc=round(auc_dir, 4), info_gain=round(ig, 4),
                success_mean=s_mean, failure_mean=f_mean,
                is_key=is_key,
                p_value=(round(p, 4) if p is not None else None),
                valid_samples=n_valid,
            )
        )

    # 按判别力排序：AUC 为主，信息增益为辅
    rankings.sort(key=lambda r: (-r.auc, -r.info_gain))
    result.rankings = rankings

    # T6：关键特征数量上限（只保留 AUC 最高的 MAX_KEY_FEATURES 个为"关键"）
    key_list = [r for r in rankings if r.is_key]
    key_capped = 0
    if len(key_list) > MAX_KEY_FEATURES:
        for r in key_list[MAX_KEY_FEATURES:]:
            r.is_key = False
            r.discard_reason = f"超出关键特征上限（仅保留 AUC 最高 {MAX_KEY_FEATURES} 个）"
            key_capped += 1
    result.discarded = discarded
    result.filter_stats = {
        "total_features": len(names),
        "kept": len(rankings),
        "rejected": len(discarded),
        "key_features": sum(1 for r in rankings if r.is_key),
        "key_capped": key_capped,
        "criteria": {
            "min_valid_samples": MIN_VALID_SAMPLES,
            "key_auc": key_auc,
            "perm_alpha": PERM_ALPHA,
            "n_perm": N_PERM,
            "max_key_features": MAX_KEY_FEATURES,
        },
    }
    if discarded:
        logger.info(
            f"归因特征过滤（T6）：{len(names)} 个特征 → 保留 {len(rankings)}，"
            f"剔除 {len(discarded)}，关键特征 {result.filter_stats['key_features']} 个"
        )

    # 画像：关键特征中成功/失败样本显著不同的方向
    success_profile: dict = {}
    failure_profile: dict = {}
    for r in rankings[:10]:
        if r.is_key:
            if r.success_mean > r.failure_mean:
                success_profile[r.feature] = round(r.success_mean, 4)
                failure_profile[r.feature] = round(r.failure_mean, 4)
            else:
                failure_profile[r.feature] = round(r.failure_mean, 4)
                success_profile[r.feature] = round(r.success_mean, 4)
    result.success_profile = success_profile
    result.failure_profile = failure_profile

    # 组合规则：判别力 Top 特征的"高分组"组合
    key_feats = [r.feature for r in rankings if r.is_key][:5]
    combo_rules: list[dict] = []
    if len(key_feats) >= 2:
        for i in range(len(key_feats)):
            for j in range(i + 1, len(key_feats)):
                f1, f2 = key_feats[i], key_feats[j]
                v1 = features[f1].to_numpy(dtype=float)
                v2 = features[f2].to_numpy(dtype=float)
                m = (~np.isnan(v1)) & (~np.isnan(v2))
                if m.sum() < MIN_CLUSTER_SAMPLES:
                    continue
                thr1 = np.nanmedian(v1)
                thr2 = np.nanmedian(v2)
                combo = m & (v1 >= thr1) & (v2 >= thr2)
                if combo.sum() >= MIN_CLUSTER_SAMPLES:
                    combo_rules.append(
                        {
                            "features": [f1, f2],
                            "sample_count": int(combo.sum()),
                            "up_prob": round(float(labels[combo].mean()), 4),
                            "baseline": round(float(labels.mean()), 4),
                        }
                    )
    combo_rules.sort(key=lambda c: -c["up_prob"])
    result.combo_rules = combo_rules

    result.cluster_stats = {"success": n_succ, "failure": n_fail}
    return result


def save_attribution_kb(result: AttributionResult, path: str = ATTRIBUTION_KB_FILE) -> bool:
    """把归因结果固化为 Agent 可加载的知识库（关键特征 + 成功画像 + 组合规则）。

    落盘内容（供 Agent 精筛参考"历史成功规律"）：
      - key_features: rankings 中 is_key 的关键特征（AUC 高），含判别方向与成功/失败均值
      - success_profile / combo_rules / cluster_stats: 成功画像与组合规律
    失败返回 False（不影响回测主流程）。
    """
    try:
        key_features = [
            {
                "feature": r.feature,
                "auc": r.auc,
                "info_gain": r.info_gain,
                "direction": "high" if r.success_mean >= r.failure_mean else "low",
                "success_mean": r.success_mean,
                "failure_mean": r.failure_mean,
            }
            for r in result.rankings
            if r.is_key
        ]
        kb = {
            "version": 1,
            "key_features": key_features,
            "success_profile": result.success_profile,
            "combo_rules": result.combo_rules,
            "cluster_stats": result.cluster_stats,
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
        logger.info(f"归因知识已固化: {path}（关键特征 {len(key_features)} 个）")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"保存归因知识失败: {exc}")
        return False


def load_attribution_kb(path: str = ATTRIBUTION_KB_FILE) -> dict | None:
    """加载已固化的归因知识库。文件不存在或损坏返回 None（Agent 精筛降级为纯语义判断）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None
