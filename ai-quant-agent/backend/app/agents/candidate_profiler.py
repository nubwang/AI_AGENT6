"""候选画像 Agent（candidate_profiler）

职责（对齐 plans/03-Agent系统.md §二 Agent#1）：
  - 从 daily_scan 产出的候选池，为每只候选组装"量化画像"
  - 补充所属板块/概念（concept_detail / ths_member → concept / ths_index）
  - 纯规则，不调 LLM

输出：每只候选的 dict，供个股精筛 Agent 组装 prompt。
"""
from __future__ import annotations

import json

from sqlalchemy import text

from app.models import SessionLocal

# 条件参数中文名（供 Agent 读取"个股全面的本地相关信息"——用户架构：条件参数是 Agent 参考条件）
# key 与 feature_extractor.CONDITION_FEATURES 对齐；未列出的用原名兜底。
CONDITION_LABELS: dict[str, str] = {
    "bps": "每股净资产(元)",
    "eps": "每股收益(元)",
    "roe": "净资产收益率(%)",
    "gross_margin": "毛利率(%)",
    "debt_ratio": "资产负债率(%)",
    "revenue_growth": "营收增速(%)",
    "profit_growth": "利润增速(%)",
    "ocf_ps": "每股经营现金流(元)",
    "pb_percentile": "PB百分位(%)",
    "pe_percentile": "PE百分位(%)",
    "circ_mv": "流通市值(亿)",
    "holder_num": "股东人数(户)",
    "avg_hold_amount": "人均持股金额(元)",
    "holder_concentration": "筹码集中度",
    "holder_change": "股东数环比变化",
    "top10_ratio": "前十大股东占比(%)",
    "pledge_ratio": "质押比例(%)",
    "turnover_rate": "换手率(%)",
    "amount_ratio": "量比",
    "volume_ratio": "量比(20日)",
    "vol_ma_change": "量能变化",
    "vol_surge_days": "放量天数",
    "vol_pct_20": "20日量占比",
    "net_inflow_5d": "5日净流入",
    "net_amount_ratio": "净流入率",
    "large_order_ratio": "大单净占比",
    "market_env": "市场环境",
    "mo_trend": "月线趋势",
    "wk_trend": "周线趋势",
    "is_ever_st": "曾ST",
    "st_risk_flag": "即将*ST风险",
}


def get_stock_industry(ts_code: str) -> str:
    """查询某只股票所属行业（stock_basic.industry）。无则返回 ''。"""
    db = SessionLocal()
    try:
        r = db.execute(
            text("SELECT industry FROM stock_basic WHERE ts_code=:c"),
            {"c": ts_code},
        ).fetchone()
        return str(r[0]) if r and r[0] else ""
    except Exception:  # noqa: BLE001
        return ""
    finally:
        db.close()


def get_stock_concepts(ts_code: str) -> list[str]:
    """查询某只股票所属概念板块名（同花顺 + 概念）。失败返回 []。

    注意：concept/ths_* 表依赖 Tushare 2000 积分权限（基础积分通常采不到），
    返回空时由 build_candidate_profile 用 industry 兜底。
    """
    concepts: list[str] = []
    db = SessionLocal()
    try:
        # 同花顺概念：ths_member.con_code -> ths_index.name
        rows = db.execute(
            text("SELECT DISTINCT m.con_code FROM ths_member m WHERE m.ts_code=:c"),
            {"c": ts_code},
        ).fetchall()
        codes = [r[0] for r in rows if r[0]]
        if codes:
            names = db.execute(
                text("SELECT name FROM ths_index WHERE ts_code IN :codes"),
                {"codes": tuple(codes)},
            ).fetchall()
            concepts += [str(r[0]) for r in names if r[0]]
        # 概念板块：concept_detail.code -> concept.name
        rows2 = db.execute(
            text("SELECT DISTINCT code FROM concept_detail WHERE ts_code=:c"),
            {"c": ts_code},
        ).fetchall()
        codes2 = [r[0] for r in rows2 if r[0]]
        if codes2:
            names2 = db.execute(
                text("SELECT name FROM concept WHERE code IN :codes2"),
                {"codes2": tuple(codes2)},
            ).fetchall()
            concepts += [str(r[0]) for r in names2 if r[0]]
    except Exception:  # noqa: BLE001
        pass
    finally:
        db.close()
    # 去重保序
    seen = set()
    out = []
    for c in concepts:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:20]


def build_candidate_profile(cand: dict, concepts: list[str] | None = None,
                            industry: str | None = None) -> dict:
    """把 daily_scan 候选项组装为结构化画像（供 LLM 消费）。

    Args:
        cand: daily_recommend/*.json 中 top_picks 的一项
        concepts: 所属概念/板块（可选，Tushare 2000 积分接口，常为空）
        industry: 所属行业（stock_basic.industry，已有数据，政策解读的基础）

    Returns:
        profile dict
    """
    hit_conds = cand.get("hit_conditions") or []
    hits_txt = "；".join(
        f"{h.get('feature') or h.get('combo')}≥{round(h.get('threshold', 0), 3)}→{round(h.get('up_prob', 0) * 100, 0)}%"
        for h in hit_conds[:6]
    ) or "无"
    # 形态规则命中（规划 15）：原始列表保留供 L2 保送判断，文本供 LLM prompt 展示
    hit_rules = cand.get("hit_rules") or []
    hit_rules_txt = "；".join(
        f"{h.get('name') or h.get('key')}→次日上涨率{round(float(h.get('hit_rate_t1', 0)) * 100, 0)}%"
        for h in hit_rules[:5]
    ) or "无"
    # 条件参数原始值（用户架构：bps/人均持股/股东人数/筹码集中度等作为 Agent 参考条件，
    # 由 Agent 结合个股全面本地信息 + 政策 + 新闻综合分析；非筛选条件、非主排序）
    cond_vals = cand.get("condition_values") or {}
    cond_parts = []
    for k, v in cond_vals.items():
        if v is None:
            continue
        label = CONDITION_LABELS.get(k, k)
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        cond_parts.append(f"{label}={fv:.2f}" if abs(fv) >= 100 else f"{label}={fv:.3f}")
    cond_txt = "；".join(cond_parts[:12]) or "无本地条件数据"
    # 回测统计参考（条件命中：condition_table 规则 → 上涨概率）——提供给 Agent 作为分析参考
    cond_stat_txt = hits_txt  # hit_conditions 即回测条件统计命中（含方向/上涨率）

    concepts = list(concepts or [])
    # 行业作为板块/政策解读的基础（概念缺失时用行业兜底）
    sectors = concepts[:]
    if industry and industry not in sectors:
        sectors.insert(0, industry)
    profile = {
        "ts_code": cand.get("ts_code", ""),
        "name": cand.get("name", ""),
        "form_type": cand.get("form_type", ""),
        "up_probability": round(cand.get("up_probability", 0) * 100, 1),
        "positive_score": round(cand.get("positive_score", 0) * 100, 1),
        "negative_score": round(cand.get("negative_score", 0), 3),
        "similarity": round(cand.get("similarity", 0), 3),
        "condition_txt": cond_txt,          # 个股条件参数原始值（Agent 参考）
        "condition_stat_txt": cond_stat_txt,  # 回测条件统计命中（Agent 参考）
        "hit_conditions": hits_txt,
        "hit_rules": hit_rules,        # 原始命中规则列表（L2 保送用）
        "hit_rules_txt": hit_rules_txt,
        "rule_prob": round(float(cand.get("rule_prob", 0)) * 100, 1),
        "risk_level": cand.get("risk_level", ""),
        "confirmed_detail": cand.get("confirmed_detail") or {},
        "industry": industry or "",
        "concepts": concepts,
        "sectors": sectors,          # 行业 + 概念（政策解读/消息面的板块基础）
    }
    return profile


def profile_to_text(p: dict) -> str:
    """画像 dict → 单行可读文本（供 prompt）。"""
    conf = p.get("confirmed_detail") or {}
    return (
        f"代码{p.get('ts_code')} {p.get('name')} 形态{p.get('form_type')} "
        f"综合上涨概率{p.get('up_probability')}% 正分{p.get('positive_score')} "
        f"负向{p.get('negative_score')} 相似度{p.get('similarity')} "
        f"命中条件[{p.get('hit_conditions')}] 风险{p.get('risk_level')} "
        f"形态细节{conf} 所属行业{p.get('industry') or '无'} "
        f"所属概念{','.join(p.get('concepts') or []) or '无'} "
        f"形态规则命中[{p.get('hit_rules_txt')}] "
        f"个股条件参数[{p.get('condition_txt')}]"
    )


def dump_profiles(candidates: list[dict], top_k: int | None = None) -> list[dict]:
    """批量组装画像（top_k 限流；None = 全部候选），返回 profile 列表。"""
    profiles = []
    for cand in candidates[:top_k]:
        code = cand.get("ts_code", "")
        try:
            concepts = get_stock_concepts(code)
        except Exception:  # noqa: BLE001
            concepts = []
        try:
            industry = get_stock_industry(code)
        except Exception:  # noqa: BLE001
            industry = ""
        profiles.append(build_candidate_profile(cand, concepts, industry))
    return profiles


def to_json(d) -> str:
    """画像转 JSON 字符串（给 LLM）。"""
    return json.dumps(d, ensure_ascii=False, default=str)
