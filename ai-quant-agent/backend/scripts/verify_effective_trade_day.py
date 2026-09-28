"""F1 验收：「有效交易日」判据（plans/24 §11.34.5 / §11.35.4）

## 修的是什么（两处，同一个病根）

    门禁（runner）：只看 `MAX(trade_date)` ⇒ 一个 443 行的**半截日**就足以判成"领先"
                     ⇒ 门禁**永不通过**，且报错原因（"领先/落后"）**误导排查方向**；
    采集（ws）：判据是「该股有没有这一天任意一行」⇒ 写进 1 行后**永久跳过**
                     ⇒ 残缺**永不自愈**。

修法：两处共用 `data_validator.effective_latest_date()` ——「**行数 ≥ 自然基数下限**的最近日期」。

## 本脚本验收 5 条（**只读，不写任何数据**）

    A 函数可用：`effective_latest_date('adj_factor')` 返回 raw / effective / rows
    B 真实场景：库里若**恰好**存在"半截日"，如实展示；没有则**如实说明并跳过**
      （⚠️ 它只是"现场演示"，**不作为判定依据**）
    C **旧行为可复现**：把 `data_gate_use_effective` 关掉 ⇒ 门禁把半截日判成"**领先**"
    D **新行为修好**：开启 ⇒ 不再"领先"，并**如实说明**有效交易日
    E 真落后仍拦得住：把锚定表日期强行改旧 ⇒ 仍然 blocked（**不能把门禁改松**）

> ★ C/D 用**打桩**（patch `effective_latest_date` 与 `_count_rows_on`）构造"半截日"，**不依赖
> 线上是否恰好有坏数据**。为什么必须这样改（plans/24 §11.36.13c）：`adj_factor` 一被补齐，
> 库里就没有半截日了，而 C/D 原先把断言绑在**真实坏数据**上 ⇒ 本脚本从 8/8 掉到 6/8。
> **那不是代码退步，是断言依赖了"问题存在"这件事** —— 问题修好了，断言反而失败，荒谬。
> 修复后的验收**在任何数据状态下都成立**。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_effective_trade_day.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from app.backtest import data_validator as DV  # noqa: E402
from app.backtest import runner as RUN  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BACKEND, "data", "validation_cache.json")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _fake_eff(raw: str, eff: str, rows: dict, min_rows: int = 5000):
    """打桩用的 `effective_latest_date`：返回**伪造的** raw/effective/rows，不查库。"""
    def f(table, *a, **k):  # noqa: ANN001, ANN002
        return {"raw": raw, "effective": eff, "rows": dict(rows), "min_rows": min_rows}
    return f


def gate_with(dv: dict, latest_td: str, use_effective: bool,
              eff_stub=None, rows_stub=None) -> dict:
    """在指定 `data_gate_use_effective` 下跑一次门禁（其余参数走真实配置）。

    `eff_stub` / `rows_stub`：可选打桩，用来**构造半截日场景**而不触碰数据库。
    """
    orig_bp, orig_rows = RUN._bp, RUN._count_rows_on
    patchers = []

    def patched(n, d):
        if n == "data_gate_use_effective":
            return use_effective
        return orig_bp(n, d)

    try:
        RUN._bp = patched  # type: ignore[assignment]
        if eff_stub is not None:
            # runner 可能是 `from ... import` 进来的，两处都尝试打桩
            for mod in (DV, RUN):
                if hasattr(mod, "effective_latest_date"):
                    p = mock.patch.object(mod, "effective_latest_date", side_effect=eff_stub)
                    p.start()
                    patchers.append(p)
        if rows_stub is not None:
            RUN._count_rows_on = rows_stub  # type: ignore[assignment]
        return RUN._check_data_gate(dv, today=latest_td, latest_td=latest_td)
    finally:
        RUN._bp = orig_bp  # type: ignore[assignment]
        RUN._count_rows_on = orig_rows  # type: ignore[assignment]
        for p in patchers:
            p.stop()


def main() -> int:
    print("=" * 100)
    print("F1 验收：「有效交易日」判据（plans/24 §11.34.5 / §11.35.4）")
    print("=" * 100)

    # ── A 函数可用 ──
    e = DV.effective_latest_date("adj_factor")
    check("A effective_latest_date 可调用", isinstance(e, dict) and "effective" in e,
          f"raw={e['raw']!r} effective={e['effective']!r} min_rows={e['min_rows']}")
    check("A 返回逐日行数（可用于审计）", len(e["rows"]) > 0,
          f"{len(e['rows'])} 天，如 {list(e['rows'].items())[:3]}")

    # ── B 真实场景 ──
    n_raw = e["rows"].get(e["raw"], 0)
    if e["raw"] and e["effective"] and e["effective"] != e["raw"]:
        check("B 真实库里确实存在「半截日」", n_raw < e["min_rows"],
              f"raw={e['raw']} 仅 {n_raw} 行 ⇒ effective={e['effective']}")
    else:
        check("B 真实库里当前没有「半截日」（无法演示，跳过 B）", True,
              f"raw={e['raw']} rows={n_raw}")

    # ── C/D 用真实校验缓存构造 dv ──
    try:
        with open(CACHE, encoding="utf-8") as f:
            dv = json.load(f)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✘ 读不到校验缓存 {CACHE}: {exc}")
        return 1
    tables = dv.get("tables") or {}
    raw_dates = {t: str((tables.get(t) or {}).get("max_date") or "")
                 for t in RUN.GATE_CORE_TABLES}
    print(f"  校验缓存里的 max_date（**未修正**）：{raw_dates}")
    latest_td = max([d for d in raw_dates.values() if d] or [""]) or ""
    if not latest_td:
        print("  ✘ 缓存里没有可用日期")
        return 1

    # ── C/D：**打桩**构造"半截日"（不碰库、不依赖线上状态）──
    #   场景：扫描日 = GOOD；除 adj_factor 外各表都到 GOOD；adj_factor 却"领先"到 HALF，
    #   且 HALF 那天只有 10 行（半截）。这正是 §11.34.5 的原始病症。
    HALF, GOOD = "20260921", "20260918"
    dv_half = json.loads(json.dumps(dv))
    tb = dv_half.get("tables") or {}
    for t in RUN.GATE_CORE_TABLES:
        if tb.get(t):
            tb[t]["max_date"] = GOOD
    if tb.get("adj_factor"):
        tb["adj_factor"]["max_date"] = HALF
    orig_rows = RUN._count_rows_on
    eff_stub = _fake_eff(HALF, GOOD, {HALF: 10, GOOD: 5565}, 5000)

    def rows_stub(table, col, date_str):  # noqa: ANN001
        return 10 if date_str == HALF else orig_rows(table, col, date_str)

    print(f"  [打桩场景] 扫描日={GOOD}｜adj_factor 的 MAX={HALF}（仅 10 行）｜有效交易日={GOOD}")
    old = gate_with(dv_half, GOOD, use_effective=False, eff_stub=eff_stub, rows_stub=rows_stub)
    new = gate_with(dv_half, GOOD, use_effective=True, eff_stub=eff_stub, rows_stub=rows_stub)
    print(f"  关掉有效交易日（旧行为）：blocked={old['blocked']}｜reasons={old['reasons']}")
    print(f"  开启有效交易日（新行为）：blocked={new['blocked']}｜reasons={new['reasons']}")

    check("C 旧行为可复现（半截日 ⇒ 门禁把它判成『领先』而报错）", bool(old["blocked"]),
          f"reasons={old['reasons'][:1]}")
    check("D① 新行为**不再**把 adj_factor 判成『领先』（旧行为那条**误导性**结论）",
          not any("adj_factor" in r and "领先" in r for r in new["reasons"]),
          f"新 reasons={new['reasons'][:1]}")
    check("D② 新行为**如实**说明『最新日是半截日』并给出有效交易日",
          any("有效交易日" in w for w in new.get("warnings", [])),
          f"{[w for w in new.get('warnings', []) if '有效交易日' in w][:1]}")
    ed = DV.effective_latest_date("daily")
    check("F 完整表**不受影响**（daily 的 effective == raw）",
          bool(ed["raw"]) and ed["effective"] == ed["raw"],
          f"daily raw={ed['raw']} effective={ed['effective']}")
    print()
    print("  ★ C/D 断言建立在**打桩场景**上 ⇒ 无论线上数据是否恰好有半截日，本验收都成立；")
    print("    这也修掉了上一版的荒谬：数据一被修好，断言反而失败（见 docstring 的说明）。")

    # ── E 真落后仍拦得住（不能把门禁改松）──
    stale = json.loads(json.dumps(dv))
    for t in RUN.GATE_ANCHOR_TABLES:
        if (stale.get("tables") or {}).get(t):
            stale["tables"][t]["max_date"] = _minus_days(raw_dates.get(t) or "20260101", 30)
    st = gate_with(stale, latest_td, use_effective=True)
    check("E 锚定表真落后 ⇒ 仍然 blocked（门禁没被改松）", bool(st["blocked"]),
          f"reasons={st['reasons'][:1]}")

    print()
    print("=" * 100)
    print(f"结果：{len(PASS)} / {len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
    print("判读：")
    print("  1) 门禁从此按「**有效交易日**」比较 ⇒ 半截日不再制造假告警；")
    print("  2) 采集增量同时获得**自愈**能力（半截表不再被「已到最新」跳过）；")
    print("  3) 真落后（如 daily 落后）**仍然拦得住** ⇒ 这是修正，不是放松。")
    return 0 if not FAIL else 1


def _minus_days(d: str, n: int) -> str:
    import datetime as _dt
    try:
        t = _dt.datetime.strptime(d, "%Y%m%d") - _dt.timedelta(days=n)
        return t.strftime("%Y%m%d")
    except Exception:  # noqa: BLE001
        return d


if __name__ == "__main__":
    sys.exit(main())
