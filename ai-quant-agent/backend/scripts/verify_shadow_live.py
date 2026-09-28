"""线上影子内测（plans/24 §11.22）：**真的把开关切到 `shadow`** 跑一遍真实路由

## 为什么必须做这一步（而不是只用单元验收）

§11.20 的验收是"对着函数"的；真实链路里还有三层东西可能出问题：

    1. 路由层的 `_with_baseline_shadow()` **串联位置**（是否真挂在 `_with_plans` 之后）；
    2. **取数**（`/today` 走 `_data_date()`；`/date/{d}` 走历史文件）；
    3. **可序列化性**（FastAPI 要把它 JSON 化 —— 混入 `nan`/`np.float64` 会 500）。

这一步**真的把参数写进 `data/evolution_config.json`**，跑一遍真实路由，
核对"**只多一个字段、`top_picks` 一字不动**"，然后**无论成败都在 `finally` 恢复原值**。

## ★ 第一版内测就纠正了我自己的一处说法（本节第一个收获）

我原本在脚本里写"**空榜单也会被附加 shadow**（白跑一次 SQL，登记为小债）"。
实测反了 —— 空榜单走的是路由里的**早退分支**：

    if report is None:
        return {"date": ..., "message": "该日暂无榜单", "top_picks": []}
    return _with_baseline_shadow(_with_plans(report, today), today)

⇒ **空榜单根本不经过 `_with_baseline_shadow`** ⇒ 既无额外开销、也不会污染"暂无榜单"的语义。
这不是"小债"，而是**既有结构本身给对的行为**。我原来的说法是错的，此处更正。

## 核对项（A~G）

    A `off` 轮：两条路径都**不得**出现 `baseline_shadow`
    B `shadow` 轮：**有榜单**的路径必须出现，且字段齐备
    C **有榜单**路径：`top_picks` 非空且**逐字节相同**
    D **有榜单**路径：除 `baseline_shadow` 外其余键**逐字节相同**
    E 可序列化（`json.dumps` 成功）—— 防 FastAPI 500
    F 空榜单路径：`off`/`on` 两轮都**不附加**（登记原因为"路由早退"）
    G `finally` 恢复后，参数读回**原值**

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_shadow_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import evolution_config as evc  # noqa: E402
from app.api import predictions as api  # noqa: E402
from app.backtest import baseline_shadow as bs  # noqa: E402

DATE_WITH_REPORT = "20260915"     # 有 agent 榜单的历史日（主验证路径）
DATE_EMPTY = "20260101"           # 无榜单的历史日（空榜单路径）
_OK = {"pass": 0, "fail": 0}


def _ok(name: str, cond: bool, extra: str = "") -> None:
    _OK["pass" if cond else "fail"] += 1
    print(f"  {'✔' if cond else '✘'} {name}" + (f"    {extra}" if extra else ""))


def _j(o) -> str:
    return json.dumps(o, ensure_ascii=False, sort_keys=True, default=str)


def _top(report) -> str:
    if not isinstance(report, dict):
        return "—"
    return _j(report.get("top_picks"))


def _n_top(report) -> int:
    if not isinstance(report, dict):
        return -1
    return len(report.get("top_picks") or [])


def _strip_shadow(report) -> str:
    if not isinstance(report, dict):
        return "—"
    return _j({k: v for k, v in report.items() if k != bs.SHADOW_KEY})


def _summarize(tag: str, sh: dict) -> None:
    print(f"  ── shadow 摘要（{tag}）──")
    for k, v in (sh.get("portfolios") or {}).items():
        print(f"     {k:<12} {v.get('n_picked')} 只 / 池 {v.get('pool_size')} 只")
    for k, g in (sh.get("gate") or {}).items():
        print(f"     gate[{k}] {g.get('verdict')} · 年化净 {g.get('net_per_year')} · "
              f"t {g.get('t')}")
    for k, g in (sh.get("gate_ours") or {}).items():
        print(f"     gate_ours[{k}] {g.get('verdict')} · "
              f"段 {g.get('segments_have')}/{g.get('segments_total')} · "
              f"缺 {g.get('segments_missing')} · 毛/期 {g.get('gross_per_period')}")
    for n in (sh.get("notes") or []):
        print(f"     note: {n}")


def main() -> int:
    print("=" * 104)
    print("线上影子内测（plans/24 §11.22）—— 真开关 + 真路由 + **无条件恢复**")
    print("=" * 104)
    print(f"  参数：{bs.PARAM}   当前：{bs.mode()}   模式：{list(bs.MODES)}")
    print(f"  配置文件：{os.path.relpath(evc.CONFIG_FILE, os.getcwd())}")
    print()

    orig = evc.get_param(bs.PARAM, bs.MODE_OFF)
    print(f"  记录原值 = {orig!r}（结束时恢复）")
    print()

    off_t: dict | None = None
    on_t: dict | None = None
    off_d: dict | None = None
    on_d: dict | None = None
    off_e: dict | None = None
    on_e: dict | None = None
    try:
        if not evc.set_param(bs.PARAM, bs.MODE_OFF):
            print("  ❌ 无法写入 off（参数未注册？）")
            return 1
        off_t = asyncio.run(api.get_today_predictions())
        off_d = asyncio.run(api.get_predictions_by_date(DATE_WITH_REPORT))
        off_e = asyncio.run(api.get_predictions_by_date(DATE_EMPTY))

        if not evc.set_param(bs.PARAM, bs.MODE_SHADOW):
            print("  ❌ 无法写入 shadow")
            return 1
        print(f"  ✅ 已切到 shadow（mode() = {bs.mode()}）")
        on_t = asyncio.run(api.get_today_predictions())
        on_d = asyncio.run(api.get_predictions_by_date(DATE_WITH_REPORT))
        on_e = asyncio.run(api.get_predictions_by_date(DATE_EMPTY))
    finally:
        ok = evc.set_param(bs.PARAM, orig if orig is not None else bs.MODE_OFF)
        back = evc.get_param(bs.PARAM, bs.MODE_OFF)
        print(f"  ↩ 已恢复：写入={ok} 读回={back!r}  mode()={bs.mode()}")
        print()

    print("=" * 104)
    print(f"0 现状登记（真实数据日）")
    print("=" * 104)
    print(f"  `/today`（数据日由 _data_date() 决定）→ top_picks = {_n_top(off_t)} 只，"
          f"message = {str((off_t or {}).get('message'))[:48]}")
    print(f"  `/date/{DATE_WITH_REPORT}` → top_picks = {_n_top(off_d)} 只  ← **主验证路径**")
    print(f"  `/date/{DATE_EMPTY}` → top_picks = {_n_top(off_e)} 只（空榜单路径）")
    _ok(f"`/date/{DATE_WITH_REPORT}` 有榜单（否则本测无意义）", _n_top(off_d) > 0)
    print()

    print("=" * 104)
    print(f"A/B 字段与判定（主路径 `/date/{DATE_WITH_REPORT}`）")
    print("=" * 104)
    _ok("off 轮无 baseline_shadow", isinstance(off_d, dict) and bs.SHADOW_KEY not in off_d)
    sh = (on_d or {}).get(bs.SHADOW_KEY) if isinstance(on_d, dict) else None
    _ok("shadow 轮出现 baseline_shadow", isinstance(sh, dict))
    if isinstance(sh, dict):
        for k in ("mode", "date", "portfolios", "gate", "gate_ours", "reference",
                  "disclaimer", "notes"):
            _ok(f"字段 {k} 存在", k in sh)
        _summarize(DATE_WITH_REPORT, sh)
    else:
        print("  ⚠ 未取到 shadow —— 请检查 data/ours_baseline_daily.json 与 "
              "data/size_baseline_daily.json 是否存在")
    print()

    print("=" * 104)
    print("C/D 核心：只多一个字段，其余一字不动（主路径）")
    print("=" * 104)
    _ok("top_picks 逐字节相同", _top(off_d) == _top(on_d),
        f"{_n_top(off_d)} 只 vs {_n_top(on_d)} 只")
    _ok("其余键逐字节相同（只多 baseline_shadow）",
        _strip_shadow(off_d) == _strip_shadow(on_d))
    _ok("顶层只多一个键且就是 baseline_shadow",
        set((on_d or {}).keys()) - set((off_d or {}).keys()) == {bs.SHADOW_KEY})
    # 注：**引用同一性**（`is`）在"两次独立请求"下必然为 False（各自 json.load 出新对象），
    # 那是单元级断言，已由 scripts/verify_baseline_shadow.py 的 C 组覆盖。
    # 内测阶段该问的是**内容**：pick 内部的字段有没有被偷偷改掉。
    _ok("pick 内部字段集合未被改动（严防往每只票里塞东西）",
        _j([sorted(p.keys()) for p in ((off_d or {}).get("top_picks") or [])])
        == _j([sorted(p.keys()) for p in ((on_d or {}).get("top_picks") or [])]),
        f"{_j(sorted((((off_d or {}).get('top_picks') or [{}])[0]).keys()))}")
    print()

    print("=" * 104)
    print("E 可序列化（防 FastAPI 500）")
    print("=" * 104)
    for tag, r in (("/today", on_t), (f"/date/{DATE_WITH_REPORT}", on_d),
                   (f"/date/{DATE_EMPTY}", on_e)):
        try:
            s = _j(r)
            _ok(f"{tag} 可 json.dumps", True, f"{len(s):,} 字节")
        except Exception as exc:  # noqa: BLE001
            _ok(f"{tag} 可 json.dumps", False, str(exc))
    print()

    print("=" * 104)
    print("F 空榜单路径：**不附加**（原因：路由早退 —— 我第一版的说法是错的）")
    print("=" * 104)
    e_off = isinstance(off_e, dict) and bs.SHADOW_KEY in off_e
    e_on = isinstance(on_e, dict) and bs.SHADOW_KEY in on_e
    t_off = isinstance(off_t, dict) and bs.SHADOW_KEY in off_t
    t_on = isinstance(on_t, dict) and bs.SHADOW_KEY in on_t
    _ok(f"`/date/{DATE_EMPTY}` off/on 都不附加", (not e_off) and (not e_on),
        f"off={e_off} on={e_on}")
    _ok("`/today`（当前为空榜单）off/on 都不附加", (not t_off) and (not t_on),
        f"off={t_off} on={t_on}")
    print("  原因（读代码得到，非猜测）：路由对 `report is None` 直接早退，")
    print("      早退分支在 `_with_baseline_shadow(...)` **之前** ⇒ 空榜单不会走到影子逻辑。")
    print("  ⇒ 这**不是**「白跑一次 SQL 的小债」，而是既有结构给出的正确行为。")
    print()

    print("=" * 104)
    print("G 恢复")
    print("=" * 104)
    _ok("恢复后 mode() 回到原值", bs.mode() == (orig or "off"), f"mode()={bs.mode()}")
    _ok("恢复后 off 轮对象仍无 shadow", bs.SHADOW_KEY not in (off_d or {}))
    print()

    total = _OK["pass"] + _OK["fail"]
    print("=" * 104)
    print(f"结果：{_OK['pass']}/{total} 通过"
          + ("" if _OK["fail"] == 0 else f"，{_OK['fail']} 项失败 ✘"))
    print("=" * 104)
    print("判读：")
    print("  · C/D 通过 ⇒ **线上真实链路**里影子对照确实只增不改（不是只在单测里成立）；")
    print("  · E 通过   ⇒ 不会因 nan/类型问题把接口打成 500；")
    print("  · F 通过   ⇒ 空榜单零开销（既有早退自然保证）；")
    print("  · G 通过   ⇒ 内测**没有留下任何配置副作用**（可反复跑）。")
    print()
    print("  诚实边界：`/today` 走的是**空榜单分支**（数据日尚无榜单文件），")
    print("            因此「今日有榜单」这条生产路径本轮**未被内测覆盖**；")
    print(f"            但它与 `/date/{DATE_WITH_REPORT}` 共用**同一个** `_with_baseline_shadow` 串联，")
    print("            故覆盖等价 —— 这一点必须写下来，而不是含糊过去。")
    return 1 if _OK["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
