"""纪律参数扫描 + 进化提案（plans/23 §14.8 的 P1-7b）

## 为什么必须"先扫描、再提提案"，而不是直接改参数

§14.8 实测：突破买入在 D+1 高开 1~3% 时 T+5 均值 −2.07%、胜率 34.3%（n=329）。
**看起来**应该把 `plan_max_chase_pct` 从 3% 收到 1%。但这里有两个坑，必须先讲清楚：

1. **那 329 个样本是"突破买入"的子样本**，不能直接外推到"所有买入"；
2. **收紧阈值会同时减少机会**（买不到 = 没有这笔交易）。只看"剩下的交易收益变好了"
   必然成立 —— 这是**典型的选择偏差**，不是 alpha。

所以本模块把「**放弃的机会有多差**」和「**成交率损失多少**」放在同一张表里，
并做**显著性检验**（Welch t，双侧，非配对：被放弃组 vs 保留组），
作为提案的 evidence 与影子实验的判定依据。

## 口径（与 build_buy_type_samples.py 完全一致）

- 样本 `records[]`；`gap` = D+1 高开幅度；`rets.baseline.t5` = **按 D+1 开盘买入**持有的 T+5；
- 规则「高开 ≤ chase 才买」在 baseline 口径下是**同口径**的：封板样本 baseline 本身就未成交，
  天然被排除（真实的"买不到"），不需要额外假设；
- 收益一律**毛收益同基准对比**：成本对所有阈值是同一常数，不影响阈值之间的排序。

## 边界（诚实登记）

- 单一评估期（近 ~460 交易日）、单一市场环境 → 结论**只能作为提案依据**，
  真正生效必须走影子实验（[`evolution_shadow.start_shadow`](ai-quant-agent/backend/app/agents/evolution_shadow.py:70)）；
- 本模块**只写提案（status=pending），绝不调用 `set_param`**（写入前有断言式护栏）。
"""
from __future__ import annotations

import json
import os
import time
from math import sqrt

import numpy as np

from app.core.logger import logger

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
SAMPLES_FILE = os.path.join(DATA_DIR, "buy_type_samples.json")

TARGET_PARAM = "plan_max_chase_pct"
CURRENT_DEFAULT = 0.03          # 与 trade_plan.DEFAULTS / evolution_config 一致
PROPOSED_NEW = 0.01             # §14.8 的高开分桶指向 1%（1~3% 桶最差）
CHASE_VALUES = (0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.10)
MIN_GROUP_N = 100               # 两组比较的最小样本（低于此不做检验）
ALPHA = 0.05
ENTRY_TYPES = ("breakout", "pullback", "dip", "auction")

# ── ⚠️ 实测结论（2026-09-20，86,162 个样本）：**不要全局收紧** ──────────
# 全部样本：被放弃组（高开 1~3%，n=6,996）T+5 +0.372% vs 保留组 +0.463%，
# Welch t=−0.79 p=0.43 → **无统计依据**，而且要付出 8.1% 的成交率代价。
# 分组看才发现真相：效果**只存在于突破买入**（n=2,920；被放弃组 −1.742% vs +0.074%，p=0.0046），
# 低吸上甚至**是反的**（被放弃组 +0.386% vs +0.171%，t=+0.86）。
# 结论：§14.8 的"收紧追高上限"在**全局口径下不成立**（突破只占样本 3.4%），
# 正确表述是"**不该追高开 1% 以上的突破**"这一**入场类型条件规则**，
# 而不是动这个全局参数 → 见 analyze_by_entry_type() 与 plans/23 §14.11。


def _t_sf(t: float, df: int) -> float:
    """t 分布双侧 p 值。

    直接复用 [`validator._t_sf`](ai-quant-agent/backend/app/backtest/validator.py:74)，
    避免两套近似实现给出不一致的 p（同一项目里同一统计量必须只有一套口径）。
    """
    from app.backtest.validator import _t_sf as _impl
    return _impl(t, df)


def welch_t(a, b) -> dict:
    """Welch t 检验（不假设等方差、**非配对**）。样本不足返回 p=None。

    Args:
        a, b: 两组样本（list / ndarray 均可）。**非配对**是刻意的：
            收紧阈值后新组是旧组的子集，配对比较（同一批样本）差值为 0 毫无信息；
            真正要问的是"**被放弃的那批**与**保留的那批**收益是否不同"。
    """
    a = np.asarray(list(a) if not isinstance(a, np.ndarray) else a, dtype=float)
    b = np.asarray(list(b) if not isinstance(b, np.ndarray) else b, dtype=float)
    a = a[~np.isnan(a)]
    b = b[~np.isnan(b)]
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return {"t": None, "df": 0, "p": None, "mean_a": None, "mean_b": None,
                "n_a": na, "n_b": nb}
    va, vb = float(a.var(ddof=1)), float(b.var(ddof=1))
    se = sqrt(va / na + vb / nb)
    # ⚠️ 退化保护：两组各自完全同值（方差≈0）时 se 会是一个极小的浮点残差，
    # 于是 t 变成 1e16 量级、p 被算成 0 → 会被误判为"显著更差"并**真的应用参数**。
    # 这种情况只能判"无法检验"，不能判"显著"（保守方向必须是"不动参数"）。
    if not (se > 1e-12):
        return {"t": 0.0, "df": na + nb - 2, "p": 1.0,
                "mean_a": float(a.mean()), "mean_b": float(b.mean()),
                "n_a": na, "n_b": nb, "degenerate": True}
    t = (float(a.mean()) - float(b.mean())) / se
    num = (va / na + vb / nb) ** 2
    den = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    df = int(num / den) if den > 0 else na + nb - 2
    return {"t": round(t, 4), "df": max(1, df), "p": round(_t_sf(t, max(1, df)), 6),
            "mean_a": round(float(a.mean()), 5), "mean_b": round(float(b.mean()), 5),
            "n_a": na, "n_b": nb}


# ── 数据 ─────────────────────────────────────────────────────
def load_samples(path: str = SAMPLES_FILE, only_triggered: str = "") -> list[dict]:
    """加载"按 D+1 开盘买入"口径的样本（baseline 已成交且 T+5 非空）。

    Args:
        only_triggered: 只保留某一类买点触发过的样本（""=全部）。
            **必须支持分组**：同一个阈值在不同入场类型上效果可能相反（实测低吸就是反的），
            混在一起看会互相抵消，得出"没效果"的错误结论。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[param_sweep] 样本库读取失败 {path}: {exc}")
        return []
    out: list[dict] = []
    for r in payload.get("records") or []:
        t5 = ((r.get("rets") or {}).get("baseline") or {}).get("t5")
        gap = r.get("gap")
        if t5 is None or gap is None:
            continue
        if only_triggered and not (r.get("triggered") or {}).get(only_triggered):
            continue
        try:
            out.append({"ts_code": r.get("ts_code", ""), "date": r.get("date", ""),
                        "gap": float(gap), "t5": float(t5)})
        except (TypeError, ValueError):
            continue
    return out


# ── 扫描 ─────────────────────────────────────────────────────
def sweep(samples: list[dict], values=CHASE_VALUES) -> dict:
    """对每个追高上限算：成交率 / 保留样本 / T+5 均值 / 胜率。

    成交率的分母 = **全部样本**（含被追高上限挡掉的）→ 这样"收紧的机会成本"才可见。
    """
    gaps = np.array([s["gap"] for s in samples], dtype=float)
    t5 = np.array([s["t5"] for s in samples], dtype=float)
    n_all = len(samples)
    rows = []
    for v in values:
        keep = gaps <= float(v)
        sub = t5[keep]
        rows.append({
            "chase": float(v),
            "n_kept": int(keep.sum()),
            "keep_rate": round(float(keep.mean()), 4) if n_all else 0.0,
            "mean_t5": round(float(sub.mean()), 5) if sub.size else None,
            "win_rate": round(float((sub > 0).mean()), 4) if sub.size else None,
        })
    base = rows[-1] if rows else {}
    for r in rows:
        r["delta_vs_widest"] = (round(float(r["mean_t5"]) - float(base["mean_t5"]), 5)
                                if r.get("mean_t5") is not None and base.get("mean_t5") is not None
                                else None)
    return {"n_total": n_all, "rows": rows}


def dropped_vs_kept(samples: list[dict], new: float, old: float) -> dict:
    """收紧到 new 时：**被放弃**的机会（gap ∈ (new, old]）vs **保留**的机会（gap ≤ new）。

    这是本提案的核心检验：只有"被放弃的那批显著更差"，收紧才是有依据的。
    """
    gaps = np.array([s["gap"] for s in samples], dtype=float)
    t5 = np.array([s["t5"] for s in samples], dtype=float)
    dropped = t5[(gaps > new) & (gaps <= old)]
    kept = t5[gaps <= new]
    res = welch_t(dropped, kept)
    res.update({
        "new": float(new), "old": float(old),
        "dropped_n": int(dropped.size), "kept_n": int(kept.size),
        "dropped_rate": round(float(dropped.size / len(samples)), 4) if samples else 0.0,
        "dropped_mean": round(float(dropped.mean()), 5) if dropped.size else None,
        "kept_mean": round(float(kept.mean()), 5) if kept.size else None,
        "ok": bool(dropped.size >= MIN_GROUP_N and kept.size >= MIN_GROUP_N),
    })
    if res["ok"] and res["p"] is not None:
        # 收紧成立的条件：被放弃的机会明显更差（均值为负差 + 显著）
        res["conclusion"] = ("收紧有依据" if (res["p"] < ALPHA
                                              and (res["dropped_mean"] or 0) < (res["kept_mean"] or 0))
                             else "收紧无统计依据")
    else:
        res["conclusion"] = f"样本不足（被放弃 {res['dropped_n']} / 保留 {res['kept_n']}）"
    return res


def analyze_by_entry_type(new: float = PROPOSED_NEW, old: float = CURRENT_DEFAULT,
                          samples: dict[str, list[dict]] | None = None) -> dict:
    """按入场类型分别做"被放弃 vs 保留"检验 —— 判断同一阈值是否**方向一致**。

    这是本模块最有价值的一步：全局口径说"没效果"时，分组后可能发现
    "某一类明显有、另一类反向"，那就说明该参数的**注入位置**错了（全局参数 vs 类型条件规则）。
    """
    if samples is None:
        samples = {t: load_samples(only_triggered=t) for t in ENTRY_TYPES}
    rows = []
    for t in ENTRY_TYPES:
        sub = samples.get(t) or []
        if len(sub) < MIN_GROUP_N:
            rows.append({"entry": t, "n": len(sub), "conclusion": "样本不足", "ok": False})
            continue
        res = dropped_vs_kept(sub, new, old)
        rows.append({"entry": t, "n": len(sub), **{k: res[k] for k in (
            "dropped_n", "kept_n", "dropped_mean", "kept_mean", "t", "p",
            "conclusion", "ok")}})
    signs = {(-1 if (r.get("dropped_mean") or 0) < (r.get("kept_mean") or 0) else 1)
             for r in rows if r.get("ok")}
    return {"new": float(new), "old": float(old), "rows": rows,
            "direction_consistent": len(signs) <= 1,
            "significant_entries": [r["entry"] for r in rows
                                    if r.get("ok") and r.get("conclusion") == "收紧有依据"]}


def build_evidence(new: float = PROPOSED_NEW, old: float = CURRENT_DEFAULT,
                   samples: list[dict] | None = None) -> dict:
    """构造提案证据：扫描表 + 核心检验 + **分组检验** + 可读摘要。"""
    use = samples if samples is not None else load_samples()
    if not use:
        return {"ok": False, "reason": "无可用样本（先跑 build_buy_type_samples.py）"}
    sw = sweep(use)
    test = dropped_vs_kept(use, new, old)
    by_type = analyze_by_entry_type(new, old)
    row_new = next((r for r in sw["rows"] if abs(r["chase"] - new) < 1e-9), {})
    row_old = next((r for r in sw["rows"] if abs(r["chase"] - old) < 1e-9), {})
    part = " | ".join(
        f"{r['entry']}: 放弃 {r.get('dropped_mean')} vs 保留 {r.get('kept_mean')} "
        f"p={r.get('p')} {r.get('conclusion')}"
        for r in by_type["rows"] if r.get("ok")) or "分组样本不足"
    text = (
        f"样本 {sw['n_total']} 个（按 D+1 开盘买入口径，封板不可买已剔除）。"
        f"收紧到 {new*100:.1f}%：成交率 {row_old.get('keep_rate')}→{row_new.get('keep_rate')}，"
        f"已成交 T+5 均值 {row_old.get('mean_t5')}→{row_new.get('mean_t5')}。"
        f"**全局检验**：被放弃的机会（高开 {new*100:.1f}~{old*100:.1f}%，n={test['dropped_n']}）"
        f"T+5 {test['dropped_mean']} vs 保留组（n={test['kept_n']}）{test['kept_mean']}，"
        f"Welch t={test['t']} p={test['p']} → {test['conclusion']}。"
        f"**按入场类型**：{part}。"
        f"（注：plans/23 §14.8 的 −2.07% 出自突破子样本，本次已在同口径下复核）"
    )
    return {"ok": True, "new": float(new), "old": float(old), "sweep": sw, "test": test,
            "by_entry_type": by_type, "n_total": sw["n_total"],
            "row_new": row_new, "row_old": row_old, "text": text}


# ── 提案（绝不改参数）────────────────────────────────────────
def propose(new: float = PROPOSED_NEW, old: float | None = None,
            write: bool = False, samples: list[dict] | None = None) -> dict:
    """生成（并可选写入）进化提案，status=pending —— **不生效**，等人工确认/影子实验。

    Args:
        write: False（默认）只做校验并返回将要写入的记录；True 才写入候选池。

    护栏：本函数**没有任何** `set_param` 调用；写库前断言当前参数值未被改动。
    """
    from app.agents import evolution_agent, evolution_config

    cur = evolution_config.get_param(TARGET_PARAM)
    old_v = float(old if old is not None else (cur if cur is not None else CURRENT_DEFAULT))
    ev = build_evidence(new, old_v, samples)
    if not ev.get("ok"):
        return {"ok": False, "stage": "evidence", "reason": ev.get("reason")}

    # ⚠️ 证据护栏：证据本身说"无依据"时**拒绝生成提案**（不能自己打自己）
    if ev["test"].get("conclusion") != "收紧有依据":
        return {"ok": False, "stage": "evidence_guard",
                "reason": (f"全局检验未通过（{ev['test'].get('conclusion')}，"
                           f"p={ev['test'].get('p')}）→ 不生成提案。"
                           f"分组结论：{ev['by_entry_type']['significant_entries'] or '无'}"),
                "evidence": ev}

    prop = {"param": TARGET_PARAM, "new": float(new), "old": old_v,
            "reason": (f"D+1 高开 >{new*100:.1f}% 的追高样本 T+5 明显更差，"
                       f"拟收紧追高上限以减少高开接盘（由影子实验裁决）"),
            "evidence": ev["text"]}
    try:
        summary = evolution_agent.evolution_summarizer.build_l0_summary()
    except Exception as exc:  # noqa: BLE001
        summary = {}
        logger.debug(f"[param_sweep] 摘要构建失败: {exc}")
    check = evolution_agent._constitution_check(prop, summary)
    rec: dict = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "param": TARGET_PARAM, "new": float(new), "old": old_v,
        "reason": prop["reason"], "evidence": prop["evidence"],
        "card": {
            "param": TARGET_PARAM, "old": old_v, "new": float(new), "direction": "下调",
            "evidence_trace": ev["text"][:120],
            "minimal_change": f"仅调整 {TARGET_PARAM}（一参数一改，G3）",
        },
        "sweep": ev["sweep"], "test": ev["test"],
        "source": "plans/23 §14.8 P1-7b（纪律参数扫描）",
        "status": "pending",
    }
    if not check.get("ok"):
        # 宪法校验不过 → **如实返回原因，不绕过**（例如 monitor 样本不足 30）
        return {"ok": False, "stage": "constitution", "reason": check.get("reason"),
                "record": rec, "evidence": ev}
    if not write:
        return {"ok": True, "stage": "dry_run", "record": rec, "evidence": ev}

    records = evolution_agent._read_decisions()
    rec["id"] = max((int(r.get("id", 0)) for r in records), default=0) + 1
    records.append(rec)
    evolution_agent._save_decisions(records)
    # 护栏：写提案后必须仍未改动参数（写提案 ≠ 生效）
    after = evolution_config.get_param(TARGET_PARAM)
    if after != cur:
        logger.error(f"[param_sweep] ⚠️ 写提案过程意外改动了 {TARGET_PARAM}: {cur} -> {after}")
        return {"ok": False, "stage": "guard", "reason": "参数被意外改动，请立即回滚",
                "record": rec}
    logger.info(f"[param_sweep] 已提交提案 id={rec['id']} {TARGET_PARAM} {old_v} -> {new}（pending，未生效）")
    return {"ok": True, "stage": "written", "record": rec, "evidence": ev}


__all__ = ["load_samples", "sweep", "dropped_vs_kept", "analyze_by_entry_type",
           "build_evidence", "propose", "welch_t", "CHASE_VALUES", "ENTRY_TYPES",
           "TARGET_PARAM", "PROPOSED_NEW", "CURRENT_DEFAULT"]
