"""诊断：解剖我们的候选池，定位 5.2pp 缺口出在哪一层（plans/24 Pool）

## 背景（为什么做这个）

H2 实测：同期（近 30 日）全市场等权 fwd5 = −0.353%，
而我们的推荐样本 = **−5.55%** —— 缺口约 **5.2 个百分点**，且**择时解释不了**。
H3 随后证明：**16 个个股博弈特征都无法解释这个缺口**（核心特征 q 0.29~0.89）。

推论的必然方向：**问题在"候选池是怎么被选出来的"，而不是"缺什么特征"**。

## 本脚本做什么

`data/daily_recommend/` 里每天留了链路各层的产物，因此可以**逐层对比**：

    total_scanned（全市场扫描）
      → candidate_pool（形态粗筛）
        → confirmed_count（形态确认）
          → top_picks 30 只（条件概率打分排序）
            → [Agent 精筛] → top_picks 4 只

对每一层的清单算 **T+1 开盘买入、持有 5 日的收益**（与 H2/H3 同口径），
看**哪一层开始系统性变差**，并回答三个具体问题：

  Q1 30 只榜单 vs Agent 精筛 4 只，谁更好？（**精筛是在救人还是在毁结果**）
  Q2 `form_type`（A/B/C/D/E/U）哪个形态在拖累？
  Q3 **打分是否有效**：`up_probability` / `rank_score` 越高，实际收益越好吗？
     —— 若无关或反向，说明**打分体系本身失效**，那才是真正的病根。

## 口径

    - 前向收益复用 H2 的 `build_panel(keep_stock=True)`（同一段代码，口径统一）；
    - 同一天有多个版本（v1/v2/v3）→ 每个 (日期, 类型) 取**最后版本**；
    - 最后 5 个交易日算不出 fwd5 → 自动排除（样本是"可完整评估"的那部分）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/diagnose_pick_pool.py
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


def _load_latest() -> pd.DataFrame:
    """读所有榜单，每个 (日期, 是否 agent) 只保留最后版本 → 长表。"""
    rows: list[dict] = []
    files = sorted(glob.glob(os.path.join(PICK_DIR, "daily_*.json")))
    latest: dict[tuple[str, bool], str] = {}
    for p in files:
        b = os.path.basename(p)
        m = re.match(r"daily_(\d{8})(_agent)?(_v\d+)?\.json$", b)
        if not m:
            continue
        date, is_agent = m.group(1), bool(m.group(2))
        latest[(date, is_agent)] = p          # 字典序递增 → 最后即最新版本
    for (date, is_agent), p in sorted(latest.items()):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        cp = d.get("candidate_pool")
        meta = {
            "file": os.path.basename(p), "date": str(d.get("date") or date),
            "is_agent": is_agent,
            "total_scanned": d.get("total_scanned"),
            "confirmed_count": d.get("confirmed_count"),
            "pool_n": len(cp) if isinstance(cp, list) else cp,
        }
        for k in ("top_picks",) :
            for it in (d.get(k) or []):
                r = dict(meta)
                r["rank"] = it.get("rank")
                r["ts_code"] = str(it.get("ts_code") or "")
                r["form_type"] = str(it.get("form_type") or "")
                r["up_probability"] = it.get("up_probability")
                r["rank_score"] = it.get("rank_score")
                r["risk_level"] = it.get("risk_level")
                r["n_hit_conditions"] = len(it.get("hit_conditions") or [])
                r["n_hit_rules"] = len(it.get("hit_rules") or [])
                rows.append(r)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in ("up_probability", "rank_score", "rank", "pool_n", "confirmed_count",
              "total_scanned"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = df["date"].astype(str).str.replace("-", "", regex=False)
    return df


def _attach_fwd(df: pd.DataFrame) -> pd.DataFrame:
    """挂上 fwd5（复用 H2 的同一份口径）。"""
    if df.empty:
        return df
    d0, d1 = str(df["date"].min()), str(df["date"].max())
    fwd = SG.build_panel(SG.HORIZONS, d0, d1, keep_stock=True)
    col = f"fwd{H}"
    out = df.merge(fwd[["trade_date", "ts_code", col]], how="left",
                   left_on=["date", "ts_code"], right_on=["trade_date", "ts_code"])
    out = out.drop(columns=[c for c in ("trade_date",) if c in out.columns])
    out = out.rename(columns={col: "fwd5"})
    out.loc[out["fwd5"].abs() > MAX_ABS, "fwd5"] = np.nan
    return out


def _stat(x: pd.Series, dates: pd.Series | None = None) -> dict:
    """同时给两个均值 —— 这两者**必须一起看**：

      - mean      ：**每条记录等权**（简单均值）
      - mean_daily：**每日先算均值、再对日等权**（与 H2/H3 同口径）

    两者差异 = "权重偏置"的大小。实测 B 形态：整体 +6.637% 但日均 **−1.318%** ——
    说明它的"优势"只是**重仓在少数好日子**造成的假象（只有 9 天数据、正收益 3/9）。
    """
    v = pd.to_numeric(x, errors="coerce")
    if dates is None:
        dd = pd.Series(["_"] * len(v), index=v.index)
    else:
        dd = dates.astype(str)
    keep = v.notna()
    v, dd = v[keep], dd[keep]
    if v.empty:
        return {"n": 0, "mean": None, "mean_daily": None, "median": None, "win": None}
    byd = v.groupby(dd).mean()
    return {"n": int(len(v)), "mean": round(float(v.mean()), 5),
            "mean_daily": (round(float(byd.mean()), 5) if len(byd) else None),
            "median": round(float(v.median()), 5),
            "win": round(float((v > 0).mean()), 4)}


def _table(title: str, df: pd.DataFrame, by: str) -> None:
    print()
    print(f"--- {title} ---")
    hdr = (f"{'分组':<22}{'n':>6}{'均值(条)':>11}{'均值(日等权)':>14}"
           f"{'中位':>10}{'胜率':>8}")
    print(hdr)
    print("-" * len(hdr))
    keys = sorted(df[by].dropna().unique(), key=lambda x: str(x))
    for k in keys:
        sub = df[df[by] == k]
        s = _stat(sub["fwd5"], sub["date"])
        # 先算成局部字符串：避免 f-string 里嵌套同类引号（Python 3.10 会语法错误）
        c1 = f"{s['mean']:+.3%}" if s["mean"] is not None else "—"
        c4 = f"{s['mean_daily']:+.3%}" if s["mean_daily"] is not None else "—"
        c2 = f"{s['median']:+.3%}" if s["median"] is not None else "—"
        c3 = f"{s['win']:.1%}" if s["win"] is not None else "—"
        print(f"{str(k):<22}{s['n']:>6}{c1:>11}{c4:>14}{c2:>10}{c3:>8}")


def main() -> int:
    print("=" * 92)
    print("候选池解剖（plans/24 Pool）—— 逐层 fwd5 对比，定位 5.2pp 缺口")
    print("=" * 92)
    picks = _load_latest()
    if picks.empty:
        print("未读到榜单")
        return 1
    picks = _attach_fwd(picks)
    ev = picks.dropna(subset=["fwd5"])
    print(f"榜单记录 {len(picks)} 条（{picks['date'].nunique()} 个日期），"
          f"其中**可完整评估**（有 fwd5）{len(ev)} 条 / {ev['date'].nunique()} 个日期")
    print(f"日期范围 {picks['date'].min()} ~ {picks['date'].max()}"
          f"（最后 {H} 个交易日无 fwd5，自动排除）")

    base = _stat(ev["fwd5"], ev["date"])
    b1 = f"{base['mean']:+.3%}" if base["mean"] is not None else "—"
    b2 = f"{base['mean_daily']:+.3%}" if base["mean_daily"] is not None else "—"
    print()
    print(f"★ 候选池整体 fwd5：均值(条) {b1}｜均值(日等权) {b2}｜"
          f"中位 {base['median']:+.3%}｜胜率 {base['win']:.1%}（n={base['n']}）")
    print(f"  对照 H2 结论：同期全市场 ≈ −0.35% ～ +0.31%（两个窗口，见 §10.5）")

    # Q1 30 只 vs Agent 精筛
    _table("Q1 30 只榜单 vs Agent 精筛 4 只", ev, "is_agent")

    # 逐层：池子规模
    print()
    print("--- 链路各层的规模（用于确认『哪一层』在缩）---")
    for c in ("total_scanned", "pool_n", "confirmed_count"):
        if c in ev.columns:
            v = pd.to_numeric(ev[c], errors="coerce").dropna()
            if not v.empty:
                print(f"  {c:<18} 中位 {v.median():>8.0f}  最小 {v.min():>8.0f}")

    # Q2 形态
    _table("Q2 按 form_type", ev, "form_type")

    # Q3 打分有效性 —— 最关键
    print()
    print("--- Q3 打分有效性（**最关键**：打分越高，实际收益越好吗？）---")
    for col in ("up_probability", "rank_score"):
        if col not in ev.columns:
            continue
        d = ev.dropna(subset=[col]).copy()
        if len(d) < 20:
            continue
        # ⚠️ 必须**按日截面**分层（= 可交易口径：每天都要选票）。
        #    若用**全局分位**，会把"高概率集中的那些天"整体算进 Q5，
        #    造成天数权重偏置 —— 实测同一批数据：
        #        全局 Q5    日等权 +0.636%
        #        按日截面 Q5 日等权 **−1.629%**（中位 −1.429%，正收益天仅 7/22）
        #    结论完全相反。**全局分位在这一步是不可用的口径。**
        d["_q"] = d.groupby("date")[col].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False)
            if len(s) >= 5 else np.nan)
        d = d.dropna(subset=["_q"])
        if d.empty:
            continue
        print()
        print(f"  按 {col} 分 5 组：")
        for q in sorted(d["_q"].dropna().unique()):
            sub = d[d["_q"] == q]
            s = _stat(sub["fwd5"], sub["date"])
            rng = f"[{sub[col].min():.3f}, {sub[col].max():.3f}]"
            c1 = f"{s['mean']:+.3%}" if s["mean"] is not None else "—"
            c4 = f"{s['mean_daily']:+.3%}" if s["mean_daily"] is not None else "—"
            c3 = f"{s['win']:.1%}" if s["win"] is not None else "—"
            print(f"    Q{int(q) + 1} {rng:<22} n={s['n']:>4}"
                  f"  均值(条) {c1:>9}  均值(日) {c4:>9}  胜率 {c3:>6}")
        # 单调性：组序号 vs 组均值的 Spearman
        gm = d.groupby("_q")["fwd5"].mean()
        if len(gm) >= 3:
            rho = pd.Series(gm.to_numpy()).corr(pd.Series(np.arange(len(gm))),
                                               method="spearman")
            print(f"    → 单调性 Spearman = {rho:+.3f}"
                  f"（{'正向有效' if rho > 0.5 else ('反向！' if rho < -0.5 else '无单调关系')}）")

    # rank 分层
    _table("附：按 rank 分层（1-5 / 6-15 / 16-30）", ev.assign(
        rank_band=pd.cut(ev["rank"], [0, 5, 15, 40],
                         labels=["1-5", "6-15", "16-30"])), "rank_band")

    # ── Q4 交叉：form_type × 同日打分档（最终策略候选）──
    print()
    print("--- Q4 form_type × 同日打分档 交叉（均值(n)）---")
    dd = ev.dropna(subset=["up_probability", "form_type"]).copy()
    dd["_pq"] = dd.groupby("date")["up_probability"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False)
        if len(s) >= 5 else np.nan)
    dd = dd.dropna(subset=["_pq"])
    if not dd.empty:
        qcols = sorted(int(q) for q in dd["_pq"].unique())
        hdr2 = f"{'形态':<6}" + "".join(f"{('Q' + str(c + 1)):>18}" for c in qcols)
        print(hdr2)
        print("-" * len(hdr2))
        for ft in sorted(dd["form_type"].unique()):
            row = f"{ft:<6}"
            for q in sorted(dd["_pq"].unique()):
                sub = dd[(dd["form_type"] == ft) & (dd["_pq"] == q)]
                if sub.empty:
                    row += f"{'—':>18}"
                else:
                    cell = f"{sub['fwd5'].mean():+.2%}({len(sub)})"
                    row += f"{cell:>18}"
            print(row)

    # ── Q5 稳健性：某形态的优势是不是只来自少数几天？──
    print()
    print("--- Q5 各形态的日期稳健性（优势会不会只来自少数几天？）---")
    for ft in sorted(ev["form_type"].unique()):
        sub = ev[ev["form_type"] == ft]
        if sub.empty or len(sub) < 20:
            continue
        byd = sub.groupby("date")["fwd5"].agg(["size", "mean"])
        oth = ev[ev["form_type"] != ft].groupby("date")["fwd5"].mean()
        diff = (byd["mean"] - oth.reindex(byd.index)).dropna()
        n_win = int((diff > 0).sum())
        print(f"  {ft}: 出现 {len(byd)} 天｜整体均值 {sub['fwd5'].mean():+.3%}"
              f"｜**日均** {byd['mean'].mean():+.3%}｜"
              f"跑赢同日其他形态 {n_win}/{len(diff)} 天"
              f"（{n_win / max(1, len(diff)):.0%}）｜正收益天 {int((byd['mean'] > 0).sum())}/{len(byd)}")
    print("  判读：若某形态『跑赢同日其他』的比例接近 50% → 它的优势只是少数几天的运气。")

    # ── Q6 收窄模拟：只取"高分档 + 特定形态"会变成什么样 ──
    print()
    print("--- Q6 收窄模拟（把池子收窄到『高质量子集』会怎样）---")
    if not dd.empty:
        cands = [
            ("全部（现状基线）", ev),
            ("只取打分最高档 Q5", ev[ev.index.isin(dd[dd["_pq"] == 4].index)]),
            ("只取形态 B", ev[ev["form_type"] == "B"]),
            ("形态 B 且打分 Q4-Q5",
             ev[ev.index.isin(dd[(dd["form_type"] == "B") & (dd["_pq"] >= 3)].index)]),
            ("剔除 A 与 D（保留 B/C/E/U）",
             ev[~ev["form_type"].isin(["A", "D"])]),
            ("剔除 A 与 D 且打分 Q5",
             ev[ev.index.isin(dd[(~dd["form_type"].isin(["A", "D"]))
                                 & (dd["_pq"] == 4)].index)]),
        ]
        hdr3 = (f"{'子集':<30}{'n':>6}{'均值(条)':>11}{'均值(日等权)':>14}"
                f"{'中位':>10}{'胜率':>8}")
        print(hdr3)
        print("-" * len(hdr3))
        for name, sdf in cands:
            s = (_stat(sdf["fwd5"], sdf["date"]) if not sdf.empty
                 else {"n": 0, "mean": None, "mean_daily": None,
                       "median": None, "win": None})
            c1 = f"{s['mean']:+.3%}" if s["mean"] is not None else "—"
            c4 = f"{s['mean_daily']:+.3%}" if s["mean_daily"] is not None else "—"
            c2 = f"{s['median']:+.3%}" if s["median"] is not None else "—"
            c3 = f"{s['win']:.1%}" if s["win"] is not None else "—"
            print(f"{name:<30}{s['n']:>6}{c1:>11}{c4:>14}{c2:>10}{c3:>8}")
        print("  ⚠️ 这是**样本内**模拟，不是样本外验证：只用于定位病根，不能当策略上线。")

    print()
    print("=" * 92)
    print("判读要点")
    print("  1) 若 Agent 精筛比 30 只更差 → **精筛在毁结果**（应关掉或重构）；")
    print("  2) 若某 form_type 明显拖累 → 该形态应单独审查（而非整体调参）；")
    print("  3) 若打分与收益**无关或反向** → **打分体系失效**，这是真正的病根，")
    print("     此时任何『改纪律/改目标位』的打磨都无意义；")
    print("  4) 本窗口仅约 25 个可评估交易日、样本 <1000 → **只看方向与量级**，不下显著性结论。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
