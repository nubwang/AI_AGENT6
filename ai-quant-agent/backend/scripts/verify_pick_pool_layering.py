"""验证：候选池「分层输出」是否够格上线（plans/24 §11.5 第 1 项）

## 背景

`diagnose_pick_pool.py` 在**样本内**发现：

    - 候选池整体（日等权）      = −2.434%
    - 打分 Q5（当日截面前 20%） = **+0.636%**（up_probability）
                                   **+2.736%**（rank_score）
    - 单调性 Spearman = +0.900（两种打分、两种口径都成立）

→ 假设：把「每天推 30 只」改成「**按打分分层输出**」能提升期望。

## 但那个结论有三个致命缺口，本脚本逐一对付

| # | 缺口 | 本脚本的对付方式 |
| - | ---- | ---------------- |
| A | **未扣成本** | 扣双边 0.50%（`plan_cost_pct`=0.25% × 2）后重算 |
| B | **阈值可能过拟合** | 换 4 种分层规则（前 20% / 前 40% / 绝对 0.85 / 绝对 0.90），看是否一致 |
| C | **可能靠少数几天或右尾** | 逐日展开：正收益天数、最大单日贡献占比、**中位数 vs 均值** |
| D | **可能只是某一个打分的运气** | 用 `rank_score`（另一个独立排序键）重做 B |

## ⚠️ 这不是严格的样本外验证

可评估交易日只有 **22 天**，按时间切一半（11 + 11）噪声大到无法判读
—— 与其做一个"看起来严谨但没有功效"的样本外，不如**把样本内结论往死里打**：
扣成本、换阈值、看分布、换打分。**四条全过才算"值得进下一步"，任一条不过就地否掉。**

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_pick_pool_layering.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.backtest import sentiment_gate as SG  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICK_DIR = os.path.join(BACKEND, "data", "daily_recommend")
H = 5
MAX_ABS = 1.5
COST_RT = 0.005          # 双边成本 0.50%（plan_cost_pct=0.25% × 2）
PASS, FAIL = [], []


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '⚠️ '} {name}" + (f" — {detail}" if detail else ""))


# ─────────────────────────────────────────────────────────────────────────────
def load_picks() -> pd.DataFrame:
    """读所有榜单（每个 (日期, 是否 agent) 取最后版本）→ 长表。"""
    files = sorted(glob.glob(os.path.join(PICK_DIR, "daily_*.json")))
    latest: dict[tuple[str, bool], str] = {}
    for p in files:
        m = re.match(r"daily_(\d{8})(_agent)?(_v\d+)?\.json$", os.path.basename(p))
        if m:
            latest[(m.group(1), bool(m.group(2)))] = p
    rows: list[dict] = []
    for (date, is_agent), p in sorted(latest.items()):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for it in (d.get("top_picks") or []):
            rows.append({
                "date": str(d.get("date") or date).replace("-", "")[:8],
                "is_agent": is_agent,
                "ts_code": str(it.get("ts_code") or ""),
                "form_type": str(it.get("form_type") or ""),
                "up_probability": it.get("up_probability"),
                "rank_score": it.get("rank_score"),
            })
    df = pd.DataFrame(rows)
    for c in ("up_probability", "rank_score"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def attach_fwd(df: pd.DataFrame) -> pd.DataFrame:
    d0, d1 = str(df["date"].min()), str(df["date"].max())
    fwd = SG.build_panel(SG.HORIZONS, d0, d1, keep_stock=True)
    col = f"fwd{H}"
    out = df.merge(fwd[["trade_date", "ts_code", col]], how="left",
                   left_on=["date", "ts_code"], right_on=["trade_date", "ts_code"])
    out = out.rename(columns={col: "fwd5"})
    out.loc[out["fwd5"].abs() > MAX_ABS, "fwd5"] = np.nan
    out["fwd5_net"] = out["fwd5"] - COST_RT
    return out


def dstat(df: pd.DataFrame, col: str = "fwd5") -> dict:
    """日等权均值 + 中位 + 胜率 + 天数（与 H2/H3/Pool 同口径）。"""
    v = pd.to_numeric(df[col], errors="coerce")
    keep = v.notna()
    if not keep.any():
        return {"n": 0, "n_days": 0, "mean_d": None, "med": None, "win": None}
    v, dd = v[keep], df["date"][keep]
    byd = v.groupby(dd).mean()
    return {"n": int(len(v)), "n_days": int(byd.shape[0]),
            "mean_d": round(float(byd.mean()), 5),
            "med": round(float(v.median()), 5),
            "win": round(float((v > 0).mean()), 4)}


def _row(name: str, df: pd.DataFrame) -> None:
    g = dstat(df, "fwd5")
    n = dstat(df, "fwd5_net")
    c1 = f"{g['mean_d']:+.3%}" if g["mean_d"] is not None else "—"
    c2 = f"{n['mean_d']:+.3%}" if n["mean_d"] is not None else "—"
    c3 = f"{g['med']:+.3%}" if g["med"] is not None else "—"
    c4 = f"{g['win']:.1%}" if g["win"] is not None else "—"
    print(f"{name:<28}{g['n']:>6}{g['n_days']:>8}{c1:>13}{c2:>13}{c3:>11}{c4:>8}")


def _hdr() -> None:
    h = (f"{'子集':<28}{'n':>6}{'天数':>8}{'均值(日等权)':>13}"
         f"{'扣成本后':>13}{'中位':>11}{'胜率':>8}")
    print(h)
    print("-" * len(h))


def top_q(df: pd.DataFrame, col: str, q: float) -> pd.DataFrame:
    """当日截面 top-q 分位（q=0.2 → 前 20%）。"""
    th = df.groupby("date")[col].transform(lambda s: s.quantile(1 - q))
    return df[(df[col].notna()) & (df[col] >= th)]


def main() -> int:
    print("=" * 96)
    print("验证：候选池「分层输出」是否够格上线（plans/24 §11.5）")
    print("=" * 96)
    df = attach_fwd(load_picks())
    ev = df.dropna(subset=["fwd5"]).copy()
    print(f"样本：{len(ev)} 条 / {ev['date'].nunique()} 个可评估交易日"
          f"（{ev['date'].min()}~{ev['date'].max()}）")
    print(f"成本假设：双边 {COST_RT:.2%}（plan_cost_pct 0.25% × 2）")

    # ── A 扣成本 ──
    print()
    print("=== A 扣成本（双边 0.50%）===")
    _hdr()
    _row("全部 30 只（现状）", ev)
    _row("打分 Q5（up_prob 前 20%）", top_q(ev, "up_probability", 0.2))
    _row("打分 Q5（rank_score 前 20%）", top_q(ev, "rank_score", 0.2))
    _row("Agent 精筛 4 只", ev[ev["is_agent"]])
    q5 = top_q(ev, "up_probability", 0.2)
    a1 = dstat(q5, "fwd5_net")["mean_d"]
    a0 = dstat(ev, "fwd5_net")["mean_d"]
    if a1 is not None and a0 is not None:
        det = f"Q5 {a1:+.3%} vs 整体 {a0:+.3%}（差 {a1 - a0:+.3%}）"
    else:
        det = f"Q5={a1} 整体={a0}"
    ck("A1 扣成本后 Q5 仍优于整体",
       a1 is not None and a0 is not None and a1 > a0, det)
    ck("A2 ★ 扣成本后 Q5 仍为**正**（否则不能上线）",
       a1 is not None and a1 > 0,
       f"Q5 扣成本后 {a1:+.3%}" + ("（为正）" if (a1 or 0) > 0 else "（为负 → 就地否掉）"))

    # ── B 阈值稳健性（up_probability）──
    print()
    print("=== B 阈值稳健性：换 4 种分层规则（up_probability）===")
    _hdr()
    rules_b = [
        ("当日截面前 20%（Q5）", top_q(ev, "up_probability", 0.2)),
        ("当日截面前 40%（Q4-Q5）", top_q(ev, "up_probability", 0.4)),
        ("绝对阈值 >= 0.85", ev[ev["up_probability"] >= 0.85]),
        ("绝对阈值 >= 0.90", ev[ev["up_probability"] >= 0.90]),
    ]
    for nm, sub in rules_b:
        _row(nm, sub)
    pos_b = [nm for nm, sub in rules_b
             if (dstat(sub, "fwd5_net")["mean_d"] or -9) > 0]
    ck("B1 至少一半规则在扣成本后仍为正（否则是阈值过拟合）",
       len(pos_b) * 2 >= len(rules_b), f"为正的规则：{pos_b or '无'}")

    # ── C 逐日分解（是否靠少数几天）──
    print()
    print("=== C 逐日分解（Q5 的优势是不是只来自少数几天）===")
    d = q5.dropna(subset=["fwd5"])
    byd = d.groupby("date")["fwd5"].agg(["size", "mean"])
    tot = float(byd["mean"].sum())
    n_pos = int((byd["mean"] > 0).sum())
    print(f"  Q5 覆盖 {len(byd)} 天，其中正收益 {n_pos} 天（{n_pos / max(1, len(byd)):.0%}）")
    if abs(tot) > 1e-12:
        top1 = float(byd["mean"].max()) / tot
        top3 = float(byd["mean"].nlargest(3).sum()) / tot
        print(f"  最大单日贡献 / 全部 = {top1:.0%}；前 3 天贡献 = {top3:.0%}")
        ck("C1 不靠单一极端日（最大单日贡献 < 60%）", top1 < 0.6,
           f"最大单日贡献 {top1:.0%}")
    ck("C2 ★ Q5 正收益天数 > 50%（否则是『偶尔暴赚』）",
       n_pos * 2 > len(byd), f"{n_pos}/{len(byd)} 天为正")
    # 中位数：若中位为负而均值为正 → 靠右尾
    qm, qmed = dstat(q5, "fwd5")["mean_d"], dstat(q5, "fwd5")["med"]
    print(f"  Q5：日等权均值 {qm:+.3%}｜**中位 {qmed:+.3%}**｜"
          f"胜率 {dstat(q5, 'fwd5')['win']:.1%}")
    ck("C3 ★ Q5 中位数不显著为负（均值不靠右尾单撑）",
       qmed is not None and qmed > -0.005,
       f"中位 {qmed:+.3%}" + ("（可接受）" if (qmed or 0) > -0.005 else "（靠右尾 → 实盘体验差）"))

    # ── D 跨打分一致性（rank_score）──
    print()
    print("=== D 跨打分一致性：同一套规则换 rank_score 重做 ===")
    _hdr()
    rules_d = [
        ("当日截面前 20%", top_q(ev, "rank_score", 0.2)),
        ("当日截面前 40%", top_q(ev, "rank_score", 0.4)),
    ]
    for nm, sub in rules_d:
        _row(nm, sub)
    ok_d = all((dstat(sub, "fwd5_net")["mean_d"] or -9) > 0 for _, sub in rules_d)
    ck("D1 ★ 两个独立排序键结论一致（rank_score 也指向'高分档更好'）", ok_d,
       "一致" if ok_d else "不一致 → 只有 up_probability 有效，需查原因")

    # ── E 形态混杂检查 ──
    print()
    print("=== E 形态混杂检查（Q5 的优势会不会只是'碰巧多拿了某形态'）===")
    _hdr()
    for ft in sorted(ev["form_type"].unique()):
        _row(f"全部·形态 {ft}", ev[ev["form_type"] == ft])
    print()
    for ft in sorted(ev["form_type"].unique()):
        _row(f"Q5·形态 {ft}", q5[q5["form_type"] == ft])

    print()
    print("=" * 96)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("未通过：")
        for f in FAIL:
            print(f"  ⚠️  {f}")
    print()
    print("判读：A2 / B1 / C2 / C3 / D1 这五条是'能否进下一步'的硬门槛。")
    print("      任一条不过 → 说明样本内的分层优势不成立，**就地否掉，不改进生产**。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
