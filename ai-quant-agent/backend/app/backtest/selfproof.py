"""自证测试回放内核（selfproof）— 对齐 plans/25 P2

## 这个模块做什么（且**只做**这些）

  ① **逐日推进**：把 as_of 从 start 移到 end（按 step），每天调用
     [`_run_daily_scan_impl()`](ai-quant-agent/backend/app/backtest/daily_scan.py:232) 重跑一遍选股
     —— 直接调 impl 而**不走** [`run_daily_scan()`](ai-quant-agent/backend/app/backtest/daily_scan.py:191)，
     因为后者持有进程内全局 `_SCAN_LOCK`（回放要并行，不能被它串行化，plans/25 F9）。
  ② **沙箱落盘**：当日榜单写 `data/selfproof/{run_id}/daily/{as_of}.json`，全部经
     [`write_json_guarded()`](ai-quant-agent/backend/app/backtest/selfproof_policy.py:296)（写真实资产直接抛错）。
  ③ **断点续跑**：`state.json` 记 next_date / 已完成日期 / 累计耗时，续跑不重复劳动。
  ④ **优雅停止**：用户停止回调 + LLM 预算熔断（`llm_metering.budget_blocked()`）→ 落盘 + 标记状态，不崩。
  ⑤ **报告头部**：P2 阶段先产出 `report.json` 的**口径骨架**（前视徽标 / 主指标 / IS-OOS / 数据门 /
     幸存者偏差 / 费用），P4 再往里填胜率与归因。

## 明确不做（避免越界）

  - 不做 T+1/T+5/T+20 结算与失效归因 → P4 的 `selfproof_verify`
  - **默认不调 LLM**（`llm=False`）：本内核先跑"规则档"，零 API 费用；LLM 档由 P1.5/P4 接上
  - 不写任何真实资产（护栏强制）
"""
from __future__ import annotations

import json
import os
import time

from app.backtest import selfproof_policy as P
from app.backtest.loader import get_trade_dates
from app.core.logger import logger

RUNS_FILE = P.sandbox_path("runs.json")

# ── report.json 契约（P4→P7 的接口，plans/25 E3）────────────────
# 先冻结键名，避免 P4/P7 来回改结构。P2 只填 flags/dates/usage_hint 部分。
REPORT_SCHEMA_KEYS = [
    "run_id", "config", "flags", "dates", "daily_stats",
    "metrics", "attribution", "usage", "created_at", "updated_at",
]


# ── 路径与任务注册 ─────────────────────────────────────────────
def run_dir(run_id: str) -> str:
    return P.sandbox_path(str(run_id))


def daily_path(run_id: str, as_of: str) -> str:
    return os.path.join(run_dir(run_id), "daily", f"{as_of}.json")


def state_path(run_id: str) -> str:
    return os.path.join(run_dir(run_id), "state.json")


def report_path(run_id: str) -> str:
    return os.path.join(run_dir(run_id), "report.json")


def new_run_id(start: str, end: str, step: int, universe: int, tag: str = "") -> str:
    """确定性 run_id（同参数同日可复现，便于对比与缓存）。"""
    base = f"{start}_{end or 'latest'}_s{step}_u{universe or 0}"
    if tag:
        base += f"_{tag}"
    return f"run_{base}"


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def load_state(run_id: str) -> dict:
    return _read_json(state_path(run_id), {}) or {}


def save_state(run_id: str, state: dict) -> bool:
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return P.write_json_guarded(state_path(run_id), state)


def write_daily(run_id: str, as_of: str, report: dict) -> bool:
    """写当日回放榜单（沙箱，带护栏）。"""
    return P.write_json_guarded(daily_path(run_id, as_of), report)


def upsert_run(meta: dict) -> None:
    runs = _read_json(RUNS_FILE, []) or []
    if not isinstance(runs, list):
        runs = []
    runs = [r for r in runs if r.get("run_id") != meta.get("run_id")]
    runs.append(meta)
    P.write_json_guarded(RUNS_FILE, runs[-200:])


def list_runs() -> list:
    runs = _read_json(RUNS_FILE, []) or []
    return runs if isinstance(runs, list) else []


# ── 日期序列 ───────────────────────────────────────────────────
def replay_dates(start: str, end: str = "", step: int = 1) -> list[str]:
    """回放用的 as_of 序列（trade_cal ∩ [start, end]，按 step 抽样）。"""
    end = end or time.strftime("%Y%m%d")
    dates = [d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
             for d in get_trade_dates(start, end)]
    dates = [d for d in dates if d]
    if step and step > 1:
        dates = dates[:: int(step)]
    return dates


# ── 数据门（缓存：`data/selfproof/data_gate.json`）─────────────
def data_gate_cached() -> dict:
    cached = _read_json(P.sandbox_path("data_gate.json"), None)
    if isinstance(cached, dict) and cached.get("data_gate"):
        return cached
    try:
        from app.models import SessionLocal
        from app.backtest import selfproof_data_gate as G
        db = SessionLocal()
        try:
            res = G.run(db)
        finally:
            db.close()
        P.write_json_guarded(P.sandbox_path("data_gate.json"), res)
        return res
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[selfproof] 数据门获取失败（回放继续，但报告缺该声明）: {exc}")
        return {}


def budget_blocked() -> bool:
    """LLM 预算是否已熔断（LLM 档时用于优雅停止）。"""
    try:
        from app.agents import llm_metering
        return bool(llm_metering.budget_blocked())
    except Exception:  # noqa: BLE001
        return False


# ── 并行度解析（P2.5）─────────────────────────────────────────
def resolve_workers(workers: int | None = None, llm: bool = False) -> int:
    """解析回放并行度（None → 读 evolution_config.selfproof_workers）。

    **LLM 档强制串行**：预算熔断要求"跑到哪、花到哪"精确，并行预取会在熔断前
    多花最多 2×workers 天的钱（见 plans/25 §4.4 硬闸门）。
    抽出来单独一个函数是为了**可被自证脚本直接断言**（不必真去调 LLM 花钱）。
    """
    if workers is None:
        try:
            workers = int(P._selfproof_param("selfproof_workers", 4) or 4)
        except Exception:  # noqa: BLE001
            workers = 4
    workers = max(1, int(workers or 1))
    if llm and workers > 1:
        logger.warning("[selfproof] LLM 档强制串行（workers=1），避免预取导致预算超支")
        return 1
    return workers


def _day_worker(payload: dict) -> dict:
    """**多进程**回放单日（必须模块级函数才能 pickle）。

    分工契约：**子进程只负责算，不负责写**。落盘/状态/runs 一律由父进程按日期顺序做，
    这样并发既不会两个进程写同一个文件，也不会把"已完成日期"的顺序搞乱（断点续跑依赖它）。
    代价：进程内 `should_stop` 无法穿透（callable 不可 pickle）→ 停止只在**日与日之间**生效，
    正在跑的那一天会跑完（可接受：一天的量级是分钟级，见 §15.1）。
    """
    from app.backtest.daily_scan import _run_daily_scan_impl

    d = str(payload.get("as_of") or "")
    try:
        return _run_daily_scan_impl(
            max_stocks=None, scan_date=d, top_k=payload.get("top_k"), progress_cb=None,
            should_stop=None, as_of=d,
            universe=payload.get("universe"),
            universe_seed=int(payload.get("universe_seed") or 0), persist=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[selfproof] as_of={d} 回放失败（子进程）: {exc}")
        return {"date": d, "error": str(exc)[:300], "top_picks": []}


# ── 主入口：逐日回放 ───────────────────────────────────────────
def run_replay(start: str, end: str = "", step: int | None = None,
               universe: int | None = None, max_days: int | None = None,
               top_k: int | None = None, run_id: str = "", resume: bool = True,
               universe_seed: int = 20260922, llm: bool = False,
               workers: int | None = None,
               progress_cb=None, should_stop=None) -> dict:
    """执行一次自证回放（默认**规则档、零 API 费用**）。

    Args:
        start/end: as_of 区间（YYYYMMDD）；end 空=最新交易日
        step: 步长（None → 读 evolution_config.selfproof_default_step，默认 1）
        universe: 抽样只数（None → 读 selfproof_universe_sample；0=全市场）
        max_days: 单次最多回放天数（None → 读 selfproof_max_days）
        llm: 是否启用 LLM 精筛（**本阶段默认 False**；开启后受预算熔断保护）
        workers: 并行度（None → 读 selfproof_workers）。**按日并行**：每天是一次独立的
            全市场扫描，天然无依赖，且落盘/台账/进度都按日组织 → 不动下游任何契约。
            实测每只每天 80ms（plans/25 §15.1），全市场一年单线程 ≈31 小时，必须并行。
            **必须用多进程**：线程池实测被 GIL 互踩，4 线程比串行还慢一个数量级（§15.4）。
            **LLM 档强制串行**（预算熔断要逐日精确，预取会超支）。
        resume: 断点续跑（跳过 state 里已完成的日期）
    """
    from app.backtest.daily_scan import _run_daily_scan_impl

    if step is None:
        try:
            step = int(P._selfproof_param("selfproof_default_step", 1) or 1)
        except Exception:  # noqa: BLE001
            step = 1
    if universe is None:
        try:
            universe = int(P._selfproof_param("selfproof_universe_sample", 0) or 0)
        except Exception:  # noqa: BLE001
            universe = 0
    if max_days is None:
        try:
            max_days = int(P._selfproof_param("selfproof_max_days", 250) or 250)
        except Exception:  # noqa: BLE001
            max_days = 250
    workers = resolve_workers(workers, bool(llm))

    dates = replay_dates(start, end, step)
    if max_days and len(dates) > max_days:
        dates = dates[: int(max_days)]
    if not dates:
        return {"status": "error", "message": "回放日期序列为空（检查 start/end 与 trade_cal）"}

    rid = run_id or new_run_id(start, end, int(step), int(universe or 0))
    state = load_state(rid) if resume else {}
    done: list[str] = list(state.get("done") or [])
    t0 = time.time()
    elapsed_prev = float(state.get("elapsed_sec") or 0.0)
    picks_total = int(state.get("picks_total") or 0)

    state.update({
        "run_id": rid, "start": start, "end": end or "", "step": int(step),
        "universe": int(universe or 0), "max_days": int(max_days or 0),
        "llm": bool(llm), "top_k": top_k or 0, "universe_seed": int(universe_seed),
        "workers": int(workers),
        "status": "running", "llm_metering_note": "规则档（未调 LLM）" if not llm else "LLM 档（受预算熔断）",
        "created_at": state.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_state(rid, state)

    # ── 逐日执行（P2.5：workers>1 按日并行；结果与串行**逐日一致**）──────
    paused_for_budget = False
    pending = [d for d in dates if d not in done]

    def _one_day(d: str) -> dict:
        """单日回放；异常转成 error 记录而不抛出，避免一天失败拖垮整轮。"""
        try:
            return _run_daily_scan_impl(
                max_stocks=None, scan_date=d, top_k=top_k, progress_cb=None,
                should_stop=should_stop, as_of=d,
                universe=(int(universe) if universe else None),
                universe_seed=int(universe_seed), persist=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[selfproof] as_of={d} 回放失败: {exc}")
            return {"date": d, "error": str(exc)[:300], "top_picks": []}

    # 进程池 vs 串行：**不用线程池** —— 实测线程池在纯 Python 重负载下 GIL 互踩，
    # 4 线程比串行慢一个数量级（CPU 365% 但吞吐崩塌，plans/25 §15.4）。
    _use_proc = workers > 1 and len(pending) > 1
    logger.info(("[selfproof] 并行回放：多进程 workers=%d（待跑 %d 天）"
                 % (workers, len(pending))) if _use_proc
                else "[selfproof] 串行回放（workers=1）")

    from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

    def _mk_pool():
        return (ProcessPoolExecutor(max_workers=workers) if _use_proc
                else ThreadPoolExecutor(max_workers=1))

    with _mk_pool() as _pool:
        _it = iter(pending)
        _window: dict = {}

        def _pull() -> None:
            """滑动窗口：既并行，又不会把全部结果堆在内存里。"""
            try:
                _d = next(_it)
            except StopIteration:
                return
            if _use_proc:
                _window[_d] = _pool.submit(_day_worker, {
                    "as_of": _d, "top_k": top_k,
                    "universe": (int(universe) if universe else None),
                    "universe_seed": int(universe_seed)})
            else:
                _window[_d] = _pool.submit(_one_day, _d)

        for _ in range(workers * 2):
            _pull()

        for i, as_of in enumerate(pending):
            fut = _window.pop(as_of, None)
            if fut is None:                       # 窗口应始终覆盖当前日；缺了说明状态异常
                logger.warning(f"[selfproof] 并行窗口缺日 {as_of}，提前结束本轮（可续跑）")
                break
            _pull()                               # 先补窗口，让 worker 不停手

            if should_stop is not None and should_stop():
                state["status"] = "stopped"
                break
            if llm and budget_blocked():
                paused_for_budget = True
                state["status"] = "paused_budget"
                logger.error(f"[selfproof] LLM 预算熔断 → 优雅停止（已完成 {len(done)} 天，可续跑）")
                break

            if progress_cb:
                try:
                    progress_cb(as_of, i + 1, len(pending), len(done))
                except Exception:  # noqa: BLE001
                    pass

            rep = fut.result()
            if rep.get("cancelled"):
                state["status"] = "stopped"
                save_state(rid, state)
                break

            rep["_selfproof"] = {"as_of": as_of, "run_id": rid, "llm": bool(llm),
                                 "written_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            write_daily(rid, as_of, rep)
            done.append(as_of)
            picks_total += len(rep.get("top_picks") or [])

            state.update({"done": done, "next_date": as_of, "picks_total": picks_total,
                          "elapsed_sec": round(elapsed_prev + (time.time() - t0), 2)})
            save_state(rid, state)

    if state.get("status") in ("running", None):
        state["status"] = "done"
    elapsed = round(elapsed_prev + (time.time() - t0), 2)
    state.update({"done": done, "elapsed_sec": elapsed, "picks_total": picks_total,
                  "dates_total": len(dates),
                  "dates_left": len([d for d in dates if d not in done])})
    save_state(rid, state)

    # ── 报告骨架（P2 先立口径，P4 填指标）──
    gate = data_gate_cached()
    flags = P.report_flags(
        as_of=f"{start}~{end or dates[-1]}",
        model=str(os.environ.get("DEEPSEEK_MODEL", "")),
        data_gate=(gate.get("data_gate") or {}),
        survivorship=(gate.get("survivorship") or {}),
    )
    report = {
        "run_id": rid,
        "config": {"start": start, "end": end or "", "step": int(step),
                   "universe": int(universe or 0), "max_days": int(max_days or 0),
                   "llm": bool(llm), "top_k": top_k or 0, "universe_seed": int(universe_seed),
                   "workers": int(workers)},
        "flags": flags,
        "dates": {"total": len(dates), "done": len(done),
                  "first": dates[0], "last": dates[-1],
                  "completed": done},
        "daily_stats": {"picks_total": picks_total,
                        "avg_picks_per_day": round(picks_total / max(len(done), 1), 2)},
        "metrics": {"note": "P4 填充：重演口径胜率（主指标=超额 excess_vs_market）", "filled": False},
        "attribution": {"note": "P4 填充：失效归因聚合", "filled": False},
        "usage": {"wall_sec": elapsed,
                  "llm_calls": "见 data/llm_usage.jsonl（按 run_id 过滤）" if llm else 0,
                  "cost_note": "规则档零 API 费用" if not llm else "见 llm_metering.summary(run_id=…)"},
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "schema_keys": REPORT_SCHEMA_KEYS,
        "paused_for_budget": paused_for_budget,
    }
    P.write_json_guarded(report_path(rid), report)
    upsert_run({"run_id": rid, "start": start, "end": end or "", "step": int(step),
                "universe": int(universe or 0), "llm": bool(llm), "workers": int(workers),
                "status": state.get("status"), "dates_done": len(done),
                "dates_total": len(dates), "picks_total": picks_total,
                "elapsed_sec": elapsed, "updated_at": state.get("updated_at")})

    return {"status": state.get("status"), "run_id": rid, "dates_total": len(dates),
            "dates_done": len(done), "dates_left": len([d for d in dates if d not in done]),
            "picks_total": picks_total, "elapsed_sec": elapsed,
            "run_dir": run_dir(rid), "report": report_path(rid),
            "paused_for_budget": paused_for_budget}


__all__ = [
    "run_replay", "replay_dates", "load_state", "save_state", "write_daily",
    "list_runs", "upsert_run", "run_dir", "daily_path", "state_path", "report_path",
    "new_run_id", "data_gate_cached", "budget_blocked", "resolve_workers",
    "REPORT_SCHEMA_KEYS", "RUNS_FILE",
]
