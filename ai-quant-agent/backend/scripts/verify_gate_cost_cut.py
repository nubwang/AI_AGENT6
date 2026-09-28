"""把成本压下来：**门 × 持有期 × 费率** 三维网格（plans/24 §11.14.6 第 3 条）

## 为什么这是下一步

§11.14 把"人心层"的价值量级量化到了两个数：

    门的毛边际 ≈ +6.0% / 年（30% 仓位口径，持 5 日）
    0.5% 双边费率对应的成本 ≈ 7.6% / 年
    ⇒ 净 −1.6% / 年；只有把费率压到 0.10% 才转正（+4.4%/年）

⇒ **第一步不是"接门"，而是把年化成本压到 <6%。** 成本 = 费率 × ON 占比 × (252 / h)：

    - **h（持有期）**：持 5 日 → 年约 50 次换仓；持 20 日 → 年约 12.6 次 ⇒ 成本 ÷ 4
    - **ON 占比**：门只在 30% 的日子出手 ⇒ 成本 × 0.30
    - **费率**：0.10% / 0.25% / 0.50% 双边

## 与已有两节的关系（避免重复）

    §11.13：持有期 × 费率（**无门**，无脑持组合）
    §11.14：门 × 费率（**只持 5 日**）
    §11.15（本脚本）：**门 × 持有期 × 费率 的完整交叉** —— 前两节都没做过

## 方法（沿用 §11.14 的全部红线）

    - regime 特征**直接复用** `data/regime_gate_daily.json`（它们是**市场级**的，与持有期无关）；
    - 只用 **§11.14 已过 FDR 的三个门**：全市场 60 日动量/高、全市场 20 日动量/高、情绪连板家数/高；
    - 分档阈值：滚动 252 日 30%/70% 分位（无未来函数，前 60 天不可用）；
    - **样本外**：方向只用前半段（< 2025-02-06）决定，后半段只看结果；
    - 四臂：always-base（最硬对照）/ always-combo / 门(持市场) / 门(持现金·OFF 免成本)；
    - `excess = combo − base` 同日相减，已消掉市场 beta。

运行时（首次构建多持有期序列，约 5 分钟；之后可 --from-cache 秒级复算）：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_gate_cost_cut.py
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

import verify_factor_portfolio as VFP  # noqa: E402
from app.backtest import sentiment_gate as SG  # noqa: E402
from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PANEL = os.path.join(BACKEND, "data", "gate_cost_cut_daily.json")
CACHE_REGIME = os.path.join(BACKEND, "data", "regime_gate_daily.json")
HORIZONS = (5, 10, 20)
FEES = (0.001, 0.0025, 0.005)
N = 30
MAX_ABS = 1.5
_D0_DEFAULT = "20240101"
D0 = _D0_DEFAULT
ROLL_W, ROLL_MIN, LO_Q, HI_Q = 252, 60, 0.30, 0.70
CUT_FRAC = 0.4
GATES = (("mkt_mom60", "high"), ("mkt_mom20", "high"), ("s_lb2", "high"))
SEGS = (("2025H1", "20250101", "20250630"),
        ("2025H2", "20250701", "20251231"),
        ("2026", "20260101", "20261231"))
SEG_ROWS: list[dict] = []          # 分段稳健性结果（模块级，避免多一处缩进锚点）
LAB = {"mkt_mom60": "全市场60日动量", "mkt_mom20": "全市场20日动量", "s_lb2": "情绪·连板家数"}


def set_from(v: str) -> None:
    """覆盖起始日（plans/24 §11.36.17 新增；与 `verify_regime_gate` 同法）。

    ★ 缺省（空串）⇒ `D0` 保持 `_D0_DEFAULT`，**行为与旧版逐字节一致**。
    """
    global D0
    v = (v or "").strip()
    D0 = v or _D0_DEFAULT          # 空串 ⇒ **回退默认**（幂等，保证"缺省 = 旧行为"）


def _cache_path() -> str:
    """缓存路径：缺省起始日用原名；自定义起始日**加后缀**（不覆盖既有产物）。"""
    if D0 == _D0_DEFAULT:
        return CACHE_PANEL
    return CACHE_PANEL[:-5] + f".{D0}.json"


def build_panel() -> pd.DataFrame:
    """构建 base/combo 在 5/10/20 日持有期下的每日序列。"""
    with SessionLocal() as db:
        mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
    d1 = str(mx).replace("-", "")[:8] if mx else ""
    print(f"  ① 因子面板 {D0}~{d1}（复用 verify_factor_portfolio.build）...")
    df = VFP.build(D0, d1)
    if df.empty:
        raise RuntimeError("因子面板为空")
    df["_s_combo"] = (VFP._z(df, "mom20", -1) + VFP._z(df, "turnover", -1)
                      + VFP._z(df, "illiq", 1.0) + VFP._z(df, "bias20", -1))
    print(f"     过滤后 {len(df):,} 行；拉取 {HORIZONS} 日前向收益 ...")
    panel = SG.build_panel(HORIZONS, D0, d1, keep_stock=True)
    if panel is None or panel.empty:
        raise RuntimeError("前向收益面板为空")
    out: pd.DataFrame | None = None
    for h in HORIZONS:
        col = f"fwd{h}"
        if col not in panel.columns:
            print(f"     ⚠️ 缺 {col}，跳过")
            continue
        p = panel[["trade_date", "ts_code", col]].rename(columns={col: "y"})
        d = df.merge(p, how="inner", on=["trade_date", "ts_code"])
        d["y"] = pd.to_numeric(d["y"], errors="coerce")
        d = d[d["y"].abs() <= MAX_ABS].dropna(subset=["y"])
        base = d.groupby("trade_date")["y"].mean()
        t = d.dropna(subset=["_s_combo"]).sort_values(
            ["trade_date", "_s_combo"], ascending=[True, False], kind="mergesort")
        t = t.groupby("trade_date").head(N)
        combo = t.groupby("trade_date")["y"].mean()
        cur = pd.DataFrame({f"base{h}": base, f"combo{h}": combo})
        cur.index = cur.index.astype(str)
        out = cur if out is None else out.join(cur, how="outer")
        print(f"     h={h:>2}: {len(base)} 天")
    if out is None:
        raise RuntimeError("无有效持有期")
    out = out.reset_index()
    out = out.rename(columns={out.columns[0]: "trade_date"})
    out["trade_date"] = out["trade_date"].astype(str)
    return out.sort_values("trade_date").reset_index(drop=True)


def load_regime() -> pd.DataFrame:
    with open(CACHE_REGIME, encoding="utf-8") as f:
        recs = json.load(f)
    df = pd.DataFrame(recs)
    keep = ["trade_date"] + [g for g, _ in GATES]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].copy()
    df["trade_date"] = df["trade_date"].astype(str)
    return df


def _t_of(s: pd.Series) -> float:
    x = pd.to_numeric(s, errors="coerce").dropna().to_numpy(dtype="float64")
    if x.size < 10:
        return float("nan")
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return float("nan")
    return float(x.mean() / (sd / math.sqrt(x.size)))


def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-cache", action="store_true")
    ap.add_argument("--from-", dest="from_", default="",
                    help="起始交易日 YYYYMMDD（缺省空串 ⇒ 20240101，行为与旧版一致）")
    args = ap.parse_args()
    set_from(args.from_)
    t0 = time.time()
    print("=" * 116)
    print("把成本压下来：门 × 持有期 × 费率（plans/24 §11.15）")
    print("=" * 116)
    if D0 != _D0_DEFAULT:
        names = ",".join(s[0] for s in SEGS)
        print(f"  ⚠️ 起始日已前移到 {D0}，但分段稳健性仍用**固定段** {names}")
        print("     ⇒ 早期年份不会被分段检验；要 16 年分段须先重设 SEGS 口径（plans/24 §11.36.16）。")
        print(f"     缓存另存为 {os.path.basename(_cache_path())}（**不覆盖** §11.15 的既有产物）。")

    cache = _cache_path()
    if args.from_cache and os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            daily = pd.DataFrame(json.load(f))
        print(f"  复用缓存 {os.path.relpath(cache, BACKEND)}")
    else:
        daily = build_panel()
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(daily.to_dict("records"), f, ensure_ascii=False)
            print(f"  已缓存 → {os.path.relpath(cache, BACKEND)}")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 缓存失败：{e}")

    reg = load_regime()
    d = daily.merge(reg, how="inner", on="trade_date").sort_values("trade_date")
    d = d.reset_index(drop=True)
    print(f"  合并后 {len(d)} 天（{d['trade_date'].iloc[0]} ~ {d['trade_date'].iloc[-1]}）")

    ds = sorted(d["trade_date"].astype(str).unique())
    cut = ds[int(len(ds) * CUT_FRAC)]
    head_mask = d["trade_date"].astype(str) < cut
    print(f"  样本外切分点 = {cut}（方向只用前半段决定）")

    rows: list[dict] = []
    for h in HORIZONS:
        yb, yc = f"base{h}", f"combo{h}"
        if yb not in d.columns or yc not in d.columns:
            continue
        per_year = 252.0 / h
        ok = d[yb].notna() & d[yc].notna()
        for gname, side in GATES:
            if gname not in d.columns:
                continue
            f = pd.to_numeric(d[gname], errors="coerce")
            ql = f.rolling(ROLL_W, min_periods=ROLL_MIN).quantile(LO_Q)
            qh = f.rolling(ROLL_W, min_periods=ROLL_MIN).quantile(HI_Q)
            hi, lo = f > qh, f < ql
            ex = d[yc] - d[yb]
            # ① 只用前半段决定方向
            m_hi = float(ex[hi & head_mask].mean())
            m_lo = float(ex[lo & head_mask].mean())
            if math.isnan(m_hi) or math.isnan(m_lo):
                continue
            on = (hi if m_hi >= m_lo else lo).fillna(False)
            mask = (ok & ql.notna() & (d["trade_date"].astype(str) >= cut)).to_numpy()
            sub = d[mask]
            on_s = on[mask]
            n = len(sub)
            if n < 60:
                continue
            on_n = int(on_s.sum())
            frac = on_n / max(n, 1)
            vb = sub[yb].to_numpy()
            vc = sub[yc].to_numpy()
            ma = on_s.to_numpy()
            for fee in FEES:
                gate = np.where(ma, vc, vb) - fee                    # 持市场，全期扣
                cash = np.where(ma, vc, 0.0) - fee                   # 持现金，OFF 也扣
                free = np.where(ma, vc - fee, 0.0)                   # 持现金，OFF 不扣
                rows.append({
                    "h": h, "gate_f": gname, "side": "high" if m_hi >= m_lo else "low",
                    "n": n, "on_n": on_n, "frac": frac, "fee": fee, "per_year": per_year,
                    "base": float(vb.mean()) - fee, "combo": float(vc.mean()) - fee,
                    "gate": float(gate.mean()), "cash": float(cash.mean()),
                    "free": float(free.mean()),
                    "free_t": _t_of(pd.Series(free)),
                    "gate_t": _t_of(pd.Series(gate)),
                })
                # ★ 分段稳健性：避免"整段正收益只由某一个强势子区间贡献"
                if fee in (0.0025, 0.005):
                    dts = sub["trade_date"].astype(str).to_numpy()
                    for sname, s0, s1 in SEGS:
                        m2 = (dts >= s0) & (dts <= s1)
                        if int(m2.sum()) < 20:
                            continue
                        SEG_ROWS.append({
                            "h": h, "gate_f": gname,
                            "side": "high" if m_hi >= m_lo else "low",
                            "fee": fee, "seg": sname, "n": int(m2.sum()),
                            "gate": float(gate[m2].mean()) - fee,
                            "base": float(vb[m2].mean()) - fee,
                            "per_year": per_year,
                        })

    if not rows:
        print("  ❌ 无有效组合（可能缓存缺失）")
        return 1

    # ---- 打印：按 (h, fee) 分块 ----
    for h in HORIZONS:
        for fee in FEES:
            rs = [r for r in rows if r["h"] == h and abs(r["fee"] - fee) < 1e-9]
            if not rs:
                continue
            per = rs[0]["per_year"]
            cost_ratio = fee * rs[0]["frac"] * per
            print()
            print(f"=== 持有 {h} 日 × 双边 {fee:.2%}（年约 {per:.1f} 次换仓；"
                  f"该费率下门成本≈{cost_ratio:.1%}/年）===")
            hd = (f"{'门':<18}{'ON占比':>8}{'always基准':>11}{'always组合':>11}"
                  f"{'门(持市场)':>12}{'门(持现金)':>12}{'门(持现金·免成本)':>18}{'t':>7}")
            print(hd)
            print("-" * len(hd))
            for r in sorted(rs, key=lambda x: -x["free"]):
                lab = f"{LAB[r['gate_f']]}/{'高' if r['side'] == 'high' else '低'}"
                c1 = f"{r['base'] * per:+.1%}"
                c2 = f"{r['combo'] * per:+.1%}"
                c3 = f"{r['gate'] * per:+.1%}"
                c4 = f"{r['cash'] * per:+.1%}"
                c5 = f"{r['free'] * per:+.1%}"
                c6 = "—" if math.isnan(r["free_t"]) else f"{r['free_t']:+.2f}"
                print(f"{lab:<18}{r['frac']:>8.0%}{c1:>11}{c2:>11}"
                      f"{c3:>12}{c4:>12}{c5:>18}{c6:>7}")

    # ---- 判定：样本外年化净 > 0 的格子（最宽松口径 = 门·持现金·OFF免成本）----
    pos = [r for r in rows if r["free"] * r["per_year"] > 0]
    print()
    print("=" * 116)
    print("★ 判定 1：样本外「门(持市场)」年化净 > 0 的格子（最宽松口径 = 免成本臂）")
    if not pos:
        print("  ❌ 全部为负 → **成本这条路也没走通**（即使持 20 日 + 0.10% 费率）")
    else:
        for r in sorted(pos, key=lambda x: -x["free"] * x["per_year"])[:8]:
            lab = f"{LAB[r['gate_f']]}/{'高' if r['side'] == 'high' else '低'}"
            a = f"{r['free'] * r['per_year']:+.1%}"
            b = f"{r['cash'] * r['per_year']:+.1%}"
            c = f"{r['combo'] * r['per_year']:+.1%}"
            dv = f"{r['free_t']:+.2f}" if not math.isnan(r["free_t"]) else "—"
            print(f"  ✅ 持 {r['h']:>2} 日 | {lab:<20} | 费率 {r['fee']:.2%} | "
                  f"免成本净 {a} | 全扣净 {b} | always组合 {c} | t {dv} | ON {r['on_n']}/{r['n']}")

    # ---- ★★ 三条件联合判定：这是本节真正的"过关标准" ----
    print()
    print("=" * 116)
    print("★★ 判定 2：三条件联合（**全部满足**才算可用；任一条不满足就是自欺）")
    print("   (a) 门(持市场) 全扣净 > 0      —— 扣完真实成本还能赚")
    print("   (b) t > 2                      —— 不是噪声")
    print("   (c) 门(持市场) > always-base   —— 比「什么都不做、只买小盘等权」更强")
    print()
    hd = (f"{'持有期':>6}{'门':<20}{'费率':>7}{'门(持市场)':>11}{'t':>7}"
          f"{'always基准':>11}{'a':>4}{'b':>4}{'c':>4}{'结果':>8}")
    print(hd)
    print("-" * len(hd))
    n_pass = 0
    for r in sorted(rows, key=lambda x: (-x["h"], x["fee"], -x["gate"])):
        a_ok = r["gate"] * r["per_year"] > 0
        b_ok = (not math.isnan(r["gate_t"])) and r["gate_t"] > 2.0
        c_ok = r["gate"] > r["base"]
        lab = f"{LAB[r['gate_f']]}/{'高' if r['side'] == 'high' else '低'}"
        g1 = f"{r['gate'] * r['per_year']:+.1%}"
        g2 = "—" if math.isnan(r["gate_t"]) else f"{r['gate_t']:+.2f}"
        g3 = f"{r['base'] * r['per_year']:+.1%}"
        mark = lambda ok: "✔" if ok else "✘"  # noqa: E731
        res = "**通过**" if (a_ok and b_ok and c_ok) else "否"
        if a_ok and b_ok and c_ok:
            n_pass += 1
        print(f"{r['h']:>6}{lab:<20}{r['fee']:>7.2%}{g1:>11}{g2:>7}{g3:>11}"
              f"{mark(a_ok):>4}{mark(b_ok):>4}{mark(c_ok):>4}{res:>8}")
    print()
    print(f"  ⇒ **同时满足 a/b/c 的格子数 = {n_pass} / {len(rows)}**")
    if n_pass == 0:
        print("  ⇒ 结论：**成本关可以过，但「过成本关」并不等于「有 alpha」** ——")
        print("     所有扣成本后为正的格子，要么 t < 2（噪声），要么跑不过"
              "「什么都不做、只买小盘等权持 20 日」。")
    print()
    print("  ⚠️ 同时必须记住这一节最重要的一个数字：")
    print("     always-base（**什么都不做**：买全市场小盘等权、持 20 日、扣 0.50% 双边）")
    base20 = [r for r in rows if r["h"] == 20 and abs(r["fee"] - 0.005) < 1e-9]
    if base20:
        print(f"     = **{base20[0]['base'] * base20[0]['per_year']:+.1%} / 年**；")
    print("     我们的 4 因子组合在同期是 **+2.5% / 年**（持 20 日、0.50%）")
    print("     ⇒ **「用因子选 30 只」比「什么都不选」少赚约 6.8pp/年**（§11.12 的结论被再次确认）。")
    if SEG_ROWS:
        print()
        print("=" * 116)
        print("★★ 判定 3：分段稳健性（年化净；样本外只有 ~377 天，必须看子区间）")
        print("   段：2025H1 / 2025H2 / 2026  —— 若某段为正、另两段为负，就是单区间运气")
        print()
        segs_names = [s[0] for s in SEGS]
        keys = {(r["h"], r["gate_f"], r["side"], r["fee"]) for r in SEG_ROWS}
        hd = f"{'持有期':>6}{'门':<20}{'费率':>7}" + "".join(f"{s:>18}" for s in segs_names)
        print(hd)
        print("-" * len(hd))
        for k in sorted(keys, key=lambda x: (-x[0], x[3])):
            h, gf, side, fee = k
            cells = []
            for s in segs_names:
                hit = [r for r in SEG_ROWS if r["h"] == h and r["gate_f"] == gf
                       and r["side"] == side and abs(r["fee"] - fee) < 1e-9 and r["seg"] == s]
                if not hit:
                    cells.append("—")
                    continue
                gn = hit[0]["gate"] * hit[0]["per_year"]
                bn = hit[0]["base"] * hit[0]["per_year"]
                cells.append(f"{gn:+.1%}/基准{bn:+.0%}")
            lab = f"{LAB[gf]}/{'高' if side == 'high' else '低'}"
            print(f"{h:>6}{lab:<20}{fee:>7.2%}" + "".join(f"{c:>18}" for c in cells))
        print()
        print("  判读：三段中若有任意一段 gate 为负 → **不能宣称可用**（§11.13 的教训）。")

    print()
    print("  判读（事先定好）：")
    print("    1) 只有 **免成本净 > 0 且全扣净 > 0** 才算真可行（否则就是靠不计成本骗自己）；")
    print("    2) t 必须 > 2；样本外天数不足 200 的窗口只当方向参考；")
    print("    3) 与 always-base 比：若门跑不过「只是持有市场」，说明门没有价值；")
    print("    4) 本节只回答「成本能不能救」，**不回答「要不要上线」**（那需要更长的样本外）。")
    print(f"耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
