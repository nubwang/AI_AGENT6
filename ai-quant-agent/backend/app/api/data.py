"""数据管理接口 - 完整的采集控制"""
from datetime import date
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.models import SessionLocal

router = APIRouter(prefix="/api/v1/data", tags=["数据管理"])


# API 清单配置
API_GROUPS = [
    {
        "group": "日频批量（按日期）",
        "apis": [
            {"name": "daily", "desc": "日线行情", "batch": True, "period": "按日"},
            {"name": "daily_basic", "desc": "每日指标", "batch": True, "period": "按日"},
            {"name": "moneyflow", "desc": "资金流向", "batch": True, "period": "按日"},
            {"name": "stk_limit", "desc": "涨跌停价格", "batch": True, "period": "按日"},
            {"name": "block_trade", "desc": "大宗交易", "batch": True, "period": "按日"},
            {"name": "ths_daily", "desc": "同花顺日线", "batch": True, "period": "按日"},
            {"name": "index_daily", "desc": "指数日线", "batch": True, "period": "按日"},
            {"name": "margin", "desc": "融资融券", "batch": True, "period": "按日"},
            {"name": "limit_list_d", "desc": "龙虎榜", "batch": True, "period": "按日"},
            {"name": "hsgt", "desc": "沪深港通", "batch": True, "period": "按日"},
        ],
    },
    {
        "group": "个股循环（按股票代码）",
        "apis": [
            {"name": "adj_factor", "desc": "复权因子", "batch": False, "period": "一次性"},
            {"name": "dividend", "desc": "分红送配", "batch": False, "period": "一次性"},
            {"name": "stk_holdernumber", "desc": "股东人数", "batch": False, "period": "一次性"},
            {"name": "fina_indicator", "desc": "财务指标", "batch": False, "period": "季频"},
            {"name": "income", "desc": "利润表", "batch": False, "period": "季频"},
            {"name": "balancesheet", "desc": "资产负债表", "batch": False, "period": "季频"},
            {"name": "cashflow", "desc": "现金流量表", "batch": False, "period": "季频"},
        ],
    },
    {
        "group": "基础信息（一次性）",
        "apis": [
            {"name": "stock_basic", "desc": "股票列表", "batch": True, "period": "一次性"},
            {"name": "trade_cal", "desc": "交易日历", "batch": True, "period": "一次性"},
            {"name": "concept", "desc": "概念板块", "batch": True, "period": "一次性"},
            {"name": "ths_index", "desc": "同花顺概念", "batch": True, "period": "一次性"},
            {"name": "index_basic", "desc": "指数信息", "batch": True, "period": "一次性"},
            {"name": "stock_company", "desc": "公司信息", "batch": False, "period": "一次性"},
            {"name": "namechange", "desc": "股票曾用名", "batch": False, "period": "一次性"},
            {"name": "new_share", "desc": "新股上市", "batch": True, "period": "一次性"},
            {"name": "index_weight", "desc": "指数成分", "batch": False, "period": "月度"},
            {"name": "index_dailybasic", "desc": "大盘指标", "batch": True, "period": "按日"},
        ],
    },
]


class ApiStatus(BaseModel):
    name: str = Field(..., description="API名称")
    desc: str = Field(..., description="说明")
    batch: bool = Field(..., description="是否批量")
    period: str = Field(..., description="频率")
    row_count: int = Field(0, description="当前数据行数")
    date_range: str = Field("", description="数据日期范围")
    status: str = Field("pending", description="pending/done/partial")


class ApiGroup(BaseModel):
    group: str = Field(..., description="分组名")
    apis: list[ApiStatus]


# 统计缓存: {table: ((row_count, date_range), timestamp)}
# 避免对千万级 TEXT 大表反复做 COUNT/MIN/MAX 全表扫描拖垮MySQL
_table_stats_cache: dict[str, tuple[tuple, float]] = {}  # 永久缓存, 采集写入后失效
_fill_queue: list[str] = []  # 待后台计算日期范围的表
_fill_task = None  # 后台填充任务


def _table_row_count(table: str) -> int:
    """information_schema 近似行数(O(1), 不扫表)"""
    db = SessionLocal()
    try:
        return int(db.execute(text(
            "SELECT TABLE_ROWS FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name=:t"
        ), {"t": table}).scalar() or 0)
    except Exception:
        return 0
    finally:
        db.close()


def _compute_table_stats(table: str):
    """同步计算某表日期范围(供后台线程调用), 完成后写缓存"""
    import time as _time
    db = SessionLocal()
    try:
        row_count = _table_row_count(table)
        date_range = ""
        for col in ["trade_date", "end_date", "cal_date"]:
            try:
                r = db.execute(text(f"SELECT MIN({col}), MAX({col}) FROM {table}")).fetchone()
                if r and r[0]:
                    date_range = f"{r[0]} ~ {r[1]}"
                    break
            except Exception:
                continue
        _table_stats_cache[table] = ((row_count, date_range), _time.time())
    finally:
        db.close()


async def _fill_stats_worker():
    """后台逐个填充统计缓存(串行, 避免并发全表扫描拖垮MySQL)"""
    global _fill_task
    try:
        while _fill_queue:
            table = _fill_queue.pop(0)
            try:
                await asyncio.to_thread(_compute_table_stats, table)
            except Exception:
                pass
    finally:
        _fill_task = None


def _ensure_fill_worker():
    global _fill_task
    if _fill_task is None or _fill_task.done():
        _fill_task = asyncio.create_task(_fill_stats_worker())


def get_table_stats(table: str) -> tuple:
    """获取表行数(近似,O(1))和日期范围.
    行数实时取 information_schema(不扫表); 日期范围读缓存(永久,采集后失效),
    缓存未命中时返回''并排队后台计算, 请求不阻塞、不触发全表扫描"""
    hit = _table_stats_cache.get(table)
    if hit:
        return hit[0]
    row_count = _table_row_count(table)
    if row_count > 0 and table not in _fill_queue:
        _fill_queue.append(table)
        _ensure_fill_worker()
    return (row_count, "")


def invalidate_table_stats(table: str):
    """采集写入后调用: 使该表统计缓存失效, 下次请求重新后台计算日期范围"""
    _table_stats_cache.pop(table, None)
    if table in _fill_queue:
        _fill_queue.remove(table)


@router.get("/apis", response_model=list[ApiGroup])
async def list_apis():
    """获取所有API清单及采集状态"""
    result = []
    for group in API_GROUPS:
        apis = []
        for api in group["apis"]:
            row_count, date_range = get_table_stats(api["name"])
            if row_count > 0:
                status = "partial" if api["batch"] and date_range else "done"
            else:
                status = "pending"
            apis.append(ApiStatus(
                name=api["name"],
                desc=api["desc"],
                batch=api["batch"],
                period=api["period"],
                row_count=row_count,
                date_range=date_range,
                status=status,
            ))
        result.append(ApiGroup(group=group["group"], apis=apis))
    return result


@router.get("/stats")
async def data_stats():
    """数据统计概览（表不存在时返回0）.
    行数用 information_schema 的 TABLE_ROWS, 避免对百万级大表 COUNT(*) 全表扫描"""
    db = SessionLocal()
    result = {"stock_count": 0, "trade_dates": 0, "daily_rows": 0, "total_tables": 42}
    try:
        for key, table, sql in [
            ("stock_count", "stock_basic", "SELECT TABLE_ROWS FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='stock_basic'"),
            ("trade_dates", "trade_cal", "SELECT COUNT(*) FROM trade_cal WHERE is_open=1"),
            ("daily_rows", "daily", "SELECT TABLE_ROWS FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='daily'"),
        ]:
            try:
                val = db.execute(text(sql)).scalar()
                if val is not None:
                    result[key] = val
            except Exception:
                pass  # 表不存在，保持默认0
        return result
    finally:
        db.close()


import asyncio
import threading
from app.api.ws import run_full_collection, run_stage1, run_stage2, run_stage3, run_stage4
from app.api.ws import count_stage_tasks, ensure_task_table, has_pending_tasks
from app.api.ws import stop_stage, stop_all_collection
from app.core.logger import logger


_collection_task: asyncio.Task | None = None
_stage_tasks: dict[int, asyncio.Task] = {}


def _auto_scan_after_collection(task: asyncio.Task) -> None:
    """全量采集完成后的**后置处理**（手动采集路径；定时路径见 main.py）。

    依据 run_full_collection 返回的真实执行状态判断：
    - done    → 采集真正执行完成 → 交给 predictions.handle_after_collection 决定下一步
    - skipped → 被其他采集/锁占用未执行, 不处理（避免基于旧数据跑）
    - error   → 采集异常, 不处理

    用户要求（2026-09-20）：**别再"每次都自动扫"**——默认只弹窗问一句"要不要跑每日推荐扫描"，
    由用户控制（模式见可进化参数 auto_scan_after_collect：confirm 默认 / auto 旧行为 / off 不扫）。
    """
    try:
        result = task.result()
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[data] 全量采集异常, 不处理每日推荐: {exc}")
        return
    if isinstance(result, dict) and result.get("status") == "done":
        logger.info("[data] 全量采集完成 → 按 auto_scan_after_collect 模式决定是否（询问）运行每日推荐")
    elif isinstance(result, dict) and result.get("status") == "skipped":
        logger.warning(f"[data] 全量采集被跳过({result.get('reason')}), 不处理每日推荐")
        return
    else:
        logger.warning("[data] 全量采集未真正执行, 不处理每日推荐")
        return

    def _after() -> None:
        try:
            from app.api.predictions import handle_after_collection
            res = handle_after_collection(source="auto_data")
            logger.info(f"[data] 采集后处理（自动来源）: {res.get('status')} — {res.get('message')}")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[data] 采集后处理失败: {exc}")

    threading.Thread(target=_after, daemon=True).start()


@router.get("/collect/status")
async def collect_status():
    """断点续采状态: 各阶段待处理任务数"""
    ensure_task_table()
    pending = has_pending_tasks()
    stages = {}
    for s in (1, 2, 3, 4):
        total, done = count_stage_tasks(s)
        if total > 0:
            stages[str(s)] = {"total": total, "done": done, "pending": total - done}
    return {
        "has_pending": pending > 0,
        "total_pending": pending,
        "stages": stages,
    }


def _running_stage_tasks() -> list:
    """返回正在运行的单阶段任务列表(跨接口判断, 避免全量/单阶段并发)"""
    return [s for s, t in _stage_tasks.items() if t and not t.done()]


@router.post("/trigger-collection")
async def trigger_collection():
    global _collection_task, _stage_tasks
    running_stages = _running_stage_tasks()
    if running_stages:
        return {"status": "stage_running", "task": f"stages{running_stages}"}
    if _collection_task and not _collection_task.done():
        return {"status": "already_running", "task": "data_collection"}
    _collection_task = asyncio.create_task(run_full_collection())
    # 采集完成后自动运行每日推荐（与定时任务行为一致）
    _collection_task.add_done_callback(_auto_scan_after_collection)
    return {"status": "triggered", "task": "data_collection"}


@router.post("/collect/all")
async def collect_all():
    """全量采集（4个阶段依次执行）"""
    global _collection_task, _stage_tasks
    running_stages = _running_stage_tasks()
    if running_stages:
        return {"status": "stage_running", "task": f"stages{running_stages}"}
    if _collection_task and not _collection_task.done():
        return {"status": "already_running", "task": "collect_all"}
    _collection_task = asyncio.create_task(run_full_collection())
    # 采集完成后自动运行每日推荐（与定时任务行为一致）
    _collection_task.add_done_callback(_auto_scan_after_collection)
    return {"status": "triggered", "task": "collect_all"}


@router.post("/collect/stage1")
async def collect_stage1():
    """仅执行阶段1：基础信息"""
    global _stage_tasks, _collection_task
    if _collection_task and not _collection_task.done():
        return {"status": "collection_running", "task": "stage1"}
    if _stage_tasks.get(1) and not _stage_tasks[1].done():
        return {"status": "already_running", "task": "stage1"}
    _stage_tasks[1] = asyncio.create_task(run_stage1())
    return {"status": "triggered", "task": "stage1"}


@router.post("/collect/stage2")
async def collect_stage2():
    """仅执行阶段2：日频批量数据"""
    global _stage_tasks, _collection_task
    if _collection_task and not _collection_task.done():
        return {"status": "collection_running", "task": "stage2"}
    if _stage_tasks.get(2) and not _stage_tasks[2].done():
        return {"status": "already_running", "task": "stage2"}
    _stage_tasks[2] = asyncio.create_task(run_stage2())
    return {"status": "triggered", "task": "stage2"}


@router.post("/collect/stage3")
async def collect_stage3():
    """仅执行阶段3：个股数据循环"""
    global _stage_tasks, _collection_task
    if _collection_task and not _collection_task.done():
        return {"status": "collection_running", "task": "stage3"}
    if _stage_tasks.get(3) and not _stage_tasks[3].done():
        return {"status": "already_running", "task": "stage3"}
    _stage_tasks[3] = asyncio.create_task(run_stage3())
    return {"status": "triggered", "task": "stage3"}


@router.post("/collect/stage4")
async def collect_stage4():
    """仅执行阶段4：补充数据"""
    global _stage_tasks, _collection_task
    if _collection_task and not _collection_task.done():
        return {"status": "collection_running", "task": "stage4"}
    if _stage_tasks.get(4) and not _stage_tasks[4].done():
        return {"status": "already_running", "task": "stage4"}
    _stage_tasks[4] = asyncio.create_task(run_stage4())
    return {"status": "triggered", "task": "stage4"}


# ── 停止采集端点（优雅停止, 进度保存可续采）─────────────────────────
@router.post("/collect/stop/stage1")
async def stop_collect_stage1():
    stop_stage(1)
    return {"status": "stopping", "task": "stage1"}


@router.post("/collect/stop/stage2")
async def stop_collect_stage2():
    stop_stage(2)
    return {"status": "stopping", "task": "stage2"}


@router.post("/collect/stop/stage3")
async def stop_collect_stage3():
    stop_stage(3)
    return {"status": "stopping", "task": "stage3"}


@router.post("/collect/stop/stage4")
async def stop_collect_stage4():
    stop_stage(4)
    return {"status": "stopping", "task": "stage4"}


@router.post("/collect/stop/all")
async def stop_collect_all():
    stop_all_collection()
    return {"status": "stopping", "task": "all"}


@router.post("/trigger-prediction")
async def trigger_prediction():
    return {"status": "triggered", "task": "prediction_pipeline"}


# ── 全市场股票查询 ────────────────────────────────────────────────
@router.get("/stocks")
async def list_stocks(page: int = 1, page_size: int = 20, keyword: str = "", market: str = ""):
    """全市场股票列表(基于stock_basic, 附带每只最新日线价)"""
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    db = SessionLocal()
    try:
        where, params = [], {}
        if keyword:
            where.append("(sb.ts_code LIKE :kw OR sb.name LIKE :kw OR sb.symbol LIKE :kw)")
            params["kw"] = f"%{keyword}%"
        if market:
            where.append("sb.market = :mkt")
            params["mkt"] = market
        wsql = (" WHERE " + " AND ".join(where)) if where else ""

        total = int(db.execute(text(f"SELECT COUNT(*) FROM stock_basic sb{wsql}"), params).scalar() or 0)
        rows = db.execute(text(
            f"SELECT sb.ts_code, sb.symbol, sb.name, sb.area, sb.industry, sb.market, sb.list_date "
            f"FROM stock_basic sb{wsql} ORDER BY sb.ts_code LIMIT :lim OFFSET :off"
        ), {**params, "lim": page_size, "off": (page - 1) * page_size}).fetchall()
        items = [dict(r._mapping) for r in rows]

        codes = [it["ts_code"] for it in items]
        latest_map = {}
        if codes:
            binds = {f"c{i}": c for i, c in enumerate(codes)}
            in_sql = ",".join(f":c{i}" for i in range(len(codes)))
            lrows = db.execute(text(
                f"SELECT d.ts_code, d.trade_date, d.close, d.pct_chg "
                f"FROM daily d JOIN (SELECT ts_code, MAX(trade_date) md FROM daily "
                f"WHERE ts_code IN ({in_sql}) GROUP BY ts_code) m "
                f"ON d.ts_code = m.ts_code AND d.trade_date = m.md"
            ), binds).fetchall()
            for r in lrows:
                m = dict(r._mapping)
                latest_map[m["ts_code"]] = m
        for it in items:
            l = latest_map.get(it["ts_code"])
            it["latest_date"] = l["trade_date"] if l else None
            it["latest_close"] = l["close"] if l else None
            it["latest_pct_chg"] = l["pct_chg"] if l else None
        return {"total": total, "page": page, "page_size": page_size, "items": items}
    finally:
        db.close()


@router.get("/stocks/{ts_code}")
async def get_stock_detail(ts_code: str):
    """股票详情: 基本信息+公司信息+最新行情/估值"""
    db = SessionLocal()
    try:
        basic = db.execute(text("SELECT * FROM stock_basic WHERE ts_code=:c"), {"c": ts_code}).mappings().first()
        if not basic:
            return {"error": "not_found", "ts_code": ts_code}
        basic = dict(basic)
        company = db.execute(text("SELECT * FROM stock_company WHERE ts_code=:c"), {"c": ts_code}).mappings().first()
        company = dict(company) if company else {}
        latest = db.execute(text(
            "SELECT d.trade_date, d.open, d.high, d.low, d.close, d.pre_close, d.pct_chg, d.vol, d.amount, "
            "b.pe, b.pe_ttm, b.pb, b.turnover_rate, b.total_mv, b.circ_mv "
            "FROM daily d LEFT JOIN daily_basic b ON b.ts_code=d.ts_code AND b.trade_date=d.trade_date "
            "WHERE d.ts_code=:c ORDER BY d.trade_date DESC LIMIT 1"
        ), {"c": ts_code}).mappings().first()
        latest = dict(latest) if latest else {}
        rng = db.execute(text(
            "SELECT MIN(trade_date) mn, MAX(trade_date) mx, COUNT(*) cnt FROM daily WHERE ts_code=:c"
        ), {"c": ts_code}).fetchone()
        return {
            "basic": basic,
            "company": company,
            "latest": latest,
            "daily_range": {"min": rng[0], "max": rng[1], "count": rng[2]} if rng and rng[0] else None,
        }
    finally:
        db.close()


@router.get("/stocks/{ts_code}/daily")
async def get_stock_daily(ts_code: str, start: str = "", end: str = "", limit: int = 500):
    """股票日线K线数据: 默认返回最近 limit 条(按日期升序), 保证K线展示最新行情"""
    limit = max(1, min(5000, limit))
    db = SessionLocal()
    try:
        where, params = "ts_code=:c", {"c": ts_code}
        if start:
            where += " AND trade_date>=:s"
            params["s"] = start
        if end:
            where += " AND trade_date<=:e"
            params["e"] = end
        rows = db.execute(text(
            f"SELECT trade_date, open, high, low, close, vol, amount, pct_chg "
            f"FROM daily WHERE {where} ORDER BY trade_date DESC LIMIT :lim"
        ), {**params, "lim": limit}).fetchall()
        # 取最近 limit 条, 再反转成升序, 保证K线时间轴从左(旧)到右(新)
        data = [dict(r._mapping) for r in reversed(rows)]
        return {"ts_code": ts_code, "count": len(data), "data": data}
    finally:
        db.close()


# ── 个股详情: 全部表信息（补全） ────────────────────────────────────
# 字段中文映射（覆盖所有个股维度的表）
FIELD_LABELS = {
    # 通用
    "ts_code": "股票代码", "symbol": "数字代码", "name": "名称",
    "trade_date": "交易日期", "end_date": "报告期", "ann_date": "公告日期",
    # stock_basic
    "area": "地区", "industry": "行业", "market": "市场",
    "list_date": "上市日期", "delist_date": "退市日期", "is_hs": "沪深港通",
    # daily / weekly / monthly
    "open": "开盘价", "high": "最高价", "low": "最低价", "close": "收盘价",
    "pre_close": "昨收价", "change": "涨跌额", "pct_chg": "涨跌幅(%)",
    "vol": "成交量", "amount": "成交额",
    # daily_basic
    "turnover_rate": "换手率(%)", "turnover_rate_f": "换手率自由(%)",
    "volume_ratio": "量比", "pe": "市盈率", "pe_ttm": "市盈率TTM",
    "pb": "市净率", "ps": "市销率", "ps_ttm": "市销率TTM",
    "dv_ratio": "股息率(%)", "dv_ttm": "股息率TTM(%)",
    "total_share": "总股本", "float_share": "流通股本", "free_share": "自由流通股本",
    "total_mv": "总市值", "circ_mv": "流通市值",
    # adj_factor / stk_limit / suspend_d
    "adj_factor": "复权因子", "up_limit": "涨停价", "down_limit": "跌停价",
    "suspend_flg": "停复牌标志", "suspend_type": "停牌类型",
    # moneyflow
    "buy_amount": "买入额", "buy_vol": "买入量", "sell_amount": "卖出额",
    "sell_vol": "卖出量", "net_amount": "净额", "net_vol": "净量",
    "amount_lt": "大单买入额", "vol_lt": "大单买入量", "net_amount_lt": "大单净额",
    "amount_mt": "中单买入额", "vol_mt": "中单买入量", "net_amount_mt": "中单净额",
    "amount_st": "小单买入额", "vol_st": "小单买入量", "net_amount_st": "小单净额",
    # block_trade
    "price": "成交价", "buyer": "买方营业部", "seller": "卖方营业部",
    # income / balancesheet / cashflow
    "f_ann_date": "实际公告日期", "report_type": "报表类型", "comp_type": "公司类型",
    "revenue": "营业收入", "operate_profit": "营业利润", "total_profit": "利润总额",
    "n_income": "净利润", "n_income_attr_p": "归母净利润",
    "basic_eps": "基本每股收益", "diluted_eps": "稀释每股收益", "update_flag": "更新标志",
    "total_assets": "总资产", "total_liab": "总负债", "total_hldr_eqy": "股东权益",
    "total_lse": "负债合计", "total_equity": "总权益",
    "n_inc_oper_act": "经营现金流净额", "n_inc_inves_act": "投资现金流净额",
    "n_inc_fnc_act": "筹资现金流净额", "net_cash_flows": "现金净增加额",
    # fina_indicator
    "roe": "净资产收益率(%)", "roe_dt": "ROE摊薄(%)", "roa": "总资产收益率(%)",
    "debt_to_assets": "资产负债率(%)", "eps": "每股收益", "bps": "每股净资产",
    "ocfps": "每股经营现金流", "profit_dedt": "扣非净利润",
    "gross_margin": "毛利率(%)", "profit_margin": "净利率(%)",
    # fina_mainbz
    "bz_item": "业务名称", "bz_sales": "业务收入", "bz_cost": "业务成本",
    "bz_profit": "业务利润", "bz_sales_ratio": "业务占比(%)",
    # forecast
    "type": "预告类型", "p_change_min": "净利润增幅下限(%)", "p_change_max": "净利润增幅上限(%)",
    "net_profit_min": "净利润下限", "net_profit_max": "净利润上限",
    # stk_holdernumber
    "holder_num": "股东人数",
    # top10_holders / top10_floatholders
    "holder_name": "股东名称", "hold_amount": "持股数量", "hold_ratio": "持股比例(%)",
    "hold_float_ratio": "流通股比例(%)",
    # stk_holdertrade
    "holder_type": "股东类型", "trade_type": "交易类型", "change_vol": "变动数量",
    "change_ratio": "变动比例(%)", "begin_date": "开始日期", "price_avg": "成交均价",
    # repurchase
    "progress": "回购进度", "exp_date": "预计截止日",
    "amount_min": "金额下限", "amount_max": "金额上限",
    "vol_min": "数量下限", "vol_max": "数量上限",
    "price_min": "价格下限", "price_max": "价格上限",
    # stk_rewards
    "exe_mode": "激励方式", "rewards_type": "激励类型", "gprice": "授予价格",
    "volume": "授予数量", "stk_name": "股票名称", "mark": "备注",
    # dividend
    "imp_ann_date": "实施公告日期", "record_date": "股权登记日", "ex_date": "除权除息日",
    "pay_date": "派息日", "div_proc": "预案进度", "cash_div": "现金分红",
    "cash_div_tax": "税前现金分红", "stk_div": "送股比例", "stk_bo": "转增比例",
    "share_tran": "折算比例",
    # pledge_stat
    "pledge_count": "质押次数", "pledge_vol": "质押数量",
    "pledge_ratio": "质押比例(%)", "market_cap": "质押市值",
    # limit_list
    "buy_times": "买入次数", "reason": "上榜原因",
    # hsgt
    "hg_sh_amount": "沪股通持股额", "hg_sz_amount": "深股通持股额", "percent": "持股比例(%)",
    # namechange
    "start_date": "开始日期", "change_reason": "变更原因",
    # new_share
    "sub_code": "申购代码", "ipo_date": "上市日期", "issue_date": "发行日期",
    "limit_amount": "申购上限", "fund": "募集资金", "market_amount": "上网发行量",
    # concept_detail / ths_member
    "code": "概念代码", "con_code": "成分股代码", "in_date": "纳入日期",
    "out_date": "剔除日期", "is_new": "是否最新", "weight": "权重(%)",
}


# 个股全部表配置: table, module(分组), module_name, name, limit, order_col, col(查询列)
STOCK_TABLES = [
    # 基本信息
    {"table": "namechange", "module": "base", "module_name": "基本信息", "name": "股票曾用名", "limit": 100, "order_col": "start_date"},
    {"table": "new_share", "module": "base", "module_name": "基本信息", "name": "新股上市", "limit": 20, "order_col": "ipo_date"},
    {"table": "dividend", "module": "base", "module_name": "基本信息", "name": "分红送配", "limit": 500, "order_col": "ex_date"},
    # 财务数据
    {"table": "fina_indicator", "module": "finance", "module_name": "财务数据", "name": "财务指标", "limit": 200, "order_col": "end_date"},
    {"table": "income", "module": "finance", "module_name": "财务数据", "name": "利润表", "limit": 200, "order_col": "end_date"},
    {"table": "balancesheet", "module": "finance", "module_name": "财务数据", "name": "资产负债表", "limit": 200, "order_col": "end_date"},
    {"table": "cashflow", "module": "finance", "module_name": "财务数据", "name": "现金流量表", "limit": 200, "order_col": "end_date"},
    {"table": "fina_mainbz", "module": "finance", "module_name": "财务数据", "name": "主营构成", "limit": 500, "order_col": "end_date"},
    {"table": "forecast", "module": "finance", "module_name": "财务数据", "name": "业绩预告", "limit": 200, "order_col": "end_date"},
    {"table": "express", "module": "finance", "module_name": "财务数据", "name": "业绩快报", "limit": 200, "order_col": "end_date"},
    # 股东信息
    {"table": "stk_holdernumber", "module": "holder", "module_name": "股东信息", "name": "股东人数", "limit": 500, "order_col": "end_date"},
    {"table": "top10_holders", "module": "holder", "module_name": "股东信息", "name": "前十大股东", "limit": 500, "order_col": "end_date"},
    {"table": "top10_floatholders", "module": "holder", "module_name": "股东信息", "name": "前十大流通股东", "limit": 500, "order_col": "end_date"},
    {"table": "stk_holdertrade", "module": "holder", "module_name": "股东信息", "name": "股东增减持", "limit": 500, "order_col": "ann_date"},
    {"table": "repurchase", "module": "holder", "module_name": "股东信息", "name": "股票回购", "limit": 500, "order_col": "ann_date"},
    {"table": "stk_rewards", "module": "holder", "module_name": "股东信息", "name": "股权激励", "limit": 500, "order_col": "ann_date"},
    {"table": "pledge_stat", "module": "holder", "module_name": "股东信息", "name": "股权质押", "limit": 500, "order_col": "end_date"},
    # 行情与资金 (单只股票完整历史, 覆盖全部交易日)
    {"table": "daily_basic", "module": "market", "module_name": "行情与资金", "name": "每日指标", "limit": 5000, "order_col": "trade_date"},
    {"table": "weekly", "module": "market", "module_name": "行情与资金", "name": "周线行情", "limit": 5000, "order_col": "trade_date"},
    {"table": "monthly", "module": "market", "module_name": "行情与资金", "name": "月线行情", "limit": 5000, "order_col": "trade_date"},
    {"table": "adj_factor", "module": "market", "module_name": "行情与资金", "name": "复权因子", "limit": 5000, "order_col": "trade_date"},
    {"table": "stk_limit", "module": "market", "module_name": "行情与资金", "name": "涨跌停价格", "limit": 5000, "order_col": "trade_date"},
    {"table": "suspend_d", "module": "market", "module_name": "行情与资金", "name": "停复牌", "limit": 500, "order_col": "trade_date"},
    {"table": "moneyflow", "module": "market", "module_name": "行情与资金", "name": "资金流向", "limit": 5000, "order_col": "trade_date"},
    {"table": "block_trade", "module": "market", "module_name": "行情与资金", "name": "大宗交易", "limit": 1000, "order_col": "trade_date"},
    {"table": "limit_list", "module": "market", "module_name": "行情与资金", "name": "龙虎榜", "limit": 1000, "order_col": "trade_date"},
    {"table": "hsgt", "module": "market", "module_name": "行情与资金", "name": "沪深港通持股", "limit": 5000, "order_col": "trade_date"},
    # 板块归属
    {"table": "concept_detail", "module": "concept", "module_name": "板块归属", "name": "概念成分", "limit": 500, "order_col": "in_date"},
    {"table": "ths_member", "module": "concept", "module_name": "板块归属", "name": "同花顺成分", "limit": 500, "order_col": "in_date", "col": "con_code"},
]


def _query_stock_tables_sync(ts_code: str) -> dict:
    """同步查询个股全部表数据(在 asyncio.to_thread 中执行, 避免大表查询阻塞事件循环)"""
    db = SessionLocal()
    try:
        exists = db.execute(text("SELECT 1 FROM stock_basic WHERE ts_code=:c"), {"c": ts_code}).scalar()
        if not exists:
            return {"error": "not_found", "ts_code": ts_code}
        modules: dict[str, dict] = {}
        for cfg in STOCK_TABLES:
            table = cfg["table"]
            col = cfg.get("col", "ts_code")
            order_col = cfg["order_col"]
            limit = cfg["limit"]
            try:
                rows = db.execute(text(
                    f"SELECT * FROM {table} WHERE {col}=:c ORDER BY {order_col} DESC LIMIT :l"
                ), {"c": ts_code, "l": limit}).fetchall()
            except Exception:
                rows = []
            col_names = list(rows[0]._mapping.keys()) if rows else []
            # 排除无业务意义的自增id列(规范表结构后新增的主键列)
            columns = [{"key": k, "label": FIELD_LABELS.get(k, k)} for k in col_names if k != "id"]
            data = [dict(r._mapping) for r in rows]
            mkey = cfg["module"]
            mod = modules.setdefault(mkey, {"key": mkey, "name": cfg["module_name"], "tables": []})
            mod["tables"].append({
                "key": table, "name": cfg["name"], "count": len(data),
                "columns": columns, "rows": data,
            })
        return {"ts_code": ts_code, "modules": list(modules.values())}
    finally:
        db.close()


@router.get("/stocks/{ts_code}/tables")
async def get_stock_tables(ts_code: str):
    """个股详情: 按模块返回全部个股维度表数据(每表限量, 供前端tab展示).
    查询通过线程池执行, 避免大表全表扫描阻塞事件循环导致其他接口超时"""
    return await asyncio.to_thread(_query_stock_tables_sync, ts_code)
