"""纪律网格（discipline_grid）— plans/22 P2：反事实评估 → 证据驱动的买卖纪律

用户诉求（plans/22）："实现吧，再看看能不能让回测也能提供一些东西"。

本模块把**历史推荐样本（回测/每日推荐）**变成"纪律证据"：对每只样本在**多组纪律参数**下做
触价回放（止损 × 止盈折扣 × 持有期 × 追高上限），回答"如果当时用另一套纪律，结果会怎样"，
产出 `data/discipline_grid.json`：

  - 每形态 `stop_loss_pct / tp1_ratio / hold_days / chase_pct`：**历史证据最优**
  - `utility`：**可部署期望收益**（含成本；未成交样本记 0 收益 —— 钱没投出去，不赚不亏，
    但"买不到"的机会成本通过分母体现，避免用"少而精"的组合虚高期望）
  - `expectancy_on_filled`：已成交样本的平均净收益（便于理解收益水平）
  - `filled_rate / missed_rate / washout_rate`：可成交率 / 未成交率 / 被止损洗出率
  - `is_utility / oos_utility / oos_pass`：**样本内（前 60% 日期）择优 → 样本外验证**（防过拟合）
  - `baseline_utility / improvement`：相对**当前参数**的改进幅度（回答"值不值得改"）
  - `deployable`：是否满足可成交率下限（`plan_min_fill_rate`，默认 30%）

[trade_plan](ai-quant-agent/backend/app/backtest/trade_plan.py:1) 优先读取本表（`plan_use_grid`）
来设定价位 → "计划的默认纪律"不再拍脑袋，而是历史证据驱动；进化大脑仍可在其上做分层微调
（本表即它的先验）。

设计要点（plans/22 §五 防过拟合）
  - **样本内外分离**：择优用样本内，样本外用独立日期段验证；OOS 为负的组合不采纳（回退全局）
  - **分层收缩**：样本 < `plan_min_samples` 的形态直接采用全局最优参数（`fallback_global=true`）
  - **可成交率约束**：成交率 < `plan_min_fill_rate` 的组合不参与择优（防"只买得到的少数"虚高）
  - **含成本**：`plan_cost_pct` 计入每笔已成交样本
  - **保守口径**：同日先判止损；高开未成交单独统计
"""
from __future__ import annotations
 
import json
import os
import time
from typing import Any
 
import numpy as np

from app.core.logger import logger
from app.kb import kb_distiller, kb_store, kb_writer
from app.backtest.loader import load_daily
from app.backtest import target_price as TP   # 止盈目标位口径（plans/23 §15.5；默认 legacy 不生效）

DATA_DIR = kb_store.DATA_DIR
GRID_FILE = os.path.join(DATA_DIR, "discipline_grid.json")

# 参数网格（4 × 3 × 3 × 5 = 180 组/样本；单样本回放为微秒级）
STOP_GRID = (0.03, 0.05, 0.08, 0.10)
TP1_GRID = (0.4, 0.6, 0.8)
HOLD_GRID = (3, 5, 10)
CHASE_GRID = (0.0, 0.02, 0.03, 0.05, 0.08)

FALLBACK_AVG_T5, FALLBACK_AVG_T20 = 0.10, 0.20
IS_RATIO = 0.6            # 样本内占比（按日期排序前 60%）
MIN_FILL_RATE = 0.30      # 可成交率下限（低于此不可部署）
MIN_OOS_FILLED = 8        # 样本外最少成交数（不足 → 不做 OOS 判定）
MAX_WASHOUT = 0.85        # 被止损洗出率上限（高于此说明止损贴太紧、一进就被打，不可部署）
TP2_SHRINK = 0.7          # T+20 目标相对 T+5 折扣的额外收缩


def _cfg(name: str, default):
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param(name, default)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def _fcfg(name: str, default: float) -> float:
    try:
        return float(_cfg(name, default))
    except Exception:  # noqa: BLE001
        return float(default)


def _avg_returns() -> dict:
    """读回测入场指引的形态平均收益（作为目标价换算基准）。"""
    path = os.path.join(DATA_DIR, "entry_guidance.json")
    out: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            by_form = (json.load(f) or {}).get("by_form", {}) or {}
        for k, v in by_form.items():
            out[str(k)] = {
                "t5": float(v.get("avg_ret_t5") or 0) or FALLBACK_AVG_T5,
                "t20": float(v.get("avg_ret_t20") or 0) or FALLBACK_AVG_T20,
                "samples": int(v.get("count") or 0),
            }
    except Exception:  # noqa: BLE001
        pass
    return out


def _grid_key(stop: float, tp1_ratio: float, hold: int, chase: float) -> str:
    return (f"s{int(round(stop * 100))}_t{int(round(tp1_ratio * 100))}"
            f"_h{int(hold)}_c{int(round(chase * 100))}")


def _parse_key(key: str) -> tuple[float, float, int, float]:
    parts = key.split("_")
    return (int(parts[0][1:]) / 100.0, int(parts[1][1:]) / 100.0,
            int(parts[2][1:]), int(parts[3][1:]) / 100.0)


# ── 样本准备（只加载一次日线并转 numpy，避免 180 组重复解析）────────
def _load_samples(limit: int) -> list[dict]:
    cases = kb_store.query("case", limit=6000)[:limit]
    if not cases:
        return []
    cache: dict[str, Any] = {}
    rows: list[dict] = []
    seen: set = set()
    for cse in cases:
        code = str(cse.get("ts_code") or "")
        d0 = str(cse.get("date") or "")[:8]
        if not code or len(d0) != 8 or (code, d0) in seen:
            continue
        seen.add((code, d0))
        if code not in cache:
            try:
                cache[code] = load_daily(code, start_date=d0)
            except Exception:  # noqa: BLE001
                cache[code] = None
        df: Any = cache[code]
        if df is None or getattr(df, "empty", True):
            continue
        try:
            darr = df["trade_date"].dt.strftime("%Y%m%d").to_numpy()
            i0 = int(np.searchsorted(darr, d0, side="left"))
            if i0 >= len(df) - 1:
                continue
            o = df["open"].to_numpy(dtype=float)
            h = df["high"].to_numpy(dtype=float)
            lo = df["low"].to_numpy(dtype=float)
            cl = df["close"].to_numpy(dtype=float)
            t0_close = float(cl[i0])
            if not np.isfinite(t0_close) or t0_close <= 0:
                continue
        except Exception:  # noqa: BLE001
            continue
        rows.append({"code": code, "date": d0, "form": str(cse.get("form_type") or "?"),
                     "arrays": (o, h, lo, cl), "i0": i0, "t0_close": t0_close})
    rows.sort(key=lambda r: r["date"])
    return rows


# ── 单次回放（与 plan_tracker 同口径；返回净收益与风险指标）────────────
def _replay(arr, i0: int, t0_close: float, avg_t5: float, avg_t20: float,
            stop_pct: float, tp1_ratio: float, hold_days: int,
            chase_pct: float, cost_pct: float,
            tp_basis: str = "legacy", tp_mult: float = 0.0,
            stop_anchor: str = "") -> dict:
    """纪律回放。

    Args:
        tp_basis: 止盈目标位口径。**默认 legacy**（= `max(3%, avg_t5 × ratio)`，与历史行为逐字节一致）；
            传 "atr" 时改用 [`target_price`](ai-quant-agent/backend/app/backtest/target_price.py:1)
            的波动率口径（tp = T0 × (1 + mult × ATR%)）。
            §15.4 实测 `avg_t5` 是「成功组 × 最佳策略」的事后统计（+13.87%），
            用它设目标位会让命中率极低；ATR 口径把"目标多远"换成"可达性"。
        stop_anchor: **止损锚点**（plans/23 §15.3.2 / P2-7）。空串 ⇒ 读可进化参数
            `plan_stop_anchor`（**默认 "close_t0" = legacy，输出逐字节不变**）；
            传 "fill" ⇒ 锚在**实际成交价**（`fill`），这才是"风险相对成本"的正确语义。
    """
    o, h, lo, cl = arr
    n = len(cl)
    if tp_basis == "atr":
        _a = TP.atr_pct_arrays(h, lo, cl, i0)
        tp1 = float(TP.resolve(t0_close, _a, avg_t5, tp1_ratio,
                               basis="atr", mult=tp_mult)["price"])
    else:
        tp1 = t0_close * (1 + max(0.03, avg_t5 * tp1_ratio))
    tp2 = t0_close * (1 + max(0.05, avg_t20 * tp1_ratio * TP2_SHRINK))
    buy_high = t0_close * (1 + chase_pct)
    # ★ 止损锚点（plans/23 §15.3.2）：close_t0 = T0 收盘（**legacy，默认 ⇒ 输出不变**）
    #   / fill = 实际成交价（成交后再重算，见下方 fill 之后）
    if not stop_anchor:
        stop_anchor = str(_cfg("plan_stop_anchor", "close_t0") or "close_t0")
    stop_ref = t0_close
    stop = stop_ref * (1 - stop_pct)

    # 成交：T+1 起 3 日内，开盘价 ≤ 买入上限 → 开盘价成交；否则日内回落进区间 → 上限价成交
    fill_idx, fill, fill_type = -1, 0.0, ""
    for k in range(i0 + 1, min(i0 + 4, n)):
        if not (np.isfinite(o[k]) and o[k] > 0):
            continue
        if o[k] <= buy_high:
            fill_idx, fill, fill_type = k, float(o[k]), "open"
            break
        if np.isfinite(lo[k]) and lo[k] <= buy_high:
            fill_idx, fill, fill_type = k, float(buy_high), "pullback_intraday"
            break
    if fill_idx < 0:
        fwd = min(n - 1, i0 + 5)
        missed = (float(cl[fwd]) / t0_close - 1.0) if t0_close > 0 else 0.0
        return {"filled": False, "ret": None, "missed_ret": round(missed, 4),
                "days": 0, "stop_hit": False, "peak_after": None,
                "tp1": tp1, "tp2": tp2, "fill_type": ""}

    # ★ 锚点在成交价时：用**实际成交价**重算止损位（P2-7）。
    #   为什么这样才对：止损是"相对**成本**的风险预算"；原实现锚在 T0 收盘，
    #   而成交在 T+1 开盘，若隔夜跳空低开，就会出现"**入门即止损**"的假信号
    #   （§15.3.2 实测：5% 止损时占成交单 29.3%，3% 时高达 57.5%）。
    if stop_anchor == "fill":
        stop_ref = float(fill)
        stop = stop_ref * (1 - stop_pct)

    # 日内回踩成交：当日最低价可能出现在限价单成交**之前** → 当天不判离场（防前视偏差）
    start_k = fill_idx if fill_type == "open" else fill_idx + 1
    # 离场：同日先判止损（保守），再判止盈
    for k in range(start_k, min(fill_idx + hold_days + 1, n)):
        if stop > 0 and np.isfinite(lo[k]) and lo[k] <= stop:
            px = min(float(o[k]), stop) if (np.isfinite(o[k]) and o[k] > 0) else stop
            look_end = min(n, k + 6)
            peak = float(np.max(h[k:look_end])) if look_end > k else 0.0
            return {"filled": True, "ret": round(px / fill - 1.0 - cost_pct, 4),
                    "missed_ret": None, "days": k - fill_idx, "stop_hit": True,
                    "peak_after": peak, "tp1": tp1, "tp2": tp2, "fill_type": fill_type}
        if np.isfinite(h[k]) and h[k] >= tp1:
            look_end = min(n, k + 11)
            peak = float(np.max(h[k:look_end])) if look_end > k else 0.0
            return {"filled": True, "ret": round(tp1 / fill - 1.0 - cost_pct, 4),
                    "missed_ret": None, "days": k - fill_idx, "stop_hit": False,
                    "peak_after": peak, "tp1": tp1, "tp2": tp2, "fill_type": fill_type}
    last = max(min(fill_idx + hold_days, n - 1), min(start_k, n - 1))
    look_end = min(n, last + 6)
    peak = float(np.max(h[last:look_end])) if look_end > last else 0.0
    return {"filled": True, "ret": round(float(cl[last]) / fill - 1.0 - cost_pct, 4),
            "missed_ret": None, "days": last - fill_idx, "stop_hit": False,
            "peak_after": peak, "tp1": tp1, "tp2": tp2, "fill_type": fill_type}


def _blank() -> dict:
    return {"n": 0, "filled": 0, "sum_ret": 0.0, "wins": 0, "stops": 0,
            "sum_days": 0, "missed": 0, "sum_missed": 0.0}


def _acc(d: dict, res: dict) -> None:
    d["n"] += 1
    if res["filled"]:
        r = float(res["ret"] or 0)
        d["filled"] += 1
        d["sum_ret"] += r
        d["wins"] += 1 if r > 0 else 0
        d["stops"] += 1 if res.get("stop_hit") else 0
        d["sum_days"] += int(res.get("days") or 0)
    else:
        d["missed"] += 1
        d["sum_missed"] += float(res.get("missed_ret") or 0)


def _utility(d: dict) -> float:
    """可部署期望收益：已成交净收益 / 全部样本（未成交记 0）。"""
    return d["sum_ret"] / max(1, d["n"])


def _fill_rate(d: dict) -> float:
    return d["filled"] / max(1, d["n"])


def _washout(d: dict) -> float:
    return d["stops"] / max(1, d["filled"])


def _entry(key: str, d: dict, base: dict, oos: dict | None, min_fill: float,
           hold_mean: float | None = 0.0, oos_hold: float | None = None,
           base_oos: float | None = None) -> dict:
    stop, tp1_ratio, hold, chase = _parse_key(key)
    filled = max(1, d["filled"])
    fr = _fill_rate(d)
    oos_u = (round(_utility(oos), 4)
             if (oos is not None and int(oos.get("filled") or 0) >= MIN_OOS_FILLED) else None)
    base_u = round(_utility(base), 4) if base and base.get("n") else None
    u = round(_utility(d), 4)
    hm = 0.0 if hold_mean is None else float(hold_mean)
    # 样本外验证口径：**相对"无纪律持有"的 alpha ≥ 0**（稳健）；同时记录相对当前参数的差异
    oos_alpha = (round(oos_u - oos_hold, 4)
                 if (oos_u is not None and oos_hold is not None) else None)
    oos_vs_base = (round(oos_u - base_oos, 4)
                   if (oos_u is not None and base_oos is not None) else None)
    return {
        "stop_loss_pct": stop, "tp1_ratio": tp1_ratio, "hold_days": int(hold),
        "chase_pct": chase,
        "utility": u,
        "expectancy_on_filled": round(d["sum_ret"] / filled, 4),
        "win_rate": round(d["wins"] / filled, 4),
        "filled_rate": round(fr, 4),
        "missed_rate": round(d["missed"] / max(1, d["n"]), 4),
        "washout_rate": round(d["stops"] / filled, 4),
        "avg_hold_days": round(d["sum_days"] / filled, 1),
        "avg_missed_ret": round(d["sum_missed"] / max(1, d["missed"]), 4) if d["missed"] else None,
        "samples": d["filled"], "n": d["n"],
        "hold_baseline_utility": hm,
        "alpha_utility": round(u - hm, 4),
        "is_utility": u,
        "oos_utility": oos_u,
        "oos_hold_baseline": oos_hold,
        "oos_alpha_utility": oos_alpha,
        "oos_vs_baseline": oos_vs_base,
        "oos_pass": (True if oos_alpha is None else oos_alpha >= 0),
        "baseline_utility": base_u,
        "improvement": (round(u - base_u, 4) if base_u is not None else None),
        "deployable": bool(fr >= min_fill),
        "fallback_global": False,
    }


def _gate_for_grid(rows: list[dict], best: dict, cost: float) -> dict:
    """★ 统一门槛接线（plans/24 §11.17 / §11.31 / D4）：把「网格最优纪律」也过一遍
    `baseline_gate.judge_default()`（费率 0.5% + 滑点 20bp + **两档 t**），
    而不是只靠本模块自定义的那几个阈值（fill/washout/improvement/oos）。

    **只读**：收集「逐日净收益序列」后调用门槛，**不改任何网格结论**；
    样本不足 / 异常 ⇒ `{"available": False, ...}`（诚实：不给数）。
    """
    try:
        if not best:
            return {"available": False, "reason": "无全局最优"}
        stop = float(best.get("stop_loss_pct") or 0.0)
        r1 = float(best.get("tp1_ratio") or 0.6)
        hold = int(best.get("hold_days") or 5)
        chase = float(best.get("chase_pct") or 0.03)
        byd: dict[str, list[float]] = {}
        for r in rows:
            res = _replay(r["arrays"], r["i0"], r["t0_close"], r["avg_t5"], r["avg_t20"],
                          stop, r1, hold, chase, cost)
            if not res.get("filled") or res.get("ret") is None:
                continue
            byd.setdefault(str(r["date"])[:10], []).append(float(res["ret"]))
        ser = [float(np.mean(v)) for k, v in sorted(byd.items()) if k and v]
        if len(ser) < 30:
            return {"available": False,
                    "reason": f"日序列仅 {len(ser)} 天（< 30，功效不足 ⇒ 不可判定）"}
        from app.backtest import baseline_gate as BG
        from app.backtest.stats_correction import newey_west_t, plain_t
        cand = BG.Candidate(
            name="纪律网格·全局最优", gross=float(np.mean(ser)),
            t=float(plain_t(ser)), t_hac=float(newey_west_t(ser, 4)),
            per_year=252.0 / max(1, hold), by_seg={}, n=len(ser))
        j = BG.judge_default(cand)
        j["coverage"] = BG.coverage(cand)
        j["n_days"] = len(ser)
        if j["coverage"]["missing"]:
            j["verdict"] = "not_decidable"
        j["available"] = True
        return j
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)[:120]}


def build_grid(limit: int = 600, save: bool = True, distill: bool = True) -> dict:
    """对历史样本跑纪律网格 → 每形态最优纪律（样本内择优 + 样本外验证）。

    止盈目标位口径由可进化参数 `plan_tp_basis` 决定（默认 **legacy** = 历史行为）。
    """
    kb_writer.flush_now()
    rows = _load_samples(limit)
    if not rows:
        return {"ok": False, "reason": "无可用历史案例（需先回填 EKB 案例）"}
    cost = _fcfg("plan_cost_pct", 0.0025)
    # 目标位口径只读一次（避免在 353×180 次回放里反复读配置）
    tp_basis = TP.tp_basis()
    tp_mult = TP.tp_atr_mult()
    if tp_basis != TP.TP_BASIS_DEFAULT:
        logger.info(f"[discipline_grid] 止盈口径 = {tp_basis}（ATR 倍数 {tp_mult}）")
    chase0 = _fcfg("plan_max_chase_pct", 0.03)
    base_stop = _fcfg("plan_stop_loss_pct", 0.05)
    base_tp1 = _fcfg("plan_tp1_ratio", 0.60)
    base_hold = int(_fcfg("plan_hold_days", 5))
    min_samples = int(_fcfg("plan_min_samples", 30))
    min_fill = _fcfg("plan_min_fill_rate", MIN_FILL_RATE)
    max_washout = _fcfg("plan_max_washout", MAX_WASHOUT)
    avg = _avg_returns()

    # 无纪律基准：T+1 收盘买入、持有到 T+5 卖出（用于算"纪律 alpha"；也按样本内/外分开）
    hold_sum: dict[str, float] = {}
    hold_n: dict[str, int] = {}
    for r in rows:
        _o, _h, _lo, cl = r["arrays"]
        i0, n_bars = r["i0"], len(cl)
        a_i, b_i = min(i0 + 1, n_bars - 1), min(i0 + 6, n_bars - 1)
        hr = (float(cl[b_i] / cl[a_i]) - 1.0 - cost) if cl[a_i] > 0 else 0.0
        r["hold_ret"] = hr

    def _hold_mean(scope: str, split: str = "all") -> float | None:
        n = hold_n.get(f"{scope}|{split}", 0)
        return round(hold_sum.get(f"{scope}|{split}", 0.0) / n, 4) if n else None

    cut = rows[int(len(rows) * IS_RATIO)]["date"] if len(rows) > 2 else "99999999"
    for r in rows:
        r["split"] = "is" if r["date"] <= cut else "oos"
        a = avg.get(r["form"]) or {}
        r["avg_t5"] = float(a.get("t5") or FALLBACK_AVG_T5)
        r["avg_t20"] = float(a.get("t20") or FALLBACK_AVG_T20)
        for scope in (r["form"], "*"):
            for split in (r["split"], "all"):
                hk = f"{scope}|{split}"
                hold_sum[hk] = hold_sum.get(hk, 0.0) + float(r["hold_ret"])
                hold_n[hk] = hold_n.get(hk, 0) + 1

    # 网格：外层组合 / 内层样本（同时累计 is+oos+all 与形态、全局两个 scope）
    stats: dict[tuple, dict] = {}
    for stop in STOP_GRID:
        for r1 in TP1_GRID:
            for hold in HOLD_GRID:
                for chase in CHASE_GRID:
                    key = _grid_key(stop, r1, hold, chase)
                    for r in rows:
                        res = _replay(r["arrays"], r["i0"], r["t0_close"], r["avg_t5"],
                                      r["avg_t20"], stop, r1, hold, chase, cost,
                                      tp_basis=tp_basis, tp_mult=tp_mult)
                        for scope in (r["form"], "*"):
                            for split in (r["split"], "all"):
                                _acc(stats.setdefault((scope, split, key), _blank()), res)

    # 当前参数基线（用于回答"改了值不值"）
    base_stats: dict[tuple, dict] = {}
    base_key = _grid_key(base_stop, base_tp1, base_hold, chase0)
    for r in rows:
        res = _replay(r["arrays"], r["i0"], r["t0_close"], r["avg_t5"], r["avg_t20"],
                      base_stop, base_tp1, base_hold, chase0, cost,
                      tp_basis=tp_basis, tp_mult=tp_mult)
        for scope in (r["form"], "*"):
            for split in (r["split"], "all"):
                _acc(base_stats.setdefault((scope, split), _blank()), res)

    def _cands(scope: str, split: str) -> list[tuple[str, dict]]:
        return [(k.split("|")[-1], v) for (sc, sp, k), v in stats.items()
                if sc == scope and sp == split]

    def _pick(scope: str, split: str, need: int) -> tuple[str, dict] | None:
        """择优：成交率/洗出率达标 + 样本足量 → 效用最大；不行再逐级放宽。"""
        base_ok = [(k, v) for k, v in _cands(scope, split)
                   if v["filled"] >= need and _fill_rate(v) >= min_fill
                   and _washout(v) <= max_washout]
        if not base_ok:
            base_ok = [(k, v) for k, v in _cands(scope, split)
                       if v["filled"] >= max(5, need // 3) and _washout(v) <= max_washout]
        if not base_ok:
            # 最后放宽洗出率（用于样本太少时给出方向，但会在结果里标出 washout 偏高）
            base_ok = [(k, v) for k, v in _cands(scope, split) if v["filled"] >= max(5, need // 3)]
        return max(base_ok, key=lambda kv: _utility(kv[1])) if base_ok else None

    def _mk(key: str, scope: str) -> dict:
        b_all = base_stats.get((scope, "all")) or {}
        b_oos = base_stats.get((scope, "oos"))
        return _entry(key, stats[(scope, "all", key)], b_all, stats.get((scope, "oos", key)),
                      min_fill, _hold_mean(scope, "all"), _hold_mean(scope, "oos"),
                      round(_utility(b_oos), 4) if (b_oos and b_oos.get("n")) else None)

    global_pick = _pick("*", "is", min(10, min_samples)) or _pick("*", "all", 10)
    forms = sorted({r["form"] for r in rows})
    by_form: dict = {}
    for form in forms:
        pick = _pick(form, "is", min_samples)
        fallback = False
        if not pick:
            pick, fallback = global_pick, True
        if not pick:
            continue
        key, _ = pick
        sc = "*" if fallback else form
        e = _mk(key, sc)
        e["fallback_global"] = fallback
        # OOS 不通过 → 回退全局（防"样本内最优只是噪声"）
        if not fallback and not e["oos_pass"] and global_pick:
            gk, _g = global_pick
            ge = _mk(gk, "*")
            ge["fallback_global"] = True
            ge["oos_rejected_key"] = key
            e = ge
            key = gk
        by_form[form] = e

    global_best: dict = {}
    if global_pick:
        gk, _ = global_pick
        global_best = _mk(gk, "*")

    # 透明性：全局 Top8 组合（供前端/进化大脑查看反事实地形）
    ranked = sorted(_cands("*", "all"), key=lambda kv: -_utility(kv[1]))[:8]
    top = [dict(_mk(k, "*"), key=k) for k, v in ranked]

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "samples": len(rows), "is_cut": cut,
        "is_samples": sum(1 for r in rows if r["split"] == "is"),
        "oos_samples": sum(1 for r in rows if r["split"] == "oos"),
        "grid": {"stop": list(STOP_GRID), "tp1_ratio": list(TP1_GRID),
                 "hold_days": list(HOLD_GRID), "chase_pct": list(CHASE_GRID)},
        "cost_pct": cost, "min_samples": min_samples, "min_fill_rate": min_fill,
        "max_washout": max_washout,
        "hold_baseline": {k: _hold_mean(k.split("|")[0], k.split("|")[1]) for k in hold_n},
        "baseline": {
            "stop_loss_pct": base_stop, "tp1_ratio": base_tp1,
            "hold_days": base_hold, "chase_pct": chase0,
            "utility": round(_utility(base_stats.get(("*", "all")) or _blank()), 4),
            "alpha_utility": round(_utility(base_stats.get(("*", "all")) or _blank())
                                   - float(_hold_mean("*") or 0.0), 4),
            "oos_utility": (round(_utility(base_stats[("*", "oos")]), 4)
                            if base_stats.get(("*", "oos")) else None),
        },
        "global": global_best,
        "by_form": by_form,
        "top_global": top,
        "note": ("反事实触价回放：效用=已成交净收益/全部样本（未成交记 0，避免'少而精'虚高）；"
                 "日内回踩成交当天不判离场（防前视偏差）；样本内(前60%日期)择优 + 样本外验证；"
                 "成交率下限 / 洗出率上限 / 样本不足 → 回退全局参数"),
    }
    # ── ★ 统一门槛接线（D4）：另附「网格最优」过 baseline_gate 的结论（**只记录**）──
    try:
        out["gate"] = _gate_for_grid(rows, out.get("global") or {}, cost)
    except Exception as _eg:  # noqa: BLE001
        out["gate"] = {"available": False, "reason": str(_eg)[:120]}

    if save and (global_best or by_form):
        try:
            with open(GRID_FILE, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            logger.info(f"[discipline_grid] 已保存 {GRID_FILE}（样本 {len(rows)}，形态 {len(by_form)}，"
                        f"全局最优 {global_best.get('stop_loss_pct')}/{global_best.get('tp1_ratio')}"
                        f"/{global_best.get('hold_days')}/{global_best.get('chase_pct')}，"
                        f"效用 {global_best.get('utility')}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[discipline_grid] 保存失败: {exc}")
    if distill:
        try:
            out["lessons"] = distill_lessons(out)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[discipline_grid] 教训沉淀失败: {exc}")
    return out


# ── 读取（供 trade_plan / API / L0）──────────────────────────
def load_grid() -> dict:
    try:
        with open(GRID_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


def best_params(form_type: str = "") -> dict:
    """取该形态最优纪律（无形态/样本不足 → 全局；无表 → 空 dict）。"""
    g = load_grid()
    if not g:
        return {}
    bf = (g.get("by_form") or {}).get(str(form_type))
    if bf:
        return bf
    return g.get("global") or {}


def plan_override(form_type: str = "") -> dict:
    """给 trade_plan 用的参数覆盖（仅在 `plan_use_grid` 开启、网格可用、且确有改进时生效）。

    采纳门槛（防"越改越差"）：
      ① 网格组合可部署（成交率达标）；
      ② 相对**当前参数**的改进 > `plan_grid_min_improvement`（默认 0.2pct）；
      ③ 样本外稳健（OOS alpha ≥ 0）。
    不满足 → 返回 {}，trade_plan 保持参数表默认值（并可在 basis 里说明"当前参数已近最优"）。
    """
    try:
        if not bool(int(_cfg("plan_use_grid", 1) or 0)):
            return {}
    except Exception:  # noqa: BLE001
        pass
    b = best_params(form_type)
    if not b or not b.get("deployable", True):
        return {}
    imp = b.get("improvement")
    min_imp = _fcfg("plan_grid_min_improvement", 0.002)
    if imp is None or float(imp) <= min_imp:
        return {}
    if not b.get("oos_pass", True):
        return {}
    # 样本量门槛：择优时可放宽（给出方向），但**采纳**必须有足够证据（默认 ≥30 笔成交样本）
    if int(b.get("samples") or 0) < int(_fcfg("plan_adopt_min_samples", 30)):
        return {}
    return {
        "plan_max_chase_pct": b.get("chase_pct"),
        "plan_stop_loss_pct": b.get("stop_loss_pct"),
        "plan_tp1_ratio": b.get("tp1_ratio"),
        "plan_hold_days": b.get("hold_days"),
        "_grid": {"samples": b.get("samples"), "n": b.get("n"),
                  "utility": b.get("utility"), "oos_utility": b.get("oos_utility"),
                  "oos_pass": b.get("oos_pass"), "fallback_global": b.get("fallback_global"),
                  "improvement": b.get("improvement")},
    }


def grid_enabled() -> bool:
    """网格是否启用（供 trade_plan / API 判断，不涉及是否采纳）。"""
    try:
        return bool(int(_cfg("plan_use_grid", 1) or 0))
    except Exception:  # noqa: BLE001
        return True


def grid_txt() -> str:
    """纪律网格文本（供 L0 / 对话 / 前端面板）。"""
    g = load_grid()
    if not g:
        return ""
    gl = g.get("global") or {}
    lines = [
        f"纪律网格({g.get('generated_at', '')[:10]}, 样本 {g.get('samples')}，"
        f"样本内 {g.get('is_samples')}/样本外 {g.get('oos_samples')}): "
        f"全局最优 止损{float(gl.get('stop_loss_pct') or 0)*100:.0f}% / 目标折扣"
        f"{float(gl.get('tp1_ratio') or 0)*100:.0f}% / 持有{gl.get('hold_days')}日 / "
        f"追高上限{float(gl.get('chase_pct') or 0)*100:.0f}% → 可部署期望"
        f"{float(gl.get('utility') or 0)*100:+.2f}%（成交率{float(gl.get('filled_rate') or 0)*100:.0f}%，"
        f"样本外{('%.2f%%' % (float(gl.get('oos_utility') or 0) * 100)) if gl.get('oos_utility') is not None else '不足'}）"
    ]
    bl = g.get("baseline") or {}
    if gl.get("utility") is not None and bl.get("utility") is not None:
        lines.append(f"当前参数(止损{float(bl.get('stop_loss_pct') or 0)*100:.0f}%/折扣"
                     f"{float(bl.get('tp1_ratio') or 0)*100:.0f}%/持有{bl.get('hold_days')}日/追高"
                     f"{float(bl.get('chase_pct') or 0)*100:.0f}%) 期望"
                     f"{float(bl.get('utility') or 0)*100:+.2f}% → 改进空间"
                     f"{(float(gl.get('utility') or 0) - float(bl.get('utility') or 0))*100:+.2f}pct")
    # 结论级提示：全部组合期望为负 → 问题在选股端（纪律只能减亏，不能创造 alpha）
    u = float(gl.get("utility") or 0.0)
    if u < 0:
        lines.append(
            f"⚠ 全部纪律组合可部署期望为负（最优 {u*100:+.2f}%）→ **问题在选股端**"
            f"（无纪律持有基准 {float(gl.get('hold_baseline_utility') or 0)*100:+.2f}%，"
            f"纪律 alpha {float(gl.get('alpha_utility') or 0)*100:+.2f}%）；"
            f"纪律的价值是减亏（「截断左尾」），应优先提高入选质量/收紧信号，而非继续调纪律"
        )
    bf = g.get("by_form") or {}
    if bf:
        parts = []
        for f, v in sorted(bf.items())[:6]:
            parts.append(f"形态{f}: 止损{float(v.get('stop_loss_pct') or 0)*100:.0f}%/折扣"
                         f"{float(v.get('tp1_ratio') or 0)*100:.0f}%/持有{v.get('hold_days')}日/"
                         f"期望{float(v.get('utility') or 0)*100:+.2f}%(n={v.get('samples')}"
                         f"{', 回退全局' if v.get('fallback_global') else ''}"
                         f"{', 样本外未过' if not v.get('oos_pass', True) else ''})")
        lines.append("分形态最优: " + " | ".join(parts))
    return "\n".join(lines)


def distill_lessons(grid: dict | None = None, min_n: int = 30) -> int:
    """把"最优纪律"沉淀为教训（type=discipline）→ 进 L0/检索，供进化大脑调参参照。"""
    g = grid or load_grid()
    if not g:
        return 0
    items: list[tuple[str, dict]] = []
    gl = g.get("global") or {}
    if gl:
        items.append(("global", gl))
    for f, v in (g.get("by_form") or {}).items():
        if v.get("fallback_global") or not v.get("samples"):
            continue
        items.append((f"form_{f}", v))
    touched = 0
    for name, v in items:
        n = int(v.get("samples") or 0)
        if n < min_n or not v.get("deployable", True):
            continue
        lid = kb_distiller.lesson_id("discipline", f"grid_{name}")
        existing = kb_store.get("lesson", lid) or {}
        stop = float(v.get("stop_loss_pct") or 0) * 100
        tp1 = float(v.get("tp1_ratio") or 0) * 100
        chase = float(v.get("chase_pct") or 0) * 100
        label = "全局" if name == "global" else f"形态{name.replace('form_', '')}"
        u_v = float(v.get("utility") or 0.0)
        extra = ("" if u_v >= 0 else "；该期望为负 → 纪律只能减亏，**应优先修选股端（信号/入选条件）**")
        text = (f"纪律最优（反事实网格，{label}）：止损 {stop:.0f}%、目标折扣 {tp1:.0f}%、"
                f"持有 {v.get('hold_days')} 日、追高上限 {chase:.0f}% → "
                f"可部署期望 {u_v*100:+.2f}%"
                f"（成交率 {float(v.get('filled_rate') or 0)*100:.0f}%，被洗出率 "
                f"{float(v.get('washout_rate') or 0)*100:.0f}%，纪律 alpha "
                f"{float(v.get('alpha_utility') or 0)*100:+.2f}%，n={n}）{extra}")
        support = dict(existing.get("support") or {})
        support.update({"cases_n": n, "total_n": int(v.get("n") or 0),
                        "last_date": time.strftime("%Y%m%d"),
                        "utility": v.get("utility"), "oos_utility": v.get("oos_utility"),
                        "improvement": v.get("improvement")})
        ok = kb_store.upsert("lesson", {
            "id": lid,
            "ts": existing.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "text": text,
            "type": "discipline",
            "scope": {"side": "both", "source": "discipline_grid", "form_type": name},
            "actionable": {
                "param_hint": ("选股端为负 → 优先修信号（入选条件/概率阈值/形态过滤），"
                               "再谈纪律参数微调" if u_v < 0 else
                               "plan_stop_loss_pct / plan_tp1_ratio / plan_hold_days / plan_max_chase_pct"),
                "hint": (f"该{label}纪律网格择优结果：相对当前参数改进 "
                         f"{float(v.get('improvement') or 0)*100:+.2f}pct，"
                         f"样本外{'稳健' if v.get('oos_pass') else '未通过（不宜采纳）'}")},
            "support": support,
            "confidence": kb_distiller.confidence_for(n),
            "half_life_days": 120.0,
            "applied_experiments": existing.get("applied_experiments") or [],
            "effect_verdict": existing.get("effect_verdict"),
            "status": "active" if (n >= 30 and v.get("oos_pass")) else "candidate",
            "last_verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        touched += 1 if ok else 0
    if touched:
        logger.info(f"[discipline_grid] 纪律网格教训沉淀 {touched} 条")
    return touched


__all__ = ["build_grid", "load_grid", "best_params", "plan_override", "grid_txt",
           "distill_lessons", "GRID_FILE", "STOP_GRID", "TP1_GRID", "HOLD_GRID", "CHASE_GRID"]
