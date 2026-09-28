"""「低位补涨」选股端在**持 20 日**下的判定（plans/24 §11.29 —— §11.26.6 登记的遗留项）

## 为什么必须补这一半（§11.26.6 的原话）

§11.14.3 的发现是**双向**的：

    ① 门（择时）：市场 60 日动量高 → 该出手（q = 0.0036）；
    ② 选股端（买什么）：买**落后股**（低 `mom20`）→ 才有超额（+0.803%，**持 5 日**）。

§11.26 只判了 ①，并把 ② **明确登记为未判**：

    「缓存里 `m20_5` 只有持 5 日版本，没有持 20 日版本（要重算面板，成本高）
      ⇒ 只判了『门』这一半。」

本脚本把 ② 补上：**同一选股规则、同一 N=30、只改持有期**。

## 口径（与 §11.14 / §11.26 完全一致，**不许改**）

    · 选股：每日按 `-mom20` 排序取前 `N=30`，等权；T+1 开盘买入（`build_panel` 已保证）；
    · 对比：`base_h` = 当日全市场等权（同面板、同 fwd、同 |y| ≤ 1.5 过滤）；
    · 判定：`judge_default()`（双边 0.5% + 单边 20bp 滑点）+ **逐段**（2024/2025H1/2025H2/2026）；
    · 显著性：**同时报 naive t 与 Newey-West HAC t**（§11.28），`lag = 持有期 − 1`；
    · 顺势门：`mkt_mom60` 的**滚动 252 日分位**（≥ 0.70 为高，至少 60 点）—— 与 §11.14 同口径。

## ★ 自检（口径漂移会立刻失败）

本脚本重算的 `m20_5` 必须与缓存 `regime_gate_daily.json` 的 `m20_5` 对得上 ——
若不一致，说明选股规则 / 过滤 / 面板口径已漂移 ⇒ **直接判失败，不给结论**。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_lowmom_scheme.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

import verify_factor_portfolio as VFP  # noqa: E402
from app.backtest import sentiment_gate as SG  # noqa: E402
from app.backtest.baseline_gate import (  # noqa: E402
    Candidate, default_slip_bp, FEE_DEFAULT, fmt_judge, judge_default,
)
from app.backtest.stats_correction import t_compare  # noqa: E402
from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_CACHE = os.path.join(BACKEND, "data", "regime_gate_daily.json")
OUT = os.path.join(BACKEND, "data", "lowmom_scheme.json")
N = 30
MAX_ABS = 1.5
D0 = "20240101"
HOLDS = (5, 20)
ROLL_W, ROLL_MIN, HI_Q = 252, 60, 0.70
SEGS = (("2024", "20240101", "20241231"),
        ("2025H1", "20250101", "20250630"),
        ("2025H2", "20250701", "20251231"),
        ("2026", "20260101", "20261231"))


def _t_of(s: pd.Series) -> float:
    x = pd.to_numeric(s, errors="coerce").dropna().to_numpy(dtype="float64")
    if x.size < 10:
        return float("nan")
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return float("nan")
    return float(x.mean() / (sd / math.sqrt(x.size)))


def segs_of(series: pd.Series, idx: pd.Series) -> dict:
    out: dict[str, float] = {}
    for nm, a, b in SEGS:
        m = (idx >= a) & (idx <= b)
        v = pd.to_numeric(series[m], errors="coerce").dropna()
        out[nm] = float(v.mean()) if len(v) else float("nan")
    return out


def _tc(x: pd.Series, lag: int) -> tuple[float, float]:
    r = t_compare(pd.to_numeric(x, errors="coerce").dropna().to_numpy(dtype="float64"), lag)
    return float(r["t_naive"]), float(r["t_hac"])


def build() -> pd.DataFrame:
    with SessionLocal() as db:
        mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
    d1 = str(mx).replace("-", "")[:8] if mx else ""
    print(f"  ① 因子面板 {D0}~{d1}（复用 verify_factor_portfolio.build）...")
    df = VFP.build(D0, d1)
    if df.empty:
        raise RuntimeError("因子面板为空")
    df["_s_m20"] = -pd.to_numeric(df["mom20"], errors="coerce")
    print(f"     过滤后 {len(df):,} 行；拉取 {HOLDS} 日前向收益 ...")
    panel = SG.build_panel(HOLDS, D0, d1, keep_stock=True)
    if panel is None or panel.empty:
        raise RuntimeError("前向收益面板为空")
    out: pd.DataFrame | None = None
    for h in HOLDS:
        col = f"fwd{h}"
        if col not in panel.columns:
            print(f"     ⚠️ 缺 {col}，跳过")
            continue
        p = panel[["trade_date", "ts_code", col]].rename(columns={col: "y"})
        d = df.merge(p, how="inner", on=["trade_date", "ts_code"])
        d["y"] = pd.to_numeric(d["y"], errors="coerce")
        d = d[d["y"].abs() <= MAX_ABS].dropna(subset=["y"])
        base = d.groupby("trade_date")["y"].mean()
        t = d.dropna(subset=["_s_m20"]).sort_values(
            ["trade_date", "_s_m20"], ascending=[True, False], kind="mergesort")
        t = t.groupby("trade_date").head(N)
        low = t.groupby("trade_date")["y"].mean()
        cur = pd.DataFrame({f"base{h}": base, f"low{h}": low})
        cur.index = cur.index.astype(str)
        out = cur if out is None else out.join(cur, how="outer")
        print(f"     h={h:>2}: {len(base)} 天，低位组 {len(low)} 天")
    if out is None:
        raise RuntimeError("无有效持有期")
    out = out.reset_index()
    out = out.rename(columns={out.columns[0]: "trade_date"})
    out["trade_date"] = out["trade_date"].astype(str)
    return out.sort_values("trade_date").reset_index(drop=True)


def main() -> int:  # noqa: C901
    t0 = time.time()
    print("=" * 108)
    print("「低位补涨」选股端 · 持 20 日判定（plans/24 §11.29）")
    print("=" * 108)
    slip = default_slip_bp()
    cost = FEE_DEFAULT + 2 * slip / 1e4
    print(f"  成本（judge_default）：双边费率 {FEE_DEFAULT:.2%} + 单边滑点 {slip:g}bp "
          f"⇒ 每期 {cost:.3%}")
    print(f"  选股：每日 `-mom20` 前 {N} 只等权 · T+1 开盘买入 · |fwd| ≤ {MAX_ABS}")
    print(f"  顺势门：`mkt_mom60` 滚动 {ROLL_W} 日分位 ≥ {HI_Q}")
    print()

    df = build()
    print()

    print("=" * 108)
    print("⓪ 自检：重算 `low5` 必须等于缓存 `regime_gate_daily.json.m20_5`")
    print("=" * 108)
    with open(GATE_CACHE, encoding="utf-8") as f:
        g = pd.DataFrame(json.load(f))
    g["trade_date"] = g["trade_date"].astype(str)
    chk = df[["trade_date", "low5"]].merge(
        g[["trade_date", "m20_5"]], on="trade_date", how="inner")
    chk = chk.dropna(subset=["low5", "m20_5"])
    if len(chk) < 100:
        print(f"  ❌ 可对比天数不足（{len(chk)}）⇒ 判失败")
        return 1
    dif = (chk["low5"] - chk["m20_5"]).abs()
    corr = float(chk["low5"].corr(chk["m20_5"]))
    ok = (corr > 0.999) and (float(dif.mean()) < 1e-6)
    print(f"  对比 {len(chk)} 天 · 相关 {corr:.6f} · 平均绝对差 {dif.mean():.2e} "
          f"· 最大差 {dif.max():.2e}")
    print(f"  ⇒ {'✅ 口径一致，可继续' if ok else '❌ 口径漂移，停止给结论'}")
    if not ok:
        return 1
    print()

    df = df.merge(g[["trade_date", "mkt_mom60"]], on="trade_date", how="left")
    df["mkt_mom60"] = pd.to_numeric(df["mkt_mom60"], errors="coerce")
    df["p_mom60"] = df["mkt_mom60"].rolling(ROLL_W, min_periods=ROLL_MIN).rank(pct=True)

    print("=" * 108)
    print("① 「低位补涨」选股端**本身**（不择时、常持）—— 持 5 日 vs 持 20 日")
    print("=" * 108)
    hd = (f"{'口径':<26}{'持有期':>6}{'有效n':>7}{'毛/期':>9}{'年化毛':>9}"
          f"{'扣成本年化净':>13}{'naive t':>9}{'HAC t':>8}{'超额/期':>9}")
    print(hd)
    print("-" * len(hd))
    recs: list[dict] = []
    for h in HOLDS:
        base = pd.to_numeric(df[f"base{h}"], errors="coerce")
        low = pd.to_numeric(df[f"low{h}"], errors="coerce")
        exc = (low - base).dropna()
        per_year = 252.0 / h
        tn, th = _tc(low, max(0, h - 1))
        net = (float(low.mean()) - cost) * per_year
        print(f"{'低位补涨(-mom20 前 30)':<26}{h:>6}{int(low.notna().sum()):>7}"
              f"{float(low.mean()):>9.3%}{float(low.mean()) * per_year:>9.1%}"
              f"{net:>13.1%}{tn:>9.2f}{th:>8.2f}{float(exc.mean()):>9.3%}")
        recs.append({"name": "低位补涨(-mom20 前30)", "h": h,
                     "n": int(low.notna().sum()), "gross": float(low.mean()),
                     "net_per_year": net, "t_naive": tn, "t_hac": th,
                     "excess_per_period": float(exc.mean())})
        b_cand = Candidate(f"全市场等权 · 持 {h} 日", float(base.mean()), _t_of(base),
                           per_year, segs_of(base, df["trade_date"]),
                           int(base.notna().sum()))
        l_cand = Candidate(f"低位补涨（-mom20 前 {N}）· 持 {h} 日", float(low.mean()),
                           _t_of(low), per_year, segs_of(low, df["trade_date"]),
                           int(low.notna().sum()))
        print(f"{'  基准（同源）':<26}{h:>6}{int(base.notna().sum()):>7}"
              f"{float(base.mean()):>9.3%}{float(base.mean()) * per_year:>9.1%}"
              f"{(float(base.mean()) - cost) * per_year:>13.1%}"
              f"{_t_of(base):>9.2f}{'—':>8}{'—':>9}")
        j = judge_default(l_cand, b_cand)
        print(f"     ⇒ 统一门槛：{fmt_judge(j)}")
        print(f"     逐段净（低位补涨）："
              + " · ".join(f"{k} {v:+.1%}" for k, v in sorted(j["seg_net"].items())))
        recs[-1]["judge"] = {"verdict": j["verdict"], "a": j["a"], "b": j["b"],
                             "c": j["c"], "d": j["d"],
                             "seg_net": {k: round(float(v), 6) for k, v in j["seg_net"].items()}}
        print()

    print("=" * 108)
    print("② ★ 顺势条件（`mkt_mom60` 滚动分位 ≥ 0.70）下的超额 —— 持 20 日")
    print("=" * 108)
    on = df["p_mom60"] >= HI_Q
    n_on = int(on.sum())
    base20 = pd.to_numeric(df["base20"], errors="coerce")
    low20 = pd.to_numeric(df["low20"], errors="coerce")
    exc20 = (low20 - base20)
    exc_on = exc20[on.fillna(False)]
    exc_off = exc20[(~on.fillna(False))]
    tn, th = _tc(exc_on, 19)
    print(f"  ON 天 {n_on}（{on.mean():.1%}）· OFF 天 {int((~on.fillna(False)).sum())}")
    print(f"  ON  天超额/期 **{float(exc_on.mean()):+.3%}** · naive t {tn:+.2f} · HAC t {th:+.2f}")
    print(f"  OFF 天超额/期 {float(exc_off.mean()):+.3%}"
          f"（方向若反向 ⇒ 支持「顺势才做反转」，§11.14.3）")
    print()
    by = segs_of(exc20.where(on.fillna(False)), df["trade_date"])
    print("  ON 天超额逐段："
          + " · ".join(f"{k} {v:+.2%}" for k, v in sorted(by.items())))
    recs.append({"name": "顺势条件超额（持 20 日）", "on_days": n_on,
                 "on_excess": float(exc_on.mean()), "off_excess": float(exc_off.mean()),
                 "t_naive": tn, "t_hac": th,
                 "seg_on": {k: round(float(v), 6) for k, v in sorted(by.items())}})
    print()

    print("=" * 108)
    print("③ 组合成策略：顺势门 + 低位补涨（持 20 日）—— 与 §11.26 的『门』对照")
    print("=" * 108)
    p_on = float(on.fillna(False).mean())
    gross_combo = p_on * float(low20[on.fillna(False)].mean())
    net_b = (gross_combo - cost * p_on) * (252.0 / 20)
    net_a = (gross_combo - cost) * (252.0 / 20)
    print(f"  ON 占比 {p_on:.1%} · 组合毛/期 {gross_combo:+.3%}")
    print(f"  ⇒ 年化净（成本按 ON 占比缩放=写法 B）**{net_b:+.1%}**")
    print(f"  ⇒ 年化净（成本按每期都换仓=写法 A，下界）{net_a:+.1%}")
    print(f"  对照：always-全市场等权（持 20 日）年化净 "
          f"{(float(base20.mean()) - cost) * (252.0 / 20):+.1%}")
    recs.append({"name": "顺势门+低位补涨（持 20 日）", "on_pct": p_on,
                 "gross": gross_combo, "net_b": net_b, "net_a": net_a,
                 "base_net": (float(base20.mean()) - cost) * (252.0 / 20)})
    print()

    print("=" * 108)
    print("④ 结论与诚实边界")
    print("=" * 108)
    v20 = recs[-3].get("judge", {}) if len(recs) >= 3 else {}
    print(f"  - 「低位补涨」常持 20 日的统一门槛判定：**{v20.get('verdict', '—')}**"
          f"（a={v20.get('a')} b={v20.get('b')} c={v20.get('c')} d={v20.get('d')}）")
    print("  - 与 §11.26（只判『门』）合起来看：**这一半是否翻案，以本节的字符为准**；")
    print("  - ⚠️ 仍**未**建模：封板/跌停卖不出、OFF 期机会成本、真实逐日换手（§11.27 已给出 3.9 次/年）；")
    print("  - ⚠️ 选股端是**长仓多头**（不是多空），所以它的超额里仍含 β；")
    print("    要区分 α/β 应看「同日同分母的超额」（本节 ①/② 都是同日差值口径 ⇒ 已消掉当日市场 β）。")
    print()

    try:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump({"n": N, "holds": list(HOLDS), "cost_per_period": cost,
                       "records": recs,
                       "elapsed_sec": round(time.time() - t0, 1)}, f,
                      ensure_ascii=False, indent=1)
        print(f"  已落盘：{os.path.relpath(OUT, os.path.dirname(BACKEND))}"
              f"（耗时 {time.time() - t0:.0f}s）")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] 落盘失败（不影响结论）：{exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
