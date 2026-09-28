"""门控择时的**真实换手回放**（plans/24 §11.27）

## 为什么必须做这一步

§11.26 判定「顺势做反转」时，成本用了**两套口径**，得到的是一个**宽到无法判定**的区间：

    写法 A（成本按"每期都换仓"扣）            净年化 −0.1%
    写法 B（成本按 ON 天数占比缩放）          净年化 +8.3%

两个数字差了 8.4pp —— 这不是"稳健性区间"，而是**口径没定**。
本轮把这件事**数出来**：门的状态序列到底产生多少次建仓/换仓。

## 三个口径的定义（区别只在"哪些期该扣费"）

    毛收益（三口径相同）：OFF 期持现金 ⇒ 收益记 0 ⇒ 毛/期 = mean(ON 期的 base20) × ON 占比

    A  保守：**不管 ON 还是 OFF，每个 20 日周期都换一批股**   ⇒ 换手 = 全部期数（12.6 次/年）
    D2 中性：**只在 ON 的周期建仓/换股，OFF 期空仓不换**     ⇒ 换手 = ON 期数
    D3 乐观：**ON 期间只要门不转弱就一直持有同一批股**       ⇒ 换手 = ON 段数

    ★ 真实执行最接近 **D2**：等权组合需定期再平衡，20 日换一批股是合理频率；
      D3 的"永不老化"假设不现实，本轮**只用来量化门的状态黏性**，不作主口径。
    ⚠️ 反过来说：**A 之所以"保守"，是因为它给空仓期也算了换仓费** ——
      数学上它确实是净收益下界，但**它不是对真实成本的估计**。

## 第二个必须一起报的东西：**样本重叠**（这是比成本更要紧的问题）

`base20` 是**逐日滚动**的 20 日收益 ⇒ 相邻样本共享 19 天 ⇒ naive t 值被**系统性放大**。
本轮同时给出：

    · naive t（现在各节用的口径）
    · **Newey-West HAC t**（Bartlett 核，lag = HOLD−1 = 19）
    · **不重叠周期口径的期数**（≈ 653/20 = 32 期）—— 这才是 t 检验真正拥有的独立样本量

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_turnover_replay.py
"""
from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.backtest.baseline_gate import (  # noqa: E402
    Candidate, FEE_DEFAULT, default_slip_bp, fmt_judge, judge_default,
)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_CACHE = os.path.join(BACKEND, "data", "regime_gate_daily.json")
COST_CACHE = os.path.join(BACKEND, "data", "gate_cost_cut_daily.json")
OUT = os.path.join(BACKEND, "data", "turnover_replay.json")
HOLD = 20
PER_YEAR = 252.0 / HOLD            # 12.6 期/年（逐日滚动口径）
SEGS = (("2024", "20240101", "20241231"),
        ("2025H1", "20250101", "20250630"),
        ("2025H2", "20250701", "20251231"),
        ("2026", "20260101", "20261231"))
ROLL_W, ROLL_MIN, HI_Q = 252, 60, 0.70


def build() -> pd.DataFrame:
    """合并两份缓存（与 §11.26 同一口径，避免 `_x/_y` 后缀）。"""
    with open(GATE_CACHE, encoding="utf-8") as f:
        g = json.load(f)
    with open(COST_CACHE, encoding="utf-8") as f:
        c = json.load(f)
    dg, dc = pd.DataFrame(g), pd.DataFrame(c)
    keep_g = ["trade_date", "mkt_mom60", "s_lb2", "base5"]
    keep_c = ["trade_date", "base20", "combo20"]
    df = dg[[x for x in keep_g if x in dg.columns]].merge(
        dc[[x for x in keep_c if x in dc.columns]], on="trade_date", how="inner")
    df["trade_date"] = df["trade_date"].astype(str)
    return df.sort_values("trade_date").reset_index(drop=True)


def rpct(s: pd.Series) -> pd.Series:
    """滚动分位（与 §11.14/§11.26 同口径：252 日窗、至少 60 点）。"""
    return s.rolling(ROLL_W, min_periods=ROLL_MIN).rank(pct=True)


def _t_of(s: pd.Series) -> float:
    x = pd.to_numeric(s, errors="coerce").dropna().to_numpy(dtype="float64")
    if x.size < 10:
        return float("nan")
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return float("nan")
    return float(x.mean() / (sd / math.sqrt(x.size)))


def nw_t(x: np.ndarray, lag: int) -> float:
    """Newey-West（Bartlett 核）HAC t 值 —— 修正"重叠样本导致 se 被低估"。"""
    v = x[np.isfinite(x)]
    t = v.size
    if t < 30 or lag < 0:
        return float("nan")
    mu = float(v.mean())
    e = v - mu
    s = float(e @ e) / t
    for l in range(1, min(lag, t - 1) + 1):
        gl = float(e[l:] @ e[:-l]) / t
        s += 2.0 * (1.0 - l / (lag + 1.0)) * gl
    if s <= 0:
        return float("nan")
    return mu / math.sqrt(s / t)


def segs_of(series: pd.Series, idx: pd.Series) -> dict:
    """四段每期净收益（OFF 期传 0，由调用方负责）。"""
    out: dict[str, float] = {}
    for nm, a, b in SEGS:
        m = (idx >= a) & (idx <= b)
        v = series[m]
        out[nm] = float(v.mean()) if len(v) else float("nan")
    return out


def runs(mask: np.ndarray) -> list[int]:
    """连续 True 段的长度列表（用来数"ON 段数"与"平均段长"）。"""
    out: list[int] = []
    cur = 0
    for v in mask:
        if v:
            cur += 1
        elif cur:
            out.append(cur)
            cur = 0
    if cur:
        out.append(cur)
    return out


def main() -> int:  # noqa: C901
    print("=" * 112)
    print("门控择时的真实换手回放（plans/24 §11.27）—— 把 §11.26 的成本夹逼收敛成一个数")
    print("=" * 112)
    slip = default_slip_bp()
    cost = FEE_DEFAULT + 2.0 * slip / 1e4
    print(f"  单次双边换手成本 = 费率 {FEE_DEFAULT:.2%} + 2×滑点 {slip:g}bp = {cost:.3%}")
    print(f"  持有期 {HOLD} 日 ⇒ 满换手 {PER_YEAR:.2f} 期/年；门口径：滚动 {ROLL_W} 日分位 ≥ {HI_Q}")
    print()

    df = build()
    df["p_mom60"] = rpct(df["mkt_mom60"])
    df["p_lb2"] = rpct(df["s_lb2"])
    g = pd.to_numeric(df["base20"], errors="coerce")
    valid = g.notna() & df["p_mom60"].notna()
    sub = df[valid].reset_index(drop=True)
    gv = pd.to_numeric(sub["base20"], errors="coerce").reset_index(drop=True)
    mv = (sub["p_mom60"] >= HI_Q).to_numpy()
    print(f"  有效样本：{len(sub):,} 个交易日（{sub['trade_date'].min()} ~ {sub['trade_date'].max()}）"
          f"；剔除 {int((~valid).sum())} 天（缺门分位或收益）")
    print()

    # ---------- ① 门的黏性：状态序列长什么样 ----------
    print("=" * 112)
    print("① 门的状态序列（决定「要换多少次手」的唯一事实来源）")
    print("=" * 112)
    segs = runs(mv)
    n = len(mv)
    n_on = int(mv.sum())
    fn = np.concatenate([[False], mv])
    on_sw = int((~fn[:-1] & fn[1:]).sum())          # OFF→ON 次数 = 建仓次数
    off_sw = int((fn[:-1] & ~fn[1:]).sum())         # ON→OFF 次数 = 清仓次数
    print(f"  逐日口径：ON {n_on} 天（{n_on / n:.1%}）· 建仓 {on_sw} 次 · 清仓 {off_sw} 次 · "
          f"ON 段数 {len(segs)} · 平均段长 {np.mean(segs):.1f} 天")
    print(f"  ⇒ 若允许「ON 期间一直持有」（D3），**真实建仓次数就只有 {on_sw} 次**"
          f"（跨 {len(sub) / 252:.1f} 年 ⇒ {on_sw / (len(sub) / 252):.1f} 次/年）")
    print()

    # ---------- ② 不重叠周期口径（真正的独立样本） ----------
    print("=" * 112)
    print("② 不重叠周期口径（每期 20 个交易日，互不重叠 —— 这才是独立样本）")
    print("=" * 112)
    k = len(sub) // HOLD
    per: list[dict] = []
    for i in range(k):
        i0 = i * HOLD
        per.append({
            "date": str(sub["trade_date"].iloc[i0]),
            "on": bool(mv[i0]),
            "ret": float(gv.iloc[i0]),
            "year": str(sub["trade_date"].iloc[i0])[:4],
        })
    pod = pd.DataFrame(per)
    p_on = int(pod["on"].sum())
    p_runs = runs(pod["on"].to_numpy())
    print(f"  周期数 {k} 期（{pod['date'].min()} ~ {pod['date'].max()}）"
          f"· ON {p_on} 期（{p_on / k:.1%}）· ON 段数 {len(p_runs)}"
          f"· 平均段长 {np.mean(p_runs):.1f} 期")
    print(f"  成本口径：A = {k} 次 · D2 = {p_on} 次 · D3 = {len(p_runs)} 次"
          f"（同一批数据，差的就是「算哪些期」）")
    print()
    gross_p = float(pod.loc[pod["on"], "ret"].mean()) * (p_on / k)
    print(f"  周期口径毛/期 {gross_p:+.3%}"
          f"· 年化毛 {gross_p * PER_YEAR:+.1%}"
          f"· t(ON 期) {_t_of(pod.loc[pod['on'], 'ret']):+.2f}（仅 {p_on} 个样本）")
    print()

    # ---------- ③ 三口径并列（逐日滚动摊薄，与其他各节可比） ----------
    print("=" * 112)
    print("③ ★ 三口径并列（逐日滚动摊薄；`净年化` = (毛/期 − 成本/期) × 12.6）")
    print("=" * 112)
    prev = np.concatenate([[False], mv[:-1]])
    seg_head = mv & ~prev                        # 段首（= 建仓那一次）
    gv_arr = gv.to_numpy(dtype="float64")
    mvi = mv.astype(float)
    gross = float(np.mean(mvi * gv_arr))         # OFF 期收益记 0
    rows: list[dict] = []
    for tag, fee_mask, note in (
        ("A) 每期都换（§11.26 的「保守」）", np.ones(n, dtype=bool), "含空仓期 ⇒ 净收益下界"),
        ("D2) 只在 ON 期换（中性·主口径）", mv, "OFF 空仓不换 ⇒ 最接近真实"),
        ("D3) 只在 ON 段首换（乐观）", seg_head, "假设 ON 期间成分股不老化"),
    ):
        n_sw = int(fee_mask.sum())
        ratio = n_sw / n
        cost_yr = cost * ratio * PER_YEAR
        net = gross - cost * ratio
        rows.append({"tag": tag, "sw": n_sw, "ratio": ratio, "cost_yr": cost_yr,
                     "net": net, "net_yr": net * PER_YEAR, "note": note})
        print(f"  {tag:<30}换手 {n_sw:>4} 次/样本"
              f"（{ratio * PER_YEAR:>5.1f} 次/年）· 成本 {cost_yr:>6.1%}/年"
              f"· 净年化 {net * PER_YEAR:>+7.1%}   {note}")
    print()
    net_d2 = mvi * gv_arr - cost * mvi
    naive = _t_of(pd.Series(net_d2))
    nwt = nw_t(net_d2, HOLD - 1)
    print(f"  ★ 主口径 D2 的显著性：naive t = {naive:+.2f} → "
          f"Newey-West(HAC, lag={HOLD - 1}) t = {nwt:+.2f}")
    print(f"    原因：base20 逐日滚动 ⇒ 相邻样本共享 {HOLD - 1} 天 ⇒ naive se 被低估；"
          f"独立样本量 ≈ {len(sub) / HOLD:.0f}")
    print()

    # ---------- ④ 用 D2/D3 过统一门槛 ----------
    print("=" * 112)
    print("④ 用实测换手过统一门槛（judge_default：费率 + 滑点）")
    print("=" * 112)
    base_s = pd.to_numeric(sub["base20"], errors="coerce")
    base_cand = Candidate("基准（同源）：全市场等权 · 持 20 日", float(base_s.mean()),
                          nw_t(base_s.to_numpy(dtype="float64"), HOLD - 1), PER_YEAR,
                          segs_of(base_s, sub["trade_date"]), len(sub))
    print(f"  基准（同源）：毛/期 {base_cand.gross:+.3%} · NW-t {base_cand.t:+.2f}")
    print()
    plans = [
        ("D2) 只在 ON 期换（中性·主口径）", mvi),
        ("D3) 只在 ON 段首换（乐观）", seg_head.astype(float)),
    ]
    out: dict = {"slip_bp": slip, "fee": FEE_DEFAULT, "cost": cost,
                 "n_days": n, "n_on": n_on, "builds": on_sw, "runs": len(segs),
                 "periods": k, "periods_on": p_on, "period_runs": len(p_runs),
                 "rows": [], "judge": {}}
    for tag, fee_arr in plans:
        ratio = float(fee_arr.mean())
        net_s = pd.Series(mvi * gv_arr - cost * fee_arr)     # 每期净（OFF 期 0）
        by = segs_of(net_s, sub["trade_date"])
        t_nw = nw_t(net_s.to_numpy(dtype="float64"), HOLD - 1)
        # 等效转换：judge 内部会再减一次 cost，故此处先把 cost 加回去
        cand = Candidate(tag, float(net_s.mean()) + cost, t_nw, PER_YEAR, by, n_on)
        j = judge_default(cand, base_cand)
        print(f"  {tag}")
        print(f"    换手 {ratio * PER_YEAR:.1f} 次/年 ⇒ 成本 {cost * ratio * PER_YEAR:+.1%}/年"
              f" · 净/期 {float(net_s.mean()):+.4%} · 净年化 {float(net_s.mean()) * PER_YEAR:+.1%}"
              f" · NW-t {t_nw:+.2f}")
        print(f"    ⇒ {fmt_judge(j)}")
        out["rows"].append({"tag": tag, "turnover_yr": ratio * PER_YEAR,
                            "cost_yr": cost * ratio * PER_YEAR,
                            "net_yr": float(net_s.mean()) * PER_YEAR,
                            "nw_t": t_nw, "verdict": j.get("verdict"), "by_seg": by})
        out["judge"][tag] = {"verdict": j.get("verdict"), "c": j.get("c"), "d": j.get("d")}
        print()

    print("=" * 112)
    print("⑤ 诚实边界")
    print("=" * 112)
    print("  - `base20` 是**逐日滚动**的 20 日收益 ⇒ ④ 里的 NW-t 已修正重叠，"
          "但**四段分解仍用重叠样本**（分段结论仍偏乐观）；")
    print("  - D3 只在**成本**上乐观（假设成分股不老化），收益仍用同一批 base20 ⇒ "
          "**D3 的净年化是上界**；")
    print("  - 门控是**择时**：OFF 持现金（收益 0），未计 OFF 期的机会成本；")
    print("  - 滑点仍是**结构性假设**（§11.24，非实测）；封板 / 跌停卖不出未建模；")
    print("  - 本脚本只判「**门**」这一半：§11.14 的「买低位补涨」选股端缓存里只有持 5 日版本。")
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n  已落盘：{os.path.relpath(OUT, os.path.dirname(BACKEND))}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  [warn] 落盘失败（不影响结论）：{exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
