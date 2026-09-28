"""改进效果追踪（improve_effect）— 对齐 plans/11 进化Agent §12.1/F7 + 终身记账衔接

职责：
  - 改进生效时记录基线（record_baseline）
  - 定期对比当前命中率（track_effect）
  - 供 guard 主指标监控与前端「进化中心」效果看板使用

数据：backend/data/improve_effect.json（EFFECT_FILE 在此模块，evolution_shadow 兼容复用）
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
EFFECT_FILE = os.path.join(DATA_DIR, "improve_effect.json")
MAX_EFFECT_RECORDS = 200  # Bug41：效果追踪记录滚动上限，防无限增长（同 Bug18 decisions）


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: str, data) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[improve_effect] 写入失败 {path}: {exc}")


def record_baseline(param: str, value) -> None:
    """改进生效时记录当前 monitor 命中率基线。"""
    # Bug42：整体包 try —— build_l0_summary 异常不能中断调用方（settle_shadow 生效路径）
    try:
        s = evolution_summarizer.build_l0_summary()
        m = s.get("monitor", {})
        eff = _read_json(EFFECT_FILE, {"records": []})
        eff.setdefault("records", []).append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "param": param, "value": value,
            "baseline_hit_rate": m.get("hit_rate", 0.0),
            "baseline_composite": m.get("composite", 0.0),
            "baseline_t1_hit_rate": m.get("t1_hit_rate", 0.0),
            "baseline_wave_hit_rate": m.get("wave_hit_rate", 0.0),
            "baseline_samples": m.get("samples", 0),
            "status": "tracking",
        })
        eff["records"] = eff["records"][-MAX_EFFECT_RECORDS:]  # Bug41 滚动上限
        _write_json(EFFECT_FILE, eff)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[improve_effect] 记录基线失败 {param}: {exc}")


def track_effect(param: str) -> dict:
    """对比当前命中率 vs 基线（效果追踪，含综合目标分 composite）。"""
    try:
        eff = _read_json(EFFECT_FILE, {"records": []})
        recs = [r for r in eff.get("records", []) if r.get("param") == param and r.get("status") == "tracking"]
        if not recs:
            return {"ok": False, "reason": f"无 {param} 效果追踪记录"}
        s = evolution_summarizer.build_l0_summary()
        m = s.get("monitor", {})
        latest = recs[-1]
        latest["current_hit_rate"] = m.get("hit_rate", 0.0)
        latest["current_composite"] = m.get("composite", 0.0)
        latest["current_t1_hit_rate"] = m.get("t1_hit_rate", 0.0)
        latest["current_wave_hit_rate"] = m.get("wave_hit_rate", 0.0)
        latest["current_samples"] = m.get("samples", 0)
        latest["delta_hit_rate"] = round(m.get("hit_rate", 0.0) - latest.get("baseline_hit_rate", 0.0), 4)
        latest["delta_composite"] = round(m.get("composite", 0.0) - latest.get("baseline_composite", 0.0), 4)
        latest["tracked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(EFFECT_FILE, eff)
        # plans/21 §七：效果追踪写回知识库实验台账 live（供"改后是否达预期"判定）
        try:
            from app.kb import kb_ingest
            kb_ingest.update_live_by_param(param, {
                "current_hit_rate": latest.get("current_hit_rate"),
                "current_composite": latest.get("current_composite"),
                "delta_hit_rate": latest.get("delta_hit_rate"),
                "delta_composite": latest.get("delta_composite"),
                "current_samples": latest.get("current_samples"),
                "tracked_at": latest.get("tracked_at"),
            })
        except Exception:  # noqa: BLE001
            pass
        return latest
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[improve_effect] 效果追踪失败 {param}: {exc}")
        return {"ok": False, "reason": str(exc)}


def effect_report() -> dict:
    """效果追踪报告（供前端）。"""
    eff = _read_json(EFFECT_FILE, {"records": []})
    return {"records": eff.get("records", [])}


__all__ = ["record_baseline", "track_effect", "effect_report", "EFFECT_FILE"]
