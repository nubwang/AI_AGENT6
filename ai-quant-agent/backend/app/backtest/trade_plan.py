"""价格化入场/离场计划（trade_plan）

用户诉求：入场建议要**标明具体价格**（什么价买入、什么价卖出），而不是只给
"T+1 开盘买入"这类定性描述。

本模块在**读取榜单时**按需生成每只标的的操作价位（不改扫描/回测主流程）：

  买入：T+1 开盘限价买入区间（T0 收盘 × (1±追高上限)），高开超过上限**不追**；
        另给"回踩低吸"区间（无后视偏差策略 pullback_t0：回踩启动价时低吸）
  止损：T0 收盘 × (1 − 止损幅度)，规则"收盘跌破则次日离场"
  止盈：T+5 目标（形态历史平均 T+5 收益 × 折扣）/ T+20 主升浪目标（× 折扣），
        折扣用于避免过度承诺；同时给"时间止盈/跟踪止盈"规则
  持有：建议持有交易日（默认 5），到期未达目标按收盘离场

设计要点
  - **价格用未复权真实价**（用户对照行情软件即可执行）；比例与复权无关；
  - 全部阈值来自 `evolution_config` 参数表 → **进化大脑可优化**（是否追高、止损多深、
    目标多高，直接关系"推荐的股票是否真的能赚到"）；
  - 形态依据来自回测产物 `entry_guidance.json`（样本不足时退化为保守默认值并标注）。
"""
from __future__ import annotations

import json
import os
import re
import time

from app.core.logger import logger
from app.models import SessionLocal

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
GUIDANCE_FILE = os.path.join(DATA_DIR, "entry_guidance.json")

# 默认参数（可被 evolution_config 覆盖 → 可进化）
DEFAULTS = {
    "plan_max_chase_pct": 0.03,     # 追高上限：T+1 开盘高于 T0×(1+此值) 则放弃
    "plan_pullback_pct": 0.03,      # 回踩低吸幅度
    "plan_stop_loss_pct": 0.05,     # 止损幅度
    "plan_tp1_ratio": 0.60,         # T+5 目标折扣（对形态历史均值打折）
    "plan_tp2_ratio": 0.40,         # T+20 目标折扣
    "plan_hold_days": 5,            # 建议持有交易日
}
# 形态统计缺失时的保守默认目标（避免给出夸张价位）
FALLBACK_TP1 = 0.08
FALLBACK_TP2 = 0.15

_CACHE: dict = {"key": "", "ts": 0.0, "data": None}
_CACHE_TTL = 300.0     # 榜单计划缓存（秒）


def _param(name: str, default):
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param(name, default)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def _pct(name: str) -> float:
    try:
        return float(_param(name, DEFAULTS[name]))
    except Exception:  # noqa: BLE001
        return float(DEFAULTS[name])


def _load_guidance() -> dict:
    try:
        with open(GUIDANCE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def load_t0_prices(ts_codes: list[str], date_str: str) -> dict[str, dict]:
    """批量取 T0（推荐日）未复权收盘价与前收（用户可对照行情软件的真实价）。

    Returns: {ts_code: {"close": float, "pre_close": float, "trade_date": str}}
    """
    out: dict[str, dict] = {}
    # 白名单过滤（防 SQL 注入：代码只允许 数字/字母/点）
    codes = [c for c in (ts_codes or []) if c and re.fullmatch(r"[0-9A-Za-z.]+", str(c))]
    if not codes:
        return out
    day = re.sub(r"[^0-9]", "", str(date_str))[:8] or "99999999"
    try:
        import pandas as pd
        from sqlalchemy import text
        db = SessionLocal()
        try:
            in_list = ",".join(f"'{c}'" for c in codes)
            sql = (
                f"SELECT ts_code, trade_date, close, pre_close FROM daily "
                f"WHERE ts_code IN ({in_list}) AND trade_date <= '{day}' "
                f"ORDER BY ts_code, trade_date DESC"
            )
            df = pd.read_sql(text(sql), db.connection())
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[trade_plan] 取 T0 价格失败: {exc}")
        return out
    if df is None or df.empty:
        return out
    for _, r in df.iterrows():
        code = str(r["ts_code"])
        if code in out:
            continue                       # 已取到该股最新的 <= T0 交易日
        try:
            close = float(r["close"] or 0)
            pre = float(r["pre_close"] or 0)
        except Exception:  # noqa: BLE001
            continue
        if close <= 0:
            continue
        out[code] = {"close": close, "pre_close": pre,
                     "trade_date": str(r["trade_date"])[:8]}
    return out


def load_latest_prices(ts_codes: list[str]) -> dict[str, dict]:
    """批量取**现价**：每只股票在日线表里最新已采集交易日的收盘价（未复权，可直接对照行情软件）。

    说明：本系统无实时行情源（日线仅采集至最近收盘），故"现价"= 最新交易日收盘价，
    返回值带 `trade_date` 以便前端明确标注日期，避免误认为是盘中实时价。

    Returns: {ts_code: {"close": float, "pre_close": float, "pct_chg": float, "trade_date": str}}
    """
    out: dict[str, dict] = {}
    codes = [c for c in (ts_codes or []) if c and re.fullmatch(r"[0-9A-Za-z.]+", str(c))]
    if not codes:
        return out
    try:
        import pandas as pd
        from sqlalchemy import text
        db = SessionLocal()
        try:
            in_list = ",".join(f"'{c}'" for c in codes)
            sql = (
                "SELECT d.ts_code, d.trade_date, d.close, d.pre_close, d.pct_chg FROM daily d "
                f"JOIN (SELECT ts_code, MAX(trade_date) AS md FROM daily WHERE ts_code IN ({in_list}) "
                "GROUP BY ts_code) m ON d.ts_code = m.ts_code AND d.trade_date = m.md"
            )
            df = pd.read_sql(text(sql), db.connection())
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[trade_plan] 取现价失败: {exc}")
        return out
    if df is None or df.empty:
        return out
    for _, r in df.iterrows():
        try:
            close = float(r["close"] or 0)
        except Exception:  # noqa: BLE001
            continue
        if close <= 0:
            continue
        try:
            pct = round(float(r["pct_chg"] or 0), 2)
        except Exception:  # noqa: BLE001
            pct = None
        out[str(r["ts_code"])] = {
            "close": _r2(close),
            "pre_close": _r2(float(r["pre_close"] or 0)),
            "pct_chg": pct,
            "trade_date": re.sub(r"[^0-9]", "", str(r["trade_date"]))[:8],
        }
    return out


def _r2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


def build_plan(pick: dict, t0: dict, guidance: dict | None = None) -> dict:
    """生成单只标的的价格化操作计划。t0 为 load_t0_prices 的结果项。

    纪律参数来源优先级（plans/22 P1）：
      ① 纪律网格（[`discipline_grid.plan_override`](ai-quant-agent/backend/app/backtest/discipline_grid.py:1)）
         —— 仅当"反事实回放证明相对当前参数有 >阈值 的改进且样本外稳健"时才覆盖；
      ② 参数表（`evolution_config`，进化大脑可热调）；
      ③ 代码内 DEFAULTS（兜底）。
    止损始终被红线 `plan_max_stop_loss_pct`（默认 10%）截断，防纪律被进化成扛单不止损。
    """
    p0 = float((t0 or {}).get("close") or 0)
    if p0 <= 0:
        return {}
    form = str(pick.get("form_type") or "")
    g = (guidance if guidance is not None else _load_guidance()).get("by_form", {}).get(form) or {}

    chase = _pct("plan_max_chase_pct")
    pull = _pct("plan_pullback_pct")
    stop = _pct("plan_stop_loss_pct")
    tp1_ratio = _pct("plan_tp1_ratio")
    tp2_ratio = _pct("plan_tp2_ratio")
    hold = int(_param("plan_hold_days", DEFAULTS["plan_hold_days"]) or 5)

    # ── 纪律网格覆盖（证据驱动；未达采纳门槛则保持参数表值，并记录"看过未采纳"）──
    grid_src = "params"
    grid_meta: dict = {}
    try:
        from app.backtest import discipline_grid
        if discipline_grid.grid_enabled():
            ov = discipline_grid.plan_override(form) or {}
            if ov:
                chase = float(ov.get("plan_max_chase_pct") or chase)
                stop = float(ov.get("plan_stop_loss_pct") or stop)
                tp1_ratio = float(ov.get("plan_tp1_ratio") or tp1_ratio)
                hold = int(ov.get("plan_hold_days") or hold)
                grid_src = "discipline_grid"
                grid_meta = dict(ov.get("_grid") or {})
            else:
                b = discipline_grid.best_params(form)
                if b:
                    grid_meta = {"samples": b.get("samples"), "improvement": b.get("improvement"),
                                 "oos_pass": b.get("oos_pass"),
                                 "reason": "网格结论未达采纳门槛（改进不足/样本外不稳健）→ 保持当前参数"}
    except Exception:  # noqa: BLE001
        grid_src, grid_meta = "params", {}
    # 红线：止损不得超过 plan_max_stop_loss_pct（默认 10%）
    try:
        stop = min(float(stop), float(_param("plan_max_stop_loss_pct", 0.10) or 0.10))
    except Exception:  # noqa: BLE001
        pass

    avg_t5 = float(g.get("avg_ret_t5") or 0) or FALLBACK_TP1
    avg_t20 = float(g.get("avg_ret_t20") or 0) or FALLBACK_TP2
    tp1 = max(0.03, avg_t5 * tp1_ratio)       # 目标不低于 3%（否则形同不设目标）
    tp2 = max(tp1 + 0.03, avg_t20 * tp2_ratio)
    basis_src = "回测形态统计" if g else "默认保守值（该形态样本不足）"

    buy_low = _r2(p0 * (1 - min(chase, 0.02)))
    buy_high = _r2(p0 * (1 + chase))
    pull_low = _r2(p0 * (1 - pull - 0.01))
    pull_high = _r2(p0 * (1 - pull + 0.01))
    stop_px = _r2(p0 * (1 - stop))
    tp1_px = _r2(p0 * (1 + tp1))
    tp2_px = _r2(p0 * (1 + tp2))

    text = (
        f"买入 {buy_low}~{buy_high} 元（T+1 开盘，高开 >{chase * 100:.0f}% 不追）；"
        f"回踩低吸 {pull_low}~{pull_high} 元；"
        f"止损 {stop_px} 元（-{stop * 100:.0f}%，收盘跌破次日离场）；"
        f"目标 {tp1_px} 元（T+{hold}）/ {tp2_px} 元（T+20 主升浪）；"
        f"持有 {hold} 个交易日，跌破 5 日均线或自最高回落 3% 提前离场"
    )
    return {
        "ref_date": (t0 or {}).get("trade_date", ""),
        "ref_price": _r2(p0),                      # T0 收盘（未复权）→ 所有价位的基准
        "buy_low": buy_low, "buy_high": buy_high,
        "buy_rule": f"T+1 开盘价 ≤ {buy_high} 元买入；高开超过 {chase * 100:.0f}% 放弃（不追高）",
        "pullback_low": pull_low, "pullback_high": pull_high,
        "pullback_rule": f"{pull * 100:.0f}% 回踩至 {pull_low}~{pull_high} 元低吸（未回踩则用买入区间）",
        "stop_loss": stop_px,
        "stop_rule": f"收盘跌破 {stop_px} 元（-{stop * 100:.0f}%）→ 次日离场",
        "take_profit": [
            {"price": tp1_px, "rule": f"持有到 T+{hold} 收盘价 ≥ {tp1_px} 元止盈",
             "gain_pct": round(tp1 * 100, 2)},
            {"price": tp2_px, "rule": f"主升浪目标：T+20 内 ≥ {tp2_px} 元（分批止盈）",
             "gain_pct": round(tp2 * 100, 2)},
        ],
        "hold_days": hold,
        "exit_rule": f"T+{hold} 到期未达目标 → 按收盘离场；期间跌破 5 日均线或自最高回落 3% 提前离场",
        "basis": {
            "form_type": form,
            "source": basis_src,
            "win_rate_t5": g.get("win_rate_t5"),
            "avg_ret_t5": g.get("avg_ret_t5"),
            "avg_ret_t20": g.get("avg_ret_t20"),
            "samples": g.get("count"),
            "tp1_ratio": tp1_ratio, "tp2_ratio": tp2_ratio,
            "discipline_src": grid_src,          # params / discipline_grid（是否采纳网格结论）
            "grid": grid_meta,
        },
        "text": text,
        "disclaimer": "价位由形态历史统计与可调参数生成，非投资建议；请结合盘面与自身风险承受力执行",
    }


def attach_plans(report: dict, cache_key: str = "") -> dict:
    """给榜单 top_picks 每条附加 entry_plan / entry_plan_text（带缓存，失败静默）。"""
    if not isinstance(report, dict) or not report.get("top_picks"):
        return report
    key = cache_key or f"{report.get('date', '')}_{len(report.get('top_picks') or [])}"
    now = time.time()
    if _CACHE["key"] == key and (now - _CACHE["ts"]) < _CACHE_TTL and _CACHE["data"]:
        cached = _CACHE["data"] or {}
        plans = cached.get("plans") or {}
        quotes = cached.get("quotes") or {}
    else:
        try:
            picks = report.get("top_picks") or []
            codes = [str(p.get("ts_code") or "") for p in picks]
            prices = load_t0_prices(codes, str(report.get("date") or ""))
            guidance = _load_guidance()
            plans = {str(p.get("ts_code")): build_plan(p, prices.get(str(p.get("ts_code")), {}), guidance)
                     for p in picks}
            try:
                # 现价：最新已采集交易日收盘价（与计划价位同源未复权，便于用户对照）
                quotes = load_latest_prices(codes)
            except Exception:  # noqa: BLE001
                quotes = {}
            _CACHE.update({"key": key, "ts": now, "data": {"plans": plans, "quotes": quotes}})
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[trade_plan] 生成操作计划失败（不影响榜单）: {exc}")
            return report
    for p in report.get("top_picks") or []:
        code = str(p.get("ts_code"))
        plan = plans.get(code) or {}
        if plan:
            p["entry_plan"] = plan
            p["entry_plan_text"] = plan.get("text", "")
        q = quotes.get(code) or {}
        if q:
            px = float(q.get("close") or 0)
            p["price_now"] = px
            p["price_now_pct"] = q.get("pct_chg")
            p["price_now_date"] = q.get("trade_date")
            ref = float(plan.get("ref_price") or 0) if plan else 0.0
            if ref > 0 and px > 0:
                p["price_now_vs_ref"] = round(px / ref - 1.0, 4)
            # 现价相对计划价位的位置（帮用户判断"现在还能不能按计划买"）
            if plan and px > 0:
                bh, sl = float(plan.get("buy_high") or 0), float(plan.get("stop_loss") or 0)
                if sl and px <= sl:
                    p["price_now_tag"], p["price_now_tag_type"] = "已破止损", "danger"
                elif bh and px > bh:
                    p["price_now_tag"], p["price_now_tag_type"] = "高于买入上限", "warning"
                else:
                    p["price_now_tag"], p["price_now_tag_type"] = "在买入区间", "success"
    return report


__all__ = ["build_plan", "attach_plans", "load_t0_prices", "load_latest_prices", "DEFAULTS"]
