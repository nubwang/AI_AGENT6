"""验收：数据门禁的"交易日/节假日"口径 + 数据损坏识别（2026-09-20）

**为什么要这个脚本**：用户实测回测被拦，日志是

```text
核心表日期不一致：最新 920992.B，落后 daily(20260918), adj_factor(20260918), daily_basic(20260918)
```

`920992.B` 不是日期（是北交所代码 `920992.BJ` 的字典序最大值）→ 说明 `stk_limit` 的
`trade_date` 列里装的是 ts_code（列标签被调换）。而旧门禁把三件事混在一起：
① 偷懒用字符串 MAX 当日期 ② 要求所有核心表 max_date 完全相等（没考虑周末/节假日）
③ 报错信息无法定位问题。

本脚本验收新门禁的三条硬要求：

  A. **按交易日**判断落后/领先（周末、长假不计入）——含国庆长假的显式场景
  B. **stk_limit 允许领先 1 个交易日**（涨跌停价按次日披露）
  C. 日期值非法 → 直接判"数据损坏"并给出具体值（不再拿去比大小）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_data_gate_calendar.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import runner as R

PASS, FAIL = [], []


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


# ── 合成交易日历（注入用，避免真实日历变化导致断言不稳）──
# 2026-09-18(五) 开市；09-19/20 周末休；09-21/22/23/24/25 开市；09-26/27 周末休
# 国庆：10-01 ~ 10-08 休市；10-09(五) 开市
SYN_DAYS = ["20260918", "20260921", "20260922", "20260923", "20260924", "20260925", "20260930",
            "20261009"]


def _inject_synthetic() -> None:
    """把 runner 里的交易日辅助函数替换成合成日历版本。"""
    R._latest_trade_date = lambda today="": max(  # type: ignore[assignment]
        [d for d in SYN_DAYS if not today or d <= today] or [""])
    R._count_trade_days = lambda a, b: (  # type: ignore[assignment]
        0 if (not a or not b or a >= b) else len([d for d in SYN_DAYS if a < d <= b]))
    R._next_trade_date = lambda a: next(  # type: ignore[assignment]
        (d for d in SYN_DAYS if d > a), "")
    # ★「有效交易日」探针**必须一起打桩**：门禁会实时查库算有效交易日，不隔离的话
    #   注入的 `dv` 快照会被真实库覆盖（2026-09-28 实测：stk_limit 半采 2361 行
    #   ⇒ 有效交易日被改判成 20260923 ⇒ A1/A3/B1/D2/D3 五项假红，与代码无关）。
    #   返回空 effective = "不动注入值"，正是本套用例想要的语义。
    R.effective_latest_probe = lambda t, *a, **k: (  # type: ignore[assignment]
        {"raw": "", "effective": "", "rows": {}, "min_rows": 5000})


def _dv(daily: str, adj: str, basic: str, limit: str, overall: str = "warning") -> dict:
    return {
        "overall": overall,
        "tables": {
            "daily": {"exists": True, "max_date": daily},
            "adj_factor": {"exists": True, "max_date": adj},
            "daily_basic": {"exists": True, "max_date": basic},
            "stk_limit": {"exists": True, "max_date": limit},
        },
    }


def main() -> int:
    _inject_synthetic()

    print("=== A 按交易日口径（周末/节假日不计入）===")
    # 周末场景：最近交易日=周五 20260918（今天正好周末），四表同日 → 必须放行
    g = R._check_data_gate(_dv("20260918", "20260918", "20260918", "20260918"),
                           today="20260920")
    ck("A1 周末：四表=最近交易日(周五) → 放行（不再因为'不是今天'报错）", not g["blocked"],
       f"latest_td={g['latest_trade_date']}")

    # 落后 1 个交易日（周四）→ 拦截，且必须说明是"交易日"口径
    g = R._check_data_gate(_dv("20260917", "20260917", "20260917", "20260917"), today="20260920")
    ck("A2 落后 1 个交易日 → 拦截", g["blocked"], "；".join(g["reasons"])[:90])
    ck("A2b 报错文案说明按交易日计（周末/节假日不计入）",
       any("交易日" in r and ("周末" in r or "节假日" in r) for r in g["reasons"]))

    # 长假场景：国庆前最后交易日 09-30；今天是 10-03（假期中）→ 09-30 就是"最近交易日" → 放行
    g = R._check_data_gate(_dv("20260930", "20260930", "20260930", "20260930"), today="20261003")
    ck("A3 国庆假期中：表=节前最后交易日 → 放行（不按自然日算落后）", not g["blocked"],
       f"latest_td={g['latest_trade_date']}")

    # 长假后第一天：最近交易日=10-09，表还停在 09-30 → 落后 1 个交易日 → 拦截
    g = R._check_data_gate(_dv("20260930", "20260930", "20260930", "20260930"), today="20261009")
    ck("A4 节后首日：仍停在节前 → 拦截（落后 1 个交易日）", g["blocked"],
       "；".join(g["reasons"])[:80])

    print("=== B stk_limit 允许领先（次日涨跌停价）===")
    g = R._check_data_gate(_dv("20260918", "20260918", "20260918", "20260921"), today="20260920")
    ck("B1 stk_limit 领先 1 个交易日 → 放行", not g["blocked"], "；".join(g["reasons"])[:80])
    g = R._check_data_gate(_dv("20260918", "20260918", "20260918", "20260924"), today="20260920")
    ck("B2 stk_limit 领先 4 个交易日 → 拦截", g["blocked"], "；".join(g["reasons"])[:80])
    g = R._check_data_gate(_dv("20260921", "20260921", "20260921", "20260918"), today="20260922")
    ck("B3 stk_limit 落后锚定表 → 拦截（不允许落后）", g["blocked"],
       "；".join(g["reasons"])[:80])

    print("=== C 数据损坏识别（本次真实故障）===")
    g = R._check_data_gate(_dv("20260918", "20260918", "20260918", "920992.BJ"), today="20260920")
    ck("C1 日期非法（本例 stk_limit=920992.BJ 列错位）→ 拦截", g["blocked"])
    ck("C1b 文案明确指出『不是合法 YYYYMMDD』并给出原值",
       any("不是合法 YYYYMMDD" in r and "920992.BJ" in r for r in g["reasons"]),
       "；".join(g["reasons"])[:110])
    ck("C1c 不再出现『最新 920992.B』这种拿非法值比大小的结论",
       not any("最新 920992" in r for r in g["reasons"]))

    print("=== D 锚定表一致性 & overall 降级 ===")
    g = R._check_data_gate(_dv("20260918", "20260917", "20260918", "20260918"), today="20260920")
    ck("D1 锚定表内部不对齐（daily vs adj_factor）→ 拦截", g["blocked"],
       "；".join(g["reasons"])[:90])
    g = R._check_data_gate(_dv("20260918", "20260918", "20260918", "20260918", overall="error"),
                           today="20260920")
    ck("D2 overall=error 只作 warning（门禁已完成结构校验，不被历史缓存一票否决）",
       not g["blocked"] and any("overall=error" in w for w in g.get("warnings", [])),
       f"blocked={g['blocked']} warnings={g.get('warnings')}")

    print("=== D2b 覆盖率抽查（日期最新 ≠ 数据完整）===")
    R._count_rows_on = lambda t, c, d: 2361 if t == "stk_limit" else 5565  # type: ignore[assignment]
    g = R._check_data_gate(_dv("20260918", "20260918", "20260918", "20260918"), today="20260920")
    ck("D3 最新交易日 stk_limit 行数远低于 daily → 出 warning（不阻塞回测）",
       not g["blocked"] and any("涨跌停价数据不完整" in w for w in g.get("warnings", [])),
       next((w for w in g.get("warnings", []) if "stk_limit" in w), "无相关 warning"))
    R._count_rows_on = lambda t, c, d: 5560 if t == "stk_limit" else 5565  # type: ignore[assignment]
    g = R._check_data_gate(_dv("20260918", "20260918", "20260918", "20260918"), today="20260920")
    ck("D4 覆盖率正常 → 不出该 warning",
       not any("涨跌停价数据不完整" in w for w in g.get("warnings", [])))

    print("=== E 真实日历自证（不注入，直接查 trade_cal）===")
    import importlib
    R2 = importlib.reload(R)   # 丢掉注入，回到真实实现
    import datetime as _dt
    sat = "20260919"           # 周六
    lt_sat = R2._latest_trade_date(sat)
    ck("E1 周六查最近交易日 → 不是周六本身（跳过双休）", bool(lt_sat) and lt_sat < sat,
       f"latest_trade_date({sat})={lt_sat}")
    ck("E2 周末两天之间不算交易日 E0 次",
       R2._count_trade_days("20260918", "20260920") == 0,
       f"20260918→20260920 交易日数={R2._count_trade_days('20260918', '20260920')}")
    # 与 trade_cal 直接统计自证一致（不写死期望值，随真实日历变化）
    from sqlalchemy import text as _t
    from app.models import SessionLocal
    db = SessionLocal()
    try:
        exp = int(db.execute(_t("SELECT COUNT(*) FROM trade_cal WHERE is_open=1 "
                                "AND cal_date > '20260918' AND cal_date <= '20260925'")).scalar() or 0)
    finally:
        db.close()
    got = R2._count_trade_days("20260918", "20260925")
    ck("E3 交易日计数与 trade_cal 直接统计一致", got == exp, f"函数={got} 直查={exp}")
    # 国庆长假：10-01~10-08 若都是休市，则 09-30→10-09 之间交易日数应远小于自然日数(9)
    holiday_cnt = R2._count_trade_days("20260930", "20261009")
    ck("E4 国庆长假区间按交易日计（远小于 9 个自然日）", 0 <= holiday_cnt <= 3,
       f"20260930→20261009 交易日数={holiday_cnt}（自然日 9）")

    print(f"\n===== 结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
