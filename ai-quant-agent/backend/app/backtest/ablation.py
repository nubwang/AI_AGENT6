"""消融实验（ablation）— plans/23 §4.3 / P1 第 9 项

## 要回答的问题（规划 §六 KPI）

> "**洗盘这一层到底值多少**？" —— 逐项关闭信号（形态/洗盘/资金/事件）→ 输出**边际贡献表**。

## 方法（可复现、不训练模型）

1. 每个信号先转成**百分位秩**（0~1，单调变换，不使用标签 → 无泄漏）；
2. **等权秩集成**作为基线评分：`score = mean(各信号秩)`；
3. 用 **K 折交叉 AUC** 评估集成评分对标签（如 T+5>0）的判别力（避免同数据自评虚高）；
4. **留一法边际贡献**：`marginal_i = AUC(全信号) − AUC(去掉信号 i)`；
5. **置换检验**：把信号 i 打乱重算边际贡献，得到 p 值（默认在子样本上做，控制成本）；
6. 多信号同时检验 → 交给 [`stats_correction.bh_fdr`](ai-quant-agent/backend/app/backtest/stats_correction.py:1) 做 FDR 校正。

## ⚠️ 诚实边界（必须写在前面，否则会被误读）

- **等权秩集成 ≠ 训练好的模型**。它回答的是"这个信号相对其它信号**有没有增量信息**"，
  不是"在最优模型里它有多重要"。要后者得上 logistic/GBDT + 严格样本外。
- 边际贡献为负（去掉某信号 AUC 反而升）是**常见且有意义**的结果：
  说明该信号在等权集成里是**噪声拖累**，不是"有用"。
- ⚠️ **等权集成里"纯噪声"的边际贡献是负的，不是 0**：剔除一列噪声后，
  剩余信号在集成里的权重变大、AUC 反而上升。所以判断"有用/无用"要看
  `marginal` 的相对排序与置换 p 值，不能拿"marginal 是否 > 0"当门槛。
- ⚠️ **样本大 ≠ 有效**：FDR 只回答"能不能排除偶然"，不回答"值不值得用"。
  实测 10.9 万样本下 AUC 0.532（边际 0.0156）也会被判显著。
  所以显著之后还要看**效应量**（`single_auc` 离 0.5 多远、`marginal` 有多大）。
- 单一数据 + 单一样本期 → **只用于相对排序与筛选**，绝对值不可当业绩；
  因此本模块的结论**不直接驱动参数修改**，只用于"该不该继续投入这条线"的判断。
"""
from __future__ import annotations

import numpy as np

from app.backtest.stats_correction import bh_fdr

MAX_PERM_N = 20000      # 置换检验用的最大子样本（控成本）
DEFAULT_PERM = 100      # 置换次数
MIN_CV_PER_FOLD = 20    # 每折最少样本（低于此不假装做了交叉验证）


def _rank01(x: np.ndarray) -> np.ndarray:
    """百分位秩（0~1）。NaN 保持 NaN（不参与集成）。"""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    ok = ~np.isnan(x)
    if ok.sum() == 0:
        return out
    order = x[ok].argsort()
    ranks = np.empty(len(order), dtype=float)
    ranks[order] = np.arange(len(order), dtype=float)
    # 平均秩处理并列（对 AUC 不敏感，但保持一致性）
    out[ok] = ranks / max(1, len(order) - 1)
    return out


def auc(y: np.ndarray, score: np.ndarray) -> float:
    """秩和法 AUC（Mann-Whitney）。样本不足/单类别返回 0.5。"""
    y = np.asarray(y, dtype=float)
    s = np.asarray(score, dtype=float)
    ok = (~np.isnan(y)) & (~np.isnan(s))
    y, s = y[ok], s[ok]
    n_pos, n_neg = int((y > 0).sum()), int((y <= 0).sum())
    if n_pos == 0 or n_neg == 0:
        return 0.5
    r = _rank01(s) * (len(s) - 1) + 1.0          # 1..n 的秩
    r_pos = r[y > 0].sum()
    return float((r_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def ensemble(signals: dict[str, np.ndarray], use: list[str] | None = None) -> np.ndarray:
    """等权秩集成：可用信号的百分位秩均值（某行全 NaN → 该行返回 NaN）。

    不用 `np.nanmean`：全 NaN 行会触发 "Mean of empty slice" 运行时警告，
    这里显式除以**有效个数**（缺失信号的行按剩余信号平均，语义也更清楚）。
    """
    keys = use if use is not None else list(signals)
    keys = [k for k in keys if k in signals]
    if not keys:
        return np.array([])
    mat = np.vstack([_rank01(np.asarray(signals[k], dtype=float)) for k in keys])
    cnt = np.sum(~np.isnan(mat), axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        tot = np.nansum(mat, axis=0)
        return np.where(cnt > 0, tot / np.where(cnt > 0, cnt, 1), np.nan)


def cv_auc(y: np.ndarray, score: np.ndarray, folds: int = 5, seed: int = 42) -> float:
    """K 折交叉 AUC（**按标签分层**分折，折外评估）。

    实测踩到的两个坑（都会让"交叉验证"变成装饰）：
    1. 折太小 → 折外 AUC 的噪声比信号大（30 个点做 5 折，每折仅 6 个点）。
       样本量不足时**直接退化为整体 AUC**，不假装做了交叉验证。
    2. 不分层 → 某些折可能只有单一类别（AUC 按定义返回 0.5），
       再把这些 0.5 平均进去会稀释整体 AUC、甚至掩盖真实方向 → 改为分层分折。
    """
    y = np.asarray(y, dtype=float)
    s = np.asarray(score, dtype=float)
    ok = (~np.isnan(y)) & (~np.isnan(s))
    y, s = y[ok], s[ok]
    n = len(y)
    if n < folds * MIN_CV_PER_FOLD:
        return auc(y, s)
    rng = np.random.default_rng(seed)
    fold_id = np.empty(n, dtype=int)
    for positive in (True, False):                      # 分层：正/负样本各自轮转分折
        idx_lab = np.where((y > 0) == positive)[0]
        idx_lab = rng.permutation(idx_lab)
        fold_id[idx_lab] = (np.arange(len(idx_lab), dtype=int) % folds).astype(int)
    vals = []
    for k in range(folds):
        te = np.where(fold_id == k)[0]
        if len(te) < 5:
            continue
        vals.append(auc(y[te], s[te]))
    return float(np.mean(vals)) if vals else 0.5


def marginal_p(y: np.ndarray, signals: dict[str, np.ndarray], key: str,
               n_perm: int = DEFAULT_PERM, seed: int = 42) -> float | None:
    """信号 key 的边际贡献置换检验 p 值（子样本上做，控成本）。"""
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n == 0:
        return None
    rng = np.random.default_rng(seed)
    sub = rng.choice(n, size=min(MAX_PERM_N, n), replace=False) if n > MAX_PERM_N else np.arange(n)

    def _marg(sig_override: np.ndarray | None = None) -> float:
        sig = {k: np.asarray(v, dtype=float)[sub] for k, v in signals.items()}
        if sig_override is not None:
            sig[key] = sig_override
        full = ensemble(sig)
        drop = ensemble({k: v for k, v in sig.items() if k != key})
        return auc(y[sub], full) - auc(y[sub], drop)

    obs = _marg()
    base = np.asarray(signals[key], dtype=float)[sub]
    hits = 0
    for _ in range(n_perm):
        perm = rng.permutation(base)
        if _marg(perm) >= obs:
            hits += 1
    return round((hits + 1) / (n_perm + 1), 4)


def ablation_table(y, signals: dict[str, np.ndarray], n_perm: int = DEFAULT_PERM,
                   alpha: float = 0.05, cv_folds: int = 5) -> dict:
    """边际贡献表 + FDR 校正。

    Returns:
        {"full_cv_auc", "signals": [{signal, single_auc, marginal, p_value, q_value, reject}],
         "fdr": {...}}
    """
    yv = np.asarray(y, dtype=float)
    sig = {k: np.asarray(v, dtype=float) for k, v in signals.items()}
    full = ensemble(sig)
    full_cv = cv_auc(yv, full, folds=cv_folds)
    rows = []
    for k in sig:
        drop = ensemble({kk: vv for kk, vv in sig.items() if kk != k})
        marginal = full_cv - cv_auc(yv, drop, folds=cv_folds)
        rows.append({
            "signal": k,
            "single_auc": round(auc(yv, sig[k]), 4),
            "marginal": round(marginal, 4),
            "p_value": marginal_p(yv, sig, k, n_perm=n_perm),
        })
    fdr = bh_fdr([r["p_value"] for r in rows], alpha)
    qmap = {fdr["idx"][i]: fdr["q"][i] for i in range(len(fdr["idx"]))} if fdr["m"] else {}
    rmap = {fdr["idx"][i]: fdr["reject"][i] for i in range(len(fdr["idx"]))} if fdr["m"] else {}
    for i, r in enumerate(rows):
        r["q_value"] = qmap.get(i)
        r["reject"] = rmap.get(i, False)
    rows.sort(key=lambda r: -(r["marginal"] or 0))
    return {"full_cv_auc": round(full_cv, 4), "signals": rows, "fdr": fdr}


__all__ = ["auc", "ensemble", "cv_auc", "marginal_p", "ablation_table"]
