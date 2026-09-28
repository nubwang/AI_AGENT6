"""绩效指标计算（metrics）

对齐规划：plans/04-回测引擎.md 六、绩效分析指标
         plans/23 §2.4 T1/T1b（回测可信度修复）

⚠️ 口径纪律（务必遵守，否则又会产出 win_rate=1.0 这类"假高分"）：
本模块输出**两类语义完全不同**的指标，禁止混用：

① 条件集描述统计（scope = "conditional_survivor"）
   样本来自"事后已确认爆发"的条件集（3 倍行情段 → success/failure_A/failure_B）。
   这些样本的 T+5 正收益比例天然接近 1（failure_A 本身就要求涨 50%~150%），
   **它不等于推荐命中率**，只能用于"形态/条件之间的相对比较"，
   **不可作为上线决策或进化主指标**。

② 推荐组合指标（scope = "recommendation_topn"）
   在 T0 时点按模型排序取 TOP-N，并配合**同口径对照组**（同期未爆发样本）计算
   命中率 / 平均收益 / 超额。只有这一类允许用于上线与进化决策。

落盘护栏：validate_metrics() 产出 quality_gate（ok / warn / error）。
存在 error 级问题时，落盘层（api/backtest.py）**拒绝覆盖 backtest_latest.json**，
避免失真指标被下游（进化大脑主指标、前端面板）当作事实消费。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from app.core.logger import logger

TRADING_DAYS = 252
RISK_FREE = 0.02

# 往返交易成本默认值（双边佣金+印花税 ≈0.15%），与 plan_tracker 口径一致
DEFAULT_COST_PCT = 0.0015

# 指标口径常量
SCOPE_CONDITIONAL = "conditional_survivor"   # 条件集（事后已爆发）描述统计
SCOPE_TOP_N = "recommendation_topn"          # 推荐 TOP-N（含对照组）指标
SCOPE_UNKNOWN = "unknown"

# 合理性阈值（validate_metrics 用）
MAX_PLAUSIBLE_AVG_T5 = 1.0          # 平均 T+5 超过 100% 不合理（样本被事后条件化的典型症状）
MAX_PLAUSIBLE_CUM_RETURN = 100.0    # 累计收益超过 100 倍不合理
MAX_PLAUSIBLE_ANNUAL = 100.0        # 年化超过 100 倍不合理
MIN_TRADES_FOR_STATS = 30           # 统计意义最低样本数
MIN_DISCRIMINATION_GAP = 0.08       # 正负样本 T+5 正收益比例最小差（8pct），低于此视为无区分度


@dataclass
class PerformanceMetrics:
    """回测绩效指标。"""
    total_trades: int = 0
    # ↓ 保留旧字段名以兼容既有调用方，但含义已更名（见 t5_pos_ratio / t5_gt5_ratio）
    win_rate: float = 0.0            # [兼容别名] == t5_pos_ratio
    hit_rate: float = 0.0            # [兼容别名] == t5_pos_ratio（**非推荐命中率**）
    accuracy: float = 0.0            # [兼容别名] == t5_gt5_ratio
    t20_continuation: float = 0.0    # T+20 仍 >0% 占比
    double_confirm: float = 0.0      # 双确认率（T+5>5% 且 T+20>0%）
    avg_t5_return: float = 0.0       # 平均 T+5 收益（已扣成本）
    profit_loss_ratio: float = 0.0   # 盈亏比
    cum_return: float = 0.0          # 累计收益率（按交易日等权组合，非顺序累乘）
    annual_return: float = 0.0       # 年化收益率
    max_drawdown: float = 0.0        # 最大回撤（负数）
    sharpe: float = 0.0              # 夏普比率
    exclude_effectiveness: float = 0.0  # 排除有效度（负模式库排除中确实失败的占比）

    # ↓ 新增：诚实命名 + 口径标注（plans/23 T1）
    t5_pos_ratio: float = 0.0        # T+5 正收益占比（诚实命名）
    t5_gt5_ratio: float = 0.0        # T+5 涨幅 >5% 占比（诚实命名）
    cost_pct: float = 0.0            # 已扣除的单边往返成本
    n_days: int = 0                  # 净值覆盖的不同交易日数
    scope: str = SCOPE_UNKNOWN       # 指标口径（见文件头说明）
    sample_note: str = ""            # 口径说明（会随报告落盘，供审计）
    # ↓ 对照组（推荐口径才有效）
    benchmark_n: int = 0             # 对照组样本数
    benchmark_t5_pos_ratio: float = 0.0
    benchmark_avg_t5: float = 0.0
    lift_t5_pos: float = 0.0         # 本组 T+5 正收益占比 - 对照组（pct）
    lift_p_value: float | None = None
    # ↓ 护栏
    valid: bool = True               # 是否存在 error 级口径问题
    quality_gate: str = "ok"         # ok / warn / error
    issues: list = field(default_factory=list)  # [{"level","code","message"}]

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _drawdown(equity: np.ndarray) -> float:
    """最大回撤（返回负数，如 -0.35 表示 -35%）。"""
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / np.maximum(peak, 1e-9)
    return float(np.min(dd))


def _proportion_p_value(x1: int, n1: int, x2: int, n2: int) -> float | None:
    """两比例单侧 z 检验（H1: p1 > p2）。样本不足返回 None。"""
    if n1 <= 0 or n2 <= 0 or (n1 + n2) <= 0:
        return None
    p1, p2 = x1 / n1, x2 / n2
    p_pool = (x1 + x2) / (n1 + n2)
    se = float(np.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2)))
    if se <= 0:
        return None
    z = (p1 - p2) / se
    # 单侧 p 值 = 1 - Φ(z)，用 erf 近似，避免 scipy 依赖
    import math
    p = 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))
    return float(p)


def build_equity_curve_by_date(
    records: Sequence,
    initial: float = 1.0,
    cost_pct: float = DEFAULT_COST_PCT,
) -> tuple[list[float], list[str], dict]:
    """按**交易日**聚合的等权组合净值曲线（plans/23 T1 修复核心）。

    旧实现 build_equity_curve() 把每笔 T+5 收益顺序累乘：
      - 同一天几十上百笔被当成"连续多次下注" → 净值指数级爆炸（曾产出 9.5e22）
      - 多个 T+5 窗口期重叠 → 资金被重复使用，现实中不可能
    新实现：
      - 同一交易日的多笔样本**等权平均**（= 当日组合收益），一天只计一次
      - 每笔扣除往返成本 cost_pct
      - 按日期升序累乘，得到可解释的净值

    Args:
        records: [(date, t5_return), ...]；date 可为 str/TS/date，t5_return 为比例（0.05=+5%）
                 也接受 dict 形式 {"date":..., "t5":...}
        initial: 初始净值
        cost_pct: 单笔往返成本（默认 0.15%）

    Returns:
        (equity, dates, stats)
        equity: 净值序列（首元素 initial）
        dates:  与 equity 对齐的日期序列（首元素 ""）
        stats:  {"n_days", "n_samples", "avg_daily_ret", "cost_pct"}
    """
    grouped: dict[str, list[float]] = {}
    for rec in records or []:
        if isinstance(rec, dict):
            date_v, ret = rec.get("date"), rec.get("t5")
        else:
            date_v, ret = (rec[0], rec[1]) if len(rec) >= 2 else (None, None)
        if ret is None or date_v is None:
            continue
        try:
            r = float(ret)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(r):
            continue
        grouped.setdefault(str(date_v)[:10], []).append(r)

    equity = [float(initial)]
    dates: list[str] = [""]
    for day in sorted(grouped.keys()):
        rets = grouped[day]
        if not rets:
            continue
        day_ret = float(np.mean(rets)) - float(cost_pct)   # 等权组合收益，扣成本
        equity.append(equity[-1] * (1.0 + day_ret))
        dates.append(day)

    stats = {
        "n_days": len(equity) - 1,
        "n_samples": int(sum(len(v) for v in grouped.values())),
        "avg_daily_ret": float(np.mean([equity[i + 1] / equity[i] - 1 for i in range(len(equity) - 1)])) if len(equity) > 1 else 0.0,
        "cost_pct": float(cost_pct),
    }
    return equity, dates, stats


def build_equity_curve(t5_returns: Sequence[float | None], initial: float = 1.0) -> list[float]:
    """[已弃用] 顺序累乘净值（保留仅为兼容旧调用方）。

    ⚠️ 该口径会把同一天的 N 笔样本当成"连续 N 次下注"，导致净值爆炸
    （实测曾产出 cum_return=9.5e22）。**不要用于任何决策或落盘**，
    请改用 build_equity_curve_by_date()。
    """
    logger.warning("build_equity_curve() 已弃用（顺序累乘会放大净值），请改用 build_equity_curve_by_date()")
    eq = [initial]
    for r in t5_returns:
        if r is not None:
            eq.append(eq[-1] * (1 + r))
    return eq


def compute_metrics(
    t5_returns: Sequence[float | None],
    t20_returns: Sequence[float | None] | None = None,
    equity_curve: Sequence[float] | None = None,
    excluded_actual_fail: int | None = None,
    excluded_total: int | None = None,
    dates: Sequence | None = None,
    scope: str = SCOPE_CONDITIONAL,
    cost_pct: float = DEFAULT_COST_PCT,
    benchmark_t5: Sequence[float | None] | None = None,
) -> PerformanceMetrics:
    """计算绩效指标。

    Args:
        t5_returns: 每笔交易的 T+5 收益
        t20_returns: 每笔交易的 T+20 收益（可选，用于双确认）
        equity_curve: 组合净值曲线（可选）。若为 None 且提供 dates → 内部按交易日等权构建
        excluded_actual_fail / excluded_total: 负模式库排除统计
        dates: 与 t5_returns 对齐的 T0 日期（推荐传入；用于按日聚合净值）
        scope: 指标口径（conditional_survivor / recommendation_topn）
        cost_pct: 单笔往返成本（仅用于按日净值与 avg_t5 净口径展示）
        benchmark_t5: 对照组 T+5 收益（仅 scope=recommendation_topn 有意义）
    """
    m = PerformanceMetrics(scope=scope, cost_pct=float(cost_pct))
    t5 = np.array([r for r in t5_returns if r is not None], dtype=float)
    if len(t5) == 0:
        m.quality_gate = "error"
        m.valid = False
        m.issues = [{"level": "error", "code": "EMPTY_SAMPLE",
                     "message": "样本为空，无法计算任何指标"}]
        return m

    m.total_trades = len(t5)
    m.t5_pos_ratio = float((t5 > 0).mean())
    m.t5_gt5_ratio = float((t5 > 0.05).mean())
    # 兼容别名（含义见字段注释）
    m.win_rate = m.t5_pos_ratio
    m.hit_rate = m.t5_pos_ratio
    m.accuracy = m.t5_gt5_ratio
    m.avg_t5_return = float(t5.mean())

    gains = t5[t5 > 0]
    losses = -t5[t5 < 0]
    m.profit_loss_ratio = float(gains.mean() / losses.mean()) if len(gains) and len(losses) and losses.mean() > 0 else 0.0

    # T+20 双确认
    if t20_returns:
        t20 = np.array([r for r in t20_returns if r is not None], dtype=float)
        if len(t20):
            m.t20_continuation = float((t20 > 0).mean())
            pair = min(len(t5), len(t20))
            both = np.array([t5[i] > 0.05 and t20[i] > 0 for i in range(pair)])
            m.double_confirm = float(both.mean()) if pair else 0.0

    # 净值曲线：优先用调用方传入；否则按交易日等权自行构建（含成本）
    if equity_curve and len(equity_curve) > 1:
        eq = np.array(equity_curve, dtype=float)
        m.n_days = len(eq) - 1
    elif dates and len(dates) == len(t5_returns):
        recs = [(d, r) for d, r in zip(dates, t5_returns) if r is not None]
        eq_list, _eq_dates, stats = build_equity_curve_by_date(recs, cost_pct=cost_pct)
        eq = np.array(eq_list, dtype=float)
        m.n_days = int(stats["n_days"])
    else:
        eq = None

    if eq is not None and len(eq) > 1 and eq[0] > 0:
        m.cum_return = float(eq[-1] / eq[0] - 1)
        # 年化按"交易日数"折算（净值点=交易日数）
        n = max(len(eq) - 1, 1)
        ratio = eq[-1] / eq[0]
        if ratio > 0:
            m.annual_return = float(ratio ** (TRADING_DAYS / n) - 1)
        m.max_drawdown = _drawdown(eq)
        daily_ret = np.diff(eq) / np.maximum(eq[:-1], 1e-9)
        if len(daily_ret) > 1 and daily_ret.std() > 0:
            m.sharpe = float((daily_ret.mean() * TRADING_DAYS - RISK_FREE) / (daily_ret.std() * np.sqrt(TRADING_DAYS)))

    # 排除有效度
    if excluded_total and excluded_total > 0:
        m.exclude_effectiveness = float((excluded_actual_fail or 0) / excluded_total)

    # 对照组（推荐口径）：算超额与显著性
    if benchmark_t5:
        b = np.array([r for r in benchmark_t5 if r is not None], dtype=float)
        if len(b):
            m.benchmark_n = len(b)
            m.benchmark_t5_pos_ratio = float((b > 0).mean())
            m.benchmark_avg_t5 = float(b.mean())
            m.lift_t5_pos = float((m.t5_pos_ratio - m.benchmark_t5_pos_ratio) * 100)
            m.lift_p_value = _proportion_p_value(
                int((t5 > 0).sum()), len(t5), int((b > 0).sum()), len(b)
            )

    # 口径说明（随报告落盘，供审计）
    if scope == SCOPE_CONDITIONAL:
        m.sample_note = (
            "条件集（事后已确认爆发样本）描述统计：T+5 正收益比例天然接近 1，"
            "**不是推荐命中率**，仅可用于组间对比；上线/进化决策请用 recommendation_topn 口径。"
        )
    else:
        m.sample_note = "推荐 TOP-N 口径（含对照组基准）；可作为上线/进化决策依据。"

    # 护栏
    m.issues = validate_metrics(m)
    m.quality_gate = _gate_from_issues(m.issues)
    m.valid = m.quality_gate != "error"
    if not m.valid:
        logger.warning(f"[metrics] 口径质量门未通过（error）: {[i['code'] for i in m.issues]}")
    return m


def _gate_from_issues(issues: list[dict]) -> str:
    if any(i.get("level") == "error" for i in issues):
        return "error"
    if any(i.get("level") == "warning" for i in issues):
        return "warn"
    return "ok"


def check_discrimination(
    pos_t5: Sequence[float | None],
    neg_t5: Sequence[float | None],
    min_gap: float | None = None,
) -> dict:
    """正负样本区分度检验（plans/23 T3）。

    用途：验证"负样本"是否真的是负样本。若正负两组的 T+5 正收益比例几乎相同
    （实测曾出现 success 0.807 vs failure_A 0.788），说明标签体系失效，
    此时基于该标签的归因/条件概率/模式库全部不可信。

    Returns:
        {"pos_n","neg_n","pos_pos_ratio","neg_pos_ratio","gap","p_value","passed","message"}
    """
    # 区分度门槛可进化（plans/23 §十二）：未显式传参时读 evolution_config
    if min_gap is None:
        min_gap = MIN_DISCRIMINATION_GAP
        try:
            from app.agents import evolution_config
            min_gap = float(evolution_config.get_num("bt_discrimination_min_gap", MIN_DISCRIMINATION_GAP))
        except Exception:  # noqa: BLE001
            pass

    p = np.array([r for r in (pos_t5 or []) if r is not None], dtype=float)
    n = np.array([r for r in (neg_t5 or []) if r is not None], dtype=float)
    out = {
        "pos_n": int(len(p)), "neg_n": int(len(n)),
        "pos_pos_ratio": float((p > 0).mean()) if len(p) else 0.0,
        "neg_pos_ratio": float((n > 0).mean()) if len(n) else 0.0,
        "gap": 0.0, "p_value": None, "passed": False,
        "message": "",
    }
    if len(p) == 0 or len(n) == 0:
        out["message"] = "正样本或负样本为空，无法检验区分度"
        return out
    out["gap"] = float(out["pos_pos_ratio"] - out["neg_pos_ratio"])
    out["p_value"] = _proportion_p_value(int((p > 0).sum()), len(p), int((n > 0).sum()), len(n))
    out["passed"] = bool(out["gap"] >= min_gap and (out["p_value"] is not None and out["p_value"] < 0.05))
    out["message"] = (
        f"正样本 T+5 正收益率 {out['pos_pos_ratio']:.1%} vs 负样本 {out['neg_pos_ratio']:.1%}，"
        f"差 {out['gap'] * 100:+.2f}pct，p={out['p_value'] if out['p_value'] is None else round(out['p_value'], 4)}"
        f" → {'通过' if out['passed'] else '未通过（标签无区分度，归因/概率表不可信）'}"
    )
    return out


def compare_to_control(
    sample_t5: Sequence[float | None],
    control_pos_ratio: float,
    control_n: int,
    min_gap: float | None = None,
) -> dict:
    """样本组 vs **同口径对照组**的区分度检验（plans/23 T4 第二步）。

    对照组 = 同一交易日、无启动迹象的股票（[`control_sampler`](ai-quant-agent/backend/app/backtest/control_sampler.py:1)），
    它只提供"正收益率 + 样本数"（每日聚合），故这里做**比例检验**而非逐样本配对：
      - gap   = 样本组 T+5 正收益率 − 对照组正收益率
      - p     = 两比例单侧 z 检验
      - passed= gap ≥ 门槛 且 p < 0.05

    这是"启动形态这一层到底有没有 alpha"的**最终判据**：
    若不通过 → 说明当前信号在统计上不优于"随便买一批没启动迹象的股票"。
    """
    s = np.array([r for r in (sample_t5 or []) if r is not None], dtype=float)
    if min_gap is None:
        min_gap = MIN_DISCRIMINATION_GAP
        try:
            from app.agents import evolution_config
            min_gap = float(evolution_config.get_num("bt_discrimination_min_gap", MIN_DISCRIMINATION_GAP))
        except Exception:  # noqa: BLE001
            pass
    out = {
        "sample_n": int(len(s)), "control_n": int(control_n),
        "sample_pos_ratio": float((s > 0).mean()) if len(s) else 0.0,
        "control_pos_ratio": float(control_pos_ratio or 0.0),
        "gap": 0.0, "p_value": None, "passed": False,
        "min_gap": float(min_gap), "message": "",
    }
    if len(s) == 0 or control_n <= 0:
        out["message"] = "样本或对照组为空，无法检验"
        return out
    out["gap"] = float(out["sample_pos_ratio"] - out["control_pos_ratio"])
    out["p_value"] = _proportion_p_value(
        int((s > 0).sum()), len(s),
        int(round(out["control_pos_ratio"] * control_n)), int(control_n))
    p = out["p_value"]
    out["passed"] = bool(out["gap"] >= min_gap and p is not None and p < 0.05)
    out["message"] = (
        f"样本组 T+5 正收益率 {out['sample_pos_ratio']:.1%}(n={len(s)}) vs 对照组 "
        f"{out['control_pos_ratio']:.1%}(n={control_n})，差 {out['gap'] * 100:+.2f}pct，"
        f"p={p if p is None else round(p, 4)} → "
        + ("通过（有真实超额）" if out["passed"] else "未通过（不优于'无启动迹象'股票，形态层无 alpha）")
    )
    return out


def validate_metrics(m: PerformanceMetrics) -> list[dict]:
    """指标合理性护栏（plans/23 T1b）。

    把"实测过的失真症状"固化为自动检查，避免再次产出 win_rate=1.0 /
    cum_return=9.5e22 这类数字被下游当事实消费。
    """
    issues: list[dict] = []

    if m.total_trades == 0:
        issues.append({"level": "error", "code": "NO_TRADES", "message": "无有效样本"})
        return issues

    # ── error 级：不可能或明显失真的数字 ──
    if m.avg_t5_return > MAX_PLAUSIBLE_AVG_T5:
        issues.append({
            "level": "error", "code": "AVG_T5_IMPLAUSIBLE",
            "message": f"平均 T+5 收益 {m.avg_t5_return:.1%} > 100%，样本几乎必然被事后条件化（幸存者偏差）",
        })
    if abs(m.cum_return) > MAX_PLAUSIBLE_CUM_RETURN:
        issues.append({
            "level": "error", "code": "CUM_RETURN_IMPLAUSIBLE",
            "message": f"累计收益 {m.cum_return:.1f}x 失真（旧『顺序累乘』口径的典型症状）；请检查是否按交易日等权聚合",
        })
    if abs(m.annual_return) > MAX_PLAUSIBLE_ANNUAL:
        issues.append({
            "level": "error", "code": "ANNUAL_RETURN_IMPLAUSIBLE",
            "message": f"年化收益 {m.annual_return:.1f}x 失真",
        })
    if m.n_days > 0 and m.n_days > m.total_trades:
        issues.append({
            "level": "error", "code": "EQUITY_DAYS_EXCEED_SAMPLES",
            "message": f"净值交易日数({m.n_days}) > 样本数({m.total_trades})，净值聚合口径错误",
        })
    if (m.t5_pos_ratio >= 0.999 or m.t5_pos_ratio <= 0.001) and m.benchmark_n == 0 and m.scope == SCOPE_TOP_N:
        issues.append({
            "level": "error", "code": "NO_BENCHMARK_EXTREME_RATIO",
            "message": f"推荐口径下 T+5 正收益比例 {m.t5_pos_ratio:.1%} 极端且无对照组，命中性无法证伪",
        })

    # ── warning 级：可疑但可能是真实的 ──
    if m.max_drawdown == 0.0:
        issues.append({
            "level": "warning", "code": "NO_DRAWDOWN",
            "message": "最大回撤为 0（净值只涨不跌），口径可疑；真实组合不可能无回撤",
        })
    if m.profit_loss_ratio == 0.0:
        issues.append({
            "level": "warning", "code": "NO_LOSING_SAMPLES",
            "message": "盈亏比为 0：该样本集内没有任何亏损样本 → 不具备评估价值",
        })
    if m.total_trades < MIN_TRADES_FOR_STATS:
        issues.append({
            "level": "warning", "code": "SMALL_SAMPLE",
            "message": f"样本仅 {m.total_trades} 条（< {MIN_TRADES_FOR_STATS}），统计结论不稳定",
        })
    if m.scope == SCOPE_CONDITIONAL:
        issues.append({
            "level": "warning", "code": "CONDITIONAL_SCOPE",
            "message": "当前为条件集口径（事后已爆发样本），**不可作为上线/进化决策依据**",
        })
    if m.scope == SCOPE_TOP_N and m.benchmark_n == 0:
        issues.append({
            "level": "warning", "code": "NO_BENCHMARK",
            "message": "推荐口径缺少同口径对照组，无法计算真实超额",
        })
    if m.benchmark_n > 0 and m.lift_p_value is not None and m.lift_p_value >= 0.05:
        issues.append({
            "level": "warning", "code": "LIFT_NOT_SIGNIFICANT",
            "message": f"相对对照组超额 {m.lift_t5_pos:+.2f}pct 不显著（p={m.lift_p_value:.3f} ≥ 0.05）",
        })
    return issues


__all__ = [
    "PerformanceMetrics", "compute_metrics", "build_equity_curve",
    "build_equity_curve_by_date", "validate_metrics", "check_discrimination",
    "SCOPE_CONDITIONAL", "SCOPE_TOP_N", "DEFAULT_COST_PCT",
]
