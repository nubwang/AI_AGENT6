"""多重检验校正（stats_correction）— plans/23 §4.3 / P1 第 9 项

## 为什么必须有它（规划原文的风险）

> "挖 1000 条规则总有几十条'显著'" —— 不校正就会把噪声当规律，
> 而且这些规则会进入条件概率表、进每日推荐打分、进而驱动进化大脑调参。

本模块提供 **Benjamini-Hochberg FDR**（控制错误发现率）、**Bonferroni**（控制族错误率）、
**Newey-West HAC t 值**（修正**重叠样本**导致的 t 膨胀，plans/24 §11.27）、
**HAC 回归系数 t 值**（`newey_west_reg_t`，用于"跨日两组比较"这类非均值检验，§11.28 A3），
以及把校正应用到"规则/形态排行榜"这类真实产物上的工具。

> 关于 NW 的来历：plans/24 §11.27 实测发现，「T+1 开盘买入 + 持 20 日」的逐日收益序列
> 相邻样本共享 19 天，naive t = 3.08 而 HAC t = 0.94 —— **差了一个数量级**。
> 从那以后，本层任何"整体显著性"结论都必须**同时报这一对**。

## 为什么默认用 FDR 而不是 Bonferroni

回测里候选规则成百上千，Bonferroni 过于保守（`α/m` 会把真实有效的规则也全部砍掉，
实测 §4.3 场景下几乎留不下任何规则）。FDR 控制"被判显著的那些里有多少是假的"，
更适合"筛出值得回测验证的候选集"这一用途。两者都提供，默认 FDR。

## 实现要点（易错处）

- BH 的 q 值必须**从大到小取累计最小值**（step-up）：`q_i = min_{j>=i} (p_j * m / j)`；
  直接算 `p*m/i` 会低估尾部（这一步写错会让 FDR 形同虚设）。
- 单调性：q 值必须随 p 单调不减；实现里用 cumulative minimum 保证。
- 边界：空输入、全 NaN 返回空结果，不抛异常。
- **重叠样本**（§11.27）：持有期 h 的逐日滚动收益 ⇒ 相邻样本相关 ⇒
  普通 t 的独立性假设不成立；统一用 `newey_west_t(x, lag=h-1)` 复核。
"""
from __future__ import annotations

import math

import numpy as np

DEFAULT_ALPHA = 0.05


def bh_fdr(pvalues: list, alpha: float = DEFAULT_ALPHA) -> dict:
    """Benjamini-Hochberg FDR 校正。

    Args:
        pvalues: 原始 p 值列表（允许含 None/NaN，会被剔除且不参与 m 的计算）
        alpha: 目标 FDR（默认 0.05）

    Returns:
        {
          "m": 有效检验数, "alpha": alpha,
          "idx": 原下标（仅有效项）, "p": 原 p 值, "q": 校正后 q 值,
          "reject": [bool], "n_reject": 拒绝原假设（显著）的个数,
          "threshold": 使用的 p 阈值（最大被拒 p）, "method": "BH",
        }
    """
    vals: list[tuple[int, float]] = []
    for i, p in enumerate(pvalues):
        try:
            v = float(p)
        except (TypeError, ValueError):
            continue
        if v == v and 0.0 <= v <= 1.0:          # 过滤 None/NaN/越界
            vals.append((i, v))
    if not vals:
        return {"m": 0, "alpha": alpha, "idx": [], "p": [], "q": [], "reject": [],
                "n_reject": 0, "threshold": None, "method": "BH"}
    vals.sort(key=lambda t: t[1])               # 按 p 升序
    m = len(vals)
    ps = np.array([v for _, v in vals], dtype=float)
    # q_i = min_{j>=i} (p_j * m / j)，j 从 1 开始计
    j = np.arange(1, m + 1, dtype=float)
    raw = ps * m / j
    q = np.minimum.accumulate(raw[::-1])[::-1]  # step-up：从大到小累计最小
    q = np.clip(q, 0.0, 1.0)
    reject = q <= alpha
    thr = float(ps[reject].max()) if reject.any() else None
    return {
        "m": m, "alpha": alpha,
        "idx": [i for i, _ in vals], "p": [round(float(v), 6) for _, v in vals],
        "q": [round(float(v), 6) for v in q],
        "reject": [bool(x) for x in reject],
        "n_reject": int(reject.sum()), "threshold": None if thr is None else round(thr, 6),
        "method": "BH",
    }


def bonferroni(pvalues: list, alpha: float = DEFAULT_ALPHA) -> dict:
    """Bonferroni 校正（族错误率）：p*m ≤ α 才拒绝。"""
    vals: list[tuple[int, float]] = []
    for i, p in enumerate(pvalues):
        try:
            v = float(p)
        except (TypeError, ValueError):
            continue
        if v == v and 0.0 <= v <= 1.0:
            vals.append((i, v))
    if not vals:
        return {"m": 0, "alpha": alpha, "idx": [], "p": [], "q": [], "reject": [],
                "n_reject": 0, "threshold": None, "method": "bonferroni"}
    m = len(vals)
    idx = [i for i, _ in vals]
    ps = [v for _, v in vals]
    q = [min(1.0, v * m) for v in ps]
    reject = [v * m <= alpha for v in ps]
    thr = max([v for v in ps if v * m <= alpha], default=None)
    return {"m": m, "alpha": alpha, "idx": idx,
            "p": [round(v, 6) for v in ps], "q": [round(v, 6) for v in q],
            "reject": reject, "n_reject": sum(reject),
            "threshold": None if thr is None else round(thr, 6), "method": "bonferroni"}


def proportion_p(x1: int, n1: int, x2: int, n2: int) -> float | None:
    """两组比例的 z 检验 p 值（双侧）。样本不足返回 None。

    用于"某形态/规则的命中率是否显著高于基准"这类规则的显著性检验
    （是本模块与规则挖掘之间的接口）。
    """
    try:
        if min(n1, n2) <= 0:
            return None
        p1, p2 = x1 / n1, x2 / n2
        p = (x1 + x2) / (n1 + n2)
        if p <= 0 or p >= 1:
            return None
        se = float(np.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)))
        if se <= 0:
            return None
        z = abs(p1 - p2) / se
        # 双侧正态尾概率：用 erf 计算，避免依赖 scipy
        from math import erf, sqrt
        return round(float(2 * (1 - 0.5 * (1 + erf(z / sqrt(2))))), 8)
    except Exception:  # noqa: BLE001
        return None


def _clean(x) -> np.ndarray:
    """转成有限值的一维 float64 数组（容忍 list / ndarray / Series / 含 NaN）。"""
    try:
        arr = np.asarray(x, dtype="float64").ravel()
    except Exception:  # noqa: BLE001
        return np.empty(0, dtype="float64")
    return arr[np.isfinite(arr)]


def plain_t(x) -> float:
    """普通（naive）t 值：`mean / (sd/sqrt(n))`。样本不足 30 或方差 ≤ 0 返回 nan。

    保留这个函数的目的只有一个：**和 `newey_west_t` 成对出现**，
    让"t 被重叠放大了多少倍"变成可看见的数字（§11.27）。
    """
    v = _clean(x)
    n = int(v.size)
    if n < 30:
        return float("nan")
    sd = float(v.std(ddof=1))
    if sd <= 0:
        return float("nan")
    return float(v.mean() / (sd / np.sqrt(n)))


def newey_west_t(x, lag: int = 0) -> float:
    """Newey-West（Bartlett 核）HAC t 值 —— 修正**重叠样本**导致的 t 膨胀。

    公式：`se² = (γ0 + 2·Σ_{l=1..L} w_l·γ_l) / T`，Bartlett 权重 `w_l = 1 − l/(L+1)`，
    其中 `γ_l = (1/T)·Σ e_t·e_{t−l}`（`e = x − mean(x)`）。

    - `lag = 0` 时**退化为普通 t 值**（`se² = γ0/T`，注意这里用 T 而非 T−1，
      与 `plain_t` 的 ddof=1 有极小差异，样本量大时可忽略）；
    - 调用方应取 `lag = 持有期 − 1`（重叠长度）。

    Args:
        x: 收益序列（list / ndarray / Series；允许含 NaN，会被剔除）
        lag: 滞后阶数

    Returns:
        t 值；样本不足（< 30）或 HAC 方差 ≤ 0 时返回 nan（不抛异常）。
    """
    v = _clean(x)
    n = int(v.size)
    if n < 30:
        return float("nan")
    mu = float(v.mean())
    e = v - mu
    s = float(e @ e) / n
    L = max(0, int(lag))
    for l in range(1, min(L, n - 1) + 1):
        gl = float(e[l:] @ e[:-l]) / n
        s += 2.0 * (1.0 - l / (L + 1.0)) * gl
    if s <= 0:
        return float("nan")
    return float(mu / np.sqrt(s / n))


def t_compare(x, lag: int) -> dict:
    """同一序列的 **naive t 与 HAC t** 对照（plans/24 §11.27 起，本层统一口径）。

    Returns:
        {"n", "mean", "lag", "t_naive", "t_hac", "shrink"}
        `shrink` = `t_hac / t_naive`（naive 被放大的倍数；naive ≤ 0 或缺失时给 nan）。
    """
    v = _clean(x)
    tn = plain_t(v)
    th = newey_west_t(v, lag)
    shrink = float("nan")
    if tn == tn and th == th and abs(tn) > 1e-12:
        shrink = th / tn
    return {"n": int(v.size), "mean": (float(v.mean()) if v.size else float("nan")),
            "lag": int(lag), "t_naive": tn, "t_hac": th, "shrink": shrink}


def correct_rules(rules: list[dict], p_key: str = "p_value", alpha: float = DEFAULT_ALPHA,
                  method: str = "bh") -> dict:
    """对一批规则（含 p 值）做校正，返回 {passed, rejected, report}。

    规则的 p 值缺失（None）视为"未检验"→ 直接不进 passed（不能因为没算 p 就默认它显著）。
    """
    ps = [r.get(p_key) for r in rules]
    rep = bh_fdr(ps, alpha) if method == "bh" else bonferroni(ps, alpha)
    passed, rejected, untested = [], [], []
    for i, r in enumerate(rules):
        try:
            fv = float(r.get(p_key))          # type: ignore[arg-type]
        except (TypeError, ValueError):
            fv = float("nan")
        if fv != fv:                          # None / 非数字 / NaN 一律视为"未检验"
            untested.append(r)
    for pos, orig_i in enumerate(rep["idx"]):
        (passed if rep["reject"][pos] else rejected).append(rules[orig_i])
    rep["n_input"] = len(rules)
    rep["n_untested"] = len(untested)
    return {"passed": passed, "rejected": rejected, "untested": untested, "report": rep}


def newey_west_reg_t(y, x, lag: int = 0) -> dict:
    """单变量 OLS `y = a + b·x + e` 中 **b 的 naive t 与 HAC t**（Bartlett 核）。

    为什么还需要它（plans/24 §11.28 A3）：「情绪闸门」（§9）的检验**不是**"一条序列的均值"，
    而是"**极端情绪日** vs **其余日** 的前向收益差异" —— 两组样本都按**日期**排列，
    且前向收益本身重叠 ⇒ 必须看**回归系数**的 HAC 标准误，`newey_west_t` 在这用不上。

    公式（单变量，故 `(X'X)^{-1} = 1/Σx̃²`，`x̃ = x − mean(x)`）：

        b = Σ x̃·y / Σ x̃²
        得分 s_t = x̃_t · e_t
        S = γ0 + 2·Σ_{l=1..L} w_l·γ_l,  γ_l = (1/T)·Σ_t s_t·s_{t−l},  w_l = 1 − l/(L+1)
        V_b = T·S / (Σx̃²)²        ← 三明治的 bread 已被解析式代入
        t = b / sqrt(V_b)

    Args:
        y, x: 长度相同的序列（**成对剔除**非有限值，不会因各自 NaN 而错位）
        lag: 滞后阶数；调用方应取 `持有期 − 1`（重叠长度）

    Returns:
        {"n", "beta", "lag", "t_naive", "t_hac", "shrink"}。
        样本 < 30、x 无变异、或 HAC 方差 ≤ 0 ⇒ 相应值为 nan（不抛异常）。

    ⚠️ 口径说明：`t_naive` 用**同方差**假设（`s²/Σx̃²`）；
    `t_hac` 在 `lag=0` 时退化为 **White 异方差稳健** t，**不等同于** `t_naive`——
    两者在小样本下会有可察觉的差异，这是有意暴露的，不要当成 bug。
    """
    a = np.asarray(y, dtype="float64").ravel()
    b_ = np.asarray(x, dtype="float64").ravel()
    n0 = min(int(a.size), int(b_.size))
    a, b_ = a[:n0], b_[:n0]
    m = np.isfinite(a) & np.isfinite(b_)
    a, b_ = a[m], b_[m]
    T = int(a.size)
    nan = float("nan")
    res = {"n": T, "beta": nan, "lag": int(lag),
           "t_naive": nan, "t_hac": nan, "shrink": nan}
    if T < 30:
        return res
    xd = b_ - float(b_.mean())
    sxx = float(xd @ xd)
    if not (sxx > 1e-18):
        return res
    beta = float(xd @ (a - float(a.mean()))) / sxx
    res["beta"] = beta
    e = a - (float(a.mean()) - beta * float(b_.mean()) + beta * b_)
    # naive（同方差）
    s2 = float(e @ e) / (T - 2)
    v_naive = s2 / sxx
    t_naive = beta / math.sqrt(v_naive) if v_naive > 0 else nan
    # HAC（Bartlett）
    s = xd * e
    L = max(0, int(lag))
    S = float(s @ s) / T
    for l in range(1, min(L, T - 1) + 1):
        S += 2.0 * (1.0 - l / (L + 1.0)) * float(s[l:] @ s[:-l]) / T
    v_hac = T * S / (sxx * sxx)
    t_hac = beta / math.sqrt(v_hac) if v_hac > 0 else nan
    res["t_naive"] = t_naive
    res["t_hac"] = t_hac
    if t_naive == t_naive and t_hac == t_hac and abs(t_naive) > 1e-12:
        res["shrink"] = t_hac / t_naive
    return res


__all__ = ["bh_fdr", "bonferroni", "proportion_p", "correct_rules", "DEFAULT_ALPHA",
           "plain_t", "newey_west_t", "t_compare", "newey_west_reg_t"]
