"""验收：每日推荐链路**不再有预算熔断、不设上限**（2026-09-29 用户要求）

## 事故背景（实测证据）

`data/llm_budget.json` 当时是：`limits.total_cny = 10.0`，而 `spent.total_cny = 10.0261512`
⇒ **total 闸门已破** ⇒ `deepseek_chat()` 里 `ensure_budget()` 返回 False
⇒ **不再发请求、直接返回 None** ⇒ RefineAgent 等大面积降级成「按规则概率保留」。

## 两条要求与对应实现

① **不要有预算熔断**：闸门**从 `llm_client.deepseek_chat()` 整层摘除** —— 本函数只做计量。
   为什么不能「按来源/run_id 放行」：`source` 在多数调用点走默认值；
   而 `run_id` **从未传播到任何 LLM 调用点**（实测 refine/decision/news/policy 全不传）
   ⇒ 「只拦带 run_id 的调用」等价于「永远不拦」。历史上闸门对自证回放生效，
   只是因为 day/total 两个额度**对所有调用一视同仁**。
   熔断能力交给**显式自查**的调用方：`selfproof.budget_blocked(run_id)` 主动查
   `ensure_budget(run_id=…)` —— 判据是「本任务自己花了多少」，比原先更精确。

② **不要设置上限**：`llm_metering.check_budget()._cmp()` —— `limit <= 0` 视为**不限**，
   三闸门默认值随之改为 0；`data/llm_budget.json` 也已改成 0。

## 口径

除 ⑥ 的真实台账只读展示外，断言全部**打桩**（伪造台账 + 伪造计量），
**不发任何真实 API 请求、不改真实台账**。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_no_budget_block.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from app.agents import llm_client as LC  # noqa: E402
from app.agents import llm_metering as M  # noqa: E402
from app.backtest import selfproof as SP  # noqa: E402
from app.backtest import selfproof_policy as SPP  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS: list[str] = []
FAIL: list[str] = []


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _budget_file(limits: dict, spent_total: float = 999.0) -> str:
    """把台账写进临时文件（**不碰真实台账**）。"""
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({"version": 1, "currency": "CNY", "enabled": True, "limits": limits,
               "spent": {"total_cny": spent_total, "day": {"29991231": spent_total},
                         "task": {"run_x": spent_total}},
               "ledger": []}, tmp)
    tmp.close()
    return tmp.name


def main() -> int:
    print("=" * 100)
    print("每日推荐：无预算熔断 / 不设上限 验收")
    print("=" * 100)

    # ── ① 闸门已从 deepseek_chat 摘除（源码级，确定性）──
    print("\n① 闸门已从 `deepseek_chat()` 摘除（仅保留计量）")
    print("-" * 100)
    src = open(os.path.join(BACKEND, "app", "agents", "llm_client.py"), encoding="utf-8").read()
    body = src.split("def deepseek_chat(", 1)[-1]
    # 注意：只断言**调用**（`_mtr.ensure_budget(`），不要断言"出现 ensure_budget 字样"——
    # docstring 里解释性提到 `llm_metering.ensure_budget(run_id=rid)` 是**说明文档**不是调用，
    # 按字样断言会造出"注释越清楚、测试越红"的假红。
    ck("A1 deepseek_chat 体内**不再**调用预算闸门",
       "_mtr.ensure_budget(" not in body and "_budget_gate" not in body)
    ck("A2 但**计量**保留（record_usage 仍在此函数内）", "record_usage" in body)
    ck("A3 `_budget_gate` 已删除（不留误导性死代码）", not hasattr(LC, "_budget_gate"))
    ck("A4 每日推荐链路必然不被熔断：闸门调用不存在于该路径上",
       "_mtr.ensure_budget(" not in body)

    # ── ② 不设上限：limit<=0 / None / 负数 一律「不限」──
    print("\n② 不设上限：limit = 0 / None / 负数 一律「不限」（已花多少都不熔断）")
    print("-" * 100)
    for label, limits in (("全 0", {"per_task_cny": 0, "per_day_cny": 0, "total_cny": 0}),
                          ("全 None", {"per_task_cny": None, "per_day_cny": None,
                                       "total_cny": None}),
                          ("负数", {"per_task_cny": -1, "per_day_cny": -1, "total_cny": -1})):
        path = _budget_file(limits)
        try:
            with mock.patch.object(M, "BUDGET_FILE", path):
                r = M.check_budget(run_id="run_x")
            ck(f"B({label}) 已花 999 元仍不熔断", r["ok"] is True and r["limit"] is None,
               f"ok={r['ok']} limit={r['limit']}")
        finally:
            os.unlink(path)

    # ── ③ 填了正数上限 ⇒ 闸门照常生效（确认「不限」没把能力吃掉）──
    print("\n③ 填了正数上限 ⇒ 闸门必须照常生效（能力未被吃掉，只是默认关闭）")
    print("-" * 100)
    path = _budget_file({"per_task_cny": 1.0, "per_day_cny": 1.0, "total_cny": 1.0})
    try:
        with mock.patch.object(M, "BUDGET_FILE", path):
            ck("C1 上限 1 元 < 已花 999 元 ⇒ 判定熔断",
               M.check_budget(run_id="run_x")["ok"] is False)
            ck("C2 ensure_budget ⇒ False（调用方可用它自查）",
               M.ensure_budget(run_id="run_x") is False)
        path2 = _budget_file({"per_task_cny": 0, "per_day_cny": 0, "total_cny": 5000.0},
                            spent_total=10.0)
        try:
            with mock.patch.object(M, "BUDGET_FILE", path2):
                ck("C3 逐闸门可单独设定（total=5000 未破 ⇒ 放行）",
                   M.check_budget(run_id="run_x")["ok"] is True)
        finally:
            os.unlink(path2)
    finally:
        os.unlink(path)

    # ── ④ 自证回放的熔断能力：改为主动按 run_id 自查 ──
    print("\n④ 自证回放（唯一需要熔断的路径）：改为**主动按 run_id 自查**")
    print("-" * 100)
    with mock.patch.object(M, "ensure_budget", return_value=False):
        ck("D1 超预算 ⇒ selfproof.budget_blocked(run_id) = True",
           SP.budget_blocked("run_x") is True)
    with mock.patch.object(M, "ensure_budget", return_value=True):
        ck("D2 未超预算 ⇒ False", SP.budget_blocked("run_x") is False)
    # 打桩确保真的按 run_id 传下去了（而不是只看被动标志）
    seen: list[str] = []
    with mock.patch.object(M, "ensure_budget",
                           side_effect=lambda run_id="": (seen.append(run_id), True)[1]):
        SP.budget_blocked("run_abc")
    ck("D3 确实把 run_id 传给了闸门（不是看被动标志）", seen == ["run_abc"], f"seen={seen}")
    sp_src = open(os.path.join(BACKEND, "app", "backtest", "selfproof.py"),
                  encoding="utf-8").read()
    ck("D4 防漂移：调用点传了 rid（否则 run_id 又白传）",
       "budget_blocked(rid)" in sp_src)
    ck("D5 向后兼容：run_id 为空时退回被动标志（不抛异常）",
       SP.budget_blocked("") in (True, False))

    # ── ⑤ 真实台账现状（**只读**）──
    print("\n⑤ 真实台账现状（只读，仅供参考）")
    print("-" * 100)
    b = M.load_budget(create=False)
    lim = b.get("limits") or {}
    sp = b.get("spent") or {}
    print(f"  limits={lim}")
    print(f"  spent.total_cny={sp.get('total_cny')}")
    ck("E1 真实台账的 total 上限已为 0（不限）", float(lim.get("total_cny") or 0) <= 0,
       f"total_cny={lim.get('total_cny')}")
    ck("E2 台账历史花费被保留（不因改上限而清零）",
       float(sp.get("total_cny") or 0) > 0, f"spent.total_cny={sp.get('total_cny')}")
    # 该参数只在自证页面**预填展示**，不参与熔断；但它若仍显示 5.0 会让人以为"还有上限"
    ck("E3 展示用预算参数 selfproof_budget_default_cny = 0（不限）",
       float(SPP._selfproof_param("selfproof_budget_default_cny", -1) or -1) <= 0,
       f"实得 {SPP._selfproof_param('selfproof_budget_default_cny', '缺省')}")

    print("\n" + "=" * 100)
    print(f"结果：{len(PASS)} / {len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        return 1
    print("判读：每日推荐链路**既不设上限、也不会被熔断**（闸门已从该路径摘除）；")
    print("      自证回放改为「按 run_id 主动自查」，能力**原样保留且更精确**（按本任务花费）；")
    print("      若日后要重新设上限：把 limits 填正数即可，本期默认 0 = 不限。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
