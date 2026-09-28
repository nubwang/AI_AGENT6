"""进化摘要器（evolution_summarizer）— 对齐 plans/11 进化Agent §6/G8

MVP：L0 摘要（事件流增量 + monitor 命中统计 + 当前参数值），供进化大脑决策。
"""
from __future__ import annotations

import json
import os
import time

from app.agents import evolution_events
from app.agents import evolution_config

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
MONITOR_STATE = os.path.join(DATA_DIR, "monitor_state.json")
SNAPSHOT_FILE = os.path.join(DATA_DIR, "evolution_snapshots.json")  # E5 状态快照
MAX_SNAPSHOTS = 200   # 快照容量上限（滚动）
_SNAPSHOT_MIN_INTERVAL = 3600.0   # Bug10：快照最小间隔（秒）——防 build_l0_summary 被频繁调用时刷屏
_snapshot_last_ts = 0.0
_KB_SNAPSHOT_MIN_INTERVAL = 6 * 3600.0   # plans/21：知识库环境快照最小间隔（秒）
_kb_snapshot_last_ts = 0.0
_KB_BLOCK_TTL = 60.0                     # plans/21：L0 知识块进程内缓存 TTL（秒）
_kb_block_cache: dict = {}


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _monitor_stats(days: int | None = None) -> dict:
    """近 N 日推荐命中统计（monitor_state.json 的 hits）。

    窗口用 evolution_config 的 eval_window（I4，默认 30 日），按 date(YYYYMMDD) 过滤。
    用户核心诉求"推荐必须涨、最好主升浪"：同时统计 次日T+1 大涨命中率 与 T+5 主升浪命中率，
    并给综合目标分 composite = t1 × (1-w) + wave × w（w=主升浪权重，可进化 daily_target_wave_w）。
    """
    if days is None:
        try:
            days = int(evolution_config.get("eval_window", 30) or 30)
        except (TypeError, ValueError):
            days = 30
    state = _read_json(MONITOR_STATE, {"hits": []})
    hits = state.get("hits", [])
    import datetime as _dt
    cutoff = (_dt.date.today() - _dt.timedelta(days=days)).strftime("%Y%m%d")
    recent = [h for h in hits if str(h.get("date", ""))[:8] >= cutoff]
    done = [h for h in recent if h.get("t5_ret") is not None]
    n = len(done)
    hit_n = sum(1 for h in done if h.get("hit"))
    avg_ret = sum(float(h.get("t5_ret", 0) or 0) for h in done) / n if n else 0.0
    # 次日大涨（T+1，D+1 买入口径）
    done1 = [h for h in recent if h.get("t1_ret") is not None]
    try:
        _t1_thr = float(evolution_config.get_param("t1_hit_threshold", 0.0) or 0.0)
        _wave_thr = float(evolution_config.get_param("wave_hit_threshold", 0.05) or 0.05)
        _w = float(evolution_config.get_param("daily_target_wave_w", 0.4) or 0.4)
    except Exception:  # noqa: BLE001
        _t1_thr, _wave_thr, _w = 0.0, 0.05, 0.4
    _w = max(0.0, min(1.0, _w))
    t1_hit = (sum(1 for h in done1 if float(h.get("t1_ret", 0) or 0) > _t1_thr) / len(done1)
              if done1 else 0.0)
    # 主升浪（T+5，D+1 买入口径）
    wave_hit = (sum(1 for h in done if float(h.get("t5_ret", 0) or 0) > _wave_thr) / n if n else 0.0)
    composite = round(t1_hit * (1 - _w) + wave_hit * _w, 4)
    return {"samples": n, "hit_n": hit_n, "hit_rate": round(hit_n / n, 4) if n else 0.0,
            "avg_t5_ret": round(avg_ret, 4), "window_days": days,
            "t1_samples": len(done1), "t1_hit_rate": round(t1_hit, 4),
            "wave_hit_rate": round(wave_hit, 4), "composite": composite, "wave_w": _w}


def _events_top(limit: int = 8) -> list[dict]:
    """增量事件 Top（按 level 权重 + 最近）。"""
    try:
        evolution_events.flush_now()  # Bug19：先落库再读，保证事件可见
    except Exception:  # noqa: BLE001
        pass
    events = evolution_events.pending_events(evolution_events.cursor() - 999999, limit=500)
    # 简化：取最近 limit 条非 INFO 事件
    return events[-limit:]


def _verify_stats() -> dict:
    """读 verify_kb.json 最新一期次日验证统计（plans/17：进化大脑聚焦每日推荐）。

    轻量读文件（不触发 daily_verify 重模块加载），供 L0 摘要使用。
    """
    d = _read_json(os.path.join(DATA_DIR, "verify_kb.json"), {"verifications": []})
    verifs = d.get("verifications", [])
    if not verifs:
        return {}
    latest = verifs[-1]
    return {
        "date": latest.get("date", ""),
        "t1_hit_rate": latest.get("t1_hit_rate", 0.0),
        "wave_hit_rate": latest.get("wave_hit_rate"),
        "composite": latest.get("composite"),
        "wave_w": latest.get("wave_w", 0.4),
        "up_n": latest.get("up_n", 0),
        "flat_n": latest.get("flat_n", 0),
        "down_n": latest.get("down_n", 0),
        "avg_t1_ret": latest.get("avg_t1_ret", 0.0),
        "improvements": latest.get("improvements", []),
        "attributions": latest.get("attributions", [])[:3],
        "stage_report": latest.get("stage_report", [])[:3],
    }


def _backtest_diag() -> dict:
    """回测健康诊断（plans/19：进化大脑考虑"是不是回测的问题"——回测逻辑失效/产物脱节）。"""
    try:
        from app.agents import backtest_ctl
        return backtest_ctl.backtest_health()
    except Exception:  # noqa: BLE001
        return {"ok": False, "verdict": "insufficient"}


def _knowledge_block() -> dict:
    """私有域知识库块（plans/21 §8.1）：L0 必读带最相关教训/活跃实验/否证清单。

    性能约束：L0 是热路径（守护巡检 12:05 / 前端刷新 / 主指标检查都会调用），
    必须**零模型依赖**（kb_index.l0_block 内部 use_vector=False），并做 60s 进程内缓存
    （实测：不缓存且走向量时单次 3.8s、首轮 30s+，会拖慢巡检并争抢显存）。
    """
    global _kb_block_cache
    now = time.time()
    if _kb_block_cache.get("ts") and now - _kb_block_cache["ts"] < _KB_BLOCK_TTL:
        return _kb_block_cache.get("data") or {}
    try:
        from app.kb import kb_index
        data = kb_index.l0_block()
    except Exception:  # noqa: BLE001
        data = {}
    _kb_block_cache = {"ts": now, "data": data}
    return data


_DISC_TTL = 60.0
_disc_cache: dict = {"ts": 0.0, "data": ""}


def _discipline_txt() -> str:
    """交易纪律块（plans/22）：计划触价回放统计 + 纪律网格反事实最优（60s 缓存，零模型依赖）。

    与知识库块同样处理：L0 是热路径，纪律统计只读 SQLite/JSON，成本极低但仍做缓存，
    避免守护巡检/前端刷新时重复汇总。
    """
    global _disc_cache
    now = time.time()
    if _disc_cache.get("ts") and now - _disc_cache["ts"] < _DISC_TTL:
        return str(_disc_cache.get("data") or "")
    parts: list[str] = []
    try:
        from app.backtest import plan_tracker
        t = plan_tracker.build_plan_txt(days=60)
        if t:
            parts.append(t)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.backtest import discipline_grid
        g = discipline_grid.grid_txt()
        if g:
            parts.append(g)
    except Exception:  # noqa: BLE001
        pass
    data = "\n".join(parts)
    _disc_cache = {"ts": now, "data": data}
    return data


def _kb_context_snapshot(summary: dict) -> None:
    """每轮评估写知识库环境快照（限频 6h，防 L0 被高频调用时刷屏）。"""
    global _kb_snapshot_last_ts
    now = time.time()
    if now - _kb_snapshot_last_ts < _KB_SNAPSHOT_MIN_INTERVAL:
        return
    _kb_snapshot_last_ts = now
    try:
        from app.kb import kb_ingest
        kb_ingest.ingest_context(time.strftime("%Y%m%d"), extra={
            "eval": True,
            "monitor": {k: summary.get("monitor", {}).get(k)
                        for k in ("hit_rate", "t1_hit_rate", "wave_hit_rate", "composite", "samples")},
            "backtest_verdict": (summary.get("backtest") or {}).get("verdict"),
            "knowledge": {k: summary.get("knowledge", {}).get(k)
                          for k in ("cases", "lessons", "experiments", "changes")},
        })
    except Exception:  # noqa: BLE001
        pass


# ── 进化健康神经元（plans/23 §十二：让进化大脑统一管理"元健康"）──────────────
# 进化大脑若只看"推荐命中率"，会在指标失真 / 标签无区分度 / 数据链路断裂的情况下
# 做出错误进化（越进化越偏）。本神经元把这些"元健康"信号聚合进 L0，使进化大脑能先判断
# "现在的结论可不可信、链路通不通"，再决定该调参、修链路还是改代码。
_HEALTH_CACHE: dict = {"ts": 0.0, "data": {}}
_HEALTH_TTL = 60.0     # 秒（L0 可能被频繁调用；数据链路探针带 SQL，必须限频）
_PLAN23_PARAM_PREFIXES = ("bt_", "data_gate_", "collect_retry_")


# ★ L0 是热路径（守护巡检 / 前端刷新 / 主指标检查都会调用），探针必须"轻到底"。
# 血泪教训（2026-09-23）：`stk_limit` 缺 trade_date 首列索引时，`MAX(trade_date)`
# 只能对 (ts_code, trade_date) 索引做全扫描（1690 万行）→ 单条 SQL 43s；
# `/evolve/status` 因冷启动 38~73s，把进化中心整页 17 个请求一起拖到前端
# axios 30s 超时（"timeout of 30000ms exceeded"）。
# 因此强制：① 每条 SQL 独立取数，单条失败只降级该字段（不整块失败）；
#          ② 每条加 `MAX_EXECUTION_TIME` 硬上限，超时自动中止并降级
#             → 任何单条查询最长 3s，接口永远不会再被一条 SQL 拖到几十秒。
_PROBE_TIMEOUT_MS = 3000


def _probe_sql(sql: str, timeout_ms: int = _PROBE_TIMEOUT_MS) -> str:
    """给探针 SQL 加执行时间上限（MySQL 8+ 生效；不支持的引擎当普通注释忽略）。"""
    return sql.replace("SELECT ", f"SELECT /*+ MAX_EXECUTION_TIME({timeout_ms}) */ ", 1)


def _data_chain_probe() -> dict:
    """一次轻量 SQL 探针：核心表日期 + 股票池新鲜度 + 事件表冗余规模。

    逐条独立取数 + 单条执行时间上限：任何一条慢/失败都只降级该字段，
    绝不阻塞 L0 摘要（见上方血泪教训）。
    """
    out: dict = {"probe_errors": []}
    try:
        from sqlalchemy import text
        from app.models import SessionLocal
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:120]
        return out
    db = SessionLocal()

    def _one(key: str, sql: str, as_int: bool = False):
        """单值探针：超时/异常 → 降级为空值，并把原因记进 probe_errors。"""
        try:
            v = db.execute(text(_probe_sql(sql))).scalar()
            out[key] = int(v or 0) if as_int else ("" if v is None else str(v))
        except Exception as exc:  # noqa: BLE001
            out[key] = 0 if as_int else ""
            out["probe_errors"].append(f"{key}: {type(exc).__name__}: {str(exc)[:60]}")

    try:
        _one("daily_max", "SELECT MAX(trade_date) FROM daily")
        _one("adj_max", "SELECT MAX(trade_date) FROM adj_factor")
        _one("stk_limit_max", "SELECT MAX(trade_date) FROM stk_limit")
        _one("stock_basic_max_list", "SELECT MAX(list_date) FROM stock_basic")
        _one("stock_basic_rows", "SELECT COUNT(*) FROM stock_basic", as_int=True)
        # 用 information_schema 近似行数（express 全表 COUNT 太慢）
        _one("express_rows",
             "SELECT table_rows FROM information_schema.tables "
             "WHERE table_schema=DATABASE() AND table_name='express'", as_int=True)
    finally:
        db.close()
    return out


def _evolution_health() -> dict:
    """进化健康神经元（零模型依赖，60s 缓存，任何异常都降级不阻塞 L0）。"""
    now = time.time()
    if now - _HEALTH_CACHE["ts"] < _HEALTH_TTL and _HEALTH_CACHE["data"]:
        return _HEALTH_CACHE["data"]

    out: dict = {"ok": True, "issues": []}
    try:
        # ① 最新回测产物的"可信度"信号（plans/23 T1/T2/T3/T6/T7/T8）
        bt: dict = {}
        try:
            from app.agents import backtest_ctl
            bt = backtest_ctl.latest_result() or {}
        except Exception:  # noqa: BLE001
            pass
        perf = bt.get("performance") or {}
        disc = perf.get("discrimination") or {}
        ab = bt.get("ab_validation") or {}
        vs = bt.get("vector_store_stats") or {}
        at = (bt.get("attribution") or {}).get("filter_stats") or {}
        dg = (bt.get("data_validation") or {}).get("gate") or {}
        vs_total = sum(v for k, v in vs.items()
                       if isinstance(v, int) and k.startswith(("success_", "failure_")))
        out["backtest"] = {
            "scope": perf.get("scope"),
            "quality_gate": perf.get("quality_gate", "unknown"),
            "usable_for_decision": perf.get("usable_for_decision"),
            "discrimination_passed": bool(disc.get("passed", False)),
            "discrimination_gap": disc.get("gap"),
            "ab_decision": ab.get("decision"),
            "ab_c_coverage": ab.get("c_coverage"),
            "ab_prob_gap": ab.get("prob_gap"),
            "vector_store_total": vs_total,
            "vector_store_written": vs.get("written_this_run"),
            "vector_store_reset": vs.get("reset"),
            "attr_kept": at.get("kept"),
            "attr_rejected": at.get("rejected"),
            "attr_key_features": at.get("key_features"),
            "data_gate": dg.get("gate"),
        }

        # ② 数据链路健康（plans/23 V1/V2/V3/Q1/T7）
        chain = _data_chain_probe()
        out["data_chain"] = chain
        # 最新推荐日期：直接扫落盘榜单目录（自包含，不跨模块耦合）
        try:
            import re as _re
            from pathlib import Path as _P
            _dir = _P(__file__).resolve().parents[2] / "data" / "daily_recommend"
            _dates = [m.group(1) for f in _dir.glob("daily_*.json")
                      if (m := _re.search(r"daily_(\d{8})", f.name))]
            out["latest_report_date"] = max(_dates) if _dates else ""
        except Exception:  # noqa: BLE001
            out["latest_report_date"] = ""

        # ③ 汇总问题清单（进化大脑的行动入口）
        issues: list[dict] = []
        if perf.get("quality_gate") == "error":
            issues.append({"level": "error", "code": "BT_QUALITY_GATE_FAILED",
                           "msg": "最近一次回测未通过质量门（结论不可用，latest 未被覆盖）"})
        if bt and not disc.get("passed", False):
            issues.append({"level": "warn", "code": "NO_DISCRIMINATION",
                           "msg": f"正负样本无区分度（gap={disc.get('gap')}）→ 标签体系待重建（T4 对照组）"})
        if ab.get("decision") and ab.get("decision") != "Go" and ab.get("c_coverage") is not None:
            if float(ab.get("ab_c_coverage") or 0) >= 0.9:
                issues.append({"level": "warn", "code": "AB_COVERAGE_INVALID",
                               "msg": "A/B 的 C 组覆盖率过高（条件表筛选力不足）"})
        if vs_total and vs.get("written_this_run") and vs_total > vs["written_this_run"] * 1.5:
            issues.append({"level": "warn", "code": "VECTOR_STORE_ACCUMULATED",
                           "msg": f"模式库累计 {vs_total} 条 > 本次样本 {vs.get('written_this_run')} 条 × 1.5"})
        if dg.get("gate") == "blocked":
            issues.append({"level": "error", "code": "DATA_GATE_BLOCKED",
                           "msg": "数据门禁未通过：" + "；".join((dg.get("reasons") or [])[:2])})
        if chain.get("daily_max") and chain.get("adj_max") and chain["daily_max"] != chain["adj_max"]:
            issues.append({"level": "warn", "code": "TABLE_DATE_MISMATCH",
                           "msg": f"核心表日期不一致 daily={chain['daily_max']} adj={chain['adj_max']}"})
        if chain.get("express_rows", 0) > 1_000_000:
            issues.append({"level": "info", "code": "EXPRESS_REDUNDANT",
                           "msg": f"express 约 {chain['express_rows']:,} 行（存在重复，待执行 dedupe_express）"})
        lb = out.get("latest_report_date") or ""
        if chain.get("daily_max") and lb and lb < chain["daily_max"]:
            issues.append({"level": "warn", "code": "RECOMMEND_STALE",
                           "msg": f"最新推荐 {lb} 落后于日线 {chain['daily_max']}（自动链路待观察）"})
        out["issues"] = issues
        out["ok"] = not any(i["level"] == "error" for i in issues)
        out["verdict"] = ("需先修可信度/链路" if any(i["level"] == "error" for i in issues)
                          else ("可继续优化" if not issues else "可优化（有告警）"))

        # ④ plans/23 注册的可进化参数当前值（进化大脑可直接调）
        try:
            table = evolution_config.params_table()
            out["plan23_params"] = {k: v.get("current", v.get("default"))
                                    for k, v in table.items()
                                    if k.startswith(_PLAN23_PARAM_PREFIXES)}
        except Exception:  # noqa: BLE001
            out["plan23_params"] = {}
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": str(exc)[:160], "issues": []}

    _HEALTH_CACHE["ts"] = now
    _HEALTH_CACHE["data"] = out
    return out


def build_l0_summary() -> dict:
    """生成 L0 摘要（进化大脑的必读输入，≈300 token）。"""
    stats = _monitor_stats()
    params = evolution_config.params_table()
    current = {k: v.get("current", v.get("default")) for k, v in params.items()}
    summary = {
        "monitor": stats,
        "verify": _verify_stats(),          # plans/17：次日自我验证结论（聚焦每日推荐）
        "backtest": _backtest_diag(),       # plans/19：回测健康诊断（是不是回测的问题）
        "knowledge": _knowledge_block(),    # plans/21：私有域知识库（教训/活跃实验/否证清单）
        "health": _evolution_health(),      # plans/23：健康神经元（可信度+数据链路，统一管理入口）
        "current_params": current,
        "recent_events": _events_top(),
        "config_version": evolution_config.get("version", "?"),
    }
    _snapshot(summary)  # E5 状态快照（每轮关键指标）
    _kb_context_snapshot(summary)  # plans/21：知识库环境快照（限频）
    return summary


def _snapshot(summary: dict) -> None:
    """E5 状态快照：记录每轮关键指标 + 当前参数 + 事件摘要，供纵向对比。

    Bug10 修复：限频（默认 1 小时一条），避免 build_l0_summary 被 guard 巡检/前端
    刷新/模拟等频繁调用时每调用一次就写快照（快照失真 + 无谓 IO）。
    """
    global _snapshot_last_ts
    now = time.time()
    if now - _snapshot_last_ts < _SNAPSHOT_MIN_INTERVAL:
        return
    _snapshot_last_ts = now
    try:
        snaps: list = _read_json(SNAPSHOT_FILE, [])
        snaps.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "hit_rate": summary.get("monitor", {}).get("hit_rate", 0.0),
            "samples": summary.get("monitor", {}).get("samples", 0),
            "avg_t5_ret": summary.get("monitor", {}).get("avg_t5_ret", 0.0),
            # 综合目标（用户核心诉求"必须涨+主升浪"）：T+1 次日大涨 + T+5 主升浪
            "composite": summary.get("monitor", {}).get("composite", 0.0),
            "t1_hit_rate": summary.get("monitor", {}).get("t1_hit_rate", 0.0),
            "wave_hit_rate": summary.get("monitor", {}).get("wave_hit_rate", 0.0),
            "params": summary.get("current_params", {}),
            "events_n": len(summary.get("recent_events", [])),
        })
        snaps = snaps[-MAX_SNAPSHOTS:]
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snaps, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass  # 快照失败不阻塞


def build_l0_text(summary: dict | None = None) -> str:
    """L0 摘要 → 文本（供 LLM 进化大脑）。"""
    s = summary or build_l0_summary()
    m = s.get("monitor", {})
    _wave_w = float(m.get("wave_w", 0.4) or 0.4)
    lines = [
        # Bug39：窗口可配置（eval_window/I4），动态显示而非硬编码"近30日"
        f"近{m.get('window_days', 30)}日推荐: 样本 {m.get('samples',0)}，命中 {m.get('hit_n',0)}，命中率 {m.get('hit_rate',0)*100:.1f}%，T+5均收益 {m.get('avg_t5_ret',0)*100:+.1f}%",
        # 综合目标（用户核心诉求"必须涨 + 主升浪"）：次日大涨命中率 + 主升浪命中率 + 综合分
        f"综合目标(唯一宪法·必须涨+主升浪): 次日T+1大涨命中率 {m.get('t1_hit_rate',0)*100:.1f}%"
        f"（样本{m.get('t1_samples',0)}），主升浪T+5命中率 {m.get('wave_hit_rate',0)*100:.1f}%"
        f"（主升浪权重{_wave_w}）→ 综合分 {m.get('composite',0)*100:.1f}%",
        "当前参数: " + ", ".join(f"{k}={v}" for k, v in s.get("current_params", {}).items()),
    ]
    # plans/23 §十二：健康神经元（进化大脑先判断"结论可不可信、链路通不通"）
    h = s.get("health", {}) or {}
    if h:
        hb = h.get("backtest", {}) or {}
        dc = h.get("data_chain", {}) or {}
        lines.append(
            f"健康神经元: 判定={h.get('verdict', '?')}｜回测口径={hb.get('scope', '?')}"
            f" 质量门={hb.get('quality_gate', '?')} 区分度={'通过' if hb.get('discrimination_passed') else '未通过'}"
            f" A/B={hb.get('ab_decision', '?')} 数据门禁={hb.get('data_gate', '?')}"
        )
        lines.append(
            f"数据链路: 日线={dc.get('daily_max', '?')} 复权={dc.get('adj_max', '?')}"
            f" 股票池最新上市={dc.get('stock_basic_max_list', '?')}({dc.get('stock_basic_rows', 0)}只)"
            f" 最新推荐={h.get('latest_report_date', '?')}"
        )
        for it in (h.get("issues") or [])[:4]:
            lines.append(f"  ⚠️ [{it.get('code')}] {it.get('msg')}")
        if h.get("plan23_params"):
            lines.append("plans/23 可进化阈值: " + ", ".join(
                f"{k}={v}" for k, v in h["plan23_params"].items()))
    # plans/17：次日自我验证结论（聚焦"每日推荐为何没涨"，进化大脑核心依据）
    v = s.get("verify", {})
    if v:
        lines.append(
            f"次日验证(D+1买入持有1日) {v.get('date','')}: T+1命中率 {v.get('t1_hit_rate',0)*100:.1f}% "
            f"(涨{v.get('up_n',0)}/平{v.get('flat_n',0)}/跌{v.get('down_n',0)}，"
            f"平均T+1 {v.get('avg_t1_ret',0)*100:+.1f}%)"
        )
        if v.get("improvements"):
            lines.append("改进方向: " + "、".join(v["improvements"]))
        if v.get("attributions"):
            lines.append("失效归因TOP: " + " | ".join(
                f"{a.get('error_type')}({a.get('count')}例)" for a in v["attributions"]))
        if v.get("stage_report"):
            lines.append("环节归因TOP: " + " | ".join(
                f"{s.get('stage_name')}({s.get('count')}例)→{s.get('optimize','')}" for s in v["stage_report"]))
    # plans/18：系统图谱摘要（回测/每日推荐逻辑+参数，实时动态，进化大脑据此定位可优化环节）
    try:
        from app.agents import system_map
        m = system_map.build_map()
        lines.append(
            f"系统图谱: 每日推荐环节 {len(m.get('daily_recommend_pipeline', []))} 个 / "
            f"回测步骤 {len(m.get('backtest_pipeline', []))} 个 / "
            f"可进化参数 {len(m.get('evolution_params', []))} 个（详见 /evolve/map）"
        )
    except Exception:  # noqa: BLE001
        pass
    # plans/19：回测健康诊断（进化大脑要考虑"是不是回测的问题"）
    h = s.get("backtest", {})
    if h.get("ok") and h.get("verdict") != "insufficient":
        bt = h.get("backtest", {})
        lv = h.get("live", {})
        diag = (f"回测诊断[{h.get('verdict')}]: 回测accuracy={bt.get('accuracy')} "
                f"T+5均收益={bt.get('avg_t5_return')} 样本={bt.get('total_trades')}")
        if lv.get("t1_hit_rate") is not None:
            diag += f" | 实盘次日T+1命中={lv.get('t1_hit_rate')}"
        if h.get("suggestions"):
            diag += " → " + "；".join(h["suggestions"][:2])
        lines.append(diag)
        # 环节归因 TOP 变化观察（建议"维持现有逻辑，持续观察环节归因 TOP 是否变化"的落地依据）
        st = h.get("stage_trend") or {}
        if st.get("ok"):
            lines.append(f"环节归因TOP观察: 最新{st.get('latest_date','')} TOP={st.get('latest_top')} "
                         f"(上一期{st.get('prev_date','')}={st.get('prev_top')}) → {st.get('note')}")
        elif st.get("note"):
            lines.append(f"环节归因TOP观察: {st.get('note')}")
    # plans/21：私有域知识库（历史经验，综合分析必查；含活性实验与否证清单）
    kb = s.get("knowledge") or {}
    if kb:
        try:
            from app.kb import kb_index
            lines.append(kb_index.l0_text(kb))
        except Exception:  # noqa: BLE001
            pass
    # plans/22：交易纪律（另一半真相：纪律 alpha / 失效类型 / 反事实最优纪律；含"问题在选股端"结论）
    dtxt = _discipline_txt()
    if dtxt:
        lines.append(dtxt)
    evs = s.get("recent_events", [])
    if evs:
        lines.append("最近事件: " + " | ".join(f"[{e['level']}]{e['source']}:{e['message'][:60]}" for e in evs[-3:]))
    else:
        lines.append("最近事件: 无")
    return "\n".join(lines)


__all__ = ["build_l0_summary", "build_l0_text"]
