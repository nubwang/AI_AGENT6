"""回测任务编排（runner）

对齐规划：plans/04-回测引擎.md 九/十二
  串联流水线：数据校验 → 召回扫描 → 形态分类 → 正负分流 → 特征提取
            → 条件概率统计 → 归因分析 → 绩效指标 → 回测报告
  支持：全量回测 / 增量回测 / 单股票调试
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logger import logger
from app.backtest.loader import load_stock_pool, prepare_stock_data
from app.backtest.surge_scanner import scan_market, scan_single_stock, dedupe_segments
from app.backtest.form_classifier import classify_batch, FORM_U, U_RATIO_WARN
from app.backtest.outcome_tracker import (
    batch_classify_outcomes, sample_summary, filter_noisy_samples,
    LABEL_SUCCESS, LABEL_FAILURE_A, LABEL_FAILURE_B,
)
from app.backtest.feature_extractor import (
    extract_features, zscore_normalize, FEATURE_NAMES, MATCH_FEATURES,
    VECTOR_FEATURES, CONDITION_FEATURES, FeatureNormalizer, save_normalizer,
)
from app.backtest.threshold_analyzer import (
    build_probability_table, save_probability_table, score_by_probability_table, ProbabilityTable,
    build_condition_table, save_condition_table,
)
from app.backtest.attribution import analyze_attribution, save_attribution_kb
from app.backtest.metrics import (
    compute_metrics, check_discrimination, compare_to_control, SCOPE_CONDITIONAL,
)
from app.backtest.control_sampler import controls_for_dates, pool_controls
from app.backtest.validator import run_ab_validation
from app.backtest.vector_store import build_vector_store, VectorStore
from app.backtest.recommender import DailyRecommender
from app.backtest.data_validator import run_data_validation
from app.backtest.cluster import cluster_by_form, cluster_attribution
from app.backtest.pattern_miner import mine_positive_patterns
from app.backtest.failure_miner import mine_failure_patterns
from app.backtest.entry_analyzer import (
    analyze_batch, aggregate_entry_results, build_entry_guidance, save_entry_guidance,
)
from app.backtest.extra_feature_loader import build_extra_map

def _bp(name: str, default):
    """读可进化参数（plans/23 §十二：阈值交给进化大脑统一调优，Tier1 热生效）。

    失败一律回退硬编码默认值（不崩回测）。
    """
    try:
        from app.agents import evolution_config
        return evolution_config.get_num(name, default)
    except Exception:  # noqa: BLE001
        return default


# 数据门禁（plans/23 §2.4 T7 / §2.6 W3）：核心表日期必须一致，否则回测拒绝启动。
# 实测症状：data_validation 显示 daily.max_date=20260916 而 adj_factor/stk_limit=20260917，
# 回测就在"日线缺最新一天"的状态下跑完了 → 特征窗口与结局窗口错位，结论不可信。
#
# ── 2026-09-20 第二轮：门禁必须"按交易日"而不是"按自然日"判断（用户要求：考虑节假日/双休）──
# 背景 Bug：`stk_limit` 的列标签被调换（trade_date 列里装的是 ts_code），门禁按字符串取 MAX
# 得到 `920992.BJ`，于是报出"核心表日期不一致：最新 920992.B"这种**看不懂也无法行动**的信息。
# 同时也暴露：原门禁要求核心表 max_date 完全相等，遇到"周五收盘 + 周末"或长假时，
# 只要有一张表落在了不同交易日就会误报/漏报。
#
# 新规则：
#   1. 日期列必须是合法 YYYYMMDD，否则直接判"数据损坏"并给出具体值（不再拿它去比大小）
#   2. 基准 = trade_cal 里"最近一个已开市、且 <= 今天"的交易日（周末/节假日自动跳过）
#      → 表格落后/领先一律按**交易日个数**衡量，节假日不会被算成"落后 3 天"
#   3. 锚定表（daily/adj_factor/daily_basic）必须同一天：它们是一条链上的（行情/复权/估值）
#   4. stk_limit 允许领先（涨跌停价按"次日"披露）→ 允许领先 1 个交易日
STRICT_DATA_GATE = True
GATE_CORE_TABLES = ("daily", "adj_factor", "stk_limit", "daily_basic")
# ★ 门禁是否按「**有效交易日**」（行数 ≥ 自然基数下限）比较（plans/24 §11.34.5）。
#   默认 **True** —— 这是**修正**：只看 MAX 会把一个 443 行的"半截日"当成"领先"，
#   导致门禁永不通过、且报错原因误导排查方向。置 False 可回退为旧行为。
GATE_USE_EFFECTIVE = True
GATE_ANCHOR_TABLES = ("daily", "adj_factor", "daily_basic")   # 必须完全同一天
GATE_MAX_LAG_TD = 0        # 允许落后多少个"交易日"（0 = 必须已是最新交易日）
GATE_ALLOW_AHEAD_TD = 1    # 允许领先多少个"交易日"（stk_limit 次日涨跌停价 → 1）

_DATE_RE = re.compile(r"^\d{8}$")


def _cal_conn():
    """交易日历查询用的轻量连接（失败返回 None → 门禁降级为"只查格式+一致性"）。"""
    try:
        from app.models import SessionLocal
        return SessionLocal()
    except Exception:  # noqa: BLE001
        return None


def _latest_trade_date(today: str = "") -> str:
    """<= today 的最近交易日（YYYYMMDD）。查不到返回 ''（门禁将降级）。"""
    from sqlalchemy import text as _t
    db = _cal_conn()
    if db is None:
        return ""
    try:
        sql = "SELECT MAX(cal_date) FROM trade_cal WHERE is_open=1"
        params = {}
        if today:
            sql += " AND cal_date <= :d"
            params["d"] = today
        r = db.execute(_t(sql), params).fetchone()
        return str(r[0]) if r and r[0] else ""
    except Exception:  # noqa: BLE001
        return ""
    finally:
        db.close()


def _count_trade_days(after: str, upto: str) -> int:
    """(after, upto] 之间的交易日个数（含端点）。任一为空/顺序不对 → 0。"""
    if not after or not upto or after >= upto:
        return 0
    from sqlalchemy import text as _t
    db = _cal_conn()
    if db is None:
        return 0
    try:
        r = db.execute(_t(
            "SELECT COUNT(*) FROM trade_cal WHERE is_open=1 AND cal_date > :a AND cal_date <= :b"),
            {"a": after, "b": upto}).fetchone()
        return int(r[0] or 0)
    except Exception:  # noqa: BLE001
        return 0
    finally:
        db.close()


def _next_trade_date(after: str) -> str:
    """after 之后的下一个交易日（用于 stk_limit 的"允许领先"判定）。"""
    if not after:
        return ""
    from sqlalchemy import text as _t
    db = _cal_conn()
    if db is None:
        return ""
    try:
        r = db.execute(_t(
            "SELECT MIN(cal_date) FROM trade_cal WHERE is_open=1 AND cal_date > :a"),
            {"a": after}).fetchone()
        return str(r[0]) if r and r[0] else ""
    except Exception:  # noqa: BLE001
        return ""
    finally:
        db.close()


def _count_rows_on(table: str, date_col: str, date_str: str) -> int:
    """数某表某交易日有多少行（用于覆盖率抽查）。失败返回 0。"""
    from sqlalchemy import text as _t
    db = _cal_conn()
    if db is None:
        return 0
    try:
        r = db.execute(_t(f"SELECT COUNT(*) FROM {table} WHERE {date_col}=:d"),
                       {"d": date_str}).fetchone()
        return int(r[0] or 0)
    except Exception:  # noqa: BLE001
        return 0
    finally:
        db.close()


def effective_latest_probe(table: str) -> dict:
    """「有效交易日」探针：默认查**真实库**（可注入，见 scripts/verify_*.py）。

    ★ 独立成函数是为了**可注入**。原因：`_check_data_gate` 的入参 `dv` 是调用方给的
      **快照**，可它又需要实时查库才能算「有效交易日」⇒ **两个数据源混用**。
      后果（2026-09-28 实测）：单测注入的打桩 `dv` 被真实库里 `stk_limit` 的半截日
      （2361 行 / 下限 5000）覆盖 ⇒ 门禁单测 5 项变红，**与代码无关**，
      纯粹是"绿不绿取决于当天库里有没有半残表"。
      需要隔离时替换本函数即可（`runner.` 与 `data_validator.` 两处属性皆可打桩）。
    """
    from app.backtest.data_validator import effective_latest_date
    return effective_latest_date(table)


def _check_data_gate(dv: dict, today: str = "", latest_td: str | None = None,
                     end_date: str = "") -> dict:
    """数据门禁：核心表日期**按交易日**校验 + 日期格式校验 + 一致性校验。

    Args:
        dv: data_validator 的结果（含 tables[].max_date）
        today: 判定基准日（YYYYMMDD，空=取系统当天）—— 便于测试与重放
        latest_td: 测试注入：直接指定"最近交易日"（None=查 trade_cal）
        end_date: 回测窗口末端（YYYYMMDD，空=以最新为准）。
            ★ 有它就以「≤ end_date 的最近交易日」为判定基准 —— **历史区间回测不该被
            "今天的数据没采完"拦住**（研究窗口与当日采集本来无关）。

    Returns:
        {"blocked", "reasons", "warnings", "dates", "anchor", "latest_trade_date", "gate"}
    """
    reasons: list[str] = []
    warnings: list[str] = []
    dates: dict[str, str] = {}
    tables = (dv or {}).get("tables") or {}
    for t in GATE_CORE_TABLES:
        info = tables.get(t) or {}
        md = str(info.get("max_date") or "")
        dates[t] = md
        if not info.get("exists"):
            reasons.append(f"核心表 {t} 不存在")
        elif not md:
            reasons.append(f"核心表 {t} 无日期数据")
        elif not _DATE_RE.match(md):
            # 关键：不再把非法值拿去比大小（否则会报出 "最新 920992.B" 这种无意义结论）
            reasons.append(
                f"数据损坏：{t} 的日期最大值 {md!r} 不是合法 YYYYMMDD（疑似列错位/脏数据）"
                f" → 请先修数据（同类问题可用 scripts/fix_*_columns.py 排查）"
            )

    # 最近交易日（周末/节假日自动跳过）
    lt = latest_td if latest_td is not None else _latest_trade_date(
        today or time.strftime("%Y%m%d"))
    if not lt:
        warnings.append("trade_cal 查不到最近交易日 → 日期门禁降级为『仅格式+一致性』校验")

    # ★ 判定基准（base）= **回测窗口末端优先**。
    #   原实现一律用系统当天 ⇒ 跑一段 2019 年的历史回测，也会因"今天的采集没跑完"被拦
    #   （这是用户反馈"太严格"里**正当**的那部分：研究窗口与当日采集本来无关）。
    #   给了 end_date 就用「≤ end_date 的最近交易日」，并**以 lt 为上限** ——
    #   窗口末端若落在未来，不能拿日历里的未来交易日去比。
    base, base_label = lt, "最近交易日"
    if end_date and _DATE_RE.match(end_date):
        w = _latest_trade_date(end_date)
        if w:
            base = min(w, lt) if lt else w
            base_label = f"回测窗口末端 {end_date} 的最近交易日"
    # "最新口径"：只有走线上链路（无历史窗口）时，"锚定表领先最新交易日"才算来源异常
    live_basis = bool(base and lt and base == lt)

    def _lag_td(a: str, b: str) -> int:
        return _count_trade_days(a, b)

    # ①-a ★ 先按「**有效交易日**」修正各核心表的 max_date（plans/24 §11.34.5）：
    #   只看 MAX 会把一个"半截日"（实测 adj_factor 20260921 只有 443 行）当成"领先"，
    #   于是门禁永远不通过、且报错原因误导排查。改为「行数 ≥ 自然基数下限」的最近日期。
    #   默认开启（这是**修正**）；可用可进化参数 `data_gate_use_effective` 回退为旧行为。
    eff_info: dict[str, dict] = {}
    if _bp("data_gate_use_effective", GATE_USE_EFFECTIVE):
        for t in GATE_CORE_TABLES:
            if not _DATE_RE.match(dates.get(t, "")):
                continue
            e = effective_latest_probe(t)
            eff_info[t] = e
            if e["effective"] and e["effective"] != e["raw"]:
                n_raw = e["rows"].get(e["raw"], 0)
                warnings.append(
                    f"{t} 最新日 {e['raw']} 只有 {n_raw} 行（< 下限 {e['min_rows']}）"
                    f"⇒ 按**有效交易日** {e['effective']} 参与门禁比较"
                )
                dates[t] = e["effective"]

    # ① 锚定表必须同一天（行情/复权/估值是一套链）
    anchor_dates = [dates[t] for t in GATE_ANCHOR_TABLES if _DATE_RE.match(dates.get(t, ""))]
    if anchor_dates:
        if len(set(anchor_dates)) > 1:
            detail = ", ".join(f"{t}({dates[t]})" for t in GATE_ANCHOR_TABLES)
            reasons.append(
                f"锚定表未对齐（同一条数据链，必须同日）：{detail}"
                f"（回测会用到不完整数据，特征/结局窗口错位）"
            )
        anchor = min(anchor_dates)
        # ★ 锚定表里有哪些表是**因"最新日没写完"而回退**到有效交易日的。
        #   这类"落后"的成因是"当天采集尚未完成"，**不是数据缺失** ——
        #   按 plans/24 §11.34.5 的本意，此时应把 as-of 回退到该完整交易日并**放行**
        #   （这正是「有效交易日」机制想解决的事，但原实现回退后仍按 lt 比 ⇒ 照样拦死）。
        rolled_back = [
            t for t in GATE_ANCHOR_TABLES
            if (eff_info.get(t) or {}).get("effective")
            and eff_info[t]["effective"] != eff_info[t].get("raw")
        ]
        # ② 锚定表 vs 判定基准：按交易日算落后
        if base:
            lag = _lag_td(anchor, base)
            max_lag = int(_bp("data_gate_max_lag_td", GATE_MAX_LAG_TD))
            if anchor > base:
                ahead = _lag_td(base, anchor)
                if ahead > 0 and live_basis:
                    reasons.append(
                        f"锚定表日期 {anchor} 领先最近交易日 {base} 共 {ahead} 个交易日"
                        f"（数据来源异常，需人工确认）"
                    )
                elif ahead > 0:
                    warnings.append(
                        f"锚定表日期 {anchor} 已覆盖到{base_label} {base} 之后"
                        f"（多 {ahead} 个交易日）→ 数据覆盖充足，放行"
                    )
            elif lag > max_lag:
                if rolled_back:
                    warnings.append(
                        f"锚定表日期 {anchor} 落后{base_label} {base} 共 {lag} 个交易日，"
                        f"但成因是**最新交易日数据未写完**（{'、'.join(rolled_back)} 的最新日行数"
                        f"低于自然基数下限）⇒ as-of 自动回退到完整交易日 {anchor}，**不阻断回测**。"
                        f"回测结论仅代表 ≤{anchor} 的样本；补齐当日数据前请勿用于实盘。"
                    )
                else:
                    reasons.append(
                        f"锚定表落后 {lag} 个交易日（数据 {anchor}，{base_label} {base}；"
                        f"已按交易日口径计算，周末/节假日不计入）→ 请先补齐/重采数据"
                    )
            else:
                warnings.append(f"锚定表日期 {anchor} = {base_label}（按交易日口径校验通过）")
        # ③ stk_limit 允许领先（次日涨跌停价），不允许落后锚定表
        sl = dates.get("stk_limit", "")
        if _DATE_RE.match(sl):
            ahead_allow = int(_bp("data_gate_allow_ahead_td", GATE_ALLOW_AHEAD_TD))
            # ★ 必须区分两种**病因完全不同**的"落后"，否则会**永久卡死回测**、且报错把人
            #   引向"补最新数据"（那儿根本没有可补的东西）：
            #   (a) **真落后**：raw max_date 本身就旧 ⇒ 补采即可 ⇒ 仍然阻断；
            #   (b) **覆盖率缺口**（新=raw 那天行数不达标，有效交易日被回退 ⇒ 看着像"落后"）：
            #       实测 20260928 仅 2361 行，缺口是**整个深市/创业板/北交所**
            #       （沪市 600/601/603/605/688/900 全在，000/001/002/003/300/301/302/920 全无）
            #       = 沪市股票数恰好 2361 ⇒ 采集分片只跑了 SSE。
            #       这是**项目早已记录的已知限制**：sentiment.py 明写"stk_limit 覆盖率实测仅
            #       42.5%…仅用于个股级校验"，且本函数 ④ 从 2026-09-20 起就把它定为 warning。
            #       ⇒ 与 ④ 同口径：记 warning，**不阻断**（否则线上结构性缺口头一次出现就
            #         把回测全卡死，事实也正是如此）。
            sl_raw = str((tables.get("stk_limit") or {}).get("max_date") or "")
            if sl_raw and sl_raw != sl:
                _e = eff_info.get("stk_limit") or {}
                _n = (_e.get("rows") or {}).get(sl_raw, "?")
                warnings.append(
                    f"stk_limit 最新日 {sl_raw} 仅 {_n} 行（低于自然基数下限 "
                    f"{_e.get('min_rows', '?')}）⇒ 有效交易日回退为 {sl}；"
                    f"成因是**采集不完整**（非日期落后），属已知限制（见 sentiment.py），不阻断回测；"
                    f"但『涨停不可买』过滤对缺失个股不生效，绩效会**偏乐观**"
                )
            elif sl < anchor:
                lag = _lag_td(sl, anchor)
                if lag > 0:
                    reasons.append(
                        f"stk_limit（{sl}）落后锚定表（{anchor}）{lag} 个交易日"
                        f"（真落后：最新日无数据 → 请补齐/重采）"
                    )
            else:
                # 允许的最晚日期 = anchor 之后的第 ahead_allow 个交易日
                allowed = anchor
                for _ in range(max(0, ahead_allow)):
                    nxt = _next_trade_date(allowed)
                    if not nxt:
                        break
                    allowed = nxt
                if allowed and sl > allowed:
                    reasons.append(
                        f"stk_limit（{sl}）领先锚定表（{anchor}）超过 {ahead_allow} 个交易日"
                        f"（允许到 {allowed}）"
                    )
    # ④ 覆盖率抽查（warning，不阻塞）：日期对上了 ≠ 数据完整。
    # 实测（2026-09-20）：stk_limit 最新交易日只有 2361 行，而 daily 同日有 5565 只（09-17 是 5644 行）
    # → "日期最新"却"只采了一部分"，会影响『涨停不可买』过滤（loader.load_limit_prices）。
    # ★ 抽查必须看**最新日（raw）**而不是"有效交易日"：有效交易日恰恰是被回退过的那个，
    #   拿它去数行数会"用完整日证明数据完整"，把真正的问题盖掉。
    if anchor_dates:
        sl_d = str((tables.get("stk_limit") or {}).get("max_date")
                   or dates.get("stk_limit", ""))
        if _DATE_RE.match(sl_d):
            n_limit = _count_rows_on("stk_limit", "trade_date", sl_d)
            n_daily = _count_rows_on("daily", "trade_date", sl_d)
            if n_daily and n_limit < 0.6 * n_daily:
                warnings.append(
                    f"stk_limit 在 {sl_d} 仅 {n_limit} 行（daily 同日 {n_daily} 只）→ 涨跌停价数据不完整，"
                    f"建议重采该日 stk_limit（影响『涨停不可买』过滤准确性）"
                )
    # ⑤ 总状态：overall=error 记为 warning（门禁自身已完成结构性校验，不再被历史缓存一票否决）
    if str((dv or {}).get("overall") or "") == "error":
        warnings.append("data_validator overall=error（详见 data_validation.issues；门禁按结构校验放行）")

    return {
        "blocked": bool(reasons),
        "reasons": reasons,
        "warnings": warnings,
        "dates": dates,
        "anchor": anchor_dates[0] if len(set(anchor_dates)) == 1 else (min(anchor_dates) if anchor_dates else ""),
        "latest_trade_date": lt,
        "gate": "blocked" if reasons else "passed",
    }


# A/B 方案 C 的口径（plans/23 §2.4 T2 修复）
# 修复前：C = "命中 ≥1 条条件规则"的样本。而条件表由"30 个条件特征 × 2 方向 × 4 分位"
# 各留一条规则构成，**单条规则都合法，但规则并集必然覆盖绝大多数样本** →
# 实测 424/424 全命中 → C ≡ A → accuracy_lift=0.0、p=1.0、决策永远 No-Go（且无人知道原因）。
# 修复后：C = **按综合上涨概率排序的前 C_TARGET_COVERAGE 比例**（与每日推荐"排序取 TOP-N"
# 的实际用法一致），并要求 C 组概率显著高于其余样本，否则判 A/B 无效。
C_TARGET_COVERAGE = 0.30   # C 组目标覆盖率（前 30%，与线上推荐取 TOP 的口径一致）
MIN_C_PROB_GAP = 0.02      # C 组与其余样本的平均概率差下限（低于此视为"概率无区分度"）
MAX_C_COVERAGE = 0.90      # C 覆盖率超过此值 → 无筛选力，直接判 A/B 无效


@dataclass
class BacktestConfig:
    """回测配置。"""
    start_date: str = "20100101"       # 数据起始
    end_date: str = ""                 # 数据截止（空=最新）
    max_stocks: int | None = None      # 限制股票数（调试用）
    min_list_days: int = 60
    # 形态/分流阈值可在 form_classifier / outcome_tracker 常量中调整


@dataclass
class BacktestResult:
    """回测结果。"""
    config: dict = field(default_factory=dict)
    samples: dict = field(default_factory=dict)          # 样本统计
    patterns: list = field(default_factory=list)          # 正样本模式（特征+元信息）
    probability_table: dict = field(default_factory=dict) # 条件概率表
    attribution: dict = field(default_factory=dict)       # 归因分析
    performance: dict = field(default_factory=dict)       # 绩效指标
    ab_validation: dict = field(default_factory=dict)     # A/B 验证结果
    vector_store_stats: dict = field(default_factory=dict)  # 模式库统计
    recommendations: list = field(default_factory=list)   # 每日推荐（演示）
    segments: list = field(default_factory=list)          # 全部候选行情段（调试）
    data_validation: dict = field(default_factory=dict)   # 数据完整性校验
    clusters: dict = field(default_factory=dict)          # 同形态聚类 + 簇内归因
    entry_analysis: dict = field(default_factory=dict)    # 最佳入场点分析
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "config": self.config,
            "samples": self.samples,
            "probability_table": self.probability_table,
            "attribution": self.attribution,
            "performance": self.performance,
            "ab_validation": self.ab_validation,
            "vector_store_stats": self.vector_store_stats,
            "recommendations": self.recommendations,
            "data_validation": self.data_validation,
            "clusters": self.clusters,
            "entry_analysis": self.entry_analysis,
            "pattern_count": len(self.patterns),
            "elapsed": round(self.elapsed, 2),
        }


def _build_feature_matrix(feature_vectors: list) -> tuple[pd.DataFrame, list, pd.DataFrame]:
    """将 FeatureVector 列表转为特征矩阵 + 元信息列表 + 条件参数矩阵（原始值）。

    架构（用户设计）：向量特征只保留形态（VECTOR_FEATURES），归一化用于相似度；
    条件参数（CONDITION_FEATURES：基本面/筹码/资金/事件等）不归一化，单独以原始值
    构建 condition_table（研究"越来越大/越来越小对上涨的影响"）。

    Returns:
        (vector_mat, meta, condition_mat)
    """
    vec_names = VECTOR_FEATURES
    cond_names = CONDITION_FEATURES
    vec_rows = [fv.to_array(vec_names) for fv in feature_vectors]
    cond_rows = [fv.to_array(cond_names) for fv in feature_vectors]
    meta = []
    for f in feature_vectors:
        m = {
            "ts_code": f.ts_code,
            "form_type": f.form_type,
            "label": f.label,
            "t0_idx": f.t0_idx,
        }
        # 携带 outcome 结果字段（存在 features 中的下划线开头的内部键）
        for k, v in f.features.items():
            if k.startswith("_outcome_"):
                m[k] = v
        meta.append(m)
    if not vec_rows:
        return (pd.DataFrame(columns=vec_names), meta, pd.DataFrame(columns=cond_names))
    vec_mat = pd.DataFrame(vec_rows, columns=vec_names)
    cond_mat = pd.DataFrame(cond_rows, columns=cond_names)
    return vec_mat, meta, cond_mat


def run_backtest(cfg: BacktestConfig | None = None, progress_cb=None) -> BacktestResult:
    """执行完整回测流水线。

    Args:
        cfg: 回测配置
        progress_cb: 可选进度回调 fn(stage: str, pct: float)

    Returns:
        BacktestResult
    """
    cfg = cfg or BacktestConfig()
    result = BacktestResult(config=cfg.__dict__)
    t0 = time.time()

    def _progress(stage: str, pct: float):
        if progress_cb:
            progress_cb(stage, pct)
        logger.info(f"[backtest] {stage} {pct:.0%}")

    # ── 1. 数据校验 + 股票池 ──
    _progress("加载股票池", 0.02)
    pool = load_stock_pool()
    if pool.empty:
        logger.error("回测中止：股票池为空，请先采集 stock_basic 数据")
        result.elapsed = time.time() - t0
        return result

    # ── 1.5 数据完整性校验（采集前必须通过，见 3.9）──
    _progress("数据完整性校验", 0.05)
    try:
        result.data_validation = run_data_validation(cfg.start_date)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"数据完整性校验失败: {exc}")
        result.data_validation = {"overall": "error", "issues": [{"level": "error", "message": str(exc)}]}

    # ── 1.6 数据门禁（plans/23 T7/W3）：核心表日期不一致 → 拒绝启动 ──
    gate = _check_data_gate(result.data_validation, end_date=cfg.end_date or "")
    result.data_validation["gate"] = gate
    if gate["blocked"]:
        logger.error(f"数据门禁未通过：{gate['reasons']}")
        if _bp("data_gate_strict", STRICT_DATA_GATE):
            logger.error("回测已中止（STRICT_DATA_GATE=True）：请先补齐/重采数据，避免产出不可信结论")
            result.performance = {
                "quality_gate": "error",
                "valid": False,
                "usable_for_decision": False,
                "issues": [{"level": "error", "code": "DATA_GATE_BLOCKED",
                            "message": "；".join(gate["reasons"])}],
            }
            result.elapsed = time.time() - t0
            return result

    # ── 1.7 本地镜像预热（plans/23 §4.5 W4：回测提速）──
    # 实测：逐股查 daily_basic/moneyflow 时 MySQL 冷读 1.8~2.0s/股（索引 988MB/965MB 装不进
    # buffer pool），单股 ~4s → 全量回测 ~80 分钟。镜像到本地 SQLite 后单股 ~20ms。
    # 这里做增量同步（数据未变时秒级返回"已是最新"）。
    mirror_info: dict = {"enabled": False}
    if _bp("extra_mirror_enabled", 1):
        try:
            from app.backtest.mirror_cache import ensure_mirror, stats as _mirror_stats
            _mr = ensure_mirror()
            mirror_info = {"enabled": True, "errors": _mr.get("errors") or [],
                           "tables": {k: {kk: vv for kk, vv in v.items() if kk != "error"}
                                      for k, v in (_mr.get("tables") or {}).items()}}
            if _mr.get("errors"):
                logger.warning(f"镜像预热部分失败（该表回退 MySQL）: {_mr['errors']}")
            mirror_info["stats"] = _mirror_stats()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"镜像预热失败（逐股查询回退 MySQL，功能不受影响）: {exc}")
            mirror_info = {"enabled": False, "error": str(exc)}
    result.vector_store_stats["mirror"] = mirror_info

    # ── 2. 召回扫描（全量 3 倍行情段）──
    _progress("召回扫描", 0.1)
    segments = scan_market(pool, start_date=cfg.start_date, end_date=cfg.end_date, max_stocks=cfg.max_stocks)
    result.segments = [
        {"ts_code": s.ts_code, "low_date": str(s.low_date.date()), "high_date": str(s.high_date.date()),
         "gain": round(s.gain, 3), "duration": s.duration}
        for s in segments
    ]
    logger.info(f"召回层：候选行情段 {len(segments)}")

    # ── 3~4. 按股票分类 + 分流（需要逐股加载全量数据）──
    _progress("形态分类+正负分流", 0.3)
    samples: list = []
    pattern_vectors: list = []
    entry_rows: list = []
    noisy_dropped = 0  # 精准度过滤剔除的噪音样本数（3.10）
    # 分组：segments 按 ts_code 分组，逐股处理
    from collections import Counter, defaultdict

    # T5（plans/23 §十三）：形态层解释力体检 —— U（无法归类）段占比 + 被否原因分布。
    # 实测（220 只）：U 段占候选段 48%，且 U 样本里 success/failure 的结构特征几乎相同 →
    # 说明"形态层"当前不产生区分信息，**必须让进化大脑看到这个事实**（而不是继续调参）。
    cls_total = 0
    u_reason = Counter()

    seg_by_code: dict[str, list] = defaultdict(list)
    for s in segments:
        seg_by_code[s.ts_code].append(s)

    codes = list(seg_by_code.keys())
    for i, code in enumerate(codes):
        try:
            df = prepare_stock_data(code, cfg.start_date, cfg.end_date)
            if df.empty:
                continue
            segs = seg_by_code[code]
            # 去重叠后重新扫描（确保 t0 等下标正确）
            segs = dedupe_segments(segs)
            classified = classify_batch(df, segs)
            cls_total += len(classified)
            for _c in classified:
                if _c.form_type == FORM_U:
                    u_reason[str((getattr(_c, "detail", {}) or {}).get("reason") or "unclassified")] += 1
            outcomes = batch_classify_outcomes(df, classified)
            # 样本精准度过滤（对齐 plans/04 3.10：⑲ T0 封板不可买 / ⑬ 结局截断 /
            # ⑥ 长期停牌复牌跳空 / ⑨ 停牌过多）——宁少而精，勿多而失真
            pre_filter_n = len(outcomes)
            outcomes = filter_noisy_samples(df, outcomes)
            noisy_dropped += pre_filter_n - len(outcomes)

            # 只用现有数据库数据：按 t0 时点加载增强特征（换手/资金/财务/筹码），严格防未来函数
            target_outcomes = [o for o in outcomes if o.label in (LABEL_SUCCESS, LABEL_FAILURE_A, LABEL_FAILURE_B)]
            extra_map = {}
            if outcomes:
                try:
                    # 用户补充：为全部 outcome 提取增强特征（含 st_risk_flag），用于剔除"即将*ST 高风险"样本
                    extra_map = build_extra_map(code, df, [o.t0_idx for o in outcomes])
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"加载增强特征失败 {code}: {exc}")

            # 用户补充：排除"即将*ST 高风险"样本（净资产<0 / 连续两期亏损）——回测正负样本均剔除，
            # 避免退市/戴帽风险污染统计。名称含 ST 的历史股（is_ever_st）仍保留（非特别高风险不排除）。
            st_dropped = 0
            if extra_map:
                keep = []
                for o in outcomes:
                    ex = extra_map.get(o.t0_idx, {})
                    if ex.get("st_risk_flag") and float(ex["st_risk_flag"]) > 0:
                        st_dropped += 1
                        continue
                    keep.append(o)
                if st_dropped:
                    outcomes = keep
                    target_outcomes = [o for o in outcomes if o.label in
                                       (LABEL_SUCCESS, LABEL_FAILURE_A, LABEL_FAILURE_B)]
                    logger.info(f"回测排除即将*ST样本 {code}: {st_dropped} 个")
            samples.extend(outcomes)

            # 对 success/failure 样本提取特征（独立模块 pattern_miner / failure_miner）
            pattern_vectors.extend(mine_positive_patterns(df, outcomes, extra_map=extra_map))
            pattern_vectors.extend(mine_failure_patterns(df, outcomes, extra_map=extra_map))

            # 最佳入场点分析（成功+失败通用，4.3）
            entry_rows.extend(analyze_batch(df, target_outcomes))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"run_backtest 处理 {code} 失败: {exc}")
            continue

        if (i + 1) % 200 == 0:
            _progress("形态分类+正负分流", 0.3 + 0.5 * (i + 1) / max(len(codes), 1))

    result.samples = sample_summary(samples)
    # T5：把"形态层解释力"写进产物（L0 健康神经元据此报警 → 进化大脑可据证据决策）
    _u_n = int(sum(u_reason.values()))
    _u_ratio = round(_u_n / cls_total, 4) if cls_total else 0.0
    result.samples["U_segments"] = _u_n
    result.samples["U_ratio"] = _u_ratio
    result.samples["U_reasons"] = u_reason.most_common(10)
    if _u_ratio > U_RATIO_WARN:
        logger.warning(
            f"[backtest] 形态层解释力不足：U（无法归类）段占 {_u_ratio:.1%}（警戒 {U_RATIO_WARN:.0%}），"
            f"原因 Top: {u_reason.most_common(3)}；U 样本将不参与同形态归因/聚类"
        )
    logger.info(f"正负分流完成: {result.samples}")
    if noisy_dropped:
        logger.info(f"精准度过滤剔除噪音样本 {noisy_dropped} 个（⑲ T0封板/⑬结局截断/⑥长期停牌复牌/⑨停牌过多）")

    # ── 5. 特征矩阵 + 归一化（按类型差异化，参数固化供每日推荐复用）──
    # 架构（用户设计）：向量特征只留形态（VECTOR_FEATURES，33 维）归一化供相似度；
    # 条件参数（CONDITION_FEATURES，30 维）不归一化，以原始值单独构建 condition_table。
    _progress("特征提取+归一化", 0.85)
    feat_mat, meta, cond_mat = _build_feature_matrix(pattern_vectors)
    normalizer = None
    # 归一化后的形态向量矩阵（供向量库相似度 + 归因）
    if not feat_mat.empty:
        normalizer = FeatureNormalizer.fit(feat_mat.to_numpy(), names=VECTOR_FEATURES)
        norm = normalizer.transform(feat_mat.to_numpy())
        feat_df = pd.DataFrame(norm, columns=VECTOR_FEATURES)
        try:
            save_normalizer(normalizer)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"保存归一化参数失败: {exc}")
    else:
        feat_df = pd.DataFrame(columns=VECTOR_FEATURES)

    # ── 6. 条件概率统计（用户核心需求）──
    # 用户架构：基本面/筹码/资金条件参数（bps/人均持股/筹码集中度/ROE 等）不进向量、
    # 不归一化，作为"采样条件"在原始值分位切分下统计"条件→T+5上涨率"，固化 condition_table。
    _progress("条件概率统计", 0.9)
    if not feat_df.empty and meta:
        # 上涨标签：T+5 > 5%（主评估口径）
        t5 = np.array([m.get("_outcome_t5", np.nan) for m in meta]) if meta else np.array([])
        labels = np.array([1 if (r is not None and r > 0.05) else 0 for r in t5], dtype=float)
        # 形态向量条件概率表（历史兼容：向量表仅用于 A/B 验证；主评分改用条件参数表）
        table = build_probability_table(feat_df, labels)
        # 采样条件参数表（主评分）：原始值分位切分，不进向量
        cond_table = build_condition_table(cond_mat, labels)
        try:
            save_probability_table(table)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"保存条件概率表失败: {exc}")
        try:
            save_condition_table(cond_table)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"保存采样条件参数表失败: {exc}")
        result.probability_table = {
            "baseline_prob": table.baseline_prob,
            "sample_count": table.sample_count,
            "rules": table.to_records(),
            "condition_rules": cond_table.to_records(),
            "sample_composition": {
                "success": result.samples.get("success", 0),
                "near_success": result.samples.get("near_success", 0),
                "failure_A": result.samples.get("failure_A", 0),
                "failure_B": result.samples.get("failure_B", 0),
            },
            "note": "向量表 baseline_prob 为样本内 T+5>5% 占比；主评分使用 condition_table（条件参数原始值分位）",
        }
    else:
        result.probability_table = {"baseline_prob": 0.0, "sample_count": 0, "rules": []}
        table = ProbabilityTable()  # 确保后续 A/B/推荐引用不 NameError
        cond_table = ProbabilityTable()

    # ── 7. 同形态聚类 + 归因分析（成功 vs 失败，同簇内对比，3.5/4.5）──
    # 架构（用户设计）：归因分析条件参数（bps/筹码/资金/事件等）"越来越大/越来越小
    # 对上涨的影响"，回答哪些条件区分成功/失败；形态（feat_df）已用于向量相似度。
    _progress("同形态聚类+归因分析", 0.95)
    if not cond_mat.empty and meta:
        succ = np.array([1 if m["label"] == LABEL_SUCCESS else 0 for m in meta], dtype=float)
        # 整体归因（对照基线）：对条件参数矩阵归因
        att = analyze_attribution(cond_mat, succ)
        result.attribution = att.to_dict()
        # 固化归因知识 → Agent 精筛加载参考（attribution_kb.json）
        try:
            save_attribution_kb(att)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"固化归因知识失败: {exc}")
        # 同形态聚类（按 form_type 分组 + 起爆前特征 KMeans，确保正负可比）
        try:
            cres = cluster_by_form(feat_df, meta)
            catt = cluster_attribution(cond_mat, meta, cres.cluster_ids)
            result.clusters = {
                "cluster_summary": cres.to_dict(),
                "per_cluster_attribution": catt,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"同形态聚类/簇内归因失败: {exc}")
            result.clusters = {}
    else:
        result.attribution = {}
        result.clusters = {}

    # ── 8. 绩效指标（plans/23 §2.4 T1/T3 修复：口径分层 + 按交易日等权净值 + 区分度检验）──
    # 修复前的问题：旧口径把每笔样本的 T+5 顺序累乘当净值（同一天几十笔 → 净值爆炸，
    # 曾产出 cum_return=9.5e22），且把"事后已爆发样本的 T+5 正收益比例"当成命中率
    # （win_rate=1.0）。现在明确标注 scope=条件集口径，并禁止用于决策。
    _progress("绩效计算", 0.98)
    if meta:
        t5_all = [m.get("_outcome_t5") for m in meta]
        t20_all = [m.get("_outcome_t20") for m in meta]
        dates_all = [m.get("_outcome_t0_date") for m in meta]
        # T4 第一步（plans/23 §2.4）：同交易日全市场等权 T+5 基准 → 让『超额』可计算
        bench_t5 = None
        try:
            from app.backtest.benchmark import benchmark_for_dates
            _bm = benchmark_for_dates([d for d in dates_all if d])
            if _bm:
                bench_t5 = [_bm.get(d) for d in dates_all]
                logger.info(f"市场基准已加载：{len(_bm)} 个交易日（用于计算真实超额）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"市场基准计算失败（不影响主流程，仅缺超额参照）: {exc}")
        # T4 第二步（plans/23 §4.1 L2）：**同口径对照组** —— 同交易日"无启动迹象"的股票。
        # 比"全市场等权"更严格：它已剔除同期正在启动的股票，因此回答的是
        # "我们的信号是否优于随便挑一批没启动的股票"（形态层到底有没有 alpha）。
        bench_src = "market"
        control_map: dict = {}
        try:
            control_map = controls_for_dates(
                [d for d in dates_all if d],
                sample_n=int(_bp("bt_control_sample_n", 400)),
                warmup_pct=float(_bp("bt_control_warmup_pct", 0.15)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"对照组采样失败（不影响主流程，仅缺同口径对照）: {exc}")
        if control_map:
            ctl_series = [control_map.get(d, {}).get("avg_t5") for d in dates_all]
            if any(v is not None for v in ctl_series):
                bench_t5 = ctl_series        # 同口径对照优先作为基准（更严格）
                bench_src = "control"
        perf = compute_metrics(t5_all, t20_all, dates=dates_all,
                               scope=SCOPE_CONDITIONAL, benchmark_t5=bench_t5)
        result.performance = perf.to_dict()
        result.performance["benchmark_source"] = bench_src
        result.performance["benchmark_note"] = (
            ("benchmark_* = 同一交易日『无启动迹象』对照组（排除 t0 前 20 日涨幅 >"
             f"{float(_bp('bt_control_warmup_pct', 0.15)):.0%} 者）的等权 T+5（含成本）；"
             if bench_src == "control" else
             "benchmark_* = 同一交易日的全市场等权 T+5（抽样 300 只、含成本）；")
            + "lift_t5_pos = 本样本组 T+5 正收益率 − 同期基准，是『条件集有无真实超额』的判据"
        )
        # T4 第二步判据（plans/23 §4.1 L2）：比例检验 —— 样本组是否**显著优于**同口径对照
        if control_map:
            pooled = pool_controls(control_map, [d for d in dates_all if d])
            if pooled["n"] > 0:
                ctl = compare_to_control(t5_all, pooled["pos_ratio"], pooled["n"])
                ctl["control_dates"] = pooled["dates"]
                ctl["control_avg_t5"] = pooled["avg_t5"]
                result.performance["vs_control"] = ctl
                if ctl["passed"]:
                    logger.info(f"[backtest] 同口径对照通过：{ctl['message']}")
                else:
                    logger.error(
                        f"[backtest] 同口径对照未通过（形态层无 alpha，需回到特征/标签重做）：{ctl['message']}"
                    )
        # 口径③：正负样本区分度检验（T3）——标签失效则归因/概率表/模式库全部不可信
        succ_t5 = [m.get("_outcome_t5") for m in meta if m.get("label") == LABEL_SUCCESS]
        fail_t5 = [m.get("_outcome_t5") for m in meta
                   if m.get("label") in (LABEL_FAILURE_A, LABEL_FAILURE_B)]
        disc = check_discrimination(succ_t5, fail_t5)
        result.performance["discrimination"] = disc
        if not disc["passed"]:
            logger.error(
                f"[backtest] 正负样本区分度未通过（标签体系失效，归因/条件概率表不可信）：{disc['message']}"
            )
        # T1b：明确标注"该口径不可用于线上/进化决策"（推荐口径需对照组，见 plans/23 §4.1）
        result.performance["usable_for_decision"] = False
        result.performance["decision_note"] = (
            "条件集口径：样本来自『事后已爆发的 3 倍行情段』，T+5 正收益比例天然接近 1，"
            "不是推荐命中率；严禁用于上线 Gate 与进化主指标。"
        )
    else:
        result.performance = {
            "quality_gate": "error",
            "valid": False,
            "usable_for_decision": False,
            "issues": [{"level": "error", "code": "NO_SAMPLES", "message": "无样本，未产出任何绩效指标"}],
        }

    # ── 8.5 最佳入场点分析（4.3）──
    _progress("入场点分析", 0.985)
    result.entry_analysis = aggregate_entry_results(entry_rows)
    # 固化"成功组各形态最佳入场"→ 每日推荐附加操作建议（无后视偏差口径）
    try:
        guidance = build_entry_guidance(entry_rows)
        save_entry_guidance(guidance)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"固化入场指引失败: {exc}")

    result.patterns = [{"ts_code": f.ts_code, "form_type": f.form_type, "label": f.label} for f in pattern_vectors]

    # ── 9. A/B 验证（对照组 A 全样本基线 vs C 采样条件表命中）──
    # 方案 C = "命中 ≥1 条采样条件高胜率规则（bps/筹码/资金等原始值分位）"的样本；
    # 条件参数不进向量，作为回测的参考统计条件（用户架构）。规则未选中的样本不计入 C。
    _progress("A/B 验证", 0.99)
    if meta and not cond_mat.empty:
        t5_list = [m.get("_outcome_t5") for m in meta]
        t20_list = [m.get("_outcome_t20") for m in meta]
        plan_c_t5, plan_c_t20 = t5_list, t20_list
        c_mask: list[bool] = []
        coverage = 0.0
        prob_gap = 0.0
        rule_hits = 0
        try:
            cond_arr = cond_mat.to_numpy()
            probs = np.zeros(len(cond_arr), dtype=float)
            for i in range(len(cond_arr)):
                sf = {CONDITION_FEATURES[j]: float(cond_arr[i][j]) for j in range(len(CONDITION_FEATURES))}
                prob, hits = score_by_probability_table(sf, cond_table)
                probs[i] = float(prob)
                if hits:
                    rule_hits += 1
            # C = 概率最高的前 C_TARGET_COVERAGE 比例（与线上"排序取 TOP"口径一致）
            # 覆盖率可进化（plans/23 §十二）：进化大脑可调 C 组严苛度
            _c_cov = float(_bp("bt_c_target_coverage", C_TARGET_COVERAGE))
            k = max(1, int(round(len(probs) * _c_cov))) if len(probs) else 0
            order = np.argsort(-probs)
            c_mask = [False] * len(probs)
            for idx in order[:k]:
                c_mask[int(idx)] = True
            coverage = float(np.mean(c_mask)) if c_mask else 0.0
            in_c = probs[np.array(c_mask, dtype=bool)] if c_mask else np.array([])
            out_c = probs[~np.array(c_mask, dtype=bool)] if c_mask else np.array([])
            prob_gap = (float(in_c.mean()) - float(out_c.mean())) if len(in_c) and len(out_c) else 0.0
            plan_c_t5 = [t5_list[i] for i in range(len(t5_list)) if c_mask[i]]
            plan_c_t20 = [t20_list[i] for i in range(len(t20_list)) if c_mask[i]]
        except Exception as exc:  # noqa: BLE001
            # 不再静默退化为全样本（那会让 C≡A、决策门永远 No-Go）
            logger.error(f"A/B 方案 C 打分异常，本次 A/B 结果无效: {exc}")
            result.ab_validation = {
                "error": f"方案 C 打分失败: {exc}",
                "plan_a_count": len(t5_list),
                "plan_c_count": None,
                "decision": "No-Go",
                "reasons": [f"方案 C 打分异常，未参与对比: {exc}"],
            }
        else:
            if len(t5_list) == 0 or coverage > MAX_C_COVERAGE or coverage <= 0.0:
                # 覆盖率异常 → 无筛选力。明确判无效并给出可操作原因，
                # 而不是产出"提升 0.0pct / p=1.0"这种看起来正经、实则无信息的结论。
                logger.error(
                    f"A/B 无效：方案 C 覆盖率 {coverage:.1%}（要求 0 < cov ≤ {MAX_C_COVERAGE:.0%}）"
                )
                result.ab_validation = {
                    "plan_a_count": len(t5_list),
                    "plan_c_count": len(plan_c_t5),
                    "plan_c_rule_hits": rule_hits,
                    "c_coverage": round(coverage, 4),
                    "prob_gap": round(prob_gap, 4),
                    "decision": "No-Go",
                    "reasons": [
                        f"方案 C 覆盖率 {coverage:.1%} 异常（要求 0 < cov ≤ {MAX_C_COVERAGE:.0%}）："
                        "条件表无筛选力，A/B 对比无意义；请检查 condition_table 规则质量"
                    ],
                }
            elif prob_gap < float(_bp("bt_min_prob_gap", MIN_C_PROB_GAP)):
                # 概率无区分度：所有样本概率几乎一样（典型原因：条件规则过于宽泛，
                # 或所有规则都不命中而全部落到中性基线）→ 排序失效，A/B 无意义
                logger.error(
                    f"A/B 无效：C 组与其余样本平均概率差仅 {prob_gap:.4f}（要求 ≥ {MIN_C_PROB_GAP}），"
                    f"条件表对概率无区分度"
                )
                result.ab_validation = {
                    "plan_a_count": len(t5_list),
                    "plan_c_count": len(plan_c_t5),
                    "plan_c_rule_hits": rule_hits,
                    "c_coverage": round(coverage, 4),
                    "prob_gap": round(prob_gap, 4),
                    "decision": "No-Go",
                    "reasons": [
                        f"条件表概率无区分度（C 组与其余样本平均概率差 {prob_gap:.4f} < {MIN_C_PROB_GAP}）："
                        f"规则命中 {rule_hits}/{len(t5_list)}；请修规则质量（min_edge / max_coverage）"
                    ],
                }
            else:
                ab = run_ab_validation(
                    plan_a_t5=t5_list,
                    plan_c_t5=plan_c_t5,
                    plan_a_t20=t20_list,
                    plan_c_t20=plan_c_t20,
                )
                result.ab_validation = ab.to_dict()
                result.ab_validation["plan_a_count"] = len(t5_list)
                result.ab_validation["plan_c_count"] = len(plan_c_t5)
                result.ab_validation["plan_c_rule_hits"] = rule_hits
                result.ab_validation["c_coverage"] = round(coverage, 4)
                result.ab_validation["prob_gap"] = round(prob_gap, 4)
    else:
        result.ab_validation = {}

    # ── 10. 构建 ChromaDB 模式库 + 每日推荐演示 ──
    _progress("模式库+推荐", 1.0)
    if pattern_vectors and meta:
        # 向量库只存"形态核心"维度（33 维），避免资金/财务/事件等状态维度稀释形态相似度
        store = build_vector_store(pattern_vectors, meta, normalizer=normalizer, feature_names=MATCH_FEATURES)
        result.vector_store_stats = store.stats()
        # T8：附上本次构建元信息（reset / written_this_run），供报告核对
        # "模式库总数 == 本次写入条数"（防再次出现跨回测累积：实测曾 6,249 条 vs 424 样本）
        result.vector_store_stats.update(getattr(store, "last_build", {}) or {})
        # 每日推荐：用正样本特征作为"当前股票"演示打分（参考条件表 cond_table）
        try:
            recommender = DailyRecommender(vector_store=store, prob_table=cond_table)
            # 用正样本作为候选演示（实际每日推荐应扫描全市场；用固化参数归一化）
            if normalizer is not None:
                norm_full = normalizer.transform(feat_mat.to_numpy())
            else:
                norm_full = zscore_normalize(pattern_vectors)
            # 候选向量也用形态子集（与模式库同维度，才能余弦匹配）
            match_idx = [FEATURE_NAMES.index(n) for n in MATCH_FEATURES]
            norm = norm_full[:, match_idx]
            cands = []
            for i, f in enumerate(pattern_vectors):
                fdict = {k: v for k, v in f.features.items() if not k.startswith("_")}
                cands.append({
                    "ts_code": f.ts_code,
                    "name": "",
                    "form_type": f.form_type,
                    "feature_vector": norm[i],
                    "feature_dict": fdict,
                })
            result.recommendations = [r.to_dict() for r in recommender.recommend_market(cands, top_k=10)]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"推荐演示失败: {exc}")
            result.recommendations = []

    result.elapsed = time.time() - t0
    _progress("完成", 1.0)
    logger.info(f"回测完成，耗时 {result.elapsed:.1f}s，样本 {len(samples)}")
    return result
