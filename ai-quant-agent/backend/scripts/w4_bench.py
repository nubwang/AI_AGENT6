"""W4 镜像提速实测（plans/23 §十三 W4 证据脚本）

对同一批股票做 A/B：镜像关（回退 MySQL）vs 镜像开，量化真实提速倍数。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/w4_bench.py --stocks 150
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text

import app.agents.evolution_config as ec
from app.backtest import extra_feature_loader as E
from app.backtest.form_classifier import classify_batch
from app.backtest.loader import is_st, load_stock_pool, prepare_stock_data
from app.backtest.mirror_cache import MIRRORED, mirror_query
from app.backtest.outcome_tracker import batch_classify_outcomes, filter_noisy_samples
from app.backtest.surge_scanner import dedupe_segments, scan_single_stock
from app.models import SessionLocal


def _targets(n: int) -> list[tuple[str, object, list[int]]]:
    pool = load_stock_pool()
    out = []
    for c in pool["ts_code"].tolist()[:n]:
        if is_st(c, pool):
            continue
        df = prepare_stock_data(c, "20100101", "")
        if df.empty:
            continue
        segs = dedupe_segments(scan_single_stock(df, c))
        if not segs:
            continue
        oc = filter_noisy_samples(df, batch_classify_outcomes(df, classify_batch(df, segs)))
        if not oc:
            continue
        out.append((c, df, [o.t0_idx for o in oc]))
        if len(out) >= 4:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=150, help="用于挑选样本的股票池扫描范围")
    args = ap.parse_args()

    codes = ["000017.SZ", "000025.SZ", "600519.SH", "300750.SZ", "002594.SZ", "601318.SH"]
    res: dict = {}

    # ① 单表查询：MySQL vs 镜像（同一批 (股票,表) 组合）
    db = SessionLocal()
    t_my = 0.0
    for t in MIRRORED:
        cols = ", ".join(MIRRORED[t]["cols"])
        dc = MIRRORED[t]["date_col"]
        for c in codes:
            t0 = time.time()
            pd.read_sql(text(f"SELECT {cols} FROM {t} WHERE ts_code='{c}' ORDER BY {dc}"),
                        db.connection())
            t_my += time.time() - t0
    db.close()
    t_mi = 0.0
    for t in MIRRORED:
        for c in codes:
            t0 = time.time()
            mirror_query(c, t, MIRRORED[t]["cols"], MIRRORED[t]["date_col"])
            t_mi += time.time() - t0
    k = len(codes) * len(MIRRORED)
    res["table_query"] = {
        "calls": k,
        "mysql_s": round(t_my, 2), "mirror_s": round(t_mi, 3),
        "mysql_ms_per": round(t_my / k * 1000, 1), "mirror_ms_per": round(t_mi / k * 1000, 1),
        "speedup": round(t_my / max(t_mi, 1e-6), 1),
    }
    print(f"① 单表查询（{k} 次）：MySQL {t_my:.2f}s（{t_my / k * 1000:.0f} ms/次）"
          f" vs 镜像 {t_mi:.3f}s（{t_mi / k * 1000:.1f} ms/次）→ 提速 {t_my / max(t_mi, 1e-6):.0f}x")

    # ② build_extra_map（回测真实瓶颈，占 92.8%）：镜像开 / 关
    tg = _targets(args.stocks)
    print(f"② 样本股票 {len(tg)} 只（有候选段）")
    per_flag: dict[int, float] = {}
    orig = ec.get_param
    for flag in (0, 1):
        ec.get_param = lambda n, d=None, _f=flag: _f if n == "extra_mirror_enabled" else orig(n, d)
        t0 = time.time()
        for c, df, idxs in tg:
            E.build_extra_map(c, df, idxs)
        per_flag[flag] = round(time.time() - t0, 2)
        ec.get_param = orig
    res["build_extra_map"] = {
        "stocks": len(tg), "mirror_off_s": per_flag[0], "mirror_on_s": per_flag[1],
        "speedup": round(per_flag[0] / max(per_flag[1], 0.01), 1),
    }
    print(f"   build_extra_map × {len(tg)} 股：镜像关 {per_flag[0]}s → 镜像开 {per_flag[1]}s"
          f"（提速 {per_flag[0] / max(per_flag[1], 0.01):.1f}x）")

    print("③ 结论：" + json.dumps(res, ensure_ascii=False))
    try:
        with open("data/w4_bench.json", "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
