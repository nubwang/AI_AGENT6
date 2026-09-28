"""洗盘识别器诊断（P1-6b）— 回答"这套识别到底有没有信息量、该不该保留"

P1-6 的 A/B 给出否证（T+1~T+10 均不显著），P1-8 的环境分层也**没能救回来**
（强势/震荡仍不显著，弱势样本不足）→ 需要进一步定位，而不是直接放弃或硬上。

本脚本做 4 个互补检验（全部复用 `data/washout_samples.json`，秒级，不重扫）：

  A **严格对照**：对照组只取 `无洗盘特征`（原版对照含大量"回调中"，与洗盘太近）
  B **分数单调性**：按 washout_score 分桶看 T+5 是否单调 —— 有信号就该单调
  C **负向检验（最有价值）**：`疑似出货`（被硬否决）组 vs 同日未被否决组。
     若出货组显著更差 → 识别器**有预测力，只是方向是"排除"而非"买入"**，
     可直接用于每日推荐的负向过滤（这才是推荐系统真正需要的）。
  D **stage 排序**：各类 stage 的 T+5 均值/赚钱率排序（看有无可用分层）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/diagnose_washout.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import washout_detector as WD
from app.backtest import washout_samples as WS

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_points() -> tuple[list[dict], dict]:
    p = WS.SAMPLES_FILE
    if not os.path.exists(p):
        return [], {}
    with open(p, "r", encoding="utf-8") as f:
        d = json.load(f)
    return d.get("points") or [], d


def _stats(points: list[dict], h: str = "t5") -> dict:
    vals = np.array([x[h] for x in points if x.get(h) is not None], dtype=float)
    if len(vals) == 0:
        return {"n": 0}
    return {"n": int(len(vals)),
            "pos": round(float((vals > 0).mean()), 4),
            "gt5": round(float((vals > 0.05).mean()), 4),
            "mean": round(float(vals.mean()), 4),
            "median": round(float(np.median(vals)), 4)}


def _line(tag: str, s: dict, ref: dict | None = None) -> str:
    if not s.get("n"):
        return f"  {tag:<26} n=0"
    extra = ""
    if ref and ref.get("n"):
        extra = f"  超额={s['mean'] - ref['mean']:+.4f}"
    return (f"  {tag:<26} n={s['n']:>6} 赚钱率={s['pos']:.3f} >5%={s['gt5']:.3f} "
            f"均值={s['mean']:+.4f} 中位={s['median']:+.4f}{extra}")


def main() -> int:
    pts, meta = _load_points()
    if not pts:
        print("❌ 无样本库，先跑 scripts/build_washout_samples.py")
        return 1
    print(f"样本库：{len(pts)} 点（{meta.get('wash_n')} 个确认日，{meta.get('built_at')}）")
    wash = [p for p in pts if p.get("confirmed")]
    print(f"洗盘组 {len(wash)}，全样本 T5 基准 {_stats(pts)['mean']:+.4f}\n")

    print("=== A 严格对照（只与『无洗盘特征』比，同日配对）===")
    clean = [p for p in pts if p.get("stage") == WD.STAGE_NONE and not p.get("vetoed")]
    wd_dates = {p.get("date") for p in wash}
    ctl = [p for p in clean if p.get("date") in wd_dates]
    a, b = _stats(wash), _stats(ctl)
    print(_line("洗盘组(confirmed)", a))
    print(_line("严格对照(无洗盘特征)", b, a))
    pv = WS.perm_p_value(np.array([p["t5"] for p in wash if p.get("t5") is not None]),
                         np.array([p["t5"] for p in ctl if p.get("t5") is not None]))
    print(f"  → 置换检验 p={pv}｜样本不足={b.get('n', 0) < WS.MIN_N}")

    print("\n=== B 分数单调性（有信号就该单调）===")
    buckets = [(0.0, 0.35), (0.35, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01)]
    for lo, hi in buckets:
        g = [p for p in pts if lo <= float(p.get("score") or 0) < hi and p.get("t5") is not None]
        tag = f"score [{lo:.2f},{hi:.2f})"
        s = _stats(g)
        print(_line(tag, s))
    print("  → 判断标准：均值应随分数上升而上升；若乱序 → 分数本身无信息")

    print("\n=== C 负向检验（最有价值：识别器能否用于『排除』）===")
    # 被硬否决（疑似出货）：拿它和"同日、未被否决"的样本比
    vetoed = [p for p in pts if p.get("vetoed")]
    v_dates = {p.get("date") for p in vetoed}
    non_veto = [p for p in pts if (not p.get("vetoed")) and p.get("date") in v_dates]
    for h in ("t1", "t5", "t10"):
        sv, sn = _stats(vetoed, h), _stats(non_veto, h)
        pvh = WS.perm_p_value(
            np.array([p[h] for p in vetoed if p.get(h) is not None], dtype=float),
            np.array([p[h] for p in non_veto if p.get(h) is not None], dtype=float))
        print(f"  [{h}] 疑似出货 n={sv.get('n')} 均值={sv.get('mean')} | "
              f"同日未否决 n={sn.get('n')} 均值={sn.get('mean')} | "
              f"差={(sv.get('mean') or 0) - (sn.get('mean') or 0):+.4f} p={pvh}")
    print("  → 若『疑似出货』显著更差：识别器有预测力，可用于负向排除（推荐系统真正需要的）")

    print("\n=== D stage 排序（看有无可用分层）===")
    for st in sorted({str(p.get("stage") or "") for p in pts}):
        g = [p for p in pts if str(p.get("stage") or "") == st]
        print(_line(st, _stats(g), _stats(pts)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
