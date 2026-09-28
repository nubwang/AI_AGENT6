"""自证：进化大脑的权限域与唯一判据（用户决策：介入全部，唯一原则=D+1 + 主升浪）

用户要求："进化大脑要介入全部，一切都是为了 D+1 以及主升浪的准确率；要有绝对的修改权限读写，
可以调整、新增、修改代码；唯一需要遵循的原则就是提高 D+1 以及主升浪的准确率。"

本脚本验证这套权限**真的生效**，同时验证"唯一原则"**不可被绕过**（否则可改度量=假提升）：

  A. 全域可改：app/**、scripts/**、frontend/src/** 均可改；**不存在的路径也可新建**
  B. 度量锁定：守护/执行链本体文件不可改；命中判定核心函数不可替换
  C. 新建文件端到端：apply_code_patch(mode=new_file) 真能建文件，且编译校验/清理正常
  D. 唯一判据：main_metric.primary = [t1_hit_rate, wave_hit_rate]，宪法哈希一致
  E. 提示词与接口：系统提示词已声明全域权限与度量边界；权限接口可返回范围

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_evolution_scope.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import code_writer, evolution_config, evolution_guard  # noqa: E402

PASS, FAIL = [], []
PROBE = "app/backtest/_scope_probe_tmp.py"


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


print("\n[A] 全域可改（含新建路径）")
cases_ok = {
    "app/backtest/daily_scan.py": True,
    "app/agents/refine_agent.py": True,
    "app/kb/kb_evidence.py": True,
    "scripts/backfill_history.py": True,
    "frontend/src/views/Evolution.vue": True,
    PROBE: True,                     # 不存在 → 允许新建
}
for f, expect in cases_ok.items():
    got, why = code_writer.is_editable(f)
    check(f"可改：{f}", got is expect, why or "允许")

print("\n[B] 度量锁定（唯一原则的守护）")
cases_locked = [
    "app/agents/evolution_guard.py",
    "app/agents/evolution_events.py",
    "app/agents/evolution_config.py",
    "scripts/verify_metrics_guard.py",
    "README.md",
]
for f in cases_locked:
    got, why = code_writer.is_editable(f)
    check(f"不可改：{f}", got is False, why)

fl = code_writer.evolvable_files()
check("全域清单规模 ≥ 60", len(fl) >= 60, f"n={len(fl)}")
# 注：evolvable_files() 只扫描 app/ 与 scripts/（列出 .py 供提示词用）；
# 前端源码同属可改域，但不列入该清单（构件不同、不走 py_compile）。
check("清单含推荐链路关键文件",
      all(k in fl for k in ("app/backtest/daily_scan.py", "app/agents/daily_verify.py",
                            "app/backtest/form_classifier.py", "app/kb/kb_evidence.py")), "")
check("前端同属可改域", code_writer.is_editable("frontend/src/views/Evolution.vue")[0] is True)
check("清单不含守护文件", not any(f in fl for f in code_writer.PERMISSION_LOCKED))
check("度量核心函数清单非空", len(code_writer.METRIC_CORE_FUNCS) >= 2,
      str(sorted(code_writer.METRIC_CORE_FUNCS)))

print("\n[C] 新建文件端到端（真写盘 → 编译校验 → 清理）")
new_code = ('"""权限探针（验证脚本临时文件，运行后自动删除）"""\n\n'
            'def probe() -> str:\n    return "ok"\n')
res = code_writer.apply_code_patch({
    "file": PROBE, "create_file": True, "full_content": new_code,
    "reason": "验证全域权限：新建模块以提高 D+1 命中率（探针）",
    "evidence": "权限自证", "target": f"code_evolve:{PROBE}",
})
abs_probe = os.path.join(code_writer.PROJECT_ROOT, PROBE)
check("new_file 提案被接受并落盘", bool(res.get("ok")) and os.path.exists(abs_probe),
      str(res.get("message") or res.get("mode")))
if os.path.exists(abs_probe):
    try:
        os.remove(abs_probe)
    except Exception:  # noqa: BLE001
        pass
check("探针文件已清理", not os.path.exists(abs_probe))
try:
    from app.agents import self_improver
    self_improver.clear_restart_flag()      # 探针不应留下"待重启"
except Exception:  # noqa: BLE001
    pass

print("\n[D] 度量核心拦截 + 唯一判据")
bad = code_writer.apply_code_patch({
    "file": "app/agents/daily_verify.py", "function": "_calc_multi",
    "new_code": "def _calc_multi(*a, **k):\n    return {}\n",
    "reason": "测试：伪造命中率（应被拒绝）", "evidence": "自证",
    "target": "code_evolve_func:app/agents/daily_verify.py:_calc_multi",
})
check("替换命中判定核心被拒绝", bad.get("ok") is False,
      str(bad.get("message", ""))[:60])

locked_res = code_writer.apply_code_patch({
    "file": "app/agents/evolution_guard.py", "full_content": "# hack\n",
    "reason": "测试：改守护（应被拒绝）", "evidence": "自证",
    "target": "code_evolve:app/agents/evolution_guard.py",
})
check("改守护文件被拒绝", locked_res.get("ok") is False, str(locked_res.get("message"))[:60])

mm = evolution_config.get("main_metric", {}) or {}
check("判据含 D+1 命中率", "t1_hit_rate" in (mm.get("primary") or []), str(mm.get("primary")))
check("判据含主升浪命中率", "wave_hit_rate" in (mm.get("primary") or []))
check("判据标记不可变", bool(mm.get("immutable")))
check("宪法哈希一致（未被篡改）", evolution_guard.check_constitution().get("ok") is True)

print("\n[E] 提示词与权限接口")
sys_txt = getattr(code_writer, "_CODE_SYSTEM_FULL", "")
check("提示词声明全域权限", "全部业务代码" in sys_txt)
check("提示词声明可新建文件", "new_file" in sys_txt)
check("提示词声明度量不可改", "度量" in sys_txt)
try:
    from app.api.system import evolve_permissions
    # 2026-09-23：进化端点改为同步 def（FastAPI 放线程池执行，避免同步重活阻塞事件循环）
    perm = evolve_permissions()
    check("权限接口可用", bool(perm.get("scope", {}).get("editable_files_n")),
          f"可改文件 {perm.get('scope', {}).get('editable_files_n')} 个")
    check("接口返回判据双指标", len(perm.get("metric", {}).get("primary") or []) == 2)
    check("接口返回锁定清单", len(perm.get("locked_files") or []) >= 5)
except Exception as exc:  # noqa: BLE001
    check("权限接口可用", False, str(exc)[:80])

print(f"\n{'=' * 62}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
    sys.exit(1)
print("全部通过 ✅ —— 进化大脑可介入全部，唯一原则（D+1+主升浪）不可绕过")
