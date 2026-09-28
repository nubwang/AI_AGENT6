"""自证回放的结算与失效归因（selfproof_verify）— 对齐 plans/25 P4

## 职责

  ① `settle_run(run_id)`  把回放产出的每日榜单**用 as_of 之后的真实行情结算**
     （T+1/T+5/T+20，**D+1 买入口径**）→ 写 `{run_id}/verify.json`
  ② `attribute_run(...)`  失败案例归因：**默认走规则化归因（零 LLM 费用）**，
     `llm=True` 时才接 [`_attribute_failure()`](ai-quant-agent/backend/app/agents/daily_verify.py:348)（受预算熔断保护）

## 口径纪律（不新增第二套算法）

  - 结算一律复用 [`_calc_multi()`](ai-quant-agent/backend/app/agents/daily_verify.py:106)（D+1 买入、前复权）；
  - 归因复用 [`_attribute_failure()`](ai-quant-agent/backend/app/agents/daily_verify.py:348) 与
    [`_aggregate()`](ai-quant-agent/backend/app/agents/daily_verify.py:387)，失败类型体系与每日验证**完全一致**，
    这样自证结论与实盘结论才能放在一起看；
  - **零成本优先**：规则化归因不需要 LLM，先把"失败的结构性原因"（大盘拖累/形态劣势/未到期）分出来，
    只有需要"为什么这只票的逻辑不成立"这种语义判断时才开 LLM。
"""
from __future__ import annotations

import json
import os

from app.backtest import selfproof as SP
from app.backtest import selfproof_policy as P
from app.core.logger import logger

try:
    from app.backtest.metrics import DEFAULT_COST_PCT
except Exception:  # noqa: BLE001
    DEFAULT_COST_PCT = 0.0015


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def load_daily_reports(run_id: str) -> dict:
    """读某个回放 run 的全部每日榜单：{as_of: report}。"""
    d = SP.run_dir(run_id)
    daily_dir = os.path.join(d, "daily")
    out: dict = {}
    if not os.path.isdir(daily_dir):
        return out
    for name in sorted(os.listdir(daily_dir)):
        if not name.endswith(".json"):
            continue
        rep = _read_json(os.path.join(daily_dir, name), None)
        if isinstance(rep, dict):
            out[str(rep.get("as_of") or name[:-5])] = rep
    return out


def _benchmarks(dates: list[str]) -> dict:
    try:
        from app.backtest.benchmark import benchmark_for_dates
        return benchmark_for_dates(dates) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[selfproof_verify] 基准计算失败（超额将缺失）: {exc}")
        return {}


def settle_run(run_id: str, max_picks_per_day: int | None = None) -> dict:
    """结算一个回放 run（零 LLM 费用）。返回 {ok, rows, stats, pending}。"""
    from app.agents.daily_verify import _calc_multi

    reports = load_daily_reports(run_id)
    if not reports:
        return {"ok": False, "reason": f"run {run_id} 没有每日榜单（先跑 selfproof.run_replay）"}

    dates = sorted(reports.keys())
    bench = _benchmarks(dates)
    rows: list[dict] = []
    pending = 0
    for as_of in dates:
        picks = reports[as_of].get("top_picks") or []
        if max_picks_per_day:
            picks = picks[: int(max_picks_per_day)]
        b5 = bench.get(as_of)
        for p in picks:
            code = str(p.get("ts_code") or "")
            if not code:
                continue
            oc = _calc_multi(code, as_of) or {}
            t1, t5, t20 = oc.get("t1"), oc.get("t5"), oc.get("t20")
            if t5 is None:
                pending += 1
            rows.append({
                "as_of": as_of, "ts_code": code, "name": p.get("name", ""),
                "form_type": p.get("form_type", ""), "rank": p.get("rank"),
                "up_probability": p.get("up_probability"), "similarity": p.get("similarity"),
                "t1": t1, "t5": t5, "t20": t20,
                "t5_net": (round(float(t5) - DEFAULT_COST_PCT, 4) if t5 is not None else None),
                "bench_t5": b5,
                "excess_t5": (round(float(t5) - b5, 4) if (t5 is not None and b5 is not None) else None),
            })

    settled = [r for r in rows if r.get("t5") is not None]
    verify = {
        "run_id": run_id, "dates": dates, "n_picks": len(rows),
        "n_settled_t5": len(settled), "n_pending_t5": pending,
        "cost_pct": DEFAULT_COST_PCT, "rows": rows,
        "note": "结算口径 = D+1 买入口径（复用 daily_verify._calc_multi），零 LLM 费用",
    }
    P.write_json_guarded(os.path.join(SP.run_dir(run_id), "verify.json"), verify)
    return {"ok": True, **verify}


def load_verify(run_id: str) -> dict:
    return _read_json(os.path.join(SP.run_dir(run_id), "verify.json"), {}) or {}


# ── 失败归因（默认规则化、零 LLM）──────────────────────────────
# 规则化归因的类别（与 daily_verify 的 error_type 体系对齐，语义可对上）
RULE_TYPES = ("市场环境拖累", "形态自身劣势", "未到期", "个股走弱", "其他")


def attribute_run(run_id: str, max_failures: int | None = None, llm: bool = False) -> dict:
    """失败归因。

    `llm=False`（默认）：**规则化归因**，零 API 费用 ——
      - 该日基准 T+5 < 0 → 「市场环境拖累」（个股跑输/跑赢都先记环境）
      - 形态在本次自证样本里平均 T+5 < 0 → 「形态自身劣势」
      - T+5 未到期 → 「未到期」
      - 其余（基准为正但个股 t5 <= 0）→ 「个股走弱」
    `llm=True`：接 daily_verify 的 LLM 归因（**受预算熔断**，烧钱前先过闸门）。
    """
    v = load_verify(run_id) or settle_run(run_id)
    rows = v.get("rows") or []
    if not rows:
        return {"ok": False, "reason": "无结算行（先 settle_run）"}

    settled = [r for r in rows if r.get("t5") is not None]
    by_form: dict = {}
    for r in settled:
        by_form.setdefault(str(r.get("form_type") or "未知"), []).append(float(r["t5"]))
    form_avg = {k: (sum(v2) / len(v2)) for k, v2 in by_form.items()}

    failures = []
    for r in rows:
        t5 = r.get("t5")
        if t5 is None:
            failures.append({**r, "error_type": "未到期", "reason": "T+5 尚未到期或无数据"})
            continue
        if float(t5) > 0:
            continue
        b5 = r.get("bench_t5")
        fa = form_avg.get(str(r.get("form_type") or "未知"))
        if b5 is not None and float(b5) < 0:
            et, why = "市场环境拖累", f"当日全市场等权 T+5={float(b5):.2%} < 0"
        elif fa is not None and fa < 0:
            et, why = "形态自身劣势", f"形态 {r.get('form_type')} 本样本均值 {fa:.2%} < 0"
        else:
            et, why = "个股走弱", "基准为正但个股下跌（个股层面问题）"
        failures.append({**r, "error_type": et, "reason": why})

    if max_failures:
        failures = failures[: int(max_failures)]

    agg: dict = {}
    for f in failures:
        agg[f["error_type"]] = agg.get(f["error_type"], 0) + 1

    out = {
        "ok": True, "run_id": run_id, "llm": bool(llm),
        "n_failures": len(failures),
        "error_type_counts": dict(sorted(agg.items(), key=lambda kv: -kv[1])),
        "failures": failures,
        "form_avg_t5": {k: round(v2, 4) for k, v2 in form_avg.items()},
    }

    if llm:
        try:
            from app.agents import llm_metering
            from app.agents.daily_verify import _aggregate, _attribute_failure, _format_pick
            attributed = []
            for f in failures:
                if llm_metering.budget_blocked():
                    out["llm_stopped_by_budget"] = True
                    break
                pick = {k: f.get(k) for k in ("ts_code", "name", "form_type", "up_probability",
                                              "similarity", "rank", "hit_rules", "condition_values")}
                try:
                    res = _attribute_failure(pick, {"date": f.get("as_of"),
                                                    "t1_ret": f.get("t1"), "t5_ret": f.get("t5")},
                                             0.0)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[selfproof_verify] LLM 归因失败 {f.get('ts_code')}: {exc}")
                    res = None
                if res:
                    attributed.append(res)
            if attributed:
                try:
                    stats, directions, stage_report, split = _aggregate(attributed)
                    out["llm_attribution"] = {"stats": stats, "directions": directions,
                                              "stage_report": stage_report, "split": split}
                except Exception as exc:  # noqa: BLE001
                    out["llm_attribution"] = {"error": str(exc)[:200]}
        except Exception as exc:  # noqa: BLE001
            out["llm_error"] = str(exc)[:200]

    P.write_json_guarded(os.path.join(SP.run_dir(run_id), "attribution.json"), out)
    return out


def build_verify_txt(run_id: str, top: int = 3) -> str:
    """给进化大脑 L0 的一段文本（自证结论摘要）。"""
    v = load_verify(run_id)
    if not v:
        return ""
    a = _read_json(os.path.join(SP.run_dir(run_id), "attribution.json"), {}) or {}
    counts = a.get("error_type_counts") or {}
    top_types = "、".join(f"{k}×{n}" for k, n in list(counts.items())[:top]) or "无"
    rep = _read_json(SP.report_path(run_id), {}) or {}
    m = ((rep.get("metrics") or {}).get("excess_t5") or {})
    return (f"【自证回放】run={run_id} 结算 {v.get('n_settled_t5')}/{v.get('n_picks')} 条；"
            f"超额T+5均值={m.get('avg')}（n={m.get('n')}）；失败归因TOP：{top_types}"
            f"（口径：{P.TERMINOLOGY['replay_metric_name']}，不等于当年系统成绩）")


__all__ = ["settle_run", "load_verify", "attribute_run", "build_verify_txt",
           "load_daily_reports", "RULE_TYPES"]
