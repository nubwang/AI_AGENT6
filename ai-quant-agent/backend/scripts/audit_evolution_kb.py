"""知识库人工抽检导出（plans/21 §十四 验证口径的落地工具）

用途：把近 N 期（默认 17 期）知识库案例导出为**人工可核对**的文件，并做**独立一致性校验**
（不依赖知识库自身的计算路径，用 monitor_state / verify_kb / 指数 CSV 交叉比对）。

产出（backend/data/kb_audit/）：
  1. cases_all_<ts>.csv        全量案例（Excel 可查、可筛选）
  2. spotcheck_<ts>.csv        抽样待人工核对清单（含原始值与库内值对照 + 核对结论空白列）
  3. consistency_<ts>.md       一致性报告（5 项自动校验结果 + 异常明细）

用法：
  cd backend && python scripts/audit_evolution_kb.py --periods 17 --sample 30
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.kb import kb_store  # noqa: E402

DATA_DIR = kb_store.DATA_DIR
OUT_DIR = os.path.join(DATA_DIR, "kb_audit")
VERIFY_KB = os.path.join(DATA_DIR, "verify_kb.json")
MONITOR = os.path.join(DATA_DIR, "monitor_state.json")
INDEX_CSV = os.path.join(DATA_DIR, "market_env_000001.SH.csv")

CASE_COLS = [
    "date", "ts_code", "name", "side", "form_type", "stage",
    "t1_ret", "t5_ret", "excess_t1", "bench_t1", "hit_threshold", "hit",
    "regime", "index_t1_ret",
    "similarity", "up_probability", "negative_score", "pred_prob",
    "error_type", "cause_stage", "confidence", "lesson", "fix", "param_hint",
    "source", "pair_id",
]


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _flatten(c: dict) -> dict:
    o = c.get("outcome") or {}
    s = c.get("snapshot") or {}
    e = c.get("env") or {}
    cause = c.get("cause") or {}
    return {
        "date": c.get("date"), "ts_code": c.get("ts_code"), "name": c.get("name"),
        "side": c.get("side"), "form_type": c.get("form_type"),
        "stage": cause.get("stage") or c.get("stage"),
        "t1_ret": o.get("t1_ret"), "t5_ret": o.get("t5_ret"),
        "excess_t1": o.get("excess_t1"), "bench_t1": o.get("bench_t1"),
        "hit_threshold": o.get("hit_threshold"), "hit": o.get("hit"),
        "regime": e.get("regime"), "index_t1_ret": e.get("index_t1_ret"),
        "similarity": s.get("similarity"), "up_probability": s.get("up_probability"),
        "negative_score": s.get("negative_score"), "pred_prob": o.get("pred_prob"),
        "error_type": cause.get("error_type"), "cause_stage": cause.get("stage"),
        "confidence": cause.get("confidence"), "lesson": cause.get("lesson"),
        "fix": cause.get("fix"), "param_hint": cause.get("param_hint"),
        "source": c.get("source"), "pair_id": c.get("pair_id"),
    }


def _index_t1(date_str: str) -> float | None:
    """独立重算：上证指数 D+1 买入持有 1 日收益（与案例口径一致）。"""
    dates, closes = [], []
    try:
        with open(INDEX_CSV, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = str(row.get("trade_date", "")).strip()
                if not d:
                    continue
                dates.append(d)
                closes.append(float(row.get("close") or 0))
    except Exception:  # noqa: BLE001
        return None
    for i, d in enumerate(dates):
        if d >= str(date_str)[:8]:
            buy = i + 1
            if buy + 1 < len(closes) and closes[buy] > 0:
                return round(closes[buy + 1] / closes[buy] - 1.0, 4)
            return None
    return None


def audit(periods: int = 0, sample: int = 30) -> dict:
    """periods=0 → 导出知识库实际覆盖的**全部期数**（推荐：避免少取一期导致"案例数对不上"的误判）。"""
    vkb = _read_json(VERIFY_KB, {"verifications": []})
    all_verifs = vkb.get("verifications") or []
    verifs = all_verifs[-periods:] if periods and periods > 0 else all_verifs
    period_dates = [str(v.get("date"))[:8] for v in verifs]
    hits = _read_json(MONITOR, {"hits": []}).get("hits", [])
    hit_map = {(str(h.get("date"))[:8], str(h.get("ts_code"))): h for h in hits}

    cases: list[dict] = []
    for d in period_dates:
        cases += kb_store.query("case", {"date": d}, limit=3000)
    flat = [_flatten(c) for c in cases]

    # ── 校验 1：每期案例数 vs 验证报告 total ──────────────────
    per_period = []
    for v in verifs:
        d = str(v.get("date"))[:8]
        n_kb = sum(1 for r in flat if r["date"] == d)
        per_period.append({
            "date": d, "kb_cases": n_kb, "verify_total": v.get("total"),
            "t1_hit_rate": v.get("t1_hit_rate"),
            "kb_failure": sum(1 for r in flat if r["date"] == d and r["side"] == "failure"),
            "kb_success": sum(1 for r in flat if r["date"] == d and r["side"] == "success"),
            "kb_neutral": sum(1 for r in flat if r["date"] == d and r["side"] == "neutral"),
            "ok": (v.get("total") in (None, n_kb)),
        })

    # ── 校验 2：side 与 t1_ret/阈值自洽 ──────────────────────
    bad_side = []
    for r in flat:
        t1, thr = r.get("t1_ret"), r.get("hit_threshold")
        if t1 is None or thr is None:
            continue
        if r["side"] == "failure" and t1 > float(thr) + 1e-9:
            bad_side.append(r)
        if r["side"] == "success" and t1 <= float(thr) + 1e-9:
            bad_side.append(r)

    # ── 校验 3：库内 t1_ret vs 原始 monitor_state ─────────────
    mismatch = []
    checked = 0
    for r in flat:
        h = hit_map.get((str(r["date"])[:8], str(r["ts_code"])))
        if not h or h.get("t1_ret") is None or r.get("t1_ret") is None:
            continue
        checked += 1
        if abs(float(h["t1_ret"]) - float(r["t1_ret"])) > 1e-6:
            mismatch.append({**r, "raw_t1": h.get("t1_ret")})

    # ── 校验 4：excess = t1 - bench 且 bench 可独立重算 ───────
    bad_excess = []
    excess_none = 0
    for r in flat:
        if r.get("excess_t1") is None:
            excess_none += 1
            continue
        t1, bench = float(r["t1_ret"] or 0), float(r["bench_t1"] or 0)
        if abs((t1 - bench) - float(r["excess_t1"])) > 1e-6:
            bad_excess.append(r)
    idx_recheck = []
    for r in flat[:200]:
        if r.get("bench_t1") is None:
            continue
        calc = _index_t1(r["date"])
        if calc is not None and abs(calc - float(r["bench_t1"])) > 1e-6:
            idx_recheck.append({**r, "bench_recalc": calc})

    # ── 校验 5：失败案例归因覆盖率 ───────────────────────────
    fails = [r for r in flat if r["side"] == "failure"]
    attr_ok = [r for r in fails if r.get("error_type")]
    attr_low = [r for r in fails if str(r.get("confidence") or "").lower() == "low"]

    # ── 校验 6：新增模块知识是否真的"接上神经"（plans/22·23·24·25）────
    plan_n = kb_store.count("plan")
    ev_rows = kb_store.query_lessons_by_type(["evidence", "discipline"], limit=2000)
    ev_by_module: dict = {}
    for l in ev_rows:
        t = str(l.get("type") or "")
        mod = t.split(":", 1)[1] if ":" in t else t
        ev_by_module[mod] = ev_by_module.get(mod, 0) + 1
    ev_active = sum(1 for l in ev_rows if str(l.get("status")) == "active")

    # ── 抽样：可疑项（≤半数）+ 失败 + 成功，保证抽样均衡 ─────
    suspicious: list[dict] = []
    suspicious += [r for r in flat if r["side"] == "success" and r.get("excess_t1") is None]
    suspicious += [r for r in attr_low]
    suspicious += [r for r in fails if not r.get("error_type")]
    successes = [x for x in flat if x["side"] == "success"]
    picked: list[dict] = []
    seen: set = set()

    def _take(pool: list[dict], cap: int) -> None:
        added = 0
        for r in pool:
            if added >= cap or len(picked) >= sample:
                break
            key = (r["date"], r["ts_code"])
            if key in seen:
                continue
            seen.add(key)
            picked.append(r)
            added += 1

    _take(suspicious, max(1, sample // 2))                  # 可疑项最多一半（避免全是同类）
    _take(fails, max(1, (sample - len(picked)) // 2))        # 失败案例
    _take(successes, sample)                                 # 成功案例补足
    _take(flat, sample)                                      # 其余（中性等）

    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(OUT_DIR, exist_ok=True)
    all_path = os.path.join(OUT_DIR, f"cases_all_{ts}.csv")
    spot_path = os.path.join(OUT_DIR, f"spotcheck_{ts}.csv")
    rep_path = os.path.join(OUT_DIR, f"consistency_{ts}.md")

    with open(all_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CASE_COLS, extrasaction="ignore")
        w.writeheader()
        for r in flat:
            w.writerow(r)

    # 抽样表：附加"原始值/重算值"与人工核对列
    spot_cols = CASE_COLS + ["raw_t1_from_monitor", "bench_recalc", "人工核对结论(请手填)", "备注"]
    with open(spot_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=spot_cols, extrasaction="ignore")
        w.writeheader()
        for r in picked:
            h = hit_map.get((str(r["date"])[:8], str(r["ts_code"]))) or {}
            row = dict(r)
            row["raw_t1_from_monitor"] = h.get("t1_ret")
            row["bench_recalc"] = _index_t1(r["date"])
            row["人工核对结论(请手填)"] = ""
            row["备注"] = ""
            w.writerow(row)

    lines = [
        f"# 私有域知识库一致性报告（{ts}）",
        "",
        f"- 抽查期数：**{len(period_dates)}**（{period_dates[0]} ~ {period_dates[-1]}）",
        f"- 案例总数：**{len(flat)}**（failure {len(fails)} / success "
        f"{sum(1 for r in flat if r['side'] == 'success')} / neutral "
        f"{sum(1 for r in flat if r['side'] == 'neutral')}）",
        f"- 导出文件：`{os.path.basename(all_path)}`、`{os.path.basename(spot_path)}`",
        "",
        "## 1. 每期案例数 vs 验证报告（应完全一致）",
        "",
        "| 日期 | 知识库案例 | 报告 total | 一致 | 失败/成功/中性 | 报告T+1命中率 |",
        "| ---- | ---------- | ---------- | ---- | -------------- | ------------- |",
    ]
    for p in per_period:
        lines.append(
            f"| {p['date']} | {p['kb_cases']} | {p['verify_total']} | "
            f"{'✅' if p['ok'] else '❌'} | {p['kb_failure']}/{p['kb_success']}/{p['kb_neutral']} | "
            f"{p['t1_hit_rate']} |")
    bad_periods = [p for p in per_period if not p["ok"]]
    lines += [
        "",
        f"→ **不一致期数：{len(bad_periods)}**（期望 0）",
        "",
        "## 2. side 与 t1_ret/阈值自洽（失败=未达阈值、成功=达阈值）",
        "",
        f"→ 矛盾条数：**{len(bad_side)}**（期望 0）",
    ]
    for r in bad_side[:10]:
        lines.append(f"- {r['date']} {r['ts_code']} side={r['side']} t1={r['t1_ret']} "
                     f"thr={r['hit_threshold']}")

    lines += [
        "",
        "## 3. 库内 t1_ret vs 原始 monitor_state（独立来源交叉验证）",
        "",
        f"→ 可比对 {checked} 条，**不一致 {len(mismatch)} 条**（期望 0）",
    ]
    for r in mismatch[:10]:
        lines.append(f"- {r['date']} {r['ts_code']} 库内={r['t1_ret']} 原始={r['raw_t1']}")

    lines += [
        "",
        "## 4. 超额收益（excess）与基线（bench）",
        "",
        f"- excess 缺失（指数 CSV 未覆盖该日期）：**{excess_none}** 条 ← 需补新指数数据后重跑回填",
        f"- excess ≠ t1 - bench 的矛盾条数：**{len(bad_excess)}**（期望 0）",
        f"- bench 独立重算不符条数：**{len(idx_recheck)}**（期望 0）",
    ]
    for r in idx_recheck[:10]:
        lines.append(f"- {r['date']} {r['ts_code']} 库内 bench={r['bench_t1']} 重算={r['bench_recalc']}")

    lines += [
        "",
        "## 5'. 新增模块知识接入（plans/22·23·24·25 —— 进化大脑的神经）",
        "",
        f"- 交易计划台账（kb_plan，纪律效果载体）：**{plan_n}** 条",
        f"- 证据型/纪律教训（evidence:*、discipline）：**{len(ev_rows)}** 条，其中 active **{ev_active}** 条",
        "- 分模块：" + ("、".join(f"{k} {v}" for k, v in sorted(ev_by_module.items(),
                                                          key=lambda kv: -kv[1])) or "（无）"),
        "",
        "> 期望：洗盘 A/B、四类买点、博弈特征、情绪门控、纪律网格、自证回测、最佳入场、"
        "条件概率表、形态规则有效性、成本口径、环境分布 均有条目；缺失说明模块产物未生成"
        "或未运行 `distill_and_reindex` / `backfill_evolution_kb.py`。",
    ]
    lines += [
        "",
        "## 5. 失败案例归因覆盖率",
        "",
        f"- 失败案例 **{len(fails)}** 条，其中有归因 **{len(attr_ok)}** 条"
        f"（覆盖率 {len(attr_ok) / max(1, len(fails)) * 100:.1f}%）",
        f"- 低置信归因：{len(attr_low)} 条（attributes 为 low，仅作提示）",
        "",
        "## 6. 人工抽检建议（打开 spotcheck 文件逐条核对）",
        "",
        "1. **数值核对**：t1_ret / excess_t1 是否与行情实际相符（表内已给原始值与指数重算值）；",
        "2. **分类核对**：side 是否符合「次日未涨 = 失败」的定义；",
        "3. **归因质量**：error_type / stage / lesson 是否**真的解释了该股为何没涨**"
        "（重点看抽样中优先挑出的可疑项）；",
        "4. **对照完整性**：成功案例是否都有同日同形态失败对照（pair_id 相同）；",
        "5. 发现错误：在表内填「人工核对结论」列，并可直接调 "
        "`/evolve/kb/lesson/feedback` 废弃错误教训。",
        "",
        "> 抽样策略：优先挑「成功但无超额数据」/「低置信归因」/「失败但无归因」的可疑项，"
        "其余按失败、成功补齐。",
    ]
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return {
        "report": rep_path, "cases_csv": all_path, "spotcheck_csv": spot_path,
        "periods": len(period_dates), "cases": len(flat),
        "period_mismatch": len(bad_periods), "side_conflict": len(bad_side),
        "t1_mismatch": len(mismatch), "t1_checked": checked,
        "excess_none": excess_none, "excess_conflict": len(bad_excess),
        "bench_recheck_conflict": len(idx_recheck),
        "failure_attr_coverage": round(len(attr_ok) / max(1, len(fails)), 4),
        "spotcheck_n": len(picked),
        "plan_n": plan_n, "evidence_n": len(ev_rows), "evidence_active": ev_active,
        "evidence_by_module": ev_by_module,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="知识库近 N 期案例抽检导出（--periods 0 = 全部期）")
    ap.add_argument("--periods", type=int, default=0)
    ap.add_argument("--sample", type=int, default=30)
    args = ap.parse_args()
    r = audit(args.periods, args.sample)
    print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
