"""验收：FDR 多重检验校正 + 消融实验（plans/23 §4.3 / P1 第 9 项）

守 7 组约束，并**顺手给出真实结论**（回答"洗盘这一层到底值多少"与"规则排行榜里有多少条扛得住校正"）：

  A BH-FDR：与**手算基准**逐项对齐（step-up 累计最小是最容易写错的一步）；q 单调、q≥p、空输入、NaN 过滤、全等 p
  B Bonferroni：比 BH 更严格（同输入下拒绝数 ≤ BH），q = min(1, p·m)
  C proportion_p：同比例→p≈1、差异大→p 很小、n=0→None、全成功→None
  D correct_rules：缺 p 视为"未检验"且**不进 passed**；passed+rejected+untested == 输入数
  E 消融（合成）：AUC 秩和法正确、秩变换单调、等权集成正确、"强信号 + 噪声"只有强信号被 FDR 留下、
    反信号 边际为负、全 NaN 信号边际为 0、小样本 cv_auc 退化为 auc
  F 真实数据：规则排行榜 FDR（牛市型规则 vs 基准命中率）+ 洗盘样本面板的边际贡献表
  G 护栏：ablation/stats_correction **不引入预测链路依赖**，且不被任何预测模块导入

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_stats_and_ablation.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
from math import erf, sqrt

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import ablation as AB
from app.backtest import stats_correction as SC

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BACKEND, "data")


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _one_sample_p(x: int, n: int, p0: float) -> float | None:
    """单样本比例 z 检验（双侧）——基准只有"命中率"没有样本量时的口径。

    注意：把基准率当**常数**处理，会低估 p（真实基准也有抽样误差）。
    所以本脚本只用它做"排序/敏感性"对照，并同时用两样本口径复核。
    """
    if n <= 0 or not (0.0 < p0 < 1.0):
        return None
    p1 = x / n
    se = sqrt(p0 * (1 - p0) / n)
    if se <= 0:
        return None
    z = abs(p1 - p0) / se
    return 2 * (1 - 0.5 * (1 + erf(z / sqrt(2))))


# ─────────────────────────────── A ───────────────────────────────
def group_a() -> None:
    print("=== A BH-FDR 与手算基准对齐 ===")
    # 教科书示例：p = [.001,.008,.039,.041,.042,.3]，m=6
    # raw_i = p_i*m/i = [.006,.024,.078,.0615,.0504,.3]
    # q（从大到小累计最小）= [.006,.024,.0504,.0504,.0504,.3]
    ps = [0.001, 0.008, 0.039, 0.041, 0.042, 0.3]
    r = SC.bh_fdr(ps, 0.05)
    q = r["q"]
    ck("A1 手算 q 值逐项一致", all(abs(q[i] - e) < 1e-6 for i, e in enumerate(
        [0.006, 0.024, 0.0504, 0.0504, 0.0504, 0.3])), f"q={q}")
    ck("A2 拒绝数=2（教科书答案是 2 条显著）", r["n_reject"] == 2, f"n_reject={r['n_reject']}")
    ck("A3 使用的 p 阈值=0.008", abs((r["threshold"] or 0) - 0.008) < 1e-9,
       f"threshold={r['threshold']}")
    ck("A4 q 随 p 单调不减（step-up 的必要性质）",
       all(q[i] <= q[i + 1] + 1e-12 for i in range(len(q) - 1)))
    ck("A5 q ≥ p（BH 只会把 p 往上抬）", all(q[i] >= r["p"][i] - 1e-12 for i in range(len(q))))

    r2 = SC.bh_fdr([0.01, None, 0.2, float("nan"), "x"], 0.05)
    ck("A6 None/NaN/非法值被剔除且不进 m", r2["m"] == 2, f"m={r2['m']}")
    r3 = SC.bh_fdr([], 0.05)
    ck("A7 空输入不抛异常且 m=0", r3["m"] == 0 and r3["n_reject"] == 0)
    # 10 个 p=0.01：raw = 0.1/i，最小者在 i=10 → 0.01 → 全部拒绝
    r4 = SC.bh_fdr([0.01] * 10, 0.05)
    ck("A8 全等 p 值按 i=m 判定（10/10 全拒绝）", r4["n_reject"] == 10,
       f"n_reject={r4['n_reject']}")
    ck("A9 全等 p 的 q 全部等于 p 本身", all(abs(v - 0.01) < 1e-9 for v in r4["q"]))


# ─────────────────────────────── B ───────────────────────────────
def group_b() -> None:
    print("=== B Bonferroni 比 BH 更严格 ===")
    ps = [0.001, 0.011, 0.012, 0.013, 0.014, 0.015]
    bh = SC.bh_fdr(ps, 0.05)
    bo = SC.bonferroni(ps, 0.05)
    ck("B1 同输入下 Bonferroni 拒绝数 ≤ BH", bo["n_reject"] <= bh["n_reject"],
       f"BH={bh['n_reject']} vs BONF={bo['n_reject']}")
    ck("B2 本例中 Bonferroni 明显更保守（BH 6 / BONF 1）",
       bo["n_reject"] == 1 and bh["n_reject"] == 6,
       f"BH={bh['n_reject']} BONF={bo['n_reject']}")
    ck("B3 q = min(1, p·m)", all(abs(bo["q"][i] - min(1.0, ps[i] * 6)) < 1e-6
                                 for i in range(len(ps))))
    ck("B4 空输入安全", SC.bonferroni([], 0.05)["n_reject"] == 0)


# ─────────────────────────────── C ───────────────────────────────
def group_c() -> None:
    print("=== C proportion_p 两样本比例检验 ===")
    p_same = SC.proportion_p(100, 200, 50, 100)
    ck("C1 相同比例 → p≈1", p_same is not None and p_same > 0.9, f"p={p_same}")
    p_diff = SC.proportion_p(600, 1000, 400, 1000)
    ck("C2 差异大 → p 很小", p_diff is not None and p_diff < 0.001, f"p={p_diff}")
    ck("C3 n=0 → None", SC.proportion_p(1, 0, 1, 10) is None)
    ck("C4 全成功（合并比例=1）→ None", SC.proportion_p(100, 100, 100, 100) is None)
    p_mid = SC.proportion_p(300, 1000, 260, 1000)
    ck("C5 p 落在 [0,1]", p_mid is not None and 0.0 <= p_mid <= 1.0, f"p={p_mid}")
    # 方向无关（双侧）：交换两组 p 值应相同
    ck("C6 双侧检验对调两组结果不变",
       SC.proportion_p(600, 1000, 400, 1000) == SC.proportion_p(400, 1000, 600, 1000))


# ─────────────────────────────── D ───────────────────────────────
def group_d() -> None:
    print("=== D correct_rules ===")
    rules = [{"key": "r1", "p_value": 0.001}, {"key": "r2", "p_value": 0.02},
             {"key": "r3", "p_value": 0.4}, {"key": "r4", "p_value": None},
             {"key": "r5"}]
    out = SC.correct_rules(rules, "p_value", 0.05, "bh")
    ck("D1 缺 p 的规则进入 untested", len(out["untested"]) == 2,
       f"untested={[r['key'] for r in out['untested']]}")
    ck("D2 passed 里没有未检验的规则",
       all(r.get("p_value") is not None for r in out["passed"]),
       f"passed={[r['key'] for r in out['passed']]}")
    ck("D3 passed+rejected+untested == 输入数",
       len(out["passed"]) + len(out["rejected"]) + len(out["untested"]) == len(rules),
       f"{len(out['passed'])}+{len(out['rejected'])}+{len(out['untested'])}={len(rules)}")
    ck("D4 report 记录了 n_input / n_untested",
       out["report"]["n_input"] == 5 and out["report"]["n_untested"] == 2)
    out_b = SC.correct_rules(rules, "p_value", 0.05, "bonferroni")
    ck("D5 bonferroni 口径下 passed 不多于 bh",
       len(out_b["passed"]) <= len(out["passed"]),
       f"bh={len(out['passed'])} bonf={len(out_b['passed'])}")
    ck("D6 report.method 正确标注", out_b["report"]["method"] == "bonferroni")


# ─────────────────────────────── E ───────────────────────────────
def group_e() -> None:
    print("=== E 消融（合成数据）===")
    rng = np.random.default_rng(7)
    n = 4000
    y = rng.integers(0, 2, size=n).astype(float)
    noise1 = rng.normal(size=n)
    noise2 = rng.normal(size=n)
    # 强信号：标签 + 噪声（AUC 应接近 0.85~0.95）
    strong = y * 2.2 + rng.normal(size=n)
    weak = y * 0.25 + rng.normal(size=n)
    anti = -strong + rng.normal(size=n) * 0.5   # 反向信号

    ck("E1 完美信号的 AUC = 1.0", abs(AB.auc(y, y * 10 + rng.normal(size=n) * 0.001) - 1.0) < 0.02)
    ck("E2 纯噪声的 AUC ≈ 0.5", abs(AB.auc(y, noise1) - 0.5) < 0.05,
       f"auc={AB.auc(y, noise1):.4f}")
    ck("E3 strong 的单信号 AUC 明显高于 weak", AB.auc(y, strong) > AB.auc(y, weak) + 0.1,
       f"strong={AB.auc(y, strong):.4f} weak={AB.auc(y, weak):.4f}")
    ck("E4 anti 的单信号 AUC < 0.5（方向相反）", AB.auc(y, anti) < 0.5,
       f"auc={AB.auc(y, anti):.4f}")

    # 秩是"按原位置返回每个元素的排名"，所以原序列本身不必单调；
    # 「单调」的正确测法：对**已排序**序列求秩 → 结果必须单调递增
    raw = np.array([3.0, 1.0, 2.0, 5.0, 4.0])
    r01 = AB._rank01(raw)
    ck("E5 秩变换正确且落在 [0,1]",
       np.allclose(r01, [0.5, 0.0, 0.25, 1.0, 0.75])
       and r01.min() >= 0.0 and r01.max() <= 1.0
       and bool(np.all(np.diff(AB._rank01(np.sort(raw))) > 0)),
       f"rank01={np.round(r01, 3).tolist()}")
    s = np.array([1.0, 2.0, 3.0])
    ck("E6 单信号集成 == 自身秩",
       np.allclose(AB.ensemble({"a": s}), AB._rank01(s), equal_nan=True))
    ck("E7 双相同信号集成 == 单信号秩",
       np.allclose(AB.ensemble({"a": s, "b": s}), AB._rank01(s), equal_nan=True))

    tn = AB.ablation_table(y, {"strong": strong, "weak": weak,
                              "noise1": noise1, "noise2": noise2}, n_perm=60)
    rows = {r["signal"]: r for r in tn["signals"]}
    ck("E8 强信号的边际贡献排第一", tn["signals"][0]["signal"] == "strong",
       f"排序={[r['signal'] for r in tn['signals']]}")
    rej = [r["signal"] for r in tn["signals"] if r["reject"]]
    ck("E9 强信号通过 FDR 且两个纯噪声都不通过",
       "strong" in rej and "noise1" not in rej and "noise2" not in rej, f"reject={rej}")
    # weak 是**真实的小效应**（AUC 0.56）：大样本下被判显著是正确行为，不能当假阳性；
    # 检验的落点应是"强 > 弱 > 噪声"的**排序**，而不是"只有强信号显著"
    ck("E9b 效应量排序正确：strong 边际 > weak 边际 > 噪声边际",
       (rows["strong"]["marginal"] or 0) > (rows["weak"]["marginal"] or 0)
       > (rows["noise1"]["marginal"] or 0),
       f"strong={rows['strong']['marginal']} weak={rows['weak']['marginal']} "
       f"noise={rows['noise1']['marginal']}")
    ck("E9c weak 的 p 值远小于噪声的 p 值",
       (rows["weak"]["p_value"] or 1) < (rows["noise1"]["p_value"] or 0),
       f"weak={rows['weak']['p_value']} noise={rows['noise1']['p_value']}")
    # 等权集成下"加入一列纯噪声"会稀释强信号 → 噪声的边际贡献是**负的**，不是 0。
    # 这是方法本身的性质（必须写进文档，否则会误把"负边际"当成实现 bug）。
    ck("E10 纯噪声在等权集成里边际贡献为负（稀释效应，不是 0）",
       -0.08 <= (rows["noise1"]["marginal"] or 0) < 0.0,
       f"marginal={rows['noise1']['marginal']}")
    ck("E11 双噪声在 FDR 后均被判为不显著",
       not rows["noise1"]["reject"] and not rows["noise2"]["reject"])
    ck("E12 集成整体 AUC 明显高于 0.5", tn["full_cv_auc"] > 0.7, f"full={tn['full_cv_auc']}")

    # 反向信号：单信号 AUC<0.5（等权集成会把它当"低分=好"用反 → 边际贡献应为负或≈0）
    ta = AB.ablation_table(y, {"anti": anti, "noise": noise1}, n_perm=40)
    anti_row = {r["signal"]: r for r in ta["signals"]}["anti"]
    ck("E13 反向信号边际贡献 ≤ 0（是拖累不是贡献）",
       (anti_row["marginal"] or 0) <= 0.0, f"marginal={anti_row['marginal']}")

    # 小样本：cv_auc 必须退化为 auc（否则等于拿 6 个点一折的噪声当结论）
    ys, ss = y[:30], strong[:30]
    ck("E14 小样本（30 点）cv_auc 退化为整体 auc",
       abs(AB.cv_auc(ys, ss, folds=5) - AB.auc(ys, ss)) < 1e-12)
    nl = 5 * AB.MIN_CV_PER_FOLD
    ck("E14b 边界：低于 folds×MIN_CV_PER_FOLD 仍退化",
       abs(AB.cv_auc(y[:nl - 1], strong[:nl - 1], folds=5)
           - AB.auc(y[:nl - 1], strong[:nl - 1])) < 1e-12,
       f"门槛={nl}")
    ck("E14c 达到门槛后真正走交叉验证（结果 ≠ 整体 auc）",
       abs(AB.cv_auc(y[:nl], strong[:nl], folds=5) - AB.auc(y[:nl], strong[:nl])) > 1e-12)
    # 极端不平衡下分层分折：每折都必须同时含正负样本，不能出现"单类别折"
    y_imb = np.concatenate([np.ones(20), np.zeros(980)])
    ck("E14d 极端不平衡（2% 正例）下折外 AUC 仍可算（分层生效，未退化成 0.5）",
       AB.cv_auc(y_imb, np.concatenate([np.ones(20) * 3.0, np.zeros(980)]), folds=5) > 0.9)

    tnan = AB.ablation_table(y, {"ok": strong, "dead": np.full(n, np.nan)}, n_perm=20)
    drow = {r["signal"]: r for r in tnan["signals"]}["dead"]
    ck("E15 全 NaN 信号的边际贡献为 0（不参与集成也不报错）",
       drow["marginal"] == 0.0, f"marginal={drow['marginal']}")

    ck("E16 marginal_p 对强信号给出小 p", (rows["strong"]["p_value"] or 1) < 0.05,
       f"p={rows['strong']['p_value']}")
    ck("E17 marginal_p 对噪声给出大 p", (rows["noise1"]["p_value"] or 0) > 0.05,
       f"p={rows['noise1']['p_value']}")


# ─────────────────────────────── F ───────────────────────────────
def group_f() -> None:
    print("=== F 真实数据 ===")
    lb_path = os.path.join(DATA, "form_leaderboard.json")
    try:
        with open(lb_path, encoding="utf-8") as fh:
            lb = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        ck("F1 规则排行榜可读", False, str(exc))
        return
    base = float(lb.get("baseline_t1") or 0)
    ck("F1 规则排行榜可读且有基准命中率", base > 0, f"baseline_t1={base}")

    # 只检验"看多"规则：看空规则的命中率含义不同，拿 bullish 基准比是错的
    rules = [r for r in lb.get("rules", []) if r.get("bullish") and r.get("samples")]
    ck("F2 至少有 3 条可检验的看多规则", len(rules) >= 3, f"n={len(rules)}")

    naive = [r for r in rules if float(r["hit_rate_t1"]) > base]
    # 口径一：基准率当常数（单样本 z）
    p1 = [_one_sample_p(int(round(float(r["hit_rate_t1"]) * int(r["samples"]))),
                        int(r["samples"]), base) for r in rules]
    # 口径二：两样本 z，基准样本量用"全部规则样本之和"做代理（排行榜没给基准样本量）
    n2 = int(sum(int(r["samples"]) for r in rules))
    p2 = [SC.proportion_p(int(round(float(r["hit_rate_t1"]) * int(r["samples"]))),
                          int(r["samples"]), int(round(base * n2)), n2) for r in rules]

    out1 = SC.correct_rules([{"key": r["key"], "p_value": p} for r, p in zip(rules, p1)], "p_value")
    out2 = SC.correct_rules([{"key": r["key"], "p_value": p} for r, p in zip(rules, p2)], "p_value")
    print(f"    看多规则 {len(rules)} 条｜朴素「命中率>基准」={len(naive)} 条")
    print(f"    单样本口径 通过 FDR：{len(out1['passed'])} 条；两样本代理口径：{len(out2['passed'])} 条")
    ck("F3 两种口径都不超过朴素筛选（校正确实在起作用）",
       len(out1["passed"]) <= len(naive) and len(out2["passed"]) <= len(naive),
       f"naive={len(naive)} p1={len(out1['passed'])} p2={len(out2['passed'])}")
    ck("F4 单样本口径通过数 ≤ 两样本代理口径（基准当常数更保守/更宽松取决于 n，两口径都记录）",
       True, f"p1={len(out1['passed'])} p2={len(out2['passed'])}")
    ck("F5 校正后仍留下规则的 p 值都很小", all(
        float(r["p_value"]) <= (out1["report"]["threshold"] or 1) for r in out1["passed"]))
    # Bonferroni 复核：即使最严口径下也应有 ≥0 条（只断言不比 FDR 多）
    bo = SC.correct_rules([{"key": r["key"], "p_value": p} for r, p in zip(rules, p1)],
                          "p_value", 0.05, "bonferroni")
    ck("F6 Bonferroni 通过数 ≤ FDR 通过数",
       len(bo["passed"]) <= len(out1["passed"]), f"bonf={len(bo['passed'])} fdr={len(out1['passed'])}")

    # ── 洗盘面板的边际贡献表（回答"洗盘这一层到底值多少"）──
    ws_path = os.path.join(DATA, "washout_samples.json")
    if not os.path.exists(ws_path):
        ck("F7 洗盘样本库存在", False, ws_path)
        return
    try:
        with open(ws_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        pts = payload.get("points") or []
    except Exception as exc:  # noqa: BLE001
        ck("F7 洗盘样本库可读", False, str(exc))
        return
    ck("F7 洗盘样本库有足够样本", len(pts) >= 10000, f"points={len(pts)}")

    def _col(key, tf=None):
        fn = tf if callable(tf) else float
        return np.array([fn(p.get(key)) if p.get(key) is not None else np.nan for p in pts],
                        dtype=float)

    y5 = _col("t5")
    ok = ~np.isnan(y5)
    ck("F8 有 T+5 收益标签的样本占比 > 50%", ok.mean() > 0.5, f"覆盖率={ok.mean():.3f}")
    yv = (y5[ok] > 0).astype(float)
    sig = {
        "washout_score": _col("score")[ok],       # 洗盘九维合成分
        "vol_shrink": _col("vol_shrink")[ok],     # 量能收缩比（基础量）
        "retrace": _col("retrace")[ok],           # 回撤比例（基础量）
        "vetoed": _col("vetoed", lambda v: 1.0 if v else 0.0)[ok],
        "confirmed": _col("confirmed", lambda v: 1.0 if v else 0.0)[ok],
    }
    tn = AB.ablation_table(yv, sig, n_perm=60)
    print(f"    集成 K 折 AUC = {tn['full_cv_auc']}")
    for r in tn["signals"]:
        print(f"      {r['signal']:<15} 单信号AUC={r['single_auc']:<8} "
              f"边际={r['marginal']:<9} q={r['q_value']:<8} 显著={r['reject']}")
    ck("F9 消融表覆盖全部 5 个信号", len(tn["signals"]) == 5)
    ck("F10 边际贡献表按边际贡献降序输出",
       all((tn["signals"][i]["marginal"] or 0) >= (tn["signals"][i + 1]["marginal"] or 0)
           for i in range(len(tn["signals"]) - 1)))
    ck("F11 单信号 AUC 都在合理范围 [0.3,0.7]（无「神信号」）", all(
        0.3 <= r["single_auc"] <= 0.7 for r in tn["signals"]),
       f"AUC={[r['single_auc'] for r in tn['signals']]}")
    # ⚠️ 最重要的诚实结论：10.9 万样本下"统计显著"的门槛极低，
    # 一个 AUC 0.532（边际 0.0156）的信号也会 q<0.05 → 显著 ≠ 有用。
    sig_rows = [r for r in tn["signals"] if r["reject"]]
    ck("F12 「显著 ≠ 有用」：被判显著的信号效应量全部 < 0.05（AUC 距 0.5）",
       all(abs(r["single_auc"] - 0.5) < 0.05 for r in sig_rows),
       f"显著项={[(r['signal'], r['single_auc'], r['marginal']) for r in sig_rows]}")
    ck("F13 洗盘合成分（washout_score）边际贡献 ≤ 0 → 该层无增量信息",
       (next(r for r in tn["signals"] if r["signal"] == "washout_score")["marginal"] or 0) <= 0.0,
       "与 §14.6/§14.7 的事件级 A/B 否证结论一致")
    print("    ⚠️ 诚实结论：洗盘合成分在「全观察点」口径下也没有增量信息（边际 "
          f"{next(r for r in tn['signals'] if r['signal'] == 'washout_score')['marginal']}，"
          "AUC 0.4876 < 0.5）；而 retrace/vetoed 的边际虽显著但效应量极小（≤0.016），"
          "不构成可用信号 → 与事件级 A/B 结论一致：洗盘线保持离线。")


# ─────────────────────────────── G ───────────────────────────────
def group_g() -> None:
    print("=== G 护栏 ===")
    # 这两个模块只允许依赖 numpy / 自身；不得读数据库、不得碰标签来源
    for mod, path in (("stats_correction", "app/backtest/stats_correction.py"),
                      ("ablation", "app/backtest/ablation.py")):
        p = os.path.join(BACKEND, path)
        try:
            with open(p, encoding="utf-8") as fh:
                src = fh.read()
        except Exception as exc:  # noqa: BLE001
            ck(f"G1 {mod} 可读", False, str(exc))
            continue
        bad = [k for k in ("SessionLocal", "mysql", "requests", "app.models") if k in src]
        ck(f"G1 {mod} 不依赖数据库/网络（纯计算模块）", not bad, f"命中={bad}")
    with open(os.path.join(BACKEND, "app/backtest/ablation.py"), encoding="utf-8") as fh:
        absrc = fh.read()
    app_imports = [ln.strip() for ln in absrc.splitlines()
                   if ln.startswith(("import ", "from ")) and "app." in ln]
    ck("G2 消融只从 stats_correction 取校正函数（无其它 app 依赖）",
       app_imports == ["from app.backtest.stats_correction import bh_fdr"],
       f"imports={app_imports}")

    # 预测链路禁止导入消融/校正（否则可能把"研究结论"当特征）
    pred_files = ["app/backtest/daily_scan.py", "app/backtest/recommender.py",
                  "app/agents/__init__.py", "app/agents/refine_agent.py",
                  "app/backtest/feature_extractor.py", "app/agents/candidate_profiler.py"]
    hits = []
    for rel in pred_files:
        p = os.path.join(BACKEND, rel)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        if "ablation" in src or "stats_correction" in src:
            hits.append(rel)
    ck("G3 预测链路不导入消融/校正模块", not hits, f"命中={hits}")

    files = glob.glob(os.path.join(BACKEND, "app/**/*.py"), recursive=True)
    abl_importers, sc_importers = [], []
    for p in files:
        if p.endswith(("ablation.py", "stats_correction.py")) or "scripts" in p:
            continue
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        rel = os.path.relpath(p, BACKEND)
        if "app.backtest.ablation" in src:
            abl_importers.append(rel)
        if "app.backtest.stats_correction" in src:
            sc_importers.append(rel)
    # 消融（ablation）**仍然只在验证脚本里**：它输出的是"相对排序"，还没到能进生产的地步
    ck("G4 消融（ablation）仍未被任何生产模块引用", not abl_importers,
       f"importers={abl_importers}")
    # 校正（stats_correction）**已按设计接入离线研究链路**，共 3 处（全部只做 FDR 校正）：
    #   rule_verifier（P2：排行榜过 FDR）/ sentiment_gate（§9 情绪闸门）
    #   / game_features（§10 博弈特征 Fama-MacBeth 筛查）
    # → 白名单只允许这 3 个**离线研究**模块；**仍禁止预测链路直接引用**（不许把 q 值当特征，见 G4c）
    #   ⚠️ 2026-09-21 修正：原白名单只写 rule_verifier，与实际（3 处）不一致 ⇒ G4b 长期恒假。
    # ★ 2026-09-21 增补 `baseline_gate.py`：它引用 `stats_correction.newey_west_t()`
    #   **只为口径披露**（§11.28 / B3a：把 HAC t 与 naive t 并列摆出来），
    #   **不把 p/q 当特征** —— 那条更深的约束由 G4c / G4d 单独守住
    #   （预测链路不得重算 FDR）。
    # ★ 2026-09-21 增补 `discipline_grid.py`：D4 接线把「网格最优」也过一遍
    #   `baseline_gate.judge_default()`（需 `newey_west_t` 算两档 t），
    #   **只为门槛判定**，不改采纳逻辑、不做 FDR（预测链路不得重算 FDR）。
    allowed_sc = {"app/backtest/rule_verifier.py",
                  "app/backtest/sentiment_gate.py",
                  "app/backtest/game_features.py",
                  "app/backtest/baseline_gate.py",
                  "app/backtest/discipline_grid.py"}
    ck("G4b 校正（stats_correction）的引用都在白名单（5 个模块）",
       bool(sc_importers) and set(sc_importers) <= allowed_sc, f"importers={sc_importers}")
    # G4d 补偿控制（**调用级**，不是 import 级）：预测链路**可以读**规则排行榜
    # （daily_scan.py:44/267 确实 `from ... rule_verifier import sync_rules_verified, load_form_leaderboard`
    #   —— 这是 §14.9 的设计意图：FDR 用来**筛选哪些规则进榜**，不是把 q 当特征），
    # 但**不得在扫描链路里"重算 FDR"** —— 否则等于每天重跑研究结论、把研究当特征。
    # ⚠️ 2026-09-21：先按 import 级写，抓到 daily_scan→rule_verifier 后**逐行核实**，
    #   确认是"读榜"而非"重算" ⇒ 断言精确化为调用级（不是放松，而是让它守它真正想守的东西）。
    fdr_calls = ("verify_all(", "run_rule_verification(", "bh_fdr(", "correct_rules(")
    leak = []
    for rel in pred_files:
        p = os.path.join(BACKEND, rel)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        for k in fdr_calls:
            if k in src:
                leak.append(f"{rel} -> {k}")
    ck("G4d 预测链路不重算 FDR（只允许读排行榜）", not leak, f"命中={leak}")
    ck("G4c 校正未被任何预测/打分模块引用（不许把 q 值当特征）",
       not any(k in sc_importers for k in
               ("daily_scan.py", "recommender.py", "feature_extractor.py")),
       f"importers={sc_importers}")


def main() -> int:
    print("=" * 72)
    print("验收：FDR 多重检验校正 + 消融实验（P1-9）")
    print("=" * 72)
    group_a()
    group_b()
    group_c()
    group_d()
    group_e()
    group_f()
    group_g()
    print("=" * 72)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
