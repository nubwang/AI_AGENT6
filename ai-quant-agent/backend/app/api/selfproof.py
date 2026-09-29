"""自证测试接口（plans/25）—— 用历史数据自证选股准确率 + 大盘相似波段

接口一览（前缀 /api/v1/selfproof）：
  - GET  /data-gate          数据门：最早可自证日期 + 幸存者偏差声明（前端必须展示）
  - GET  /params             默认参数（步长/抽样/最大天数/引擎/预算）
  - POST /estimate           运行前预估：耗时 + 样本量 + LLM 费用（低/中/高）+ 余额/预算闸门
  - POST /run                后台启动自证回放（可断点续跑；LLM 档受预算熔断保护）
  - POST /stop               协作式停止（当前这一天跑完即停，已完成的可续跑）
  - GET  /status             当前任务状态（进度/已完成天数/已出票数/预计剩余时间）
  - GET  /runs               历史自证任务列表
  - GET  /report/{run_id}    取报告（没有则实时结算 + 组装）
  - POST /settle/{run_id}    强制重新结算 + 重组报告
  - GET  /regime/candidates  大盘相似波段候选（模式 B：待 P5 实现，当前明确返回未实现）

设计口径（重要，避免误读）：
  1. 自证结果一律是「**重演口径**」：用 as_of 当天能看到的数据重跑选股，
     再用之后的真实行情验证 —— 它证明的是"**方法**在同一段历史里是否有效"，
     不是"当年的实盘成绩"（那时还没有这套系统）。
  2. 主指标是**超额**（`excess_vs_market`），不是胜率：胜率会被大盘 beta 主导。
  3. **抽样只数太小会一条票都出不来**（实测出票率 ≈0.13%/只/天，plans/25 §15.2），
     所以默认 universe=0（全市场）；想省时间请调大 `step`，别砍抽样。
  4. 全市场一年单线程 ≈99 小时（近期区间实测，plans/25 §16.4）→ 预估器会显式警告。
"""
from __future__ import annotations

import json
import os
import threading
import time

from fastapi import APIRouter, Body, HTTPException

from app.agents import selfproof_report as SR
from app.agents import selfproof_verify as SV
from app.backtest import selfproof as SP
from app.backtest import selfproof_policy as P
from app.core.logger import logger

router = APIRouter(prefix="/api/v1/selfproof", tags=["自证测试"])

# ── 运行前预估用的标定系数（plans/25 §15.1 / §16.4 实测，改代码前请先复测）──
COST_PER_STOCK_DAY_SEC = 0.31     # 近期区间实测（2019 区间只有 0.08，属于乐观口径）
FIXED_PER_DAY_SEC = 2.13          # 指数/基准/日历等与抽样只数无关的固定开销
HIT_RATE_PER_STOCK_DAY = 0.0013   # 出票率：P3 实测日均约 6 条 / 4600 只
SAMPLE_FOR_VERDICT = 30           # 样本量门槛（对齐 §五 护栏）

# ── 任务状态（单任务：自证回放会占满 IO/CPU，不允许并发跑两个）──
_REPLAY = {
    "status": "idle",        # idle / running / success / failed / stopped / paused_budget
    "run_id": "",
    "message": "",
    "started_at": "",
    "finished_at": "",
    "progress": {},          # 前端进度条与 ETA
    "result": None,
}
_STOP = threading.Event()
_LOCK = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ────────────────────────── 数据门 / 参数 ──────────────────────────
@router.get("/data-gate")
def data_gate():
    """数据门：**必须先看这个**，它决定"能自证到多早"和"边界在哪"。"""
    gate = SP.data_gate_cached() or {}
    return {
        "data_gate": gate.get("data_gate") or {},
        "survivorship": gate.get("survivorship") or {},
        "latest_as_of": gate.get("latest_as_of") or "",
        "note": ("自证只能落在『最早可自证日期』之后；且历史全市场扫描缺退市股（幸存者偏差），"
                 "报告里会带这条声明，读数时不要把它当成『当时全市场』"),
    }


@router.get("/params")
def params():
    """回放默认参数（可进化的那些，供前端表单预填）。"""
    def _p(name, default):
        try:
            return P._selfproof_param(name, default)
        except Exception:  # noqa: BLE001
            return default

    return {
        "step": _p("selfproof_default_step", 1),
        "universe": _p("selfproof_universe_sample", 0),
        "max_days": _p("selfproof_max_days", 250),
        "workers": _p("selfproof_workers", 1),
        # 0 = **不限**（用户要求"LLM 预算不要设置上限，预算无上限"）。
        # 该字段**只用于前端预填展示，不参与任何熔断判定**（判定在 llm_metering.check_budget，
        # 三闸门默认 0 = 不限；且闸门已从 deepseek_chat 摘除，每日推荐路径根本不受其约束）。
        "budget_cny": _p("selfproof_budget_default_cny", 0.0),
        "deep_top_n": _p("selfproof_deep_top_n", 50),
        "regime_topk": _p("selfproof_regime_topk", 5),
        # main_metric 在策略层是个 dict（含 hit_window / immutable 等元信息）；
        # 前端只需要一个可读的字符串，所以这里摊平成 str，元信息另放 main_metric_meta。
        "main_metric": str((P.main_metric() or {}).get("hit_definition") or "excess_vs_market"),
        "main_metric_meta": P.main_metric(),
        "terminology": P.TERMINOLOGY,
    }


# ────────────────────────── 预估 ──────────────────────────
def _estimate(start: str, end: str, step: int, universe: int, max_days: int,
              llm: bool, deep_top_n: int) -> dict:
    """运行前预估：天数 × (固定开销 + 只数×成本) → 耗时；出票率 → 样本量；LLM → 费用。"""
    dates = SP.replay_dates(start, end, step)
    if max_days and len(dates) > max_days:
        dates = dates[: int(max_days)]
    n_days = len(dates)
    # universe=0 → 全市场：用 stock_basic 行数近似（受幸存者偏差影响，但量级对）
    if not universe:
        try:
            from sqlalchemy import text

            from app.models import SessionLocal
            db = SessionLocal()
            try:
                universe_n = int(db.execute(text("SELECT COUNT(*) FROM stock_basic")).scalar() or 0)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            universe_n = 5400
    else:
        universe_n = int(universe)

    secs = n_days * (FIXED_PER_DAY_SEC + universe_n * COST_PER_STOCK_DAY_SEC)
    est_picks = round(n_days * universe_n * HIT_RATE_PER_STOCK_DAY, 1)
    out = {
        "start": start, "end": end or "", "step": int(step),
        "universe": int(universe), "universe_effective": universe_n,
        "days": n_days, "first": dates[0] if dates else "", "last": dates[-1] if dates else "",
        "wall_sec_estimate": round(secs, 1),
        "wall_human": (f"{secs / 3600:.1f} 小时" if secs >= 3600 else f"{secs / 60:.1f} 分钟"),
        "picks_estimate": est_picks,
        "sample_enough": est_picks >= SAMPLE_FOR_VERDICT,
        "llm": bool(llm),
        "engine_note": ("规则档：零 API 费用（推荐先跑这个）" if not llm
                        else "LLM 档：复刻每日推荐（预算不设上限、不会熔断，串行执行）"),
    }
    if est_picks < SAMPLE_FOR_VERDICT:
        out["warning"] = (f"⚠️ 预计只有 {est_picks} 条推荐（门槛 {SAMPLE_FOR_VERDICT}）："
                          "**抽样太小会一条都出不来**（实测出票率 0.13%/只/天，plans/25 §15.2）。"
                          "请把抽样调到 0（全市场），用『步长』来省时间。")
    if secs > 3 * 3600:
        out["warning_long"] = (f"⚠️ 预计耗时 {out['wall_human']}：全市场一年单线程约 99 小时（近期区间实测）。"
                              "建议加大 step（如 5 或 10）+ 夜间跑，或先用模式 C（历史榜单零成本回灌）拿结论。")

    # LLM 费用（三档报价 + 余额/预算）
    if llm:
        try:
            from app.agents import llm_metering as M
            out["llm_cost"] = M.estimate_task(days=n_days, deep_top_n=int(deep_top_n))
        except Exception as exc:  # noqa: BLE001
            out["llm_cost"] = {"error": str(exc)[:200]}
        try:
            from app.agents import llm_metering as M
            out["balance"] = M.balance()
            out["budget"] = M.spend_snapshot()
        except Exception as exc:  # noqa: BLE001
            out["balance"] = {"error": str(exc)[:160]}
    return out


@router.post("/estimate")
def estimate(payload: dict = Body(default={})):  # noqa: B008
    start = str(payload.get("start") or "").strip()
    if len(start) != 8 or not start.isdigit():
        raise HTTPException(400, "start 必须是 8 位日期，如 20240102")
    gate = (SP.data_gate_cached() or {}).get("data_gate") or {}
    earliest = str(gate.get("earliest_full_as_of") or "")
    if earliest and start < earliest:
        raise HTTPException(400, f"起始日早于『最早可自证日期』{earliest}（数据门，plans/25 P0.5）")
    return _estimate(
        start=start,
        end=str(payload.get("end") or "").strip(),
        step=int(payload.get("step") or 1),
        universe=int(payload.get("universe") or 0),
        max_days=int(payload.get("max_days") or 250),
        llm=bool(payload.get("llm")),
        deep_top_n=int(payload.get("deep_top_n") or 50),
    )


# ────────────────────────── 运行 / 停止 / 状态 ──────────────────────────
def _run_job(cfg: dict) -> None:
    """后台线程：回放 → 自动结算 → 组装报告（失败也写状态，前端能看到原因）。"""
    rid = cfg["run_id"]
    try:
        _REPLAY.update({"status": "running", "message": "回放中…", "started_at": _now()})
        last = {"t": 0.0}

        def _cb(as_of: str, i: int, total: int, done: int) -> None:
            now = time.time()
            if now - last["t"] < 2:          # 限流：不要把状态刷爆
                return
            last["t"] = now
            elapsed = now - float(_REPLAY.get("_t0") or now)
            per = elapsed / max(done, 1)
            _REPLAY["progress"] = {
                "as_of": as_of, "done": done, "total": total,
                "pct": round(done / max(total, 1) * 100, 1),
                "elapsed_sec": round(elapsed, 1),
                "eta_sec": round(per * max(total - done, 0), 1),
            }
            _REPLAY["message"] = f"回放中：{as_of}（{done}/{total}）"

        _REPLAY["_t0"] = time.time()
        res = SP.run_replay(
            start=cfg["start"], end=cfg["end"], step=cfg["step"],
            universe=cfg["universe"], max_days=cfg["max_days"], top_k=None,
            run_id=rid, resume=True, llm=cfg["llm"], workers=cfg["workers"],
            progress_cb=_cb, should_stop=_STOP.is_set,
        )
        # 回放完立刻结算 + 出报告（用户点开就能看结论）
        _REPLAY["message"] = "结算与报告组装中…"
        settle_err = ""
        try:
            SV.settle_run(rid)
            _b = SR.build(rid, llm_attribution=False)
            # build 用 `{ok:False, reason}` 表达"拒绝组装"（例如 0 推荐时样本为空），
            # 它**不抛异常** —— 不显式检查就会把骨架报告当成结论（踩过）。
            if isinstance(_b, dict) and not _b.get("ok", True):
                settle_err = f"报告未生成：{_b.get('reason')}（回放没有产出推荐，多半是抽样太小）"
        except Exception as exc:  # noqa: BLE001
            # 踩过的坑：这里如果只写 message，后面的"完成"文案会把它**覆盖掉**，
            # 于是结算失败被伪装成成功。所以错误必须存下来，在最终文案里带出去。
            settle_err = str(exc)[:300]
            logger.exception(f"[selfproof] 结算/报告失败 run_id={rid}: {exc}")

        st = str(res.get("status") or "")
        base = ("已停止（可续跑）" if st == "stopped" else
                "预算熔断，已优雅停止（可续跑）" if st == "paused_budget" else
                f"完成：{res.get('dates_done')} 天 / {res.get('picks_total')} 条推荐")
        _REPLAY.update({
            "status": "stopped" if st in ("stopped", "paused_budget") else "success",
            "message": base + (f"｜⚠️ 结算/报告失败：{settle_err}" if settle_err else ""),
            "settle_error": settle_err,
            "finished_at": _now(), "result": res,
        })
    except Exception as exc:  # noqa: BLE001
        _REPLAY.update({"status": "failed", "message": f"失败：{str(exc)[:300]}",
                        "finished_at": _now()})


@router.post("/run")
def run(payload: dict = Body(default={})):  # noqa: B008
    with _LOCK:
        if _REPLAY["status"] == "running":
            raise HTTPException(409, f"已有自证任务在跑（{_REPLAY.get('run_id')}），请先停止或等它结束")
        start = str(payload.get("start") or "").strip()
        if len(start) != 8 or not start.isdigit():
            raise HTTPException(400, "start 必须是 8 位日期，如 20240102")
        gate = (SP.data_gate_cached() or {}).get("data_gate") or {}
        earliest = str(gate.get("earliest_full_as_of") or "")
        if earliest and start < earliest:
            raise HTTPException(400, f"起始日早于『最早可自证日期』{earliest}（数据门，plans/25 P0.5）")

        end = str(payload.get("end") or "").strip()
        step = int(payload.get("step") or 1)
        universe = int(payload.get("universe") or 0)
        max_days = int(payload.get("max_days") or 250)
        llm = bool(payload.get("llm"))
        workers = SP.resolve_workers(
            int(payload["workers"]) if payload.get("workers") is not None else None, llm)

        rid = str(payload.get("run_id") or "") or SP.new_run_id(start, end, step, universe)
        est = _estimate(start, end, step, universe, max_days, llm,
                        int(payload.get("deep_top_n") or 50))
        cfg = {"start": start, "end": end, "step": step, "universe": universe,
               "max_days": max_days, "llm": llm, "workers": workers, "run_id": rid}
        _STOP.clear()
        _REPLAY.update({"status": "running", "run_id": rid, "message": "启动中…",
                        "started_at": _now(), "finished_at": "", "progress": {},
                        "result": None, "estimate": est, "config": cfg})

    threading.Thread(target=_run_job, args=(cfg,), daemon=True).start()
    return {"ok": True, "run_id": rid, "config": cfg, "estimate": est}


@router.post("/stop")
def stop():
    """协作式停止：当前这一天跑完即停（已完成的日期保留，可续跑）。"""
    if _REPLAY["status"] != "running":
        return {"ok": False, "message": f"当前没有在跑的任务（status={_REPLAY['status']}）"}
    _STOP.set()
    _REPLAY["message"] = "已请求停止：当前这一天跑完即停…"
    return {"ok": True, "run_id": _REPLAY.get("run_id")}


@router.get("/status")
def status():
    out = {k: v for k, v in _REPLAY.items() if not k.startswith("_")}
    rid = out.get("run_id")
    if rid:
        st = SP.load_state(rid) or {}
        out["state"] = {
            "done": len(st.get("done") or []), "total": st.get("dates_total"),
            "picks_total": st.get("picks_total"), "elapsed_sec": st.get("elapsed_sec"),
            "status": st.get("status"), "workers": st.get("workers"),
        }
    return out


@router.get("/runs")
def runs(limit: int = 30):
    items = SP.list_runs() or []
    items = sorted(items, key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return {"total": len(items), "items": items[: max(1, int(limit))]}


# ────────────────────────── 报告 / 结算 ──────────────────────────
def _read_report_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# 「0 推荐」时的统一提示：这是用户最容易踩的坑（抽样太小 → 一条票都出不来）
_EMPTY_HINT = ("回放没有产出推荐。实测出票率只有 0.13%/只/天（plans/25 §15.2）："
               "抽样几十只 × 十几天大概率是 0 条。请把『抽样只数』设为 0（全市场），"
               "用『步长』来省时间后重跑。")


@router.get("/report/{run_id}")
def report(run_id: str, rebuild: bool = False):
    """取报告。

    契约（**重要**）：报告可能"没有结论"，这时**绝不**用一堆 None 冒充结果，
    而是显式返回 `{ok: False, reason, hint}` —— 前端据此给用户看得懂的原因。
    """
    path = SP.report_path(run_id)
    if rebuild or not os.path.exists(path):
        try:
            SV.settle_run(run_id)
            built = SR.build(run_id, llm_attribution=False)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"报告组装失败：{str(exc)[:300]}") from exc
        if isinstance(built, dict) and not built.get("ok", True):
            return {"ok": False, "run_id": run_id, "reason": built.get("reason"),
                    "hint": _EMPTY_HINT,
                    "skeleton": (_read_report_file(path) if os.path.exists(path) else None)}
        return built
    try:
        data = _read_report_file(path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"报告读取失败：{str(exc)[:200]}") from exc
    # run_replay 落的是**占位骨架**（metrics.filled=False），它不是结论 —— 明确标出来
    if (data.get("metrics") or {}).get("filled") is False:
        return {"ok": False, "run_id": run_id,
                "reason": "该任务还没有可用结论（回放无产出或尚未结算）",
                "hint": _EMPTY_HINT,
                "skeleton": data}
    data.setdefault("ok", True)
    return data


@router.post("/settle/{run_id}")
def settle(run_id: str):
    v = SV.settle_run(run_id)
    rep = SR.build(run_id, llm_attribution=False)
    return {"ok": bool(v.get("ok")), "settle": {k: v.get(k) for k in
            ("ok", "n_picks", "n_settled_t5", "n_pending_t5", "reason")},
            "verdict": (rep or {}).get("verdict"), "report_path": SP.report_path(run_id)}


# ────────────────────────── 模式 B：大盘相似波段（待 P5）──────────────────────────
@router.get("/regime/candidates")
def regime_candidates():
    """大盘相似波段候选（模式 B）。

    当前**故意**返回明确的未实现状态，而不是给一个假列表：
    相似波段的 K 固定、整段距离口径、随机 K 段基线、稳健性检验都还没落地（plans/25 P5），
    先给数字会让人误以为"跨年份验证已经做完了"。
    """
    return {
        "implemented": False,
        "stage": "P5",
        "note": ("相似波段匹配（regime_match）尚未实现：需要先定 K、整段距离口径、"
                 "随机 K 段基线与稳健性检验（plans/25 §三 模式 B）。"
                 "在它落地前，跨年份验证请用『模式 A 起始日自证』把起始日设到目标年份。"),
        "planned_params": ["selfproof_regime_topk", "selfproof_similarity_metric", "regime_sim_window"],
    }
