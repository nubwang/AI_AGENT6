"""知识入库适配层（kb_ingest）— 对齐 plans/21 §5 / §7 / §九

把各模块的既有产物（次日验证报告 / 回测结果 / 进化改动记录）翻译成 EKB 五类档案，
各模块只需调用这里的函数即可沉淀知识，避免各写各的 schema。

对外 API：
  - build_cases / ingest_verify_period   失败·成功·中性案例 + 环境快照（daily_verify 调用）
  - ingest_context                       环境与口径快照
  - ingest_backtest                      回测结果 → experiment（带防自欺口径指纹）
  - backtest_components / digest_of / diff_components   口径指纹（护栏 P4 复用）
  - record_experiment / update_experiment_verdict       实验台账（提案/生效/结论）
  - record_change / update_change_result                改动因果链（为啥改/结果/回滚）

设计：全部异步入队（kb_writer），失败静默，绝不阻塞推荐/回测主流程。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import time

from app.core.logger import logger
from app.kb import kb_writer, kb_distiller, kb_store

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
INDEX_CSV = os.path.join(DATA_DIR, "market_env_000001.SH.csv")
VERIFY_KB_FILE = os.path.join(DATA_DIR, "verify_kb.json")
MONITOR_STATE_FILE = os.path.join(DATA_DIR, "monitor_state.json")

# 影响"主指标口径"的参数（进指纹：这些被改动 → 指标不可比，判 invalid）
_METRIC_PARAMS = ("hit_threshold", "t1_hit_threshold", "wave_hit_threshold",
                  "outcome_success_gain")

_INDEX_CACHE: dict = {"loaded": False, "dates": [], "closes": []}


def _cfg() -> dict:
    try:
        from app.agents import evolution_config
        return evolution_config.get("knowledge_base", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _success_min_excess() -> float:
    try:
        return float((_cfg().get("success", {}) or {}).get("min_excess", 0.0))
    except Exception:  # noqa: BLE001
        return 0.0


def _success_min_t1() -> float:
    """成功侧最低涨幅门槛（默认 2%）。

    人工抽检发现：仅以 t1 > 0 判"成功"，会把 t1=+0.65% 但 T+5 为 -13% 的**微涨噪声**
    收进成功侧，污染"成功规律"。故引入门槛：thr < t1 < min_t1 归入 neutral（小涨无信息），
    与规划 §5.1 对 neutral 的原始定义（"小涨小跌无信息"）一致。
    """
    try:
        return float((_cfg().get("success", {}) or {}).get("min_t1", 0.02))
    except Exception:  # noqa: BLE001
        return 0.02


def classify_side(t1_ret: float | None, thr: float, excess: float | None,
                  min_t1: float | None = None, min_excess: float | None = None) -> str:
    """统一的案例分类口径（build_cases 与历史重分类共用，避免两处逻辑漂移）。"""
    if t1_ret is None:
        return "neutral"
    t1 = float(t1_ret)
    m_t1 = _success_min_t1() if min_t1 is None else float(min_t1)
    m_ex = _success_min_excess() if min_excess is None else float(min_excess)
    if t1 <= float(thr) + 1e-9:
        return "failure"                     # 未达次日上涨阈值 → 失败（需归因）
    if t1 < m_t1:
        return "neutral"                     # 微涨：无信息量，不做成功归因
    if excess is not None and float(excess) < m_ex:
        return "neutral"                     # 涨了但未跑赢大盘：同样不做成功归因
    return "success"


def _param(name: str, default):
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param(name, default)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


# ── 大盘环境（上证指数 T+1/T+5 与 regime）────────────────────
def _index_series() -> tuple[list[str], list[float]]:
    """加载上证指数日线（模块级缓存）。失败返回空。"""
    if _INDEX_CACHE["loaded"]:
        return _INDEX_CACHE["dates"], _INDEX_CACHE["closes"]
    dates: list[str] = []
    closes: list[float] = []
    try:
        with open(INDEX_CSV, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = str(row.get("trade_date", "")).strip()
                try:
                    c = float(row.get("close", "") or 0)
                except Exception:  # noqa: BLE001
                    continue
                if d and c > 0:
                    dates.append(d)
                    closes.append(c)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[kb_ingest] 指数数据不可用: {exc}")
    _INDEX_CACHE.update({"loaded": True, "dates": dates, "closes": closes})
    return dates, closes


def _index_idx(date_str: str) -> int | None:
    """推荐日 D 在指数序列中的位置（取 >= D 的首个交易日）。"""
    dates, _ = _index_series()
    for i, d in enumerate(dates):
        if d >= str(date_str)[:8]:
            return i
    return None


def env_snapshot(date_str: str) -> dict:
    """环境快照（D+1 买入口径，与个股收益口径一致）。

    Returns: {index_t1_ret, index_t5_ret, index_mom20, regime, index_date}
    """
    dates, closes = _index_series()
    out: dict = {"index_t1_ret": None, "index_t5_ret": None,
                 "index_mom20": None, "regime": "unknown"}
    i = _index_idx(date_str)
    if i is None:
        return out
    buy = i + 1
    if buy >= len(closes):
        return out
    base = closes[buy]
    if base <= 0:
        return out
    if buy + 1 < len(closes):
        out["index_t1_ret"] = round(closes[buy + 1] / base - 1.0, 4)
    if buy + 5 < len(closes):
        out["index_t5_ret"] = round(closes[buy + 5] / base - 1.0, 4)
    if i >= 19 and closes[i - 19] > 0:
        mom = closes[i] / closes[i - 19] - 1.0
        out["index_mom20"] = round(mom, 4)
        out["regime"] = "强势" if mom >= 0.03 else ("弱势" if mom <= -0.03 else "震荡")
    out["index_date"] = dates[i]
    return out


# ── 案例档案构建 ─────────────────────────────────────────────
def _pick_snapshot(pick: dict) -> dict:
    """榜单详情 → 推荐依据快照（精简，控体积）。"""
    rules = pick.get("hit_rules") or []
    conds = pick.get("hit_conditions") or []
    return {
        "similarity": pick.get("similarity"),
        "negative_score": pick.get("negative_score"),
        "up_probability": pick.get("up_probability"),
        "positive_score": pick.get("positive_score"),
        "hit_rules": [r.get("name") for r in rules[:5] if isinstance(r, dict)],
        "hit_conditions": [c.get("feature") for c in conds[:8] if isinstance(c, dict)],
        "agent_reason": str(pick.get("agent_reason") or "")[:120],
        "rank": pick.get("rank"),
    }


def build_cases(date_str: str, hits: list[dict], detail: dict | None = None,
                source: str = "daily_verify") -> list[dict]:
    """把某日推荐结果构建为案例档案（failure / success / neutral）。

    分类口径（与 plans/21 §5.1 一致，人工抽检后收紧）：
      - failure：t1_ret ≤ t1_hit_threshold（未达预期，需归因）
      - neutral：微涨（thr < t1 < success.min_t1，默认 2%）或未跑赢大盘（excess 低于门槛）
        —— "小涨无信息"，**不做成功归因**（避免把 +0.65% 却 T+5 跌 13% 的噪声当成功样本）
      - success：t1 ≥ success.min_t1 且（无指数数据或）跑赢大盘 → 成功侧归因对象
    """
    if detail is None:
        try:
            from app.agents import daily_verify
            detail = daily_verify._load_report_detail(date_str)
        except Exception:  # noqa: BLE001
            detail = {}
    try:
        thr = float(_param("t1_hit_threshold", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        thr = 0.0
    env = env_snapshot(date_str)
    min_excess = _success_min_excess()
    cases: list[dict] = []
    for h in hits or []:
        ts_code = str(h.get("ts_code", ""))
        if not ts_code:
            continue
        # 防御：T+1 尚未回填的推荐**不得**当案例（否则会被当成"未上涨"，污染知识库）
        if h.get("t1_ret") is None:
            continue
        pick = (detail or {}).get(ts_code, {}) or {}
        t1 = float(h.get("t1_ret", 0) or 0)
        t5 = h.get("t5_ret")
        t20 = h.get("t20_ret") if h.get("t20_ret") is not None else None
        bench = env.get("index_t1_ret")
        excess = round(t1 - float(bench), 4) if bench is not None else None
        # 统一口径（失败 / 微涨与未超额=neutral / 真成功）
        side = classify_side(t1, thr, excess, min_excess=min_excess)
        form_type = str(pick.get("form_type") or h.get("form_type") or "")
        cases.append({
            "id": f"case:{date_str[:8]}:{ts_code}",
            "date": str(date_str)[:8],
            "ts_code": ts_code,
            "name": pick.get("name") or h.get("name") or "",
            "side": side,
            "form_type": form_type,
            "stage": pick.get("stage") or "",
            "snapshot": _pick_snapshot(pick),
            "outcome": {
                "t1_ret": round(t1, 4),
                "t5_ret": round(float(t5), 4) if t5 is not None else None,
                "t20_ret": round(float(t20), 4) if t20 is not None else None,
                "bench_t1": bench,
                "bench_t5": env.get("index_t5_ret"),
                "excess_t1": excess,
                "hit": bool(t1 > thr),
                "hit_threshold": thr,
                "pred_prob": h.get("pred_prob"),
            },
            "env": env,
            "cause": {},
            "pair_id": f"pair:{date_str[:8]}:{form_type}",
            "source": source,
            "confidence": None,
        })
    return cases


def _apply_attribution(cases: list[dict], failures: list[dict]) -> None:
    """把 daily_verify 的逐只归因结果回填到对应 case.cause。"""
    amap = {str(f.get("ts_code", "")): f for f in failures or []}
    for c in cases:
        f = amap.get(c["ts_code"])
        if not f:
            continue
        c["cause"] = {
            "error_type": f.get("error_type", "其他"),
            "stage": f.get("stage", "other"),
            "stage_hint": f.get("stage_hint", ""),
            "optimize": f.get("optimize", ""),
            "lesson": f.get("lesson", ""),
            "fix": f.get("fix", ""),
            "signal_hint": f.get("signal_hint", ""),
            "param_hint": f.get("param_hint", "无需调参"),
            "confidence": f.get("confidence", "mid"),
        }
        c["stage"] = f.get("stage", c.get("stage") or "")
        c["confidence"] = 0.8 if f.get("confidence") == "high" else (
            0.6 if f.get("confidence") == "mid" else 0.4)


def ingest_verify_period(report: dict, hits: list[dict], detail: dict | None = None) -> dict:
    """一期次日验证 → EKB：案例 + 教训（失败侧）+ 环境快照。

    Returns: {"date", "cases", "failure", "success", "neutral", "lessons"}
    """
    if not report or not report.get("date"):
        return {"ok": False, "reason": "无验证报告"}
    date_str = str(report["date"])[:8]
    cases = build_cases(date_str, hits, detail)
    _apply_attribution(cases, report.get("failures") or [])
    kb_writer.record_many("case", cases)
    lessons = kb_distiller.absorb_cases([c for c in cases if c["side"] == "failure"])
    ingest_context(date_str, extra={
        "verify": {
            "t1_hit_rate": report.get("t1_hit_rate"),
            "wave_hit_rate": report.get("wave_hit_rate"),
            "composite": report.get("composite"),
            "total": report.get("total"),
            "stage_report": (report.get("stage_report") or [])[:3],
            "improvements": (report.get("improvements") or [])[:5],
        },
        "env": env_snapshot(date_str),
        "metric_basis": report.get("basis", "D+1_buy"),
    })
    counts = {"failure": 0, "success": 0, "neutral": 0}
    for c in cases:
        counts[c["side"]] = counts.get(c["side"], 0) + 1
    return {"ok": True, "date": date_str, "cases": len(cases), "lessons": lessons, **counts}


# 外部软知识库挂引用（plans/21 §三 处置列："统一加入索引（不搬迁、只挂引用）"）
EXTERNAL_KB_FILES = {
    "news": os.path.join(DATA_DIR, "news_lesson_kb.json"),
    "decision": os.path.join(DATA_DIR, "decision_kb.json"),
    "policy_intent": os.path.join(DATA_DIR, "policy_intent_kb.json"),
    "attribution": os.path.join(DATA_DIR, "attribution_kb.json"),
}


def import_external_kbs() -> dict:
    """把既有外部软知识库统一挂进 EKB（只读引用，幂等）。

    - 新闻教训（news_lesson_kb）→ external_news
    - 决策教训（decision_kb）→ external_decision
    - 政策措辞意图（policy_intent_kb）→ external_policy
    - 归因统计（attribution_kb：关键特征/组合规则）→ external_stat
    这些条目 status='reference'：可供综合分析检索，但**不参与置信度升级与否证退役**，
    原始文件保持不动（双读），确保"不搬迁、不丢事实"。
    """
    out: dict = {}
    cap = confidence_for_external()

    def _write(type_: str, key_text: str, text: str, *, hint: str = "",
               signal: str = "", n: int = 1, scope: dict | None = None,
               samples: list | None = None) -> bool:
        if not text:
            return False
        lid = kb_distiller.lesson_id(type_, key_text)
        return kb_store.upsert("lesson", {
            "id": lid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "text": text[:150], "type": type_,
            "scope": {"side": "reference", "source": type_, **(scope or {})},
            "actionable": {"hint": hint[:120], "signal_hint": signal[:60]},
            "support": {"n": max(1, int(n or 1)), "samples": (samples or [])[-20:]},
            "confidence": cap, "half_life_days": 240.0,
            "applied_experiments": [], "effect_verdict": None,
            "status": "reference",
            "last_verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    # 1) 新闻/决策教训（结构相同：error_type/lesson/fix/signal_hint/count/samples）
    for tag, path in (("news", EXTERNAL_KB_FILES["news"]),
                      ("decision", EXTERNAL_KB_FILES["decision"])):
        d = _read_json_file(path, {})
        items = d.get("lessons") or []
        n_ok = 0
        for it in items:
            ok = _write(f"external_{tag}", str(it.get("lesson") or ""),
                        str(it.get("lesson") or ""),
                        hint=str(it.get("fix") or ""), signal=str(it.get("signal_hint") or ""),
                        n=int(it.get("count") or 1), samples=list(it.get("samples") or []),
                        scope={"error_type": it.get("error_type"), "agent": it.get("agent")})
            n_ok += 1 if ok else 0
        out[tag] = n_ok

    # 2) 政策措辞意图（phrase → intent/strength/note）
    d = _read_json_file(EXTERNAL_KB_FILES["policy_intent"], {})
    intents = d.get("intents") or []
    n_ok = 0
    for it in intents:
        phrase = str(it.get("phrase") or "")
        text = f"政策措辞「{phrase}」→ {it.get('intent')}（力度 {it.get('strength')}）：{it.get('note')}"
        n_ok += 1 if _write("external_policy", phrase, text,
                            hint=str(it.get("note") or ""),
                            scope={"intent": it.get("intent"),
                                   "strength": it.get("strength")}) else 0
    out["policy_intent"] = n_ok

    # 3) 归因统计（关键特征 + 组合规则）
    d = _read_json_file(EXTERNAL_KB_FILES["attribution"], {})
    n_ok = 0
    for it in (d.get("key_features") or []):
        feat = str(it.get("feature") or "")
        text = (f"关键特征 {feat}：{it.get('direction')} 值时成功率更高"
                f"（AUC {it.get('auc')}，成功均值 {round(float(it.get('success_mean') or 0), 2)}"
                f" vs 失败均值 {round(float(it.get('failure_mean') or 0), 2)}）")
        n_ok += 1 if _write("external_stat", f"feat:{feat}", text,
                            hint=f"筛查/精筛可提升 {feat} 权重（回测归因 AUC>0.6）",
                            scope={"kind": "key_feature", "auc": it.get("auc")}) else 0
    for it in (d.get("combo_rules") or []):
        feats = it.get("features") or []
        key = "+".join(str(x) for x in feats)
        text = (f"特征组合 [{key}] 上涨概率 {it.get('up_prob')}"
                f"（基线 {it.get('baseline')}，样本 {it.get('sample_count')}）")
        n_ok += 1 if _write("external_stat", f"combo:{key}", text,
                            hint="条件打分/规则命中可优先考虑该组合",
                            n=int(it.get("sample_count") or 1),
                            scope={"kind": "combo_rule"}) else 0
    out["attribution"] = n_ok
    # plans/23·24·25：新模块的**验证产出**（洗盘/四类买点/博弈特征/情绪门控/纪律网格/自证）
    # 与外部软知识库走同一条处置路径（只读、幂等、不搬迁原始文件）—— 这一步是"接神经"：
    # 没有它，进化大脑看不到这些已经统计检验过的结论，只能凭个案教训猜。
    try:
        from app.kb import kb_evidence
        ev = kb_evidence.absorb_all()
        out["module_evidence"] = int(ev.get("total", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[kb_ingest] 模块证据接入跳过: {exc}")
    out["total"] = sum(v for v in out.values() if isinstance(v, int))
    logger.info(f"[kb_ingest] 外部知识库挂引用: {out}")
    return out


def confidence_for_external() -> float:
    """外部（引用型）知识的固定置信度：高到可被检索参考，但不高于一线自产规律。"""
    return 0.6


def _read_json_file(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def backfill_from_verify_kb(periods: int = 60) -> dict:
    """从既有 verify_kb + monitor_state + 榜单明细**回填历史**（幂等，只读原始文件不修改）。

    供 CLI 脚本与 API 共用（plans/21 §十.4 首拉回填）。
    """
    vkb = _read_json_file(VERIFY_KB_FILE, {"verifications": []})
    verifs = (vkb.get("verifications") or [])[-max(1, int(periods)):]
    hits = _read_json_file(MONITOR_STATE_FILE, {"hits": []}).get("hits", [])
    by_date: dict[str, list] = {}
    for h in hits:
        if h.get("t1_ret") is None:      # 只回填"已完成 T+1 回填"的推荐（防数据污染）
            continue
        by_date.setdefault(str(h.get("date", ""))[:8], []).append(h)
    total_cases = total_lessons = unattributed = 0
    for v in verifs:
        d = str(v.get("date", ""))[:8]
        day_hits = by_date.get(d, [])
        if not d or not day_hits:
            continue
        cases = build_cases(d, day_hits, source="backfill")
        _apply_attribution(cases, v.get("failures") or [])
        total_cases += kb_store.upsert_many("case", cases)
        unattributed += sum(1 for c in cases if c["side"] == "failure"
                            and not (c.get("cause") or {}).get("error_type"))
        total_lessons += kb_distiller.absorb_cases(cases)
        ingest_context(d, extra={
            "verify": {"t1_hit_rate": v.get("t1_hit_rate"),
                       "wave_hit_rate": v.get("wave_hit_rate"),
                       "composite": v.get("composite"), "total": v.get("total")},
            "metric_basis": v.get("basis") or vkb.get("metric_basis") or "D+1_buy",
            "backfill": True,
        })
    # 历史回填后立即补全未归因案例（同形态就近继承；低置信、不入教训库）
    inherited = inherit_missing_attribution()
    kb_writer.flush_now()
    return {"periods": len(verifs), "cases": total_cases, "lessons": total_lessons,
            "failures_without_attribution": unattributed, "attribution_inherited": inherited}


def ingest_context(date_str: str, extra: dict | None = None) -> str:
    """写环境与口径快照（含全量可进化参数 + 主指标口径）。"""
    params = {}
    metric = {}
    try:
        from app.agents import evolution_config
        params = {k: v.get("current", v.get("default"))
                  for k, v in (evolution_config.params_table() or {}).items()}
        metric = evolution_config.get("main_metric", {}) or {}
    except Exception:  # noqa: BLE001
        pass
    payload = {
        "params": params,
        "main_metric": metric,
        "config_version": _cfg_version(),
        **(extra or {}),
    }
    return kb_writer.record_context(
        id=f"context:{str(date_str)[:8]}",
        date=str(date_str)[:8],
        payload=payload,
    )


def _cfg_version():
    try:
        from app.agents import evolution_config
        return evolution_config.get("version", "?")
    except Exception:  # noqa: BLE001
        return "?"


# ── 回测结果 → 实验台账（含防自欺口径指纹）────────────────────
def backtest_components(period: dict | None = None, result: dict | None = None) -> dict:
    """回测口径指纹的组成要件（**主指标口径不可变**，详见 plans/21 §九）。"""
    result = result or {}
    metric = {}
    try:
        from app.agents import evolution_config
        metric = evolution_config.get("main_metric", {}) or {}
    except Exception:  # noqa: BLE001
        pass
    metric_params = {}
    for p in _METRIC_PARAMS:
        try:
            metric_params[p] = _param(p, None)
        except Exception:  # noqa: BLE001
            metric_params[p] = None
    return {
        "main_metric": metric,
        "metric_params": metric_params,
        "period": {
            "start": (period or {}).get("start_date") or result.get("start_date"),
            "end": (period or {}).get("end_date") or result.get("end_date"),
        },
        # 数据集指纹：优先用回测报告自带；否则用样本量近似（变化即视为不可比）
        # 注意：回测报告的 samples 常为 dict（{total, success, failure_A...}），
        # 直接取会把整个 dict 当样本数 → 任何字段变化都被判"数据集变了"。这里取 total/n。
        "dataset": (result.get("data_fingerprint")
                    or result.get("dataset_fingerprint")
                    or {"samples": _sample_n(result),
                        "stocks": result.get("total_stocks") or result.get("stocks")}),
    }


def _sample_n(result: dict):
    """样本量取数：支持 int 与 dict（{total|n|samples}）两种历史格式。"""
    v = (result or {}).get("total_samples")
    if v is not None:
        return v
    s = (result or {}).get("samples")
    if isinstance(s, dict):
        return s.get("total") or s.get("n") or s.get("samples")
    return s


def digest_of(components: dict) -> str:
    """口径指纹（sha256）。"""
    try:
        blob = json.dumps(components, ensure_ascii=False, sort_keys=True, default=str)
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
    except Exception:  # noqa: BLE001
        return "sha256:unknown"


def diff_components(before: dict | None, after: dict | None) -> list[str]:
    """比较两次口径要件，返回发生变化的键（防自欺：非空即指标不可比）。"""
    if not before or not after:
        return []
    return [k for k in set(before) | set(after)
            if json.dumps(before.get(k), sort_keys=True, default=str)
            != json.dumps(after.get(k), sort_keys=True, default=str)]


def _last_backtest_components(exclude_id: str = "") -> dict | None:
    """取上一次回测记录的口径要件（供前后对比，判断本次指标是否可比）。"""
    try:
        kb_writer.flush_now()
        for e in kb_store.query("experiment", {"kind": "backtest"}, limit=20):
            if str(e.get("id")) == exclude_id:
                continue
            comp = (e.get("backtest") or {}).get("components")
            if comp:
                return comp
    except Exception:  # noqa: BLE001
        pass
    return None


def ingest_backtest(result: dict, source: str = "backtest", period: dict | None = None,
                    before_components: dict | None = None) -> dict:
    """回测结果 → experiment 台账 + context 快照。

    - 记录命中率/收益指标（metrics 快照）
    - 写入口径指纹：与上一次回测口径/数据集对比，**不一致 → invalid（不计入统计）**，
      防止"改口径/改数据集让指标变好看"的自欺型进化（plans/21 §九.1）
    """
    if not result:
        return {"ok": False, "reason": "无回测结果"}
    comp = backtest_components(period, result)
    digest = digest_of(comp)
    eid = f"experiment:backtest:{time.strftime('%Y%m%d')}:{digest[-8:]}"
    if before_components is None:
        before_components = _last_backtest_components(exclude_id=eid)
    changed = diff_components(before_components, comp) if before_components else []
    # 红线 vs 非红线（plans/21 §九.1）：
    #   红线 = 主指标口径 / 口径相关参数 / 数据集变化 → 指标不可比，判 invalid 且不计入统计
    #   非红线（如回测时段不同）→ 仅标注"不可直接对比"，不污染统计
    critical = [k for k in changed if k in ("main_metric", "metric_params", "dataset")]
    # 指标取数兼容：历史/现网回测报告把指标放在 performance / samples / ab_validation 下
    # （而非 metrics），只读 metrics 会导致台账里指标全为 None（进化大脑看不到实际能力）
    m = result.get("metrics") or {}
    _perf = result.get("performance")
    _smp = result.get("samples")
    _ab = result.get("ab_validation")
    perf: dict = _perf if isinstance(_perf, dict) else {}
    smp: dict = _smp if isinstance(_smp, dict) else {}
    ab: dict = _ab if isinstance(_ab, dict) else {}

    def _pick(*keys):
        for k in keys:
            v = m.get(k) if isinstance(m, dict) else None
            if v is None and perf:
                v = perf.get(k)
            if v is not None:
                return v
        return None

    metrics = {
        "accuracy": _pick("accuracy"),
        "hit_rate": _pick("hit_rate"),
        "t1_hit_rate": _pick("t1_hit_rate"),
        "win_rate": _pick("win_rate"),
        "avg_ret": _pick("avg_ret", "avg_t5_ret", "avg_t5_return"),
        "samples": _pick("total_samples", "samples") or smp.get("total"),
        # AB 验证结论（配对的 A/C 方案是否值得上）—— 直接决定"要不要改"
        "ab_decision": ab.get("decision"),
        "ab_accuracy_lift": ab.get("accuracy_lift"),
        "ab_reasons": (ab.get("reasons") or [])[:2],
    }
    verdict = "invalid" if critical else None
    if critical:
        # plans/21 §九.1：口径/数据集变动而指标变好 → 告警（防自欺型进化）
        try:
            from app.agents import evolution_events
            evolution_events.record_event(
                "WARNING", "kb_backtest",
                f"回测口径要素发生变化 {critical}，本次指标不可比（已标记 invalid，不计入统计）")
        except Exception:  # noqa: BLE001
            pass
    kb_writer.record_experiment(
        id=eid,
        ts=time.strftime("%Y-%m-%d %H:%M:%S"),
        kind="backtest",
        target={"source": source},
        change={"kind": "backtest_run", "digest": digest},
        hypothesis="回测结果反映当前逻辑/参数下的真实能力（用于指导进化）",
        evidence_in=f"period={comp.get('period')}",
        backtest={"after": metrics, "components": comp, "digest": digest,
                  "unchanged": (not changed), "changed_keys": changed,
                  "critical_changed": critical, "comparable": (not critical)},
        verdict=verdict,
        verdict_reason=("口径/数据集发生变化，指标不可比（防自欺护栏）" if critical else ""),
        status="invalid" if critical else ("effective" if not changed else "applied"),
    )
    ingest_context(time.strftime("%Y%m%d"), extra={"backtest": metrics})
    return {"ok": True, "experiment_id": eid, "digest": digest,
            "changed_keys": changed, "critical_changed": critical,
            "valid": not critical, "metrics": metrics}


# ── 实验台账 / 改动因果链（提案 → 生效 → 结论）────────────────
def record_experiment(id_: str, *, kind: str, target: dict, change: dict,
                      hypothesis: str = "", evidence_in: str = "",
                      backtest: dict | None = None, live: dict | None = None,
                      status: str = "proposed", related_lessons: list[str] | None = None,
                      verdict: str | None = None, verdict_reason: str = "") -> str:
    """写实验台账（含未达标/放弃的尝试；verdict 到期由 update_experiment_verdict 回填）。"""
    return kb_writer.record_experiment(
        id=id_,
        ts=time.strftime("%Y-%m-%d %H:%M:%S"),
        kind=kind, target=target, change=change, hypothesis=hypothesis,
        evidence_in=evidence_in, backtest=backtest, live=live,
        verdict=verdict, verdict_reason=verdict_reason,
        status=status, related_lessons=related_lessons or [],
        timeline=[{"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": status,
                   "detail": (evidence_in or "")[:200]}],
    )


def update_experiment_verdict(id_: str, verdict: str, reason: str = "",
                              live: dict | None = None, status: str | None = None,
                              backtest: dict | None = None) -> bool:
    """回填实验结论（improved/no_change/worse/rolled_back/abandoned/invalid + 原因）。

    - 约定：**verdict_reason 必填**（"改后为啥没达到预期"，plans/21 §5.2）
    - 回填时同步把结论回馈给相关教训（升权/降权/退役）
    """
    fields: dict = {
        "verdict": verdict,
        "verdict_reason": reason or "未说明",
        "status": status or ("rolled_back" if verdict == "rolled_back" else "closed"),
    }
    if live is not None:
        fields["live"] = live
    if backtest is not None:
        fields["backtest"] = backtest
    ok = kb_writer.update_experiment(id_, fields)
    try:
        exp = _pending_experiment.get(id_) or {}
        lessons = exp.get("related_lessons") or []
        if lessons:
            kb_distiller.record_verdict(lessons, id_, verdict)
    except Exception:  # noqa: BLE001
        pass
    return ok


# 进程内缓存最近写入的实验（供更新时取 related_lessons，避免异步读不到）
_pending_experiment: dict[str, dict] = {}


def link_lessons(experiment_id: str, lesson_ids: list[str]) -> None:
    """登记实验关联的教训（使其结论可回馈教训置信度）。"""
    if not experiment_id:
        return
    _pending_experiment[experiment_id] = {"related_lessons": list(lesson_ids or [])}
    if lesson_ids:
        kb_writer.update_experiment(experiment_id, {"related_lessons": list(lesson_ids)})


def reclassify_cases(dry_run: bool = False) -> dict:
    """按最新分类口径重算历史案例 side（幂等）。

    failure 判定不变；主要把"微涨（< success.min_t1）"从 success 降为 neutral，
    避免成功侧规律被微涨噪声污染（人工抽检发现 t1=+0.65% 却被判 success）。
    """
    kb_writer.flush_now()
    rows = kb_store.query("case", limit=6000)
    changed = 0
    dist: dict = {"failure": 0, "success": 0, "neutral": 0}
    for c in rows:
        o = c.get("outcome") or {}
        t1, thr = o.get("t1_ret"), o.get("hit_threshold")
        if t1 is None or thr is None:
            continue
        new_side = classify_side(t1, float(thr), o.get("excess_t1"))
        dist[new_side] = dist.get(new_side, 0) + 1
        if new_side == c.get("side"):
            continue
        changed += 1
        if not dry_run:
            rec = dict(c)
            rec["side"] = new_side
            kb_store.upsert("case", rec)
    return {"scanned": len(rows), "changed": changed, "distribution": dist,
            "dry_run": dry_run}


def inherit_missing_attribution(dry_run: bool = False) -> dict:
    """补全"失败但无归因"的历史案例：用**同形态同期已归因案例**的结论就近继承。

    背景（人工抽检发现）：`daily_verify` 单期归因有条数上限（控 token），
    导致大期（如 206 条）中大量失败案例只有数值、没有原因，归因覆盖率仅约 73%。

    做法（低成本、可解释、不污染教训库）：
      1. 先按 (date, form_type) 取同组已归因案例中置信最高者；
      2. 同组无 → 退化为同 form_type 在库中最近的已归因案例；
      3. 继承结果标记 `inherited=true` + `confidence=low` + `inherited_from=<案例id>`，
         便于人工抽检识别，也便于将来真实归因覆盖；
      4. **不写入教训库**（继承值不参与规律升级，避免以讹传讹）。
    """
    kb_writer.flush_now()
    rows = kb_store.query("case", limit=5000)
    fails = [c for c in rows if c.get("side") == "failure"]
    missing = [c for c in fails if not ((c.get("cause") or {}).get("error_type"))]
    if not missing:
        return {"missing": 0, "filled": 0, "unresolved": 0, "dry_run": dry_run}

    def _conf_rank(c: dict) -> int:
        v = str(((c.get("cause") or {}).get("confidence")) or "").lower()
        return {"high": 3, "mid": 2, "low": 1}.get(v, 0)

    by_date_form: dict[tuple, list[dict]] = {}
    by_form: dict[str, list[dict]] = {}
    for c in fails:
        if not ((c.get("cause") or {}).get("error_type")):
            continue
        by_date_form.setdefault((str(c.get("date")), str(c.get("form_type"))), []).append(c)
        by_form.setdefault(str(c.get("form_type")), []).append(c)
    for v in by_form.values():
        v.sort(key=lambda c: str(c.get("date") or ""), reverse=True)

    filled = unresolved = 0
    for c in missing:
        form = str(c.get("form_type") or "")
        donors = by_date_form.get((str(c.get("date")), form)) or by_form.get(form) or []
        if not donors:
            unresolved += 1
            continue
        d = max(donors, key=_conf_rank)
        dc = dict(d.get("cause") or {})
        new_cause = {
            "error_type": dc.get("error_type", "其他"),
            "stage": dc.get("stage") or "other",
            "stage_hint": dc.get("stage_hint", ""),
            "optimize": dc.get("optimize", ""),
            "lesson": dc.get("lesson", ""),
            "fix": dc.get("fix", ""),
            "signal_hint": dc.get("signal_hint", ""),
            "param_hint": dc.get("param_hint", "无需调参"),
            "confidence": "low",
            "inherited": True,
            "inherited_from": d.get("id"),
            "inherited_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if dry_run:
            filled += 1
            continue
        rec = dict(c)
        rec["cause"] = new_cause
        rec["stage"] = new_cause["stage"]
        if kb_store.upsert("case", rec):
            filled += 1
    return {"missing": len(missing), "filled": filled, "unresolved": unresolved,
            "dry_run": dry_run}


def update_live_by_param(param: str, live: dict, all_rows: bool = False) -> int:
    """把实盘效果追踪结果写入该参数的 experiment.live（improve_effect 追踪时调用）。"""
    if not param:
        return 0
    kb_writer.flush_now()
    rows = [e for e in kb_store.query("experiment", limit=300)
            if str((e.get("target") or {}).get("param") or "") == param]
    rows.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
    n = 0
    for e in (rows if all_rows else rows[:1]):
        eid = str(e.get("id"))
        if eid and kb_writer.update_experiment(eid, {"live": live}):
            n += 1
    return n


def mark_param_applied(param: str, old=None, new=None, note: str = "",
                       file_hint: str = "") -> bool:
    """参数改动落地（self_improver 等热生效路径调用）：对应实验台账标记 applied。

    台账中若无该改动（如人工直接改配置），补建一条 —— 保证"改动必留痕"。
    """
    if not param:
        return False
    eid = f"experiment:{time.strftime('%Y%m%d')}:{param}:{old}->{new}"
    kb_writer.flush_now()
    exists = any(str(e.get("id")) == eid for e in kb_store.query("experiment", limit=300))
    if not exists:
        record_experiment(eid, kind="param", target={"param": param, "file": file_hint},
                          change={"old": old, "new": new},
                          hypothesis=note or "参数落地（补建台账）", status="applied")
        return True
    return kb_writer.update_experiment(eid, {"status": "applied"})


def finalize_param_experiments(param: str, verdict: str, reason: str = "",
                               live: dict | None = None) -> int:
    """按参数名回填实验结论（生命周期结账/回滚时调用）。

    - 更新该参数名下所有尚未定论的 experiment（status=applied/proposed）
    - 同步回填改动因果链（change.verdict / root_cause / observed，plans/21 §5.3）
    - 同步把结论回馈给关联教训（升权/降权/退役，plans/21 §5.4）
    """
    if not param:
        return 0
    kb_writer.flush_now()
    rows = [e for e in kb_store.query("experiment", limit=500)
            if str((e.get("target") or {}).get("param") or "") == param
            and str(e.get("verdict") or "") in ("", "None")]
    n = 0
    for e in rows:
        eid = str(e.get("id"))
        if not eid:
            continue
        fields: dict = {"verdict": verdict, "verdict_reason": reason or "未说明",
                        "status": "closed"}
        if live is not None:
            fields["live"] = live
        if kb_writer.update_experiment(eid, fields):
            n += 1
        lessons = [str(x) for x in (e.get("related_lessons") or [])]
        if lessons:
            kb_distiller.record_verdict(lessons, eid, verdict)
    # 改动因果链同步回填（plans/21 §5.3：为啥改 → 结果 → 是否达预期）
    try:
        for c in kb_store.query("change", limit=500):
            if param not in str(c.get("id") or ""):
                continue
            kb_writer.update_change(str(c.get("id")), {
                "verdict": verdict, "root_cause": reason or "未说明",
                "observed": live or {},
            })
    except Exception:  # noqa: BLE001
        pass
    return n


def record_change(id_: str, *, why: str, what: dict, who: str = "evolution_agent",
                  expectation: str = "", effective_at: str = "") -> str:
    """写改动因果链（为啥改 → 改了什么 → 期望）。"""
    return kb_writer.record_change(
        id=id_, ts=time.strftime("%Y-%m-%d %H:%M:%S"), why=why, what=what, who=who,
        effective_at=effective_at, expectation=expectation,
        observed={}, verdict=None, root_cause="", rollback={},
    )


def update_change_result(id_: str, *, observed: dict | None = None,
                         verdict: str | None = None, root_cause: str = "",
                         rollback: dict | None = None) -> bool:
    """回填改动结果（实测变化 / 结论 / 未达预期的根因 / 是否回滚）。"""
    fields: dict = {"root_cause": root_cause or ""}
    if observed is not None:
        fields["observed"] = observed
    if verdict:
        fields["verdict"] = verdict
    if rollback is not None:
        fields["rollback"] = rollback
    return kb_writer.update_change(id_, fields)


__all__ = [
    "env_snapshot", "build_cases", "ingest_verify_period", "ingest_context",
    "backfill_from_verify_kb",
    "backtest_components", "digest_of", "diff_components", "ingest_backtest",
    "record_experiment", "update_experiment_verdict", "link_lessons",
    "update_live_by_param", "mark_param_applied", "finalize_param_experiments",
    "inherit_missing_attribution",
    "record_change", "update_change_result",
]
