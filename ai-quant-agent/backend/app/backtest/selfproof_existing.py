"""模式 C：历史榜单**零成本回灌结算**（selfproof_existing）— 对齐 plans/25 §三 模式 C / P3

## 为什么先做这个

回放（模式 A/B）要重跑选股、要花时间（还可能花 API 钱）；但**历史榜单已经落盘了**，
直接用 as_of 之后的真实行情给它们打分，就能立刻回答"我这套系统历史上到底准不准"——
**零 LLM 调用、零 API 费用、读+算而已**。它同时是模式 A/B 的**校准标尺**
（重跑结果 vs 当年真实榜单结果的偏差 = 重放保真度）。

## 口径（与宪法一致，不许各写一套）

  - 结算口径：**D+1 买入口径**（推荐日 D 收盘后才知道结果，实际只能 D+1 买）—— 直接复用
    [`_calc_multi()`](ai-quant-agent/backend/app/agents/daily_verify.py:106)，绝不自造第二套算法。
  - 主指标：**超额 = 个股收益 − 同期全市场等权基准**（`excess_vs_market`，继承 evolution_config）——
    绝对胜率会被市场 beta 主导（大盘涨、什么都"准"），只能作辅。
  - 成本：净口径扣 `DEFAULT_COST_PCT`（0.15% 往返），毛口径同时给出，避免"看起来能赚"。
  - **复权口径安全**：这里用今天的前复权序列算历史收益率——收益率**不受复权基准影响**
    （基准只影响绝对价格水平；P2 的验证已实测"两口径收益率序列完全一致"），故无需额外处理。

## 覆盖度优先（F11）

`daily_recommend/` 里可能只有几个月、甚至没有 Agent 版榜单 —— 所以报告**先给覆盖度**，
再给结论；样本不足直接标 `insufficient_samples`，而不是拿 8 个样本吹胜率。
"""
from __future__ import annotations

import glob
import json
import os
import re

from app.backtest import selfproof_policy as P
from app.core.logger import logger

# 与回测口径一致的往返成本
try:
    from app.backtest.metrics import DEFAULT_COST_PCT
except Exception:  # noqa: BLE001
    DEFAULT_COST_PCT = 0.0015

REPORT_DIR = os.path.join(P.DATA_DIR, "daily_recommend")
OUT_DIR = P.sandbox_path("replay_existing")
STATE_FILE_MONITOR = os.path.join(P.DATA_DIR, "monitor_state.json")

MIN_SAMPLES_FOR_VERDICT = 30      # 少于这个样本量不打结论（与进化提案门槛同量级）


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _norm_date(v) -> str:
    s = str(v or "").strip().split(" ")[0].replace("-", "")
    return s if len(s) == 8 and s.isdigit() else ""


# ── 榜单发现（含版本化与 Agent 版）─────────────────────────────
_PAT_AGENT = re.compile(r"^daily_(\d{8})_agent(?:_v(\d+))?\.json$")
_PAT_SCAN = re.compile(r"^daily_(\d{8})(?:_v(\d+))?\.json$")


def discover_boards() -> dict:
    """发现历史榜单：{date: {"path", "source", "version", "picks"}}。

    同一天多个文件时优先 Agent 最终榜单（更接近实盘推荐），其次版本号最大者。
    """
    out: dict = {}
    for path in glob.glob(os.path.join(REPORT_DIR, "daily_*.json")):
        name = os.path.basename(path)
        m_a, m_s = _PAT_AGENT.match(name), _PAT_SCAN.match(name)
        if m_a:
            d, ver, src = m_a.group(1), int(m_a.group(2) or 0), "agent"
        elif m_s:
            d, ver, src = m_s.group(1), int(m_s.group(2) or 0), "scan"
        else:
            continue
        rank = (1 if src == "agent" else 0, ver)
        cur = out.get(d)
        if cur and cur["_rank"] >= rank:
            continue
        data = _read_json(path, None) or {}
        out[d] = {"path": path, "source": src, "version": ver,
                  "picks": data.get("top_picks") or [], "report_date": data.get("date", d),
                  "_rank": rank}
    for v in out.values():
        v.pop("_rank", None)
    return dict(sorted(out.items()))


def coverage() -> dict:
    """覆盖度（先看清楚有多少料，再谈结论）。"""
    boards = discover_boards()
    dates = list(boards.keys())
    per_day = [len(b["picks"]) for b in boards.values()]
    src_count: dict = {}
    for b in boards.values():
        src_count[b["source"]] = src_count.get(b["source"], 0) + 1

    state = _read_json(STATE_FILE_MONITOR, {}) or {}
    hits = state.get("hits", []) or []
    filled = [h for h in hits if h.get("t5_ret") is not None]
    hit_dates = sorted({_norm_date(h.get("date")) for h in hits if _norm_date(h.get("date"))})

    return {
        "board_dir": REPORT_DIR,
        "board_files": len(boards),
        "board_dates": dates,
        "board_date_range": [dates[0], dates[-1]] if dates else [],
        "board_source": src_count,
        "picks_total": int(sum(per_day)),
        "picks_per_day_avg": round(sum(per_day) / len(per_day), 1) if per_day else 0,
        "monitor_hits": len(hits),
        "monitor_hits_with_t5": len(filled),
        "monitor_hit_dates": hit_dates,
        "note": ("daily_recommend 与 monitor_state 的覆盖范围决定结论可信度；"
                 "两者都没有的历史区间只能靠模式 A/B 重跑（要花时间/钱）"),
    }


# ── 结算 ───────────────────────────────────────────────────────
def _settle_pick(ts_code: str, rec_date: str) -> dict | None:
    """单只票的 T+1/T+5/T+20（D+1 买入口径）。复用 daily_verify，不自造口径。"""
    try:
        from app.agents.daily_verify import _calc_multi
        return _calc_multi(ts_code, rec_date)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[selfproof_existing] 结算失败 {ts_code}@{rec_date}: {exc}")
        return None


def _benchmarks(dates: list[str]) -> dict:
    """同期全市场等权 T+5 基准（含成本），带磁盘缓存。失败返回 {}。"""
    try:
        from app.backtest.benchmark import benchmark_for_dates
        return benchmark_for_dates(dates) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[selfproof_existing] 基准计算失败（超额指标将缺失）: {exc}")
        return {}


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _win_rate(xs: list[float]) -> float | None:
    return round(sum(1 for x in xs if x > 0) / len(xs), 4) if xs else None


def _t_stat(xs: list[float]) -> float | None:
    """单样本 t 值（均值/标准误），仅作粗筛；正式判据留给 P7 的 BH-FDR。"""
    n = len(xs)
    if n < 5:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return None
    return round(m / ((var / n) ** 0.5), 3)


def monitor_settled(max_records: int | None = None) -> dict:
    """**台账口径**：直接用 `monitor_state.hits` 里**已回填**的记录聚合。

    为什么它是模式 C 的**主数据源**（实测）：本项目 `daily_recommend/` 只有 30 个榜单、
    共 127 条推荐（日均 4.2 条 —— Agent 精筛后 select 很少），而 `monitor_state` 有
    **1909 条命中记录、1895 条已回填 T+5** ⇒ 样本量大 15 倍，且收益率已经算好（零成本）。

    ⚠️ 口径不同，**不可与榜单口径混算**：monitor 记的是"扫描命中台账"（含历史多版本），
    不等于"最终给用户的推荐榜单"。两个口径要**并列展示**，而不是相加。
    """
    state = _read_json(STATE_FILE_MONITOR, {}) or {}
    hits = state.get("hits", []) or []
    if max_records:
        hits = hits[-int(max_records):]
    rows: list[dict] = []
    for h in hits:
        d = _norm_date(h.get("date"))
        if not d:
            continue
        t1, t5, t20 = h.get("t1_ret"), h.get("t5_ret"), h.get("t20_ret")
        if t1 is None and t5 is None and t20 is None:
            continue
        rows.append({"date": d, "ts_code": str(h.get("ts_code") or ""),
                     "form_type": str(h.get("form_type") or ""),
                     "t1": t1, "t5": t5, "t20": t20, "hit": h.get("hit")})
    dates = sorted({r["date"] for r in rows})
    bench = _benchmarks(dates) if dates else {}

    settled = [r for r in rows if r.get("t5") is not None]
    for r in settled:
        b5 = bench.get(r["date"])
        r["bench_t5"] = b5
        r["excess_t5"] = (round(float(r["t5"]) - b5, 4) if b5 is not None else None)
        r["t5_net"] = round(float(r["t5"]) - DEFAULT_COST_PCT, 4)

    t5s = [float(r["t5"]) for r in settled]
    t1s = [float(r["t1"]) for r in rows if r.get("t1") is not None]
    t20s = [float(r["t20"]) for r in rows if r.get("t20") is not None]
    net5 = [r["t5_net"] for r in settled]
    exc = [r["excess_t5"] for r in settled if r.get("excess_t5") is not None]

    by_form: dict = {}
    for r in settled:
        by_form.setdefault(r.get("form_type") or "未知", []).append(float(r["t5"]))
    by_date: dict = {}
    for r in settled:
        by_date.setdefault(r["date"], []).append(r)

    return {
        "ok": True,
        "source": "monitor_state（台账口径：扫描命中，含历史多版本；不等于最终榜单）",
        "date_range": [dates[0], dates[-1]] if dates else [],
        "n_records": len(rows),
        "n_settled_t5": len(settled),
        "cost_pct": DEFAULT_COST_PCT,
        "stats": {
            "t1": {"n": len(t1s), "win_rate": _win_rate(t1s), "avg": _mean(t1s)},
            "t5": {"n": len(t5s), "win_rate": _win_rate(t5s), "avg_gross": _mean(t5s),
                   "avg_net": _mean(net5)},
            "t20": {"n": len(t20s), "win_rate": _win_rate(t20s), "avg": _mean(t20s)},
            "excess_t5": {"n": len(exc), "win_rate": _win_rate(exc), "avg": _mean(exc),
                          "t_stat": _t_stat(exc)},
        },
        "by_form_type": {k: {"n": len(v), "avg_t5": _mean(v), "win_rate_t5": _win_rate(v)}
                         for k, v in sorted(by_form.items(), key=lambda kv: -len(kv[1]))},
        "series": {d: {"n": len(v), "avg_t5": _mean([float(x["t5"]) for x in v]),
                       "avg_excess_t5": _mean([x["excess_t5"] for x in v
                                               if x.get("excess_t5") is not None])}
                   for d, v in sorted(by_date.items())},
    }


def settle(max_dates: int | None = None, max_picks_per_day: int | None = None) -> dict:
    """对历史榜单逐日结算（读+算，零 API 费用）。"""
    boards = discover_boards()
    dates = list(boards.keys())
    if max_dates:
        dates = dates[-int(max_dates):]
    if not dates:
        return {"ok": False, "reason": "没有历史榜单可结算（daily_recommend 为空）", "coverage": coverage()}

    bench = _benchmarks(dates)
    rows: list[dict] = []
    pending = 0
    for d in dates:
        picks = boards[d]["picks"] or []
        if max_picks_per_day:
            picks = picks[: int(max_picks_per_day)]
        for p in picks:
            code = str(p.get("ts_code") or "")
            if not code:
                continue
            oc = _settle_pick(code, d)
            if not oc:
                pending += 1
                continue
            t1, t5, t20 = oc.get("t1"), oc.get("t5"), oc.get("t20")
            if t5 is None:
                pending += 1
            b5 = bench.get(d)
            rows.append({
                "date": d, "ts_code": code, "name": p.get("name", ""),
                "form_type": p.get("form_type", ""), "rank": p.get("rank"),
                "up_probability": p.get("up_probability"),
                "similarity": p.get("similarity"),
                "source": boards[d]["source"],
                "t1": t1, "t5": t5, "t20": t20,
                "t5_net": (round(t5 - DEFAULT_COST_PCT, 4) if t5 is not None else None),
                "bench_t5": b5,
                "excess_t5": (round(t5 - b5, 4) if (t5 is not None and b5 is not None) else None),
            })

    settled = [r for r in rows if r.get("t5") is not None]
    t1s = [r["t1"] for r in rows if r.get("t1") is not None]
    t5s = [r["t5"] for r in settled]
    t20s = [r["t20"] for r in rows if r.get("t20") is not None]
    net5 = [r["t5_net"] for r in settled if r.get("t5_net") is not None]
    exc = [r["excess_t5"] for r in settled if r.get("excess_t5") is not None]

    by_form: dict = {}
    for r in settled:
        k = str(r.get("form_type") or "未知")
        by_form.setdefault(k, []).append(r["t5"])
    form_stats = {k: {"n": len(v), "avg_t5": _mean(v), "win_rate_t5": _win_rate(v)}
                  for k, v in sorted(by_form.items(), key=lambda kv: -len(kv[1]))}

    # 按日聚合（画曲线用）
    by_date: dict = {}
    for r in settled:
        by_date.setdefault(r["date"], []).append(r)

    return {
        "ok": True,
        "coverage": coverage(),
        "n_picks": len(rows) + pending,
        "n_settled_t5": len(settled),
        "n_pending_t5": pending,
        "cost_pct": DEFAULT_COST_PCT,
        "stats": {
            "t1": {"n": len(t1s), "win_rate": _win_rate(t1s), "avg": _mean(t1s)},
            "t5": {"n": len(t5s), "win_rate": _win_rate(t5s), "avg_gross": _mean(t5s),
                   "avg_net": _mean(net5)},
            "t20": {"n": len(t20s), "win_rate": _win_rate(t20s), "avg": _mean(t20s)},
            "excess_t5": {"n": len(exc), "win_rate": _win_rate(exc), "avg": _mean(exc),
                          "t_stat": _t_stat(exc),
                          "note": "主指标：超额 vs 同期全市场等权 T+5（含成本）；t 值仅粗筛"},
        },
        "by_form_type": form_stats,
        "series": {d: {"n": len(v),
                       "avg_t5": _mean([x["t5"] for x in v]),
                       "avg_excess_t5": _mean([x["excess_t5"] for x in v
                                               if x.get("excess_t5") is not None])}
                   for d, v in sorted(by_date.items())},
        "rows": rows,
    }


def run(max_dates: int | None = None, max_picks_per_day: int | None = None,
        write: bool = True) -> dict:
    """跑一次模式 C 并落盘沙箱报告。"""
    res = settle(max_dates=max_dates, max_picks_per_day=max_picks_per_day)
    if not res.get("ok"):
        if write:
            P.write_json_guarded(os.path.join(OUT_DIR, "report.json"), res)
        return res

    n = res["stats"]["excess_t5"]["n"] or 0
    # 台账口径（主数据源：样本大、已回填、零成本）
    mon = monitor_settled()
    n_mon = (mon.get("stats", {}).get("excess_t5", {}) or {}).get("n") or 0

    report = {
        "mode": "replay_existing",
        "flags": P.report_flags(
            as_of=(res["coverage"]["board_date_range"] or [""])[0] + "~" +
                  (res["coverage"]["board_date_range"] or ["", ""])[-1],
            usage={"llm_calls": 0, "cost_note": "模式 C 只用已落盘榜单 + monitor 台账 + 行情，零 API 费用"},
            extra={"sample_size_board": n, "sample_size_monitor": n_mon},
        ),
        "verdict": {
            "sufficient_samples_board": n >= MIN_SAMPLES_FOR_VERDICT,
            "sufficient_samples_monitor": n_mon >= MIN_SAMPLES_FOR_VERDICT,
            "min_samples": MIN_SAMPLES_FOR_VERDICT,
            "note": ("两个口径**不可混算**：board=最终榜单口径、monitor=扫描台账口径；"
                     "样本不足者只作观察，不得据此下结论/进化（plans/25 §五 样本量护栏）"),
        },
        "result_board": res,          # 最终榜单口径（需重算收益率）
        "result_monitor": mon,        # 台账口径（已回填，样本大）
        "written_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 兼容旧字段名（避免调用方措手不及）
    report["result"] = res
    if write:
        P.write_json_guarded(os.path.join(OUT_DIR, "report.json"), report)
    return report


__all__ = ["discover_boards", "coverage", "settle", "run", "OUT_DIR",
           "MIN_SAMPLES_FOR_VERDICT"]
