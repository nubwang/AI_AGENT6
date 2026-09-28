"""验证：数据门禁（T7/W3）+ 模式库重建语义（T8）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_vector_and_gate.py

覆盖：
  D1. 核心表日期不一致 → 门禁 blocked，并给出可读原因（T7）
  D2. 核心表日期一致 → 门禁 passed
  D3. 核心表缺失/无日期 → blocked
  D4. runner 在 STRICT_DATA_GATE 下会中止（源码级断言：存在 DATA_GATE_BLOCKED 分支）
  T8a. VectorStore.reset 能把某集合清空（内存降级与 ChromaDB 两种模式都验）
  T8b. build_vector_store 默认 reset=True 且记录 last_build（防止跨回测累积）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


from app.backtest.runner import _check_data_gate, STRICT_DATA_GATE  # noqa: E402

# ── D1~D3 数据门禁 ──
print("[D1-D3] 数据门禁")
dv_mismatch = {"overall": "warning", "tables": {
    "daily": {"exists": True, "max_date": "20260916"},
    "adj_factor": {"exists": True, "max_date": "20260917"},
    "stk_limit": {"exists": True, "max_date": "20260917"},
    "daily_basic": {"exists": True, "max_date": "20260916"},
}}
g1 = _check_data_gate(dv_mismatch)
check("D1 日期不一致 → blocked", g1["blocked"] is True, g1["reasons"][:1])
check("D1 原因里点出落后表与日期",
      any("20260916" in r and "daily" in r for r in g1["reasons"]), str(g1["reasons"]))

dv_ok = {"overall": "ok", "tables": {t: {"exists": True, "max_date": "20260918"}
                                     for t in ("daily", "adj_factor", "stk_limit", "daily_basic")}}
g2 = _check_data_gate(dv_ok)
check("D2 日期一致 → passed", g2["blocked"] is False and g2["gate"] == "passed")

dv_missing = {"overall": "ok", "tables": {
    "daily": {"exists": True, "max_date": "20260918"}}}
g3 = _check_data_gate(dv_missing)
check("D3 核心表缺失 → blocked", g3["blocked"] is True,
      f"原因数={len(g3['reasons'])}")

src = (BACKEND / "app" / "backtest" / "runner.py").read_text(encoding="utf-8")
check("D4 STRICT_DATA_GATE 开启且会中止回测",
      STRICT_DATA_GATE is True and "DATA_GATE_BLOCKED" in src and "回测已中止" in src)

# ── T8a/T8b 模式库重建 ──
print("\n[T8] 模式库重建语义")
from app.backtest.vector_store import VectorStore, CHROMA_AVAILABLE  # noqa: E402

tmp = tempfile.mkdtemp(prefix="vs_test_")
vs = VectorStore(path=tmp, dim=4)
vecs = np.array([[1.0, 0, 0, 0], [0, 1.0, 0, 0]], dtype=float)
metas = [{"ts_code": "000001.SZ", "form_type": "A", "label": "success", "t0_date": "2026-01-01"},
         {"ts_code": "000002.SZ", "form_type": "A", "label": "success", "t0_date": "2026-01-02"}]
n1 = vs.add_patterns("success", "A", vecs, metas)
c1 = vs._get_collection("success", "A").count()
vs.add_patterns("success", "A", vecs, metas)      # 再追加一批（模拟第二次回测）
c2 = vs._get_collection("success", "A").count()
vs.reset("success", "A")
c3 = vs._get_collection("success", "A").count()
print(f"    写入 {n1} → count={c1}；再追加 → count={c2}；reset 后 → count={c3}"
      f"（chromadb={'可用' if CHROMA_AVAILABLE else '未安装，内存降级'}）")
check("T8a reset 后集合为空", c3 == 0, f"count={c3}")
check("T8a 追加确实会累积（复现问题）", c2 > c1, f"{c1} → {c2}")

vsrc = (BACKEND / "app" / "backtest" / "vector_store.py").read_text(encoding="utf-8")
check("T8b build_vector_store 默认 reset=True", "reset: bool = True" in vsrc)
check("T8b 写入前调用 reset", "store.reset(label, form)" in vsrc)
check("T8b 记录 last_build 供核对", "store.last_build" in vsrc)

rsrc = (BACKEND / "app" / "backtest" / "runner.py").read_text(encoding="utf-8")
check("T8b 报告里合并 last_build", 'getattr(store, "last_build", {})' in rsrc)

print(f"\n{'=' * 60}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
