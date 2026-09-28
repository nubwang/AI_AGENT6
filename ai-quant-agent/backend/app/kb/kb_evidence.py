"""模块证据接入层（kb_evidence）— 把 plans/23 / 24 / 25 的**验证产出**接成进化大脑的神经

## 为什么需要这一层

plans/21 建好了私有域知识库（EKB）与适配层（kb_ingest），但它只接了三类来源：
次日验证案例（daily_verify）、回测结果（backtest）、进化改动台账（code_writer/guard）。

plans/23（回测增强：洗盘 / 四类买点 / 目标价 / 网格）、plans/24（人心博弈：博弈特征 /
情绪门控）、plans/25（自证测试：历史回放 + OOS 超额）产出了**大量经过统计检验的结论**，
却散落在 `data/*.json` 里 —— 进化大脑看不见它们，等于"神经元没有连上"：
每次提案仍只能凭案例教训猜，重复劳动、还可能采纳已被否证的因子。

本层做**只读挂引用**（与 kb_ingest.import_external_kbs 同一处置原则：不搬迁、不改原始文件）：

    模块产物 JSON ──提取结论──> lesson（type=`evidence:<module>`，带样本量与可操作 hint）

于是：
  - 检索层（kb_index.search_lessons）能召回"洗盘是否有效 / 哪个买点能赚 / 哪个博弈特征
    通过了 FDR / 自证 OOS 超额是多少"；
  - L0 摘要与综合分析上下文（kb_context）会带上这些实证结论 → 进化大脑每次分析都"看得见"；
  - 教训置信度沿用 kb_distiller.confidence_for(n)（小样本只作观察，宪法 C3）。

## 诚实约束（对齐 plans/21 §8.3 硬规则）

  - **只搬运结论与样本量**，不搬运原始数据（30MB 的 samples 文件不会进库）；
  - 没有样本量的一律按 n=1 计（→ candidate，只作观察），绝不冒充实证；
  - 提取失败静默跳过，绝不阻塞推荐/回测主流程；
  - 幂等：id 为确定性指纹，重复接入不产生重复事实。
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter

from app.core.logger import logger
from app.kb import kb_store, kb_distiller

DATA_DIR = kb_store.DATA_DIR

# 结论文本长度上限（控 token；库内 lesson.text 在 L0/检索里会被再截断）
_TXT_MAX = 200
# 数值摘要里最多列几个键（防个别模块字段过多把文本撑爆）
_KV_MAX = 6


def _read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except Exception:  # noqa: BLE001
        return None


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:  # noqa: BLE001
        return default


def _pct(v, digits: int = 2) -> str:
    n = _num(v)
    return f"{n * 100:+.{digits}f}%" if n is not None else "-"


def _slug(key: str) -> str:
    """把结论键转成安全 id 片段：**保留数字**（t1/t5/t10、h1/h5 必须可区分）。

    注意：kb_distiller.lesson_id 的归一化会删掉所有数字，用于"同类教训跨期合并"是对的，
    但会把 washout 的 ab_t1/ab_t5/ab_t10 合并成同一条（丢结论）——本层因此自建确定性 id。
    """
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(key or "").strip().lower())
    return (s.strip("_") or "x")[:64]


def _kv_brief(d, keys: list[str] | None = None, max_items: int = _KV_MAX) -> str:
    """把一个 dict 压成 "k=v" 短摘要（默认只取数值/短字符串键，按给定时序）。"""
    if not isinstance(d, dict):
        return ""
    items: list[str] = []
    pool = keys if keys else list(d.keys())
    for k in pool:
        if k not in d or len(items) >= max_items:
            continue
        v = d[k]
        if isinstance(v, bool):
            items.append(f"{k}={'是' if v else '否'}")
        elif isinstance(v, (int, float)):
            n = _num(v)
            if n is not None:
                items.append(f"{k}={round(n, 4)}")
        elif isinstance(v, str) and 0 < len(v) <= 40:
            items.append(f"{k}={v}")
    return ", ".join(items)


# ── 各模块提取器（返回 list[dict]：每条 = 一条可入库结论）──────
def _extract_washout(d) -> list[dict]:
    """plans/23 §3.2 洗盘样本 A/B（同交易日配对 + 置换检验）。"""
    out: list[dict] = []
    ab = (d or {}).get("ab") or {}
    n = _int((d or {}).get("wash_n") or (d or {}).get("points_n"))
    for h in ("t1", "t5", "t10"):
        v = ab.get(h)
        if not isinstance(v, dict):
            continue
        brief = _kv_brief(v)
        if not brief:
            continue
        out.append({
            "key": f"ab_{h}",
            "text": f"洗盘确认信号 A/B（同交易日配对，plans/23）{h.upper()}：{brief}",
            "hint": ("洗盘确认（地量+收阳+站上MA5）若在 T+5 显著为正且 p<0.05，"
                     "可作为入选加分项；未显著则不得据此加权"),
            "n": n,
        })
    if not out:
        out.append({
            "key": "sample",
            "text": (f"洗盘样本库已建立但 A/B 结论缺失（wash_n={_int((d or {}).get('wash_n'))}，"
                     f"points_n={_int((d or {}).get('points_n'))}）—— 先跑 build_washout_samples"),
            "hint": "无 A/B 结论时，禁止把洗盘信号用于打分或过滤",
            "n": 1,
        })
    return out


_BUY_TYPE_CN = {"breakout": "①突破", "pullback": "②回踩", "dip": "③低吸",
                "auction": "④竞价", "baseline": "基准(无条件D+1开盘)"}
_BUY_KEYS = ("n", "triggers", "triggered", "filled", "fill_rate", "unfillable",
             "sealed", "win_rate", "hit_rate", "avg_t5", "mean_t5", "excess_t5",
             "excess", "p", "p_value", "avg_ret", "expectancy")


def _extract_buy_types(d) -> list[dict]:
    """plans/23 §3.3 四类买点分别回测（含成交率与高开分桶）。"""
    out: list[dict] = []
    summary = (d or {}).get("summary") or {}
    total = _int((d or {}).get("points") or summary.get("total_points"))
    for bt, v in summary.items():
        if bt == "total_points" or not isinstance(v, dict):
            continue
        brief = _kv_brief(v, list(_BUY_KEYS))
        if not brief:
            continue
        cn = _BUY_TYPE_CN.get(bt, bt)
        out.append({
            "key": f"type_{bt}",
            "text": f"买点回测 {cn}（D+1 成交口径，plans/23）：{brief}",
            "hint": (f"挂单/成交设计参考 {cn} 的成交率与从成交价起算的 T+5；"
                     f"成交率低的买点（买不到）不得按理想价假设收益"),
            "n": _int(v.get("n") or v.get("triggered") or v.get("triggers") or total),
        })
    # 高开分桶：回答"高开多少还能追"（只在有数据时给一条）
    gaps = (d or {}).get("gap_buckets") or {}
    for bt, g in list(gaps.items())[:2]:
        if not isinstance(g, dict):
            continue
        best = max(((_num(b.get("excess_t5") or b.get("avg_t5") or b.get("win_rate")) or -9,
                    b.get("bucket") or b.get("label") or k)
                    for k, b in g.items() if isinstance(b, dict)), default=None)
        if best and best[0] > -9:
            out.append({
                "key": f"gap_{bt}",
                "text": (f"高开分桶最优（{_BUY_TYPE_CN.get(bt, bt)}）："
                         f"{best[1]} → {round(best[0], 4)}（plans/23 追高上限依据）"),
                "hint": "追高上限（plan_max_chase_pct）应设置在最优分桶上界附近，而非凭感觉",
                "n": total,
            })
    return out


def _extract_game_features(d) -> list[dict]:
    """plans/24 人心博弈特征（Fama-MacBeth 两步法 + BH-FDR，判据：|spread|≥min_spread 且 q<0.05）。"""
    d = d or {}
    out: list[dict] = []
    n_days = _int(d.get("n_days"))
    min_spread = _num(d.get("min_spread"))
    usable = d.get("usable") or []
    results = d.get("results") or []
    for r in usable[:16]:
        if not isinstance(r, dict):
            continue
        feat = str(r.get("feature") or "")
        out.append({
            "key": f"usable_{feat}",
            "text": (f"博弈特征可用「{feat}」(计谋 {r.get('plan') or '-'})："
                     f"日度高-低组差 {_pct(r.get('spread'))}，t={r.get('t')}，p={r.get('p')}，"
                     f"有效天数 {_int(r.get('n_days'))}（plans/24，已过 FDR 与经济意义门槛）"),
            "hint": f"可将「{feat}」纳入入选/打分（建议先做增量回测再开闸）",
            "n": _int(r.get("n_days")),
        })
    if not usable:
        top = [str(r.get("feature")) for r in results[:4]
               if isinstance(r, dict) and r.get("feature")]
        out.append({
            "key": "no_usable",
            "text": (f"博弈特征当前**无一项**通过判据（FDR q<0.05 且 |spread|≥{min_spread}）："
                     f"共检验 {len(results)} 项，有效天数 {n_days}"
                     + (f"，未通过项如 {'、'.join(top)}" if top else "")),
            "hint": ("禁止把未通过检验的博弈特征加入打分/过滤（避免过拟合式'进化'）；"
                     "若要再试，需改变口径（换 horizon / 换分组数）并重新做 FDR"),
            "n": 1,
        })
    return out


def _extract_sentiment_gate(d) -> list[dict]:
    """plans/23/24 情绪门控（分组边际 + BH-FDR）。"""
    d = d or {}
    out: list[dict] = []
    fdr = d.get("fdr") or {}
    n_reject = _int(fdr.get("n_reject_q"))
    tests = d.get("results") or []
    for r in (d.get("usable_tests") or [])[:8]:
        if not isinstance(r, dict):
            continue
        feat = str(r.get("feature") or "")
        out.append({
            "key": f"usable_{feat}",
            "text": (f"情绪门控可用「{feat}」(h={r.get('horizon')})："
                     f"高低组差 {_pct(r.get('edge') or r.get('spread_hi_minus_lo'))}，"
                     f"p={r.get('p')}，q={r.get('q')}，n={_int(r.get('n'))}"),
            "hint": "可用于择时/仓位门控（先做增量回测：与既有 regime 门控是否重复）",
            "n": _int(r.get("n")),
        })
    if not out and tests:
        out.append({
            "key": "no_usable",
            "text": (f"情绪门控 {len(tests)} 项检验中 FDR 后通过 {n_reject} 项"
                     f"（整体市场 fwd：{_kv_brief(d.get('overall_market_fwd'))}）"),
            "hint": ("无通过项时不得开闸情绪门控；情绪指标只作解释不作决策"),
            "n": 1,
        })
    return out


def _extract_discipline_grid(d) -> list[dict]:
    """plans/22/23 纪律网格（反事实网格择优，含 IS/OOS 与成本）。"""
    d = d or {}
    b = d.get("baseline") or {}
    if not b:
        return []
    stop = _num(b.get("stop_loss_pct")) or 0.0
    tp1 = _num(b.get("tp1_ratio")) or 0.0
    chase = _num(b.get("chase_pct")) or 0.0
    return [{
        "key": "best_params",
        "text": (f"纪律网格择优（反事实，plans/22）：止损 {stop * 100:.0f}%、目标折扣 {tp1 * 100:.0f}%、"
                 f"持有 {b.get('hold_days')} 日、追高上限 {chase * 100:.0f}% → "
                 f"期望 {_pct(b.get('utility'))}，纪律alpha {_pct(b.get('alpha_utility'))}，"
                 f"样本外 {_pct(b.get('oos_utility'))}（成本 {_num(d.get('cost_pct'))}%，"
                 f"样本 {_int(d.get('samples'))}，IS 截止 {d.get('is_cut')}）"),
        "hint": ("plan_stop_loss_pct / plan_tp1_ratio / plan_hold_days / plan_max_chase_pct "
                 "可按此设定；**样本外为负则说明瓶颈在选股端，纪律只能减亏**"),
        "n": _int(d.get("samples")),
        "param_hint": "plan_stop_loss_pct / plan_tp1_ratio / plan_hold_days / plan_max_chase_pct",
    }]


def _selfproof_reports(max_runs: int = 2) -> list[tuple[str, dict]]:
    """取 selfproof 沙箱里最新的若干份 report.json（按 mtime 倒序）。"""
    base = os.path.join(DATA_DIR, "selfproof")
    found: list[tuple[float, str, dict]] = []
    try:
        for rid in os.listdir(base):
            p = os.path.join(base, rid, "report.json")
            if not os.path.exists(p):
                continue
            d = _read_json(p, None)
            if isinstance(d, dict) and (d.get("metrics") or d.get("dates")):
                found.append((os.path.getmtime(p), rid, d))
    except Exception:  # noqa: BLE001
        return []
    found.sort(reverse=True, key=lambda x: x[0])
    return [(rid, d) for _, rid, d in found[:max_runs]]


def _extract_selfproof(d, run_id: str = "") -> list[dict]:
    """plans/25 自证测试（历史回放 + OOS 分层）：**主指标 = 超额**。

    key 带 run 标识 —— 不同 run 的结论并列保留（可对比"改前/改后自证"），同 run 幂等。
    """
    d = d or {}
    m = d.get("metrics") or {}
    run_id = str(d.get("run_id") or run_id or "")
    rid = run_id[-16:] or "run"
    dates = d.get("dates") or {}
    n_settled = _int((d.get("daily_stats") or {}).get("n_settled_t5"))
    out: list[dict] = []
    oos = m.get("is_oos") or d.get("is_oos") or {}
    oos_block = oos.get("out_of_sample") or {}
    brief = _kv_brief(m, ["avg_excess_t5", "win_rate_excess", "avg_t5", "win_rate",
                          "avg_t5_net", "n_settled"], max_items=5)
    text = (f"自证回放（plans/25）{run_id}：{dates.get('first')}~{dates.get('last')} "
            f"（{_int(dates.get('n_days'))} 日 / {n_settled} 单）"
            + (f"；样本外(超额) {_pct(oos_block.get('avg_excess_t5'))}（n={_int(oos_block.get('n'))}）"
               if oos_block else "")
            + (f"；{brief}" if brief else ""))
    out.append({
        "key": f"{rid}:oos_excess",
        "text": text,
        "hint": ("验收只看样本外超额（正=真本事，负=选股端未解决）；"
                 "与实盘解耦，禁止用实盘反馈当自证验收"),
        "n": n_settled,
    })
    # 分层：哪个环境/形态在自证里真的赚钱
    layers = (d.get("layers") or {})
    by_regime = layers.get("by_regime") or {}
    if isinstance(by_regime, dict) and by_regime:
        rows = []
        for k, v in by_regime.items():
            if isinstance(v, dict) and v.get("avg_excess_t5") is not None:
                rows.append((str(k), _num(v.get("avg_excess_t5")), _int(v.get("n"))))
        rows.sort(key=lambda x: -(x[1] if x[1] is not None else -9))
        if rows:
            out.append({
                "key": f"{rid}:layer_regime",
                "text": ("自证分层（环境，超额排序）："
                         + " | ".join(f"{k} {_pct(e)}（n={n}）" for k, e, n in rows[:4])),
                "hint": "为负的环境应降权/回避（环境门控依据）；分层样本 n<10 只作观察",
                "n": sum(n for _, _, n in rows),
            })
    # 归因：自证里最典型的失效类型
    att = d.get("attribution") or {}
    ft = att.get("failure_types") or att.get("by_type") or {}
    if isinstance(ft, dict) and ft:
        if all(isinstance(v, (int, float)) for _, v in ft.items()):
            top = sorted(ft.items(), key=lambda kv: -(float(kv[1]) or 0.0))[:3]
        else:
            top = list(ft.items())[:3]
        out.append({
            "key": f"{rid}:failure_types",
            "text": ("自证失效归因 TOP：" + "、".join(f"{k}({v})" for k, v in top)),
            "hint": "高频失效类型对应修选股端（信号/形态/过滤），而不是只调纪律参数",
            "n": n_settled,
        })
    return out


def _extract_selfproof_gate(d) -> list[dict]:
    """自证数据门与幸存者偏差声明（plans/25 P0.5）：样本边界必须写清。"""
    d = d or {}
    g = d.get("data_gate") or {}
    sv = d.get("survivorship") or {}
    return [{
        "key": "gate",
        "text": (f"自证数据门（as_of {d.get('as_of')}）：{_kv_brief(g, max_items=6)}；"
                 f"幸存者偏差 {_kv_brief(sv, max_items=4)}"),
        "hint": ("自证结论必须带数据门边界；退市股缺口会系统性高估收益，"
                 "缺口未补前不得宣称能力提升"),
        "n": _int(g.get("n_rows") or g.get("rows") or g.get("n_days") or 1),
    }]


def _extract_entry_guidance(d) -> list[dict]:
    """最佳入场（无后视偏差策略）。"""
    d = d or {}
    out: list[dict] = []
    for form, v in (d.get("by_form") or {}).items():
        brief = _kv_brief(v) if isinstance(v, dict) else (str(v) if isinstance(v, str) else "")
        if not brief:
            continue
        out.append({
            "key": f"form_{form}",
            "text": f"最佳入场（形态 {form}）：{brief}",
            "hint": "下单方式（D+1 开盘 / 回踩限价）按此选择，避免用「理想价」高估成交",
            "n": _int((v or {}).get("samples") or (v or {}).get("n")) if isinstance(v, dict) else 1,
        })
    return out


def _extract_condition_table(d) -> list[dict]:
    """条件概率表（特征阈值 → 上涨概率，与 baseline 对比）。"""
    d = d or {}
    out: list[dict] = []
    base = _num((d or {}).get("baseline_prob"))
    n_all = _int((d or {}).get("sample_count"))
    for r in (d.get("rules") or [])[:8]:
        if not isinstance(r, dict):
            continue
        feat = str(r.get("feature") or "")
        out.append({
            "key": f"rule_{feat}_{r.get('direction')}",
            "text": (f"条件概率「{feat} {r.get('direction')} {r.get('threshold')}」"
                     f"→ 上涨概率 {r.get('up_prob')}（基线 {base}，提升 {r.get('gain')}，"
                     f"n={_int(r.get('sample_count'))}）"),
            "hint": f"可作为打分/过滤参考（{feat}）；样本 n<30 只作观察",
            "n": _int(r.get("sample_count")),
        })
    for r in (d.get("combos") or [])[:4]:
        if not isinstance(r, dict):
            continue
        feats = "+".join(str(x) for x in (r.get("features") or []))
        out.append({
            "key": f"combo_{feats}",
            "text": (f"组合条件 [{feats}] → 上涨概率 {r.get('up_prob')}"
                     f"（基线 {r.get('baseline_prob') or base}，lift {r.get('lift')}，"
                     f"n={_int(r.get('sample_count'))}）"),
            "hint": "条件打分可优先考虑该组合；组合样本通常偏少，需 OOS 复核",
            "n": _int(r.get("sample_count")),
        })
    return out[:10]


def _extract_form_leaderboard(d) -> list[dict]:
    """形态规则榜单：**有效规则**与其样本（被删规则的删除原因对"防复活"极有价值）。"""
    d = d or {}
    out: list[dict] = []
    for r in (d.get("rules") or [])[:6]:
        if not isinstance(r, dict):
            continue
        key = str(r.get("key") or r.get("name") or "")
        out.append({
            "key": f"rule_{key}",
            "text": (f"形态规则有效「{r.get('name') or key}」({r.get('form_type') or '-'})："
                     f"T+1 命中率 {r.get('hit_rate_t1')}（样本 {_int(r.get('samples'))}，"
                     f"基线 {d.get('baseline_t1')}）"),
            "hint": "该规则可继续用于召回/加分；低于基线时应从规则库剔除",
            "n": _int(r.get("samples")),
        })
    deleted = d.get("deleted") or []
    if deleted:
        top = [(str(r.get("name") or r.get("key")), r.get("hit_rate_t1"), r.get("reason"))
               for r in deleted[:4] if isinstance(r, dict)]
        out.append({
            "key": "deleted_rules",
            "text": ("被剔除的形态规则（勿复活）："
                     + "；".join(f"{n}(命中率 {h}，{reason})" for n, h, reason in top)),
            "hint": "这些规则已被证明不达基线 —— 进化提案不得再启用同名规则",
            "n": max(1, len(deleted)),
        })
    return out


def _extract_cost_basis(d) -> list[dict]:
    """成本/换手口径（plans/23 §14：成本吃掉多少收益 —— 直接决定"能不能赚"）。"""
    d = d or {}
    out: list[dict] = []
    res = (d or {}).get("res") or {}
    ours = (d or {}).get("ours") or []
    if res:
        out.append({
            "key": "slippage_basis",
            "text": (f"成本口径基准：费率 {res.get('fee')}，滑点 {res.get('slips_bp')} bp，"
                     f"horizons {res.get('horizons')}，不可成交 {res.get('unfillable')}"),
            "hint": ("回测/提案一律用净收益口径（含双边成本）；毛收益好看但净收益为负的改动无效"),
            "n": max([_int(o.get("n_days")) for o in ours if isinstance(o, dict)] or [1]),
        })
        for o in ours[:2]:
            if not isinstance(o, dict):
                continue
            out.append({
                "key": f"h{o.get('h')}",
                "text": (f"成本对比（持有 {o.get('h')} 日）：毛 {_pct(o.get('gross_all'))} / "
                         f"净 {_pct(o.get('net_all') or o.get('net_fillable'))}"
                         f"（成交率相关 {o.get('fill_rate')}，滑点均值 {o.get('slip_bp_mean')} bp，"
                         f"n={_int(o.get('n'))}）"),
                "hint": "换手越高，成本侵蚀越大；持有期与调仓频率应按净收益择优选",
                "n": _int(o.get("n")),
            })
    return out


def _extract_regime_stats(d) -> list[dict]:
    """环境（regime）历史分布：为"必须分层"提供基础事实。"""
    if not isinstance(d, dict) or not d:
        return []
    cnt: dict[str, int] = {}
    for v in d.values():
        if isinstance(v, dict) and v.get("regime"):
            cnt[str(v["regime"])] = cnt.get(str(v["regime"]), 0) + 1
    if not cnt:
        return []
    total = sum(cnt.values())
    parts = "、".join(f"{k} {v / total * 100:.0f}%" for k, v in
                      sorted(cnt.items(), key=lambda kv: -kv[1]))
    return [{
        "key": "distribution",
        "text": f"市场环境分布（regime_split，共 {total} 个交易日）：{parts}",
        "hint": "结论必须按环境分层看待：震荡市占比通常最高，样本要按 regime 加权",
        "n": total,
    }]


# ── 第二批：对照 / 口径 / 数据可信度类产物（"选股到底有没有用"的证据）────
def _extract_pool_filter_oos(d) -> list[dict]:
    """选股池剔除方案的样本内外一致性（plans/23 §14：池过滤不是想剔就剔）。"""
    d = d or {}
    out: list[dict] = []
    for scope in ("池内", "全市场"):
        blk = d.get(scope)
        if not isinstance(blk, dict):
            continue
        full = blk.get("full_sample") or {}
        schemes = full.get("schemes") if isinstance(full, dict) else []
        best = None
        for s in (schemes or []):
            if not isinstance(s, dict):
                continue
            m = _num(s.get("mean"))
            if m is None:
                continue
            if best is None or m > best[1]:
                best = (str(s.get("scheme")), m, _int(s.get("n_kept")), _num(s.get("t_hac")))
        if best is None:
            continue
        out.append({
            "key": f"{scope}",
            "text": (f"池过滤 OOS（{scope}）：基准净 {_pct(full.get('base_net'))}；"
                     f"最优方案「{best[0]}」净 {_pct(best[1])}（保留 {best[2]} 条，"
                     f"HAC t={best[3]}；样本 {_int(blk.get('n_days'))} 天 / "
                     f"{_int(blk.get('n_records'))} 条）"),
            "hint": ("池过滤只有在样本外同样成立（HAC t≥2）时才可启用；"
                     "只在半段成立的方案视为噪声"),
            "n": _int(blk.get("n_days")),
        })
    return out


def _extract_walk_forward(d) -> list[dict]:
    """滚动前向验证（plans/23 §14.9）：训练段挑的方案在测试段是否还有效。"""
    d = d or {}
    s = d.get("summary") or {}
    out: list[dict] = [{
        "key": "summary",
        "text": (f"滚动前向验证（k={_int(d.get('k'))} 折）：正收益折 {_int(s.get('n_pos'))}/"
                 f"{_int(s.get('n_folds'))}，HAC 显著 {_int(s.get('n_hac_sig'))} 折，"
                 f"可判定 d 项 {_int(s.get('n_decidable_d'))}，放行 {_int(s.get('n_usable_nod'))}"),
        "hint": ("方案必须「训练段挑出、测试段仍有效」才算成立；"
                 "只有全样本好看的方案不要采纳"),
        "n": _int(s.get("n_folds")),
    }]
    for f in (d.get("folds") or [])[:4]:
        if not isinstance(f, dict):
            continue
        j = f.get("judgment") or {}
        reasons = j.get("reasons") or []
        out.append({
            "key": f"fold{_int(f.get('fold'))}",
            "text": (f"前向第 {_int(f.get('fold'))} 折（测试 {f.get('test')}）："
                     f"选中「{f.get('picked')}」净 {_pct(j.get('net') or f.get('mean'))}，"
                     f"HAC t={f.get('t_hac')}，判定 {j.get('verdict')}"
                     + (f"（{str(reasons[0])[:40]}）" if reasons else "")),
            "hint": "verdict=reject 的方案不得在提案中复用",
            "n": _int(f.get("n")),
        })
    return out


def _extract_agent_refine_effect(d) -> list[dict]:
    """LLM 精筛到底有没有用（plans/23）：保留组 vs 剔除组 vs 新增组的净收益。"""
    d = d or {}
    sub = d.get("subsets") or {}

    def _m(k: str):
        v = sub.get(k) or {}
        return (_num(v.get("mean")), _int(v.get("n")))

    kept, kn = _m("kept")
    drop, dn = _m("dropped")
    add, an = _m("added")
    if kept is None and drop is None:
        return []
    gain = (kept - drop) if (kept is not None and drop is not None) else None
    return [{
        "key": "refine_gain",
        "text": (f"LLM 精筛效果（h={_int(d.get('h'))}，top{_int(d.get('top_n'))}，"
                 f"n={_int(d.get('n_records'))}）：保留 {_pct(kept)}（n={kn}） vs "
                 f"剔除 {_pct(drop)}（n={dn}）"
                 + (f"，差值 {_pct(gain)}" if gain is not None else "")
                 + (f"；新增 {_pct(add)}（n={an}）" if add is not None else "")
                 + f"；市场基准 {_pct(d.get('market_base'))}"),
        "hint": ("保留组明显优于剔除组 → 精筛有效；差值≤0 → 精筛在削弱收益，"
                 "应改 prompt 或退回规则档（不要为了用 LLM 而用）"),
        "n": _int(d.get("n_days")),
    }]


def _extract_lowmom_scheme(d) -> list[dict]:
    """低动量 / 低位补涨方案判定：毛收益好但净收益是否为负。"""
    d = d or {}
    out: list[dict] = []
    for r in (d.get("records") or [])[:4]:
        if not isinstance(r, dict):
            continue
        j = r.get("judge") or {}
        name = str(r.get("name") or "")
        out.append({
            "key": f"{_slug(name)}_h{_int(r.get('h'))}",
            "text": (f"方案「{name}」（持 {_int(r.get('h'))} 日，n={_int(r.get('n'))}）："
                     f"毛 {_pct(r.get('gross'))} / 年化净 {_pct(r.get('net_per_year'))} / "
                     f"单期超额 {_pct(r.get('excess_per_period'))}；HAC t={r.get('t_hac')}，"
                     f"判定 {j.get('verdict')}"),
            "hint": "verdict=reject（净收益或分段不为正）→ 不得作为选股方案启用",
            "n": _int(r.get("n")),
        })
    return out


def _extract_turnover_replay(d) -> list[dict]:
    """换手与成本重放：成本吃掉多少、哪个换手口径还站得住。"""
    d = d or {}
    out: list[dict] = []
    for r in (d.get("rows") or [])[:3]:
        if not isinstance(r, dict):
            continue
        out.append({
            "key": _slug(str(r.get("tag"))),
            "text": (f"换手成本重放「{r.get('tag')}」：年换手 {r.get('turnover_yr')}，"
                     f"年成本 {_pct(r.get('cost_yr'))}，年化净 {_pct(r.get('net_yr'))}，"
                     f"NW t={r.get('nw_t')}，判定 {r.get('verdict')}"
                     f"（分年 {_kv_brief(r.get('by_seg'), max_items=4)}）"),
            "hint": "换手越高成本侵蚀越大；净收益不显著的换手口径不得启用",
            "n": _int(d.get("periods_on") or d.get("periods")),
        })
    return out


def _extract_t_overlap_recheck(d) -> list[dict]:
    """T 重叠样本的显著性重检（plans/23 §14.9）：naive t 有多虚高。"""
    d = d or {}
    s = d.get("summary") or {}
    rows = [r for r in (d.get("rows") or []) if isinstance(r, dict)]
    downgraded = [r for r in rows
                  if _num(r.get("t_naive")) is not None and _num(r.get("t_hac")) is not None
                  and float(r["t_naive"]) >= 2 > float(r["t_hac"])]
    out: list[dict] = [{
        "key": "summary",
        "text": (f"T 重叠重检：可重检 {_int(s.get('recheckable'))} 项，"
                 f"因 HAC 修正失去显著性 {len(downgraded)} 项"
                 f"（保留 {_int(s.get('keep'))} / 放弃 {_int(s.get('lost'))}）"),
        "hint": ("所有『T 日重叠样本』的显著性必须看 HAC t 值；naive t 会系统性虚高，"
                 "只靠 naive t 通过的结论不得采纳"),
        "n": len(rows),
    }]
    for r in downgraded[:3]:
        out.append({
            "key": f"lost_{_slug(str(r.get('src')))}",
            "text": (f"显著性被修正掉：「{r.get('src')}/{r.get('series')}」h={r.get('h')} "
                     f"n={_int(r.get('n'))}，naive t={r.get('t_naive')} → HAC t={r.get('t_hac')}"),
            "hint": "该结论在 HAC 下不成立 → 不得作为提案依据",
            "n": _int(r.get("n")),
        })
    return out


def _extract_ours_baseline(d) -> list[dict]:
    """我们 vs 基准（plans/24 §11.16）：超额是否存在 —— 选股是否真的有用。"""
    out: list[dict] = []
    for r in (d if isinstance(d, list) else [])[:8]:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("kind") or r.get("label") or "?")
        out.append({
            "key": f"{_slug(kind)}_h{_int(r.get('h'))}",
            "text": (f"我们 vs 基准「{kind}」持 {_int(r.get('h'))} 日："
                     f"毛 {_pct(r.get('gross'))} / 净 {_pct(r.get('net'))} / "
                     f"年化净 {_pct(r.get('net_per_year'))}（n={_int(r.get('n'))}，"
                     f"{_int(r.get('n_days'))} 天）"),
            "hint": "净超额才是真本事；与基准无差异时应优先改选股端，而不是继续加过滤层",
            "n": _int(r.get("n")),
        })
    return out


def _extract_size_baseline(d) -> list[dict]:
    """市值分层基线（plans/24）：收益是否只是小市值效应。"""
    out: list[dict] = []
    for r in (d if isinstance(d, list) else [])[:6]:
        if not isinstance(r, dict):
            continue
        out.append({
            "key": f"q{r.get('q')}_h{_int(r.get('h'))}",
            "text": (f"市值分层 q{r.get('q')}（{r.get('kind')}）持 {_int(r.get('h'))} 日："
                     f"毛 {_pct(r.get('gross'))}，naive t={r.get('t')}，HAC t={r.get('t_hac')}"
                     + (f"，分段 {_kv_brief(r.get('by_seg'), max_items=4)}"
                        if r.get("by_seg") else "")),
            "hint": "HAC 不显著 → 该分层收益不可依赖（可能是重叠样本假象）",
            "n": _int(r.get("n")),
        })
    return out


def _extract_data_validation(d) -> list[dict]:
    """数据可信度门：数据不干净时任何结论都不可信。"""
    d = d or {}
    core = d.get("core") or {}
    ic = d.get("issue_count") or {}
    issues = [i for i in (d.get("issues") or []) if isinstance(i, dict)]
    warn = [f"{i.get('table')}:{i.get('message')}" for i in issues
            if str(i.get("level")) == "warning"][:3]
    return [{
        "key": "core_status",
        "text": (f"数据可信度：核心表状态 {core.get('status')}，"
                 f"error {_int(ic.get('error'))} / warning {_int(ic.get('warning'))}"
                 f"（覆盖 {_int(d.get('total_stocks'))} 只，起始 {d.get('start_date')}）"
                 + (f"；告警示例：{'；'.join(warn)}" if warn else "")),
        "hint": "数据门未过（error>0）时，回测/自证结论一律不得作为进化依据（先补数据）",
        "n": _int(d.get("total_stocks")),
    }]


def _extract_early_data_quality(d) -> list[dict]:
    """早期数据可比性（plans/25 数据门）：哪些年份可比、哪些要降权。"""
    d = d or {}
    s = d.get("summary") or {}
    years = [y for y in (d.get("years") or []) if isinstance(y, dict)]
    care = [str(y.get("year")) for y in years
            if str(y.get("label") or "") not in ("可比", "")]
    return [{
        "key": "summary",
        "text": (f"早期数据可比性：可比 {_int(s.get('n_ok'))} 年 / 需留意 "
                 f"{_int(s.get('n_care'))} / 不可用 {_int(s.get('n_bad'))}"
                 + (f"；需留意年份 {'、'.join(care[:6])}" if care else "")),
        "hint": "跨年回测/自证时对『需留意』年份降权或剔除，否则数据缺口会污染长期结论",
        "n": len(years),
    }]


def _extract_form_coverage(d) -> list[dict]:
    """形态识别覆盖率（漏斗）：未分类占比决定形态规则能覆盖多少机会。"""
    d = d or {}
    f = d.get("funnel") or {}
    return [{
        "key": "funnel",
        "text": (f"形态漏斗：扫描 {_int(f.get('scanned_stocks'))} → "
                 f"候选段 {_int(f.get('candidate_segments'))} → "
                 f"已分类 {_int(f.get('classified_segments'))}"
                 f"（未分类占比 {_pct(f.get('unclassified_ratio'))}，"
                 f"分类率 {_pct(f.get('classified_ratio'))}）；"
                 f"形态分布 {_kv_brief(d.get('form_distribution'), max_items=6)}"),
        "hint": ("未分类占比高 → 先补形态/规则提高召回，再谈阈值调优；"
                 "未分类段里的高涨幅标的代表被漏掉的机会"),
        "n": _int(f.get("scanned_stocks")),
    }]


def _extract_improve_effect(d) -> list[dict]:
    """改动实盘追踪：同一参数改动后命中率是否真的提升。"""
    d = d or {}
    out: list[dict] = []
    for r in (d.get("records") or [])[-4:]:
        if not isinstance(r, dict):
            continue
        out.append({
            "key": f"{_slug(str(r.get('param')))}",
            "text": (f"改动实盘追踪「{r.get('param')}」={r.get('value')}："
                     f"基线命中率 {r.get('baseline_hit_rate')}"
                     f"（{_int(r.get('baseline_samples'))} 样本）→ "
                     f"当前 {r.get('current_hit_rate')}"
                     f"（{_int(r.get('current_samples'))} 样本）"),
            "hint": "实盘追踪未提升的改动应回滚，而不是叠加新改动（防参数堆叠式退化）",
            "n": _int(r.get("current_samples") or r.get("baseline_samples")),
        })
    return out


def _extract_evolution_snapshots(d) -> list[dict]:
    """进化历史快照：命中率是否长期上行（"进化是否真的在进化"的硬证据）。"""
    rows = [r for r in (d if isinstance(d, list) else []) if isinstance(r, dict)]
    if len(rows) < 2:
        return []
    first, last = rows[0], rows[-1]
    return [{
        "key": "trend",
        "text": (f"进化快照趋势（{len(rows)} 个快照，{first.get('ts')} → {last.get('ts')}）："
                 f"命中率 {first.get('hit_rate')} → {last.get('hit_rate')}，"
                 f"T+5 均值 {first.get('avg_t5_ret')} → {last.get('avg_t5_ret')}，"
                 f"样本 {_int(first.get('samples'))} → {_int(last.get('samples'))}"),
        "hint": ("命中率长期不升说明进化在做无效改动 → 回到瓶颈（选股端）而不是继续微调参数；"
                 "样本量差异大时不可直接对比"),
        "n": _int(last.get("samples")),
    }]


def _extract_probability_table(d) -> list[dict]:
    """概率表：特征阈值 → 上涨概率（供打分/过滤使用，含组合条件）。"""
    d = d or {}
    base = _num(d.get("baseline_prob"))
    out: list[dict] = []
    for r in (d.get("rules") or [])[:10]:
        if not isinstance(r, dict):
            continue
        out.append({
            "key": f"{_slug(str(r.get('feature')))}",
            "text": (f"概率表「{r.get('feature')} {r.get('direction')} {r.get('threshold')}」"
                     f"→ 上涨概率 {r.get('up_prob')}"
                     f"（基线 {r.get('baseline_prob') or base}，gain {r.get('gain')}，"
                     f"n={_int(r.get('sample_count'))}）"),
            "hint": "用于打分/过滤参考；样本 <30 只作观察，不得据此调阈值",
            "n": _int(r.get("sample_count")),
        })
    return out


def _extract_news_verdicts(d) -> list[dict]:
    """新闻判定分布：新闻层只用于风险规避（证伪/烟雾弹）。"""
    d = d or {}
    recs = [r for r in (d.get("records") or []) if isinstance(r, dict)]
    if not recs:
        return []
    cnt = Counter(str(r.get("verdict") or "?") for r in recs)
    dist = "、".join(f"{k} {v}" for k, v in cnt.most_common(5))
    return [{
        "key": "distribution",
        "text": f"新闻判定分布（{len(recs)} 条）：{dist}",
        "hint": "新闻判定只能用于风险规避；不得把「利好」当作上涨理由（已有验证否证）",
        "n": len(recs),
    }]


def _extract_policy_impact(d) -> list[dict]:
    """政策影响统计（plans/24）：政策事件的记录与兑现情况。"""
    d = d or {}
    ev = d.get("events") or []
    st = d.get("stats") or {}
    if not ev and not st:
        return []
    return [{
        "key": "stats",
        "text": (f"政策影响库：事件 {len(ev)} 条，统计 {_kv_brief(st, max_items=6)}"
                 f"（v{d.get('version')}）"),
        "hint": "政策事件只作情景参考；『已兑现 / 未兑现』必须分开，避免把预期当事实",
        "n": len(ev),
    }]


# ── 产物登记表（模块 → 文件 + 提取器 + 类型标签）──────────────
ARTIFACTS: list[dict] = [
    {"module": "washout", "label": "洗盘确认信号（plans/23）",
     "file": "washout_samples.json", "extract": _extract_washout, "plan": "23"},
    {"module": "buy_types", "label": "四类买点回测（plans/23）",
     "file": "buy_type_samples.json", "extract": _extract_buy_types, "plan": "23"},
    {"module": "game_features", "label": "人心博弈特征（plans/24）",
     "file": "game_features.json", "extract": _extract_game_features, "plan": "24"},
    {"module": "sentiment_gate", "label": "情绪门控（plans/23·24）",
     "file": "sentiment_gate.json", "extract": _extract_sentiment_gate, "plan": "24"},
    {"module": "discipline_grid", "label": "纪律网格择优（plans/22·23）",
     "file": "discipline_grid.json", "extract": _extract_discipline_grid, "plan": "22"},
    {"module": "entry_guidance", "label": "最佳入场（plans/23）",
     "file": "entry_guidance.json", "extract": _extract_entry_guidance, "plan": "23"},
    {"module": "condition_table", "label": "条件概率表（plans/23）",
     "file": "condition_table.json", "extract": _extract_condition_table, "plan": "23"},
    {"module": "form_leaderboard", "label": "形态规则有效性（plans/12·16）",
     "file": "form_leaderboard.json", "extract": _extract_form_leaderboard, "plan": "16"},
    {"module": "cost_basis", "label": "成本与滑点口径（plans/23）",
     "file": "cost_slippage_daily.json", "extract": _extract_cost_basis, "plan": "23"},
    {"module": "regime", "label": "市场环境分布（plans/23）",
     "file": "regime_series.json", "extract": _extract_regime_stats, "plan": "23"},
    # ── 第二批：对照 / 口径 / 数据可信度（"选股到底有没有用"）──────
    {"module": "pool_filter_oos", "label": "池过滤样本内外一致性（plans/23）",
     "file": "pool_filter_oos.json", "extract": _extract_pool_filter_oos, "plan": "23"},
    {"module": "walk_forward", "label": "滚动前向验证（plans/23）",
     "file": "walk_forward.json", "extract": _extract_walk_forward, "plan": "23"},
    {"module": "agent_refine", "label": "LLM 精筛净效果（plans/23）",
     "file": "agent_refine_effect.json", "extract": _extract_agent_refine_effect, "plan": "23"},
    {"module": "lowmom_scheme", "label": "低动量方案判定（plans/23）",
     "file": "lowmom_scheme.json", "extract": _extract_lowmom_scheme, "plan": "23"},
    {"module": "turnover_cost", "label": "换手成本重放（plans/23）",
     "file": "turnover_replay.json", "extract": _extract_turnover_replay, "plan": "23"},
    {"module": "t_overlap", "label": "T 重叠显著性重检（plans/23）",
     "file": "t_overlap_recheck.json", "extract": _extract_t_overlap_recheck, "plan": "23"},
    {"module": "ours_vs_baseline", "label": "我们 vs 基准（plans/24）",
     "file": "ours_baseline_daily.json", "extract": _extract_ours_baseline, "plan": "24"},
    {"module": "size_baseline", "label": "市值分层基线（plans/24）",
     "file": "size_baseline_daily.json", "extract": _extract_size_baseline, "plan": "24"},
    {"module": "data_validation", "label": "数据可信度门（口径前提）",
     "file": "validation_cache.json", "extract": _extract_data_validation, "plan": "25"},
    {"module": "early_data_quality", "label": "早期数据可比性（plans/25）",
     "file": "early_data_quality.json", "extract": _extract_early_data_quality, "plan": "25"},
    {"module": "form_coverage", "label": "形态识别覆盖率（plans/14）",
     "file": "form_coverage_test.json", "extract": _extract_form_coverage, "plan": "14"},
    {"module": "improve_effect", "label": "改动实盘追踪（plans/17）",
     "file": "improve_effect.json", "extract": _extract_improve_effect, "plan": "17"},
    {"module": "evolution_trend", "label": "进化快照趋势（总纲）",
     "file": "evolution_snapshots.json", "extract": _extract_evolution_snapshots, "plan": "21"},
    {"module": "probability_table", "label": "概率表（plans/23）",
     "file": "probability_table.json", "extract": _extract_probability_table, "plan": "23"},
    {"module": "news_verdict", "label": "新闻判定分布（plans/03）",
     "file": "news_verdicts.json", "extract": _extract_news_verdicts, "plan": "03"},
    {"module": "policy_impact", "label": "政策影响统计（plans/24）",
     "file": "policy_impact_kb.json", "extract": _extract_policy_impact, "plan": "24"},
]

EVIDENCE_PREFIX = "evidence:"
SELF_PROOF_TYPE = "evidence:selfproof"


def _upsert_lesson(module: str, label: str, item: dict, *, plan: str = "",
                   src_file: str = "") -> bool:
    """一条证据 → lesson（幂等；保留既有实验回馈字段）。"""
    key = str(item.get("key") or "")
    if not key:
        return False
    # 本层自建确定性 id（**保留数字**）：kb_distiller.lesson_id 会删数字，
    # 会把 ab_t1/ab_t5/ab_t10、h1/h5 压成同一条 → 丢结论
    lid = f"lesson:{EVIDENCE_PREFIX}{module}:{_slug(key)}"
    existing = kb_store.get("lesson", lid) or {}
    n = max(1, _int(item.get("n")))
    text = str(item.get("text") or "")[:_TXT_MAX]
    if not text:
        return False
    hint = str(item.get("hint") or "")[:160]
    conf = kb_distiller.confidence_for(n)
    # 与案例教训同一门槛：样本不足只作 candidate（只提示、不影响提案）
    status = "active" if n >= 30 else "candidate"
    if str(existing.get("status") or "") == "retired":
        status = "candidate"
    scope = {
        "side": "reference",          # 证据型知识：可检索、参考，但不冒充一线案例规律
        "source": f"kb_evidence:{module}",
        "module": module,
        "plan": plan,
        "label": label,
        "file": src_file,
    }
    if item.get("scope"):
        scope.update(item["scope"])
    return kb_store.upsert("lesson", {
        "id": lid,
        "ts": existing.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "text": text,
        "type": f"{EVIDENCE_PREFIX}{module}",
        "scope": scope,
        "actionable": {
            "hint": hint,
            "param_hint": str(item.get("param_hint") or "")[:160],
        },
        "support": {
            "n": n,
            "cases": [src_file] if src_file else [],
            "dates": [time.strftime("%Y%m%d")],
            "last_date": time.strftime("%Y%m%d"),
            "evidence": True,
        },
        "confidence": conf,
        "half_life_days": 240.0,      # 实证结论衰减比个案教训慢（半年口径量级）
        "applied_experiments": existing.get("applied_experiments") or [],
        "effect_verdict": existing.get("effect_verdict"),
        "status": status,
        "last_verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def absorb_module(artifact: dict) -> dict:
    """接入单个模块产物（返回 {"module", "file", "items", "ok", "reason"?}）。"""
    module = str(artifact.get("module") or "")
    label = str(artifact.get("label") or module)
    fname = str(artifact.get("file") or "")
    path = os.path.join(DATA_DIR, fname)
    if not fname or not os.path.exists(path):
        return {"module": module, "file": fname, "items": 0, "ok": False, "reason": "产物不存在"}
    data = _read_json(path, None)
    if data is None:
        return {"module": module, "file": fname, "items": 0, "ok": False, "reason": "读取失败"}
    try:
        items = artifact["extract"](data) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[kb_evidence] 提取 {module} 失败: {exc}")
        return {"module": module, "file": fname, "items": 0, "ok": False,
                "reason": f"提取异常:{exc}"}
    ok = 0
    for it in items:
        try:
            ok += 1 if _upsert_lesson(module, label, it, plan=str(artifact.get("plan") or ""),
                                      src_file=fname) else 0
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[kb_evidence] 写入 {module}:{it.get('key')} 失败: {exc}")
    return {"module": module, "file": fname, "items": ok, "ok": ok > 0}


def absorb_selfproof(max_runs: int = 2) -> dict:
    """接入自证测试报告（plans/25）—— 进化大脑最硬的"我的真实水平"证据。"""
    reps = _selfproof_reports(max_runs=max_runs)
    if not reps:
        return {"module": "selfproof", "items": 0, "ok": False, "reason": "无 report.json"}
    ok = 0
    for run_id, d in reps:
        for it in _extract_selfproof(d, run_id=run_id):
            try:
                ok += 1 if _upsert_lesson("selfproof", "自证回放（plans/25）", it, plan="25",
                                          src_file=f"selfproof/{run_id}/report.json") else 0
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[kb_evidence] 写入 selfproof:{it.get('key')} 失败: {exc}")
    # 数据门 / 幸存者偏差声明：样本边界必须与结论一起进脑
    gate = _read_json(os.path.join(DATA_DIR, "selfproof", "data_gate.json"), None)
    if isinstance(gate, dict) and gate:
        for it in _extract_selfproof_gate(gate):
            try:
                ok += 1 if _upsert_lesson("selfproof", "自证数据门（plans/25）", it, plan="25",
                                          src_file="selfproof/data_gate.json") else 0
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[kb_evidence] 写入 selfproof_gate 失败: {exc}")
    return {"module": "selfproof", "items": ok, "ok": ok > 0, "runs": len(reps)}


def absorb_all(rebuild: bool = False) -> dict:
    """遍历登记表，把新增模块的验证产出统一接成"进化大脑的神经"。

    幂等（id 确定性）、只读原始文件、失败静默。返回各模块条数与总数。
    rebuild=True：先清空证据层再重建（id 口径升级/维护时用；不动 discipline）。
    """
    if rebuild:
        n = kb_store.delete_lessons_by_type_prefix(EVIDENCE_PREFIX.rstrip(":"))
        if n:
            logger.info(f"[kb_evidence] 证据层重建：清理旧条目 {n} 条")
    out: dict = {}
    total = 0
    for art in ARTIFACTS:
        r = absorb_module(art)
        out[str(art.get("module"))] = r.get("items", 0)
        total += int(r.get("items", 0) or 0)
    selfproof = absorb_selfproof()
    out["selfproof"] = selfproof.get("items", 0)
    total += int(selfproof.get("items", 0) or 0)
    out["total"] = total
    if total:
        logger.info(f"[kb_evidence] 模块证据接入完成: {out}")
    return out


def evidence_lessons(limit: int = 20, include_archived: bool = False) -> list[dict]:
    """取证据型教训（供 L0 摘要 / 综合分析上下文 / 前端看板）。

    说明：type 前缀匹配由 kb_store 的 LIKE 实现处理（'evidence' → 'evidence:%'）。
    另外统一样本量键：纪律教训（plan_tracker/distill）用 cases_n/total_n，这里归一化到
    support.n，避免下游把"有样本的实证"误显示为 n=None。
    """
    rows = kb_store.query_lessons_by_type(
        [EVIDENCE_PREFIX.rstrip(":"), "discipline"], limit=limit,
        exclude_status=() if include_archived else ("archived",))
    for l in rows:
        s = dict(l.get("support") or {})
        if not s.get("n"):
            s["n"] = s.get("cases_n") or s.get("total_n") or 1
            l["support"] = s
    return rows


def stats() -> dict:
    """证据层总览：各模块条数 + active 条数（供 API/审计）。

    用 SQL 前缀直取（而非全表 limit），避免库超过 limit 时漏统计。
    """
    rows = (kb_store.query_lessons_by_type([EVIDENCE_PREFIX.rstrip(":")], limit=5000)
            + kb_store.query_lessons_by_type(["discipline"], limit=2000))
    by_mod: dict[str, int] = {}
    active = 0
    for l in rows:
        t = str(l.get("type") or "")
        mod = t.split(":", 1)[1] if ":" in t else t
        by_mod[mod] = by_mod.get(mod, 0) + 1
        if str(l.get("status")) == "active":
            active += 1
    return {"by_module": by_mod, "n": sum(by_mod.values()), "active": active,
            "artifacts": len(ARTIFACTS) + 1}


__all__ = ["ARTIFACTS", "absorb_all", "absorb_module", "absorb_selfproof",
           "evidence_lessons", "stats", "EVIDENCE_PREFIX", "SELF_PROOF_TYPE"]
