"""最佳入场点分析（entry_analyzer）

对齐规划：plans/04-回测引擎.md 4.3 与第十四章第 9 项
  目的：回答"启动点 T0 确认后，到底怎么买最划算"（正负样本通用）。
  对每个 outcome 样本评估 4 种入场策略在 T+5 / T+20 的收益、胜率与回撤，
  最终按 成功 / 失败 分组汇总，找出各组的"最佳入场方式"。

  入场策略：
    t1_open     ：T+1 开盘买入（最贴近"次日竞价追入"）
    t1_close    ：T+1 收盘买入（观望到次日尾盘）
    pullback_t0 ：后 10 日内回踩启动价 ±3% 时低吸（否则退化为 T+1 收盘）
    break_peak  ：后 N 日内有效突破 T0 后首个 10 日高点时买入（否则退化为 T+1 收盘）

  口径：
    - 所有价格统一换算为前复权（adj），确保除权除息不影响收益
    - 入场日若"一字封死涨停"视为不可交易（tradeable=False），不参与胜率统计

  每日推荐接入（固化产物）：
    - build_entry_guidance：仅从"无后视偏差"策略（t1_open/t1_close/pullback_t0）
      中选出成功组各形态的最佳入场，固化为 entry_guidance.json 供每日推荐附加操作建议。
    - 排除 break_peak：其"前高"取 T0 后 10 日最高价，含后视偏差，不宜作实盘建议。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logger import logger
from app.backtest.outcome_tracker import LABEL_SUCCESS, LABEL_FAILURE_A, LABEL_FAILURE_B

ENTRY_STRATEGIES = ("t1_open", "t1_close", "pullback_t0", "break_peak")
# 无后视偏差策略（可作实盘操作建议；break_peak 含未来数据，仅作研究参考）
NO_LOOKAHEAD_STRATEGIES = ("t1_open", "t1_close", "pullback_t0")
# 入场观察窗口（交易日）
PULLBACK_WINDOW = 10
BREAK_WINDOW = 10
PULLBACK_TOL = 0.03          # 回踩容差 ±3%
BREAKOUT_PCT = 1.0           # 突破=close 站上前高（等于或高于）
MAX_HOLD = 20                # 收益观察窗口
WIN_RET = 0.05               # T+5 胜率口径：>5%

# 策略中文描述（每日推荐"建议入场方式"展示）
ENTRY_DESC = {
    "t1_open": "T+1 开盘买入（次日竞价追入）",
    "t1_close": "T+1 收盘买入（次日尾盘）",
    "pullback_t0": "启动价回踩 ±3% 低吸（10 日内等回调）",
    "break_peak": "突破 T0 后 10 日高点买入（含未来数据，仅参考）",
}
# 入场指引固化（回测产出 → 每日推荐附加操作建议，防参数漂移）
ENTRY_GUIDANCE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "entry_guidance.json",
)


@dataclass
class EntryResult:
    """单样本各策略入场表现。"""
    ts_code: str
    form_type: str
    label: str
    t0_date: str = ""
    strategies: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ts_code": self.ts_code,
            "form_type": self.form_type,
            "label": self.label,
            "t0_date": self.t0_date,
            "strategies": self.strategies,
        }


def _adj_price(df: pd.DataFrame, col: str) -> np.ndarray:
    """将原始价列换算为前复权价（复用 df 中的 adj_factor）。"""
    if col not in df.columns:
        return np.array([], dtype=float)
    vals = df[col].to_numpy(dtype=float)
    if "adj_factor" in df.columns:
        af = df["adj_factor"].to_numpy(dtype=float)
        latest = af[-1] if len(af) and af[-1] else 1.0
        if latest:
            vals = vals * af / latest
    return vals


def analyze_entry(df: pd.DataFrame, sample) -> EntryResult | None:
    """对单个 outcome 样本分析各入场策略。

    Args:
        df: prepare_stock_data 输出（需含 open/high/low/close/adj_factor/is_sealed_up）
        sample: OutcomeSample（含 t0_idx）

    Returns:
        EntryResult 或 None（数据不足/不可入场）
    """
    if df is None or df.empty or sample is None:
        return None
    t0 = int(sample.t0_idx)
    n = len(df)
    if t0 < 0 or t0 + 1 >= n:
        return None

    adj_close = _adj_price(df, "close")
    adj_open = _adj_price(df, "open")
    adj_high = _adj_price(df, "high")
    sealed = df["is_sealed_up"].to_numpy() if "is_sealed_up" in df.columns else np.zeros(n, dtype=bool)
    trade_date = df["trade_date"].to_numpy()
    start_price = float(adj_close[t0])
    if start_price <= 0:
        return None

    def _ret_from(entry_idx: int, entry_price: float, offset: int) -> float | None:
        idx = entry_idx + offset
        if idx < n:
            return float(adj_close[idx] / entry_price - 1.0)
        return None

    def _max_dd_from(entry_idx: int, entry_price: float) -> float:
        end = min(entry_idx + MAX_HOLD, n)
        if end <= entry_idx:
            return 0.0
        seg = adj_close[entry_idx : end + 1]
        running_peak = np.maximum.accumulate(seg)
        dds = (seg - running_peak) / running_peak
        return float(np.min(dds)) if len(dds) else 0.0

    def _entry(tag: str, entry_idx: int, entry_price: float) -> dict:
        return {
            "strategy": tag,
            "entry_date": str(pd.Timestamp(trade_date[entry_idx]).date()) if entry_idx < n else "",
            "entry_price": round(entry_price, 4),
            "entry_gain": round(entry_price / start_price - 1.0, 4),
            "ret_t5": _ret_from(entry_idx, entry_price, 5),
            "ret_t20": _ret_from(entry_idx, entry_price, 20),
            "max_dd": round(_max_dd_from(entry_idx, entry_price), 4),
            "tradeable": bool(not bool(sealed[entry_idx])),
        }

    strategies: dict = {}

    # ── t1_open / t1_close ──
    if t0 + 1 < n:
        strategies["t1_open"] = _entry("t1_open", t0 + 1, adj_open[t0 + 1])
        strategies["t1_close"] = _entry("t1_close", t0 + 1, adj_close[t0 + 1])

    # ── pullback_t0：后 PULLBACK_WINDOW 日内回踩启动价 ±3% 低吸 ──
    entry_idx, entry_price = t0 + 1, adj_close[t0 + 1]
    for j in range(t0 + 1, min(t0 + 1 + PULLBACK_WINDOW, n)):
        p = adj_close[j]
        if start_price * (1 - PULLBACK_TOL) <= p <= start_price * (1 + PULLBACK_TOL):
            entry_idx, entry_price = j, p
            break
    strategies["pullback_t0"] = _entry("pullback_t0", entry_idx, entry_price)

    # ── break_peak：突破 T0 后首个 BREAK_WINDOW 高点买入 ──
    peak_hi = float(np.max(adj_high[t0 + 1 : min(t0 + 1 + BREAK_WINDOW, n)])) if t0 + 1 < n else 0.0
    entry_idx, entry_price = t0 + 1, adj_close[t0 + 1]
    if peak_hi > 0:
        for j in range(t0 + 1, min(t0 + 1 + BREAK_WINDOW, n)):
            if adj_close[j] >= peak_hi * BREAKOUT_PCT:
                entry_idx, entry_price = j, adj_close[j]
                break
    strategies["break_peak"] = _entry("break_peak", entry_idx, entry_price)

    return EntryResult(
        ts_code=sample.ts_code,
        form_type=sample.form_type,
        label=sample.label,
        t0_date=str(sample.t0_date.date()) if hasattr(sample.t0_date, "date") else str(sample.t0_date),
        strategies=strategies,
    )


def _agg_strategy(rows: list[dict]) -> dict:
    """汇总某标签下某策略的统计。"""
    if not rows:
        return {}
    rets5 = [r["ret_t5"] for r in rows if r.get("ret_t5") is not None and r.get("tradeable")]
    rets20 = [r["ret_t20"] for r in rows if r.get("ret_t20") is not None and r.get("tradeable")]
    tradeable_n = sum(1 for r in rows if r.get("tradeable"))
    if not rets5:
        return {"count": tradeable_n, "note": "无可交易样本"}

    wins = sum(1 for r in rets5 if r > WIN_RET)
    profits = sum(r for r in rets20 if r > 0)
    losses = abs(sum(r for r in rets20 if r < 0))
    pf = (profits / losses) if losses > 1e-9 else (float("inf") if profits > 0 else 0.0)
    return {
        "count": tradeable_n,
        "win_rate_t5": round(wins / len(rets5), 4),
        "avg_ret_t5": round(float(np.mean(rets5)), 4),
        "avg_ret_t20": round(float(np.mean(rets20)), 4) if rets20 else None,
        "profit_factor": round(float(pf), 4) if pf != float("inf") else None,
        "avg_entry_gain": round(float(np.mean([r["entry_gain"] for r in rows if r.get("tradeable")])), 4),
    }


def aggregate_entry_results(rows: list[EntryResult]) -> dict:
    """汇总所有样本的入场分析：按 label 分组 × 策略统计。

    Returns:
        {
          "by_label": { success: {strategy: {...}}, failure: {...}, failure_A: {...}, failure_B: {...} },
          "best_by_label": { success: "t1_open", failure: "..." },
          "total_analyzed": n,
        }
    """
    if not rows:
        return {"by_label": {}, "best_by_label": {}, "total_analyzed": 0}

    by_label: dict[str, list[dict]] = {}
    for r in rows:
        d = r.to_dict()
        # 展平：strategy dict 列表
        for tag, sd in d["strategies"].items():
            sd = dict(sd)
            sd["ts_code"] = d["ts_code"]
            sd["form_type"] = d["form_type"]
            sd["t0_date"] = d["t0_date"]
            by_label.setdefault(d["label"], []).append(sd)

    result_by_label: dict[str, dict] = {}
    best_by_label: dict[str, str] = {}
    for label, srows in by_label.items():
        per_strategy = {}
        for tag in ENTRY_STRATEGIES:
            tag_rows = [s for s in srows if s.get("strategy") == tag]
            if tag_rows:
                per_strategy[tag] = _agg_strategy(tag_rows)
        result_by_label[label] = per_strategy
        # 最佳：按 avg_ret_t20 优先，其次 win_rate_t5
        ranked = [
            (tag, st) for tag, st in per_strategy.items()
            if st and st.get("avg_ret_t20") is not None
        ]
        if ranked:
            best = max(ranked, key=lambda kv: (kv[1]["avg_ret_t20"], kv[1]["win_rate_t5"]))
            best_by_label[label] = best[0]

    return {
        "by_label": result_by_label,
        "best_by_label": best_by_label,
        "total_analyzed": len(rows),
    }


def analyze_batch(df: pd.DataFrame, samples: list) -> list[EntryResult]:
    """批量分析（同一股票的多个样本）。"""
    results: list[EntryResult] = []
    for s in samples:
        try:
            r = analyze_entry(df, s)
            if r is not None:
                results.append(r)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"entry_analyzer 失败 {s.ts_code}@{s.t0_idx}: {exc}")
    return results


def build_entry_guidance(rows: list[EntryResult]) -> dict:
    """构建"每日推荐入场指引"：成功组各形态的最佳入场方式（无后视偏差策略）。

    与 aggregate_entry_results 的区别：
      - 只看成功组（success），因为每日推荐推荐的是"可能成功的股票"
      - 只在 NO_LOOKAHEAD_STRATEGIES（t1_open/t1_close/pullback_t0）中选 best，
        排除含未来数据的 break_peak（其"前高"取 T0 后 10 日最高价，有后视偏差）

    Returns:
        {
          "by_form": {
              "A": {"strategy": "pullback_t0", "desc": "...", "win_rate_t5": 0.6,
                    "avg_ret_t20": 0.15, "count": 120},
              ...
          },
          "generated_at": "YYYY-MM-DD HH:MM:SS",
        }
    """
    from datetime import datetime as _dt

    # 展平：成功组 样本 → (form_type, strategy, stats)
    groups: dict[str, dict[str, list[dict]]] = {}  # form -> strategy -> rows
    for r in rows:
        d = r.to_dict()
        if d["label"] != LABEL_SUCCESS:
            continue
        form = d["form_type"]
        for tag, sd in d["strategies"].items():
            sd = dict(sd)
            sd["ts_code"] = d["ts_code"]
            groups.setdefault(form, {}).setdefault(tag, []).append(sd)

    by_form: dict[str, dict] = {}
    for form, strat_rows in groups.items():
        best: tuple[str, dict] | None = None
        for tag in NO_LOOKAHEAD_STRATEGIES:
            tag_rows = strat_rows.get(tag, [])
            if not tag_rows:
                continue
            agg = _agg_strategy(tag_rows)
            if not agg or agg.get("avg_ret_t20") is None:
                continue
            key = (agg["avg_ret_t20"], agg.get("win_rate_t5", 0.0))
            if best is None or key > (best[1]["avg_ret_t20"], best[1].get("win_rate_t5", 0.0)):
                best = (tag, agg)
        if best is not None:
            tag, agg = best
            by_form[form] = {
                "strategy": tag,
                "desc": ENTRY_DESC.get(tag, tag),
                "win_rate_t5": agg.get("win_rate_t5"),
                "avg_ret_t20": agg.get("avg_ret_t20"),
                "avg_ret_t5": agg.get("avg_ret_t5"),
                "count": agg.get("count"),
            }

    return {
        "by_form": by_form,
        "note": "最佳入场仅从无后视偏差策略（t1_open/t1_close/pullback_t0）中选取",
        "generated_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_entry_guidance(guidance: dict, path: str = ENTRY_GUIDANCE_FILE) -> None:
    """固化入场指引到磁盘（每日推荐加载同一份，防参数漂移）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(guidance, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"入场指引已固化: {path}（{len(guidance.get('by_form', {}))} 个形态）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"固化入场指引失败: {exc}")


def load_entry_guidance(path: str = ENTRY_GUIDANCE_FILE) -> dict | None:
    """加载已固化的入场指引。"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"加载入场指引失败: {exc}")
    return None


def get_entry_advice(guidance: dict | None, form_type: str) -> dict | None:
    """按形态取入场建议（每日推荐每条推荐附加用）。"""
    if not guidance:
        return None
    by_form = guidance.get("by_form", {})
    return by_form.get(str(form_type))
