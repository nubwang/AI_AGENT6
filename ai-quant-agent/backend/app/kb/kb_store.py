"""私有域知识库存储层（kb_store）— 对齐 plans/21 §五 知识模型 / §六 存储

唯一真源：backend/data/evolution_kb.sqlite（WAL）
五类档案：
  - case       : 案例档案（failure / success / neutral，含快照/结果/环境/成因/配对键）
  - experiment : 实验台账（每次"为提升成功率而做的尝试"，含改前改后回测与实盘、verdict）
  - change     : 改动因果链（为啥改 → 改了什么 → 何时生效 → 结果 → 是否回滚）
  - lesson     : 教训/规律（可操作、置信度、样本量、环境标签、失效期、升权降权退役）
  - context    : 环境与口径快照（指数/涨停数/口径版本/参数全量快照）

设计原则：
  - 纯标准库 sqlite3，失败静默返回默认值（绝不阻塞主流程）
  - 幂等：id 为确定性指纹，写入走 upsert（重复写不产生重复事实）
  - 结构化精确查询为主力（零幻觉、零 token 成本）
  - 结构化字段（snapshot/outcome/env/cause 等）以 JSON 文本存储，读取时尽力还原
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from app.core.logger import logger

# 数据目录：backend/data/
DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
KB_DB = os.path.join(DATA_DIR, "evolution_kb.sqlite")

SCHEMA_VER = 1

_WRITE_LOCK = threading.Lock()   # 单库单写（WAL 下多读一写）
_SCHEMA_READY = False            # 建表只做一次（进程内）

# ── 档案表定义（表名 → 列清单）────────────────────────────────
_TABLE = {
    "case": "kb_case",
    "experiment": "kb_experiment",
    "change": "kb_change",
    "lesson": "kb_lesson",
    "context": "kb_context",
    "plan": "kb_plan",        # plans/22：交易计划台账（纪律效果评估的载体）
}

# 各表的 JSON 字段（写入时 dumps、读取时 loads）
_JSON_COLS = {
    "case": ("snapshot", "outcome", "env", "cause"),
    "experiment": ("target", "change", "backtest", "live", "timeline", "related_lessons"),
    "change": ("what", "observed", "rollback"),
    "lesson": ("scope", "actionable", "support", "applied_experiments"),
    "context": ("payload",),
    "plan": ("plan", "execution", "baseline", "alpha", "env"),
}

# 各表列清单（顺序固定，供 upsert 使用）
_COLS = {
    "case": ("id", "date", "ts_code", "name", "side", "form_type", "stage",
             "snapshot", "outcome", "env", "cause", "pair_id", "source",
             "confidence", "schema_ver", "created_at", "updated_at"),
    "experiment": ("id", "ts", "kind", "target", "change", "hypothesis", "evidence_in",
                   "backtest", "live", "verdict", "verdict_reason", "related_lessons",
                   "status", "timeline", "created_at", "updated_at"),
    "change": ("id", "ts", "why", "what", "who", "effective_at", "expectation",
               "observed", "verdict", "root_cause", "rollback", "created_at", "updated_at"),
    "lesson": ("id", "ts", "text", "type", "scope", "actionable", "support",
               "confidence", "half_life_days", "applied_experiments", "effect_verdict",
               "status", "last_verified_at", "created_at", "updated_at"),
    "context": ("id", "date", "payload", "created_at", "updated_at"),
    "plan": ("id", "date", "ts_code", "name", "form_type", "params_version",
             "plan", "t0_close", "execution", "baseline", "alpha", "failure_type",
             "env", "created_at", "updated_at"),
}

# 时间列的默认值填充（缺省时自动补）
_TS_COL = {"case": "date", "experiment": "ts", "change": "ts",
           "lesson": "ts", "context": "date", "plan": "date"}


# ── 连接与建表 ───────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(KB_DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    except Exception:  # noqa: BLE001
        pass
    return c


_DDL = """
CREATE TABLE IF NOT EXISTS kb_case (
  id TEXT PRIMARY KEY, date TEXT, ts_code TEXT, name TEXT, side TEXT,
  form_type TEXT, stage TEXT, snapshot TEXT, outcome TEXT, env TEXT, cause TEXT,
  pair_id TEXT, source TEXT, confidence REAL, schema_ver INTEGER,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS kb_experiment (
  id TEXT PRIMARY KEY, ts TEXT, kind TEXT, target TEXT, change TEXT, hypothesis TEXT,
  evidence_in TEXT, backtest TEXT, live TEXT, verdict TEXT, verdict_reason TEXT,
  related_lessons TEXT, status TEXT, timeline TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS kb_change (
  id TEXT PRIMARY KEY, ts TEXT, why TEXT, what TEXT, who TEXT, effective_at TEXT,
  expectation TEXT, observed TEXT, verdict TEXT, root_cause TEXT, rollback TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS kb_lesson (
  id TEXT PRIMARY KEY, ts TEXT, text TEXT, type TEXT, scope TEXT, actionable TEXT,
  support TEXT, confidence REAL, half_life_days REAL, applied_experiments TEXT,
  effect_verdict TEXT, status TEXT, last_verified_at TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS kb_context (
  id TEXT PRIMARY KEY, date TEXT, payload TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS kb_plan (
  id TEXT PRIMARY KEY, date TEXT, ts_code TEXT, name TEXT, form_type TEXT,
  params_version TEXT, plan TEXT, t0_close REAL, execution TEXT, baseline TEXT,
  alpha TEXT, failure_type TEXT, env TEXT, created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_plan_date ON kb_plan(date);
CREATE INDEX IF NOT EXISTS ix_plan_code ON kb_plan(ts_code);
CREATE INDEX IF NOT EXISTS ix_plan_fail ON kb_plan(failure_type);
CREATE TABLE IF NOT EXISTS kb_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_case_date ON kb_case(date);
CREATE INDEX IF NOT EXISTS ix_case_code ON kb_case(ts_code);
CREATE INDEX IF NOT EXISTS ix_case_side ON kb_case(side, stage, form_type);
CREATE INDEX IF NOT EXISTS ix_case_pair ON kb_case(pair_id);
CREATE INDEX IF NOT EXISTS ix_exp_status ON kb_experiment(status, verdict);
CREATE INDEX IF NOT EXISTS ix_chg_verdict ON kb_change(verdict);
CREATE INDEX IF NOT EXISTS ix_lesson_status ON kb_lesson(status, type);
CREATE INDEX IF NOT EXISTS ix_ctx_date ON kb_context(date);
"""

# 教训全文索引（FTS5 可选；中文用 jieba 分词后写入 seg 列）
_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS kb_lesson_fts
USING fts5(id UNINDEXED, text, seg, tokenize='unicode61')
"""


def init_schema(force: bool = False) -> bool:
    """建表 + 索引（幂等）。失败静默返回 False（不影响主流程）。"""
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return True
    try:
        with _WRITE_LOCK:
            db = _conn()
            try:
                db.executescript(_DDL)
                try:
                    db.executescript(_FTS_DDL)
                except Exception:  # noqa: BLE001
                    pass  # FTS5 不可用 → 检索层回退 LIKE
                db.execute("INSERT OR REPLACE INTO kb_meta (k, v) VALUES (?, ?)",
                           ("schema_ver", str(SCHEMA_VER)))
                db.commit()
            finally:
                db.close()
        _SCHEMA_READY = True
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[kb_store] 建表失败: {exc}")
        return False


# ── 序列化辅助 ───────────────────────────────────────────────
def _dumps(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(v)


def _loads(v):
    if v is None or not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return v
    if s[0] in "[{":
        try:
            return json.loads(s)
        except Exception:  # noqa: BLE001
            return v
    return v


def _row_to_dict(table: str, row: sqlite3.Row) -> dict:
    d = {k: row[k] for k in row.keys()}
    for col in _JSON_COLS.get(table, ()):
        if col in d:
            d[col] = _loads(d[col])
    return d


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _prepare(table: str, rec: dict) -> dict:
    """补齐时间戳 + JSON 序列化（只取该表登记列，防脏字段写库）。"""
    cols = _COLS[table]
    out: dict = {}
    json_cols = _JSON_COLS.get(table, ())
    tcol = _TS_COL.get(table)
    for c in cols:
        if c in ("created_at", "updated_at"):
            continue
        if c in json_cols:
            out[c] = _dumps(rec.get(c))
        else:
            out[c] = rec.get(c)
    if tcol and out.get(tcol) in (None, ""):
        out[tcol] = _now()
    if not out.get("id"):
        return {}
    # schema_ver 仅 case 表登记
    if "schema_ver" in cols and out.get("schema_ver") in (None, ""):
        out["schema_ver"] = SCHEMA_VER
    out["created_at"] = rec.get("created_at") or _now()
    out["updated_at"] = _now()
    return out


# ── 写入（幂等 upsert）──────────────────────────────────────
def upsert_many(table: str, records: list[dict]) -> int:
    """批量幂等写入。返回成功条数（失败整体静默，不抛）。"""
    key = _TABLE.get(table)
    if not key or not records:
        return 0
    if not init_schema():
        return 0
    cols = _COLS[table]
    sql = (
        f"INSERT INTO {key} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(id) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    )
    rows: list[tuple] = []
    prepared: list[dict] = []
    for rec in records:
        p = _prepare(table, rec or {})
        if not p:
            continue
        prepared.append(p)
        rows.append(tuple(p.get(c) for c in cols))
    if not rows:
        return 0
    try:
        with _WRITE_LOCK:
            db = _conn()
            try:
                db.executemany(sql, rows)
                db.commit()
            finally:
                db.close()
        if table == "lesson":
            _sync_lesson_fts(prepared)
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[kb_store] 写入 {table} 失败: {exc}")
        return 0


def upsert(table: str, record: dict) -> bool:
    """单条幂等写入。"""
    return upsert_many(table, [record]) > 0


def update_fields(table: str, id_: str, fields: dict) -> bool:
    """部分字段更新（值字典；JSON 字段自动序列化）。"""
    key = _TABLE.get(table)
    if not key or not id_ or not fields:
        return False
    if not init_schema():
        return False
    json_cols = _JSON_COLS.get(table, ())
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _COLS[table] or k == "id":
            continue
        sets.append(f"{k}=?")
        vals.append(_dumps(v) if k in json_cols else v)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(id_)
    try:
        with _WRITE_LOCK:
            db = _conn()
            try:
                cur = db.execute(f"UPDATE {key} SET {','.join(sets)} WHERE id=?", vals)
                db.commit()
                ok = cur.rowcount > 0
            finally:
                db.close()
        if table == "lesson" and ok:
            row = get(table, id_)
            if row:
                _sync_lesson_fts([{
                    "id": row.get("id"),
                    "text": " ".join([str(row.get("text") or ""),
                                      str(row.get("actionable") or "")]),
                }])
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[kb_store] 更新 {table}#{id_} 失败: {exc}")
        return False


# ── FTS 同步（教训全文检索）──────────────────────────────────
def _seg(text: str) -> str:
    """中文分词（jieba 可用则分词，否则原样）。"""
    try:
        import jieba  # type: ignore
        return " ".join(jieba.cut_for_search(text or ""))
    except Exception:  # noqa: BLE001
        return text or ""


def _sync_lesson_fts(lessons: list[dict]) -> None:
    try:
        with _WRITE_LOCK:
            db = _conn()
            try:
                for rec in lessons:
                    lid = rec.get("id")
                    if not lid:
                        continue
                    text = (rec.get("text") or "") + " " + (rec.get("actionable") or "")
                    db.execute("DELETE FROM kb_lesson_fts WHERE id=?", (lid,))
                    db.execute("INSERT INTO kb_lesson_fts (id, text, seg) VALUES (?,?,?)",
                               (lid, text, _seg(text)))
                db.commit()
            finally:
                db.close()
    except Exception:  # noqa: BLE001
        pass  # FTS 不可用 → 检索层回退 LIKE


# ── 元数据（索引游标等）─────────────────────────────────────
def meta_get(key: str, default: str = "") -> str:
    """读 kb_meta 键值（如向量索引游标）。失败返回默认值。"""
    try:
        db = _conn()
        try:
            row = db.execute("SELECT v FROM kb_meta WHERE k=?", (key,)).fetchone()
        finally:
            db.close()
        return str(row["v"]) if row else default
    except Exception:  # noqa: BLE001
        return default


def meta_set(key: str, value: str) -> bool:
    """写 kb_meta 键值（如向量索引游标）。失败静默返回 False。"""
    try:
        with _WRITE_LOCK:
            db = _conn()
            try:
                db.execute("INSERT OR REPLACE INTO kb_meta (k, v) VALUES (?, ?)",
                           (key, str(value)))
                db.commit()
            finally:
                db.close()
        return True
    except Exception:  # noqa: BLE001
        return False


# ── 读取 ─────────────────────────────────────────────────────
def get(table: str, id_: str) -> dict | None:
    key = _TABLE.get(table)
    if not key or not id_:
        return None
    try:
        db = _conn()
        try:
            row = db.execute(f"SELECT * FROM {key} WHERE id=?", (id_,)).fetchone()
        finally:
            db.close()
        return _row_to_dict(table, row) if row else None
    except Exception:  # noqa: BLE001
        return None


def query(table: str, filters: dict | None = None, *, date_gte: str | None = None,
          date_lte: str | None = None, order: str | None = None,
          limit: int = 100, offset: int = 0) -> list[dict]:
    """结构化精确查询（主力检索）。

    Args:
        filters: 等值过滤 {列: 值}（列名须在该表登记清单内）
        date_gte/date_lte: 时间范围（按该表时间列过滤，YYYYMMDD 或 YYYY-MM-DD 前缀均可）
        order: 排序表达式（默认按时间列倒序）
        limit/offset: 分页
    """
    key = _TABLE.get(table)
    if not key:
        return []
    cols = _COLS[table]
    tcol = _TS_COL.get(table, "created_at")
    where, vals = [], []
    for c, v in (filters or {}).items():
        if c not in cols or v is None:
            continue
        where.append(f"{c}=?")
        vals.append(v)
    if date_gte:
        where.append(f"{tcol}>=?")
        vals.append(str(date_gte))
    if date_lte:
        where.append(f"{tcol}<=?")
        vals.append(str(date_lte))
    sql = f"SELECT * FROM {key}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + (order or f"{tcol} DESC")
    sql += " LIMIT ? OFFSET ?"
    vals.extend([int(limit), int(offset)])
    try:
        db = _conn()
        try:
            rows = db.execute(sql, vals).fetchall()
        finally:
            db.close()
        return [_row_to_dict(table, r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def count(table: str, filters: dict | None = None) -> int:
    key = _TABLE.get(table)
    if not key:
        return 0
    cols = _COLS[table]
    where, vals = [], []
    for c, v in (filters or {}).items():
        if c not in cols or v is None:
            continue
        where.append(f"{c}=?")
        vals.append(v)
    sql = f"SELECT COUNT(1) FROM {key}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    try:
        db = _conn()
        try:
            return int(db.execute(sql, vals).fetchone()[0])
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return 0


def group_count(table: str, col: str, filters: dict | None = None,
                limit: int = 20) -> list[dict]:
    """分组计数（供统计/前端看板）。"""
    key = _TABLE.get(table)
    if not key or col not in _COLS[table]:
        return []
    cols = _COLS[table]
    where, vals = [], []
    for c, v in (filters or {}).items():
        if c not in cols or v is None:
            continue
        where.append(f"{c}=?")
        vals.append(v)
    sql = f"SELECT {col} AS k, COUNT(1) AS n FROM {key}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" GROUP BY {col} ORDER BY n DESC LIMIT ?"
    vals.append(int(limit))
    try:
        db = _conn()
        try:
            rows = db.execute(sql, vals).fetchall()
        finally:
            db.close()
        return [{"k": r["k"], "n": r["n"]} for r in rows]
    except Exception:  # noqa: BLE001
        return []


def delete_lessons_by_type_prefix(prefix: str) -> int:
    """按 type 前缀删除教训（维护用，如证据层重建）。返回删除条数，失败静默 0。

    注意：FTS 表中可能残留已删 id 的倒排项，但检索一律 JOIN kb_lesson，故无副作用。
    """
    if not prefix:
        return 0
    try:
        with _WRITE_LOCK:
            db = _conn()
            try:
                cur = db.execute("DELETE FROM kb_lesson WHERE type LIKE ?",
                                 (f"{prefix}%",))
                db.commit()
                return cur.rowcount or 0
            finally:
                db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[kb_store] 删除教训(type~{prefix}) 失败: {exc}")
        return 0


def search_lessons_like(keyword: str, limit: int = 20) -> list[dict]:
    """教训 LIKE 检索（FTS 不可用时的兜底）。"""
    if not keyword:
        return []
    try:
        db = _conn()
        try:
            rows = db.execute(
                "SELECT * FROM kb_lesson WHERE text LIKE ? OR actionable LIKE ? "
                "ORDER BY confidence DESC LIMIT ?",
                (f"%{keyword}%", f"%{keyword}%", int(limit)),
            ).fetchall()
        finally:
            db.close()
        return [_row_to_dict("lesson", r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def query_lessons_by_type(type_prefixes: list[str], limit: int = 20,
                          exclude_status: tuple = (),
                          order: str = "confidence DESC") -> list[dict]:
    """按 type 前缀（或全等值）直取教训 —— SQL 过滤，不解析全表 JSON。

    用途：把"证据型知识 / 纪律知识"（evidence:*、discipline）单独取出来喂 L0 摘要与
    综合分析上下文（plans/23·24·25 的验证结论必须被进化大脑"看得见"）。
    """
    if not type_prefixes:
        return []
    where, vals = [], []
    for p in type_prefixes:
        where.append("(type = ? OR type LIKE ?)")
        vals.extend([str(p), f"{p}%"])
    sql = f"SELECT * FROM kb_lesson WHERE ({' OR '.join(where)})"
    if exclude_status:
        sql += " AND (status IS NULL OR status NOT IN (" + ",".join("?" * len(exclude_status)) + "))"
        vals.extend([str(s) for s in exclude_status])
    # order 仅允许白名单（防注入）
    _ord = order if order in ("confidence DESC", "updated_at DESC", "ts DESC") \
        else "confidence DESC"
    sql += f" ORDER BY {_ord} LIMIT ?"
    vals.append(int(limit))
    try:
        db = _conn()
        try:
            rows = db.execute(sql, vals).fetchall()
        finally:
            db.close()
        return [_row_to_dict("lesson", r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def search_lessons_fts(keyword: str, limit: int = 20) -> list[dict]:
    """教训 FTS5 检索（中文经 jieba 分词后匹配），失败返回 []。"""
    if not keyword:
        return []
    try:
        import jieba  # type: ignore
        toks = [t.strip() for t in jieba.cut_for_search(keyword) if t.strip()]
    except Exception:  # noqa: BLE001
        toks = [keyword]
    if not toks:
        return []
    match_expr = " OR ".join(toks)
    try:
        db = _conn()
        try:
            rows = db.execute(
                "SELECT l.* FROM kb_lesson_fts f JOIN kb_lesson l ON l.id=f.id "
                "WHERE kb_lesson_fts MATCH ? LIMIT ?",
                (match_expr, int(limit)),
            ).fetchall()
        finally:
            db.close()
        return [_row_to_dict("lesson", r) for r in rows]
    except Exception:  # noqa: BLE001
        return search_lessons_like(keyword, limit)


# ── 维护 / 统计 ─────────────────────────────────────────────
def prune_context(max_rows: int = 400) -> int:
    """context 快照滚动保留最近 N 条（防无限增长）。返回删除条数。"""
    if max_rows <= 0:
        return 0
    try:
        with _WRITE_LOCK:
            db = _conn()
            try:
                cur = db.execute(
                    "DELETE FROM kb_context WHERE id NOT IN "
                    "(SELECT id FROM kb_context ORDER BY date DESC LIMIT ?)",
                    (int(max_rows),),
                )
                db.commit()
                return cur.rowcount or 0
            finally:
                db.close()
    except Exception:  # noqa: BLE001
        return 0


def stats() -> dict:
    """知识库总览（供 L0 摘要 / 前端看板）。"""
    init_schema()
    out = {"schema_ver": SCHEMA_VER, "db": KB_DB}
    for t in _TABLE:
        out[f"{t}_n"] = count(t)
    out["case_side"] = {r["k"]: r["n"] for r in group_count("case", "side")}
    out["lesson_status"] = {r["k"]: r["n"] for r in group_count("lesson", "status")}
    out["experiment_status"] = {r["k"]: r["n"] for r in group_count("experiment", "status")}
    out["experiment_verdict"] = {r["k"]: r["n"] for r in group_count("experiment", "verdict")}
    return out


def db_size_mb() -> float:
    """库文件大小（MB；含 WAL）。"""
    total = 0
    for p in (KB_DB, KB_DB + "-wal", KB_DB + "-shm"):
        try:
            total += os.path.getsize(p)
        except Exception:  # noqa: BLE001
            continue
    return round(total / 1024 / 1024, 3)


__all__ = [
    "KB_DB", "DATA_DIR", "SCHEMA_VER",
    "init_schema", "upsert", "upsert_many", "update_fields",
    "get", "query", "count", "group_count",
    "search_lessons_fts", "search_lessons_like", "query_lessons_by_type",
    "delete_lessons_by_type_prefix",
    "prune_context", "stats", "db_size_mb",
]
