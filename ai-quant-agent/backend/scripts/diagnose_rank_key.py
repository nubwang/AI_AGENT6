"""诊断：榜单的 `rank` 到底按什么排的？哪个键在**正确口径**下真的有效？

## 为什么先诊断再改（而不是直接"修 rank"）

Pool 诊断发现 `rank` 的三档是**非单调**的：

    rank 1-5 = −2.790% ｜ 6-15 = −1.807% ｜ 16-30 = −2.144%（日等权）

而 `up_probability` / `rank_score` 的分层又"看起来单调"——但那是**全局分位口径**，
换成**按日截面**（唯一可交易）后两者都变负（§11.8）。

**所以"rank 排序键坏了"这个结论还不能下**：必须先回答两个前置问题
——否则"修 rank"就是瞎猜：

    Q1 `rank` 是按哪个字段生成的？（与各打分的**日内序一致性**）
    Q2 在**按日截面 + 扣成本**口径下，`up_probability` / `rank_score` /
       `positive_score` / `negative_score` / `similarity` 里，**哪一个仍然单调有效**？

只有 Q2 有答案，才谈得上"把 rank 改成用 X 排"。

## 口径（吸取 §11.8 的教训）

    - 分层一律用**按日截面分位**（每天都要选票的可交易口径），**不用全局分位**；
    - 收益一律给"扣成本前后"两列（双边 0.50%）；
    - 单调性 = 5 组组均值的 Spearman（越接近 ±1 越单调，**符号才是方向**）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/diagnose_rank_key.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.backtest import sentiment_gate as SG  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICK_DIR = os.path.join(BACKEND, "data", "daily_recommend")
H = 5
MAX_ABS = 1.5
COST_RT = 0.005

CAND_KEYS = ("up_probability", "rank_score", "positive_score",
             "negative_score", "similarity", "rule_prob")
FIELDS = ("rank",) + CAND_KEYS


def load_picks() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(PICK_DIR, "daily_*.json")))
    latest: dict[tuple[str, bool], str] = {}
    for p in files:
        m = re.match(r"daily_(\d{8})(_agent)?(_v\d+)?\.json$", os.path.basename(p))
        if m:
            latest[(m.group(1), bool(m.group(2)))] = p
    rows: list[dict] = []
    for (date, is_agent), p in sorted(latest.items()):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for it in (d.get("top_picks") or []):
            r = {"date": str(d.get("date") or date).replace("-", "")[:8],
                 "is_agent": is_agent, "ts_code": str(it.get("ts_code") or ""),
                 "form_type": str(it.get("form_type") or "")}
            for f in FIELDS:
                r[f] = it.get(f)
            rows.append(r)
    df = pd.DataFrame(rows)
    for f in FIELDS:
        df[f] = pd.to_numeric(df[f], errors="coerce")
    return df


def attach_fwd(df: pd.DataFrame) -> pd.DataFrame:
    d0, d1 = str(df["date"].min()), str(df["date"].max())
    fwd = SG.build_panel(SG.HORIZONS, d0, d1, keep_stock=True)
    col = f"fwd{H}"
    out = df.merge(fwd[["trade_date", "ts_code", col]], how="left",
                   left_on=["date", "ts_code"], right_on=["trade_date", "ts_code"])
    out = out.rename(columns={col: "fwd5"})
    out.loc[out["fwd5"].abs() > MAX_ABS, "fwd5"] = np.nan
    out["fwd5_net"] = out["fwd5"] - COST_RT
    return out


def dmean(df: pd.DataFrame, col: str) -> float | None:
    v = pd.to_numeric(df[col], errors="coerce")
    k = v.notna()
    if not k.any():
        return None
    byd = v[k].groupby(df["date"][k]).mean()
    return round(float(byd.mean()), 5) if len(byd) else None


def group_a(ev: pd.DataFrame) -> None:
    print("=== A `rank` 是按哪个字段生成的？（日内序一致性）===")
    print(f"  {'字段':<18}{'可比天数':>10}{'逐日 Spearman 均值':>22}{'与 rank 严格同序的比例':>26}{'覆盖率':>10}")
    n_days_all = ev["date"].nunique()
    for f in CAND_KEYS:
        sps, same, nd = [], [], 0
        for _date, g in ev.groupby("date"):
            g2 = g.dropna(subset=["rank", f])
            if len(g2) < 5:
                continue
            nd += 1
            sp = g2["rank"].corr(g2[f], method="spearman")
            if pd.notna(sp):
                sps.append(float(sp))
            # rank 是否等于"按 f 降序的序号"（1-based）
            want = g2[f].rank(ascending=False, method="first")
            same.append(float((g2["rank"].astype(float) == want).mean()))
        cov = float(ev[f].notna().mean())
        a = f"{np.mean(sps):+.3f}" if sps else "—"
        b = f"{np.mean(same):.1%}" if same else "—"
        print(f"  {f:<18}{nd:>10}/{n_days_all:<8}{a:>22}{b:>26}{cov:>10.1%}")
    print("  判读：Spearman 接近 −1 且'严格同序'接近 100% 的字段 = rank 的来源键。")


def group_b(ev: pd.DataFrame) -> None:
    print()
    print("=== B 在**按日截面**口径下，哪个键仍单调有效？（5 组 + 扣成本）===")
    for f in CAND_KEYS:
        d = ev.dropna(subset=[f, "fwd5"]).copy()
        if len(d) < 50:
            print(f"  {f:<18} 样本不足（n={len(d)}），跳过")
            continue
        d["_q"] = d.groupby("date")[f].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False)
            if len(s) >= 5 else np.nan)
        d = d.dropna(subset=["_q"])
        if d.empty:
            print(f"  {f:<18} 分组失败，跳过")
            continue
        ms, mns, ns = [], [], []
        for q in sorted(d["_q"].unique()):
            sub = d[d["_q"] == q]
            ms.append(dmean(sub, "fwd5"))
            mns.append(dmean(sub, "fwd5_net"))
            ns.append(len(sub))
        rho = None
        if len(ms) >= 3 and all(m is not None for m in ms):
            rho = pd.Series(np.asarray(ms, dtype="float64")).corr(
                pd.Series(np.arange(len(ms))), method="spearman")
        c5 = f"{ms[4]:+.3%}" if (len(ms) > 4 and ms[4] is not None) else "—"
        n5 = f"{mns[4]:+.3%}" if (len(mns) > 4 and mns[4] is not None) else "—"
        r = f"{rho:+.3f}" if rho is not None else "—"
        verdict = ("Q5 扣成本后为正 且 单调>0" if (n5 != "—" and mns[4] is not None
                                                 and mns[4] > 0 and (rho or 0) > 0)
                   else "不达标")
        print(f"  {f:<18} n={len(d):<6} Q5均值 {c5:>9}  Q5扣成本 {n5:>9}  "
              f"单调 {r:>7}  → {verdict}")
    print("  ⚠️ 只有『Q5 扣成本后为正 **且** 单调为正』的键，才值得作为 rank 的排序依据。")


def group_c(ev: pd.DataFrame) -> None:
    print()
    print("=== C `rank` 的固定分档（不等量分档会误导判读，一并给出样本量）===")
    ev2 = ev.dropna(subset=["fwd5"]).copy()
    ev2["band"] = pd.cut(ev2["rank"], [0, 5, 10, 15, 20, 30, 40],
                         labels=["1-5", "6-10", "11-15", "16-20", "21-30", "31+"])
    rows = []
    for b in ["1-5", "6-10", "11-15", "16-20", "21-30", "31+"]:
        sub = ev2[ev2["band"] == b]
        if sub.empty:
            continue
        rows.append((b, len(sub), dmean(sub, "fwd5"), dmean(sub, "fwd5_net")))
    print(f"  {'档':<8}{'n':>8}{'均值(日等权)':>14}{'扣成本后':>14}")
    for b, n, m, mn in rows:
        c1 = f"{m:+.3%}" if m is not None else "—"
        c2 = f"{mn:+.3%}" if mn is not None else "—"
        print(f"  {b:<8}{n:>8}{c1:>14}{c2:>14}")
    print("  判读：若各档随 rank 下降而单调**变好或变差**，rank 才有排序意义；否则它就是噪声。")


def main() -> int:
    print("=" * 100)
    print("诊断：rank 的来源键 + 哪个键在正确口径下真的有效（plans/24 Pool 后续）")
    print("=" * 100)
    ev = attach_fwd(load_picks())
    ev = ev.dropna(subset=["fwd5"])
    print(f"样本：{len(ev)} 条 / {ev['date'].nunique()} 个可评估交易日（口径：按日截面 + 扣成本 {COST_RT:.2%}）")
    group_a(ev)
    group_b(ev)
    group_c(ev)
    print()
    print("=" * 100)
    print("结论用途：只有 B 组里『扣成本后 Q5 为正且单调为正』的键，才可作为 rank 的排序依据；")
    print("        否则『修 rank』就只是换一个噪声 —— 那不如不修。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
