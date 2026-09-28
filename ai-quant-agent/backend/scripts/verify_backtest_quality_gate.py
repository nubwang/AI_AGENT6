"""验证：回测落盘质量门 + 旧报告失真指标可被拦下（plans/23 T1b）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_backtest_quality_gate.py

覆盖：
  G1. quality_gate=error → **不覆盖** backtest_latest.json（只留时间戳版本）
  G2. quality_gate=ok    → 覆盖 latest，且顶层带 quality_gate
  G3. quality_gate=warn  → 覆盖 latest（warn 不拦，仅提示）
  G4. 兼容旧结构（外面包了一层 "result"）
  G5. 用**真实的旧报告数字**（win_rate=1.0 / cum_return=9.5e22）跑一遍护栏 → 必须判 error
  G6. 全部改动文件可编译
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


# ── G6: 编译检查 ──
print("[G6] 编译检查")
import py_compile  # noqa: E402

for rel in (
    "app/backtest/metrics.py",
    "app/backtest/runner.py",
    "app/backtest/pattern_miner.py",
    "app/api/backtest.py",
):
    try:
        py_compile.compile(str(BACKEND / rel), doraise=True)
        check(f"G6 编译 {rel}", True)
    except Exception as exc:  # noqa: BLE001
        check(f"G6 编译 {rel}", False, str(exc))

# ── 导入落盘模块并隔离目录 ──
import app.api.backtest as bt  # noqa: E402

tmp = tempfile.mkdtemp(prefix="bt_gate_")
bt.RESULT_DIR = tmp
bt.LATEST_FILE = os.path.join(tmp, "backtest_latest.json")

print("\n[G1-G3] 质量门行为")
bt._save_result({"performance": {"quality_gate": "error", "issues": [
    {"level": "error", "code": "CUM_RETURN_IMPLAUSIBLE", "message": "失真"}]}})
check("G1 error 门不放行 latest（拒绝覆盖）", not os.path.exists(bt.LATEST_FILE))
check("G1 但保留时间戳版本供排查",
      len([f for f in os.listdir(tmp) if f.startswith("backtest_20")]) == 1)

bt._save_result({"performance": {"quality_gate": "ok", "issues": []}, "config": {"start_date": "20100101"}})
check("G2 ok 门放行 latest", os.path.exists(bt.LATEST_FILE))
with open(bt.LATEST_FILE, encoding="utf-8") as f:
    d2 = json.load(f)
check("G2 顶层写入 quality_gate", d2.get("quality_gate") == "ok", f"gate={d2.get('quality_gate')}")

bt._save_result({"performance": {"quality_gate": "warn", "issues": [
    {"level": "warning", "code": "NO_BENCHMARK", "message": "无对照组"}]}})
with open(bt.LATEST_FILE, encoding="utf-8") as f:
    d3 = json.load(f)
check("G3 warn 门放行 latest（仅提示不拦）", d3.get("quality_gate") == "warn")

print("\n[G4] 结构兼容")
bt._save_result({"result": {"performance": {"quality_gate": "ok", "issues": []}}})
with open(bt.LATEST_FILE, encoding="utf-8") as f:
    d4 = json.load(f)
check("G4 兼容 result 包裹结构（未误判为 unknown/error）", d4.get("quality_gate") == "ok")

# ── G5: 用真实旧报告数字验证护栏能抓住 ──
print("\n[G5] 用真实旧报告数字跑护栏（应判 error）")
from app.backtest.metrics import PerformanceMetrics, validate_metrics  # noqa: E402

old = PerformanceMetrics(
    total_trades=424, win_rate=1.0, hit_rate=1.0, accuracy=0.7995283018867925,
    t20_continuation=1.0, double_confirm=0.7995283018867925, avg_t5_return=0.14000593544964485,
    profit_loss_ratio=0.0, cum_return=9.50726646200285e22, annual_return=42134258916694.54,
    max_drawdown=0.0, sharpe=15.753902180974015, exclude_effectiveness=0.0,
)
issues = validate_metrics(old)
codes = {i["code"] for i in issues}
check("G5a 抓到累计收益失真", "CUM_RETURN_IMPLAUSIBLE" in codes, str(sorted(codes)))
check("G5b 抓到年化收益失真", "ANNUAL_RETURN_IMPLAUSIBLE" in codes)
check("G5c 抓到无亏损样本（盈亏比=0）", "NO_LOSING_SAMPLES" in codes)
check("G5d 抓到无回撤", "NO_DRAWDOWN" in codes)
check("G5e 判定为 error 级（会被落盘门拦住）",
      any(i["level"] == "error" for i in issues))

print(f"\n{'=' * 60}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
