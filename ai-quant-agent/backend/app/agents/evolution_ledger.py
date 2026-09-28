"""进化终身记账（evolution_ledger）— 对齐 plans/11 进化Agent §12.7/G4

记录每项改进**从生效到被替代/回滚的整个生命周期**的累积效果（命中率/收益变化），
回答"哪个进化决策长期真正赚钱"（不只短期影子通过）。
"""
from __future__ import annotations

import json
import os
import time

from app.core.logger import logger
from app.agents import evolution_summarizer

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
LEDGER_FILE = os.path.join(DATA_DIR, "evolution_ledger.json")


def _read() -> dict:
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"entries": {}}


def _write(data: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LEDGER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution_ledger] 写入失败: {exc}")


def _current_stats() -> dict:
    return evolution_summarizer.build_l0_summary().get("monitor", {})


def open_entry(param: str, old, new, baseline_hit_rate: float | None = None) -> dict:
    """改进生效时开账：记录旧值/新值/基线命中率/开始时间。

    Bug13 修复：若该参数已有 active 记账（上一笔未结），先自动结账（replaced），
    结账记录追加到 history（entries 单键只保留当前 active，避免覆盖丢失历史）。
    """
    data = _read()
    data.setdefault("history", [])
    prev = data.get("entries", {}).get(param)
    if prev and prev.get("status") == "active":
        m = _current_stats()
        prev["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        prev["status"] = "closed"
        prev["close_reason"] = "replaced_by_new"
        prev["final_hit_rate"] = m.get("hit_rate", 0.0)
        prev["final_delta"] = round(m.get("hit_rate", 0.0) - prev.get("baseline_hit_rate", 0.0), 4)
        data["history"].append(prev)
        logger.info(f"[evolution_ledger] {param} 被新改进替代，旧账自动结清")
    m = _current_stats()
    entry = {
        "param": param, "old": old, "new": new,
        "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "baseline_hit_rate": baseline_hit_rate if baseline_hit_rate is not None else m.get("hit_rate", 0.0),
        "baseline_samples": m.get("samples", 0),
        "closed_at": None, "status": "active",
        "final_delta": None,
    }
    data["entries"][param] = entry
    _write(data)
    # plans/21 §七：改进生效 → 写入私有域知识库实验台账（改了什么/预期依据/基线）
    try:
        from app.kb import kb_ingest as _kbi
        _kbi.record_experiment(
            f"experiment:{time.strftime('%Y%m%d')}:{param}:{old}->{new}",
            kind="param", target={"param": param},
            change={"old": old, "new": new},
            hypothesis="改进提案生效（先验证后生效闭环）",
            evidence_in=(f"基线命中率 {entry.get('baseline_hit_rate')}"
                         f"（样本 {entry.get('baseline_samples')}）"),
            backtest={"before": {"hit_rate": entry.get("baseline_hit_rate"),
                                 "samples": entry.get("baseline_samples")}},
            status="applied",
        )
    except Exception:  # noqa: BLE001
        pass
    return entry


def update_entry(param: str) -> dict:
    """定期更新当前效果（累积命中率对比）。"""
    data = _read()
    e = data.get("entries", {}).get(param)
    if not e or e.get("status") != "active":
        return {}
    m = _current_stats()
    e["current_hit_rate"] = m.get("hit_rate", 0.0)
    e["current_samples"] = m.get("samples", 0)
    e["delta_hit_rate"] = round(m.get("hit_rate", 0.0) - e.get("baseline_hit_rate", 0.0), 4)
    e["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["entries"][param] = e
    _write(data)
    return e


def close_entry(param: str, reason: str = "replaced") -> dict:
    """改进被替代/回滚时结账：记录生命周期累积效果（追加到 history 保留历史）。"""
    data = _read()
    data.setdefault("history", [])
    e = data.get("entries", {}).get(param)
    if not e:
        return {}
    m = _current_stats()
    e["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    e["status"] = "closed"
    e["close_reason"] = reason
    e["final_hit_rate"] = m.get("hit_rate", 0.0)
    e["final_samples"] = m.get("samples", 0)
    e["final_delta"] = round(m.get("hit_rate", 0.0) - e.get("baseline_hit_rate", 0.0), 4)
    data["history"].append(e)
    data["entries"].pop(param, None)  # 结账后移出 active
    _write(data)
    logger.info(f"[evolution_ledger] {param} 结账: 生命周期命中率变化 {e['final_delta']*100:+.1f}pp（{reason}）")
    # plans/21 §5.2/§5.4：结账 → 回填实验 verdict（improved/no_change/worse）+ 回馈教训置信度
    try:
        from app.kb import kb_ingest as _kbi
        delta = float(e.get("final_delta") or 0.0)
        verdict = "improved" if delta > 0.02 else ("worse" if delta < -0.02 else "no_change")
        _kbi.finalize_param_experiments(
            param, verdict,
            reason=(f"生命周期命中率变化 {delta*100:+.1f}pp（{reason}，样本 {e.get('final_samples')}）；"
                    f"未达预期原因：" + ("效果不足（改善 <2pp）" if verdict == "no_change"
                                        else ("指标下降" if verdict == "worse" else "达标"))),
            live={"final_hit_rate": e.get("final_hit_rate"),
                  "final_samples": e.get("final_samples"), "final_delta": delta},
        )
    except Exception:  # noqa: BLE001
        pass
    return e


def ledger_report() -> dict:
    """终身记账报告。"""
    data = _read()
    entries = data.get("entries", {})
    history = data.get("history", [])
    active = [e for e in entries.values() if e.get("status") == "active"]
    closed = [e for e in history if e.get("status") == "closed"]
    pos = [e for e in closed if (e.get("final_delta") or 0) > 0]
    return {
        "active": active, "closed": closed,
        "positive_closed": len(pos), "closed_total": len(closed),
        "summary": f"已结账 {len(closed)} 项，其中 {len(pos)} 项生命周期命中率提升" if closed else "暂无结账改进",
    }


__all__ = ["open_entry", "update_entry", "close_entry", "ledger_report"]
