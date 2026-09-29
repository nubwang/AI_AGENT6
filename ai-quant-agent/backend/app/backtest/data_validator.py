"""数据完整性校验（data_validator）

对齐规划：plans/04-回测引擎.md 3.9（采集前必须通过）与第十四章第 1 项
  目标：在回测/推荐前确认"原材料"完整，避免因缺表/缺数据导致回测结论失真。

  校验内容：
    1. 表存在性（information_schema，O(1)）
    2. 表行数（TABLE_ROWS 近似值，O(1)）
    3. 日期范围（MIN/MAX，核心表必须覆盖 2010 至今）
    4. 股票覆盖率（COUNT(DISTINCT ts_code) vs 全市场股票数）
    5. 输出 per-table 状态 + 问题清单 + 总体结论（ok / warning / error）

  层级（tier）：
    core      ：回测硬依赖（daily/adj_factor/suspend_d/stk_limit），缺则回测结果不可信
    important ：高频增强维度（daily_basic/moneyflow/财务/筹码），缺失则特征大量为 NaN
    optional  ：低频/板块/指数，缺失影响小
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from sqlalchemy import text

from app.models import SessionLocal
from app.core.logger import logger

# 回测要求的数据起始（2010 年至今）
DEFAULT_START = "20100101"
# 日期覆盖允许的误差天数（个别表从某月中途开始）
DATE_SLACK_DAYS = 45
# 核心表行数下限（daily 至少应有近 100 万行级别的量级；此处给出保守下限防"假存在"）
CORE_MIN_ROWS = {
    "daily": 500_000,
    "adj_factor": 500_000,
    "stk_limit": 400_000,
    "suspend_d": 10_000,
}
# 超过该行数的表，覆盖度检查跳过 COUNT(DISTINCT) 精确统计（数百万行全索引扫描极慢，
# 实测 daily/adj_factor/stk_limit/daily_basic/moneyflow 等 5 张大表累计 ~100s，
# 拖慢回测首次数据校验）；大表以存在性/行数下限/日期覆盖兜底，返回 -1 哨兵表示"大表免检"
COVERAGE_SKIP_ROWS = 1_000_000

STATUS_OK = "ok"
STATUS_WARN = "warning"
STATUS_ERROR = "error"

# 校验结果磁盘缓存（避免每次回测重复全表扫描；数据采集低频，TTL 内复用）
# 路径：backend/data/validation_cache.json（与 chroma_db 同级）
CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "validation_cache.json",
)
CACHE_TTL = 6 * 3600  # 6 小时


@dataclass
class TableSpec:
    """表校验配置。"""
    table: str
    desc: str
    tier: str                      # core / important / optional
    date_col: str = ""             # 空 = 无日期列（只查存在性与行数）
    key_col: str = "ts_code"       # 股票列（覆盖率用）


# 全量表注册表（对齐 models/stock.py 与 01b-MySQL建表方案）
TABLE_SPECS: list[TableSpec] = [
    # ── 核心行情（回测硬依赖）──
    TableSpec("daily", "日线行情", "core", "trade_date"),
    TableSpec("adj_factor", "复权因子", "core", "trade_date"),
    TableSpec("stk_limit", "涨跌停价格", "core", "trade_date"),
    TableSpec("suspend_d", "停复牌信息", "core", "trade_date"),
    # ── 每日指标 / 资金 ──
    TableSpec("daily_basic", "每日指标", "important", "trade_date"),
    TableSpec("moneyflow", "个股资金流向", "important", "trade_date"),
    # ── 财务面 ──
    TableSpec("income", "利润表", "important", "end_date"),
    TableSpec("balancesheet", "资产负债表", "important", "end_date"),
    TableSpec("cashflow", "现金流量表", "important", "end_date"),
    TableSpec("fina_indicator", "财务指标", "important", "end_date"),
    TableSpec("fina_mainbz", "主营业务构成", "important", "end_date"),
    TableSpec("forecast", "业绩预告", "important", "end_date"),
    TableSpec("express", "业绩快报", "important", "end_date"),
    # ── 筹码面 ──
    TableSpec("stk_holdernumber", "股东人数", "important", "end_date"),
    TableSpec("top10_holders", "前十大股东", "important", "end_date"),
    TableSpec("top10_floatholders", "前十大流通股东", "important", "end_date"),
    TableSpec("stk_holdertrade", "股东增减持", "important", "end_date"),
    TableSpec("repurchase", "股票回购", "important", "end_date"),
    TableSpec("stk_rewards", "股权激励", "optional", "end_date"),
    TableSpec("dividend", "分红送配", "optional", "ex_date"),
    # ── 事件 / 板块 / 融资 / 龙虎榜 ──
    TableSpec("new_share", "新股上市", "optional", "ipo_date"),
    TableSpec("namechange", "股票曾用名", "optional", "start_date"),
    TableSpec("concept", "概念板块", "optional", ""),
    TableSpec("concept_detail", "概念成分股", "optional", ""),
    TableSpec("ths_index", "同花顺概念", "optional", ""),
    TableSpec("ths_daily", "同花顺日线", "optional", "trade_date"),
    TableSpec("ths_member", "同花顺成分", "optional", ""),
    TableSpec("margin", "融资融券汇总", "optional", "trade_date"),
    TableSpec("margin_detail", "融资融券明细", "optional", "trade_date"),
    TableSpec("limit_list", "龙虎榜", "optional", "trade_date"),
    TableSpec("hsgt", "沪深港通持股", "optional", "trade_date"),
    TableSpec("pledge_stat", "股权质押", "optional", "end_date"),
    # ── 指数 ──
    TableSpec("index_basic", "指数基本信息", "optional", ""),
    TableSpec("index_daily", "指数日线", "optional", "trade_date"),
    TableSpec("index_dailybasic", "大盘指数每日指标", "optional", "trade_date"),
    TableSpec("index_weight", "指数成分权重", "optional", ""),
]


@dataclass
class TableCheckResult:
    """单表校验结果。"""
    table: str
    desc: str
    tier: str
    exists: bool = False
    rows: int = 0
    min_date: str = ""
    max_date: str = ""
    coverage_pct: float = 0.0       # 股票覆盖率（%）
    status: str = STATUS_WARN

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "desc": self.desc,
            "tier": self.tier,
            "exists": self.exists,
            "rows": self.rows,
            "min_date": self.min_date,
            "max_date": self.max_date,
            # 大表免检时 coverage_pct == -1，展示为 null（语义：跳过精确覆盖度统计）
            "coverage_pct": None if self.coverage_pct < 0 else round(self.coverage_pct, 1),
            "status": self.status,
        }


def _table_exists(table: str) -> bool:
    db = SessionLocal()
    try:
        r = db.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": table},
        ).fetchone()
        return bool(r and r[0] > 0)
    except Exception:  # noqa: BLE001
        return False
    finally:
        db.close()


def _table_rows(table: str) -> int:
    """近似行数（information_schema，O(1)）。"""
    db = SessionLocal()
    try:
        r = db.execute(
            text(
                "SELECT table_rows FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            ),
            {"t": table},
        ).fetchone()
        return int(r[0]) if r and r[0] is not None else 0
    except Exception:  # noqa: BLE001
        return 0
    finally:
        db.close()


def _date_bounds(table: str, date_col: str) -> tuple[str, str]:
    """查询 MIN/MAX 日期（str YYYYMMDD）。失败返回 ('','')。"""
    db = SessionLocal()
    try:
        r = db.execute(text(f"SELECT MIN({date_col}), MAX({date_col}) FROM {table}")).fetchone()
        if not r or r[0] is None or r[1] is None:
            return "", ""
        def _fmt(d):
            if hasattr(d, "strftime"):
                return d.strftime("%Y%m%d")
            s = str(d)
            return s.replace("-", "")[:8] if len(s) >= 8 else s
        return _fmt(r[0]), _fmt(r[1])
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"date_bounds({table}.{date_col}) 失败: {exc}")
        return "", ""
    finally:
        db.close()


def _stock_coverage(table: str, key_col: str = "ts_code", total_stocks: int = 0, rows: int = 0) -> float:
    """表内股票数 / 全市场股票数（%）。

    性能优化：对超大表（rows > COVERAGE_SKIP_ROWS）跳过 COUNT(DISTINCT) 精确统计，
    直接返回 -1 哨兵（"大表免检"，存在性/行数下限/日期覆盖仍在判定层兜底）。
    COUNT(DISTINCT) 在千万级行表上是全索引扫描，会把回测首次数据校验拖慢到分钟级。
    """
    if rows > COVERAGE_SKIP_ROWS:
        return -1.0
    db = SessionLocal()
    try:
        r = db.execute(text(f"SELECT COUNT(DISTINCT {key_col}) FROM {table}")).fetchone()
        n = int(r[0]) if r and r[0] is not None else 0
        if total_stocks <= 0:
            return float(n)
        return float(n) / total_stocks * 100.0
    except Exception:  # noqa: BLE001
        return 0.0
    finally:
        db.close()


def _total_stocks() -> int:
    db = SessionLocal()
    try:
        r = db.execute(text("SELECT COUNT(*) FROM stock_basic")).fetchone()
        return int(r[0]) if r else 0
    except Exception:  # noqa: BLE001
        return 0
    finally:
        db.close()


# ──────────────────────── ★ 有效交易日（plans/24 §11.34.5 / §11.35.4）────────────────────────
# 背景（本轮实测的两个真实缺陷，**同一个病根**）：
#   ① 数据门禁（runner） 只看 `MAX(trade_date)` ⇒ 一个 **443 行的"半截日"** 就足以
#      把一个表判成"领先"，把门禁拖进"永远不通过"，而且**报出来的原因会误导排查方向**；
#   ② 采集增量（ws）判据是「该股在该表**有没有**这一天任意一行」⇒ 一旦写进 1 行，
#      该股就被**永久跳过** ⇒ 残缺**永不自愈**（实测：16:15 整份采集后 daily 补齐了，
#      adj_factor 一行没动）。
# ⇒ 两处共用同一个「有效交易日」定义：「**行数 ≥ 自然基数下限**的最近日期」。
BASE_ROWS_MIN: dict[str, int] = {
    "daily": 5000, "daily_basic": 5000, "adj_factor": 5000,
    "moneyflow": 5000, "stk_limit": 5000,
}
EFF_DAYS = 15          # 只看最近 N 个"出现过的日期"，避免大表全表扫描
EFF_MIN_ROWS = 5000    # 未知表的兜底下限（个股表自然基数 ~5550）


def effective_latest_date(table: str, min_rows: int | None = None,
                          date_col: str = "trade_date", days: int = EFF_DAYS) -> dict:
    """最近一个**行数达标**的日期（"有效交易日"）。

    Returns:
        {"raw", "effective", "rows": {date: n}, "min_rows"}
        查不到 / 出错时 raw 与 effective 均为 `""`（**不抛异常**，由调用方按"查不到"处理）。
    """
    out: dict = {"raw": "", "effective": "", "rows": {},
                 "min_rows": int(min_rows or BASE_ROWS_MIN.get(table, EFF_MIN_ROWS))}
    try:
        db = SessionLocal()
        try:
            rows = db.execute(
                text(f"SELECT {date_col}, COUNT(*) FROM {table} "
                     f"GROUP BY {date_col} ORDER BY {date_col} DESC LIMIT :k"),
                {"k": int(days)},
            ).fetchall()
        finally:
            db.close()
        pairs = [(str(r[0]), int(r[1])) for r in rows if r[0] is not None]
        if not pairs:
            return out
        out["rows"] = dict(pairs)
        out["raw"] = pairs[0][0]                      # 已按日期倒序
        for d, n in pairs:
            if n >= out["min_rows"]:
                out["effective"] = d
                break
    except Exception:  # noqa: BLE001
        pass
    return out


def _judge(spec: TableSpec, exists: bool, rows: int, min_date: str, max_date: str,
           coverage_pct: float, start_date: str) -> str:
    """判定单表状态。

    分级语义：
      core      ：回测硬依赖，缺失/日期不覆盖 = error（阻塞回测）
      important ：增强维度，缺失 = warning（特征不全但不阻塞核心回测）
      optional  ：低频/板块/指数，缺失 = warning（影响小）
    """
    if not exists:
        return STATUS_ERROR if spec.tier == "core" else STATUS_WARN
    if rows == 0:
        return STATUS_ERROR if spec.tier == "core" else STATUS_WARN

    # 核心表必须覆盖 2010 至今
    if spec.tier == "core" and spec.date_col:
        if not min_date or not max_date:
            return STATUS_ERROR
        # 起始覆盖：min_date 应 <= start_date + 允许误差
        try:
            import datetime as _dt
            min_dt = _dt.datetime.strptime(min_date, "%Y%m%d")
            req_dt = _dt.datetime.strptime(start_date, "%Y%m%d")
            slack = _dt.timedelta(days=DATE_SLACK_DAYS)
            start_ok = min_dt <= req_dt + slack
        except Exception:  # noqa: BLE001
            start_ok = False
        if not start_ok:
            return STATUS_ERROR
        # 核心表行数下限（偏少 → warning）
        if rows < CORE_MIN_ROWS.get(spec.table, 0):
            return STATUS_WARN

    return STATUS_OK


def _load_cache() -> dict | None:
    """读取磁盘校验缓存。"""
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _save_cache(report: dict) -> None:
    """写磁盘校验缓存。"""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"保存校验缓存失败: {exc}")


# 缓存里"变化慢、代价大"的结构性事实（存在性/近似行数/覆盖率）可以复用，
# 但 **max_date 是回测门禁的唯一判据** —— 当天 16:15 采集补齐后，若仍返回盘中写下的快照，
# 门禁就会拿"daily 还停在昨天"去判『锚定表未对齐 / 落后 N 个交易日』⇒ **假拦截**。
# 实测（2026-09-28 16:46）：缓存 daily=20260924，库里 daily=20260928（5557 行、锚定表已对齐），
# 回测却因"锚定表未对齐：daily(20260924), adj_factor(20260928)…"被中止。
# ★ 另有一处耦合让"有效交易日"修正救不回来：runner 的覆盖条件是 `effective != raw`，
#   而它拿到的是**实时** raw（已=20260928），与实时 effective 相同 ⇒ 不覆盖 ⇒
#   dates["daily"] 继续沿用**缓存里的** 20260924。实时修正救不了过期缓存，必须在源头刷新。
# 核心表边界是索引上的 MIN/MAX（毫秒级），所以命中缓存时强制实时刷新边界，
# 只改 min_date/max_date，不动行数/覆盖率（那些才是缓存的真正价值）。
BOUNDARY_REFRESH_TIERS = ("core",)
# ★ tier 分类与门禁锚定表**并不一致**：`daily_basic` 在 TABLE_SPECS 里是 important，
#   却同时是 runner.GATE_ANCHOR_TABLES 的成员 ⇒ 只按 tier=="core" 刷新会漏掉它，
#   门禁依旧拿它的过期 max_date 报"锚定表未对齐"（实测 daily_basic 仍停在 20260924）。
#   因此刷新集合 = core 层 ∪ 门禁实际用到的表（此处显式列出，避免隐式耦合）。
#   不做成"刷新全部 important"：那些表多为 end_date 且**无索引**，MIN/MAX 会退化成全表扫描。
BOUNDARY_REFRESH_TABLES = ("daily_basic",)


def boundary_refresh_targets(specs: list[TableSpec] | None = None) -> dict[str, str]:
    """需要实时刷新边界的表 → 日期列（供刷新与"防漂移"校验共用）。"""
    specs = specs or TABLE_SPECS
    out = {s.table: s.date_col for s in specs
           if (s.tier in BOUNDARY_REFRESH_TIERS or s.table in BOUNDARY_REFRESH_TABLES)
           and s.date_col}
    for t in BOUNDARY_REFRESH_TABLES:
        if t not in out:
            out[t] = "trade_date"
    return out


def _refresh_boundaries(report: dict, specs: list[TableSpec] | None = None) -> dict:
    """命中缓存时用**实时**查询覆盖门禁相关表的边界，避免读到过期 `max_date`。

    只覆盖 min_date/max_date；行数、覆盖率、状态沿用缓存（它们变化慢且查询昂贵）。
    刷新动作不写回磁盘缓存（保持"结构性快照"语义）。
    """
    targets = boundary_refresh_targets(specs)
    maps = [m for m in (report.get("tables"),
                        (report.get("core") or {}).get("tables"))
            if isinstance(m, dict)]
    refreshed: list[str] = []
    for table, date_col in targets.items():
        hit = [m for m in maps if isinstance(m.get(table), dict)]
        if not hit or not any(m[table].get("exists") for m in hit):
            continue
        old = str(hit[0][table].get("max_date") or "")
        try:
            mn, mx = _date_bounds(table, date_col)
        except Exception:  # noqa: BLE001
            continue
        if not mn or not mx or old == mx:
            continue
        for m in hit:
            m[table]["min_date"], m[table]["max_date"] = mn, mx
        refreshed.append(f"{table}:{old or '(空)'}→{mx}")
    if refreshed:
        report["boundaries_refreshed_at"] = int(time.time())
        report["boundaries_refreshed"] = refreshed
        logger.info("数据校验缓存边界已实时刷新（防门禁读过期 max_date）：" + ", ".join(refreshed))
    return report


def run_data_validation(
    start_date: str = DEFAULT_START,
    full_check: bool = False,
    specs: list[TableSpec] | None = None,
    force: bool = False,
) -> dict:
    """执行全表数据完整性校验。

    Args:
        start_date: 数据要求起始（YYYYMMDD），默认 20100101
        full_check: 是否对所有表查日期范围（True 更慢；默认只对 core/important 查）
        specs: 自定义表清单（默认 TABLE_SPECS）

    Returns:
        {
          "overall": ok/warning/error,
          "checked_at": epoch, "start_date": ...,
          "tables": {table: {...}},
          "core": {...}, "issues": [ ... ],
          "elapsed": seconds,
        }
    """
    specs = specs or TABLE_SPECS
    t0 = time.time()

    # 命中缓存直接返回（数据采集低频，校验结果短期不变；自定义 specs 不缓存）
    # ★ 但**边界（min/max_date）必须实时**：门禁只认核心表 max_date，
    #   采集补齐后若仍返回旧快照 → 假拦截（见 _refresh_boundaries 注释）
    if not force and specs is TABLE_SPECS:
        cache = _load_cache()
        if cache and cache.get("start_date") == start_date and cache.get("full_check") == full_check:
            age = time.time() - cache.get("checked_at", 0)
            if 0 <= age < CACHE_TTL:
                logger.info(f"数据校验命中缓存（{int(age)}s 前，overall={cache.get('overall')}）")
                return _refresh_boundaries(cache)

    total_stocks = _total_stocks()
    results: list[TableCheckResult] = []
    issues: list[dict] = []

    for spec in specs:
        exists = _table_exists(spec.table)
        if not exists:
            st = STATUS_ERROR if spec.tier == "core" else STATUS_WARN
            results.append(
                TableCheckResult(spec.table, spec.desc, spec.tier, exists=False, status=st)
            )
            issues.append({"table": spec.table, "level": st, "message": f"表不存在：{spec.desc}"})
            continue

        rows = _table_rows(spec.table)
        min_date, max_date = "", ""
        coverage_pct = 0.0

        # 日期范围：core/important 必查（important 缺失/不新鲜会导致增强特征大量 NaN，
        # 回测特征矩阵失真）；optional 仅在 full_check 时查（低频表全表扫较慢）
        # 修复：原实现仅 core 必查，导致 important 表 min/max/coverage 恒为空（评估缺口④）
        need_dates = spec.tier in ("core", "important") or full_check
        if need_dates and spec.date_col:
            min_date, max_date = _date_bounds(spec.table, spec.date_col)

        # 股票覆盖率：core/important 表 + 有 key_col 的表（大表自动免检，见 _stock_coverage）
        if spec.tier in ("core", "important") and spec.key_col:
            coverage_pct = _stock_coverage(spec.table, spec.key_col, total_stocks, rows)

        status = _judge(spec, exists, rows, min_date, max_date, coverage_pct, start_date)
        if status == STATUS_ERROR:
            msg = f"数据缺失/不完整：rows={rows}"
            if spec.date_col:
                msg += f", 日期范围 {min_date}~{max_date}"
            issues.append({"table": spec.table, "level": STATUS_ERROR, "message": msg})
        elif status == STATUS_WARN:
            issues.append({"table": spec.table, "level": STATUS_WARN, "message": f"数据偏少：rows={rows}"})

        results.append(
            TableCheckResult(spec.table, spec.desc, spec.tier, exists=exists, rows=rows,
                             min_date=min_date, max_date=max_date, coverage_pct=coverage_pct,
                             status=status)
        )

    # 汇总：core 缺失 = error（回测不可信）；有 warning = warning；否则 ok
    core_statuses = [r.status for r in results if r.tier == "core"]
    n_err = sum(1 for r in results if r.status == STATUS_ERROR)
    n_warn = sum(1 for r in results if r.status == STATUS_WARN)
    if STATUS_ERROR in core_statuses or n_err > 0:
        overall = STATUS_ERROR
    elif n_warn > 0:
        overall = STATUS_WARN
    else:
        overall = STATUS_OK

    core = {
        "status": STATUS_ERROR if STATUS_ERROR in core_statuses
                 else (STATUS_WARN if STATUS_WARN in core_statuses else STATUS_OK),
        "tables": {r.table: r.to_dict() for r in results if r.tier == "core"},
    }

    report = {
        "overall": overall,
        "start_date": start_date,
        "full_check": full_check,
        "total_stocks": total_stocks,
        "checked_at": int(time.time()),
        "elapsed": round(time.time() - t0, 2),
        "tables": {r.table: r.to_dict() for r in results},
        "core": core,
        "issue_count": {"error": n_err, "warning": n_warn},
        "issues": issues,
    }
    logger.info(f"数据校验完成: overall={overall}, error={n_err}, warning={n_warn}, 耗时 {report['elapsed']}s")
    if specs is TABLE_SPECS:
        _save_cache(report)
    return report


if __name__ == "__main__":
    # 独立运行：python -m app.backtest.data_validator
    import json
    rep = run_data_validation()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
