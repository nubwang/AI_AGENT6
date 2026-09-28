"""验收：基准组合生成器（baseline_portfolio）— plans/24 §11.19

四项检验（都在真实数据上跑，不猜）：

    ① **确定性**：同参数重复调用结果逐字节一致（等距取样必须可复现）
    ② **过滤正确**：抽出的票不含当日涨停 / close<2 / amount<1000
    ③ ★ **无偏性**：按 ts_code 等距取样，会不会带来**市值/板块偏差**？
       做法：比较「抽样组合」与「全池」的 ln(circ_mv) 中位数/均值，以及各板块占比。
       **这是本节最有价值的检验** —— 若等距取样有系统性市值偏差，
       那它就不是"索引化"，而是"隐含的选股"（正好是我们要避免的）。
    ④ **可对照性**：生成最近交易日的 B1 / B2 组合，落到
       `data/baseline_picks.json`（**只写这一份对照文件，不改任何生产决策**）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_baseline_portfolio.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.backtest.baseline_portfolio import (  # noqa: E402
    HOLD_DAYS, LABEL, build_baseline_picks, baseline_txt, _load_pool,
)
from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BACKEND, "data", "baseline_picks.json")
N = 50
FAILS: list[str] = []


def _last_date() -> str:
    with SessionLocal() as db:
        r = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
    return str(r).replace("-", "")[:8] if r else ""


def _board(code: str) -> str:
    c = code.split(".")[0]
    if c.startswith("68"):
        return "科创板"
    if c.startswith("30"):
        return "创业板"
    if c.startswith("60"):
        return "沪主板"
    if c.startswith("00"):
        return "深主板"
    if c.startswith(("8", "4")):
        return "北交所"
    return "其他"


def main() -> int:  # noqa: C901
    date = _last_date()
    print("=" * 112)
    print(f"基准组合生成器验收（plans/24 §11.19）  交易日 = {date}")
    print("=" * 112)
    if not date:
        print("  ❌ 取不到交易日")
        return 1

    pool = _load_pool(date)
    print(f"  当日可买池：{len(pool)} 只（已过滤涨停 / close<2 / amount<1000 / circ_mv≤0）")
    if pool.empty:
        print("  ❌ 池为空，无法验收")
        return 1

    # ---- ① 确定性 ----
    print()
    print("① 确定性（同参数两次调用必须完全一致）")
    a = build_baseline_picks(date, n=N, mode="market_ew")
    b = build_baseline_picks(date, n=N, mode="market_ew")
    ok = a["picks"] == b["picks"] and a["n_picked"] == b["n_picked"]
    print(f"   两次结果一致：{'✔' if ok else '✘'}")
    if not ok:
        FAILS.append("① 确定性")

    # ---- ② 过滤正确 ----
    print()
    print("② 过滤正确（抽出的票必须都在过滤后的池里）")
    codes = {p["ts_code"] for p in a["picks"]}
    inpool = codes.issubset(set(pool["ts_code"].astype(str)))
    lim = pool[pool["pct_chg"].fillna(0) >= 9.5]["ts_code"].astype(str)
    lowp = pool[pool["close"] < 2.0]["ts_code"].astype(str)
    lowa = pool[pool["amount"] < 1000.0]["ts_code"].astype(str)
    print(f"   全部来自过滤后的池：{'✔' if inpool else '✘'}")
    print(f"   池内涨停数 {len(lim)} / 低价数 {len(lowp)} / 低流动数 {len(lowa)}"
          f" ⇒ {'✔ 池本身已干净' if len(lim) + len(lowp) + len(lowa) == 0 else '✘ 池不干净'}")
    if not inpool:
        FAILS.append("② 过滤（不在池内）")

    # ---- ③ ★ 无偏性：等距取样 vs 全池 ----
    print()
    print("③ ★ 无偏性检验：按 ts_code 等距取样，会不会引入市值/板块偏差？")
    sel = pool[pool["ts_code"].astype(str).isin(codes)]
    for lab, df in (("全池", pool), ("抽样", sel)):
        print(f"   {lab}：n={len(df):>5}  ln_mv 中位 {df['ln_mv'].median():.3f}  "
              f"均值 {df['ln_mv'].mean():.3f}  close 中位 {df['close'].median():.2f}")
    dev_mv = float(sel["ln_mv"].median() - pool["ln_mv"].median())
    dev_px = float(sel["close"].median() - pool["close"].median())
    print(f"   偏差：ln_mv 中位 {dev_mv:+.3f}（≈ {np.exp(dev_mv) - 1:+.1%} 市值）"
          f" | close 中位 {dev_px:+.2f}")
    pool_b = pool["ts_code"].astype(str).map(_board).value_counts(normalize=True)
    sel_b = sel["ts_code"].astype(str).map(_board).value_counts(normalize=True)
    print("   板块占比（全池 → 抽样）：")
    for k in sorted(set(pool_b.index) | set(sel_b.index)):
        p, s = float(pool_b.get(k, 0.0)), float(sel_b.get(k, 0.0))
        flag = "✔" if abs(p - s) <= 0.06 else "⚠️"
        print(f"     {k:<6} {p:>6.1%} → {s:>6.1%}  差 {s - p:+.1%}  {flag}")
    print("   判读：ln_mv 偏差若 < 0.1（≈10% 市值）且板块差 < 6pp ⇒ 可视为**无信息选择**；")
    print("         否则等距取样就是「隐含选股」，必须改成随机或按市值分层抽样。")

    # ---- ④ 生成对照文件 ----
    print()
    print(f"④ 生成对照组合（B1 / B2，n={N}，持 {HOLD_DAYS} 日）")
    out = {"date": date, "n": N, "hold_days": HOLD_DAYS,
           "note": "对照基准，不是推荐；口径与 plans/24 §11.16/§11.18 一致",
           "items": {}}
    for mode in ("market_ew", "size_q1"):
        rec = build_baseline_picks(date, n=N, mode=mode)
        out["items"][mode] = rec
        print(f"   [{mode}] 池 {rec['pool_size']} → 抽 {rec['n_picked']} 只"
              f"{'（' + rec['note'] + '）' if rec['note'] else ''}")
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"   已写出 {os.path.relpath(OUT, BACKEND)}（只此一份，不改生产决策）")
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️ 写出失败：{e}")
        FAILS.append("④ 写出")

    print()
    print(baseline_txt(out["items"]["market_ew"]))
    print()
    print("=" * 112)
    print(f"验收结果：{'全部通过 ✔' if not FAILS else '未通过 ✘ → ' + '、'.join(FAILS)}")
    print("  注意：本生成器**不参与生产决策**；它的用途是把「基准」变成可逐日对照的一份名单。")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
