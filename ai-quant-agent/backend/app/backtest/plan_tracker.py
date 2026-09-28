"""交易计划台账与触价回放（plan_tracker）— plans/22 商品化纪律进化 P0

解决的核心问题：**纪律（买卖点位）此前无人评估**。
进化大脑只优化"哪些票会涨"，但用户赚不赚钱 = 选股能力 × 执行纪律。

本模块把每份操作计划（[`trade_plan`](ai-quant-agent/backend/app/backtest/trade_plan.py:1) 产出）
变成可评估对象：

  ① save_plans()            每日榜单 → 写入 EKB `kb_plan` 台账（含纪律参数版本）
  ② replay_pending()        用**日线 high/low 触价回放**模拟"严格按计划执行"：
                            成交判定 / 止损 / 止盈 / 到期离场 + 含成本收益 + 基准对照（纪律 alpha）
  ③ analyze_failures()      **六类失效规则归因**（把亏钱原因翻译成"哪个参数错了"）
  ④ plan_stats() / build_plan_txt()   汇总与 L0 文本（供进化大脑）
  ⑤ backfill_from_cases()   用历史推荐案例（kb_case）重建计划并回放 → 立刻有历史纪律样本

口径与保守原则（plans/22 §五）：
  - 成交价：T+1 开盘 ≤ buy_high → 以开盘价成交；高开回落进入区间 → 以 buy_high 成交
  - **同日既触止损又触止盈 → 先判止损**（保守，避免回放结果虚高）
  - 跳空跌破止损 → 以开盘价离场（真实可成交价）
  - 收益含成本（`plan_cost_pct`，默认 0.25%）
"""
from __future__ import annotations

import json
import os
import time

from app.core.logger import logger
from app.kb import kb_store, kb_writer, kb_distiller
from app.backtest import trade_plan
from app.backtest.loader import load_daily

DATA_DIR = kb_store.DATA_DIR
FAILURE_TYPES = (
    "missed_gap_up",        # 高开超上限未追，且后续大涨（严重错失）
    "missed_fill",          # 高开超上限未成交（错过，后续未大涨）
    "chased_high",          # 追高被套
    "stop_whipsaw",         # 止损后反弹（假摔，止损太紧）
    "take_profit_early",    # 止盈过早（只吃一段）
    "expired_no_gain",      # 到期不达标
    "no_stop_loss",         # 未止损扩大亏损（执行纪律失效）
)


def _param(name: str, default):
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param(name, default)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def _cost_pct() -> float:
    try:
        return float(_param("plan_cost_pct", 0.0025) or 0.0025)
    except Exception:  # noqa: BLE001
        return 0.0025


def _params_version(form_type: str = "") -> str:
    """纪律参数版本指纹（便于回溯"这份计划是哪套参数生成的"）。

    若该形态采纳了纪律网格结论（`discipline_grid.plan_override`），指纹按**实际生效值**计算并加 `+grid` 标记，
    这样台账里能直接看出"这笔计划是参数表纪律还是网格纪律"→ 支持"改后为何没达预期"的追溯。
    """
    keys = ("plan_max_chase_pct", "plan_pullback_pct", "plan_stop_loss_pct",
            "plan_tp1_ratio", "plan_tp2_ratio", "plan_hold_days")
    vals = [str(_param(k, "")) for k in keys]
    tag = ""
    try:
        from app.backtest import discipline_grid
        ov = discipline_grid.plan_override(form_type) or {}
        if ov:
            vals[0] = str(ov.get("plan_max_chase_pct"))
            vals[2] = str(ov.get("plan_stop_loss_pct"))
            vals[3] = str(ov.get("plan_tp1_ratio"))
            vals[5] = str(ov.get("plan_hold_days"))
            tag = "+grid"
    except Exception:  # noqa: BLE001
        pass
    return f"plan@v{abs(hash('-'.join(vals))) % 100000:05d}{tag}"


# ── ① 计划落盘 ────────────────────────────────────────────────
def save_plans(date: str, report: dict) -> int:
    """把某日榜单的操作计划写入 kb_plan 台账（幂等）。返回写入条数。"""
    picks = (report or {}).get("top_picks") or []
    if not picks:
        return 0
    # 确保榜单已带计划（读接口通常已附加；此处兜底）
    if not any(p.get("entry_plan") for p in picks):
        try:
            report = trade_plan.attach_plans(report, cache_key=f"{date}_save")
            picks = report.get("top_picks") or picks
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[plan_tracker] 附加计划失败: {exc}")
    ver_cache: dict[str, str] = {}
    recs: list[dict] = []
    for p in picks:
        plan = p.get("entry_plan") or {}
        if not plan:
            continue
        form = str(p.get("form_type") or "")
        ver = ver_cache.setdefault(form, _params_version(form))
        recs.append({
            "id": f"plan:{str(date)[:8]}:{p.get('ts_code')}",
            "date": str(date)[:8],
            "ts_code": str(p.get("ts_code") or ""),
            "name": p.get("name") or "",
            "form_type": form,
            "params_version": ver,
            "plan": plan,
            "t0_close": plan.get("ref_price"),
            "execution": {},          # 回放后填充
            "baseline": {},
            "alpha": {},
            "failure_type": "",
            "env": {"regime": None},   # 待指数数据补新后回填
        })
    n = kb_store.upsert_many("plan", recs)
    kb_writer.flush_now()
    logger.info(f"[plan_tracker] 计划台账写入 {n} 条（{date}，{ver}）")
    return n


# ── ② 触价回放 ────────────────────────────────────────────────
def replay_one(rec: dict, df) -> dict | None:
    """对单条计划做触价回放（保守口径）。df 为 load_daily 的未复权日线。"""
    plan = rec.get("plan") or {}
    if not plan or df is None or df.empty:
        return None
    dates = df["trade_date"].dt.strftime("%Y%m%d").tolist()
    d0 = str(rec.get("date"))[:8]
    idx = next((i for i, d in enumerate(dates) if d >= d0), None)
    if idx is None or idx + 1 >= len(df):
        return None
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    n = len(df)

    buy_low, buy_high = float(plan.get("buy_low") or 0), float(plan.get("buy_high") or 0)
    stop = float(plan.get("stop_loss") or 0)
    tps = plan.get("take_profit") or []
    tp1 = float((tps[0] or {}).get("price") or 0) if tps else 0.0
    tp2 = float((tps[1] or {}).get("price") or 0) if len(tps) > 1 else 0.0
    hold = int(plan.get("hold_days") or 5)
    t0_close = float(rec.get("t0_close") or 0) or float(c[idx])

    out: dict = {"filled": False, "fill_price": None, "fill_date": None, "fill_type": "",
                 "exit_price": None, "exit_date": None, "exit_reason": "",
                 "plan_ret": None, "plan_ret_net": None, "hold_days_used": None,
                 "cost_pct": _cost_pct(), "tradeable": True}

    # 纪律影子实验喂数用（plans/23 §14.11）：D+1 高开幅度 + 按 D+1 开盘买入的 T+5（**毛收益**，
    # 与 param_sweep 的口径完全一致；成本对两组是同一常数，不影响两组比较）。
    # 纯附加字段，不改变既有回放语义。
    _nxt = idx + 1
    if _nxt < n and c[idx] > 0 and o[_nxt] > 0:
        out["t1_gap"] = round(float(o[_nxt] / c[idx] - 1.0), 5)
        out["t5_from_open"] = round(float(c[min(_nxt + 5, n - 1)] / o[_nxt] - 1.0), 5)

    # ── 成交判定（T+1 起，最多观察 3 日用回踩方式成交）──
    fill_idx = None
    for k in range(idx + 1, min(idx + 4, n)):
        if o[k] <= buy_high:
            out.update({"filled": True, "fill_price": float(o[k]),
                        "fill_date": dates[k], "fill_type": "open",
                        "tradeable": o[k] > 0})
            fill_idx = k
            break
        if lo[k] <= buy_high:      # 高开回落进入买入区间
            out.update({"filled": True, "fill_price": float(buy_high),
                        "fill_date": dates[k], "fill_type": "pullback_intraday"})
            fill_idx = k
            break
    if fill_idx is None:
        # 未成交：区分"错失大涨"与普通未成交（两者都是纪律代价，必须都统计）
        fwd = min(n - 1, idx + 5)
        ret = (c[fwd] / c[idx] - 1.0) if c[idx] > 0 else 0.0
        out["failure_type"] = "missed_gap_up" if ret > 0.05 else "missed_fill"
        out["missed_ret"] = round(ret, 4)
        out["exit_reason"] = "no_fill"       # 标记已回放（统计需含未成交）
        return out

    fill = float(out["fill_price"])
    # 日内回踩成交（pullback_intraday）：当日最低价可能出现在**我们的限价单成交之前**，
    # 用它判当天止损属前视偏差 → 该情形从次日起判离场（口径修正，见 plans/22）。
    start_k = fill_idx if out.get("fill_type") == "open" else fill_idx + 1
    # ── 逐日离场判定（当日先止损，后止盈 → 保守）──
    exit_idx = None
    for k in range(start_k, min(fill_idx + hold + 1, n)):
        if stop and lo[k] <= stop:
            px = min(float(o[k]), stop) if o[k] > 0 else stop   # 跳空跌破按开盘价
            out.update({"exit_price": px, "exit_date": dates[k], "exit_reason": "stop_loss"})
            exit_idx = k
            break
        if tp1 and h[k] >= tp1:
            out.update({"exit_price": tp1, "exit_date": dates[k], "exit_reason": "tp1"})
            exit_idx = k
            break
    if exit_idx is None:
        last = max(min(fill_idx + hold, n - 1), min(start_k, n - 1))
        out.update({"exit_price": float(c[last]), "exit_date": dates[last],
                    "exit_reason": "expire"})
        exit_idx = last

    hold_used = max(0, exit_idx - fill_idx)
    gross = (float(out["exit_price"]) / fill - 1.0) if fill > 0 else 0.0
    out["plan_ret"] = round(gross, 4)
    out["plan_ret_net"] = round(gross - _cost_pct(), 4)
    out["hold_days_used"] = hold_used

    # ── 基准（无纪律对照）：T+1 收盘买入，持有到 T+5 / T+20 ──
    base_idx = fill_idx
    if base_idx + 1 < n:
        b_ret_t5 = (c[min(base_idx + 5, n - 1)] / c[base_idx] - 1.0) if c[base_idx] > 0 else 0.0
        b_ret_t20 = (c[min(base_idx + 20, n - 1)] / c[base_idx] - 1.0) if c[base_idx] > 0 else 0.0
    else:
        b_ret_t5 = b_ret_t20 = 0.0
    out["baseline"] = {"buy_hold_t5_ret": round(b_ret_t5 - _cost_pct(), 4),
                       "buy_hold_t20_ret": round(b_ret_t20 - _cost_pct(), 4)}
    out["alpha"] = {"vs_hold_t5": round(out["plan_ret_net"] - out["baseline"]["buy_hold_t5_ret"], 4),
                    "vs_hold_t20": round(out["plan_ret_net"] - out["baseline"]["buy_hold_t20_ret"], 4)}

    # ── 六类失效归因（规则，零 token）──
    ftype = ""
    if out["exit_reason"] == "stop_loss":
        # 止损后是否快速反弹（假摔）：止损后 5 日内最高价重新站上 tp1/买入上限
        look_end = min(n, exit_idx + 6)
        rebound = max(h[exit_idx:look_end]) if look_end > exit_idx else 0.0
        if rebound >= (tp1 or buy_high):
            ftype = "stop_whipsaw"
        else:
            ftype = "chased_high" if fill > t0_close * (1 + float(_param("plan_max_chase_pct", 0.03))) else ""
    elif out["exit_reason"] == "tp1":
        # 离场后是否继续大涨（止盈过早）
        look_end = min(n, exit_idx + 11)
        peak = max(h[exit_idx:look_end]) if look_end > exit_idx else 0.0
        if peak >= (tp2 or tp1 * 1.15):
            ftype = "take_profit_early"
    elif out["exit_reason"] == "expire":
        if out["plan_ret_net"] is not None and out["plan_ret_net"] < 0:
            ftype = "expired_no_gain"
            seg = lo[start_k:exit_idx + 1] if exit_idx + 1 > start_k else lo[start_k:start_k + 1]
            if stop and seg.size and seg.min() < stop * 0.97:
                ftype = "no_stop_loss"       # 期间已明显跌破止损仍未离场
    out["failure_type"] = ftype
    return out


def replay_pending(limit: int = 200, force: bool = False) -> dict:
    """回放"未回放"的计划（force=True 时全部重放，用于口径/规则变更后重算）。"""
    kb_writer.flush_now()
    rows = kb_store.query("plan", limit=2000) if force else [
        p for p in kb_store.query("plan", limit=2000)
        if not (p.get("execution") or {}).get("exit_reason")]
    if not rows:
        return {"pending": 0, "replayed": 0}
    rows.sort(key=lambda r: str(r.get("date") or ""))
    done = skipped = 0
    for rec in rows[:limit]:
        try:
            # 只取 T0 及之后的日线（回放只需 T0+20 日），避免每只股票全量拉数
            df = load_daily(str(rec.get("ts_code")), start_date=str(rec.get("date"))[:8])
            res = replay_one(rec, df)
            if not res:
                skipped += 1
                continue
            # 纪律影子实验喂数：若 plan_max_chase_pct 正在纪律影子期，记录一个买点观察
            # （没有影子在跑时是 no-op；有跑时每条回放贡献一个样本）
            if res.get("t1_gap") is not None and res.get("t5_from_open") is not None:
                try:
                    from app.agents import evolution_shadow as _shadow
                    _shadow.record_buy_verdict(
                        "plan_max_chase_pct", str(rec.get("ts_code") or ""),
                        str(rec.get("date") or "")[:8], res["t1_gap"], res["t5_from_open"])
                except Exception:  # noqa: BLE001
                    pass
            kb_store.update_fields("plan", str(rec.get("id")), {
                "execution": res, "baseline": res.get("baseline") or {},
                "alpha": res.get("alpha") or {}, "failure_type": res.get("failure_type") or "",
            })
            done += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[plan_tracker] 回放失败 {rec.get('id')}: {exc}")
            skipped += 1
    kb_writer.flush_now()
    logger.info(f"[plan_tracker] 触价回放完成: {done} 条（跳过 {skipped}）")
    return {"pending": len(rows), "replayed": done, "skipped": skipped}


def backfill_from_cases(limit: int = 600) -> dict:
    """用历史推荐案例（kb_case）重建计划并回放 → 立刻获得历史纪律样本。

    说明：用**当前纪律参数**重建历史计划，属"参数评估"常规做法（存在轻微前视），
    故 params_version 标注 `backfill@<date>`，统计时可单独过滤。
    """
    kb_writer.flush_now()
    cases = kb_store.query("case", limit=6000)[:limit]
    if not cases:
        return {"cases": 0, "saved": 0}
    ver = "backfill@" + time.strftime("%Y%m%d")
    # 关键：T0 基准价必须取**该案例推荐日**的收盘价（此前误用"今天"的价格，价位全部锚错日期）
    by_date: dict[str, list[str]] = {}
    for c in cases:
        d = str(c.get("date"))[:8]
        if d and c.get("ts_code"):
            by_date.setdefault(d, []).append(str(c.get("ts_code")))
    prices: dict[tuple, dict] = {}
    for d, codes in by_date.items():
        try:
            for code, val in (trade_plan.load_t0_prices(codes, d) or {}).items():
                prices[(d, code)] = val
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[plan_tracker] 取 {d} 基准价失败: {exc}")
    recs: list[dict] = []
    for c in cases:
        code = str(c.get("ts_code"))
        t0 = prices.get((str(c.get("date"))[:8], code)) or {}
        plan = trade_plan.build_plan({"ts_code": code, "form_type": c.get("form_type")}, t0)
        if not plan:
            continue
        recs.append({
            "id": f"plan:{str(c.get('date'))[:8]}:{code}",
            "date": str(c.get("date"))[:8], "ts_code": code, "name": c.get("name") or "",
            "form_type": str(c.get("form_type") or ""), "params_version": ver,
            "plan": plan, "t0_close": plan.get("ref_price"),
            "execution": {}, "baseline": {}, "alpha": {},
            "failure_type": "", "env": {"regime": (c.get("env") or {}).get("regime")},
        })
    saved = kb_store.upsert_many("plan", recs)
    kb_writer.flush_now()
    rep = replay_pending(limit=max(200, saved + 50))
    return {"cases": len(cases), "saved": saved, "replay": rep}


# ── ③ 统计与 L0 文本 ─────────────────────────────────────────
def plan_stats(days: int = 60) -> dict:
    """纪律统计：含成本收益、纪律 alpha、六类失效占比、成交率（供 L0/API/前端）。"""
    kb_writer.flush_now()
    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - max(1, days) * 86400))
    rows = [p for p in kb_store.query("plan", limit=3000)
            if str(p.get("date") or "")[:8] >= cutoff]
    # 已回放 = 有 execution.exit_reason（含 no_fill 未成交 —— 未成交本身就是纪律结果，必须统计）
    replayed = [p for p in rows if (p.get("execution") or {}).get("exit_reason")]
    if not rows:
        return {"plans": 0, "replayed": 0, "note": "暂无计划台账（首次运行后积累）"}
    filled = [p for p in replayed if (p.get("execution") or {}).get("filled")]
    traded = [p for p in filled]     # 仅"已成交"用于收益口径（未成交没有收益）
    alphas = [float((p.get("alpha") or {}).get("vs_hold_t5") or 0) for p in traded]
    nets = [float((p.get("execution") or {}).get("plan_ret_net") or 0) for p in traded]
    wins = sum(1 for v in nets if v > 0)
    ft: dict = {}
    for p in replayed:
        k = str(p.get("failure_type") or "")
        if k:
            ft[k] = ft.get(k, 0) + 1
    by_form: dict = {}
    for p in traded:
        f = str(p.get("form_type") or "?")
        d = by_form.setdefault(f, {"n": 0, "sum_net": 0.0, "sum_alpha": 0.0, "wins": 0})
        d["n"] += 1
        d["sum_net"] += float((p.get("execution") or {}).get("plan_ret_net") or 0)
        d["sum_alpha"] += float((p.get("alpha") or {}).get("vs_hold_t5") or 0)
        d["wins"] += 1 if float((p.get("execution") or {}).get("plan_ret_net") or 0) > 0 else 0
    return {
        "window_days": days, "plans": len(rows), "replayed": len(replayed),
        "traded": len(traded),
        "filled_rate": round(len(filled) / max(1, len(replayed)), 4),
        "missed_rate": round((len(replayed) - len(filled)) / max(1, len(replayed)), 4),
        "avg_plan_ret_net": round(sum(nets) / max(1, len(nets)), 4),
        "plan_win_rate": round(wins / max(1, len(nets)), 4),
        "avg_alpha_vs_hold_t5": round(sum(alphas) / max(1, len(alphas)), 4),
        "alpha_positive_rate": round(sum(1 for v in alphas if v > 0) / max(1, len(alphas)), 4),
        "failure_type": ft,
        "failure_type_pct": {k: round(v / max(1, len(replayed)), 4) for k, v in ft.items()},
        "by_form": {k: {"n": v["n"],
                        "avg_plan_ret_net": round(v["sum_net"] / max(1, v["n"]), 4),
                        "avg_alpha": round(v["sum_alpha"] / max(1, v["n"]), 4),
                        "win_rate": round(v["wins"] / max(1, v["n"]), 4)}
                    for k, v in by_form.items()},
        "params_versions": sorted({str(p.get("params_version") or "") for p in replayed})[:5],
    }


_CN = {
    "missed_gap_up": "高开未追且错失大涨",
    "missed_fill": "高开超上限未成交",
    "chased_high": "追高被套",
    "stop_whipsaw": "止损后反弹（假摔）",
    "take_profit_early": "止盈过早（只吃一段）",
    "expired_no_gain": "到期不达标",
    "no_stop_loss": "未止损扩大亏损",
}


def build_plan_txt(days: int = 60) -> str:
    """纪律结论文本（供 L0/对话）：纪律 alpha + 六类失效 TOP。"""
    s = plan_stats(days)
    if not s.get("replayed"):
        return ""
    _missed = float(s.get("missed_rate", 0) or 0)
    # 仅当未成交比例确实显著时才给出"高开买不到"的解释（修正基准价后通常 <1%，不应误导）
    _missed_note = "（未成交主因：高开超追高上限买不到）" if _missed >= 0.05 else ""
    lines = [
        f"交易纪律({s['window_days']}日/{s['replayed']}单): 成交率 {s['filled_rate']*100:.0f}%"
        f"（未成交 {_missed*100:.1f}%）{_missed_note}；"
        f"已成交 {s.get('traded', 0)} 单计划收益均值 {s['avg_plan_ret_net']*100:+.2f}%"
        f"（胜率 {s['plan_win_rate']*100:.0f}%），纪律alpha(vs 无脑持有T+5) {s['avg_alpha_vs_hold_t5']*100:+.2f}%"
        f"（{s['alpha_positive_rate']*100:.0f}% 的单子跑赢基准）"
    ]
    ft = s.get("failure_type_pct") or {}
    if ft:
        top = sorted(ft.items(), key=lambda kv: -kv[1])[:3]
        lines.append("纪律失效TOP: " + " | ".join(f"{_CN.get(k, k)}({v*100:.0f}%)" for k, v in top))
    return "\n".join(lines)


def distill_lessons(days: int = 60, min_pct: float = 0.05, min_n: int = 30) -> int:
    """把高频纪律失效沉淀为教训（plans/22 D13）：进 L0 与检索，重复错误被否证清单拦。"""
    s = plan_stats(days)
    ft = s.get("failure_type") or {}
    n_total = max(1, int(s.get("replayed") or 0))
    if n_total < min_n:
        return 0
    hint = {
        "stop_whipsaw": "放宽 plan_stop_loss_pct（或改用形态关键位止损），减少被洗出",
        "take_profit_early": "提高 plan_tp1_ratio 或启用跟踪止盈（plan_trail_mode）",
        "missed_gap_up": "放宽 plan_max_chase_pct（错失强势票的代价大于追高）",
        "missed_fill": "追高上限过紧导致大量买不到 → 放宽 plan_max_chase_pct 或改为回踩低吸为主",
        "chased_high": "收紧 plan_max_chase_pct，减少高开接盘",
        "expired_no_gain": "缩短 plan_hold_days 或提高选股质量（信号侧问题）",
        "no_stop_loss": "检查止损执行与滑点，必要时收紧 plan_stop_loss_pct",
    }
    touched = 0
    for k, v in ft.items():
        if k not in FAILURE_TYPES or v / n_total < min_pct:
            continue
        lid = kb_distiller.lesson_id("discipline", k)
        existing = kb_store.get("lesson", lid) or {}
        support = dict(existing.get("support") or {})
        support.update({"cases_n": v, "total_n": n_total, "last_date": time.strftime("%Y%m%d"),
                        "rate": round(v / n_total, 4)})
        conf = kb_distiller.confidence_for(v)
        ok = kb_store.upsert("lesson", {
            "id": lid, "ts": existing.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "text": f"交易纪律失效：{_CN.get(k, k)}（{v}/{n_total} 单，{v/n_total*100:.0f}%）",
            "type": "discipline",
            "scope": {"side": "failure", "source": "plan_tracker", "failure_type": k},
            "actionable": {"hint": hint.get(k, ""), "param_hint": hint.get(k, "")},
            "support": support, "confidence": conf, "half_life_days": 90.0,
            "applied_experiments": existing.get("applied_experiments") or [],
            "effect_verdict": existing.get("effect_verdict"),
            "status": ("active" if v >= 30 else "candidate"),
            "last_verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        touched += 1 if ok else 0
    if touched:
        logger.info(f"[plan_tracker] 纪律教训沉淀 {touched} 条")
    return touched


__all__ = ["save_plans", "replay_pending", "replay_one", "backfill_from_cases",
           "plan_stats", "build_plan_txt", "distill_lessons", "FAILURE_TYPES"]
