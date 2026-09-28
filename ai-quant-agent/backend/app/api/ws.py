"""WebSocket 实时日志 + 42API增量采集 + 进度条心跳 + 42API实时状态"""
import asyncio, json, os, time, pandas as pd
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import text
from app.core.logger import logger
from app.data.tushare_api import tushare_client
from app.data.rate_limiter import alimiter, rate_limiter
from app.models import SessionLocal

router = APIRouter()
connected_clients: set[WebSocket] = set()
_collection_running = False
_stage_running_flags = {1: False, 2: False, 3: False, 4: False}
_start_time = 0.0
_api_call_count = 0
STAGE_WEIGHTS = {1: 0.25, 2: 0.25, 3: 0.25, 4: 0.25}
# 每阶段并发处理任务数(配合限流器把吞吐打满到 Tushare 配额)
STAGE_CONCURRENCY = 8
# ★ 一次性「强制回补」令牌（plans/24 §11.36.1c）：由阶段构建函数（stage3_stocks）写入，
#   由队列消费函数（process_stage_queue）取出并**立即清空**。集合内 api 本轮忽略 collection_skip
#   缓存，用于修补"半截表"（最新交易日行数不足）。空集合 = 行为与改动前完全一致。
_force_apis: set[str] = set()


def _take_force_apis() -> set[str]:
    """取出并清空「强制回补」令牌（一次性，避免误伤后续阶段/后续运行）。"""
    global _force_apis
    s = set(_force_apis)
    _force_apis = set()
    return s


# ── 42个API完整清单 ──────────────────────────────────────────────
ALL_APIS = [
    {"name": "trade_cal", "desc": "交易日历", "stage": 1, "date_col": "cal_date"},
    {"name": "stock_basic", "desc": "股票列表", "stage": 1, "date_col": ""},
    {"name": "new_share", "desc": "新股上市", "stage": 1, "date_col": ""},
    {"name": "concept", "desc": "概念板块", "stage": 1, "date_col": ""},
    {"name": "concept_detail", "desc": "概念成分股", "stage": 1, "date_col": ""},
    {"name": "ths_index", "desc": "同花顺概念", "stage": 1, "date_col": ""},
    {"name": "ths_member", "desc": "同花顺成分股", "stage": 1, "date_col": ""},
    {"name": "index_basic", "desc": "指数信息", "stage": 1, "date_col": ""},
    {"name": "index_weight", "desc": "指数成分权重", "stage": 1, "date_col": ""},
    {"name": "daily", "desc": "日线行情", "stage": 2, "date_col": "trade_date"},
    {"name": "daily_basic", "desc": "每日指标", "stage": 2, "date_col": "trade_date"},
    {"name": "moneyflow", "desc": "资金流向", "stage": 2, "date_col": "trade_date"},
    {"name": "stk_limit", "desc": "涨跌停价格", "stage": 2, "date_col": "trade_date"},
    {"name": "block_trade", "desc": "大宗交易", "stage": 2, "date_col": "trade_date"},
    {"name": "suspend_d", "desc": "停复牌信息", "stage": 2, "date_col": "trade_date"},
    {"name": "forecast", "desc": "业绩预告", "stage": 2, "date_col": "ann_date"},
    {"name": "express", "desc": "业绩快报", "stage": 2, "date_col": "ann_date"},
    {"name": "margin", "desc": "融资融券", "stage": 2, "date_col": "trade_date"},
    {"name": "limit_list_d", "desc": "龙虎榜", "stage": 2, "date_col": "trade_date"},
    {"name": "hsgt", "desc": "沪深港通", "stage": 2, "date_col": "trade_date"},
    {"name": "ths_daily", "desc": "同花顺日线", "stage": 2, "date_col": "trade_date"},
    {"name": "index_daily", "desc": "指数日线", "stage": 2, "date_col": "trade_date"},
    {"name": "index_dailybasic", "desc": "大盘指标", "stage": 2, "date_col": "trade_date"},
    {"name": "stock_company", "desc": "公司信息", "stage": 3, "date_col": ""},
    {"name": "namechange", "desc": "股票曾用名", "stage": 3, "date_col": ""},
    {"name": "adj_factor", "desc": "复权因子", "stage": 3, "date_col": ""},
    {"name": "dividend", "desc": "分红送配", "stage": 3, "date_col": ""},
    {"name": "stk_holdernumber", "desc": "股东人数", "stage": 3, "date_col": ""},
    {"name": "fina_indicator", "desc": "财务指标", "stage": 3, "date_col": ""},
    {"name": "income", "desc": "利润表", "stage": 3, "date_col": ""},
    {"name": "balancesheet", "desc": "资产负债表", "stage": 3, "date_col": ""},
    {"name": "cashflow", "desc": "现金流量表", "stage": 3, "date_col": ""},
    {"name": "fina_mainbz", "desc": "主营构成", "stage": 3, "date_col": ""},
    {"name": "top10_holders", "desc": "前十大股东", "stage": 3, "date_col": ""},
    {"name": "top10_floatholders", "desc": "前十大流通", "stage": 3, "date_col": ""},
    {"name": "stk_holdertrade", "desc": "股东增减持", "stage": 3, "date_col": ""},
    {"name": "repurchase", "desc": "股票回购", "stage": 3, "date_col": ""},
    {"name": "stk_rewards", "desc": "股权激励", "stage": 3, "date_col": ""},
    {"name": "pledge_stat", "desc": "股权质押", "stage": 3, "date_col": ""},
    {"name": "weekly", "desc": "周线行情", "stage": 3, "date_col": ""},
    {"name": "monthly", "desc": "月线行情", "stage": 4, "date_col": ""},
]


def build_api_statuses():
    return {a["name"]: {"desc": a["desc"], "stage": a["stage"], "status": "pending", "rows": 0, "skip_reason": ""} for a in ALL_APIS}


PROGRESS = {
    "stage": 0, "stage_name": "", "total": 0, "current": 0,
    "api": "", "status": "idle", "overall_pct": 0,
    "elapsed": 0, "eta": 0, "api_count": 0,
    "stages": [
        {"idx": 1, "name": "基础信息", "total": 0, "current": 0, "pct": 0},
        {"idx": 2, "name": "日频批量数据", "total": 0, "current": 0, "pct": 0},
        {"idx": 3, "name": "个股数据循环", "total": 0, "current": 0, "pct": 0},
        {"idx": 4, "name": "补充数据", "total": 0, "current": 0, "pct": 0},
    ],
    "api_statuses": build_api_statuses(),
}


# ── 数据库辅助函数（增量采集关键） ───────────────────────────────
def get_max_date(table: str, col: str) -> str:
    """查询表中某日期列的最大值, 返回 'YYYYMMDD' 或 ''"""
    if not col:
        return ""
    db = SessionLocal()
    try:
        r = db.execute(text(f"SELECT MAX({col}) FROM {table}")).scalar()
        return str(r) if r else ""
    except Exception:
        return ""
    finally:
        db.close()


def table_has_data(table: str) -> bool:
    """检查表是否有数据"""
    db = SessionLocal()
    try:
        cnt = db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
        return cnt > 0
    except Exception:
        return False
    finally:
        db.close()


def stock_has_data(table: str, ts_code: str) -> bool:
    """检查某股票在表中是否有数据"""
    db = SessionLocal()
    try:
        cnt = db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE ts_code='{ts_code}'")).scalar() or 0
        return cnt > 0
    except Exception:
        return False
    finally:
        db.close()


def get_stock_max_date(table: str, col: str, ts_code: str) -> str:
    """查询某股票在某表的日期列最大值, 返回 'YYYYMMDD' 或 ''"""
    db = SessionLocal()
    try:
        r = db.execute(text(f"SELECT MAX({col}) FROM {table} WHERE ts_code='{ts_code}'")).scalar()
        if not r:
            return ""
        return str(r).replace("-", "")
    except Exception:
        return ""
    finally:
        db.close()


def add_days(date_str: str, days: int = 1) -> str:
    """给 YYYYMMDD 或 YYYY-MM-DD 日期加天数, 返回 YYYYMMDD"""
    import datetime
    d = datetime.datetime.strptime(date_str[:8].replace("-", ""), "%Y%m%d")
    return (d + datetime.timedelta(days=days)).strftime("%Y%m%d")


def get_stocks_with_data(table: str) -> set:
    """批量查询表中已有数据的股票集合(一次查询替代逐条检查)"""
    db = SessionLocal()
    try:
        rows = db.execute(text(f"SELECT DISTINCT ts_code FROM {table}")).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()
    finally:
        db.close()


def get_stock_max_dates(table: str, col: str) -> dict:
    """批量查询表中每只股票的最大日期(一次查询替代逐条检查)"""
    db = SessionLocal()
    try:
        rows = db.execute(text(f"SELECT ts_code, MAX({col}) FROM {table} GROUP BY ts_code")).fetchall()
        return {r[0]: str(r[1]).replace("-", "") for r in rows if r[1]}
    except Exception:
        return {}
    finally:
        db.close()


# ── 持久化任务队列（断点续采核心） ────────────────────────────────
TASK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS collection_tasks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    stage INT NOT NULL,
    task_key VARCHAR(255) NOT NULL,
    api VARCHAR(50) NOT NULL,
    kwargs_json TEXT,
    detail VARCHAR(255) DEFAULT '',
    status VARCHAR(10) NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY (stage, task_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_task_table():
    """确保 collection_tasks 表存在（幂等）"""
    db = SessionLocal()
    try:
        db.execute(text(TASK_TABLE_SQL))
        db.commit()
    except Exception as e:
        logger.warning(f"ensure_task_table: {e}")
    finally:
        db.close()


# ── 跨进程采集互斥锁（多worker防重入） ─────────────────────────────
COLLECT_LOCK_TABLE = """
CREATE TABLE IF NOT EXISTS collect_lock (
    lock_name VARCHAR(50) PRIMARY KEY,
    pid INT NOT NULL,
    acquired_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def _pid_alive(pid: int) -> bool:
    """检查进程是否存活(用于接管崩溃进程残留的锁)"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def try_acquire_collect_lock() -> bool:
    """获取跨进程采集锁(全量/单阶段/自动续采共用). 获取失败说明已有采集在运行"""
    db = SessionLocal()
    try:
        db.execute(text(COLLECT_LOCK_TABLE))
        db.commit()
        rows = db.execute(text("SELECT pid FROM collect_lock WHERE lock_name='collection'")).fetchall()
        if rows:
            owner = rows[0][0]
            if owner == os.getpid() or _pid_alive(owner):
                return False  # 锁被存活进程(或本进程)持有
            # 持有者进程已崩溃 → 接管残留锁
            db.execute(text("DELETE FROM collect_lock WHERE lock_name='collection'"))
            db.commit()
        db.execute(text("INSERT INTO collect_lock (lock_name, pid) VALUES ('collection', :pid)"),
                   {"pid": os.getpid()})
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.warning(f"获取采集锁失败: {e}")
        return False
    finally:
        db.close()


def release_collect_lock():
    """释放采集锁"""
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM collect_lock WHERE lock_name='collection' AND pid=:pid"),
                   {"pid": os.getpid()})
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"释放采集锁失败: {e}")
    finally:
        db.close()


def rebuild_stage_tasks(stage: int, tasks: list) -> int:
    """清空并重建某阶段的持久化任务队列.
    tasks: list[(task_key, api, kwargs_json, detail)]"""
    ensure_task_table()
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM collection_tasks WHERE stage=:s"), {"s": stage})
        for key, api, kwargs_json, detail in tasks:
            db.execute(text(
                "INSERT INTO collection_tasks (stage, task_key, api, kwargs_json, detail, status) "
                "VALUES (:s, :k, :a, :j, :d, 'pending')"
            ), {"s": stage, "k": key, "a": api, "j": kwargs_json, "d": detail})
        db.commit()
        return len(tasks)
    finally:
        db.close()


def count_stage_tasks(stage: int) -> tuple:
    """返回 (total, done+skip) 某阶段任务统计"""
    db = SessionLocal()
    try:
        total = db.execute(text("SELECT COUNT(*) FROM collection_tasks WHERE stage=:s"), {"s": stage}).scalar() or 0
        done = db.execute(text(
            "SELECT COUNT(*) FROM collection_tasks WHERE stage=:s AND status IN ('done','skip')"
        ), {"s": stage}).scalar() or 0
        return int(total), int(done)
    except Exception:
        return 0, 0
    finally:
        db.close()


def load_pending_tasks(stage: int, limit: int = 30) -> list:
    """加载某阶段待处理任务"""
    db = SessionLocal()
    try:
        return db.execute(text(
            "SELECT id, api, kwargs_json, detail, attempts FROM collection_tasks "
            "WHERE stage=:s AND status='pending' ORDER BY id LIMIT :l"
        ), {"s": stage, "l": limit}).fetchall()
    except Exception:
        return []
    finally:
        db.close()


def mark_task(task_id: int, status: str, attempts: int = 0):
    """更新任务状态"""
    db = SessionLocal()
    try:
        db.execute(text("UPDATE collection_tasks SET status=:st, attempts=:a WHERE id=:i"),
                   {"st": status, "a": attempts, "i": task_id})
        db.commit()
    except Exception:
        pass
    finally:
        db.close()


def has_pending_tasks() -> int:
    """检查是否有未完成任务(含error), 返回总数(供启动续采)"""
    db = SessionLocal()
    try:
        return int(db.execute(text(
            "SELECT COUNT(*) FROM collection_tasks WHERE status NOT IN ('done','skip')"
        )).scalar() or 0)
    except Exception:
        return 0
    finally:
        db.close()


def active_collection_tasks() -> int:
    """当前真正在执行(pending/running)的采集任务数。

    用于 is_idle / 回测忙检测等"采集是否进行中"的判断：**历史 error/pending 残留不算采集忙**，
    否则反复失败的采集任务(如部分股票某接口永久无数据)会令系统被永久判定 busy=['collection']，
    导致触发回测/进化/定时任务被无限期拒绝(实测 48 条 fina_mainbz error 卡死整个系统)。
    """
    db = SessionLocal()
    try:
        return int(db.execute(text(
            "SELECT COUNT(*) FROM collection_tasks WHERE status IN ('pending','running')"
        )).scalar() or 0)
    except Exception:
        return 0
    finally:
        db.close()


def reset_stuck_tasks(stage: int):
    """把中断遗留的running/error任务重置为pending, 以便续采(停止/重启/失败后健壮性)"""
    db = SessionLocal()
    try:
        db.execute(text(
            "UPDATE collection_tasks SET status='pending' WHERE stage=:s AND status IN ('running','error')"
        ), {"s": stage})
        db.commit()
    except Exception:
        pass
    finally:
        db.close()


def count_error_tasks(stage: int) -> int:
    """统计某阶段失败(error)任务数"""
    db = SessionLocal()
    try:
        return int(db.execute(text(
            "SELECT COUNT(*) FROM collection_tasks WHERE stage=:s AND status='error'"
        ), {"s": stage}).scalar() or 0)
    except Exception:
        return 0
    finally:
        db.close()


# ── 无数据/无权限记录表(防止空结果任务反复重建) ──────────────────────
SKIP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS collection_skip (
    api VARCHAR(50) NOT NULL,
    ts_code VARCHAR(20) NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (api, ts_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_skip_table_checked = False


def ensure_skip_table():
    """确保 collection_skip 表存在(幂等, 进程内只建一次)"""
    global _skip_table_checked
    if _skip_table_checked:
        return
    db = SessionLocal()
    try:
        db.execute(text(SKIP_TABLE_SQL))
        db.commit()
        _skip_table_checked = True
    except Exception as e:
        logger.warning(f"ensure_skip_table: {e}")
    finally:
        db.close()


def mark_collection_skip(api: str, ts_code: str):
    """记录某股票×某API确认为无数据/无权限(避免每次运行都重建补齐任务)"""
    ensure_skip_table()
    db = SessionLocal()
    try:
        db.execute(text(
            "INSERT INTO collection_skip (api, ts_code, updated_at) VALUES (:a, :t, NOW()) "
            "ON DUPLICATE KEY UPDATE updated_at = NOW()"
        ), {"a": api, "t": ts_code})
        db.commit()
    except Exception as e:
        logger.warning(f"mark_collection_skip {api}/{ts_code}: {e}")
    finally:
        db.close()


def load_collection_skip(days: int = 7) -> set:
    """加载最近 days 天内确认为无数据/无权限的 (api, ts_code) 集合"""
    ensure_skip_table()
    db = SessionLocal()
    try:
        rows = db.execute(text(
            f"SELECT api, ts_code FROM collection_skip "
            f"WHERE updated_at >= DATE_SUB(NOW(), INTERVAL {int(days)} DAY)"
        )).fetchall()
        return {(r[0], r[1]) for r in rows}
    except Exception:
        return set()
    finally:
        db.close()


def latest_report_period(today: str) -> str:
    """计算截至今日已应发布的最新报告期(YYYYMMDD).

    披露规则: 一季报(0331)→4/30前, 半年报(0630)→8/31前,
    三季报(0930)→10/31前, 年报(1231)→次年4/30前.
    """
    y = int(today[:4])
    m = int(today[4:6])
    if m >= 10:
        return f"{y}0930"
    if m >= 8:
        return f"{y}0630"
    if m >= 4:
        return f"{y}0331"
    return f"{y-1}1231"


def _report_due(info: tuple | None, today: str) -> bool:
    """按披露计划判断该股最新报告期是否已到披露时间(应发请求).

    info: (pre_date, actual_date) 或 None. 日期为 YYYYMMDD 或 "".
    返回 True=应发请求; False=未到披露日, 该报告期尚未披露, 请求必空。
    逻辑:
      - 无披露计划信息 → 保守发请求(不阻断);
      - 实际披露日已到(<=today) → 已披露, 发请求;
      - 预约披露日未到(>today) → 未披露, 跳过(精确"检查日期");
      - 预约日已到但 actual 未回填 → 发请求确认。
    """
    if not info:
        return True
    pre, act = info
    if act and act <= today:
        return True
    if pre and pre > today:
        return False
    return True


def _latest_trade_day(today: str) -> str:
    """数据库 trade_cal 中 <= today 的最近交易日(YYYYMMDD). 失败兜底 today."""
    db = SessionLocal()
    try:
        r = db.execute(text(
            "SELECT MAX(cal_date) FROM trade_cal WHERE is_open=1 AND cal_date <= :d"
        ), {"d": today}).scalar()
        return str(r).replace("-", "")[:8] if r else today
    except Exception:  # noqa: BLE001
        return today
    finally:
        db.close()


def _latest_week_day(today: str) -> str:
    """最近一个"已完成的交易周"的最后交易日(周线理论最新日期).

    周线每周一条(通常周五)。周中运行(周一~周四)时本周周线尚未生成,
    最新周线是"最近一个周五且为交易日"的日期。用于精确判断某股周线是否已到最新,
    避免周中对已到最新的股票反复拉取 0 行空转。
    """
    last = _latest_trade_day(today)
    import datetime as _dt
    d = _dt.datetime.strptime(last, "%Y%m%d")
    db = SessionLocal()
    try:
        for _ in range(30):
            ds = d.strftime("%Y%m%d")
            r = db.execute(text(
                "SELECT COUNT(*) FROM trade_cal WHERE cal_date=:d AND is_open=1"
            ), {"d": ds}).scalar()
            if r and d.weekday() == 4:   # 周五且为交易日
                return ds
            d -= _dt.timedelta(days=1)
    except Exception:  # noqa: BLE001
        return last
    finally:
        db.close()
    return last


def _is_full_task_kwargs(kwargs: dict) -> bool:
    """判断是否为全量补齐任务(仅有ts_code, 无日期范围参数)"""
    return "ts_code" in kwargs and not any(
        k in kwargs for k in ("start_date", "end_date", "period", "trade_date", "ann_date")
    )


def api_desc(name: str) -> str:
    """从 ALL_APIS 查找 API 描述"""
    for a in ALL_APIS:
        if a["name"] == name:
            return a["desc"]
    return name


async def process_stage_queue(stage: int, force_apis: set[str] | None = None) -> int:
    """从持久化队列并发处理某阶段所有 pending 任务（断点续采核心）

    force_apis：本次**强制回补**的 api 集合（半截表自愈，见 `_force_apis`）。集合内 api
    在本轮**绝不吃** collection_skip 缓存 —— 否则构建侧修好了、消费侧仍把它整体抵消
    （plans/24 §11.36.1c 实测：10,684 个任务 ≈4ms/个全部标 skip，一次 API 都没发）。
    """
    # 只检查阶段标志位: 单独停止某阶段立即生效(stop_all 也会把各阶段标志置 False)
    running_ctx = lambda: _stage_running_flags.get(stage, False)
    await asyncio.to_thread(ensure_skip_table)
    batch = STAGE_CONCURRENCY
    done_in_run = 0
    # 强制回补集合：显式传入优先，否则取阶段构建时留下的"一次性令牌"
    force: set[str] = set(force_apis) if force_apis is not None else _take_force_apis()
    # 已确认无数据/无权限的 (api, ts_code) 集合: 队列中此类历史遗留任务直接跳过, 不消耗 Tushare 配额
    skip_set = await asyncio.to_thread(load_collection_skip, 7)
    if force:
        await broadcast({"type": "stage", "content":
                         f"🔧 半截表强制回补: {', '.join(sorted(force))}（本轮忽略 skip 缓存）"})

    async def _worker(task):
        nonlocal done_in_run
        task_id, api_name, kwargs_json, detail, attempts = task
        mark_task(task_id, "running", attempts + 1)
        kwargs = json.loads(kwargs_json) if kwargs_json else {}
        # 该股票×API 近期已确认无数据/无权限 → 直接跳过, 不再发请求。
        # ★ 例外（自愈）：半截表 api 不得走这个快速通道（否则残缺永远修不上）
        ts_code = kwargs.get("ts_code")
        if ts_code and (api_name, ts_code) in skip_set and api_name not in force:
            mark_task(task_id, "skip", attempts + 1)
            done_in_run += 1
            return
        func = getattr(tushare_client, api_name, None)
        if not func:
            mark_task(task_id, "skip")
            done_in_run += 1
            return
        result = await collect(api_name, api_desc(api_name), func, detail=detail,
                               force=api_name in force, **kwargs)
        if result == "error":
            new_attempts = attempts + 1
            # 累计失败已达阈值 → 降温为 skip + 记 collection_skip(7天不再试), 防空转
            # ★ 半截表 api 不写 skip 缓存（否则下一轮自愈又被抵消）
            if new_attempts >= MAX_TASK_ERROR_ROUNDS:
                if kwargs.get("ts_code") and api_name not in force:
                    await asyncio.to_thread(mark_collection_skip, api_name, kwargs["ts_code"])
                mark_task(task_id, "skip", new_attempts)
            else:
                mark_task(task_id, "error", new_attempts)
        elif result in ("skip", "empty"):
            # 空结果/无权限 → 记录到 skip 表(所有带 ts_code 的任务), 避免下次重建再次排队空转。
            # mark_collection_skip 幂等(ON DUPLICATE KEY UPDATE), 与 collect 内写入不冲突
            if kwargs.get("ts_code") and api_name not in force:
                await asyncio.to_thread(mark_collection_skip, api_name, kwargs["ts_code"])
            mark_task(task_id, "skip", attempts + 1)
        else:
            mark_task(task_id, "done", attempts + 1)
        done_in_run += 1

    # 一次获取总任务数(运行期间队列固定, 除非重建), 替代原每任务两次COUNT
    total, done0 = count_stage_tasks(stage)
    PROGRESS.update(stage=stage, total=total, current=done0)

    while running_ctx():
        tasks = load_pending_tasks(stage, limit=batch)
        if not tasks:
            break
        # 并发处理一批任务, 配合限流器把吞吐打满到 Tushare 配额
        await asyncio.gather(*(_worker(t) for t in tasks))
        PROGRESS.update(stage=stage, total=total, current=min(total, done0 + done_in_run))
    return done_in_run


# ── 进度/广播 ────────────────────────────────────────────────────
def calc_overall_pct():
    stage = PROGRESS["stage"]
    if stage == 0:
        return 0
    completed = sum(STAGE_WEIGHTS[s] for s in range(1, stage))
    cur_pct = PROGRESS["current"] / max(PROGRESS["total"], 1)
    return round((completed + STAGE_WEIGHTS.get(stage, 0) * cur_pct) * 100)


def calc_eta():
    overall = PROGRESS["overall_pct"]
    elapsed = PROGRESS["elapsed"]
    if overall <= 0 or elapsed <= 0:
        return 0
    return round(elapsed / overall * (100 - overall))


async def broadcast(msg: dict):
    global connected_clients
    msg["ts"] = time.strftime("%H:%M:%S")
    dead = set()
    # 快照迭代: await 期间集合可能被并发增删, 避免 "Set changed size during iteration"
    for ws in list(connected_clients):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    connected_clients -= dead


def refresh_progress():
    PROGRESS["overall_pct"] = calc_overall_pct()
    PROGRESS["elapsed"] = int(time.time() - _start_time) if _start_time > 0 else 0
    PROGRESS["eta"] = calc_eta()
    PROGRESS["api_count"] = _api_call_count
    for s in PROGRESS["stages"]:
        if s["idx"] == PROGRESS["stage"]:
            s["total"] = PROGRESS["total"]
            s["current"] = PROGRESS["current"]
            s["pct"] = round(PROGRESS["current"] / max(PROGRESS["total"], 1) * 100)


async def send_progress():
    refresh_progress()
    await broadcast({"type": "progress", **PROGRESS})


async def progress_ticker():
    while _collection_running or any(_stage_running_flags.values()):
        await send_progress()
        await asyncio.sleep(1.5)
    await send_progress()


@router.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    await send_progress()
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected_clients.discard(websocket)
    except Exception:
        connected_clients.discard(websocket)


def _is_lock_error(e) -> bool:
    """判断是否为 InnoDB 锁冲突错误(1213死锁/1205锁等待超时)"""
    orig = getattr(e, "orig", None)
    errno = getattr(orig, "args", [None])[0] if orig is not None else None
    return errno in (1213, 1205)


# ── 字典表每日增量同步（plans/23 §2.5 V3 修复）────────────────────────────
# 背景：stage1_basic 把所有"一次性表"统一用 `table_has_data` 判断，导致 stock_basic
# 自 2026-08-03 起再未更新（实测：当日 5553 只交易中 30 只不在 stock_basic）→
# 回测股票池与每日推荐候选同时永久漏掉新股/次新（learing/loader.load_stock_pool 驱动）。
#
# ⚠️ 不能复用 collect()：它走 store() 的 to_sql(if_exists="append")（无幂等键），
# 每天重复拉 stock_basic 会把 5535 行重复插入（express 表 1,494 万行冗余就是这么来的）。
# 因此这里对字典表使用 delete+insert 的 upsert 语义。
DAILY_INCREMENTAL_APIS = ("stock_basic", "new_share")
_stage1_daily_synced: dict[str, str] = {}   # {api: YYYYMMDD} 进程内当日已同步标记


def upsert_dict_table(table: str, df: pd.DataFrame, key_col: str = "ts_code") -> int:
    """按唯一键 upsert 字典表（先删命中的键，再插入），保证不产生重复行。

    仅用于小体量字典表（stock_basic / new_share，数千行/天）。
    """
    if df is None or df.empty or key_col not in df.columns:
        return 0
    d = df.drop_duplicates(subset=[key_col]).copy()
    keep = [c for c in d.columns if c != "id"]
    d = d[keep]
    db = SessionLocal()
    try:
        keys = [str(k) for k in d[key_col].tolist()]
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ",".join(f":k{j}" for j in range(len(chunk)))
            params = {f"k{j}": v for j, v in enumerate(chunk)}
            db.execute(text(f"DELETE FROM {table} WHERE {key_col} IN ({placeholders})"), params)
        db.commit()
        d.to_sql(table, db.connection(), if_exists="append", index=False, method="multi", chunksize=2000)
        db.commit()
        return len(d)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning(f"upsert_dict_table {table} 失败: {e}")
        return 0
    finally:
        db.close()


async def _sync_daily_dict_tables() -> None:
    """每日增量同步字典表（stock_basic / new_share / trade_cal）。

    - stock_basic / new_share：新股上市必须当天进池（否则回测池与推荐池漏新股）
    - trade_cal：跨年交易日历（原为"有数据就永久跳过"，2027 年将会没有日历可用）
    """
    today = time.strftime("%Y%m%d")
    apis = (("stock_basic", "股票列表", tushare_client.stock_basic, "ts_code"),
            ("new_share", "新股上市", tushare_client.new_share, "ts_code"),
            ("trade_cal", "交易日历", tushare_client.trade_cal, "cal_date"))
    for name, desc, func, key_col in apis:
        if _stage1_daily_synced.get(name) == today:
            continue
        start_t = time.time()
        try:
            async with alimiter(name):
                df = await asyncio.to_thread(func)
            n = await asyncio.to_thread(upsert_dict_table, name, df, key_col)
            _stage1_daily_synced[name] = today
            await broadcast({
                "type": "api", "api": name, "desc": desc, "detail": "每日增量",
                "status": "✅" if n else "⏭️", "rows": n, "cost": f"{time.time() - start_t:.1f}s",
                "content": f"{desc} 每日增量同步 → ✅ ({n}行)",
            })
            logger.info(f"[data] {name} 每日增量同步完成: {n} 行")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[data] {name} 每日增量同步失败（下次采集重试）: {e}")


# ── 采集追溯台账（plans/23 §2.6 W2 修复）────────────────────────────────
# 问题：所有业务表都没有采集时间戳，出问题无法回答"这天数据什么时候落库、有没有补采过"。
# 决策：**不给 40+ 张表加 created_at**（对历史行会写入伪造的"当前时间"，反而误导），
#       改为统一台账 ingest_log —— 记录每次写入（api/交易日/行数/来源/时间），
#       通用、零副作用，可回答"何时采到、是否补采"。
_INGEST_LOG_READY = False
INGEST_LOG_SQL = """
CREATE TABLE IF NOT EXISTS ingest_log (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    api VARCHAR(50) NOT NULL,
    trade_date VARCHAR(10) DEFAULT NULL,
    rows_written INT DEFAULT 0,
    source VARCHAR(24) DEFAULT 'collect',
    written_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_ingest_api_date (api, trade_date),
    KEY idx_ingest_written (written_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def ensure_ingest_log() -> bool:
    """确保 ingest_log 存在（幂等，进程内只建一次）。"""
    global _INGEST_LOG_READY
    if _INGEST_LOG_READY:
        return True
    db = SessionLocal()
    try:
        db.execute(text(INGEST_LOG_SQL))
        db.commit()
        _INGEST_LOG_READY = True
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ingest_log 建表失败: {e}")
        return False
    finally:
        db.close()


def log_ingest(table: str, rows: int, trade_date: str = "", source: str = "collect") -> None:
    """写一条采集台账（失败静默，绝不影响采集主流程）。"""
    try:
        if not ensure_ingest_log():
            return
        db = SessionLocal()
        try:
            db.execute(text(
                "INSERT INTO ingest_log (api, trade_date, rows_written, source) "
                "VALUES (:a, :d, :n, :s)"),
                {"a": table, "d": trade_date or None, "n": int(rows), "s": source})
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass


# ★ 缓存带 TTL：DDL（例如刚给 `daily`/`daily_basic` 加上唯一索引）之后必须能**自动失效**，
#   否则进程里会一直用旧答案（`[['id']]`）⇒ 明明已有幂等键却仍退回 append。TTL 到期即重查。
_UNIQ_CACHE: dict[str, tuple[float, list[list[str]]]] = {}
_UNIQ_TTL = 300.0
# 冲突时不覆盖的列：唯一键本身 + 自增/日期锚点列
_UPSERT_SKIP = {"id", "ts_code", "trade_date", "cal_date", "ann_date", "end_date", "period"}


def _unique_indexes(table: str) -> list[list[str]]:
    """返回该表**全部**唯一索引的列组（**业务键优先**，自增 PRIMARY(id) 排最后）。

    ★ 踩坑记录（plans/24 §11.36.1c）：第一版只取 `ORDER BY (INDEX_NAME='PRIMARY') DESC` 的
    第一个分组 ⇒ 永远拿到 `['id']`，而 Tushare 返回的 df 里没有 `id` 列 ⇒ 退回 append，
    **缺陷等于没修**。实测确认 `adj_factor` 除 PRIMARY(id) 外还有 `uk_ts_date(ts_code,trade_date)`。
    """
    hit = _UNIQ_CACHE.get(table)
    if hit and (time.time() - hit[0]) < _UNIQ_TTL:
        return hit[1]
    groups: dict[str, list[str]] = {}
    db = SessionLocal()
    try:
        rows = db.execute(text(
            "SELECT INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND NON_UNIQUE = 0 "
            "ORDER BY (INDEX_NAME = 'PRIMARY'), INDEX_NAME, SEQ_IN_INDEX"
        ), {"t": table}).fetchall()
        for idx_name, col_name in rows:
            groups.setdefault(str(idx_name), []).append(str(col_name))
    except Exception:  # noqa: BLE001
        groups = {}
    finally:
        db.close()
    out = [cols for cols in groups.values() if cols]
    # ★ 排序必须让「恰好是自增主键 id 的那一组」永远排最后：
    #   上一版只写 `sort(key=len)`，而 `['id']` 长度 1 ⇒ 反而排最前 —— 只因 Tushare 的 df 里
    #   没有 `id` 列才"侥幸"没选错（实测：`daily` 返回 `[['id'], ['ts_code','trade_date']]`）。
    #   语义应显式表达：**业务键优先，自增 id 兜底**。
    out.sort(key=lambda k: (k == ["id"], len(k)))
    _UNIQ_CACHE[table] = (time.time(), out)
    return out


def _upsert(table: str, df: pd.DataFrame, key: list[str], db) -> int:
    """幂等写入：命中唯一键的行 UPDATE，未命中的 INSERT。分批 500 行避免 SQL 过长。

    ★ 为什么必须这样（plans/24 §11.36.1c，实测踩到）：原 `to_sql(if_exists="append")` 在**有唯一键**
    的表上，任一行冲突 ⇒ **整批 2000 行全部失败**，而 except 只打一条 warning ⇒ **静默丢数据**
    （10,684 个采集任务跑完，`adj_factor` 一行没变）。
    """
    cols = [str(c) for c in df.columns]
    touched = [c for c in cols if c not in _UPSERT_SKIP and c not in key]
    col_sql = ", ".join(f"`{c}`" for c in cols)
    ph = ", ".join(f":p{i}" for i in range(len(cols)))
    if touched:
        upd = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in touched)
    else:
        upd = ", ".join(f"`{c}`=`{c}`" for c in key)  # 全键表：冲突即空更新(幂等)
    stmt = text(f"INSERT INTO `{table}` ({col_sql}) VALUES ({ph}) ON DUPLICATE KEY UPDATE {upd}")
    recs = df.astype(object).where(pd.notnull(df), None).to_dict("records")
    n = 0
    for i in range(0, len(recs), 500):
        chunk = recs[i:i + 500]
        db.execute(stmt, [{f"p{j}": r[c] for j, c in enumerate(cols)} for r in chunk])
        n += len(chunk)
    return n


def store(table: str, df: pd.DataFrame) -> int:
    """入库(带 InnoDB 死锁/锁等待自动重试). 并发写同表时偶发1213/1205, 退避重试即可

    有唯一键的表走**幂等 upsert**（`_upsert`），无唯一键的表走原 append（行为不变）。
    """
    if df is None or df.empty:
        return 0
    last_err = None
    for attempt in range(3):
        db = SessionLocal()
        try:
            have = set(map(str, df.columns))
            key = next((k for k in _unique_indexes(table) if all(c in have for c in k)), None)
            if key:
                n = _upsert(table, df, key, db)
            else:
                # chunksize 2000: 更大批次写入, 减少DB往返, 加快入库
                df.to_sql(table, db.connection(), if_exists="append", index=False,
                          method="multi", chunksize=2000)
                n = len(df)
            db.commit()
            # 使数据管理页的统计缓存失效, 下次请求重新计算日期范围(延迟import避免循环依赖)
            try:
                from app.api.data import invalidate_table_stats
                invalidate_table_stats(table)
            except Exception:
                pass
            return n
        except Exception as e:
            db.rollback()
            last_err = e
            if _is_lock_error(e) and attempt < 2:
                time.sleep(1 + attempt * 2)  # 死锁/锁等待超时 → 指数退避重试
                continue
            logger.warning(f"{table}: {e}")
            return 0
        finally:
            db.close()
    logger.warning(f"{table}: {last_err}")
    return 0


# 可重试的瞬态错误关键词（网络超时/连接/限流）
RETRYABLE_ERR = ["timeout", "timed out", "connection", "max retries", "refused", "unreachable",
                 "502", "503", "504", "too many requests", "frequency", "频率超限", "超限", "超过",
                 # ★ InnoDB 死锁/锁等待（1213/1205）：`store()` 内部本来就把它们当"可重试"，
                 #   但 `collect()` 这一层此前不认 ⇒ 明明可重试却直接判 error（plans/24 §11.36.13）
                 "deadlock", "lock wait", "1213", "1205"]
MAX_ATTEMPTS = 3
# 单任务累计失败轮次上限(每轮含至多 MAX_ATTEMPTS 次重试)。超过则自动降温为 skip 并记入
# collection_skip(7天暂停该 api×ts_code), 避免每轮采集无限重试同一批必然失败的
# 股票×接口("没数据还在采集"、白白消耗 Tushare 配额)——对应修复 fina_mainbz 类任务
# 反复 error 后仍被重启续采/每轮全量无限重试的问题。
MAX_TASK_ERROR_ROUNDS = 4


async def collect(name: str, desc: str, func, detail: str = "", force: bool = False, **kwargs):
    """采集一个API并更新状态看板(带瞬态错误自动重试)。

    force=True 表示这是"半截表强制回补"任务（plans/24 §11.36.1c）：空结果**不写** collection_skip
    缓存 —— 该 0 行是"这次没取到"，不是"这只股票无此数据"；一旦写入缓存，下一轮自愈又会被整体抵消。
    """
    global _api_call_count
    _api_call_count += 1
    start_t = time.time()
    # 是否为"全量补齐"任务(仅ts_code, 无日期范围): 空结果需记录到skip表避免反复重建
    is_full = _is_full_task_kwargs(kwargs)

    if name in PROGRESS["api_statuses"]:
        PROGRESS["api_statuses"][name]["status"] = "running"

    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            PROGRESS["api"] = f"{desc}{' · '+detail if detail else ''}"
            # 用线程池执行阻塞操作(网络调用+to_sql), 避免阻塞事件循环导致API无响应
            # 按接口独立限流桶(每接口配额 200/min): 不同接口并行满速互不拖累
            async with alimiter(name):
                df = await asyncio.to_thread(func, **kwargs)
            n = await asyncio.to_thread(store, name, df)
            # plans/23 W2：写采集台账（api/交易日/行数/来源/时间），供追溯与"是否补采"排查
            _td = str(kwargs.get("trade_date") or kwargs.get("ann_date") or "")
            await asyncio.to_thread(log_ingest, name, n, _td, "collect")
            cost = time.time() - start_t
            status_icon = "✅" if n > 0 else "⏭️"

            if name in PROGRESS["api_statuses"]:
                PROGRESS["api_statuses"][name]["status"] = "done"
                PROGRESS["api_statuses"][name]["rows"] += n

            await broadcast({
                "type": "api", "api": name, "desc": desc, "detail": detail,
                "status": status_icon, "rows": n, "cost": f"{cost:.1f}s",
                "content": f"{desc} {detail} → {status_icon} ({n}行, {cost:.1f}s)",
            })
            if n == 0:
                # 空结果(全量补齐/增量) → 写入skip表, 避免下次重建反复排队空转。
                # 财报等低频数据在两次披露之间反复请求必然 0 行, 白白消耗 Tushare 配额
                ts_code = kwargs.get("ts_code")
                if ts_code and not force:
                    await asyncio.to_thread(mark_collection_skip, name, ts_code)
                # force(半截表自愈) 时也不上报 empty: 否则调用方会把它当"无数据"记 skip
                return "empty" if (is_full and not force) else "ok"
            return "ok"
        except Exception as e:
            last_err = e
            err_msg = str(e)
            # 权限/接口名错误 - 永久性, 不重试, 标记跳过
            is_permission_error = any(kw in err_msg for kw in ["没有接口", "没有访问权限", "请指定正确的接口名", "权限的具体详情"])
            if is_permission_error:
                cost = time.time() - start_t
                if name in PROGRESS["api_statuses"]:
                    PROGRESS["api_statuses"][name]["status"] = "done"
                    PROGRESS["api_statuses"][name]["skip_reason"] = f"无权限: {err_msg[:50]}"
                await broadcast({
                    "type": "api", "api": name, "desc": desc, "detail": detail,
                    "status": "⏭️", "rows": 0, "cost": f"{cost:.1f}s",
                    "content": f"{desc} → ⏭️ 无权限访问, 已跳过 ({err_msg[:60]})",
                })
                return "skip"
            # 瞬态错误(超时/连接/限流)才重试
            is_retryable = any(kw.lower() in err_msg.lower() for kw in RETRYABLE_ERR)
            if not is_retryable or attempt == MAX_ATTEMPTS:
                continue
            wait = attempt * 3  # 3s, 6s 递增退避
            if name in PROGRESS["api_statuses"]:
                PROGRESS["api_statuses"][name]["status"] = "running"
            await broadcast({
                "type": "api", "api": name, "desc": desc, "detail": detail,
                "status": "🔁", "rows": 0, "cost": f"{time.time()-start_t:.1f}s",
                "content": f"{desc} {detail} → 🔁 重试{attempt}/{MAX_ATTEMPTS} ({err_msg[:50]})",
            })
            await asyncio.sleep(wait)

    # 重试耗尽, 标记错误
    e = last_err
    cost = time.time() - start_t
    err_msg = str(e) if e else "未知错误"
    # ★ 必须落日志（plans/24 §11.36.13）：此前这里**只 broadcast 不写 logger** ⇒
    #   事后排查"这批任务为什么 error"时日志里什么都没有，只能靠人工复现 ——
    #   而瞬态错误**复现不出来**（实测：39 个 error 的样本原样重发，Tushare 正常返回、
    #   `_upsert` 成功 ⇒ 结论只能是"瞬态"，却无证据链）。可观测性缺口就是缺陷。
    logger.warning(f"[collect] {name} {detail} 失败（已重试 {MAX_ATTEMPTS} 次）: "
                   f"{type(e).__name__ if e else 'UnknownError'}: {err_msg[:300]}")
    if name in PROGRESS["api_statuses"]:
        PROGRESS["api_statuses"][name]["status"] = "error"
    await broadcast({
        "type": "api", "api": name, "desc": desc, "detail": detail,
        "status": "❌", "rows": 0, "cost": f"{cost:.1f}s",
        "content": f"{desc} {detail} → ❌ {err_msg} ({cost:.1f}s)",
    })
    return "error"


async def skip_api(name: str, desc: str, reason: str):
    """跳过已有数据的API, 标记为done"""
    if name in PROGRESS["api_statuses"]:
        PROGRESS["api_statuses"][name]["status"] = "done"
        PROGRESS["api_statuses"][name]["skip_reason"] = reason
    await broadcast({
        "type": "api", "api": name, "desc": desc, "detail": f"⏭️ 跳过({reason})",
        "status": "⏭️", "rows": 0, "cost": "0s",
        "content": f"{desc} → ⏭️ 已有数据, 跳过 ({reason})",
    })


def mark_stage_apis(stage: int, status: str):
    for name, info in PROGRESS["api_statuses"].items():
        if info["stage"] == stage:
            info["status"] = status


# ── 阶段1: 基础信息（持久化队列）─────────────────────────────────
async def stage1_basic() -> tuple:
    """构建阶段1任务并持久化. 返回 (total, done)"""
    # 续采模式
    total, done = count_stage_tasks(1)
    if total > 0 and done < total:
        return (total, done)

    tasks = []

    # ── 每日增量同步字典表（plans/23 §2.5 V3 修复）──
    # 修复前：stock_basic / trade_cal 被当作"一次性表"（table_has_data 为真即永久跳过），
    # 实测 stock_basic 停在 2026-08-03 → 30 只新上市股票不在股票池。
    await _sync_daily_dict_tables()

    # 一次性拉取的API（不需要参数）: 无数据才加入
    # 已移除无权限接口(实测拒绝): concept / ths_index / concept_detail / ths_member
    # → 避免白白消耗限流额度
    # 注：stock_basic / new_share / trade_cal 已改为每日 upsert 同步（见上），
    #     不再走"有数据就永久跳过"的一次性路径。
    direct_apis = ["index_basic"]
    for name in direct_apis:
        if not await asyncio.to_thread(table_has_data, name):
            tasks.append((f"s1:{name}::", name, "{}", api_desc(name)))

    # index_weight: 需要 index_basic 有数据后才枚举(依赖)
    if not table_has_data("index_weight"):
        async with alimiter("index_basic"):
            idx_df = tushare_client.index_basic()
        if idx_df is not None and not idx_df.empty:
            index_codes = idx_df["ts_code"].tolist()
        else:
            index_codes = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH"]
        for code in index_codes:
            tasks.append((f"s1:index_weight:{code}", "index_weight", json.dumps({"index_code": code}), f"指数成分权重 {code}"))

    total = rebuild_stage_tasks(1, tasks)
    return (total, 0)


# ── [已删除] _collect_concept_detail / _collect_ths_member（plans/23 §2.5 V4 清理）──
# 这两个函数原负责"从 concept / ths_index 循环拉取成分股"，但 stage1_basic 已把
# concept / ths_index / concept_detail / ths_member 判为"实测无权限"并移除了调用 →
# 成为死代码（造成 代码 / 规划文档 / tushare_api 封装 三方不一致，维护必踩坑）。
# 待 §2.2 用真实 token 实测确权后，再按结论重新实现（此处不再保留误导性死代码）。


# ── 阶段2: 日频批量（持久化队列）────────────────────────────────
async def stage2_daily() -> tuple:
    """构建阶段2任务并持久化. 返回 (total, done)"""
    async with alimiter("trade_cal"):
        cal = tushare_client.trade_cal(start_date="20100101", end_date=time.strftime("%Y%m%d"))
    all_dates = sorted(cal[cal["is_open"] == 1]["cal_date"].tolist()) if cal is not None else []
    if not all_dates:
        return (0, 0)

    # 已移除无权限接口(实测拒绝): limit_list_d / hsgt / ths_daily → 减少21%请求
    apis: list[tuple[str, str, str]] = [
        ("daily", "日线行情", "trade_date"),
        ("daily_basic", "每日指标", "trade_date"),
        ("moneyflow", "资金流向", "trade_date"),
        ("stk_limit", "涨跌停", "trade_date"),
        ("block_trade", "大宗交易", "trade_date"),
        ("suspend_d", "停复牌", "trade_date"),
        ("forecast", "业绩预告", "ann_date"),
        ("express", "业绩快报", "ann_date"),
        ("margin", "融资融券", "trade_date"),
        ("index_daily", "指数日线", "trade_date"),
        ("index_dailybasic", "大盘指标", "trade_date"),
    ]

    # 续采模式
    total, done = count_stage_tasks(2)
    if total > 0 and done < total:
        return (total, done)

    # 增量关键：每个API独立计算增量起点, 避免"跨表取最大值"导致落后表历史缺口无法补齐
    tasks = []
    for j, (name, desc, param) in enumerate(apis):
        info = next((a for a in ALL_APIS if a["name"] == name), None)
        max_existing = "20100101"
        if info and info.get("date_col"):
            md = await asyncio.to_thread(get_max_date, name, info["date_col"])
            if md and md > max_existing:
                max_existing = md
        for date in all_dates:
            if date <= max_existing:
                continue
            day_detail = f"{date[:4]}-{date[4:6]}-{date[6:]}"
            tasks.append((f"s2:{name}:{date}", name, json.dumps({param: date}),
                          f"{day_detail} · {desc} (API{j+1}/{len(apis)})"))

    total = rebuild_stage_tasks(2, tasks)
    return (total, 0)


# ── 阶段3: 个股循环（持久化队列: 补齐缺失 → 增量最新）──────────────
async def stage3_stocks() -> tuple:
    """构建阶段3任务并持久化. 返回 (total, done). 有pending则续采不重建"""
    db = SessionLocal()
    try:
        rows = db.execute(text("SELECT ts_code FROM stock_basic")).fetchall()
        all_stocks = [r[0] for r in rows]
    except Exception:
        all_stocks = []
    finally:
        db.close()
    if not all_stocks:
        return (0, 0)

    apis = [
        ("adj_factor", "复权因子"), ("dividend", "分红送配"), ("stk_holdernumber", "股东人数"),
        ("fina_indicator", "财务指标"), ("income", "利润表"), ("balancesheet", "资产负债表"),
        ("cashflow", "现金流量表"), ("fina_mainbz", "主营构成"), ("top10_holders", "前十大股东"),
        ("top10_floatholders", "前十大流通"), ("stk_holdertrade", "增减持"),
        ("repurchase", "回购"), ("stk_rewards", "股权激励"),
        ("pledge_stat", "股权质押"), ("weekly", "周线行情"),
        ("stock_company", "公司信息"), ("namechange", "股票曾用名"),
    ]

    # 增量API: {api: (增量日期列, 参数名, 类型)}
    #   ann    → 按公告日增量(start_date/end_date 过滤 ann_date), 无新公告则跳过
    #   trade  → 按交易日增量(adj_factor/weekly), 已到最新则跳过
    #   period → 按报告期增量(fina_mainbz 用 period 参数, 只拉单期)
    incremental_apis = {
        "adj_factor": ("trade_date", "start_date", "trade"),
        "fina_indicator": ("ann_date", "start_date", "ann"),
        "income": ("ann_date", "start_date", "ann"),
        "balancesheet": ("ann_date", "start_date", "ann"),
        "cashflow": ("ann_date", "start_date", "ann"),
        "fina_mainbz": ("end_date", "period", "period"),
        "top10_holders": ("ann_date", "start_date", "ann"),
        "top10_floatholders": ("ann_date", "start_date", "ann"),
        "weekly": ("trade_date", "start_date", "trade"),
    }

    # ★ 自愈（plans/24 §11.35.4 / §11.36.1c）：若某 trade 表的"最新日"是**半截日**（行数 < 自然基数下限），
    #   则把它登记进 `_force_apis`（一次性"强制回补"令牌），三处同时生效：
    #     ① 构建侧：不按"该股已有该日任意一行"跳过；
    #     ② 消费侧：不吃 collection_skip 快速通道（否则 4ms/任务全部标 skip）；
    #     ③ 空结果也不写 collection_skip（否则下一轮又被缓存抵消）。
    #   病根：增量判据把「存在」当「完整」（实测 20260917/18/21 仅 3519/443/443 行，Tushare 侧 5565）。
    #   与门禁共用同一个"有效交易日"定义（app.backtest.data_validator.effective_latest_date）。
    #   检测刻意放在"续采判断"之前，保证任何路径（新建 / 续采）都先算出强制回补集合。
    global _force_apis
    incomplete_tables: set[str] = set()
    try:
        from app.backtest.data_validator import effective_latest_date
        for _n, (_dc, _p, _k) in incremental_apis.items():
            if _k != "trade":
                continue
            _e = effective_latest_date(_n, date_col=_dc)
            if _e["raw"] and _e["effective"] and _e["effective"] != _e["raw"]:
                incomplete_tables.add(_n)
                logger.warning(
                    f"[data] {_n} 最新日 {_e['raw']} 仅 {_e['rows'].get(_e['raw'], 0)} 行"
                    f"（< {_e['min_rows']}）⇒ 本次强制回补最近交易日（自愈）")
    except Exception as _e3:  # noqa: BLE001
        logger.warning(f"[data] 半截日检测失败（不影响采集）: {_e3}")
    _force_apis = set(incomplete_tables)

    # 续采模式: 已有pending任务则不重建队列
    total, done = count_stage_tasks(3)
    if total > 0 and done < total:
        return (total, done)

    # ═══ 批量预查询(线程池执行, 避免大表全表扫描阻塞事件循环) ════
    have_sets = await asyncio.to_thread(
        lambda: {name: get_stocks_with_data(name) for name, _ in apis}
    )
    today = time.strftime("%Y%m%d")
    # 最近7天内已确认"该股票×该API无数据/无权限"的集合, 避免反复重建全量补齐任务
    skip_set = await asyncio.to_thread(load_collection_skip, 7)
    # 披露计划(当前应有报告期): 按"预约披露日/实际披露日"精确判断哪些股票应发请求。
    # 未到披露日的股票该报告期必然 0 行, 直接跳过 —— 这是"检查日期"而非固定时间窗口。
    period_now = latest_report_period(today)
    disclosure_map = {}
    try:
        async with alimiter("disclosure_date"):
            df_d = await asyncio.to_thread(tushare_client.disclosure_date, end_date=period_now)
        if df_d is not None and not df_d.empty:
            for _, r in df_d.iterrows():
                tc = str(r.get("ts_code") or "")
                pre = str(r.get("pre_date") or "").replace("-", "")[:8]
                act = str(r.get("actual_date") or "").replace("-", "")[:8]
                if tc:
                    disclosure_map[tc] = (pre, act)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"加载披露计划失败(降级为全量轮询): {exc}")
    # 增量基准数据: 公告日(ann)与报告期(end_date)分别统计
    max_dates = {}
    ann_end_dates = {}
    for name, _ in apis:
        if name in incremental_apis:
            date_col, _, kind = incremental_apis[name]
            max_dates[name] = await asyncio.to_thread(get_stock_max_dates, name, date_col)
            if kind == "ann":
                ann_end_dates[name] = await asyncio.to_thread(get_stock_max_dates, name, "end_date")

    # trade 类型(日频/周频)的"理论最新日期": 用交易日历精确计算, 避免周中空转。
    #   adj_factor → 最近交易日; weekly → 最近已完成的周五(周线)
    trade_latest = {
        "adj_factor": _latest_trade_day(today),
        "weekly": _latest_week_day(today),
    }

    # ═══ 构建任务清单: (task_key, api, kwargs_json, detail) ════
    tasks = []
    # 1) 补齐缺失: 每只股票 × 每个表, 若该股该表无数据且未被skip
    for s in all_stocks:
        for name, _desc in apis:
            if s in have_sets[name]:
                continue
            if (name, s) in skip_set:
                continue
            tasks.append((f"s3:{name}:{s}:full", name, json.dumps({"ts_code": s}), f"{s} · 补齐{api_desc(name)}"))

    # 2) 增量最新: 仅对已存在数据的股票, 且确实可能产生新数据时才发请求
    for s in all_stocks:
        for name, (_, _, kind) in incremental_apis.items():
            # 7天内该股票×该API已确认无新数据(增量空结果已写入skip) → 跳过, 避免反复空转
            # ★ 例外（plans/24 §11.36.1 收口）：该表被判定为"半截表"时，skip 缓存必须**失效**——
            #   否则自愈会被这个 7 天缓存整体抵消（实测：只建出 1 个任务、1 秒就"采集完成"）。
            if (name, s) in skip_set and name not in incomplete_tables:
                continue
            max_date = max_dates[name].get(s)
            if not max_date:
                continue
            if kind == "ann":
                # 已含最新报告期 → 无新数据, 跳过(避免每只股票每天空转一次)
                if ann_end_dates[name].get(s, "") >= period_now:
                    continue
                # 未到披露日(检查披露计划日期) → 该报告期尚未披露, 请求必空, 跳过
                if not _report_due(disclosure_map.get(s), today):
                    continue
                start = add_days(max_date)  # 仅拉取最新公告日之后的新公告
                if start > today:
                    continue
                tasks.append((f"s3:{name}:{s}:incr:{start}", name,
                              json.dumps({"ts_code": s, "start_date": start, "end_date": today}),
                              f"{s} · 增量{api_desc(name)}@{start}"))
            elif kind == "trade":
                # 已到该表"理论最新日期"(如周线已到最近完成的周五) → 跳过, 不再空转。
                # 修复: 原 start>today 判断在周中对 weekly 失效(最新周线是上周五, start 总<=today),
                # 导致已到最新的股票周中反复拉取 0 行; 改为按交易日历的理论最新日期判断。
                # ★ 例外（自愈）：该表存在"半截日"时**不跳过** ⇒ 让残缺能被下一次正常采集修好。
                if (max_date >= trade_latest.get(name, today)
                        and name not in incomplete_tables):
                    continue
                start = add_days(max_date)
                if start > today:
                    continue
                tasks.append((f"s3:{name}:{s}:incr:{start}", name,
                              json.dumps({"ts_code": s, "start_date": start}),
                              f"{s} · 增量{api_desc(name)}@{start}"))
            elif kind == "period":
                # fina_mainbz: 只拉最新已发布报告期, 且该期已入库则跳过
                period = period_now
                if period <= max_date:
                    continue
                # 未到披露日(检查披露计划日期) → 该报告期尚未披露, 请求必空, 跳过
                if not _report_due(disclosure_map.get(s), today):
                    continue
                tasks.append((f"s3:{name}:{s}:incr:{period}", name,
                              json.dumps({"ts_code": s, "period": period}),
                              f"{s} · 增量{api_desc(name)}@{period}"))

    total = rebuild_stage_tasks(3, tasks)
    return (total, 0)


# ── 阶段4: 补充数据（持久化队列）──────────────────────────────────
async def stage4_extra() -> tuple:
    """构建阶段4任务并持久化. 返回 (total, done)"""
    db = SessionLocal()
    try:
        rows = db.execute(text("SELECT ts_code FROM stock_basic LIMIT 1000")).fetchall()
        all_stocks = [r[0] for r in rows]
    except Exception:
        all_stocks = []
    finally:
        db.close()
    if not all_stocks:
        return (0, 0)

    total, done = count_stage_tasks(4)
    if total > 0 and done < total:
        return (total, done)

    have = await asyncio.to_thread(get_stocks_with_data, "monthly")
    tasks = []
    for s in all_stocks:
        if s not in have:
            tasks.append((f"s4:monthly:{s}", "monthly", json.dumps({"ts_code": s}), f"{s} · 月线行情"))

    total = rebuild_stage_tasks(4, tasks)
    return (total, 0)


# ── 独立阶段运行包装（持久化队列驱动 + 断点续采）───────────────────
async def _run_single_stage(stage: int, builder, stage_name: str) -> bool:
    """运行单个阶段: 构建/续采任务队列 → 处理队列. 返回是否被用户停止"""
    global _start_time, _api_call_count, PROGRESS
    if _stage_running_flags[stage]:
        return False

    _stage_running_flags[stage] = True
    if not _collection_running:
        _start_time = time.time()
        _api_call_count = 0
    # 手动采集与自动断点续采都置为 running, 否则前端"停止"按钮因状态非running被禁用
    PROGRESS["status"] = "running"

    # 重置上次中断遗留的 running/error 任务为 pending(停止/重启/失败后可续采)
    reset_stuck_tasks(stage)

    # 只重置当前阶段的 api_statuses
    for name, info in PROGRESS["api_statuses"].items():
        if info["stage"] == stage:
            info["status"] = "pending"
            info["rows"] = 0
            info["skip_reason"] = ""

    ticker = asyncio.create_task(progress_ticker())
    stopped = False

    try:
        await broadcast({"type": "stage", "content": f"🚀 开始阶段{stage}: {stage_name}"})
        total, done = await builder()  # 构建或续采任务队列
        if total == 0:
            await broadcast({"type": "stage", "content": f"✅ 阶段{stage}: 数据已最新, 无需采集"})
        else:
            PROGRESS.update(stage=stage, stage_name=stage_name, total=total, current=done)
            PROGRESS["stages"][stage-1].update(total=total, current=done, pct=round(done/max(total,1)*100))
            mark_stage_apis(stage, "running")
            if done > 0:
                await broadcast({"type": "stage", "content": f"⏯️ 阶段{stage}断点续采: 已{done}/{total}完成, 继续处理剩余{total-done}项..."})
            else:
                await broadcast({"type": "stage", "content": f"📊 阶段{stage}: 待处理{total-done}项(已{done}项完成), 开始..."})
            await process_stage_queue(stage)
            _t, _d = count_stage_tasks(stage)
            PROGRESS.update(stage=stage, stage_name=stage_name, total=_t, current=_d)
            PROGRESS["stages"][stage-1].update(total=_t, current=_d, pct=round(_d/max(_t,1)*100))
            mark_stage_apis(stage, "done")
            if _stage_running_flags.get(stage, False):
                elapsed = int(time.time() - _start_time) if _start_time > 0 else 0
                await broadcast({"type": "stage", "content": f"✅ 阶段{stage}完成! 处理{_d}/{_t}项, 耗时{elapsed//60}分{elapsed%60}秒"})
            else:
                stopped = True
                await broadcast({"type": "stage", "content": f"⏹️ 阶段{stage}已停止, 进度{_d}/{_t}已保存, 可随时续采"})
            errs = count_error_tasks(stage)
            if errs > 0:
                await broadcast({"type": "stage", "content": f"⚠️ 阶段{stage}: {errs}项失败, 已保留在队列, 下次开始自动重试"})
    except Exception as e:
        await broadcast({"type": "stage", "content": f"❌ 阶段{stage}异常: {e}"})
    finally:
        _stage_running_flags[stage] = False
        PROGRESS["status"] = "idle"
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        await send_progress()
    return stopped


def stop_stage(stage: int):
    """优雅停止指定阶段的采集(设置标志→循环退出→进度保存)"""
    global _stage_running_flags
    _stage_running_flags[stage] = False
    logger.info(f"已请求停止阶段{stage}采集")


def stop_all_collection():
    """优雅停止全部采集"""
    global _collection_running, _stage_running_flags
    _collection_running = False
    for s in _stage_running_flags:
        _stage_running_flags[s] = False
    logger.info("已请求停止全部采集")


async def _run_stage_locked(stage: int, builder, stage_name: str):
    """独立按钮触发入口: 获取跨进程采集锁后运行单个阶段"""
    if not try_acquire_collect_lock():
        await broadcast({"type": "stage", "content": f"⏭️ 已有采集任务在运行, 阶段{stage}未启动"})
        return
    try:
        await _run_single_stage(stage, builder, stage_name)
    finally:
        release_collect_lock()


async def run_stage1():
    await _run_stage_locked(1, stage1_basic, "基础信息")


async def run_stage2():
    await _run_stage_locked(2, stage2_daily, "日频批量数据")


async def run_stage3():
    await _run_stage_locked(3, stage3_stocks, "个股数据循环")


async def run_stage4():
    await _run_stage_locked(4, stage4_extra, "补充数据")


STAGE_BUILDERS = {1: stage1_basic, 2: stage2_daily, 3: stage3_stocks, 4: stage4_extra}
STAGE_NAMES = {1: "基础信息", 2: "日频批量数据", 3: "个股数据循环", 4: "补充数据"}


async def resume_pending():
    """启动时自动续采: 处理所有有pending任务的阶段"""
    global _collection_running, _start_time, _api_call_count
    if _collection_running:
        return
    ensure_task_table()
    pending = has_pending_tasks()
    if pending <= 0:
        return
    # 跨进程互斥: 已有采集在运行则跳过本次自动续采
    if not try_acquire_collect_lock():
        logger.info("检测到采集已在其他进程运行, 跳过自动断点续采")
        return
    _collection_running = True
    _start_time = time.time()
    _api_call_count = 0
    logger.info(f"检测到 {pending} 项未完成任务, 开始断点续采")
    try:
        # 阶段2(日频)优先于阶段3(个股循环): 避免日线等日频数据被大量个股任务长期阻塞
        for stage in (2, 3, 4, 1):
            total, done = count_stage_tasks(stage)
            if total > 0 and done < total:
                stopped = await _run_single_stage(stage, STAGE_BUILDERS[stage], STAGE_NAMES[stage])
                if stopped:
                    logger.info(f"阶段{stage}被停止, 终止断点续采")
                    break
    except Exception as e:
        logger.warning(f"断点续采异常: {e}")
    finally:
        _collection_running = False
        release_collect_lock()
        await send_progress()


# ── 主采集流程 ─────────────────────────────────────────────────────
async def run_full_collection() -> dict:
    """全量采集（4阶段依次执行）。返回执行结果字典供上层判断是否真正执行采集。

    Returns:
        {"status": "done", "api_calls": int, "elapsed": int}   正常完成
        {"status": "skipped", "reason": "running"|"lock"}      被其他采集占用/锁冲突，未执行
        {"status": "error", "error": str}                      采集过程异常
    """
    global _collection_running, PROGRESS, _start_time, _api_call_count
    if _collection_running:
        logger.warning("[data] 全量采集被跳过: 已有采集任务在本进程运行(_collection_running)")
        return {"status": "skipped", "reason": "running"}
    # 跨进程互斥: 已有采集在运行则跳过
    if not try_acquire_collect_lock():
        await broadcast({"type": "stage", "content": "⏭️ 已有采集任务在运行, 全量采集未启动"})
        logger.warning("[data] 全量采集被跳过: 跨进程采集锁被占用")
        return {"status": "skipped", "reason": "lock"}
    _collection_running = True
    _start_time = time.time()
    _api_call_count = 0

    PROGRESS = {
        "stage": 0, "stage_name": "", "total": 0, "current": 0,
        "api": "", "status": "running", "overall_pct": 0,
        "elapsed": 0, "eta": 0, "api_count": 0,
        "stages": [
            {"idx": 1, "name": "基础信息", "total": 0, "current": 0, "pct": 0},
            {"idx": 2, "name": "日频批量数据", "total": 0, "current": 0, "pct": 0},
            {"idx": 3, "name": "个股数据循环", "total": 0, "current": 0, "pct": 0},
            {"idx": 4, "name": "补充数据", "total": 0, "current": 0, "pct": 0},
        ],
        "api_statuses": build_api_statuses(),
    }

    ticker = asyncio.create_task(progress_ticker())

    try:
        await broadcast({"type": "stage", "content": "🚀 开始增量采集 (42个API, 已有数据自动跳过)"})
        for stage in (1, 2, 3, 4):
            if _collection_running:
                stopped = await _run_single_stage(stage, STAGE_BUILDERS[stage], STAGE_NAMES[stage])
                if stopped:
                    logger.info(f"阶段{stage}被停止, 终止后续阶段")
                    break

        PROGRESS["status"] = "done"
        PROGRESS["overall_pct"] = 100
        PROGRESS["api"] = ""
        for s in PROGRESS["stages"]:
            s["pct"] = 100
        elapsed = int(time.time() - _start_time)
        await broadcast({"type": "stage", "content": f"✅ 增量采集完成! 耗时{elapsed//60}分{elapsed%60}秒, 共{_api_call_count}次API调用"})
        return {"status": "done", "api_calls": _api_call_count, "elapsed": elapsed}
    except Exception as e:
        PROGRESS["status"] = "error"
        await broadcast({"type": "stage", "content": f"❌ 采集异常: {e}"})
        return {"status": "error", "error": str(e)}
    finally:
        _collection_running = False
        release_collect_lock()
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        await send_progress()


@router.get("/ws/progress")
async def get_progress():
    refresh_progress()
    return PROGRESS
