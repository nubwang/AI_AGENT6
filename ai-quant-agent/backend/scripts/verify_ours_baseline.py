"""「我们自己」的基准口径落盘（plans/24 §11.21 —— §11.20.7 遗留项）

## 为什么需要它

§11.20 把 B1/B2 送进了门槛与影子字段，但**我们自己**没有 ——
因为 `baseline_gate.judge()` 需要 `by_seg`（四段收益），
而我们的候选池**从来没落盘过分段收益**。

结果是：**门槛只能判基准，判不了我们** —— 相当于体检报告里只有参考值、没有自己的指标。

## ★ 本脚本第一版就踩到一个真坑（已修正，这是本节第二个自我否证）

第一版把所有 `daily_*.json` 的 `top_picks` **直接等权**，得到"每日 75.5 只"。
实测发现 `top_picks` 的只数**极不稳定**：

    daily_20260810.json          → **476 只**   （早期的**候选池**，未截断）
    daily_20260819_v1.json       → **30 只**    （已按 `daily_top_k` 截断）
    daily_*_agent_v1.json        → 1~10 只      （Agent 精筛）

⇒ 把两类混在一起平均，得到的**既不是候选池也不是推荐榜**，是个无意义的混合体。
（顺带说明：§11.12 的 `load_picks_same_window()` 用的是**同一混合口径**，
所以它的 −2.934% 也是"候选池 + 推荐榜"的混合物 —— 结论方向不受影响，
但**口径名不副实**，这一点在 §11.21 更正。）

所以本脚本算**两条口径**并强制分开命名：

    ours_top30  = 规则榜按 `rank` 取前 30 + Agent 榜全部  ⇒ **实盘口径**（对齐 daily_top_k=30）
    ours_all    = 不截断                                  ⇒ **候选池口径**（与 §11.12 可对照）

## 口径（与 §11.16 完全一致）

    - 收益 = `sentiment_gate.build_panel`（**T+1 开盘买入**），持有 h ∈ {5, 10, 20} 日
    - 一律**日等权**（每天组合收益再对天平均，**不按记录计数**）
    - 一律扣**双边 0.50%**
    - `|fwd| > 1.5` 剔除（新股 / 数据异常）
    - 同窗口对照：同一批日期上的**全市场等权（= B1 口径）**，用于严格同日比较

## ★ 最重要的产物不是数字，而是一个**否定性结论**

我们的推荐记录只有 **2026-08 ~ 2026-09**（约 20 个交易日），
而 §11.17 的门槛要求**四段全正**（2024 / 2025H1 / 2025H2 / 2026）。

⇒ 我们**缺 2024 / 2025 三段** ⇒ 门槛对我们**不可判定（`not_decidable`）**，
**不是"判定为差"**。把"没有数据"当成"证据"，是这一轮必须挡住的错误 ——
所以本脚本用新增的 `baseline_gate.coverage()` 把这两件事**显式分开**。

## 为什么不去"重建历史选股"凑满四段

唯一办法是"用今天的规则/参数回测 2024–2025"。
但那**不是"我们当时的推荐"**，而是"今天参数的假设性表现" ——
在参数会漂移（存在参数版本指纹机制）的前提下，那等于**制造一个新的未来函数**。
所以本脚本**只算真实记录**，历史段**宁缺不造**。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_ours_baseline.py
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.backtest import baseline_gate as BG  # noqa: E402
from app.backtest import sentiment_gate as SG  # noqa: E402
from app.backtest.stats_correction import t_compare  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICK_DIR = os.path.join(BACKEND, "data", "daily_recommend")
CACHE = os.path.join(BACKEND, "data", "ours_baseline_daily.json")
# 日序列**另存**（不写进 CACHE：baseline_gate.load_ours() 依赖它的 list 结构）
# —— 供 §11.28 的 HAC 复核复用
SERIES_OUT = os.path.join(BACKEND, "data", "ours_baseline_series.json")
HORIZONS = (5, 10, 20)
FEE = 0.005
MAX_ABS = 1.5
TOP_N = BG.TOP_N                       # 30，与 daily_top_k 默认值一致
SEGS = BG.SEGMENTS
SEG_RANGE = {"2024": ("20240101", "20241231"),
             "2025H1": ("20250101", "20250630"),
             "2025H2": ("20250701", "20251231"),
             "2026": ("20260101", "20261231")}
FILE_RE = re.compile(r"daily_(\d{8})(_agent)?(_v\d+)?\.json$")
# 口径 key → (源线, 截断只数 or None)
LINE_MIX: dict[str, tuple[tuple[str, int | None], ...]] = {
    "top30": (("rules", TOP_N), ("agent", None)),
    "all": (("rules", None), ("agent", None)),
    "rules_top30": (("rules", TOP_N),),
    "agent": (("agent", None),),
}
LINE_NAME = {"rules": "规则扫描榜", "agent": "Agent 精筛榜"}
BIG_PICKS = 100                        # 单日只数 > 此值 ⇒ 判定为"未截断的候选池"


def _t_of(s: pd.Series) -> float:
    x = pd.to_numeric(s, errors="coerce").dropna().to_numpy(dtype="float64")
    if x.size < 10:
        return float("nan")
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return float("nan")
    return float(x.mean() / (sd / math.sqrt(x.size)))


def load_picks() -> tuple[pd.DataFrame, dict]:
    """扫描真实推荐记录：**每日每线各取最新一份**（与 §11.12 同源逻辑）。"""
    files = sorted(glob.glob(os.path.join(PICK_DIR, "daily_*.json")))
    latest: dict[tuple[str, bool], str] = {}
    for p in files:
        m = FILE_RE.match(os.path.basename(p))
        if m:
            latest[(m.group(1), bool(m.group(2)))] = p
    rows: list[dict] = []
    bad: list[str] = []
    for (fdate, is_agent), p in sorted(latest.items()):
        base = os.path.basename(p)
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{base}: {exc}")
            continue
        jd = str(d.get("date") or fdate).replace("-", "")[:8]
        for it in (d.get("top_picks") or []):
            code = str(it.get("ts_code") or "").strip()
            if not code:
                continue
            try:
                rk = int(it.get("rank") or 0)
            except (TypeError, ValueError):
                rk = 0
            rows.append({"date": jd, "filedate": fdate, "file": base, "ts_code": code,
                         "line": "agent" if is_agent else "rules",
                         "rank": rk if rk > 0 else 9999})
    meta = {"files": len(files), "used": len(latest), "bad": bad}
    return pd.DataFrame(rows), meta


def _mix(pk: pd.DataFrame, key: str) -> pd.DataFrame:
    """按口径取子集：`cap` 非空时按 rank 取前 cap（当日文件内）。"""
    parts: list[pd.DataFrame] = []
    for ln, cap in LINE_MIX[key]:
        s = pk[pk["line"] == ln]
        if s.empty:
            continue
        if cap is not None:
            s = s[s["rank"] <= cap]
        if not s.empty:
            parts.append(s)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def evaluate(pk: pd.DataFrame) -> tuple[list[dict], list[dict], dict]:
    d0, d1 = str(pk["date"].min()), str(pk["date"].max())
    panel = SG.build_panel(HORIZONS, d0, d1, keep_stock=True)
    agg = SG.build_panel(HORIZONS, d0, d1, keep_stock=False)
    recs: list[dict] = []
    detail: list[dict] = []
    series: dict[str, dict[str, float]] = {}
    for key in LINE_MIX:
        sub = _mix(pk, key)
        if sub.empty:
            continue
        for h in HORIZONS:
            col = f"fwd{h}"
            if col not in panel.columns:
                continue
            m = sub.merge(panel[["trade_date", "ts_code", col]], how="left",
                          left_on=["date", "ts_code"],
                          right_on=["trade_date", "ts_code"])
            m[col] = pd.to_numeric(m[col], errors="coerce")
            m.loc[m[col].abs() > MAX_ABS, col] = np.nan
            m = m.dropna(subset=[col])
            if m.empty:
                continue
            byd = m.groupby("date")[col].mean().sort_index()
            days = [str(x) for x in byd.index]
            seg: dict[str, float | None] = {}
            seg_days: dict[str, int] = {}
            for nm in SEGS:
                a, b = SEG_RANGE[nm]
                part = byd[(byd.index >= a) & (byd.index <= b)]
                seg_days[nm] = int(len(part))
                seg[nm] = (float(part.mean()) if len(part) else None)
            b1 = None
            if col in agg.columns:
                s = agg[col].reindex(days).dropna()
                b1 = (float(s.mean()) if len(s) else None)
            gross = float(byd.mean())
            tc = t_compare(byd.to_numpy(dtype="float64"), h - 1)
            series[f"h{h}_ours_{key}"] = {str(d): float(v) for d, v in byd.items()}
            recs.append({
                "h": h, "kind": f"ours_{key}", "label": BG.OURS_LABEL[f"ours_{key}"],
                "gross": gross, "net": gross - FEE,
                "net_per_year": (gross - FEE) * (252.0 / h),
                "n": int(len(m)), "n_days": int(len(byd)),
                "n_picks_per_day": round(float(len(m) / max(len(byd), 1)), 1),
                "date_from": days[0], "date_to": days[-1],
                "t": tc["t_naive"], "t_hac": tc["t_hac"],
                "by_seg": seg, "seg_days": seg_days,
                "b1_same_window": b1,
                "excess_vs_b1": (gross - b1) if b1 is not None else None,
            })
            if h == 20:
                for d, v in byd.items():
                    detail.append({"kind": key, "date": str(d), "ret": float(v),
                                   "n_picks": int((m["date"] == d).sum())})
    return recs, detail, series


def _pct(v, nd: int = 2) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return "nan" if math.isnan(f) else f"{f * 100:+.{nd}f}%"


def _num(v, nd: int = 2) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return "nan" if math.isnan(f) else f"{f:+.{nd}f}"


def main() -> int:
    t0 = time.time()
    print("=" * 108)
    print("「我们自己」的基准口径落盘（plans/24 §11.21）—— 让门槛能判我们，而不只是判基准")
    print("=" * 108)
    print(f"  口径：T+1 开盘买入 · 日等权 · 扣双边 {FEE:.2%} · |fwd|>{MAX_ABS} 剔除")
    print("  数据：真实记录 data/daily_recommend/daily_*.json（每日每线取最新一份）")
    print()

    pk, meta = load_picks()
    if pk.empty:
        print("  ❌ 没读到任何推荐记录（data/daily_recommend 为空？）")
        return 1
    print(f"  推荐文件：{meta['files']} 个，去重后使用 {meta['used']} 个"
          f"（不良 {len(meta['bad'])} 个）")
    for b in meta["bad"]:
        print(f"    ⚠ {b}")
    print(f"  记录：{len(pk):,} 条 · 代码 {pk['ts_code'].nunique()} 个 · "
          f"日期 {pk['date'].min()} ~ {pk['date'].max()}（{pk['date'].nunique()} 个交易日）")
    per = pk.groupby(["date", "line"]).size()
    for ln in ("rules", "agent"):
        s = per[per.index.get_level_values("line") == ln]
        if s.empty:
            continue
        print(f"    - {LINE_NAME[ln]:<12}: {int(s.sum()):>5} 条 / {len(s)} 天 / "
              f"每日 {s.mean():.1f} 只（最少 {int(s.min())}、最多 {int(s.max())}）")
    big = per[per > BIG_PICKS]
    if not big.empty:
        print(f"    ⚠ **{len(big)} 个(日期,线) 的只数 > {BIG_PICKS}** —— 这些是"
              f"**未截断的候选池**，与已截断的推荐榜混算会得到无意义的口径：")
        big_rows = big.reset_index()
        big_rows.columns = ["date", "line", "n"]
        for _, br in big_rows.head(8).iterrows():
            d, ln, n = str(br["date"]), str(br["line"]), int(br["n"])
            lab = LINE_NAME.get(ln, ln)
            print(f"        {d} {lab}: {n} 只")
        print("      ⇒ 本脚本因此分成两套口径（下方 ① 里并列展示）。")
    print()

    recs, detail, series = evaluate(pk)
    if not recs:
        print("  ❌ 面板合并后无有效样本（行情数据缺该区间？）")
        return 1

    print("=" * 108)
    print("① 结果（两套口径 × 三个持有期；毛/期 · 扣成本/期 · 扣成本年化 · t · 同窗 B1）")
    print("=" * 108)
    hd = (f"{'口径':<30}{'持有':>5}{'毛/期':>10}{'净/期':>10}{'净/年':>10}"
          f"{'t':>8}{'t_HAC':>8}{'天数':>5}{'每日':>6}{'同窗B1/期':>11}{'超额/期':>10}")
    print(hd)
    print("-" * len(hd))
    for key in LINE_MIX:
        for h in HORIZONS:
            r = next((x for x in recs if x["kind"] == f"ours_{key}" and x["h"] == h), None)
            if r is None:
                continue
            print(f"{r['label'][:28]:<30}{h:>5}{_pct(r['gross'], 3):>10}"
                  f"{_pct(r['net'], 3):>10}{_pct(r['net_per_year'], 1):>10}"
                  f"{_num(r['t']):>8}{_num(r.get('t_hac')):>8}"
                  f"{r['n_days']:>5}{r['n_picks_per_day']:>6.1f}"
                  f"{_pct(r['b1_same_window'], 3):>11}{_pct(r['excess_vs_b1'], 3):>10}")
    print("  注：t 需要 ≥10 个交易日才计算，故持 20 日的 t 多为 nan（**不是 0**）。")
    print("  ⚠️ `t` **未修正重叠**：持 h 日的逐日滚动收益相邻样本共享 h−1 天 ⇒")
    print("     **判读请看 `t_HAC`**（Newey-West, lag=h−1；plans/24 §11.28）。")
    print()

    if series:
        try:
            with open(SERIES_OUT, "w", encoding="utf-8") as f:
                json.dump({"series": series}, f, ensure_ascii=False)
            print(f"  ✅ 已落盘日序列 → {os.path.relpath(SERIES_OUT, os.getcwd())}"
                  f"（{len(series)} 条；供 §11.28 的 HAC 复核复用）")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 日序列落盘失败：{e}")

    payload = recs
    try:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"  ✅ 已落盘 {os.path.relpath(CACHE, os.getcwd())}（{len(payload)} 条）")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 落盘失败：{exc}")
        return 1
    ours = BG.load_ours(20)
    print(f"  ✅ 回读校验：load_ours(20) 读到 {len(ours)} 条 → {sorted(ours.keys())}")
    print()

    print("=" * 108)
    print("② 分段分解（§11.13 的教训：不给分段就等于没有结论）")
    print("=" * 108)
    seg_hd = f"{'口径':<30}{'持有':>5}" + "".join(f"{s:>12}" for s in SEGS) + f"{'段天数':>18}"
    print(seg_hd)
    print("-" * len(seg_hd))
    for key in LINE_MIX:
        for h in (20,):
            r = next((x for x in recs if x["kind"] == f"ours_{key}" and x["h"] == h), None)
            if r is None:
                continue
            cells = "".join(f"{_pct(r['by_seg'].get(s), 2):>12}" for s in SEGS)
            dsc = "".join(f"{int(r['seg_days'].get(s, 0)):>6}" for s in SEGS)
            print(f"{r['label'][:28]:<30}{h:>5}{cells}{dsc:>18}")
    print()

    print("=" * 108)
    print("③ ★ 门槛可判定性（本轮新增的方法论护栏：区分「不通过」与「不可判定」）")
    print("=" * 108)
    for kind in ("ours_top30", "ours_all", "ours_rules_top30", "ours_agent"):
        c = ours.get(kind)
        if c is None:
            continue
        cov = BG.coverage(c)
        miss = "、".join(cov["missing"]) if cov["missing"] else "无"
        verdict = "可判定" if cov["decidable"] else "**不可判定（not_decidable）**"
        print(f"  {c.name[:32]:<34} 有 {cov['n_have']}/{cov['n_total']} 段"
              f"（缺 {miss}）⇒ {verdict}")
    print()
    print("  判读（这一条比数字重要）：")
    print("    - 我们的推荐记录只有 2026-08~09 ⇒ **缺 2024 / 2025 三段**；")
    print("    - §11.17 的门槛要求四段全正 ⇒ 我们是**没有资格被判定**，不是「判定为差」；")
    print("    - 若直接调 judge() 会得到 reject（挂在 d），但那是**缺段所致**；")
    print("      把它读成「我们的体系很差」就是**拿没有数据当证据** —— 这正是要挡住的错误；")
    print("    - 想凑满四段只有一条路：用今天的参数回测 2024–2025，")
    print("      但那不是「我们当时的推荐」，而是**新的未来函数**，故本脚本不造。")
    print()

    print("=" * 108)
    print("④ 逐日明细（持 20 日；供人工核对是否有单日极端值在撑总收益）")
    print("=" * 108)
    for key in ("top30", "all", "agent"):
        rows = sorted([x for x in detail if x["kind"] == key], key=lambda x: x["date"])
        if not rows:
            continue
        lab = BG.OURS_LABEL.get(f"ours_{key}", key)
        print(f"  [{lab}]")
        for x in rows:
            print(f"    {x['date']}  {x['n_picks']:>4} 只  {_pct(x['ret'], 3)}")
    print()

    print(f"  用时 {time.time() - t0:.1f}s")
    print("  下一步：baseline_shadow 已可直接读它（影子字段里会多出「我们」这一段）；")
    print("         但「切权重」仍被 §11.20.7 的四条件挡着，且我们**尚不可判定**。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
