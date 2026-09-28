"""持续监控与滚动再验证（monitor）

对齐规划：plans/04-回测引擎.md 5.6 与第十四章第 18 项
  目标：防止"上线后失效"。回测通过只是起点，上线后必须持续监控：

    1. 记录回测基线（预测命中率 = 条件概率表 baseline_prob，附 A/B 决策门结果）
    2. 记录每日推荐实际命中（T+5 涨幅 >5% 是否兑现，按推荐日期累计）
    3. 月度漂移检测：实际命中率 vs 回测基线，二项比例 z 检验（纯 math，无 scipy）
       连续 2 个月显著低于基线 → 标记"需重新验证"（告警）
    4. 模式失效检测：按月统计各形态命中率，显著退化 → 降权/剔除建议
    5. 月度滚动再验证：用最新数据窗口重跑 A/B 对比，输出保持/切换建议

  状态持久化：backend/data/monitor_state.json（低频小数据，无需建表）
"""
from __future__ import annotations

import json
import math
import os
import time

from app.core.logger import logger
# 注意：evolution_config 改为**函数内延迟导入**（修循环导入缺陷）。
# 原顶层导入会形成：backtest.monitor → agents.__init__ → news_agent → learning_runner
# → reflect_agent → backtest.monitor（半初始化）→ ImportError，
# 使得任何"先导入 app.backtest.monitor"的入口（脚本/单测/新调用方）都会直接崩。

# 状态文件：backend/data/monitor_state.json
STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "monitor_state.json",
)
HIT_RET_THRESHOLD = 0.05    # 命中口径：T+5 涨幅 >5%；可进化（evolution_config: hit_threshold）
DECAY_MIN_SAMPLE = 20       # 模式失效检测最小样本
SIG_ALPHA = 0.05            # 显著性水平（单侧）
CONSECUTIVE_MONTHS = 2      # 连续 N 个月显著低于 → 告警


def _norm_cdf(x: float) -> float:
    """标准正态 CDF（用 math.erf，避免 scipy 依赖）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _prop_ztest(p_hat: float, p0: float, n: int) -> tuple[float, float]:
    """二项比例单侧 z 检验（H1: 实际命中率 p_hat < 回测基线 p0）。

    Returns:
        (z, p_value)：p_value = P(Z < z)，越小说明实际越显著低于基线
    """
    if n <= 0:
        return 0.0, 1.0
    se = math.sqrt(p0 * (1.0 - p0) / n)
    if se <= 1e-12:
        return 0.0, 1.0
    z = (p_hat - p0) / se
    return z, _norm_cdf(z)


def _load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:  # noqa: BLE001
        pass
    return {"baseline": None, "hits": [], "monitor": None}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"监控状态保存失败: {exc}")


def record_baseline(result_dict: dict) -> dict:
    """回测完成后记录基线（预测命中率 + A/B 决策门）。"""
    prob_table = result_dict.get("probability_table") or {}
    ab = result_dict.get("ab_validation") or {}
    predicted = float(prob_table.get("baseline_prob") or 0.0)
    state = _load_state()
    state["baseline"] = {
        "updated_at": int(time.time()),
        "start_date": (result_dict.get("config") or {}).get("start_date", ""),
        "predicted_hit_rate": round(predicted, 4),
        "sample_count": int(prob_table.get("sample_count") or 0),
        "ab_validation": ab,
        "go": ab.get("go"),
    }
    _save_state(state)
    logger.info(f"监控基线已更新: predicted_hit_rate={predicted:.3f}, go={ab.get('go')}")
    return state["baseline"]


def record_hits(records: list[dict]) -> int:
    """追加每日推荐的实际命中记录（按 date+ts_code 去重）。

    Args:
        records: [{"date":"YYYYMMDD","ts_code":"...","form_type":"A",
                   "pred_prob":0.4,"t5_ret":0.07}]
        t5_ret 为空/None 表示尚未到 T+5（先记录，不计入月度聚合）

    Returns:
        新增记录数
    """
    state = _load_state()
    seen = {(h.get("date", ""), h.get("ts_code", "")) for h in state["hits"]}
    added = 0
    for r in records:
        key = (str(r.get("date", "")), str(r.get("ts_code", "")))
        if key in seen:
            continue
        t5 = r.get("t5_ret")
        state["hits"].append({
            "date": str(r.get("date", "")),
            "ts_code": str(r.get("ts_code", "")),
            "form_type": str(r.get("form_type", "")),
            "pred_prob": float(r.get("pred_prob", 0.0) or 0.0),
            "t5_ret": t5,
            "source": str(r.get("source", "rule")),   # rule=规则版 / agent=Agent 最终榜单
            "hit": bool(t5 is not None and float(t5) > _hit_threshold()),
        })
        seen.add(key)
        added += 1
    if len(state["hits"]) > 5000:
        state["hits"] = state["hits"][-5000:]
    _save_state(state)
    logger.info(f"监控命中记录新增 {added} 条（累计 {len(state['hits'])}）")
    return added


def record_agent_hits(rec_date: str, records: list[dict]) -> int:
    """用 Agent 精筛最终榜单登记命中（反馈对齐优化）。

    用户实际看到/依赖的是 Agent 最终榜单（daily_YYYYMMDD_agent_vN.json），而
    daily_verify / 进化反馈此前基于规则版榜单统计命中，导致进化目标与真实推荐脱节。
    此函数以 Agent 最终榜单为准覆盖同日期记录（Agent 榜单是规则版候选的精筛子集+重排序），
    使次日验证/进化反馈对齐"用户真正看到的最终推荐"。

    Args:
        rec_date: 榜单日期 YYYYMMDD（Agent 榜单的数据日期）
        records: [{"ts_code","form_type","up_probability",...}] Agent 最终 top_picks

    Returns:
        登记条数
    """
    state = _load_state()
    # 移除该日期旧记录（规则版 / 旧 Agent 版），以本次 Agent 最终榜单为准
    state["hits"] = [h for h in state["hits"] if str(h.get("date", "")) != rec_date]
    seen: set[str] = set()
    added = 0
    for r in records:
        code = str(r.get("ts_code", ""))
        if not code or code in seen:
            continue
        seen.add(code)
        state["hits"].append({
            "date": rec_date,
            "ts_code": code,
            "form_type": str(r.get("form_type", "")),
            "pred_prob": float(r.get("up_probability", r.get("pred_prob", 0.0)) or 0.0),
            "t5_ret": None,
            "source": "agent",
            "hit": False,
        })
        added += 1
    if len(state["hits"]) > 5000:
        state["hits"] = state["hits"][-5000:]
    _save_state(state)
    logger.info(f"[monitor] Agent 最终榜单命中登记 {rec_date}: {added} 条（覆盖规则版，反馈对齐）")
    return added


def _hit_threshold() -> float:
    """命中阈值（可进化 F2/E4）：读 evolution_config.hit_threshold，缺省 HIT_RET_THRESHOLD。"""
    try:
        from app.agents import evolution_config  # 延迟导入（防循环导入，见文件头说明）
        return float(evolution_config.get_param("hit_threshold", HIT_RET_THRESHOLD) or HIT_RET_THRESHOLD)
    except Exception:  # noqa: BLE001
        return HIT_RET_THRESHOLD


def _monthly_aggregate(hits: list[dict], baseline_rate: float) -> list[dict]:
    """按月聚合实际命中率 + 漂移 z 检验。"""
    by_month: dict[str, list[dict]] = {}
    for h in hits:
        if h.get("t5_ret") is None:
            continue
        by_month.setdefault(h["date"][:6], []).append(h)

    months = []
    for month in sorted(by_month):
        rows = by_month[month]
        n = len(rows)
        hit_n = sum(1 for r in rows if r.get("hit"))
        hit_rate = hit_n / n if n else 0.0
        z, p = _prop_ztest(hit_rate, baseline_rate, n)
        months.append({
            "month": month,
            "n": n,
            "hit_n": hit_n,
            "hit_rate": round(hit_rate, 4),
            "baseline_rate": round(baseline_rate, 4),
            "z_stat": round(z, 3),
            "p_value": round(p, 4),
            "significantly_below": bool(p < SIG_ALPHA),
        })
    return months


def _pattern_decay(hits: list[dict], baseline_rate: float) -> list[dict]:
    """模式失效检测：按形态统计命中率，显著退化 → decayed + 降权建议。"""
    by_form: dict[str, list[dict]] = {}
    for h in hits:
        if h.get("t5_ret") is None:
            continue
        by_form.setdefault(h.get("form_type", "?"), []).append(h)

    result = []
    for form, rows in by_form.items():
        n = len(rows)
        if n < DECAY_MIN_SAMPLE:
            result.append({"form_type": form, "n": n, "hit_rate": None, "decayed": False, "note": "样本不足"})
            continue
        hit_n = sum(1 for r in rows if r.get("hit"))
        hit_rate = hit_n / n
        _, p = _prop_ztest(hit_rate, baseline_rate, n)
        decayed = bool(p < SIG_ALPHA)
        result.append({
            "form_type": form, "n": n, "hit_n": hit_n,
            "hit_rate": round(hit_rate, 4), "baseline_rate": round(baseline_rate, 4),
            "p_value": round(p, 4), "decayed": decayed,
            "suggestion": "降权/剔除" if decayed else "保持",
        })
    result.sort(key=lambda x: -x["n"])
    return result


def run_monitor(date: str = "") -> dict:
    """生成监控报告（月度漂移 + 模式失效检测 + 重验证建议）。"""
    date = date or time.strftime("%Y%m%d")
    state = _load_state()
    baseline = state.get("baseline")
    if not baseline:
        return {
            "run_at": date, "has_baseline": False,
            "message": "尚无回测基线，请先运行一次回测（POST /api/v1/backtest/run）",
        }

    baseline_rate = float(baseline.get("predicted_hit_rate") or 0.0)
    hits = state.get("hits", [])
    months = _monthly_aggregate(hits, baseline_rate)
    decays = _pattern_decay(hits, baseline_rate)

    # 连续 CONSECUTIVE_MONTHS 个月显著低于 → 触发重新验证
    below_streak = 0
    for m in reversed(months):
        if m.get("significantly_below"):
            below_streak += 1
        else:
            break
    need_revalidate = below_streak >= CONSECUTIVE_MONTHS

    report = {
        "run_at": date,
        "has_baseline": True,
        "baseline": baseline,
        "total_hits_recorded": len(hits),
        "monthly": months,
        "pattern_decay": decays,
        "consecutive_below_months": below_streak,
        "need_revalidate": need_revalidate,
        "alert": ("⚠️ 实际命中率连续低于回测基线，建议重新验证/考虑市场风格切换"
                  if need_revalidate else "✅ 正常，无需干预"),
        "suggestion": ("建议：用最新数据窗口重跑 A/B 对比（POST /api/v1/backtest/monitor/revalidate）"
                       if need_revalidate else "保持当前方案，下月继续监控"),
    }
    state["monitor"] = {"updated_at": int(time.time()), "report": report}
    _save_state(state)
    logger.info(
        f"监控报告完成: 月数={len(months)}, 需重验证={need_revalidate}, "
        f"失效形态={[d['form_type'] for d in decays if d.get('decayed')]}"
    )
    return report


def get_monitor_report() -> dict:
    """获取最近一次监控报告。"""
    state = _load_state()
    m = state.get("monitor")
    return m["report"] if m else {"message": "尚无监控报告，请先运行监控（POST /api/v1/backtest/monitor/run）"}


def roll_revalidate(result_dict: dict) -> dict:
    """月度滚动再验证：把最新回测结果记为基线，并结合 A/B 决策门给出上线建议。

    说明：真正"用最新数据窗口重跑回测"由定时任务/API 触发 run_backtest 后调用本函数；
    本函数负责刷新基线并输出"通过/暂缓"决策。
    """
    new_base = record_baseline(result_dict)
    go = new_base.get("go")
    return {
        "rolled_at": int(time.time()),
        "new_baseline": new_base,
        "decision": (
            "✅ 通过决策门，可继续接入每日推荐" if go is True
            else ("⏸ 未通过决策门，暂缓上线（提升不显著或样本不足）" if go is False
                  else "❓ 决策门未判定（样本不足），请扩大样本重跑")
        ),
    }
