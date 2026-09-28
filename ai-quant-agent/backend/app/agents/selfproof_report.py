"""自证报告组装（selfproof_report）— 对齐 plans/25 P4 / §十三 F4·F5·F6·F8·F13

把"回放 + 结算 + 归因"的结果组装成**可用于进化决策与前端展示**的报告：

  ① 主指标 = **超额**（excess_vs_market，F8）；绝对胜率仅作辅
  ② **分层**：按环境三态（强/震/弱，复用 regime_split）、按形态、按相似度分位
  ③ **对照**：同期全市场等权 T+5（`bench_t5`，即"随机候选"的期望）—— 没有它就无法回答
     "是我选得好，还是那几天大盘好"（F6 的随机基线思想）
  ④ **IS/OOS**：按年份 K-fold 标记，并分别汇总（F4：验收只看 OOS）
  ⑤ **显著性**：t 值只做粗筛；**正式判据留给 P7 的 BH-FDR**（F5）
  ⑥ **费用对账**：按 run_id 从 `llm_metering` 取真实花费（F13）
  ⑦ **口径声明**：`selfproof_policy.report_flags()`（前视徽标 / 术语 / 数据门 / 幸存者偏差）
"""
from __future__ import annotations

import os
import time

from app.agents import selfproof_verify as SV
from app.backtest import selfproof as SP
from app.backtest import selfproof_policy as P
from app.core.logger import logger

MIN_SAMPLES_FOR_VERDICT = 30


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _win_rate(xs: list[float]) -> float | None:
    return round(sum(1 for x in xs if x > 0) / len(xs), 4) if xs else None


def _t_stat(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 5:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return None
    return round(m / ((var / n) ** 0.5), 3)


def _regime_of(date: str) -> str:
    """环境三态（**只用 <= date 的指数数据**，复用 regime_split）。失败返回"未知"。"""
    try:
        from app.backtest.regime_split import regime_of
        return str((regime_of(date) or {}).get("regime") or "未知")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[selfproof_report] regime_of({date}) 失败: {exc}")
        return "未知"


def _bucket(x: float | None, edges: tuple[float, ...], labels: tuple[str, ...]) -> str:
    if x is None:
        return "无"
    for e, lab in zip(edges, labels):
        if x <= e:
            return lab
    return labels[-1]


def build(run_id: str, llm_attribution: bool = False, max_failures: int | None = None) -> dict:
    """组装报告并写 `{run_id}/report.json`（覆盖 P2 的骨架，补齐 metrics/attribution）。"""
    v = SV.load_verify(run_id) or SV.settle_run(run_id)
    rows = v.get("rows") or []
    if not rows:
        return {"ok": False, "reason": "无结算数据（先 run_replay 再 settle_run）"}

    settled = [r for r in rows if r.get("t5") is not None]
    t5 = [float(r["t5"]) for r in settled]
    t5_net = [float(r["t5_net"]) for r in settled if r.get("t5_net") is not None]
    t1 = [float(r["t1"]) for r in rows if r.get("t1") is not None]
    t20 = [float(r["t20"]) for r in rows if r.get("t20") is not None]
    exc = [float(r["excess_t5"]) for r in settled if r.get("excess_t5") is not None]
    bench = [float(r["bench_t5"]) for r in settled if r.get("bench_t5") is not None]

    # ── 分层：环境 / 形态 / 相似度分位 ──
    # 单遍聚合 + 环境**按日期缓存**：原本每票都调一次 regime_of，20k 行就是 2 万次调用（P4 放大时会拖死）。
    regime_by_date: dict = {}

    def _reg(day: str) -> str:
        if day not in regime_by_date:
            regime_by_date[day] = _regime_of(day)
        return regime_by_date[day]

    buckets: dict = {k: {} for k in ("by_regime", "by_form", "by_similarity")}
    for r in settled:
        sim = r.get("similarity")
        sim_lab = _bucket(float(sim) if sim is not None else None,
                          (0.5, 0.7, 0.85), ("低≤0.5", "中0.5-0.7", "较高0.7-0.85", "高>0.85"))
        for key, b in (("by_regime", _reg(str(r.get("as_of") or ""))),
                       ("by_form", str(r.get("form_type") or "未知")),
                       ("by_similarity", sim_lab)):
            d = buckets[key].setdefault(b, {"t5": [], "exc": []})
            d["t5"].append(float(r["t5"]))
            if r.get("excess_t5") is not None:
                d["exc"].append(float(r["excess_t5"]))
    layers = {k: {b: {"n": len(d["t5"]), "avg_t5": _mean(d["t5"]),
                      "avg_excess_t5": _mean(d["exc"]),
                      "win_rate_excess": _win_rate(d["exc"])}
                  for b, d in vv.items()}
              for k, vv in buckets.items()}

    # ── IS / OOS（按年份 K-fold 标记，F4）──
    is_e, oos_e = [], []
    for r in settled:
        if r.get("excess_t5") is None:
            continue
        y = int(str(r.get("as_of") or "00000000")[:4] or 0)
        (oos_e if P.is_oos_year(y, fold=0) else is_e).append(float(r["excess_t5"]))
    is_oos = {
        "mode": P.oos_mode(), "ratio": P.split_ratio(), "frozen_oos_years": P.frozen_oos_years(),
        "in_sample": {"n": len(is_e), "avg_excess_t5": _mean(is_e), "win_rate": _win_rate(is_e)},
        "out_of_sample": {"n": len(oos_e), "avg_excess_t5": _mean(oos_e), "win_rate": _win_rate(oos_e)},
        "note": ("**验收只看 out_of_sample**（进化生效判据）；且 OOS 与实盘反馈解耦 —— "
                 "实盘只做监控存证，不进自证验收（plans/25 F4）"),
    }

    # ── 归因（默认规则化，零 LLM 费用）──
    att = SV.attribute_run(run_id, max_failures=max_failures, llm=bool(llm_attribution))

    # ── 费用对账（F13）──
    usage: dict = {"llm_calls": 0, "cost_note": "回放内核默认规则档：零 API 费用"}
    try:
        from app.agents import llm_metering
        s = llm_metering.summary(days=3650, run_id=run_id)
        usage = {"llm_calls": s.get("calls", 0), "prompt_tokens": s.get("prompt_tokens", 0),
                 "completion_tokens": s.get("completion_tokens", 0),
                 "reasoning_tokens": s.get("reasoning_tokens", 0),
                 "cost_cny": s.get("cost_cny", 0.0),
                 "priced": s.get("priced"),
                 "note": "按 run_id 归集；未调 LLM 时为 0"}
    except Exception as exc:  # noqa: BLE001
        usage["error"] = str(exc)[:160]

    dates = sorted({str(r.get("as_of")) for r in rows})
    n_exc = len(exc)
    # 数据门/幸存者偏差声明（P0.5 产物，缓存命中秒回）：报告必须带边界声明，
    # 否则读者不知道这段自证的样本边界在哪（会误以为"全市场"）。
    gate = SP.data_gate_cached() or {}
    report = {
        "run_id": run_id,
        "mode": "replay",
        "config": (SP.load_state(run_id) or {}),
        "flags": P.report_flags(
            as_of=f"{dates[0]}~{dates[-1]}" if dates else "",
            model=str(os.environ.get("DEEPSEEK_MODEL", "")),
            data_gate=(gate.get("data_gate") or {}),
            survivorship=(gate.get("survivorship") or {}),
            usage=usage,
            extra={"sample_size": n_exc},
        ),
        "dates": {"n_days": len(dates), "first": dates[0] if dates else "",
                  "last": dates[-1] if dates else "", "list": dates},
        "daily_stats": {"n_picks": len(rows), "n_settled_t5": len(settled),
                        "n_pending_t5": v.get("n_pending_t5")},
        "metrics": {
            "cost_pct": v.get("cost_pct"),
            "t1": {"n": len(t1), "win_rate": _win_rate(t1), "avg": _mean(t1)},
            "t5": {"n": len(t5), "win_rate": _win_rate(t5), "avg_gross": _mean(t5),
                   "avg_net": _mean(t5_net)},
            "t20": {"n": len(t20), "win_rate": _win_rate(t20), "avg": _mean(t20)},
            "excess_t5": {"n": n_exc, "avg": _mean(exc), "win_rate": _win_rate(exc),
                          "t_stat": _t_stat(exc),
                          "note": "**主指标**（超额 vs 同期全市场等权 T+5）；t 值仅粗筛，"
                                  "正式判据见 P7 的 BH-FDR"},
            "bench_t5": {"n": len(bench), "avg": _mean(bench),
                         "note": "同期全市场等权 T+5 = 『随机候选』的期望（F6 随机基线）"},
            "lift_vs_bench": (round((_mean(t5) or 0) - (_mean(bench) or 0), 4)
                              if (_mean(t5) is not None and _mean(bench) is not None) else None),
        },
        "layers": layers,
        "is_oos": is_oos,
        "attribution": {"n_failures": att.get("n_failures"),
                        "error_type_counts": att.get("error_type_counts"),
                        "form_avg_t5": att.get("form_avg_t5"),
                        "llm": att.get("llm"),
                        "llm_attribution": att.get("llm_attribution"),
                        "note": "默认规则化归因（零 LLM 费用）"},
        "usage": usage,
        "verdict": {
            "sufficient_samples": n_exc >= MIN_SAMPLES_FOR_VERDICT,
            "min_samples": MIN_SAMPLES_FOR_VERDICT,
            "note": ("样本量不足 → 只作观察，不得据此下结论/进化（plans/25 §五 样本量护栏）"
                     if n_exc < MIN_SAMPLES_FOR_VERDICT
                     else "样本量达门槛（正式判据仍需 P7 的 BH-FDR + 同区间 A/B）"),
        },
        "schema_keys": SP.REPORT_SCHEMA_KEYS,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    P.write_json_guarded(SP.report_path(run_id), report)
    return report


__all__ = ["build", "MIN_SAMPLES_FOR_VERDICT"]
