"""验收：stage2 按日表的「半截日自愈」（2026-09-28 真实事故：stk_limit 残缺把回测卡死）

## 事故链条（每一环都有实测证据）

    ① stk_limit 半采：最新日只有沪市 2361 行（正常日 5647 行 = 全市场）
       —— 000/001/002/003/200/201/300/301/302/920 整块缺失，600/601/603/605/688/689/900 全在
       （见 scripts/diagnose_stk_limit_gap.py）
    ② stage2 的增量判据是 `date <= MAX(trade_date)`，而 MAX 已经是 20260928
       ⇒ 那些残缺日**永远不在重建集合里** ⇒ **残缺永不自愈**
    ③ 门禁的「有效交易日」把"最新日行数不足"回退成 20260923
       ⇒ 报出"stk_limit 落后锚定表 2 个交易日"⇒ 回测被中止

本脚本验收的是**第②环**的修复：`stage2_incomplete_days()` 必须检出半截日，
且 `stage2_daily()` 必须**无视增量边界**把残缺日重排进队列。

## 口径（与 verify_effective_trade_day.py 同一套哲学）

断言**建立在打桩场景上** ⇒ 无论线上此刻有没有半截日，本验收都成立。
否则会出现"数据一被修好，验收反而变红"的荒谬结果。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_stage2_halfday_selfheal.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from app.api import ws as W  # noqa: E402
from app.backtest import data_validator as DV  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS: list[str] = []
FAIL: list[str] = []


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _fake_eff(rows: dict, min_rows: int = 5000):
    """打桩 `effective_latest_date`：给定 {date: n} 的伪造逐日行数，不查库。"""
    def f(table, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return {"raw": max(rows) if rows else "", "effective": "", "rows": dict(rows),
                "min_rows": int(k.get("min_rows") or min_rows)}
    return f


def main() -> int:
    print("=" * 100)
    print("stage2 半截日自愈验收")
    print("=" * 100)

    print("\n① 白名单口径：只含'自然基数 = 个股只数'的按日表")
    print("-" * 100)
    ck("A1 stk_limit 在白名单内（本次事故主角）", "stk_limit" in W.STAGE2_BASE_ROWS)
    ck("A2 daily / daily_basic / moneyflow 同样覆盖",
       {"daily", "daily_basic", "moneyflow"} <= set(W.STAGE2_BASE_ROWS))
    # suspend_d（每日 10~20 只）/ index_daily（指数 ~1070）的自然基数 ≠ 个股数：
    # 套 5000 会**误报**成半截日，进而每轮都白跑一遍采集 ⇒ 必须不在白名单
    ck("A3 suspend_d / index_daily **不得**进白名单（否则误报、白烧配额、白烧额度）",
       not ({"suspend_d", "index_daily", "index_dailybasic"} & set(W.STAGE2_BASE_ROWS)))
    ck("A4 回看窗口有界（避免无限追补陈年残缺日）",
       0 < int(W.STAGE2_HEAL_DAYS) <= 30, f"STAGE2_HEAL_DAYS={W.STAGE2_HEAL_DAYS}")

    print("\n② 打桩：半截日必须被检出（不依赖线上数据）")
    print("-" * 100)
    rows = {"20260928": 2361, "20260924": 2361, "20260923": 5647}
    with mock.patch.object(DV, "effective_latest_date", side_effect=_fake_eff(rows)):
        got = W.stage2_incomplete_days(["stk_limit", "daily"])
    ck("B1 检出 20260928 / 20260924 为半截日",
       got.get("stk_limit") == {"20260928", "20260924"},
       f"got={ {k: sorted(v) for k, v in got.items()} }")
    ck("B2 完整日（5647 行）**不得**被误判为半截日", "20260923" not in (got.get("stk_limit") or set()))

    print("\n③ 打桩：全市场都完整时不得产生任何自愈任务（防空转）")
    print("-" * 100)
    rows_ok = {"20260928": 5647, "20260924": 5646}
    with mock.patch.object(DV, "effective_latest_date", side_effect=_fake_eff(rows_ok)):
        got_ok = W.stage2_incomplete_days(["stk_limit"])
    ck("C1 数据完整 → 自愈集合为空", got_ok == {}, f"got={got_ok}")

    print("\n④ 检测失败**绝不能**阻断采集（降级为不支持自愈，而不是抛异常）")
    print("-" * 100)
    with mock.patch.object(DV, "effective_latest_date", side_effect=RuntimeError("boom")):
        got_err = W.stage2_incomplete_days(["stk_limit"])
    ck("D1 查询异常 → 返回空集合，不抛异常", got_err == {}, f"got={got_err}")

    print("\n⑤ 防漂移：stage2 的跳过判据必须**尊重**自愈集合（源码级断言）")
    print("-" * 100)
    src = open(os.path.join(BACKEND, "app", "api", "ws.py"), encoding="utf-8").read()
    ck("E1 stage2_daily 调用了 stage2_incomplete_days",
       "stage2_incomplete_days" in src)
    # 关键：跳过判据必须是 `date <= max_existing and not need_heal`
    # （即自愈日能穿过增量边界；若有人改回只看增量，本验收立刻变红）
    ck("E2 自愈日能穿过增量边界（`date <= max_existing and not need_heal`）",
       re.search(r"if date <= max_existing and not need_heal:", src) is not None)
    ck("E3 自愈日有显式标记，便于在采集进度里辨认",
       "半截日自愈" in src)

    print("\n⑥ 线上实况（**仅供参考，不作为断言**）")
    print("-" * 100)
    live = W.stage2_incomplete_days([a[0] for a in (
        ("daily", "", ""), ("daily_basic", "", ""), ("moneyflow", "", ""), ("stk_limit", "", ""))])
    if live:
        print("  当前库中检出半截日：" + "；".join(f"{k}={sorted(v)}" for k, v in sorted(live.items())))
        print("  ⇒ 下一次采集会自动重排这些日期（幂等 upsert，走正常链路）")
    else:
        print("  当前库中未检出半截日 ✅")

    print("\n" + "=" * 100)
    print(f"结果：{len(PASS)} / {len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
    print("判读：修复前 stage2 的按日表**完全在'半截日自愈'覆盖之外**（该机制此前只覆盖")
    print("      stage3 的 adj_factor/weekly）⇒ stk_limit 这类残缺永不重建、永不修好；")
    print("      修复后残缺日会被强制重排，且检测失败不影响采集。")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
