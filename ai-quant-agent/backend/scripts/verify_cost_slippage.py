"""滑点与「买不进」的实测（plans/24 §11.24 —— §11.17 一直登记的遗留项）

## 为什么必须做（这是**口径**问题，不是新功能）

到 §11.23 为止，**所有数字都只扣了双边 0.5% 的费率**，另有两条真实约束被明确登记为"未扣"：

    1. **滑点**：按市价成交必然偏离开盘价，**小市值组最重**（流动性差）；
    2. **买不进**：`T+1 开盘`若已在涨停价上，**这笔交易根本不存在**。

这两条对**小市值策略**（B2 与我们的候选池都偏小市值）尤其致命 ——
如果不先把它们扣掉，后面任何新方案都可能在**错误的基础上**被判"有效/无效"。

## 本脚本怎么建模（以及**哪些是假设、哪些是实测**）

**① 买不进（实测，用 `stk_limit`）**：

    T+1 开盘价 ≥ T+1 涨停价  ⇒ 该样本**剔除**（开盘即涨停，市价买不到）

用真实 `stk_limit.up_limit` 判定（注意：`stk_limit.trade_date` 是 **Date**，
而 `daily.trade_date` 是 **VARCHAR** ⇒ 一律在 Python 侧合并，**不做 SQL 跨表比较**，
避免 plans/24 §11.16 踩过的 collation/类型坑）。

**② 滑点（结构性假设，非实测）**：

我们**没有任何盘口/成交回报数据**，所以滑点只能按**流动性分档假设**
（单边，按 T0 成交额 `amount`，单位千元）：

    ≥ 100 亿 → 5bp    ≥ 1 亿 → 10bp    ≥ 1000 万 → 20bp    其余 → 40bp

⇒ 因此本脚本的产出是**敏感性分析**（"滑点多大会让结论翻转"），
**不是"我们实测了滑点是多少"** —— 这个区别必须写在结论里。

## 输出

    ① 买不进的实测比例（总体 / 按市值组 / 按年）
    ② B1 / B2 在不同滑点档下的年化净收益 + **归零滑点**
    ③ 我们的实盘榜（ours_top30）同口径（样本仅 29 天，单独标注）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_cost_slippage.py
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
from sqlalchemy import text  # noqa: E402

from app.backtest import sentiment as ST  # noqa: E402
from app.backtest import sentiment_gate as SG  # noqa: E402
from app.backtest.stats_correction import t_compare  # noqa: E402
from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BACKEND, "data", "cost_slippage_daily.json")
# 日序列**另存**（不写进 CACHE，保持它的既有结构不变）—— 供 §11.28 的 HAC 复核复用
SERIES_OUT = os.path.join(BACKEND, "data", "cost_slippage_series.json")
PICK_DIR = os.path.join(BACKEND, "data", "daily_recommend")
HORIZONS = (5, 10, 20)
FEE = 0.005                 # 双边总费率（与 §11.16 一致）
MAX_ABS = 1.5
D0 = "20240101"
NQ = 5
MIN_PRICE = 2.0
MIN_AMOUNT = 1000.0         # 千元
LIMIT_UP_PCT = 9.5
SLIPS_BP = (0, 5, 10, 20, 30, 50)     # **单边**滑点（bp）
SEGS = (("2024", "20240101", "20241231"),
        ("2025H1", "20250101", "20250630"),
        ("2025H2", "20250701", "20251231"),
        ("2026", "20260101", "20261231"))
FILE_RE = re.compile(r"daily_(\d{8})(_agent)?(_v\d+)?\.json$")
TOP_N = 30
QN = ("Q1 最小", "Q2", "Q3", "Q4", "Q5 最大")


def _load(sql: str, d0: str, d1: str) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    with SessionLocal() as db:
        conn = db.connection()
        for a, b in ST._year_spans(d0, d1):
            df = pd.read_sql(text(sql), conn, params={"a": a, "b": b})
            if df is not None and not df.empty:
                parts.append(df)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    out["trade_date"] = out["trade_date"].astype(str).str.replace("-", "", regex=False)
    out["ts_code"] = out["ts_code"].astype(str)
    return out


def _t_of(s: pd.Series) -> float:
    x = pd.to_numeric(s, errors="coerce").dropna().to_numpy(dtype="float64")
    if x.size < 10:
        return float("nan")
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return float("nan")
    return float(x.mean() / (sd / math.sqrt(x.size)))


def _lab(s: pd.Series) -> pd.Series:
    try:
        return pd.qcut(s.rank(method="first"), NQ, labels=False)
    except ValueError:
        return pd.Series(np.nan, index=s.index)


def slip_bp(amount: pd.Series) -> pd.Series:
    """单边滑点的**结构性假设**（按 T0 成交额分档；千元）。**不是实测值。**"""
    a = pd.to_numeric(amount, errors="coerce").fillna(0.0)
    out = pd.Series(40.0, index=a.index)
    out[a >= 1e4] = 20.0        # ≥ 1000 万
    out[a >= 1e5] = 10.0        # ≥ 1 亿
    out[a >= 1e6] = 5.0         # ≥ 10 亿
    return out


def build_panel_with_exec() -> pd.DataFrame:
    """构建 (T0 行) 面板：含 fwd_h、市值分组、T1 是否涨停开盘、T0 成交额。"""
    with SessionLocal() as db:
        mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
    d1 = str(mx).replace("-", "")[:8] if mx else ""
    print(f"  区间：{D0} ~ {d1}")

    daily = _load("SELECT trade_date, ts_code, open, high, low, close, amount, pct_chg "
                  "FROM daily WHERE trade_date >= :a AND trade_date <= :b", D0, d1)
    if daily.empty:
        raise RuntimeError("daily 为空")
    for c in ("open", "high", "low", "close", "amount", "pct_chg"):
        daily[c] = pd.to_numeric(daily[c], errors="coerce")
    print(f"     daily  {len(daily):,} 行")

    # stk_limit：trade_date 是 Date ⇒ 统一成 YYYYMMDD 字符串后再在 Python 侧合并
    lim = _load("SELECT trade_date, ts_code, up_limit FROM stk_limit "
                "WHERE trade_date >= :a AND trade_date <= :b", D0, d1)
    if not lim.empty:
        lim["up_limit"] = pd.to_numeric(lim["up_limit"], errors="coerce")
    print(f"     stk_limit {len(lim):,} 行（覆盖 {lim['trade_date'].nunique() if not lim.empty else 0} 天）")

    # ★ 关键：给每行补"同一只股票的下一交易日"（= T+1 买入日）
    daily = daily.sort_values(["ts_code", "trade_date"], kind="mergesort")
    g = daily.groupby("ts_code", sort=False)
    daily["t1_date"] = g["trade_date"].shift(-1)
    daily["t1_open"] = g["open"].shift(-1)
    if not lim.empty:
        daily = daily.merge(lim, how="left", on=["trade_date", "ts_code"])
        daily = daily.sort_values(["ts_code", "trade_date"], kind="mergesort")
        g = daily.groupby("ts_code", sort=False)
        daily["t1_up_limit"] = g["up_limit"].shift(-1)
    else:
        daily["up_limit"] = np.nan
        daily["t1_up_limit"] = np.nan

    # 可买池（与 §11.16 完全一致的过滤）
    d = daily[(daily["close"] >= MIN_PRICE) & (daily["amount"] >= MIN_AMOUNT)
              & (daily["pct_chg"].fillna(0) < LIMIT_UP_PCT)]
    d = d[d["t1_open"].notna()].copy()
    print(f"     过滤 + 有 T+1 行：{len(d):,} 行")
    # T+1 开盘即涨停 ⇒ 买不进（up_limit 缺失时视为可成交，保守）
    d["unfillable"] = (d["t1_up_limit"].notna()
                       & (d["t1_up_limit"] > 0)
                       & (d["t1_open"] >= d["t1_up_limit"] - 1e-9))
    print(f"     T+1 开盘即涨停（买不进）：{int(d['unfillable'].sum()):,} 行 "
          f"（{d['unfillable'].mean():.2%}）")
    return d


def _daily_ew(m: pd.DataFrame, col: str, mask: pd.Series | None = None,
              slip: float = 0.0) -> tuple[pd.Series, float]:
    """逐日等权的「每期」收益序列 + 其括号内单边滑点(bp)下的净年化（调用方给 h）。"""
    x = m if mask is None else m[mask]
    if x.empty:
        return pd.Series(dtype=float), float("nan")
    byd = x.groupby("trade_date")[col].mean()
    return byd, float(byd.mean() - FEE - 2.0 * slip)


def analyze(d: pd.DataFrame, panel: pd.DataFrame) -> dict:
    out: dict = {"fee": FEE, "slips_bp": list(SLIPS_BP), "horizons": {}, "unfillable": {}}
    d["_slip"] = slip_bp(d["amount"])

    # ① 买不进比例（总体 + 按年）
    out["unfillable"]["overall"] = float(d["unfillable"].mean())
    for nm, a, b in SEGS:
        s = d[(d["trade_date"] >= a) & (d["trade_date"] <= b)]
        out["unfillable"][nm] = float(s["unfillable"].mean()) if len(s) else None

    ser: dict = out.setdefault("series", {})
    for h in HORIZONS:
        col = f"fwd{h}"
        if col not in panel.columns:
            continue
        m = d.merge(panel[["trade_date", "ts_code", col]], how="inner",
                    on=["trade_date", "ts_code"])
        m[col] = pd.to_numeric(m[col], errors="coerce")
        m = m[m[col].abs() <= MAX_ABS].dropna(subset=[col])
        if m.empty:
            continue
        per = 252.0 / h
        rec: dict = {"n_days": int(m["trade_date"].nunique()), "per_year": per, "rows": []}

        # 基准 = 全市场等权
        for tag, mask in (("all(基线)", None),
                          ("fillable(剔除买不进)", ~m["unfillable"])):
            byd, _ = _daily_ew(m, col, mask)
            gm = float(byd.mean()) if len(byd) else float("nan")
            tc = (t_compare(byd.to_numpy(dtype="float64"), h - 1) if len(byd)
                  else {"t_naive": float("nan"), "t_hac": float("nan")})
            ser[f"h{h}_{'all' if mask is None else 'fillable'}"] = {
                str(k): float(v) for k, v in byd.items()}
            row = {"tag": tag, "gross": gm, "n_days": int(len(byd)),
                   "t": tc["t_naive"], "t_hac": tc["t_hac"], "net_by_slip": {}}
            for sb in SLIPS_BP:
                row["net_by_slip"][str(sb)] = ((gm - FEE - 2.0 * sb / 1e4) * per)
            row["breakeven_bp"] = (gm - FEE) * 1e4 / 2.0 if gm == gm else float("nan")
            row["by_seg"] = {
                nm: float(byd[(byd.index >= a) & (byd.index <= b)].mean())
                for nm, a, b in SEGS}
            rec["rows"].append(row)

        # 按市值分组的"买不进"比例（Q1 vs Q5）—— 证明小市值组受影响最重
        mm = m.copy()
        mm["_q"] = mm.groupby("trade_date")["amount"].transform(_lab)  # 用成交额近似流动性
        qstat = {}
        for q in range(NQ):
            s = mm[mm["_q"] == q]
            if len(s):
                byd_q = s.groupby("trade_date")[col].mean()
                ser[f"h{h}_liq{q}"] = {str(k): float(v) for k, v in byd_q.items()}
                qstat[QN[q]] = {"unfillable": float(s["unfillable"].mean()),
                                "slip_bp_mean": float(s["_slip"].mean()),
                                "gross": float(byd_q.mean())}
        rec["by_liquidity"] = qstat
        out["horizons"][h] = rec
    return out


def ours_rows(panel: pd.DataFrame, d: pd.DataFrame) -> list[dict]:
    """我们的实盘榜（规则榜 rank 前 30 + Agent 全部）—— 同口径。"""
    files = sorted(glob.glob(os.path.join(PICK_DIR, "daily_*.json")))
    latest: dict[tuple[str, bool], str] = {}
    for p in files:
        mm = FILE_RE.match(os.path.basename(p))
        if mm:
            latest[(mm.group(1), bool(mm.group(2)))] = p
    rows: list[dict] = []
    for (fdate, is_agent), p in sorted(latest.items()):
        try:
            with open(p, encoding="utf-8") as f:
                js = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        jd = str(js.get("date") or fdate).replace("-", "")[:8]
        for it in (js.get("top_picks") or []):
            code = str(it.get("ts_code") or "").strip()
            if not code:
                continue
            try:
                rk = int(it.get("rank") or 0)
            except (TypeError, ValueError):
                rk = 0
            if is_agent or (0 < rk <= TOP_N):
                rows.append({"date": jd, "ts_code": code})
    if not rows:
        return []
    pk = pd.DataFrame(rows).drop_duplicates()
    res: list[dict] = []
    dd = d[["trade_date", "ts_code", "amount", "unfillable"]].copy()
    dd["_slip"] = slip_bp(dd["amount"])
    for h in (5, 20):
        col = f"fwd{h}"
        if col not in panel.columns:
            continue
        m = pk.merge(panel[["trade_date", "ts_code", col]], how="left",
                     left_on=["date", "ts_code"], right_on=["trade_date", "ts_code"])
        m = m.merge(dd, how="left", on=["trade_date", "ts_code"])
        m[col] = pd.to_numeric(m[col], errors="coerce")
        m = m[m[col].abs() <= MAX_ABS].dropna(subset=[col])
        if m.empty:
            continue
        per = 252.0 / h
        byd = m.groupby("date")[col].mean()
        gm = float(byd.mean())
        keep = m[~m["unfillable"].fillna(False)]
        gmf = float(keep.groupby("date")[col].mean().mean()) if len(keep) else float("nan")
        res.append({
            "h": h, "n_days": int(m["date"].nunique()), "n": int(len(m)),
            "unfillable": float(m["unfillable"].fillna(False).mean()),
            "slip_bp_mean": float(m["_slip"].mean()),
            "gross_all": gm, "gross_fillable": gmf,
            "net_by_slip": {str(sb): ((gmf - FEE - 2.0 * sb / 1e4) * per)
                            for sb in SLIPS_BP},
            "breakeven_bp": (gmf - FEE) * 1e4 / 2.0,
        })
    return res


def _pct(v, nd: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{float(v) * 100:+.{nd}f}%"


def _bp(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{float(v):.1f}bp"


def main() -> int:
    t0 = time.time()
    print("=" * 104)
    print("滑点与「买不进」实测（plans/24 §11.24）—— 把 §11.17 登记的遗留项扣掉")
    print("=" * 104)
    print(f"  费率口径：双边 {FEE:.2%}（与 §11.16 一致）；滑点按**单边**给出，净 = 毛 − 费率 − 2×单边")
    print("  ⚠️ 滑点是按流动性分档的**结构性假设**，不是实测（我们没有盘口数据）")
    print()

    d = build_panel_with_exec()
    panel = SG.build_panel(HORIZONS, D0, str(d["trade_date"].max()), keep_stock=True)
    print(f"     面板 {len(panel):,} 行")
    print()
    res = analyze(d, panel)

    print("=" * 104)
    print("① 买不进（T+1 开盘即涨停）实测比例")
    print("=" * 104)
    print(f"  总体：{res['unfillable']['overall']:.3%}")
    for nm, _, _ in SEGS:
        v = res["unfillable"].get(nm)
        if v is not None:
            print(f"    {nm:<8}{v:.3%}")
    print()
    h20 = res["horizons"].get(20) or {}
    print("  按流动性分组（用成交额分位近似；证明'越难成交的组，滑点与买不进越重'）：")
    print(f"    {'组':<10}{'买不进':>10}{'假设滑点':>12}{'毛/期':>12}")
    for k, v in (h20.get("by_liquidity") or {}).items():
        print(f"    {k:<10}{v['unfillable']:>9.2%}{_bp(v['slip_bp_mean']):>12}{_pct(v['gross'], 2):>12}")
    print()

    print("=" * 104)
    print("② B1（全市场等权）与 B2（Q1 最小市值）在滑点下的年化净")
    print("=" * 104)
    hdr = (f"{'h':>4}{'口径':<24}{'毛/期':>10}{'t':>7}{'t_HAC':>8}{'天数':>6}"
           + "".join(f"{str(sb) + 'bp':>9}" for sb in SLIPS_BP) + f"{'归零滑点':>10}")
    print(hdr)
    print("-" * len(hdr))
    for h in HORIZONS:
        rec = res["horizons"].get(h) or {}
        for row in rec.get("rows", []):
            tv = row.get("t")
            tv_s = "—" if tv is None or tv != tv else f"{tv:+.2f}"
            th = row.get("t_hac")
            th_s = "—" if th is None or th != th else f"{th:+.2f}"
            line = (f"{h:>4}{row['tag'][:22]:<24}{_pct(row['gross'], 3):>10}"
                    f"{tv_s:>7}{th_s:>8}{row['n_days']:>6}")
            for sb in SLIPS_BP:
                line += f"{_pct(row['net_by_slip'][str(sb)]):>9}"
            line += f"{_bp(row['breakeven_bp']):>10}"
            print(line)
    print("  读法：`归零滑点` = 该策略年化净收益**归零**时的单边滑点（越小越脆弱）。")
    print()

    print("=" * 104)
    print("③ 分段分解（只在滑点 = 20bp 单边这一档；验证结论不依赖某一段）")
    print("=" * 104)
    for h in (20,):
        rec = res["horizons"].get(h) or {}
        for row in rec.get("rows", []):
            if row["tag"] != "fillable(剔除买不进)":
                continue
            net = (row["gross"] - FEE - 2.0 * 0.0020) * rec["per_year"]
            print(f"  h={h} {row['tag']}：滑点 20bp 后年化净 {_pct(net)}")
            segs = "、".join(f"{nm} {_pct(v, 2)}" for nm, v in (row["by_seg"] or {}).items())
            print(f"     各段每期毛：{segs}")
    print()

    print("=" * 104)
    print("④ 我们的实盘榜（ours_top30）—— 样本仅 29 天，单独标注")
    print("=" * 104)
    ours = ours_rows(panel, d)
    if not ours:
        print("  （无推荐记录，跳过）")
    for r in ours:
        print(f"  h={r['h']}：{r['n_days']} 天 / {r['n']} 条 · "
              f"买不进 {r['unfillable']:.2%} · 平均假设滑点 {_bp(r['slip_bp_mean'])}")
        print(f"     毛（全样本）{_pct(r['gross_all'], 3)} → 毛（剔除买不进）"
              f"{_pct(r['gross_fillable'], 3)}")
        nets = "、".join(f"{sb}bp {_pct(r['net_by_slip'][str(sb)])}" for sb in SLIPS_BP)
        print(f"     年化净：{nets}")
        print(f"     归零滑点：{_bp(r['breakeven_bp'])}")
    print()

    payload = {"res": res, "ours": ours}
    try:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"  ✅ 已落盘 {os.path.relpath(CACHE, os.getcwd())}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 落盘失败（不影响结论）：{exc}")
    # 日序列**另存**（不写进 CACHE，保持它的既有结构不变）—— 供 §11.28 的 HAC 复核复用
    ser_out = res.get("series") or {}
    if ser_out:
        try:
            with open(SERIES_OUT, "w", encoding="utf-8") as f:
                json.dump({"series": ser_out}, f, ensure_ascii=False)
            print(f"  ✅ 已落盘日序列 → {os.path.relpath(SERIES_OUT, os.getcwd())}"
                  f"（{len(ser_out)} 条；供 §11.28 的 HAC 复核复用）")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ 日序列落盘失败（不影响结论）：{exc}")
    print()
    print("  诚实边界（必须与结论一起读）：")
    print("    - 滑点是**假设**（按成交额分档），不是实测 ⇒ 本表是**敏感性分析**；")
    print("    - '买不进' 只判了'T+1 开盘即涨停'；**盘中封板/流动性耗尽**仍未建模；")
    print("    - 未考虑停牌（`daily` 无该行时 shift 会跳到更远的交易日，等于跳过停牌）；")
    print("    - 卖出侧同样会滑点（已按双倍计入），但未建模'跌停卖不出'。")
    print(f"  用时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
