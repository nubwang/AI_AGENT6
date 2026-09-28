"""知识库写入网关（kb_writer）— 对齐 plans/21 §七 写入路径

职责：全项目唯一的知识库写入入口。

设计（复用 evolution_events 的成熟模式）：
  - 异步队列 + 后台线程批量落库（主流程零阻塞，E15）
  - emit 内**禁止调用 logger**（防日志递归风暴，E1）
  - 写失败静默 + 计数（绝不抛异常到调用方）
  - 幂等：详情见 kb_store.upsert（同一事实重复上报不产生重复记录）

对外 API（各模块只需一行调用即可沉淀知识）：
  record_case / record_experiment / record_change / record_lesson / record_context
  update_experiment / update_change / update_lesson  （异步部分字段更新）
  flush_now / stats / is_enabled
"""
from __future__ import annotations

import queue
import threading
import time

from app.kb import kb_store

# 异步批量队列（E15）
_QUEUE: "queue.Queue[tuple]" = queue.Queue(maxsize=5000)
_FLUSH_INTERVAL = 1.0
_BATCH_SIZE = 50
_MAX_BATCH_WRITE = 500          # 单次 flush 最多写 500 条，防长时间持锁

_STARTED = False
_START_LOCK = threading.Lock()
_reentrant = threading.local()  # 防重入标记（emit 内不写日志）
_FLUSH_REQ = threading.Event()  # flush_now 唤醒后台线程立即落盘

# 观测计数（供 API/守护查看；只增不改）
_COUNTER = {"queued": 0, "written": 0, "dropped": 0, "failed": 0, "updates": 0}

# 写入异常低频告警状态（dropped/failed 增长才告警，最少间隔 5 分钟，防刷屏）
_ALERT_STATE: dict = {"ts": 0.0, "dropped": 0, "failed": 0}
_ALERT_MIN_INTERVAL = 300.0


def _maybe_alert() -> None:
    """写入异常告警：队列丢弃/写入失败增长时写事件流（plans/21 §七：失败静默但不失可观测）。

    说明：静默只针对"调用方"，系统层面必须可见（否则知识库悄悄丢数据没人知道）。
    """
    now = time.time()
    if now - float(_ALERT_STATE.get("ts") or 0.0) < _ALERT_MIN_INTERVAL:
        return
    d = _COUNTER["dropped"] - int(_ALERT_STATE.get("dropped") or 0)
    f = _COUNTER["failed"] - int(_ALERT_STATE.get("failed") or 0)
    if d <= 0 and f <= 0:
        return
    _ALERT_STATE.update({"ts": now, "dropped": _COUNTER["dropped"],
                         "failed": _COUNTER["failed"]})
    try:
        from app.agents import evolution_events
        evolution_events.record_event(
            "WARNING", "kb_writer",
            f"知识库写入异常: 新增丢弃 {d} 条 / 失败 {f} 条"
            f"（累计 丢弃 {_COUNTER['dropped']} / 失败 {_COUNTER['failed']}）")
    except Exception:  # noqa: BLE001
        pass


def is_enabled() -> bool:
    """知识库总开关（evolution_config.knowledge_base.enabled，默认开启）。"""
    try:
        from app.agents import evolution_config
        kb = evolution_config.get("knowledge_base", {}) or {}
        return bool(kb.get("enabled", True))
    except Exception:  # noqa: BLE001
        return True


def _ensure_worker() -> None:
    """启动后台写入线程（幂等）。"""
    global _STARTED
    if _STARTED:
        return
    with _START_LOCK:
        if _STARTED:
            return
        t = threading.Thread(target=_worker, name="kb-writer", daemon=True)
        t.start()
        _STARTED = True


def _worker() -> None:
    """后台线程：批量消费队列 → 落库。

    注意：未满一批时条目暂存在线程本地 batch 中，故 flush_now() 需通过
    _FLUSH_REQ 事件唤醒本线程立即落盘（否则"写后立即读"会读不到刚写入的记录）。
    """
    batch: list[tuple] = []
    last = time.time()
    while True:
        try:
            item = _QUEUE.get(timeout=_FLUSH_INTERVAL)
            batch.append(item)
        except queue.Empty:
            pass
        need = (len(batch) >= _BATCH_SIZE
                or (batch and (time.time() - last) >= _FLUSH_INTERVAL)
                or (_FLUSH_REQ.is_set() and batch))
        if need:
            _flush(batch)
            batch = []
            last = time.time()
        if _FLUSH_REQ.is_set():
            _FLUSH_REQ.clear()


def _flush(batch: list[tuple]) -> None:
    """批量落库：按 (表, 操作) 分组，减少事务次数。失败静默（不抛）。"""
    try:
        inserts: dict[str, list[dict]] = {}
        updates: list[tuple] = []
        for item in batch[:_MAX_BATCH_WRITE]:
            if not item:
                continue
            action = item[0]
            if action == "upsert":
                _, table, record = item
                inserts.setdefault(table, []).append(record)
            elif action == "bulk":
                _, table, records = item
                inserts.setdefault(table, []).extend(records or [])
            elif action == "update":
                _, table, id_, fields = item
                updates.append((table, id_, fields))
        for table, records in inserts.items():
            n = kb_store.upsert_many(table, records)
            _COUNTER["written"] += n
            _COUNTER["failed"] += max(0, len(records) - n)
        for table, id_, fields in updates:
            if kb_store.update_fields(table, id_, fields):
                _COUNTER["updates"] += 1
            else:
                _COUNTER["failed"] += 1
    except Exception:  # noqa: BLE001
        _COUNTER["failed"] += len(batch)
        # 静默：写入失败绝不能影响主流程（但系统层面要可见 → 低频告警）
    finally:
        _maybe_alert()


def _emit(item: tuple) -> bool:
    """入队（非阻塞）。失败静默丢弃（绝不阻塞主流程）。emit 内不写日志（防递归 E1）。"""
    if getattr(_reentrant, "in_emit", False):
        return False
    _reentrant.in_emit = True
    try:
        if not is_enabled():
            return False
        _ensure_worker()
        try:
            _QUEUE.put_nowait(item)
            _COUNTER["queued"] += 1
            return True
        except queue.Full:
            _COUNTER["dropped"] += 1
            _maybe_alert()
            return False
    except Exception:  # noqa: BLE001
        _COUNTER["dropped"] += 1
        _maybe_alert()
        return False
    finally:
        _reentrant.in_emit = False


# ── 对外写入 API ─────────────────────────────────────────────
def record_case(**fields) -> str:
    """写入案例档案（failure/success/neutral）。返回 id（入队即返回，异步落库）。"""
    cid = fields.get("id") or _auto_id("case", fields)
    fields["id"] = cid
    if fields.get("side") not in ("failure", "success", "neutral"):
        fields["side"] = "neutral"
    _emit(("upsert", "case", fields))
    return cid


def record_experiment(**fields) -> str:
    """写入实验台账（每次为提升成功率而做的尝试，含失败/放弃）。"""
    eid = fields.get("id") or _auto_id("experiment", fields)
    fields["id"] = eid
    _emit(("upsert", "experiment", fields))
    return eid


def record_change(**fields) -> str:
    """写入改动因果链（为啥改 → 改了什么 → 结果 → 是否回滚）。"""
    pid = fields.get("id") or _auto_id("change", fields)
    fields["id"] = pid
    _emit(("upsert", "change", fields))
    return pid


def record_lesson(**fields) -> str:
    """写入教训/规律（去重合并见 kb_distiller；此处只做事实落库）。"""
    lid = fields.get("id") or _auto_id("lesson", fields)
    fields["id"] = lid
    _emit(("upsert", "lesson", fields))
    return lid


def record_context(**fields) -> str:
    """写入环境/口径快照。"""
    cid = fields.get("id") or _auto_id("context", fields)
    fields["id"] = cid
    _emit(("upsert", "context", fields))
    return cid


def record_many(table: str, records: list[dict]) -> int:
    """批量写入同类档案（如一期验证的几十条 case）。

    一次入队 + 一次批量落库（动作标记 bulk，由 _flush 分发到 kb_store.upsert_many），
    避免逐条 emit 的队列压力。返回入队条数（异步落库，不代表已写盘）。
    """
    if not records or table not in ("case", "experiment", "change", "lesson", "context"):
        return 0
    norm: list[dict] = []
    for rec in records:
        r = dict(rec or {})
        r.setdefault("id", _auto_id(table, r))
        if table == "case" and r.get("side") not in ("failure", "success", "neutral"):
            r["side"] = "neutral"
        norm.append(r)
    if _emit(("bulk", table, norm)):
        return len(norm)
    return 0


def update_experiment(id_: str, fields: dict) -> bool:
    """异步部分更新实验台账（如 verdict/状态/timeline 追加）。"""
    return _emit(("update", "experiment", id_, fields))


def update_change(id_: str, fields: dict) -> bool:
    """异步部分更新改动因果链（如原因分析/回滚记录/verdict）。"""
    return _emit(("update", "change", id_, fields))


def update_lesson(id_: str, fields: dict) -> bool:
    """异步部分更新教训（置信度升降/退役，由 kb_distiller 使用）。"""
    return _emit(("update", "lesson", id_, fields))


def flush_now(timeout: float = 0.5) -> None:
    """立即落库（供"写后立即读"场景：摘要/前端查询/回填脚本）。

    性能关键（实测教训）：**没有在途数据时必须立即返回**。后台线程空闲时阻塞在
    `_QUEUE.get(timeout=1s)`，若每次都 set 事件等它醒来，会给每次调用叠加 ~1s
    无谓延迟（L0 摘要/检索这类热路径会被拖慢一倍）。
    因此：仅当存在"已入队但未落库"的在途数据时才唤醒并等待。
    """
    batch: list[tuple] = []
    while True:
        try:
            batch.append(_QUEUE.get_nowait())
        except queue.Empty:
            break
    if batch:
        _flush(batch)
        return
    # 队列已空：若计数显示仍有在途（后台线程本地批次未落盘）才等待，否则直接返回
    if not _STARTED or _COUNTER["queued"] <= _COUNTER["written"] + _COUNTER["updates"]:
        return
    _FLUSH_REQ.set()
    t0 = time.time()
    while _FLUSH_REQ.is_set() and (time.time() - t0) < timeout:
        time.sleep(0.02)


# ── 辅助 ─────────────────────────────────────────────────────
def _auto_id(kind: str, fields: dict) -> str:
    """确定性指纹 id（同一天同一标的/同一改动重复上报 → 幂等覆盖）。

    形如：case:20260817:601886.SH / experiment:20260901:daily_lambda_exclude:0.5->0.65
    """
    ts = str(fields.get("date") or fields.get("ts") or time.strftime("%Y%m%d"))
    if kind == "case":
        return f"case:{ts[:8]}:{fields.get('ts_code', 'unknown')}"
    if kind == "experiment":
        tgt = fields.get("param") or fields.get("file") or "unknown"
        chg = fields.get("change") or {}
        o, n = "", ""
        if isinstance(chg, dict):
            o, n = chg.get("old", ""), chg.get("new", "")
        sig = f"{o}->{n}" if (o != "" or n != "") else str(fields.get("kind", "exp"))
        return f"experiment:{ts[:8]}:{tgt}:{sig}"
    if kind == "change":
        w = fields.get("what") or {}
        f = w.get("file", "unknown") if isinstance(w, dict) else "unknown"
        return f"change:{ts[:8]}:{f}:{str(fields.get('diff_hash', 'x'))[:8]}"
    if kind == "lesson":
        return f"lesson:{fields.get('type', 'lesson')}:{str(fields.get('key', 'auto'))[:48]}"
    if kind == "context":
        return f"context:{ts[:8]}"
    return f"{kind}:{ts[:8]}:{int(time.time())}"


def stats() -> dict:
    """写入网关统计（队列/成功/丢弃/失败 + 库总量）。"""
    out: dict = dict(_COUNTER)
    out["queue_size"] = _QUEUE.qsize()
    out["started"] = _STARTED
    out["enabled"] = is_enabled()
    try:
        out["store"] = kb_store.stats()
        out["db_size_mb"] = kb_store.db_size_mb()
    except Exception:  # noqa: BLE001
        out["store"] = {}
    return out


__all__ = [
    "record_case", "record_experiment", "record_change", "record_lesson", "record_context",
    "update_experiment", "update_change", "update_lesson",
    "flush_now", "stats", "is_enabled",
]
