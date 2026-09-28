"""回测瓶颈定位（plans/23 §4.5 / §十三 W4：回测提速）

背景：最近一次全量回测耗时 **4784s（约 80 分钟）**，导致
  - 进化大脑的"待回测验证"任务长期排队；
  - T4/T5 的改动无法快速验证（跑一次要等一个多小时）。

本脚本**不做优化**，只做**证据采集**：对真实股票逐股跑回测主循环的各阶段，
输出每个阶段的耗时占比 + SQL 语句次数，从而回答"时间到底花在哪"：

  阶段：prepare_stock_data / scan_single_stock / classify_batch /
        batch_classify_outcomes / filter_noisy_samples / build_extra_map /
        pattern_miner(正向+失败) / analyze_batch(入场点)

同时统计"有候选段的股票数 / 全池股票数"→ 推算真实回测的循环规模。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/profile_backtest.py --stocks 60
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event

from app.models import engine
from app.backtest.loader import load_stock_pool, prepare_stock_data, is_st
from app.backtest.surge_scanner import scan_single_stock, dedupe_segments
from app.backtest.form_classifier import classify_batch
from app.backtest.outcome_tracker import (
    batch_classify_outcomes, filter_noisy_samples,
    LABEL_SUCCESS, LABEL_FAILURE_A, LABEL_FAILURE_B,
)
from app.backtest.extra_feature_loader import build_extra_map
from app.backtest.pattern_miner import mine_positive_patterns
from app.backtest.failure_miner import mine_failure_patterns
from app.backtest.entry_analyzer import analyze_batch

# ── SQL 计数（全局探针）──
_SQL = {"n": 0, "t": 0.0}


def _install_sql_probe() -> None:
    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        _SQL["n"] += 1

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        pass


class _Stage:
    """阶段计时 + SQL 计数快照。"""

    def __init__(self) -> None:
        self.data: dict[str, dict] = defaultdict(lambda: {"t": 0.0, "sql": 0, "n": 0})

    def __call__(self, name: str):
        return _Ctx(self, name)

    def add(self, name: str, dt: float, dsql: int) -> None:
        s = self.data[name]
        s["t"] += dt
        s["sql"] += dsql
        s["n"] += 1


class _Ctx:
    def __init__(self, stage: _Stage, name: str) -> None:
        self.stage = stage
        self.name = name

    def __enter__(self):
        self.t0 = time.time()
        self.q0 = _SQL["n"]
        return self

    def __exit__(self, *exc):
        self.stage.add(self.name, time.time() - self.t0, _SQL["n"] - self.q0)
        return False


def profile(max_stocks: int, start_date: str, end_date: str) -> dict:
    _install_sql_probe()
    st = _Stage()
    pool = load_stock_pool()
    if pool.empty:
        return {"error": "股票池为空"}
    codes_all = pool["ts_code"].tolist()
    codes = codes_all[:max_stocks]
    print(f"股票池 {len(codes_all)} 只，本次抽样 {len(codes)} 只（start={start_date}, end={end_date or '最新'}）")

    t_all0 = time.time()
    q_all0 = _SQL["n"]
    with_seg = 0
    seg_total = 0
    outcomes_total = 0
    form_counter: dict[str, int] = defaultdict(int)
    unclassified = 0

    for i, code in enumerate(codes):
        try:
            if is_st(code, pool):
                continue
            with st("prepare_stock_data"):
                df = prepare_stock_data(code, start_date, end_date)
            if df.empty:
                continue
            with st("scan_single_stock"):
                segs = dedupe_segments(scan_single_stock(df, code))
            if not segs:
                continue
            with_seg += 1
            seg_total += len(segs)
            with st("classify_batch"):
                classified = classify_batch(df, segs)
            unclassified += sum(1 for c in classified if c.form_type == "U")
            for c in classified:
                form_counter[c.form_type] += 1
            with st("batch_classify_outcomes"):
                outcomes = batch_classify_outcomes(df, classified)
            with st("filter_noisy_samples"):
                outcomes = filter_noisy_samples(df, outcomes)
            outcomes_total += len(outcomes)
            with st("build_extra_map"):
                extra_map = build_extra_map(code, df, [o.t0_idx for o in outcomes])
            targets = [o for o in outcomes if o.label in (LABEL_SUCCESS, LABEL_FAILURE_A, LABEL_FAILURE_B)]
            with st("pattern_miner"):
                mine_positive_patterns(df, outcomes, extra_map=extra_map)
                mine_failure_patterns(df, outcomes, extra_map=extra_map)
            with st("analyze_batch(entry)"):
                analyze_batch(df, targets)
        except Exception as exc:  # noqa: BLE001
            print(f"  处理 {code} 失败: {exc}")
            continue
        if (i + 1) % 20 == 0:
            print(f"  进度 {i + 1}/{len(codes)}，累计 {time.time() - t_all0:.1f}s，SQL {_SQL['n'] - q_all0}")

    total = time.time() - t_all0
    total_sql = _SQL["n"] - q_all0
    rows = []
    for name, s in sorted(st.data.items(), key=lambda kv: -kv[1]["t"]):
        rows.append({
            "stage": name,
            "seconds": round(s["t"], 2),
            "pct": round(s["t"] / total * 100, 1) if total else 0.0,
            "sql": s["sql"],
            "calls": s["n"],
            "per_call_s": round(s["t"] / s["n"], 3) if s["n"] else 0.0,
        })

    out = {
        "sampled_stocks": len(codes),
        "pool_stocks": len(codes_all),
        "stocks_with_segments": with_seg,
        "segments": seg_total,
        "classified_U": unclassified,
        "form_dist": dict(form_counter),
        "outcomes": outcomes_total,
        "elapsed_s": round(total, 2),
        "sql_total": total_sql,
        "stage_breakdown": rows,
        "extrapolation": {
            "loop_stocks_est": with_seg,
            "est_full_backtest_s": round(total / max(with_seg, 1) * 5535, 1) if with_seg else None,
            "note": "est_full_backtest_s = 单股均时 × 股票池规模（粗估，真实回测的循环规模≈有候选段的股票数）",
        },
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=60, help="抽样股票数（按股票池顺序取前 N）")
    ap.add_argument("--start", default="20100101")
    ap.add_argument("--end", default="")
    ap.add_argument("--out", default="data/profile_backtest.json")
    args = ap.parse_args()

    res = profile(args.stocks, args.start, args.end)
    print("\n=== 阶段耗时（降序）===")
    print(f"{'stage':<26}{'sec':>9}{'pct':>7}{'sql':>8}{'calls':>7}{'s/call':>9}")
    for r in res.get("stage_breakdown", []):
        print(f"{r['stage']:<26}{r['seconds']:>9}{r['pct']:>6}%{r['sql']:>8}{r['calls']:>7}{r['per_call_s']:>9}")
    print(f"\n总计 {res.get('elapsed_s')}s，SQL {res.get('sql_total')} 条，"
          f"有候选段股票 {res.get('stocks_with_segments')}/{res.get('sampled_stocks')}，"
          f"候选段 {res.get('segments')}，U 段 {res.get('classified_U')}")
    print(f"形态分布：{res.get('form_dist')}")
    print(f"外推：{json.dumps(res.get('extrapolation'), ensure_ascii=False)}")

    try:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"已写入 {args.out}")
    except Exception as exc:  # noqa: BLE001
        print(f"写入失败: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
