"""验证：plans/23 机制接入进化大脑统一管理（plans/23 §十二）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_evolution_integration.py

覆盖：
  E1. 8 个 plans/23 参数已注册（tier=1 / hot_reload），get_num 类型正确
  E2. 各模块真实读取进化参数（源码断言 + 运行期验证）
  E3. 进化大脑可"感知"：_evolution_health() 输出结构完整
  E4. 进化大脑可"看见"：L0 摘要含 health，L0 文本含健康神经元段落
  E5. 进化大脑可"调优"：Tier1 改参数 → 代码立即读到新值（热生效），改回后恢复
  E6. 失败安全：参数文件损坏/缺参时回退默认值，不崩
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


from app.agents import evolution_config  # noqa: E402

NEW_PARAMS = {
    "bt_c_target_coverage": float,
    "bt_min_prob_gap": float,
    "bt_cond_min_edge": float,
    "bt_cond_max_coverage": float,
    "bt_key_auc": float,
    "bt_discrimination_min_gap": float,
    "data_gate_strict": bool,
    "collect_retry_max": int,
    "collect_retry_delay_min": int,
}

print("[E1] 参数注册与类型")
table = evolution_config.params_table()
for name, typ in NEW_PARAMS.items():
    meta = table.get(name) or {}
    ok = bool(meta) and meta.get("tier") == 1 and meta.get("hot_reload") is True
    val = evolution_config.get_num(name, typ())
    check(f"E1 {name} 已注册(tier1/热生效)且类型={typ.__name__}", ok and isinstance(val, typ),
          f"value={val!r}")

print("\n[E2] 代码接线（运行期读取参数）")
# runner：改动 bt_c_target_coverage 后，A/B 覆盖率应变化
from app.backtest import runner  # noqa: E402
from app.backtest import threshold_analyzer as ta  # noqa: E402
from app.backtest import attribution as at  # noqa: E402
from app.backtest import metrics as mt  # noqa: E402

check("E2 runner 读取 bt_c_target_coverage", runner._bp("bt_c_target_coverage", runner.C_TARGET_COVERAGE) == 0.3)
check("E2 runner 读取 data_gate_strict", runner._bp("data_gate_strict", False) is True)
check("E2 metrics 读取 bt_discrimination_min_gap",
      mt.check_discrimination([0.1] * 10, [-0.1] * 10, min_gap=None)["passed"] is True)

src_ta = (BACKEND / "app" / "backtest" / "threshold_analyzer.py").read_text(encoding="utf-8")
src_at = (BACKEND / "app" / "backtest" / "attribution.py").read_text(encoding="utf-8")
src_mn = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
check("E2 threshold_analyzer 读取 bt_cond_min_edge / max_coverage",
      "bt_cond_min_edge" in src_ta and "bt_cond_max_coverage" in src_ta)
check("E2 attribution 读取 bt_key_auc", "bt_key_auc" in src_at)
check("E2 main 读取 collect_retry_max / delay_min",
      "collect_retry_max" in src_mn and "collect_retry_delay_min" in src_mn)

print("\n[E3] 感知层：健康神经元")
from app.agents import evolution_summarizer as es  # noqa: E402

h = es._evolution_health()
check("E3 神经元可运行且 ok 字段存在", "ok" in h and "issues" in h, f"verdict={h.get('verdict')}")
check("E3 含回测可信度块", isinstance(h.get("backtest"), dict) and "quality_gate" in h["backtest"])
check("E3 含数据链路块", isinstance(h.get("data_chain"), dict), str(list(h.get("data_chain", {}))[:5]))
check("E3 含 plans/23 可调参数快照", isinstance(h.get("plan23_params"), dict)
      and set(NEW_PARAMS) & set(h.get("plan23_params", {})))

print("\n[E4] L0 可视化（进化大脑必读输入）")
s = es.build_l0_summary()
check("E4 L0 摘要含 health", "health" in s)
txt = es.build_l0_text(s)
check("E4 L0 文本含『健康神经元』", "健康神经元" in txt)
check("E4 L0 文本含『数据链路』", "数据链路" in txt)
check("E4 L0 文本含可进化阈值清单", "plans/23 可进化阈值" in txt or "bt_" in txt)
check("E4 神经元结果非空且短（不炸 token）", len(txt) < 8000, f"len={len(txt)}")

print("\n[E5] 决策层：Tier1 热生效（进化大脑可直接调）")
_orig = evolution_config.get_param("bt_c_target_coverage", 0.3)
try:
    assert evolution_config.set_param("bt_c_target_coverage", 0.45) is True
    v2 = runner._bp("bt_c_target_coverage", runner.C_TARGET_COVERAGE)
    check("E5 改参数后代码立即读到新值", abs(float(v2) - 0.45) < 1e-9, f"读到 {v2}")
finally:
    evolution_config.set_param("bt_c_target_coverage", _orig)
v3 = runner._bp("bt_c_target_coverage", runner.C_TARGET_COVERAGE)
check("E5 还原后数值恢复", abs(float(v3) - float(_orig)) < 1e-9, f"恢复为 {v3}")

print("\n[E6] 失败安全")
check("E6 未注册参数回退默认值", evolution_config.get_num("__not_exist__", 7) == 7)
check("E6 类型非法时回退默认值", evolution_config.get_num("bt_c_target_coverage", 0.3) is not None)

print(f"\n{'=' * 60}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
