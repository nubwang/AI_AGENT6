"""验证：市场基准（plans/23 §2.4 T4 第一步）—— 让"超额"可计算

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_market_benchmark.py

覆盖：
  B1. 单日基准可算，落在合理区间
  B2. 批量基准 + 磁盘缓存命中（第二次不再查库）
  B3. 与上证指数同期涨幅量级一致（等权 vs 加权，允许偏差）
  B4. runner 已接线（源码断言）且 300 只抽样口径写清楚
  B5. 真实结论打印：条件集 T+5 均值 vs 全市场同期（用最新回测报告里的数字对比）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


import time as _t  # noqa: E402

from app.backtest.benchmark import BENCH_FILE, benchmark_for_dates, market_t5  # noqa: E402

print("[B1] 单日基准")
v = market_t5("20260910")
check("B1 可算出数值", isinstance(v, float), f"20260910 全市场等权 T+5 = {v if v is None else round(v * 100, 2)}%")
check("B1 落在合理区间(-20%~+20%)", v is not None and -0.20 < v < 0.20)

print("\n[B2] 批量 + 缓存")
dates = ["20260908", "20260909", "20260910", "20260911", "20260914", "20260915", "20260916", "20260917"]
t0 = _t.time()
bm1 = benchmark_for_dates(dates)
d1 = _t.time() - t0
t0 = _t.time()
bm2 = benchmark_for_dates(dates)
d2 = _t.time() - t0
print(f"    首次 {d1:.1f}s / 二次 {d2:.2f}s；基准：" +
      ", ".join(f"{k[4:]}={v * 100:+.2f}%" for k, v in list(bm1.items())[:8]))
# 注意：T+5 窗口尚未走完的日期**不应**有值（这是正确行为，不是缺陷）；
# 数据末端附近的样本本身 _outcome_t5=None 也不会进统计。
late_ok = all(d not in bm1 for d in ("20260916", "20260917"))
check("B2 T+5 可算的日期都有值（≥3）", len(bm1) >= 3, f"{len(bm1)}/{len(dates)}")
check("B2 未来窗口未走完的日期不给值（防未来函数）", late_ok,
      f"未给值日期={[d for d in dates if d not in bm1]}")
check("B2 缓存生效（二次调用显著更快）", d2 < max(0.5, d1 * 0.3), f"{d1:.1f}s → {d2:.2f}s")
check("B2 缓存文件已生成", Path(BENCH_FILE).exists())
check("B2 两次结果一致", bm1 == bm2)

print("\n[B3] 与指数同期对照（量级 sanity）")
try:
    from sqlalchemy import text
    from app.models import SessionLocal
    db = SessionLocal()
    try:
        head = db.execute(text("SELECT close FROM index_daily WHERE ts_code='000001.SH' AND trade_date='20260910'")).scalar()
        tail = db.execute(text("SELECT close FROM index_daily WHERE ts_code='000001.SH' AND trade_date='20260917'")).scalar()
    finally:
        db.close()
    idx_ret = float(tail) / float(head) - 1
    has = "20260910" in bm1
    same_day = float(bm1.get("20260910", 0.0))
    diff = abs(same_day - idx_ret)
    check("B3 等权基准与上证同期涨幅量级一致", has and diff < 0.15,
          f"等权 {same_day * 100:+.2f}% vs 上证 {idx_ret * 100:+.2f}%（差 {diff * 100:.2f}pct）")
except Exception as exc:  # noqa: BLE001
    check("B3 指数对照", False, str(exc)[:100])

print("\n[B4] runner 接线（源码断言）")
src = (BACKEND / "app" / "backtest" / "runner.py").read_text(encoding="utf-8")
check("B4 runner 调用 benchmark_for_dates", "benchmark_for_dates" in src)
check("B4 基准传入 compute_metrics", "benchmark_t5=bench_t5" in src)
check("B4 报告中写明基准口径", "benchmark_note" in src)
bsrc = (BACKEND / "app" / "backtest" / "benchmark.py").read_text(encoding="utf-8")
check("B4 抽样口径与成本口径写清楚", "DEFAULT_SAMPLE_N = 300" in bsrc and "DEFAULT_COST_PCT = 0.0015" in bsrc)

print("\n[B5] 真实结论对照（最新回测报告）")
try:
    rp = BACKEND / "data" / "backtest_results" / "backtest_latest.json"
    rep = json.loads(rp.read_text(encoding="utf-8"))
    perf = rep.get("performance") or {}
    avg = perf.get("avg_t5_return")
    print(f"    最新回测：条件集 T+5 均值 = {avg * 100:+.2f}%（样本 {perf.get('total_trades')}）"
          f"；同期全市场等权 T+5 均值 ≈ {sum(bm1.values()) / len(bm1) * 100:+.2f}%"
          if isinstance(avg, (int, float)) else "    最新回测无 avg_t5_return")
    check("B5 对照数字可读（说明超额口径已可用）", isinstance(avg, (int, float)))
except Exception as exc:  # noqa: BLE001
    check("B5 读取最新回测报告", False, str(exc)[:100])

print(f"\n{'=' * 60}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
