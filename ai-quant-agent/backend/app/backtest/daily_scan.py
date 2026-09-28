"""每日全市场扫描（daily_scan）

对齐规划：plans/09-每日推荐详细规划.md 三/四/五
  每日推荐流水线（与回测完全对齐）：
    全市场股票池 → 逐股取数 → 风险预筛 → 当前时点形态识别（classify_current + 启动确认）
    → 提取 59 维特征（固化 FeatureNormalizer 归一化）
    → 形态粗筛（33 维正模式库相似度，确认形态进入候选池）
    → 条件概率打分（固化条件概率表，综合上涨概率）
    → 反向排除（负模式库相似度抑制）
    → 风险过滤（ST/高质押/涨停/流动性）
    → 按综合上涨概率排序 → TOP-30 → 榜单落盘 + 命中登记

  依赖：回测产出的固化产物
    - backend/data/feature_normalizer.json（归一化参数）
    - backend/data/probability_table.json（条件概率表）
    - ChromaDB 模式库（short_term_patterns_d33_*，33 维形态）
"""
from __future__ import annotations

import glob
import json
import os
import random
import re
import threading
import time

import numpy as np
import pandas as pd

from app.core.logger import logger
from app.backtest.loader import (
    load_stock_pool, prepare_stock_data, get_trade_dates, get_latest_daily_date,
)
from app.backtest.form_classifier import classify_current
from app.backtest.feature_extractor import (
    extract_features, FEATURE_NAMES, MATCH_FEATURES, VECTOR_FEATURES,
    CONDITION_FEATURES, load_normalizer,
)
from app.backtest.extra_feature_loader import build_extra_map
from app.backtest.threshold_analyzer import load_condition_table, score_by_probability_table
from app.backtest.entry_analyzer import load_entry_guidance, get_entry_advice
from app.backtest.vector_store import VectorStore
from app.backtest.risk_filter import check_risk
from app.backtest.rule_forms import detect_all
from app.backtest.rule_verifier import sync_rules_verified, load_form_leaderboard

DEFAULT_TOP_K = 30            # 可选榜单截断数（None = 显示全部候选）
LAMBDA_EXCLUDE = 0.5          # 负向抑制强度（与回测一致）
# 规划 15：主排序为 20 天向量单信号（up_probability）。
# 形态规则命中（hit_rules）不参与主排序，由 Agent 精筛 L2 强制保送进深度精筛 Top-50。
LAMBDA_NEG_RULE = 0.3         # 看跌规则负向抑制强度（规则已删，保留兼容）


def _prepare_replay(code: str, as_of: str):
    """**回放取数**（plans/25 F3）：截断 K 线到 as_of + **复权基准也锚到 as_of**。

    为什么必须锚复权基准：前复权价 = raw × adj_factor / **最新** adj_factor，
    而 [`load_adj_close()`](ai-quant-agent/backend/app/backtest/loader.py:96) 取的是**全表最新**因子
    → 未来发生的除权会改写 as_of 之前的整段价格序列（绝对价格、价格化参数如止损/追高上限都受影响）。
    传 `adj_base_date=as_of` 后，分母取 `<= as_of` 的最新因子，时序才与当年实盘一致。

    兼容性：若 loader 版本不支持 `adj_base_date`（旧代码），仍**照常截断 K 线**（不引入未来 K 线），
    但会打 warning 说明"复权基准未锚定"，绝不静默。
    """
    if not as_of:
        return prepare_stock_data(code)
    try:
        return prepare_stock_data(code, end_date=as_of, adj_base_date=as_of)
    except TypeError:
        logger.warning("[daily_scan] prepare_stock_data 不支持 adj_base_date → 复权基准未锚定到 as_of"
                       "（绝对价格类判断仍含轻微前视，见 plans/25 F3）")
        return prepare_stock_data(code, end_date=as_of)


def _evolve_param(name: str, default):
    """读取可进化参数（对齐 plans/17：进化中心热生效修改每日推荐逻辑）。

    读 evolution_config 的 params 表；失败/未登记回退默认值（J1 防御，不崩推荐主流程）。
    热路径调用量低（每只候选几次），直接读（evolution_config 内部已有 mtime 缓存 G5）。
    """
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param(name, default)
        return v if v is not None else default
    except Exception:  # noqa: BLE001
        return default

# 榜单落盘目录：backend/data/daily_recommend/
REPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "daily_recommend",
)

# 每日扫描进程内互斥锁（Bug8 修复）：三个入口会并发触发扫描——
#   main.py 定时任务(_run_daily_collection_job 采集完成自动扫描) /
#   data.py _auto_scan_after_collection(采集 done 后自动扫描) /
#   predictions.py trigger_scan(手动)——
# 但彼此都不检查对方状态，实测 3 线程并发处理同一批 5535 只股票，
# 每只被重复处理多次、互相争抢 MySQL 连接/IO，单次扫描被拖到 16 小时。
# 加非阻塞互斥：已有扫描在跑时，后到的入口直接返回（不重复执行），从根源防并发。
_SCAN_LOCK = threading.Lock()


def _report_path(scan_date: str, version: int | None = None) -> str:
    """榜单落盘路径：同一天多次推荐按版本分文件保存（不覆盖）。

    - version 为空 → 旧版无版本文件 daily_{scan_date}.json（仅兼容读取历史数据）
    - version 为 n → daily_{scan_date}_v{n}.json（新版版本化落盘）
    """
    if version is None:
        return os.path.join(REPORT_DIR, f"daily_{scan_date}.json")
    return os.path.join(REPORT_DIR, f"daily_{scan_date}_v{version}.json")


def next_report_version(prefix: str) -> int:
    """生成下一个版本号（prefix：不带路径、不带扩展名的文件名前缀）。

    同一天多次推荐不覆盖：统计 {prefix}_v*.json 已有最大版本号并返回 +1；
    即便已有旧无版本文件也统一从 v1 开始，绝不覆盖历史文件。

    Args:
        prefix: 如 daily_20260811（扫描榜单）或 daily_20260811_agent（Agent 最终榜单）

    Returns:
        下一个版本号（>=1）
    """
    base = os.path.basename(prefix)
    files = glob.glob(os.path.join(REPORT_DIR, f"{base}_v*.json"))
    versions = [
        int(m.group(1)) for f in files
        if (m := re.match(rf"^{re.escape(base)}_v(\d+)\.json$", os.path.basename(f)))
    ]
    return (max(versions) if versions else 0) + 1


def _compute_rule_signal(df, end_idx: int, verified_rules: dict, baseline: float, lambda_neg: float = LAMBDA_NEG_RULE) -> dict:
    """计算规则形态信号（信号 2）：rule_prob + 命中规则列表（含看跌抑制）。

    Args:
        df: prepare_stock_data 输出
        end_idx: 当前日
        verified_rules: 已验证规则 {key: leaderboard_dict}
        baseline: 全市场 T+1 基线（未命中任何规则时的中性值）
        lambda_neg: 看跌规则负向抑制强度

    Returns:
        {"rule_prob": float, "hit_rules": [ {key,name,hit_rate_t1,form_type} ]}
    """
    hits = detect_all(df, end_idx, verified_only=True)
    if not hits:
        return {"rule_prob": float(baseline), "hit_rules": []}

    bullish_hits: list[dict] = []
    bearish_neg: float = 0.0
    bearish_n = 0
    for r in hits:
        lb = verified_rules.get(r.key, {})
        if r.bullish:
            bullish_hits.append({
                "key": r.key,
                "name": lb.get("name", r.name),
                "hit_rate_t1": float(lb.get("hit_rate_t1", r.hit_rate_t1)),
                "form_type": lb.get("form_type", r.form_type),
                "samples": int(lb.get("samples", r.samples)),
            })
        else:
            # 看跌规则：fall_rate_t1 未直接存，用 (1 - hit_rate_t1) 近似下跌率
            fall = 1.0 - float(lb.get("hit_rate_t1", r.hit_rate_t1))
            bearish_neg += fall
            bearish_n += 1

    # 看涨部分：加权平均（按 hit_rate_t1 相对基线的 lift 权重）
    rule_pos = baseline
    if bullish_hits:
        total_w = 0.0
        acc = 0.0
        for h in bullish_hits:
            w = max(h["hit_rate_t1"] - baseline, 0.01)   # lift 权重
            acc += w * h["hit_rate_t1"]
            total_w += w
        rule_pos = acc / total_w if total_w > 0 else baseline

    # 看跌抑制
    if bearish_n:
        rule_pos *= (1.0 - lambda_neg * (bearish_neg / bearish_n))

    return {"rule_prob": float(rule_pos), "hit_rules": bullish_hits}


def run_daily_scan(
    max_stocks: int | None = None,
    scan_date: str = "",
    top_k: int | None = None,
    progress_cb=None,
    should_stop=None,
) -> dict:
    """每日全市场扫描入口（带进程内并发互斥锁，Bug8 修复）。

    三个入口会并发触发扫描——main.py 定时任务（采集完成自动扫描）/ data.py
    _auto_scan_after_collection（采集 done 后自动扫描）/ predictions.py trigger_scan（手动），
    但彼此不检查对方状态，实测 3 线程并发处理同一批股票、互相争抢 MySQL，单次被拖到 16 小时。
    加非阻塞互斥：已有扫描在跑时，后到入口直接返回（不重复执行），由先到线程产出榜单。
    """
    if not _SCAN_LOCK.acquire(blocking=False):
        logger.warning(
            f"[daily_scan] 已有扫描在运行，本次触发跳过（并发防护）。"
            f"scan_date={scan_date or ''}"
        )
        return {
            "date": scan_date or "",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_scanned": 0,
            "confirmed_count": 0,
            "candidate_pool": 0,
            "top_picks": [],
            "skipped": "already_running",
            "message": "已有扫描在运行，本次触发跳过（并发防护）",
        }
    try:
        return _run_daily_scan_impl(
            max_stocks=max_stocks,
            scan_date=scan_date,
            top_k=top_k,
            progress_cb=progress_cb,
            should_stop=should_stop,
        )
    finally:
        _SCAN_LOCK.release()


def _run_daily_scan_impl(
    max_stocks: int | None = None,
    scan_date: str = "",
    top_k: int | None = None,
    progress_cb=None,
    should_stop=None,
    as_of: str = "",
    universe: int | None = None,
    universe_seed: int = 20260922,
    persist: bool = True,
) -> dict:
    """执行每日全市场扫描，产出推荐榜单（落盘 + 返回）。（内部实现，锁由 run_daily_scan 持有）

    Args:
        max_stocks: 限制扫描股票数（调试用）
        scan_date: 扫描日期 YYYYMMDD（空=今天）
        top_k: 榜单条数（None/0 = 全部候选，不截断）
        progress_cb: 进度回调 fn(stage, pct)

    Returns:
        {
          "date", "generated_at", "total_scanned", "confirmed_count",
          "candidate_pool", "top_picks": [...], "elapsed"
        }
    """
    # 结果日期以"股票日线最新日期"为时间标准（而非运行当天日期）：
    # 例如 8/12 运行扫描但日线仅采集到 8/11，则结果日期应为 20260811，
    # 避免用运行日期虚标尚未有数据的交易日。无日线数据时才兜底用运行日期。
    if not scan_date:
        _latest = get_latest_daily_date()
        scan_date = _latest.strftime("%Y%m%d") if _latest is not None else time.strftime("%Y%m%d")
    t0 = time.time()
    # 榜单条数可进化（plans/17）：top_k 未显式传入时读 evolution_config.daily_top_k（热生效）
    if top_k is None:
        try:
            top_k = int(_evolve_param("daily_top_k", DEFAULT_TOP_K) or DEFAULT_TOP_K)
        except (TypeError, ValueError):  # noqa: BLE001
            top_k = DEFAULT_TOP_K

    def _progress(stage: str, pct: float):
        if progress_cb:
            progress_cb(stage, pct)
        logger.info(f"[daily_scan] {stage} {pct:.0%}")

    # ── 1. 加载回测固化产物（防参数漂移）──
    normalizer = load_normalizer()
    if normalizer is None:
        logger.error("daily_scan 中止：未找到 feature_normalizer.json，请先运行回测")
        return {"date": scan_date, "error": "未找到归一化参数，请先运行回测", "top_picks": []}
    # 采样条件参数表（用户架构：bps/筹码/资金等原始值分位统计"条件→上涨率"，
    # 不进向量、不归一化，作为每日推荐条件打分与 Agent 参考）
    cond_table = load_condition_table()
    store = VectorStore()
    if cond_table is None:
        logger.warning("daily_scan：未找到采样条件参数表 condition_table，条件打分退化为中性基线")
    # 入场指引（回测固化：成功组各形态最佳入场，无后视偏差口径）
    entry_guidance = load_entry_guidance()
    if entry_guidance:
        logger.info("daily_scan：已加载入场指引，推荐将附加操作建议")
    else:
        logger.warning("daily_scan：未找到入场指引 entry_guidance.json，推荐无操作建议（请先运行回测）")

    # 规则形态排行榜（规划 12 v4.0）：同步已验证规则状态 + 基线
    rule_leaderboard = load_form_leaderboard()
    verified_rules = sync_rules_verified(rule_leaderboard) if rule_leaderboard else {}
    rule_baseline = float(rule_leaderboard.get("baseline_t1", 0.5)) if rule_leaderboard else 0.5
    if verified_rules:
        logger.info(f"daily_scan：已加载 {len(verified_rules)} 条已验证规则形态（基线 {rule_baseline:.2f}）")
    else:
        logger.warning("daily_scan：未找到已验证规则形态 form_leaderboard.json，规则信号退化为中性基线（请先运行规则验证）")

    # 最近交易日（停牌检测：最后一日远离最新交易日则视为停牌，跳过）
    # 回放时基准必须换成 as_of —— 否则会拿"今天"去判断 2019 年某股是否停牌（未来函数）。
    latest_trade = None
    try:
        _ref_date = as_of or scan_date
        recent = get_trade_dates("20100101" if as_of else "20250101", _ref_date)
        if recent:
            latest_trade = recent[-1]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"获取交易日失败: {exc}")

    # ── 2. 股票池 ──
    _progress("加载股票池", 0.02)
    pool = load_stock_pool()
    if pool.empty:
        return {"date": scan_date, "error": "股票池为空", "top_picks": []}
    codes = pool["ts_code"].tolist()
    # 粗筛选：剔除科创板(688/689.SH) 与 北交所(.BJ) 股票，暂不计入每日推荐考虑范围
    total_pool = len(codes)
    codes = [
        c for c in codes
        if not (str(c).startswith(("688", "689")) or str(c).endswith(".BJ"))
    ]
    excluded = total_pool - len(codes)
    if excluded > 0:
        logger.info(f"daily_scan：粗筛选剔除科创板/北交所 {excluded} 只（保留 {len(codes)} 只）")
    if max_stocks:
        codes = codes[:max_stocks]

    # 回放 universe 抽样（plans/25 F12 省钱主旋钮）：**确定性抽样**（固定 seed + 先排序）——
    # 否则同一 as_of 两次跑出不同结果，"可复现"就没了。
    if as_of and universe and 0 < int(universe) < len(codes):
        rnd = random.Random(int(universe_seed))
        codes = sorted(codes)                     # 先规范化顺序，保证跨机器/跨次一致
        codes = sorted(rnd.sample(codes, int(universe)))
        logger.info(f"daily_scan：回放抽样 universe={len(codes)} 只（seed={universe_seed}）")
    name_map = dict(zip(pool["ts_code"].astype(str), pool["name"].astype(str)))

    candidates: list[dict] = []
    confirmed_count = 0
    total_scanned = 0

    # ── 3. 逐股：形态识别 → 特征 → 打分 → 风险过滤 ──
    _progress("逐股扫描", 0.1)
    cancelled = False
    for i, code in enumerate(codes):
        # 停止扫描（前端"停止扫描"按钮 / POST /predictions/scan/stop）：
        # 协作式取消——每只股票前检查一次，命中即中断且**不落盘、不登记命中**，
        # 避免把"跑了一半的榜单"当成当日正式推荐（否则 monitor_state / T+5 回填会被污染）。
        if should_stop is not None and should_stop():
            cancelled = True
            logger.warning(
                f"[daily_scan] 收到停止请求：已扫描 {total_scanned}/{len(codes)} 只"
                f"（确认启动 {confirmed_count}，候选 {len(candidates)}）→ 中断，不产出榜单"
            )
            break
        total_scanned += 1
        try:
            # 回放：截断到 as_of + 复权基准锚到 as_of；实时：与原来完全一致
            df = _prepare_replay(code, as_of)
            if df is None or df.empty or len(df) < 30:
                continue
            # 无未来函数硬断言（plans/25 §五）：回放取到的最后一根 K 线必须 <= as_of
            if as_of:
                _last = pd.Timestamp(df["trade_date"].iloc[-1]).strftime("%Y%m%d")
                if _last > as_of:
                    logger.error(f"[daily_scan] {code} 回放数据越界（{_last} > {as_of}）→ 跳过，防止未来函数")
                    continue
            # 停牌检测：最后一日距最新交易日 > 5 自然日 → 停牌中，跳过
            if latest_trade is not None:
                gap = (latest_trade - df["trade_date"].iloc[-1]).days
                if gap > 5:
                    continue
            end_idx = len(df) - 1

            # 当前时点形态识别 + 启动确认（快筛，不查关联表）
            pat = classify_current(df, end_idx, code)
            if pat is None or not pat.confirmed:
                continue
            if pat.form_type == "E":      # 连板难买入，不推荐
                continue
            confirmed_count += 1
            logger.info(
                f"[daily_scan] {code} 确认启动形态 {pat.form_type}（置信度 {pat.confidence}）"
                f"{pat.detail or ''}"
            )

            # 提取当前时点 59 维特征（含现有数据库增强特征）
            extra_map = build_extra_map(code, df, [end_idx])
            extra = extra_map.get(end_idx, {})
            fv = extract_features(
                df, end_idx, ts_code=code, form_type=pat.form_type, label="", extra=extra,
            )

            # 归一化（固化参数）：只对形态向量 VECTOR_FEATURES 归一化（用户架构）
            vec_vec = normalizer.transform(fv.to_array(VECTOR_FEATURES).reshape(1, -1))[0]
            n_dims = vec_vec.shape[0] if hasattr(vec_vec, "shape") else len(vec_vec)
            if n_dims != len(VECTOR_FEATURES):
                logger.warning(f"[daily_scan] {code} 归一化维度 {n_dims} != 形态维度 {len(VECTOR_FEATURES)}"
                               f"（normalizer 为旧版本，请重跑回测生成一致产物），跳过")
                continue

            # 形态粗筛：与同形态正模式库相似度（33 维形态）
            vec_match = vec_vec
            pos = store.query("success", pat.form_type, vec_match, top_k=1)
            pos_sim = pos[0].similarity if pos else 0.0

            # 反向排除：与失败模式库相似度
            neg = store.query("failure", pat.form_type, vec_match, top_k=1)
            neg_sim = neg[0].similarity if neg else 0.0

            # 条件参数（bps/人均持股/股东人数/筹码集中度等）——用户架构：
            #   - 向量特征只用于"形态相似度召回"（找相似的上涨波段），已在上方完成；
            #   - 条件参数不作为向量、不作为主排序，而是携带原始值供 Agent 精筛作为"参考条件"；
            #   - 回测统计（condition_table）结果提供给 Agent 作为分析参考。
            # 关键：condition_table 在"原始值空间"按分位切分学习（回测 build_condition_table 用
            # 未归一化的条件矩阵），故此处必须用条件参数原始值（而非归一化）查表，否则阈值错位。
            cond_fdict = {CONDITION_FEATURES[i]: float(fv.features.get(CONDITION_FEATURES[i], np.nan))
                          for i in range(len(CONDITION_FEATURES))}
            prob, hits = score_by_probability_table(cond_fdict, cond_table) if cond_table is not None else (0.0, [])

            # 综合概率（负向抑制，作为 Agent 参考分，不主导排序——排序由形态相似度决定）
            # 负向抑制强度可进化（plans/17）：读 evolution_config.daily_lambda_exclude（热生效）
            final_prob = prob * (1.0 - _evolve_param("daily_lambda_exclude", LAMBDA_EXCLUDE) * neg_sim) * (0.7 + 0.3 * pos_sim)
            logger.info(
                f"[daily_scan] {code} 条件参考: 条件概率 {prob:.2%} 负向抑制 {neg_sim:.3f} → 综合 {final_prob:.2%}"
                f"（命中 {len(hits)} 条）"
            )

            # 风险过滤（用户补充：st_risk_flag 排除即将*ST高风险股：净资产<0/连续亏损）
            risk = check_risk(code, df, pledge_ratio=extra.get("pledge_ratio"),
                              basic=pool, st_risk_flag=extra.get("st_risk_flag"))
            if not risk["pass"]:
                logger.info(f"[daily_scan] {code} 风险过滤排除: {risk.get('reasons')}")
                continue
            logger.info(f"[daily_scan] {code} 通过风险过滤，进入候选池")

            # 入场建议（回测固化：成功组该形态最佳入场方式）
            advice = get_entry_advice(entry_guidance, pat.form_type)

            # ── 规则形态信号（规划 12 v4.0：信号 2，T+1 次日口径）──
            # 看跌规则抑制强度可进化（plans/17）：读 evolution_config.daily_rule_lambda（热生效）
            rule_sig = _compute_rule_signal(
                df, end_idx, verified_rules, rule_baseline,
                lambda_neg=_evolve_param("daily_rule_lambda", LAMBDA_NEG_RULE),
            )
            rule_prob = rule_sig["rule_prob"]
            hit_rules = rule_sig["hit_rules"]

            candidates.append({
                "rank": 0,
                "ts_code": code,
                "name": name_map.get(code, ""),
                "form_type": pat.form_type,
                "up_probability": round(float(final_prob), 4),
                "rule_prob": round(float(rule_prob), 4),
                "positive_score": round(float(prob), 4),
                "negative_score": round(float(neg_sim), 4),
                "similarity": round(float(pos_sim), 4),
                "hit_conditions": hits,
                "hit_rules": hit_rules,
                # 条件参数原始值（bps/人均持股/股东人数/筹码集中度/ROE 等）——供 Agent 精筛作为"参考条件"。
                # 用户架构：向量只用于形态相似度召回；条件参数携带给 Agent 结合政策/新闻等综合分析。
                "condition_values": {
                    k: (None if (v is None or (isinstance(v, float) and np.isnan(v))) else round(float(v), 4))
                    for k, v in cond_fdict.items()
                },
                "risk_level": "高" if neg_sim > 0.75 else ("中" if neg_sim > 0.6 else "低"),
                "confirmed_detail": pat.detail,
                "entry_advice": advice,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[daily_scan] 处理 {code} 失败: {exc}")
            continue

        if (i + 1) % 200 == 0:
            _progress("逐股扫描", 0.1 + 0.85 * (i + 1) / max(len(codes), 1))
            logger.info(
                f"[daily_scan] 扫描进度 {i + 1}/{len(codes)}: 已确认启动 {confirmed_count}，"
                f"候选池 {len(candidates)}"
            )

    if cancelled:
        _progress("已停止", 1.0)
        return {
            "date": scan_date,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cancelled": True,
            "total_scanned": total_scanned,
            "confirmed_count": confirmed_count,
            "candidate_pool": len(candidates),
            "top_picks": [],
            "elapsed": round(time.time() - t0, 2),
            "message": (
                f"扫描已按请求停止：已扫描 {total_scanned}/{len(codes)} 只，"
                f"确认启动 {confirmed_count} 只；本次未产出榜单（不覆盖已有结果）"
            ),
        }

    # ── 4. 排序 → 输出（优化：推荐必须涨 → 主排序以上涨概率为核心）──
    _progress("排序输出", 0.97)
    # 用户核心诉求"推荐的股票必须涨、最好主升浪"：主排序以综合上涨概率 up_probability
    # （条件概率 + 负向抑制后的最终概率）为核心，形态相似度作为辅助信号加权。
    # 权重可进化（daily_rank_alpha：相似度权重 0~1，0=纯上涨概率，1=纯相似度，默认 0.3）。
    try:
        _rank_alpha = float(_evolve_param("daily_rank_alpha", 0.3) or 0.3)
    except (TypeError, ValueError):  # noqa: BLE001
        _rank_alpha = 0.3
    _rank_alpha = max(0.0, min(1.0, _rank_alpha))

    def _rank_key(c: dict) -> float:
        prob = float(c.get("up_probability", 0) or 0)
        sim = float(c.get("similarity", 0) or 0)
        return prob * (1.0 - _rank_alpha) + sim * _rank_alpha

    for c in candidates:
        c["rank_score"] = round(_rank_key(c), 4)
    candidates.sort(key=_rank_key, reverse=True)
    top_picks = candidates if not top_k else candidates[:top_k]
    for r, c in enumerate(top_picks):
        c["rank"] = r + 1
    logger.info(f"[daily_scan] 候选池 {len(candidates)} → 榜单 {len(top_picks)} 条"
                f"（主排序=上涨概率×(1-{_rank_alpha})+相似度×{_rank_alpha}）")

    report = {
        "date": scan_date,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_scanned": total_scanned,
        "confirmed_count": confirmed_count,
        "candidate_pool": len(candidates),
        "top_picks": top_picks,
        "elapsed": round(time.time() - t0, 2),
    }

    # ── 5. 落盘 + 命中登记（**回放 persist=False 时全部跳过**，见 plans/25 G3）──
    report.update({"as_of": as_of or "", "universe": int(universe) if (as_of and universe) else 0,
                   "persist": bool(persist), "mode": "replay" if as_of else "live",
                   "universe_seed": int(universe_seed) if as_of else 0})
    if persist:
        try:
            os.makedirs(REPORT_DIR, exist_ok=True)
            version = next_report_version(f"daily_{scan_date}")
            path = _report_path(scan_date, version)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, default=str)
            logger.info(f"每日榜单已落盘: {path}（v{version}，{len(top_picks)} 条）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"每日榜单落盘失败: {exc}")

        # 命中登记（推荐时 t5_ret 未知，先记录；T+5 后由外部回填实际涨幅）
        try:
            from app.backtest.monitor import record_hits
            records = [
                {"date": scan_date, "ts_code": c["ts_code"], "form_type": c["form_type"],
                 "pred_prob": c["up_probability"], "t5_ret": None}
            for c in top_picks
            ]
            record_hits(records)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"命中登记失败: {exc}")
    else:
        logger.info(f"[daily_scan] persist=False（回放模式）：跳过落盘与命中登记，"
                    f"结果由自证内核写入沙箱（{len(top_picks)} 条）")

    _progress("完成", 1.0)
    logger.info(f"每日扫描完成: 扫描 {total_scanned}，确认启动 {confirmed_count}，候选 {len(candidates)}，推荐 {len(top_picks)}")
    return report


def load_report(scan_date: str) -> dict | None:
    """加载某日已生成的榜单（同一天多版本时返回版本最新者，前端/API 只展示最新推荐结果）。

    兼容旧无版本文件 daily_{scan_date}.json（视为最低版本）。
    """
    path = _latest_report_path(scan_date)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"加载榜单失败: {exc}")
        return None


def _latest_report_path(scan_date: str) -> str | None:
    """返回该日期下版本号最新的榜单文件路径；无任何榜单时返回 None。"""
    files = glob.glob(os.path.join(REPORT_DIR, f"daily_{scan_date}_v*.json"))
    versions = []
    for f in files:
        m = re.match(rf"^daily_{scan_date}_v(\d+)\.json$", os.path.basename(f))
        if m:
            versions.append((int(m.group(1)), f))
    if versions:
        versions.sort(key=lambda x: x[0])
        return versions[-1][1]
    # 无版本文件时回退旧无版本文件（兼容历史数据）
    legacy = _report_path(scan_date)
    return legacy if os.path.exists(legacy) else None
