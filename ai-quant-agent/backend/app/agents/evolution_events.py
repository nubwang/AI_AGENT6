"""进化事件流（evolution_events）— 对齐 plans/11 进化Agent §5/E1/E15/F4

MVP 能力：
  - 全项目错误/告警汇聚：logger Hook（EvolutionEventHandler，防重入 E1）
  - SQLite 存储（evolution_events.db）+ 增量游标（供 summarizer 只消费新事件）
  - 写失败静默（不阻塞主流程；emit 内不调 logger，防递归风暴）
  - 异步批量 E15：后台线程批量落库（emit 只入内存队列，主流程零阻塞）
  - 噪音分级 F4：ERROR/WARNING 高价值；DEBUG/重复噪音低价值降权

说明：本模块被 main.py 在启动时 attach_logger_hook() 挂到 quant logger；
logger.py 不 import 本模块（避免循环依赖）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import sqlite3
import threading
import time

# 异步批量队列（E15）：emit 只入队，后台线程批量写库
_ASYNC_QUEUE: "queue.Queue[tuple]" = queue.Queue(maxsize=2000)
_FLUSH_INTERVAL = 1.0          # 批量刷新间隔（秒）
_BATCH_SIZE = 50               # 每批最多条数

# 噪音分级（F4）：ERROR/WARNING 高价值全记；INFO 按源合并；DEBUG 不记（防刷屏）
_NOISE_LEVELS = ("ERROR", "WARNING")

EVENTS_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "evolution_events.db",
)

_lock = threading.Lock()
_reentrant = threading.local()


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(EVENTS_DB), exist_ok=True)
    c = sqlite3.connect(EVENTS_DB, timeout=10)
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, level TEXT, source TEXT, signature TEXT,
            message TEXT, count INTEGER
        )
    """)
    return c


def _flush_events() -> None:
    """后台线程：批量消费队列 → 单次批量写入（E15，主流程零阻塞）。"""
    batch: list = []
    last_flush = time.time()
    while True:
        try:
            item = _ASYNC_QUEUE.get(timeout=_FLUSH_INTERVAL)
        except queue.Empty:
            if batch:
                _flush_batch(batch)
                batch = []
            last_flush = time.time()
            continue
        batch.append(item)
        if len(batch) >= _BATCH_SIZE or (time.time() - last_flush) >= _FLUSH_INTERVAL:
            _flush_batch(batch)
            batch = []
            last_flush = time.time()


def _flush_batch(batch: list) -> None:
    """批量写入一批事件（Bug6：同签名合并计数，减少重复噪音）。失败静默。"""
    try:
        merged: dict[str, list] = {}
        for (level, source, message) in batch:
            sig = hashlib.md5(message[:100].encode()).hexdigest()[:24]
            merged.setdefault(sig, [level, source, message[:500], 0])
            merged[sig][3] += 1
        rows = []
        for sig, (level, source, message, cnt) in merged.items():
            rows.append((time.strftime("%Y-%m-%d %H:%M:%S"), level, source, sig, message, cnt))
        with _lock:
            db = _conn()
            try:
                db.executemany(
                    "INSERT INTO events (ts, level, source, signature, message, count) VALUES (?,?,?,?,?,?)",
                    rows,
                )
                db.commit()
            finally:
                db.close()
    except Exception:  # noqa: BLE001
        pass  # 事件写入失败静默（防递归 / 不阻塞主流程 E1）


def flush_now() -> None:
    """Bug19：立即把队列剩余事件落库（供"写后立即读"场景：反思事件 → evaluate 摘要）。"""
    batch: list = []
    while True:
        try:
            batch.append(_ASYNC_QUEUE.get_nowait())
        except queue.Empty:
            break
    if batch:
        _flush_batch(batch)


def record_event(level: str, source: str, message: str, count: int = 1) -> None:
    """写入一条事件（异步批量 E15：入队后由后台线程批量落库）。"""
    # 噪音分级（F4）：DEBUG 直接丢弃；INFO 在 emit 处按源合并，这里兜底仍可入队
    if level in ("DEBUG", "TRACE"):
        return
    try:
        _ASYNC_QUEUE.put_nowait((level, source, message))
    except queue.Full:
        pass  # 队列满丢弃（限频 E9：避免日志洪峰压垮）


class EvolutionEventHandler(logging.Handler):
    """日志 → 事件流（E1 防重入：emit 内不调 logger，写失败静默）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_reentrant, "active", False):
            return  # 防重入：已在写入过程中
        _reentrant.active = True
        try:
            # 噪音分级（F4）：只记 ERROR/WARNING（高价值）；INFO/DEBUG 由显式 record_event 主动上报
            if record.levelno >= logging.WARNING:
                record_event(record.levelname, record.name, self.format(record), 1)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _reentrant.active = False


def cursor() -> int:
    """当前增量游标（已消费到的最大事件 id）。"""
    db = _conn()
    try:
        r = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        return int(r[0]) if r else 0
    finally:
        db.close()


def pending_events(after_id: int, limit: int = 500) -> list[dict]:
    """增量拉取：id > after_id 的事件（供 summarizer L0 摘要）。"""
    db = _conn()
    try:
        rows = db.execute(
            "SELECT id, ts, level, source, message FROM events WHERE id > :a ORDER BY id LIMIT :n",
            {"a": after_id, "n": limit},
        ).fetchall()
        return [{"id": r[0], "ts": r[1], "level": r[2], "source": r[3], "message": r[4]} for r in rows]
    finally:
        db.close()


def recent_errors(hours: float = 24.0, limit: int = 80) -> list[dict]:
    """近 N 小时 ERROR/WARNING 报错（供进化大脑修复 bug）。

    进化大脑"监控后台接口报错 → 修复 bug"闭环（用户新需求）：
    从事件流聚合近期 ERROR/WARNING，按 signature 合并重复（count），
    供 code_writer.propose_bug_fix 分析并生成修复补丁。

    Args:
        hours: 回溯窗口（小时），默认 24h
        limit: 最多返回条数（按重复次数降序）

    Returns:
        [{"ts","level","source","message","count"}] 按 count 降序
    """
    try:
        flush_now()  # 先落库再读，保证最近报错可见
    except Exception:  # noqa: BLE001
        pass
    db = _conn()
    try:
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        rows = db.execute(
            "SELECT ts, level, source, message, count FROM events "
            "WHERE level IN ('ERROR','WARNING') AND ts >= :c ORDER BY count DESC, ts DESC LIMIT :n",
            {"c": cutoff, "n": limit},
        ).fetchall()
        return [{"ts": r[0], "level": r[1], "source": r[2], "message": r[3], "count": r[4]}
                for r in rows]
    finally:
        db.close()


def attach_logger_hook() -> None:
    """把事件 Handler 挂到 quant logger（全项目错误/告警接入 E1）。防重复挂载。"""
    root = logging.getLogger("quant")
    if not any(isinstance(h, EvolutionEventHandler) for h in root.handlers):
        root.addHandler(EvolutionEventHandler())
    # 启动异步批量刷新线程（E15，守护线程，进程退出自动结束）
    for t in threading.enumerate():
        if t.name == "evolution_events_flusher":
            return
    t = threading.Thread(target=_flush_events, name="evolution_events_flusher", daemon=True)
    t.start()


__all__ = ["record_event", "pending_events", "cursor", "attach_logger_hook", "EVENTS_DB"]
