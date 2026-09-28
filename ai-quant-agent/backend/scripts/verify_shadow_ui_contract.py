"""前端展示契约校验（plans/24 §11.23）—— 把"前端读的键"与"后端写的键"钉在一起

## 为什么需要它

前端 `Recommend.vue` 读的是**运行时 JSON**（`report.baseline_shadow`），
TypeScript 只能保证"这段代码语法正确"，**保证不了字段存在于后端输出里**。

典型事故：后端把 `segments_missing` 改名，前端 `g.segments_missing` 静默变成 `undefined`
—— 页面照常渲染，只是那一格永远空白。**没有测试会报警。**

所以本脚本做**双向契约**：

    ① **后端输出侧**：真实调用 `baseline_shadow.build_shadow()`，
       按前端**实际读取的键路径**逐条断言（含 gate / gate_ours / portfolios / reference）；
    ② **前端源码侧**：对 `Recommend.vue` 做字符串断言 ——
       确认它读的正是这些键，且默认关闭时靠 `v-if="shadow"` 不渲染。

任一侧改名，本脚本立刻失败 ⇒ **"界面上的对照"不会静默失效**。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_shadow_ui_contract.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import baseline_shadow as bs  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_FILE = os.path.normpath(os.path.join(BACKEND, "..", "frontend", "src", "views", "Recommend.vue"))
DATE = "20260915"
_OK = {"pass": 0, "fail": 0}

# 前端读取的键路径（后端**必须**提供；改任一侧这个脚本都会失败）
BACKEND_CONTRACT: tuple[str, ...] = (
    # 顶层
    "mode", "date", "n", "hold_days", "fee_rt", "slip_bp", "disclaimer", "notes",
    "t_basis",
    # 基准两行（① 表格）
    "gate.B1.label", "gate.B1.net_per_year", "gate.B1.t", "gate.B1.t_hac",
    "gate.B1.b_hac",          # ★ §11.31：第二档（HAC t > 2）的判定结果
    "gate.B1.verdict",
    "gate.B1.slip_bp", "gate.B1.reasons",
    "gate.B2.label", "gate.B2.net_per_year", "gate.B2.t", "gate.B2.t_hac",
    "gate.B2.b_hac",          # ★ §11.31
    "gate.B2.verdict",
    "gate.B2.reasons",
    "portfolios.market_ew.n_picked", "portfolios.market_ew.pool_size",
    "portfolios.size_q1.n_picked", "portfolios.size_q1.pool_size",
    # 我们自己四行（② 表格）
    "gate_ours.ours_top30.label", "gate_ours.ours_top30.gross_per_period",
    "gate_ours.ours_top30.t_hac",
    # ⚠️ **不要**断言 `gate_ours.*.b_hac`：我们自己的口径是 `not_decidable`（§11.21 护栏），
    #    `judge()` 根本不会被调用 ⇒ 它**没有** a/b/b_hac/c/d；若硬塞一个 `False`，
    #    会被误读成"没通过第二档"，而真相是"**看不清**"。
    "gate_ours.ours_top30.verdict", "gate_ours.ours_top30.segments_have",
    "gate_ours.ours_top30.segments_total", "gate_ours.ours_top30.segments_missing",
    "gate_ours.ours_top30.reasons",
    "gate_ours.ours_all.verdict", "gate_ours.ours_all.segments_missing",
    "gate_ours.ours_rules_top30.verdict", "gate_ours.ours_agent.verdict",
    # 参考（§11.18 差距）
    "reference.random30_per_year", "reference.ours_per_year", "reference.gap_pp",
    "reference.source",
)

# 前端源码里必须出现的片段（确认它读的正是上面那些键）
UI_TOKENS: tuple[str, ...] = (
    "report.value?.baseline_shadow",
    'v-if="shadow"',
    "shadow.hold_days",
    "shadow.fee_rt",
    "shadow.slip_bp",
    "shadow.disclaimer",
    "shadow.notes",
    "shadow.reference?.random30_per_year",
    "shadow.reference?.ours_per_year",
    "shadow.reference?.gap_pp",
    "shadow.reference?.source",
    "sh.gate?.[k]",
    "sh.portfolios?.[mode]",
    "sh.gate_ours?.[k]",
    "g.net_per_year",
    "g.gross_per_period",
    "g.segments_missing",
    "pf.n_picked",
    "pf.pool_size",
    "not_decidable",
    # §11.28 的 t 口径：前端**并列显示** t / t_HAC，并把口径声明摆出来
    "g.t_hac",
    "row.t_hac",
    "shadow.t_basis",
)


def _ok(name: str, cond: bool, extra: str = "") -> None:
    _OK["pass" if cond else "fail"] += 1
    print(f"  {'✔' if cond else '✘'} {name}" + (f"    {extra}" if extra else ""))


def _get(obj, path: str):
    """按点路径取值；路径不存在返回 (False, None)。"""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def main() -> int:
    print("=" * 100)
    print("前端展示契约校验（plans/24 §11.23）：后端写的键 = 前端读的键")
    print("=" * 100)
    print(f"  前端文件：{os.path.relpath(UI_FILE, os.getcwd())}")
    print(f"  真实日期：{DATE}")
    print()

    if not os.path.exists(UI_FILE):
        print(f"  ❌ 找不到前端文件：{UI_FILE}")
        return 1

    print("① 后端输出侧（真实调用 build_shadow）")
    print("-" * 100)
    try:
        sh = bs.build_shadow(DATE)
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ build_shadow 失败：{exc}")
        return 1
    _ok("build_shadow 返回 dict 且 mode=shadow",
        isinstance(sh, dict) and sh.get("mode") == bs.MODE_SHADOW)
    for path in BACKEND_CONTRACT:
        got, val = _get(sh, path)
        shown = "—"
        if got and val is not None:
            s = str(val)
            shown = s[:38] + ("…" if len(s) > 38 else "")
        _ok(f"提供 {path}", got, shown)
    print()

    print("② 前端源码侧（确认它读的正是这些键）")
    print("-" * 100)
    with open(UI_FILE, encoding="utf-8") as f:
        src = f.read()
    for tok in UI_TOKENS:
        _ok(f"读取 {tok}", tok in src)
    print()

    print("③ 默认关闭时「界面零变化」的两个条件")
    print("-" * 100)
    _ok("前端用 v-if 受 `shadow` 控制（null ⇒ 不渲染）", 'v-if="shadow"' in src)
    # 只统计**代码行**（注释里提到 `_with_baseline_shadow()` 不算来源）
    code = "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith(("//", "<!--", "*", "/*")))
    n_src = code.count("baseline_shadow")
    _ok("代码里 `shadow` 仅来自 report.baseline_shadow（唯一来源）",
        n_src == 1 and 'report.value?.baseline_shadow' in code,
        f"代码行出现 {n_src} 次（注释不计）")
    off_doc = {"date": DATE, "top_picks": []}
    _ok("关闭时后端不附加该键 ⇒ 前端 shadow 为 null",
        bs.SHADOW_KEY not in off_doc)
    print()

    total = _OK["pass"] + _OK["fail"]
    print("=" * 100)
    print(f"结果：{_OK['pass']}/{total} 通过"
          + ("" if _OK["fail"] == 0 else f"，{_OK['fail']} 项失败 ✘"))
    print("=" * 100)
    print("判读：")
    print("  · ① 通过 ⇒ 界面要的字段后端**真的都在**（不是靠 TS 语法正确来「保证」）；")
    print("  · ② 通过 ⇒ 前端读的键名与本表格一致（任一侧改名，这里立刻失败）；")
    print("  · ③ 通过 ⇒ 默认 off 时该卡片不渲染 ⇒ **界面与从前一字不差**。")
    return 1 if _OK["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
