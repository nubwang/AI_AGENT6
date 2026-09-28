"""自证测试的数据门与幸存者偏差检查（selfproof_data_gate）— plans/25 P0.5 / §十三 F7·F10

## 为什么必须**先测边界**再谈自证

用户的想法是"按大盘走势挑相似的其他年份，做全市场选股验证"。但在动手之前必须先回答两个问题：

1. **能自证到多早？** 各表的 `MIN(date)` 不一样（有的表只有近几年）→ 早于"最早可自证日期"的回放
   会大面积 NaN，结论没有意义（F10）。这个日期必须**先算出来**，而不是跑完一年才发现。
2. **股票池有没有幸存者偏差？** 今天的 `stock_basic` 只有**当前存续**的股票 —— 回放 2018 年会
   自动排除那之后退市的公司，等于"只从活到今天的公司里挑" → 系统性乐观（F7）。

本模块因此提供：
  - `table_spans()`  各表数据边界（MIN/MAX 日期 + 行数近似）
  - `data_gate()`    「最早可自证日期」+ 关键表缺失清单 + verdict
  - `survivorship()` 幸存者偏差量化（**实算**：历史区间出现过的股票集合 − 今日存续集合 = 退市缺口）
  - `latest_as_of()` 可回放的最新交易日

## 口径诚实

- `rows` 来自 `information_schema.TABLE_ROWS`，MyISAM/InnoDB 下都是**近似值**（已在输出里标注）。
- 幸存者偏差的"退市缺口"用 `daily` 表区间 DISTINCT 与 `stock_basic` 做差 —— 这是**真实可算**的，
  不是估计值；但计算量较大，默认由 `deep=False` 跳过（CLI 用 `--deep` 打开）。
"""
from __future__ import annotations

import os

from sqlalchemy import text

from app.core.logger import logger

# (表名, 日期列, 用途, 是否关键)
# 「关键」= 缺失/起点太晚会直接影响回放结论，必须进「最早可自证日期」的取最晚值。
REQUIRED_TABLES: list[tuple[str, str, str, bool]] = [
    ("daily", "trade_date", "日线行情：价格与涨跌幅的根基", True),
    ("adj_factor", "trade_date", "复权因子：前复权基准（回放须锚在 as_of）", True),
    ("stk_limit", "trade_date", "涨跌停价：可成交性判定（一字板不可买）", True),
    ("daily_basic", "trade_date", "换手率/市值/估值等形态特征", True),
    ("trade_cal", "cal_date", "交易日历：as_of 序列与 T+N 结算", True),
    ("index_daily", "trade_date", "指数日线：环境三态与相似波段", True),
    ("moneyflow", "trade_date", "资金流向特征", False),
    ("weekly", "trade_date", "周线趋势特征", False),
    ("monthly", "trade_date", "月线趋势特征", False),
    ("block_trade", "trade_date", "大宗交易折溢价", False),
    ("fina_indicator", "ann_date", "财务指标（bps/eps/roe）", False),
    ("income", "ann_date", "营收（同比）", False),
    ("balancesheet", "ann_date", "资产负债率", False),
    ("stk_holdernumber", "ann_date", "股东人数/人均持股/筹码集中度", False),
    ("top10_holders", "ann_date", "前十大股东占比", False),
    ("pledge_stat", "end_date", "质押比例（风险过滤）", False),
    ("namechange", "start_date", "更名历史（ST 识别）", False),
    ("stock_basic", "list_date", "股票池（含 list/delist_date）", True),
]


def _norm(v) -> str:
    """日期归一化为 YYYYMMDD（空值返回 ''）。"""
    if v is None:
        return ""
    s = str(v).strip().split(" ")[0].replace("-", "")
    return s if len(s) == 8 and s.isdigit() else str(v)[:10]


def columns_of(db, table: str) -> set[str]:
    """实际表列名集合（**必须看真实 schema**：ORM 模型声明的列未必在库里）。"""
    try:
        rows = db.execute(
            text("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                 "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"), {"t": table}
        ).scalars().all()
        return {str(c) for c in rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[selfproof_data_gate] 取 {table} 列名失败: {exc}")
        return set()


def table_spans(db) -> dict:
    """各表数据边界。返回 {table: {"min","max","rows","rows_approx":True}|{"error"}}。"""
    out: dict = {}
    for table, col, why, critical in REQUIRED_TABLES:
        try:
            row = db.execute(text(f"SELECT MIN({col}), MAX({col}) FROM {table}")).first()
            rows = db.execute(
                text("SELECT TABLE_ROWS FROM information_schema.TABLES "
                     "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"),
                {"t": table},
            ).scalar()
            out[table] = {
                "date_col": col, "why": why, "critical": critical,
                "min": _norm(row[0] if row else None),
                "max": _norm(row[1] if row else None),
                "rows": int(rows or 0), "rows_approx": True,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[selfproof_data_gate] {table} 统计失败: {exc}")
            out[table] = {"date_col": col, "why": why, "critical": critical,
                          "error": str(exc)[:160]}
    return out


def data_gate(db, as_of: str = "") -> dict:
    """数据门：输出「最早可自证日期」+ 关键表缺失清单 + verdict。

    `earliest_full_as_of` = **关键表** min(date) 的最大值（所有关键数据都齐的最早一天）。
    非关键表缺失只记 warning（会缺哪些特征）。
    """
    spans = table_spans(db)
    critical, optional, missing = [], [], []
    for t, s in spans.items():
        if s.get("error") or not s.get("min"):
            missing.append({"table": t, "why": s.get("why", ""), "critical": s.get("critical")})
            continue
        row = {"table": t, "min": s["min"], "max": s["max"], "rows": s["rows"],
               "date_col": s["date_col"]}
        (critical if s.get("critical") else optional).append(row)

    earliest_full = max([r["min"] for r in critical], default="")
    latest = min([r["max"] for r in critical], default="")
    late = [r for r in critical if earliest_full and r["min"] > _p50([x["min"] for x in critical])]

    verdict, notes = "ok", []
    if missing:
        crit_missing = [m for m in missing if m.get("critical")]
        verdict = "blocked" if crit_missing else "warn"
        notes.append("关键表缺失：" + ", ".join(m["table"] for m in crit_missing)
                     if crit_missing else
                     "非关键表缺失：" + ", ".join(m["table"] for m in missing))
    if late:
        notes.append("起点明显偏晚（会把最早可自证日拖后）："
                     + ", ".join(f"{r['table']}({r['min']})" for r in late))
    return {
        "as_of": _norm(as_of),
        "earliest_full_as_of": earliest_full,
        "latest_as_of": latest,
        "critical": critical,
        "optional": optional,
        "missing": missing,
        "verdict": verdict,
        "notes": notes,
        "rows_approx": True,
        "howto": "回放 as_of < earliest_full_as_of 时应拒绝或降级（并在报告里写明缺哪些特征）",
    }


def _p50(vals: list[str]) -> str:
    vals = sorted(v for v in vals if v)
    return vals[len(vals) // 2] if vals else ""


def latest_as_of(db) -> str:
    """可回放的最新交易日（以 daily 表为准）。"""
    try:
        v = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
        return _norm(v)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[selfproof_data_gate] 取最新交易日失败: {exc}")
        return ""


def survivorship(db, as_of: str, deep: bool = False) -> dict:
    """幸存者偏差量化（**实算缺口**，并在 schema 层暴露根因）。

    核心口径：`daily` 全历史出现过的 ts_code 数 − `stock_basic` 今日存续数 = **退市缺口**。
    这是自证测试最硬的偏差来源：股票池只有"活到今天的公司"，回放历史等于只从幸存者里挑。

    另外检查 `stock_basic` 是否真的具备 `list_status` / `delist_date` 列 ——
    实测本项目**没有这两列**（Tushare 默认只返回上市中股票，退市股需 `list_status='D'` 单独采），
    因此缺口是**结构性**的：不补数据就只能"如实标注"，不能靠报告糊过去。
    """
    as_of = _norm(as_of)
    out: dict = {
        "as_of": as_of, "pool_size": None, "listed_after_as_of": None,
        "hist_codes_all": None, "gap_estimate": None, "gap_pct": None,
        "deep": bool(deep),
        "warning": "股票池 = 当前存续股票（stock_basic）→ 回放历史存在幸存者偏差："
                   "已退市公司不会被选出，结论偏乐观。",
        "mitigation": [
            "回放时剔除 list_date > as_of 的股票（避免'未来上市'混入）",
            "报告强制标注缺口数量与占比；结论里避免与'当年真实榜单'直接比较",
            "根治：补采退市股（Tushare stock_basic list_status='D'/'P'）+ 给表加 list_status/delist_date 列，"
            "再用 as-of 股票池回放（当前为已知限制，见 structural_warning）",
        ],
    }
    cols = columns_of(db, "stock_basic")
    out["stock_basic_columns"] = sorted(cols)
    out["has_list_status"] = "list_status" in cols
    out["has_delist_date"] = "delist_date" in cols
    if not out["has_delist_date"]:
        out["structural_warning"] = (
            "stock_basic **没有 delist_date/list_status 列**（ORM 模型声明了 delist_date，但表里不存在）"
            "→ 退市股从未被采集 ⇒ 幸存者偏差是**结构性**的，无法靠报告标注消除，只能靠补数据根治。"
        )
    try:
        out["pool_size"] = int(db.execute(text("SELECT COUNT(*) FROM stock_basic")).scalar() or 0)
        out["listed_after_as_of"] = int(db.execute(
            text("SELECT COUNT(*) FROM stock_basic WHERE list_date > :d"), {"d": as_of}
        ).scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:160]
        return out

    try:
        hist_all = int(db.execute(text("SELECT COUNT(DISTINCT ts_code) FROM daily")).scalar() or 0)
        pool = int(out["pool_size"] or 0)
        out["hist_codes_all"] = hist_all
        out["gap_estimate"] = max(hist_all - pool, 0)
        out["gap_pct"] = round(out["gap_estimate"] / hist_all * 100, 2) if hist_all else None
    except Exception as exc:  # noqa: BLE001
        out["gap_error"] = str(exc)[:160]

    if deep:
        try:
            hist = int(db.execute(
                text("SELECT COUNT(DISTINCT ts_code) FROM daily WHERE trade_date <= :d"),
                {"d": as_of},
            ).scalar() or 0)
            out["hist_codes_upto_as_of"] = hist
            out["gap_upto_as_of"] = max(hist - int(out["pool_size"] or 0), 0)
        except Exception as exc:  # noqa: BLE001
            out["deep_error"] = str(exc)[:160]
    return out


def run(db, as_of: str = "", deep: bool = False) -> dict:
    """一次跑完数据门 + 幸存者偏差（供 CLI 与报告共用）。"""
    a = _norm(as_of) or latest_as_of(db)
    g = data_gate(db, as_of=a)
    s = survivorship(db, as_of=a, deep=deep)
    return {"as_of": a, "data_gate": g, "survivorship": s,
            "usage_hint": "报告请把本结果塞进 selfproof_policy.report_flags(data_gate=..., survivorship=...)"}


__all__ = ["REQUIRED_TABLES", "table_spans", "data_gate", "latest_as_of", "survivorship", "run"]
