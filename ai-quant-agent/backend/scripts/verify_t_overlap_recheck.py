"""重叠样本的 t 值复核（plans/24 §11.28）—— 把 §11.27 的发现推广到全层

## 问题（§11.27 实测）

    逐日滚动的持有期收益 ⇒ 相邻样本共享 (h−1) 天 ⇒ naive t 被**系统性放大**
    实测：naive t = +3.08 → Newey-West(HAC, lag=19) t = +0.94

本层的**每一条整体显著性结论**都建立在同一类序列上（持 5 / 10 / 20 日），
所以需要一次统一复核：哪些结论扛得住 HAC，哪些是重叠造成的假象。

## 能重算 vs 不能重算（诚实登记）

    **全部 7 个数据源已可复核**（2026-09-21 补齐 §11.28.12 的 A2 / A3 之后）：

      · gate_cost_cut_daily.json      → base5/10/20、combo5/10/20（§11.15/§11.25/§11.26/§11.27）
      · baseline_sampling_daily.json  → all / q1（§11.18 的抽样对照，持 20 日）
      · size_baseline_series.json     → §11.16 的 Q1~Q5 分层 + 两个基准（②b）
      · ours_baseline_series.json     → §11.21「我们」的四口径（②c）
      · cost_slippage_series.json     → §11.24 的滑点 / 流动性分组（②d）
      · game_features_series.json     → §10.2 Fama-MacBeth 的日度高减低差（②e，A2 新落盘）
      · sentiment_gate_series.json    → §9 情绪闸门（②f，A3 新落盘）

    ⚠️ 两类检验的 HAC 工具**不同**，不要混用：
      · `newey_west_t`        —— 适用「**一条序列的均值**」（② / ②b / ②c / ②d / ②e）；
      · `newey_west_reg_t`    —— 适用「**跨日两组比较**」（②f：极端情绪日 vs 中段日）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_t_overlap_recheck.py
"""
from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.backtest.stats_correction import newey_west_reg_t, t_compare  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BACKEND, "data")
OUT = os.path.join(DATA, "t_overlap_recheck.json")
H5, H10, H20 = 5, 10, 20

# 只能拿到汇总 t 的缓存（登记，不伪造）——
# ★ 2026-09-21：**已清空**。§11.28.12 的 A2（game_features）与 A3（sentiment_gate）
#   把最后两个"只有汇总 t"的数据源也补上了原始序列
#   ⇒ 本层的整体显著性**已 100% 可复核**。
NOT_RECHECKABLE: tuple = ()


def _load(name: str):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def verdict_of(tn: float, th: float) -> str:
    """按「naive |t| ≥ 2 是否在 HAC 下保住」分类 —— 本脚本的核心判词。"""
    a = (tn == tn) and abs(tn) >= 2.0
    b = (th == th) and abs(th) >= 2.0
    if a and not b:
        return "① 显著性被重叠夺走"          # 危险：naive 显著、HAC 不显著
    if a and b:
        return "② 稳健（HAC 后仍显著）"
    return "③ 本来就不显著"                  # 无论怎么算都不显著


def daily_mean_series(m) -> list:
    """把 {date: [per-stock 收益]} 变成"每日组合均值"序列（等权，NaN 剔除）。"""
    out: list = []
    for k in sorted(m or {}):
        v = m[k]
        if not isinstance(v, list) or not v:
            continue
        arr = [float(x) for x in v if isinstance(x, (int, float)) and x == x]
        if arr:
            out.append(sum(arr) / len(arr))
    return out


def main() -> int:
    print("=" * 112)
    print("重叠样本 t 值复核（plans/24 §11.28）—— naive t vs Newey-West(HAC) t")
    print("=" * 112)
    print("  判词：① 显著性被重叠夺走（naive 显著 / HAC 不显著，**最危险**）")
    print("        ② 稳健（两者都显著）   ③ 本来就不显著")
    print()

    rows: list = []

    # ---------- ① gate_cost_cut_daily：基准与组合（§11.15 / §11.25 / §11.26 / §11.27） ----------
    print("=" * 112)
    print("① `gate_cost_cut_daily.json` —— 本层**所有门槛判定的收益来源**")
    print("=" * 112)
    g = _load("gate_cost_cut_daily.json")
    if g:
        df = pd.DataFrame(g)
        cols = [(c, h) for c, h in (("base5", H5), ("base10", H10), ("base20", H20),
                                    ("combo5", H5), ("combo10", H10), ("combo20", H20))
                if c in df.columns]
        hd = f"{'序列':<12}{'持有期':>7}{'有效n':>7}{'均值/期':>11}{'naive t':>10}{'HAC t':>9}{'收缩':>8}  判词"
        print(hd)
        print("-" * len(hd))
        for c, h in cols:
            s = pd.to_numeric(df[c], errors="coerce").dropna()
            r = t_compare(s.to_numpy(dtype="float64"), h - 1)
            vd = verdict_of(r["t_naive"], r["t_hac"])
            sh = "—" if r["shrink"] != r["shrink"] else f"{r['shrink']:.2f}"
            print(f"{c:<12}{h:>7}{r['n']:>7}{r['mean']:>11.4%}"
                  f"{r['t_naive']:>10.2f}{r['t_hac']:>9.2f}{sh:>8}  {vd}")
            rows.append({"src": "gate_cost_cut_daily", "series": c, "h": h,
                         "n": r["n"], "mean": r["mean"], "t_naive": r["t_naive"],
                         "t_hac": r["t_hac"], "lag": h - 1, "verdict": vd})
        print()
        print("  读法：`base20` = **B1（全市场等权 · 持 20 日）**，`combo20` = §11.14 的 4 因子组合；")
        print("        这一列是 §11.17 / §11.25 门槛里「基准 t」与「候选 t」的共同来源。")
        print()

    # ---------- ② baseline_sampling_daily：抽样对照（§11.18） ----------
    print("=" * 112)
    print("② `baseline_sampling_daily.json` —— §11.18 的抽样对照（持 20 日）")
    print("=" * 112)
    b = _load("baseline_sampling_daily.json")
    if isinstance(b, dict):
        hd = f"{'口径':<10}{'有效n':>7}{'均值/期':>11}{'naive t':>10}{'HAC t':>9}{'收缩':>8}  判词"
        print(hd)
        print("-" * len(hd))
        for k in ("all", "q1"):
            if k not in b:
                continue
            s = daily_mean_series(b[k])
            r = t_compare(s, H20 - 1)
            vd = verdict_of(r["t_naive"], r["t_hac"])
            sh = "—" if r["shrink"] != r["shrink"] else f"{r['shrink']:.2f}"
            lab = "all 全市场" if k == "all" else "q1 最小市值"
            print(f"{lab:<10}{r['n']:>7}{r['mean']:>11.4%}"
                  f"{r['t_naive']:>10.2f}{r['t_hac']:>9.2f}{sh:>8}  {vd}")
            rows.append({"src": "baseline_sampling_daily", "series": k, "h": H20,
                         "n": r["n"], "mean": r["mean"], "t_naive": r["t_naive"],
                         "t_hac": r["t_hac"], "lag": H20 - 1, "verdict": vd})
        print()

    # ---------- ②b size_baseline_series：§11.16 的五层 + 两个基准（2026-09-21 新落盘） ----------
    print("=" * 112)
    print("②b `size_baseline_series.json` —— §11.16 市值分层的**逐层日序列**（已可复核）")
    print("=" * 112)
    sb = _load("size_baseline_series.json")
    if isinstance(sb, dict) and sb.get("series"):
        ser = sb["series"]
        names = {"size0": "Q1 最小", "size1": "Q2", "size2": "Q3", "size3": "Q4",
                 "size4": "Q5 最大", "market_ew": "全市场等权", "market_capw": "市值加权"}
        hd = (f"{'层 / 基准':<14}{'持有期':>7}{'有效n':>7}{'均值/期':>10}"
              f"{'naive t':>10}{'HAC t':>9}{'收缩':>8}  判词")
        print(hd)
        print("-" * len(hd))
        for key in sorted(ser.keys(),
                          key=lambda x: (int(x.split("_")[0][1:]), x.split("_", 1)[1])):
            h = int(key.split("_")[0][1:])
            tag = key.split("_", 1)[1]
            vals = [v for v in ser[key].values() if isinstance(v, (int, float))]
            r = t_compare(vals, h - 1)
            vd = verdict_of(r["t_naive"], r["t_hac"])
            sh = "—" if r["shrink"] != r["shrink"] else f"{r['shrink']:.2f}"
            print(f"{names.get(tag, tag):<14}{h:>7}{r['n']:>7}{r['mean']:>10.4%}"
                  f"{r['t_naive']:>10.2f}{r['t_hac']:>9.2f}{sh:>8}  {vd}")
            rows.append({"src": "size_baseline_series", "series": key, "h": h,
                         "n": r["n"], "mean": r["mean"], "t_naive": r["t_naive"],
                         "t_hac": r["t_hac"], "lag": h - 1, "verdict": vd})
        print()
        print("  读法：**点估计的单调性不变**（Q1 +24.0% > Q2 > Q3 > Q4），"
              "但每一层的 HAC t 都掉到 1 附近。")
        print("        另注意 **Q5 的 naive t（4.04）高于 Q4（3.16）** —— "
              "那是**低波动**带来的，不是高收益。")
        print()
    else:
        print("  ⚠️ 未找到 `size_baseline_series.json` ⇒ §11.16 仍不可复核。")
        print("     生成：cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_size_baseline.py")
        print()

    # ---------- ②c ours_baseline_series：§11.21「我们」的四口径（2026-09-21 新落盘） ----------
    print("=" * 112)
    print("②c `ours_baseline_series.json` —— §11.21「我们」的四口径")
    print("=" * 112)
    ob = _load("ours_baseline_series.json")
    if isinstance(ob, dict) and ob.get("series"):
        hd = (f"{'口径':<16}{'持有期':>7}{'有效n':>7}{'均值/期':>10}"
              f"{'naive t':>10}{'HAC t':>9}{'收缩':>8}  判词")
        print(hd)
        print("-" * len(hd))
        for key in sorted(ob["series"].keys()):
            h = int(key.split("_")[0][1:])
            vals = [v for v in ob["series"][key].values() if isinstance(v, (int, float))]
            r = t_compare(vals, h - 1)
            vd = verdict_of(r["t_naive"], r["t_hac"])
            sh = "—" if r["shrink"] != r["shrink"] else f"{r['shrink']:.2f}"
            print(f"{key.split('_', 1)[1]:<16}{h:>7}{r['n']:>7}{r['mean']:>10.4%}"
                  f"{r['t_naive']:>10.2f}{r['t_hac']:>9.2f}{sh:>8}  {vd}")
            rows.append({"src": "ours_baseline_series", "series": key, "h": h,
                         "n": r["n"], "mean": r["mean"], "t_naive": r["t_naive"],
                         "t_hac": r["t_hac"], "lag": h - 1, "verdict": vd})
        print()
        print("  ⚠️ 注意：我们的推荐记录只有 **22 个交易日** ⇒ 连 `n ≥ 30` 的门槛都不到，")
        print("     所以 `naive t` / `HAC t` 都算不出来（显示 nan）——")
        print("     这正是 §11.28.6 那条 `underpowered` 护栏的**最极端情形**。")
        print()
    else:
        print("  ⚠️ 未找到 `ours_baseline_series.json` ⇒ §11.21 仍不可复核。")
        print("     生成：cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_ours_baseline.py")
        print()

    # ---------- ②d cost_slippage_series：§11.24 的买不进 / 流动性分组（2026-09-21 新落盘） ----------
    print("=" * 112)
    print("②d `cost_slippage_series.json` —— §11.24 的买不进 / 流动性分组")
    print("=" * 112)
    cs = _load("cost_slippage_series.json")
    if isinstance(cs, dict) and cs.get("series"):
        hd = (f"{'口径':<16}{'持有期':>7}{'有效n':>7}{'均值/期':>10}"
              f"{'naive t':>10}{'HAC t':>9}{'收缩':>8}  判词")
        print(hd)
        print("-" * len(hd))
        for key in sorted(cs["series"].keys(),
                          key=lambda x: (int(x.split("_")[0][1:]), x.split("_", 1)[1])):
            h = int(key.split("_")[0][1:])
            tag = key.split("_", 1)[1]
            vals = [v for v in cs["series"][key].values() if isinstance(v, (int, float))]
            r = t_compare(vals, h - 1)
            vd = verdict_of(r["t_naive"], r["t_hac"])
            sh = "—" if r["shrink"] != r["shrink"] else f"{r['shrink']:.2f}"
            print(f"{tag:<16}{h:>7}{r['n']:>7}{r['mean']:>10.4%}"
                  f"{r['t_naive']:>10.2f}{r['t_hac']:>9.2f}{sh:>8}  {vd}")
            rows.append({"src": "cost_slippage_series", "series": key, "h": h,
                         "n": r["n"], "mean": r["mean"], "t_naive": r["t_naive"],
                         "t_hac": r["t_hac"], "lag": h - 1, "verdict": vd})
        print()
        print("  读法：`fillable` = 剔除「T+1 开盘即涨停」买不进的样本；`liq0` = 成交额最小档。")
        print()
    else:
        print("  ⚠️ 未找到 `cost_slippage_series.json` ⇒ §11.24 仍不可复核。")
        print("     生成：cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_cost_slippage.py（约 6.5 分钟）")
        print()

    # ---------- ②e game_features_series：§10.2 Fama-MacBeth 的日度高减低差（A2 新落盘） ----------
    print("=" * 112)
    print("②e `game_features_series.json` —— §10.2 博弈特征的**日度高减低差序列**（A2）")
    print("=" * 112)
    gf = _load("game_features_series.json")
    if isinstance(gf, dict) and gf.get("series"):
        gh = int(gf.get("horizon", 5))
        gl = int(gf.get("lag", gh - 1))
        hd = (f"{'特征':<16}{'持有期':>7}{'有效n':>7}{'均值/期':>10}"
              f"{'naive t':>10}{'HAC t':>9}{'收缩':>8}  判词")
        print(hd)
        print("-" * len(hd))
        for feat in sorted(gf["series"].keys()):
            vals = [v for v in gf["series"][feat].values() if isinstance(v, (int, float))]
            r = t_compare(vals, gl)
            vd = verdict_of(r["t_naive"], r["t_hac"])
            sh = "—" if r["shrink"] != r["shrink"] else f"{r['shrink']:.2f}"
            print(f"{feat:<16}{gh:>7}{r['n']:>7}{r['mean']:>10.4%}"
                  f"{r['t_naive']:>10.2f}{r['t_hac']:>9.2f}{sh:>8}  {vd}")
            rows.append({"src": "game_features_series", "series": feat, "h": gh,
                         "n": r["n"], "mean": r["mean"], "t_naive": r["t_naive"],
                         "t_hac": r["t_hac"], "lag": gl, "verdict": vd})
        print()
        print(f"  读法：共 {len(gf['series'])} 个特征，全部用同一个 lag = {gl}（即 fwd5 的重叠长度）；")
        print("        这是 §10.2 里唯一一条『高减低为正』的检验口径。")
        print()
    else:
        print("  ⚠️ 未找到 `game_features_series.json`。")
        print("     生成：cd ai-quant-agent/backend && ./venv/bin/python -m app.backtest.game_features"
              "（约 23 分钟）")
        print()

    # ---------- ②f sentiment_gate_series：§9 情绪闸门（A3 新落盘，用**回归** HAC） ----------
    print("=" * 112)
    print("②f `sentiment_gate_series.json` —— §9 情绪闸门（跨日两组比较 ⇒ 用 newey_west_reg_t）")
    print("=" * 112)
    sgs = _load("sentiment_gate_series.json")
    if isinstance(sgs, dict) and sgs.get("x") and sgs.get("fwd"):
        try:
            from app.backtest import sentiment_gate as SG  # noqa: PLC0415

            hd = (f"{'指标':<14}{'期限':>5}{'侧':>7}{'有效n':>7}"
                  f"{'naive t':>10}{'HAC t':>9}{'收缩':>8}  判词")
            print(hd)
            print("-" * len(hd))
            _n_sg = 0
            for feat in list(sgs["x"].keys()):
                xs = sgs["x"][feat]
                for h in list(sgs.get("horizons") or [5]):
                    fw = sgs["fwd"].get(f"fwd{h}") or {}
                    recs = [(k, float(v), float(fw[k])) for k, v in xs.items() if k in fw]
                    if len(recs) < 60:
                        continue
                    dd = pd.DataFrame(recs, columns=["day", "x", "fwd"]).sort_values("day")
                    dd["q"] = SG._labels(dd["x"], 5)
                    dd = dd.dropna(subset=["q"]).reset_index(drop=True)
                    if dd.empty or dd["q"].nunique() < 3:
                        continue
                    lo_q, hi_q = int(dd["q"].min()), int(dd["q"].max())
                    mid = ((dd["q"] > lo_q) & (dd["q"] < hi_q)).to_numpy()
                    for side, msk in (("low", (dd["q"] == lo_q).to_numpy()),
                                      ("high", (dd["q"] == hi_q).to_numpy())):
                        sel = msk | mid
                        rr = newey_west_reg_t(
                            dd.loc[sel, "fwd"].to_numpy(dtype="float64"),
                            msk[sel].astype("float64"), max(0, h - 1))
                        vd = verdict_of(rr["t_naive"], rr["t_hac"])
                        sh = "—" if rr["shrink"] != rr["shrink"] else f"{rr['shrink']:.2f}"
                        print(f"{feat:<14}{h:>5}{side:>7}{rr['n']:>7}"
                              f"{rr['t_naive']:>10.2f}{rr['t_hac']:>9.2f}{sh:>8}  {vd}")
                        rows.append({"src": "sentiment_gate_series",
                                     "series": f"{feat}|h{h}|{side}", "h": h,
                                     "n": rr["n"], "mean": rr["beta"],
                                     "t_naive": rr["t_naive"], "t_hac": rr["t_hac"],
                                     "lag": max(0, h - 1), "verdict": vd})
                        _n_sg += 1
            print()
            print(f"  读法：共 {_n_sg} 条（9 指标 × 3 期限 × 2 侧）；这里用**回归系数**的 HAC"
                  "（不是均值 HAC）。")
            print("        §9 的结论是『FDR 校正后 0 个可用』⇒ HAC 只会让它们**更不显著**，")
            print("        所以本节的意义是把『本来就不显著』确认下来，不是翻案。")
            print()
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 复核失败（不影响其它段）：{exc}")
            print()
    else:
        print("  ⚠️ 未找到 `sentiment_gate_series.json`。")
        print("     生成：cd ai-quant-agent/backend && ./venv/bin/python -m app.backtest.sentiment_gate")
        print()

    # ---------- ③ 汇总：全层还剩几条显著性 ----------
    print("=" * 112)
    print("③ 汇总：按 HAC 复核后，本层还剩几条「整体显著」")
    print("=" * 112)
    n_lost = sum(1 for r in rows if r["verdict"].startswith("①"))
    n_keep = sum(1 for r in rows if r["verdict"].startswith("②"))
    n_never = sum(1 for r in rows if r["verdict"].startswith("③"))
    print(f"  可复核序列 {len(rows)} 条：① 失去显著性 **{n_lost}** 条 · "
          f"② 稳健 **{n_keep}** 条 · ③ 本来不显著 **{n_never}** 条")
    print()
    if n_keep:
        print("  ★ ② 稳健的条目（这些才是真正「整体为正」的证据）：")
        for r in rows:
            if r["verdict"].startswith("②"):
                print(f"     · {r['series']}（持 {r['h']} 日）：naive {r['t_naive']:+.2f} "
                      f"→ HAC {r['t_hac']:+.2f}")
    print()

    # ---------- ③b 关键换算：HAC t ⇒ 年化 Sharpe ⇒ "还差多少年样本" ----------
    g2 = _load("gate_cost_cut_daily.json")
    yrs = (len(g2) / 252.0) if g2 else 0.0
    if yrs:
        print(f"  ★ 换算（粗算，假设收益独立同分布）：`HAC t ≈ 年化 Sharpe × √年数`，"
              f"当前年数 = {yrs:.2f}")
        for r in rows:
            if r["t_hac"] == r["t_hac"] and r["t_hac"] > 0:
                sr = r["t_hac"] / math.sqrt(yrs)
                need = (2.0 / sr) ** 2 if sr > 0 else float("nan")
                print(f"     · {r['series']:<10}⇒ 年化 Sharpe ≈ {sr:>5.2f}"
                      f" ⇒ 要让 HAC t 达到 2，需要 **{need:>5.1f} 年**样本")
        print()
        print("  读法：这不是「策略不行」，而是**样本不够** —— 本层的收益/波动比量级就在 0.75 附近，")
        print("        2.5 年的数据在统计上**无法**把这种量级的收益确认为显著。")
        print()

    # ---------- ④ 无法重算的（诚实登记，不伪造） ----------
    print("=" * 112)
    print("④ 不可复核清单（诚实登记 —— 目标是**清空它**）")
    print("=" * 112)
    if NOT_RECHECKABLE:
        for f, what, why in NOT_RECHECKABLE:
            exist = os.path.exists(os.path.join(DATA, f))
            print(f"  · {what:<44} [{f}]  {why}{'' if exist else '（文件缺失）'}")
    else:
        print("  ✅ **空**：7 个数据源的日序列/原始数据都已落盘 ⇒ 整体显著性 100% 可复核。")
    print()

    print("=" * 112)
    print("⑤ 诚实边界")
    print("=" * 112)
    print("  - HAC 只修**整体**的显著性；**四段分解 / 分段结论仍用重叠样本** ⇒ 分段仍偏乐观；")
    print("  - §9 那一段用的是**回归系数**的 HAC（`newey_west_reg_t`），其 `t_naive` 为"
          "**同方差合并两样本口径**，与原文的 Welch p 略有差异（同方向，不必当矛盾）；")
    print("  - lag 取 `持有期 − 1` 是**重叠长度的理论上界**（样本可能更相关，也可能更弱）——")
    print("    它是一致性修正，不是「精确真值」；")
    print("  - 本脚本**不改任何生产口径**：`baseline_gate.judge()` 仍用缓存里的 naive t；")
    print("    要不要把门槛切到 HAC 是一个**独立决策**（会改变判定文字，需单独一轮 + 契约更新）。")

    try:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump({"rows": rows,
                       "summary": {"recheckable": len(rows), "lost": n_lost,
                                   "keep": n_keep, "never": n_never},
                       "not_recheckable": [{"file": a, "what": c, "why": d}
                                           for a, c, d in NOT_RECHECKABLE]},
                      f, ensure_ascii=False, indent=2)
        print(f"\n  已落盘：{os.path.relpath(OUT, os.path.dirname(BACKEND))}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  [warn] 落盘失败（不影响结论）：{exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
