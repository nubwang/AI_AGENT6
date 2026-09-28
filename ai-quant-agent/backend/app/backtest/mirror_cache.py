"""本地镜像缓存（mirror_cache）— plans/23 §4.5 / §十三 W4：回测提速

**实测瓶颈（[`profile_backtest.py`](ai-quant-agent/backend/scripts/profile_backtest.py:1)，2026-09-18）**
```
stage                    sec    pct   sql calls
build_extra_map       690.85  92.8%   181    10   ← 单股 ~69s（含一次性开销）
prepare_stock_data     51.81   7.0%   224    56
其余（分类/分流/特征/入场）      ≈0
```
单表定位（同一股票连续查两次）：
```
daily_basic 首查 1.80s → 再查 0.17s
moneyflow   首查 2.00s → 再查 0.17s
```
→ 结论：**不是 SQL 写法慢，而是 MySQL 冷读放大**。回测逐股循环，每次都在读"新的"索引页
（daily_basic 索引 988MB / moneyflow 965MB），InnoDB buffer pool 装不下 → 每只股票都要随机盘读，
单股 ~4s，全量回测 ~80 分钟。

**做法**：把这些"逐股按 ts_code 查大表"的数据**一次性顺序扫出来**，落到本地 SQLite 镜像
（`data/mirror.db`，带 (ts_code, date_col) 索引），之后逐股查询走本地文件
（OS page cache 友好、无随机盘读；实测单股 ~20ms）。

**构建路径（性能关键，踩过坑）**
- ❌ `pd.read_sql(chunksize=...)` + `to_sql`：客户端 Decimal→object 转换成为瓶颈
  （40 万行要 3 分钟，1400 万行 ≈ 30+ 分钟，且 92% CPU 全在客户端）
- ✅ **服务端流式导出 TSV（`stream_results`）→ `sqlite3 .import` 批量装载**
  （零逐行 ORM 开销，分钟级完成；`.import` 对已存在的表是追加语义）
- 数值列用 REAL 亲和性建表：否则下游 `df[cols].sum(axis=1)` 会变成字符串拼接（静默 NaN）

**安全**：镜像只读、只写自己的 `data/mirror.db`，不碰业务表；开关 `extra_mirror_enabled=0`
即回退 MySQL（行为与优化前完全一致）。
"""
from __future__ import annotations

import csv
import os
import re
import shutil
import sqlite3
import subprocess
import time

import pandas as pd
from sqlalchemy import text

from app.core.config import settings
from app.core.logger import logger
from app.models import SessionLocal, engine

MIRROR_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "mirror.db",
)
TSV_DIR = os.path.join(os.path.dirname(MIRROR_DB), "_mirror_tmp")

# 需要镜像的表 → 列定义（只存回测真正用到的列，控制体积）
MIRRORED: dict[str, dict] = {
    "daily_basic": {
        "cols": ["ts_code", "trade_date", "turnover_rate", "pe_ttm", "pb", "circ_mv"],
        "date_col": "trade_date",
        "numeric": ["turnover_rate", "pe_ttm", "pb", "circ_mv"],
    },
    "moneyflow": {
        "cols": ["ts_code", "trade_date", "buy_sm_amount", "buy_md_amount",
                 "buy_lg_amount", "buy_elg_amount", "sell_sm_amount", "sell_md_amount",
                 "sell_lg_amount", "sell_elg_amount", "net_mf_amount"],
        "date_col": "trade_date",
        "numeric": ["buy_sm_amount", "buy_md_amount", "buy_lg_amount", "buy_elg_amount",
                    "sell_sm_amount", "sell_md_amount", "sell_lg_amount", "sell_elg_amount",
                    "net_mf_amount"],
    },
    # ── 其余"逐股查"的表（[`build_extra_map`](ai-quant-agent/backend/app/backtest/extra_feature_loader.py:238) 还在查 15 张表）──
    # 首版只镜像了 daily_basic/moneyflow，实测 build_extra_map 从 4.3s/股 → 1.66s/股（2.6x），
    # 剩余成本就摊在这些小表上（每张几十~几百毫秒的冷读 × 16 张）。一并镜像后逐股查询基本零等待。
    # 注意：`namechange` 主键不是 id（无法用 id 游标增量）→ 保持 MySQL（每股仅 4 行，可忽略）。
    "fina_indicator": {
        "cols": ["ts_code", "ann_date", "end_date", "roe", "grossprofit_margin",
                 "netprofit_yoy", "ocfps", "bps", "eps"],
        "date_col": "ann_date",
        "numeric": ["roe", "grossprofit_margin", "netprofit_yoy", "ocfps", "bps", "eps"],
    },
    "stk_holdernumber": {
        "cols": ["ts_code", "ann_date", "end_date", "holder_num"],
        "date_col": "ann_date", "numeric": ["holder_num"],
    },
    "income": {
        "cols": ["ts_code", "ann_date", "end_date", "revenue"],
        "date_col": "ann_date", "numeric": ["revenue"],
    },
    "balancesheet": {
        "cols": ["ts_code", "ann_date", "end_date", "total_assets", "total_liab"],
        "date_col": "ann_date", "numeric": ["total_assets", "total_liab"],
    },
    # ⚠️ fina_mainbz 暂不镜像：实测导出 928,499 行 → 镜像只落 224,346 行（丢 70 万行，24%），
    # 而同类表（top10/pledge/income/…）全部逐行一致。用 SQL 检查过 tab/换行只有 1~2 行，
    # 不足以解释；为不把"没查清的问题"带进回测，先退回 MySQL 直查（该表只服务
    # mainbz_concentration 一个特征，每股一次小查询，代价可忽略）。待定位后再启用。
    "fina_mainbz": {
        "cols": ["ts_code", "end_date", "bz_item", "bz_sales"],
        "date_col": "end_date", "numeric": ["bz_sales"],
        "disabled": True,
    },
    "top10_holders": {
        "cols": ["ts_code", "ann_date", "end_date", "hold_ratio"],
        "date_col": "ann_date", "numeric": ["hold_ratio"],
    },
    "pledge_stat": {
        "cols": ["ts_code", "end_date", "pledge_ratio"],
        "date_col": "end_date", "numeric": ["pledge_ratio"],
    },
    "dividend": {
        "cols": ["ts_code", "ann_date", "ex_date"], "date_col": "ann_date",
    },
    "repurchase": {
        "cols": ["ts_code", "ann_date"], "date_col": "ann_date",
    },
    "stk_rewards": {
        "cols": ["ts_code", "ann_date"], "date_col": "ann_date",
    },
    "stk_holdertrade": {
        "cols": ["ts_code", "ann_date", "in_de", "change_ratio"],
        "date_col": "ann_date", "numeric": ["change_ratio"],
    },
    "forecast": {
        "cols": ["ts_code", "ann_date", "type"], "date_col": "ann_date",
    },
    "express": {
        "cols": ["ts_code", "ann_date", "yoy_net_profit"],
        "date_col": "ann_date", "numeric": ["yoy_net_profit"],
    },
    "block_trade": {
        "cols": ["ts_code", "trade_date", "price"],
        "date_col": "trade_date", "numeric": ["price"],
    },
    "weekly": {
        "cols": ["ts_code", "trade_date", "close"],
        "date_col": "trade_date", "numeric": ["close"],
    },
}

# 无 id 主键、不能走 id 游标增量的表（保持 MySQL 直查）
UNMIRRORABLE = ("namechange", "fina_mainbz")


def is_mirrored(table: str) -> bool:
    """该表是否可以走镜像（存在配置、未禁用、且镜像已就绪）。"""
    cfg = MIRRORED.get(table)
    if not cfg or cfg.get("disabled"):
        return False
    return is_ready(table)

_conn: sqlite3.Connection | None = None


def enabled_tables() -> list[str]:
    """可镜像（且已通过一致性验证）的表。"""
    return [t for t, c in MIRRORED.items() if not c.get("disabled")]


def _ddl(table: str) -> str:
    """按 affinity 建表 DDL（ts_code/日期 TEXT，数值列 REAL）。"""
    cfg = MIRRORED[table]
    numeric = set(cfg.get("numeric") or [])
    parts = [f"{c} {'REAL' if c in numeric else 'TEXT'}" for c in cfg["cols"]]
    return f"CREATE TABLE IF NOT EXISTS {table} (" + ", ".join(parts) + ")"


def _stage_table(table: str) -> str:
    return f"{table}__stage"


def _stage_ddl(table: str) -> str:
    """中转表 DDL：多一个 id 列（导出时带 id 用于推进增量游标）。

    ⚠️ 为什么必须先落中转表：导出 TSV 的第一列是 `id`，而目标表没有 id 列，
    直接 `.import` 会导致**整行列错位**（ts_code 列里装的是 id —— 实测踩到，
    表现为 mirror_query 返回 0 行）。所以先导入中转表，再按列名搬运。
    """
    cfg = MIRRORED[table]
    numeric = set(cfg.get("numeric") or [])
    parts = [f"id INTEGER"] + [f"{c} {'REAL' if c in numeric else 'TEXT'}" for c in cfg["cols"]]
    return f"CREATE TABLE IF NOT EXISTS {_stage_table(table)} (" + ", ".join(parts) + ")"


def _get_conn() -> sqlite3.Connection:
    """进程级 SQLite 连接（查询用）。"""
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(MIRROR_DB), exist_ok=True)
        _conn = sqlite3.connect(MIRROR_DB, check_same_thread=False, timeout=30.0)
        try:
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA cache_size=-65536")  # 64MB 页缓存
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"mirror PRAGMA 设置失败: {exc}")
    return _conn


def _meta_get(table: str) -> dict:
    conn = _get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mirror_meta ("
            "table_name TEXT PRIMARY KEY, last_id INTEGER, src_max_date TEXT, "
            "rows INTEGER, built_at REAL)")
        cur = conn.execute(
            "SELECT last_id, src_max_date, rows, built_at FROM mirror_meta WHERE table_name=?",
            (table,))
        row = cur.fetchone()
        if not row:
            return {"last_id": 0, "src_max_date": "", "rows": 0, "built_at": 0.0}
        return {"last_id": int(row[0] or 0), "src_max_date": str(row[1] or ""),
                "rows": int(row[2] or 0), "built_at": float(row[3] or 0.0)}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"mirror_meta 读取失败: {exc}")
        return {"last_id": 0, "src_max_date": "", "rows": 0, "built_at": 0.0}


def _meta_set(table: str, last_id: int, src_max_date: str, rows: int) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO mirror_meta(table_name, last_id, src_max_date, rows, built_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(table_name) DO UPDATE SET "
        "last_id=excluded.last_id, src_max_date=excluded.src_max_date, "
        "rows=excluded.rows, built_at=excluded.built_at",
        (table, int(last_id), str(src_max_date)[:10], int(rows), time.time()))
    conn.commit()


def _src_status(table: str, date_col: str) -> tuple[int, str]:
    """源表 (max(id), max(date_col))。失败返回 (0, '')。"""
    db = SessionLocal()
    try:
        row = db.execute(text(f"SELECT MAX(id), MAX({date_col}) FROM {table}")).fetchone()
        if not row:
            return 0, ""
        return int(row[0] or 0), str(row[1] or "")[:10]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"mirror 源表状态读取失败 {table}: {exc}")
        return 0, ""
    finally:
        db.close()


def _select_exprs(table: str) -> str:
    """生成导出用 SELECT 表达式：数值列 IFNULL 兜空，文本列清洗 TAB/换行。

    文本列含 TAB/换行会破坏 TSV 列结构（`.import` 会整行报错丢弃 → 静默少数据），
    故对所有非数值列统一做防御性清洗（实测该表的极端脏值只有 1~2 行，但代价为零）。
    """
    cfg = MIRRORED[table]
    numeric = set(cfg.get("numeric") or [])
    out: list[str] = []
    for c in cfg["cols"]:
        if c in numeric:
            out.append(f"IFNULL({c},'')")
        elif c == "ts_code":
            out.append(c)
        else:
            out.append(f"REPLACE(REPLACE(REPLACE({c},'\\t',' '),'\\n',' '),'\\r',' ')")
    return ", ".join(out)


def _export_tsv(table: str, after_id: int, path: str) -> tuple[int, int]:
    """服务端流式导出 `id > after_id` 到 TSV。返回 (行数, 本批最大 id)。

    数值列用 IFNULL(col,'') 规避 NULL；字符串里的制表/换行会破坏 TSV 列结构，故清洗为空格。
    """
    cfg = MIRRORED[table]
    sel = _select_exprs(table)
    n = 0
    max_id = int(after_id)
    eng = engine.execution_options(stream_results=True, max_row_buffer=10000)
    with eng.connect() as conn:
        rs = conn.exec_driver_sql(
            f"SELECT id, {sel} FROM {table} WHERE id > %s ORDER BY id", (int(after_id),))
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, delimiter="\t", lineterminator="\n",
                           quoting=csv.QUOTE_NONE, quotechar="", escapechar="\\")
            for row in rs:
                vals = []
                for v in row:
                    if v is None:
                        vals.append("")
                    elif isinstance(v, (int, float)):
                        vals.append(str(v))
                    else:
                        vals.append(str(v).replace("\t", " ").replace("\n", " ").replace("\r", " "))
                try:
                    max_id = int(float(vals[0])) if vals[0] else max_id
                except (TypeError, ValueError):
                    pass
                w.writerow(vals)
                n += 1
    return n, max_id


def _mysql_cli() -> tuple[list[str], dict, str] | None:
    """解析 settings.database_url → mysql CLI 连接参数（密码放 MYSQL_PWD，不进 argv）。"""
    exe = shutil.which("mysql")
    if not exe:
        return None
    m = re.match(r"mysql\+pymysql://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)", settings.database_url or "")
    if not m:
        return None
    user, pwd, host, port, db = m.groups()
    # 两个必加参数（都实测踩过）：
    #   --quick：CLI 默认把**整个结果集缓存到内存**后才输出，1400 万行会先静默几分钟
    #            （实测 TSV 长时间 0 字节）再一次性吐出来；--quick 改为流式。
    #   -N    ：不带列名。默认会输出一行表头，表头里是 `IFNULL(...)` 表达式原文
    #            （实测首行变成 ('ts_code','trade_date','IFNULL(turnover_rate,...')）→ 脏数据。
    args = [exe, "--batch", "--raw", "--quick", "-N", "-h", host, "-P", port, "-u", user, db]
    env = dict(os.environ)
    env["MYSQL_PWD"] = pwd
    return args, env, db


def _scan_tsv(path: str) -> tuple[int, int]:
    """顺序扫一遍 TSV：返回 (行数, 末行 id)。"""
    n = 0
    last = ""
    with open(path, "rb") as fh:
        for i, line in enumerate(fh):
            if i == 0 and line.startswith((b"id\t", b"id ")):
                continue  # 防御：万一又带了表头
            n += 1
            if line.strip():
                last = line.split(b"\t", 1)[0].decode("utf-8", "ignore")
    try:
        return n, int(float(last))
    except (TypeError, ValueError):
        return n, 0


def _export_tsv_cli(table: str, after_id: int, path: str) -> tuple[int, int] | None:
    """用 mysql 客户端直接导出 TSV（服务端顺序扫 → 客户端直写文件，不经 Python 行循环）。

    实测：Python 逐行流式导出 1400 万行要 ~20 分钟（客户端 CPU 全在转换上），
    mysql CLI 顺序导出快一个量级。失败返回 None（调用方回退 Python 流式导出）。
    """
    cli = _mysql_cli()
    if cli is None:
        return None
    args, env, _db = cli
    cfg = MIRRORED[table]
    sel = _select_exprs(table)
    sql = f"SELECT id, {sel} FROM {table} WHERE id > {int(after_id)} ORDER BY id"
    try:
        with open(path, "wb") as fh:
            r = subprocess.run(args + ["-e", sql], stdout=fh, stderr=subprocess.PIPE, env=env,
                               timeout=7200)
        if r.returncode != 0:
            logger.warning(f"[mirror] mysql CLI 导出失败（回退 Python 流式）: "
                           f"{r.stderr.decode('utf-8', 'ignore').strip()[:200]}")
            return None
        return _scan_tsv(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[mirror] mysql CLI 导出异常（回退 Python 流式）: {exc}")
        return None


def _import_tsv(table: str, tsv: str) -> bool:
    """用 sqlite3 CLI `.import` 批量装载到**中转表**，再按列名搬进目标表。

    比 pandas/executemany 快一个量级；列错位风险由中转表 + 显式列名消除。
    """
    exe = shutil.which("sqlite3")
    if not exe:
        return False
    stage = _stage_table(table)
    conn = _get_conn()
    try:
        conn.execute(f"DROP TABLE IF EXISTS {stage}")
        conn.commit()
        conn.execute(_stage_ddl(table))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[mirror] 建中转表失败（回退逐块写入）: {exc}")
        return False
    cmd = [exe, MIRROR_DB,
           "-cmd", "PRAGMA journal_mode=WAL",
           "-cmd", "PRAGMA synchronous=OFF",
           "-cmd", ".mode tabs",
           "-cmd", f".import {tsv} {stage}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            logger.warning(f"[mirror] sqlite3 .import 失败（回退逐块写入）: {r.stderr.strip()[:200]}")
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[mirror] sqlite3 .import 异常（回退逐块写入）: {exc}")
        return False
    try:
        cols = ", ".join(MIRRORED[table]["cols"])
        conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {stage}")
        conn.commit()
        conn.execute(f"DROP TABLE IF EXISTS {stage}")
        conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[mirror] 中转表搬运失败: {exc}")
        return False


def _import_pandas(table: str, tsv: str) -> bool:
    """兜底：无 sqlite3 CLI 时用 pandas 分块写入。"""
    conn = _get_conn()
    try:
        for chunk in pd.read_csv(tsv, sep="\t", header=None, dtype=str,
                                 chunksize=200_000, na_filter=False):
            chunk.columns = MIRRORED[table]["cols"]  # type: ignore[assignment]
            for c in MIRRORED[table].get("numeric") or []:
                chunk[c] = pd.to_numeric(chunk[c], errors="coerce")
            chunk.to_sql(table, conn, if_exists="append", index=False, chunksize=10_000)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[mirror] pandas 兜底写入失败: {exc}")
        return False


# 就绪状态进程内缓存：is_ready() 早期实现用 `SELECT COUNT(*)` 判断，被逐股调用时
# 在 1400 万行表上每次耗时 ~0.9s（实测把镜像本该 ~20ms 的查询拖到 ~939ms）→ 改为
# `SELECT 1 ... LIMIT 1`（O(1)）+ 结果缓存（sync_table 后刷新）。
_READY: dict[str, bool] = {}


def is_ready(table: str) -> bool:
    """镜像是否可用（表存在且有数据）。O(1) 判定 + 进程内缓存。"""
    if table not in MIRRORED or MIRRORED[table].get("disabled"):
        return False
    cached = _READY.get(table)
    if cached is not None:
        return cached
    if not os.path.exists(MIRROR_DB):
        _READY[table] = False
        return False
    try:
        conn = _get_conn()
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            _READY[table] = False
            return False
        cur = conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        ok = bool(cur.fetchone())
    except Exception:  # noqa: BLE001
        ok = False
    _READY[table] = ok
    return ok


def _ensure_index(table: str, date_col: str) -> None:
    conn = _get_conn()
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_code_date ON {table}(ts_code, {date_col})")
    conn.commit()


def sync_table(table: str, force: bool = False) -> dict:
    """同步一张表到镜像（增量；force=True 全量重建）。

    Returns:
        {"table","rows","added","seconds","rebuilt"}
    """
    if table not in MIRRORED:
        return {"table": table, "rows": 0, "added": 0, "seconds": 0.0,
                "rebuilt": False, "error": "未配置镜像"}
    cfg = MIRRORED[table]
    date_col = cfg["date_col"]
    t0 = time.time()
    conn = _get_conn()
    meta = _meta_get(table)
    src_max_id, src_max_date = _src_status(table, date_col)

    rebuilt = False
    if force:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
        meta = {"last_id": 0, "src_max_date": "", "rows": 0, "built_at": 0.0}
        rebuilt = True
    if src_max_id and int(meta["last_id"]) >= src_max_id and not rebuilt:
        return {"table": table, "rows": meta["rows"], "added": 0,
                "seconds": round(time.time() - t0, 2), "rebuilt": False,
                "src_max_date": src_max_date, "note": "已是最新"}

    conn.execute(_ddl(table))
    conn.commit()

    os.makedirs(TSV_DIR, exist_ok=True)
    tsv = os.path.join(TSV_DIR, f"{table}.tsv")
    after = int(meta["last_id"])
    added = 0
    max_id = after
    try:
        res = _export_tsv_cli(table, after, tsv)
        if res is None:
            res = _export_tsv(table, after, tsv)
        rows, max_id = res
        if rows:
            ok = _import_tsv(table, tsv) or _import_pandas(table, tsv)
            added = rows if ok else 0
            if not ok:
                logger.error(f"[mirror] {table} 装载失败（该表将回退 MySQL 查询）")
        logger.info(f"[mirror] {table} 导出 {rows} 行（after_id={after}）")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[mirror] {table} 同步失败: {exc}")
        return {"table": table, "rows": meta["rows"], "added": added,
                "seconds": round(time.time() - t0, 2), "rebuilt": rebuilt, "error": str(exc)}
    finally:
        try:
            os.remove(tsv)
        except OSError:
            pass

    _ensure_index(table, date_col)
    cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
    rows_total = int(cur.fetchone()[0])
    _READY.pop(table, None)   # 刷新就绪缓存（增量同步后表已变）
    _meta_set(table, max_id or src_max_id or 0, src_max_date, rows_total)
    dt = round(time.time() - t0, 2)
    logger.info(f"[mirror] {table} 就绪：{rows_total} 行（本次新增 {added}，{dt}s，{MIRROR_DB}）")
    return {"table": table, "rows": rows_total, "added": added, "seconds": dt,
            "rebuilt": rebuilt, "src_max_date": src_max_date}


def ensure_mirror(tables: list[str] | None = None, force: bool = False) -> dict:
    """确保镜像就绪（增量同步）。失败不抛异常（回测可回退 MySQL）。"""
    out: dict[str, dict] = {}
    errors: list[str] = []
    for t in (tables or list(MIRRORED.keys())):
        try:
            r = sync_table(t, force=force)
            out[t] = r
            if r.get("error"):
                errors.append(f"{t}: {r['error']}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{t}: {exc}")
            out[t] = {"table": t, "error": str(exc)}
    return {"tables": out, "errors": errors}


def mirror_query(ts_code: str, table: str, cols: list[str],
                 order_col: str = "trade_date", limit: int | None = None) -> pd.DataFrame | None:
    """从镜像读一只股票的数据（升序）。镜像不可用时返回 None（调用方回退 MySQL）。"""
    if table not in MIRRORED or not is_mirrored(table):
        return None
    keep = [c for c in cols if c in MIRRORED[table]["cols"]]
    if not keep:
        return None
    sql = f"SELECT {', '.join(keep)} FROM {table} WHERE ts_code = ? ORDER BY {order_col}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    try:
        conn = _get_conn()
        df = pd.read_sql(sql, conn, params=(str(ts_code),))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"mirror_query {table}({ts_code}) 失败: {exc}")
        return None
    # 防御性数值转换（偶发脏值会以 TEXT 存储，导致下游 sum 变字符串拼接）
    for c in MIRRORED[table].get("numeric") or []:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def stats() -> dict:
    """镜像状态（供运维/健康神经元查看）。"""
    out: dict = {"db": MIRROR_DB, "exists": os.path.exists(MIRROR_DB), "tables": {}}
    if not out["exists"]:
        return out
    try:
        out["size_mb"] = round(os.path.getsize(MIRROR_DB) / 1024 / 1024, 1)
    except Exception:  # noqa: BLE001
        pass
    for t in MIRRORED:
        m = _meta_get(t)
        out["tables"][t] = {
            "rows": m["rows"], "src_max_date": m["src_max_date"],
            "built_at": (time.strftime("%Y-%m-%d %H:%M", time.localtime(m["built_at"]))
                         if m["built_at"] else ""),
            "ready": is_ready(t),
        }
    return out


__all__ = ["MIRRORED", "MIRROR_DB", "ensure_mirror", "sync_table",
           "mirror_query", "is_ready", "stats"]
