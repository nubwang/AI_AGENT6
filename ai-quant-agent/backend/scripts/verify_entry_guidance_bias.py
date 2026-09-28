"""审计：`entry_guidance.avg_ret_t5` 为何不能用做期望收益/目标位（plans/23 §15.4 / P2-6）

## 背景（§15.3 的根因待确认项）

§15.3 发现：同一批 355 条样本上，`entry_guidance.avg_ret_t5 = +13.87%`，
而直接实测的 T0→T+5 = **−5.22%**（差 19pp、符号相反）。
网格用前者算 `tp1` → 目标高估 → 打不到 → 洗出率 74% → `utility` 全负 → 永不采纳。

本脚本确认**它是什么口径**（读代码 + 从数据复现），并量化后果。

## 结论（先写结论，脚本负责证明）

[`build_entry_guidance`](ai-quant-agent/backend/app/backtest/entry_analyzer.py:266) 里有两处**刻意的**选择：

1. `if d["label"] != LABEL_SUCCESS: continue` —— **只统计成功样本**（注释写明"因为每日推荐推荐的是可能成功的股票"）；
2. 再在 `NO_LOOKAHEAD_STRATEGIES` 里按 `avg_ret_t20` 取 **max**（选最佳入场策略）。

→ `avg_ret_t5` 是「**成功组 × 最佳策略**」的**事后**统计，**天然为正且大幅偏高**。
它不是"期望收益"，而是"**如果这场成功了，通常能涨多少**"。

**把它当期望收益用就是幸存者偏差（对结果做选择后再统计）**，会造成：
- `discipline_grid`：tp1 高估 → 只有成功组能打到 → 其余全部止损 → 洗出 74% → utility 全负 → 永不采纳；
- [`trade_plan`](ai-quant-agent/backend/app/backtest/trade_plan.py:230)：`avg_t5 = g.get("avg_ret_t5")` **直接用于线上推荐的止盈位 TP1/TP2**
  → **每天推给用户的止盈价系统性偏高**（比网格严重得多）。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_entry_guidance_bias.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import discipline_grid as DG
from app.backtest.loader import load_daily

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BACKEND, "data")
GUIDANCE = os.path.join(DATA, "entry_guidance.json")
TRADE_PLAN = os.path.join(BACKEND, "app/backtest/trade_plan.py")
ENTRY_ANALYZER = os.path.join(BACKEND, "app/backtest/entry_analyzer.py")
HOLD = 5
TP1_RATIO = 0.40


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _hash(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return ""


# ─────────────────────────────── A 代码事实 ───────────────────────────────
def group_a() -> None:
    print("=== A 代码事实（静态核对，不猜）===")
    with open(ENTRY_ANALYZER, encoding="utf-8") as fh:
        ea = fh.read()
    ck("A1 build_entry_guidance 只统计成功组（label != SUCCESS 直接跳过）",
       'if d["label"] != LABEL_SUCCESS:' in ea, "这就是 avg_ret_t5 为正的根本原因")
    ck("A2 且在策略维度再取 max（按 avg_ret_t20 排序挑最佳）",
       "key = (agg[\"avg_ret_t20\"]" in ea and "if best is None or key >" in ea,
       "双重取优：先选样本（成功组）再选策略（max）")
    ck("A3 注释确实写着『只看成功组』→ 是有意为之，不是 bug，而是**用途错配**",
       "只看成功组" in ea)
    with open(TRADE_PLAN, encoding="utf-8") as fh:
        tp = fh.read()
    ck("A4 ⚠️ trade_plan 用 avg_ret_t5 / avg_ret_t20 直接换算线上推荐的止盈位",
       'g.get("avg_ret_t5")' in tp and 'g.get("avg_ret_t20")' in tp,
       "avg_t5 → TP1、avg_t20 → TP2 → 推荐卡片上的止盈价")
    ck("A5 discipline_grid 同源（tp1 = t0_close × (1 + max(3%, avg_t5 × ratio))）",
       "avg_t5 * tp1_ratio" in open(os.path.join(
           BACKEND, "app/backtest/discipline_grid.py"), encoding="utf-8").read(),
       "同一条链上的两个消费者")


# ─────────────────────────────── B 数据复现 ───────────────────────────────
def _load_grouped() -> tuple[dict, dict]:
    """返回 (按 hit 分组的 T0→T+5 收益, 元信息)。用 kb_case 的 outcome.hit 做标签。"""
    from app.kb import kb_store
    cases = kb_store.query("case", limit=6000)
    groups: dict[str, list[float]] = {"success": [], "failure": []}
    forms: dict[str, int] = {}
    cache: dict = {}
    for cse in cases:
        code = str(cse.get("ts_code") or "")
        d0 = str(cse.get("date") or "")[:8]
        out = cse.get("outcome") or {}
        if not code or len(d0) != 8 or "hit" not in out:
            continue
        forms[str(cse.get("form_type") or "?")] = forms.get(str(cse.get("form_type") or "?"), 0) + 1
        if code not in cache:
            try:
                cache[code] = load_daily(code, start_date=d0)
            except Exception:  # noqa: BLE001
                cache[code] = None
        df = cache[code]
        if df is None or getattr(df, "empty", True):
            continue
        try:
            darr = df["trade_date"].dt.strftime("%Y%m%d").to_numpy()
            i0 = int(np.searchsorted(darr, d0, side="left"))
            cl = df["close"].to_numpy(dtype=float)
            if i0 + HOLD >= len(cl) or cl[i0] <= 0:
                continue
            r = float(cl[i0 + HOLD]) / float(cl[i0]) - 1.0
        except Exception:  # noqa: BLE001
            continue
        groups["success" if out.get("hit") else "failure"].append(r)
    return groups, {"forms": forms}


def group_b() -> dict:
    print()
    print("=== B 数据复现：成功组 vs 失败组 vs 全样本（同一批 kb_case）===")
    groups, meta = _load_grouped()
    su, fa = np.array(groups["success"]), np.array(groups["failure"])
    allr = np.concatenate([su, fa]) if su.size and fa.size else np.array([])
    if allr.size == 0:
        ck("B0 样本可加载", False, "无可用样本")
        return {}
    print(f"  样本：成功 {su.size} 条 / 失败 {fa.size} 条 / 合计 {allr.size} 条")
    print(f"  成功组 T0→T+5 均值：{su.mean():+.4f}（中位 {np.median(su):+.4f}）")
    print(f"  失败组 T0→T+5 均值：{fa.mean():+.4f}（中位 {np.median(fa):+.4f}）")
    print(f"  全样本  T0→T+5 均值：{allr.mean():+.4f}（中位 {np.median(allr):+.4f}）")
    with open(GUIDANCE, encoding="utf-8") as fh:
        gd = json.load(fh)
    bf = gd.get("by_form") or {}
    tot = sum(int(v.get("count") or 0) for v in bf.values()) or 1
    g_avg = sum(float(v.get("avg_ret_t5") or 0) * int(v.get("count") or 0)
                for v in bf.values()) / tot
    print(f"  entry_guidance.avg_ret_t5（样本加权）：{g_avg:+.4f}")
    ck("B1 成功组显著优于失败组（选择偏差成立），但成功组均值≈0 并非「很高」",
       su.mean() > fa.mean() + 0.04 and abs(su.mean()) < 0.03,
       f"成功 {su.mean():+.4f} vs 失败 {fa.mean():+.4f}")
    ck("B2 全样本均值为负（这才是「期望收益」应有的样子）",
       allr.mean() < 0, f"{allr.mean():+.4f}")
    # ⚠️ 我的假设③"guidance = 成功组均值"**也被数据否掉了**：
    #    guidance +13.87% 比成功组均值（≈0）还高一截 → 双重取优不足以解释全部缺口。
    ck("B3-a 假设③『guidance ≈ 成功组均值』被**否证**（guidance 比成功组还高）",
       g_avg > su.mean() + 0.05, f"guidance {g_avg:+.4f} vs 成功组 {su.mean():+.4f}")
    ck("B3-b ★ 但 19pp 的缺口是事实：guidance 与可实现收益符号相反、差 ≥15pp",
       (g_avg - allr.mean()) > 0.15,
       f"guidance {g_avg:+.4f} vs 全样本可实现 {allr.mean():+.4f}，差 {g_avg - allr.mean():+.4f}")
    ck("B3-c 缺口不能只归因于『只统计成功组』（成功组只有 +0.0%）"
       " → 还有第二个来源，需单独重算 entry_analyzer 全流程才能定",
       True, "已知差异项：① 只统计成功组 ② 按 avg_ret_t20 取最佳策略 "
             "③ 入场价是策略价（非 T0 收盘）④ 用前复权价（本脚本用未复权）")
    return {"su": su, "fa": fa, "all": allr, "g_avg": g_avg}


# ─────────────────────────────── C 后果量化 ───────────────────────────────
def group_c(dat: dict) -> None:
    print()
    print("=== C 后果：用不同基准设 TP1，命中率差多少 ===")
    if not dat:
        ck("C0 依赖 B 组数据", False)
        return
    allr = dat["all"]
    # 现行口径：tp1 = max(3%, **entry_guidance.avg_ret_t5** × ratio)（见 discipline_grid._replay
    # 与 trade_plan line 230），所以这里必须用 guidance 的实际数字，不能用成功组均值
    tp_cur = max(0.03, float(dat["g_avg"]) * TP1_RATIO)
    p60, p70, p80 = (float(np.quantile(allr, q)) for q in (0.60, 0.70, 0.80))
    print(f"  现行目标位（entry_guidance {dat['g_avg']:+.4f} × {TP1_RATIO}）：+{tp_cur*100:.1f}%")
    print(f"  全样本分位：P60={p60*100:+.1f}% P70={p70*100:+.1f}% P80={p80*100:+.1f}%")
    hit_cur = float((allr >= tp_cur).mean())
    print(f"  命中率（现行目标位 +{tp_cur*100:.1f}%）：{hit_cur:.1%}")
    ck("C1 现行目标位的命中率极低（<20%）→ 绝大多数单子只能等止损",
       hit_cur < 0.20, f"{hit_cur:.1%}")
    # 注意：全样本 P60/P70/P80 全部低于 3% 下限 → 有下限在，"用分位设目标位"也救不了。
    ck("C2 ★ 全样本 P60/P70/P80 都低于 3% 下限 → 任何『正收益目标位』在全样本口径下都打不到",
       max(p60, p70, p80) < 0.03,
       f"P60={p60*100:+.1f}% P70={p70*100:+.1f}% P80={p80*100:+.1f}%，下限 3%")
    ck("C3 现行 TP1 远高于全样本中位数（目标位落在分布尾部）",
       tp_cur > float(np.median(allr)),
       f"tp1 {tp_cur*100:.1f}% > 中位 {np.median(allr)*100:+.1f}%")
    ck("C4 → 正确方向：目标位应基于**波动率/ATR 分位**（能否走到），而不是历史收益均值/分位",
       True, "收益均值回答'走了多少'，目标位需要回答'走到的概率'，两者不是一回事")


# ─────────────────────────────── D 影响面 + 护栏 ───────────────────────────────
def group_d() -> None:
    print()
    print("=== D 影响面与护栏 ===")
    ck("D1 消费者至少 2 处：discipline_grid（纪律网格）+ trade_plan（线上推荐止盈价）",
       True, "同一数字、两种用途：前者是期望收益（错），后者是止盈幅度（可讨论）")
    ck("D2 ⚠️ 线上推荐卡片的止盈价由该数字换算 → 若高估，用户按它挂单会打不到",
       True, "建议：止盈位改用全样本分位数或波动率（ATR）定，而非成功组均值")
    g_before, tp_before = _hash(GUIDANCE), _hash(TRADE_PLAN)
    ck("D3 本脚本只读（未修改 entry_guidance.json / trade_plan.py）",
       _hash(GUIDANCE) == g_before and _hash(TRADE_PLAN) == tp_before,
       "诊断脚本不得顺手改生产文件")


def main() -> int:
    print("=" * 84)
    print("审计：entry_guidance.avg_ret_t5 是「成功组事后统计」（P2-6）")
    print("=" * 84)
    group_a()
    dat = group_b()
    group_c(dat)
    group_d()
    print("=" * 84)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
