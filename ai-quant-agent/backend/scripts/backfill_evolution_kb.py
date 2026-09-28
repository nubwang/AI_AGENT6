"""历史回填脚本：把既有知识资产灌入私有域知识库（EKB）

对齐 plans/21 §十.4（首拉回填）：
  只读原始文件，**不修改任何现有数据文件**；重复执行幂等（id 为确定性指纹）。

回填来源：
  1. verify_kb.json        → case（失败/成功/中性）+ 失败侧教训 + context
  2. monitor_state.json    → case 的 outcome（T+1/T+5/T+20）
  3. daily_recommend/*.json→ case 的推荐依据快照
  4. self_improve_log.json → experiment（参数型改动：变更为何/结果）
  5. code_writer_state.json→ experiment + change（代码型改动：为啥改/结果/回滚）
  6. guard.applied_log / regression → experiment.verdict（含回滚与未达标标记）
  7. 与现网下一致：stocks 环境快照由 kb_ingest.env_snapshot 计算（指数 CSV）

用法：
  cd backend && python scripts/backfill_evolution_kb.py            # 全量（默认最近 60 期）
  cd backend && python scripts/backfill_evolution_kb.py --periods 200
  cd backend && python scripts/backfill_evolution_kb.py --no-experiments
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.kb import kb_store, kb_writer, kb_distiller, kb_ingest  # noqa: E402

DATA_DIR = kb_store.DATA_DIR
VERIFY_KB = os.path.join(DATA_DIR, "verify_kb.json")
MONITOR = os.path.join(DATA_DIR, "monitor_state.json")
SELF_LOG = os.path.join(DATA_DIR, "self_improve_log.json")
REFLECT_KB = os.path.join(DATA_DIR, "reflect_kb.json")
CW_STATE = os.path.join(DATA_DIR, "code_writer_state.json")
GUARD_STATE = os.path.join(DATA_DIR, "evolution_guard_state.json")
BACKTEST_LATEST = os.path.join(DATA_DIR, "backtest_results", "backtest_latest.json")


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def backfill_cases(periods: int = 60) -> dict:
    """verify_kb + monitor_state + 榜单明细 → case 档案 + 失败侧教训 + context。

    复用 app.kb.kb_ingest.backfill_from_verify_kb（与 API /evolve/kb/backfill 同一实现）。
    """
    return kb_ingest.backfill_from_verify_kb(periods=periods)


def backfill_module_evidence() -> dict:
    """plans/23·24·25 模块验证产出 → 证据型 lesson（幂等）。

    来源：洗盘 A/B、四类买点、博弈特征（FDR）、情绪门控、纪律网格、最佳入场、
    条件概率表、形态规则有效性、成本口径、环境分布、自证回测报告。
    这些是"接神经"的一步：让进化大脑看得见已被检验过的结论。
    """
    from app.kb import kb_evidence
    r = kb_evidence.absorb_all()
    kb_writer.flush_now()
    return r


def backfill_reflections() -> dict:
    """reflect_kb.json（T+5 慢反思，6883+ 条）→ 教训库（累积型，幂等）。"""
    d = _read_json(REFLECT_KB, {"reflections": []})
    refls = d.get("reflections") or []
    if not refls:
        return {"reflections": 0, "absorbed": 0}
    # 全量吸收（历史回填场景），不受 recent_days 限制
    n = kb_distiller.absorb_reflections(refls, max_items=len(refls) + 1, recent_days=36500)
    kb_writer.flush_now()
    return {"reflections": len(refls), "absorbed": n}


def backfill_backtest() -> dict:
    """最新回测结果 → experiment（带口径指纹）。"""
    r = _read_json(BACKTEST_LATEST, None)
    if not isinstance(r, dict):
        return {"ok": False, "reason": "无 backtest_latest.json"}
    return kb_ingest.ingest_backtest(r, source="backfill")


def backfill_experiments() -> dict:
    """self_improve_log + code_writer_state + guard.applied_log/regression → experiment/change。"""
    # 参数型改动
    logs = _read_json(SELF_LOG, [])
    if not isinstance(logs, list):
        logs = []
    # 代码型改动（含回滚与未达标）
    cw = _read_json(CW_STATE, {"history": [], "regression": []})
    guard = _read_json(GUARD_STATE, {"applied_log": []})

    verdict_by_param: dict[str, str] = {}
    reason_by_param: dict[str, str] = {}
    for a in (guard.get("applied_log") or []):
        if a.get("status") != "ok":
            verdict_by_param[str(a.get("param"))] = "abandoned"
            reason_by_param[str(a.get("param"))] = "守护拦截/应用失败（见 applied_log）"
    for r in (cw.get("regression") or []):
        tgt = str(r.get("target") or r.get("file") or "")
        if tgt:
            verdict_by_param[tgt] = "rolled_back"
            reason_by_param[tgt] = (f"生效后命中率连续 {r.get('low_count')} 次低于基线，"
                                    f"自动回滚（baseline_composite={r.get('baseline_composite')}）")

    n_exp = n_chg = 0
    for it in logs:
        param = str(it.get("param") or "")
        if not param:
            continue
        ts = str(it.get("ts") or "")[:8] or time.strftime("%Y%m%d")
        eid = f"experiment:{ts}:{param}:{it.get('old')}->{it.get('new')}"
        verdict = None
        status = "applied" if it.get("status") in (None, "ok") else "rejected"
        reason = ""
        if status == "rejected":
            verdict, reason = "abandoned", str(it.get("note") or "应用失败")
        if param in verdict_by_param:
            verdict, reason = verdict_by_param[param], reason_by_param.get(param, "")
        kb_ingest.record_experiment(
            eid, kind="param",
            target={"param": param, "file": it.get("file")},
            change={"old": it.get("old"), "new": it.get("new"),
                    "tier": it.get("tier"), "mode": it.get("mode")},
            hypothesis=str(it.get("reason") or "")[:200],
            evidence_in=str(it.get("evidence") or "")[:200],
            verdict=verdict, verdict_reason=reason or "回填：历史结论未记录（见 improve_effect）",
            status=status if not verdict else "closed",
        )
        n_exp += 1
    for h in (cw.get("history") or []):
        f = str(h.get("file") or "")
        ts = str(h.get("ts") or "")[:8].replace("-", "") or time.strftime("%Y%m%d")
        cid = f"change:{ts}:{f}:{str(h.get('backup') or 'x')[-8:]}"
        kb_ingest.record_change(
            cid, why=str(h.get("note") or h.get("target") or "代码进化")[:200],
            what={"file": f, "scope": h.get("target") or "auto", "backup": h.get("backup")},
            who="code_writer", expectation=str(h.get("target") or ""),
            effective_at=str(h.get("ts") or ""),
        )
        n_chg += 1
    kb_writer.flush_now()
    return {"experiments": n_exp, "changes": n_chg}


def main() -> None:
    ap = argparse.ArgumentParser(description="回填私有域知识库（EKB）")
    ap.add_argument("--periods", type=int, default=60, help="回填最近 N 期次日验证（默认 60）")
    ap.add_argument("--no-experiments", action="store_true", help="跳过实验台账回填")
    ap.add_argument("--no-backtest", action="store_true", help="跳过回测结果回填")
    args = ap.parse_args()

    print("== 初始化知识库 ==")
    print("init_schema:", kb_store.init_schema())
    t0 = time.time()
    print("== 回填案例/教训/环境快照 ==")
    print(json.dumps(backfill_cases(args.periods), ensure_ascii=False))
    print("== 回填 T+5 慢反思教训 ==")
    print(json.dumps(backfill_reflections(), ensure_ascii=False))
    print("== 接入模块验证产出（plans/23·24·25：洗盘/买点/博弈/情绪/纪律/自证）==")
    print(json.dumps(backfill_module_evidence(), ensure_ascii=False))
    print("== 外部软知识库挂引用（新闻/决策/政策意图/归因统计）==")
    print(json.dumps(kb_ingest.import_external_kbs(), ensure_ascii=False))
    if not args.no_backtest:
        print("== 回填回测结果（含口径指纹）==")
        print(json.dumps(backfill_backtest(), ensure_ascii=False))
    if not args.no_experiments:
        print("== 回填实验台账/改动因果链 ==")
        print(json.dumps(backfill_experiments(), ensure_ascii=False))
    kb_writer.flush_now()
    print("== 知识库总览 ==")
    print(json.dumps(kb_store.stats(), ensure_ascii=False, indent=2))
    print("教训库:", json.dumps(kb_distiller.stats(), ensure_ascii=False)[:600])
    print(f"用时 {time.time() - t0:.1f}s，库大小 {kb_store.db_size_mb()} MB")


if __name__ == "__main__":
    main()
