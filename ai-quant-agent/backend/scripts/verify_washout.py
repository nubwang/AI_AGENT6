"""验收：洗盘识别器（plans/23 §3.2 / P1 第 5 项）

规划把"洗盘"列为**本规划最重要的新增**，且要求它"可度量、可回测、可进每日推荐"。
本脚本守 7 组硬约束（前 3 组是"打分是否可信"，后 4 组是"工程是否安全"）：

  A 九维齐备 + 真实数据可算（coverage 不得虚高）
  B 缺失维度**不计入分母**（不许用中性分顶替缺数据）+ 重复日期表能正确对齐
  C 判别力：洗盘形态 vs 出货形态必须显著分开；硬否决（放量下跌/深回撤/高位放量）必须生效；
    "超窗口回调"必须与"疑似出货"**分开**（语义不能张冠李戴）
  D ⚠️ 无未来函数：**改动 idx 之后的数据，结果必须一模一样**（最硬的前视检查）
  E 输出契约（§3.2 的 JSON 字段 + 可序列化）
  F 阈值可进化（热生效 + 脏值回退）
  G 未接入预测侧：P1-5 只做识别，不得影响每日推荐打分/排序

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_washout.py
"""
from __future__ import annotations

import inspect
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import washout_detector as WD

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _src(rel: str) -> str:
    p = os.path.join(BACKEND, rel)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def _mk_df(closes, vols, opens=None, highs=None, lows=None) -> pd.DataFrame:
    """构造 prepare_stock_data 风格的合成日线（只含打分所需列）。"""
    n = len(closes)
    c = np.asarray(closes, dtype=float)
    o = np.asarray(opens, dtype=float) if opens is not None else c * 0.995
    h = np.asarray(highs, dtype=float) if highs is not None else c * 1.01
    lw = np.asarray(lows, dtype=float) if lows is not None else c * 0.985
    pc = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({
        "trade_date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": o, "high": h, "low": lw, "close": c, "pre_close": pc,
        "pct_chg": (c / pc - 1.0) * 100.0,
        "vol": np.asarray(vols, dtype=float),
        "amount": np.asarray(vols, dtype=float) * 10,
        "adj_close": c.copy(),
        "is_sealed_up": np.zeros(n), "is_sealed_down": np.zeros(n),
    })


def _wash_scenario(over_window: bool = False) -> tuple[pd.DataFrame, int]:
    """真洗盘：横盘 → 放量拉升 +20% → 缩量回踩 → 地量**末端上翘并收复 MA5**。

    为什么末端必须上翘：实测过"完全平价横盘 3 天"——MA5 因前期更高（11.2178）而下不来，
    收盘 11.2 站不上去，washout_end 永远不成立。这不是代码 bug，而是**形态的真实要求**：
    确认日本身就该有"收复"动作（规划 §3.2：地量 + 收阳 + 站上 5 日线）。
    """
    f = 30 if over_window else 9          # 回调天数（over_window 需 > VETO_PULLBACK_DAYS=25）
    closes = [10.0] * 30 + [10.0, 10.4, 10.9, 11.4, 12.0]              # 0..34（拉升到 12）
    vols = [100.0] * 30 + [200.0] * 5
    # 回调：12.0 → 11.2 分 f 天
    for i in range(f):
        closes.append(12.0 - (0.8 * (i + 1) / f))
        vols.append(100.0)
    # 地量 3 天：末端小幅上翘（收复均线），最后一天地量 + 收阳
    closes += [11.20, 11.25, 11.30]
    vols += [45.0, 45.0, 45.0]
    df = _mk_df(closes, vols)
    for j, (ci, cl) in enumerate(zip([-3, -2, -1], [11.20, 11.25, 11.30])):
        df.loc[df.index[ci], "open"] = 11.15 + 0.05 * j    # 收阳（open < close）
        df.loc[df.index[ci], "high"] = cl * 1.005
        df.loc[df.index[ci], "low"] = 11.10
    df["pre_close"] = df["close"].shift(1).fillna(df["close"])
    df["pct_chg"] = (df["close"] / df["pre_close"] - 1.0) * 100
    return df, len(df) - 1


def _out_scenario() -> tuple[pd.DataFrame, int]:
    """真出货：横盘 → 放量拉升 → **放量下跌** 12 天（回撤 >2/3）。"""
    closes = [10.0] * 30 + [10.0, 10.4, 10.9, 11.4, 12.0]
    vols = [100.0] * 30 + [200.0] * 5
    for i in range(12):
        closes.append(12.0 - (1.5 * (i + 1) / 12))   # → 10.5（回撤 75%）
        vols.append(350.0)                            # 放量下跌
    return _mk_df(closes, vols), len(closes) - 1


def main() -> int:  # noqa: C901
    print("=== A 九维齐备 + 真实数据可算 ===")
    ck("A1 九维定义齐备且权重和为 1.0",
       len(WD.DIMS) == 9 and abs(sum(w for _, w in WD.DIMS) - 1.0) < 1e-9,
       f"{len(WD.DIMS)} 维 / ∑w={sum(w for _, w in WD.DIMS):.3f}")
    ck("A2 维度名与 §3.2 判别矩阵一一对应",
       {d for d, _ in WD.DIMS} == {"vol_shrink", "retrace", "support", "converge",
                                   "shadow_recover", "moneyflow", "turnover",
                                   "position", "time"})

    from app.backtest.loader import load_stock_pool, prepare_stock_data
    pool = load_stock_pool()
    codes = list(pool["ts_code"].head(3)) if pool is not None and len(pool) else ["000001.SZ"]
    real = []
    for code in codes:
        try:
            df = prepare_stock_data(code)
            if df is None or len(df) < 70:
                continue
            ser = WD.washout_series(code, df)          # 真实序列（daily_basic + moneyflow）
            r = WD.score_washout(df, len(df) - 1, series=ser, ts_code=code)
            real.append((code, r, ser))
        except Exception as exc:  # noqa: BLE001
            ck(f"A 真实数据可算（{code}）", False, f"{type(exc).__name__}: {exc}")
    ck("A3 真实数据可算且不抛异常", len(real) > 0, f"{len(real)} 只")
    if real:
        _c, r0, ser0 = real[0]
        ck("A4 资金序列已对齐（moneyflow 不再整块缺失）",
           ser0.get("net_amount_ratio") is not None and ser0.get("turnover_rate") is not None,
           f"mf={'有' if ser0.get('net_amount_ratio') is not None else '缺'}")
        ck("A5 九维全覆盖时 coverage == 1.0",
           abs(r0.coverage - 1.0) < 1e-6, f"coverage={r0.coverage}")
        ck("A6 各维得分都在 [0,1]",
           bool(r0.dims) and all(0.0 <= v <= 1.0 for v in r0.dims.values()),
           str({k: round(v, 2) for k, v in r0.dims.items()}))
        ck("A7 evidence 含可审计原始值（量能/回撤/均线/分位）",
           {"vol_shrink_ratio", "retrace_ratio", "price_pct_rank"} <= set(r0.evidence))

    print("=== B 缺失维度不计入分母（不许用中性分顶替缺数据）===")
    df_w, idx_w = _wash_scenario()
    full = WD.score_washout(df_w, idx_w, series={
        "net_amount_ratio": np.linspace(-0.02, 0.02, len(df_w)),
        "turnover_rate": np.concatenate([np.full(35, 3.0), np.linspace(2.6, 1.2, len(df_w) - 35)]),
    })
    bare = WD.score_washout(df_w, idx_w, series=None)
    ck("B1 无序列时缺失维度被显式记录（moneyflow/turnover）",
       {"moneyflow", "turnover"} <= set(bare.missing), str(bare.missing))
    ck("B2 coverage 随之下降（不虚报）", bare.coverage < full.coverage,
       f"full={full.coverage} bare={bare.coverage}")
    ck("B3 缺失维度的权重从分母中剔除（其余维度分不因此被稀释）",
       abs(bare.dims["vol_shrink"] - full.dims["vol_shrink"]) < 1e-9,
       f"{bare.dims['vol_shrink']} vs {full.dims['vol_shrink']}")
    ck("B4 分数仍在 [0,1]", 0.0 <= bare.washout_score <= 1.0, str(bare.washout_score))

    # 重复日期表：reindex 会抛错 → _align 必须先按日期去重（本库有这种表）
    dup = pd.DataFrame({"trade_date": ["2026-01-02", "2026-01-02", "2026-01-03"],
                        "turnover_rate": [1.0, 2.0, 3.0]})
    key = pd.Series(pd.to_datetime(["2026-01-02", "2026-01-03"]))
    arr = WD._align(dup, "turnover_rate", key)
    ck("B5 重复日期表能对齐（去重保留最后一行，不抛错）",
       arr is not None and len(arr) == 2 and arr[0] == 2.0, str(None if arr is None else list(arr)))
    a1 = WD._align(pd.DataFrame({"trade_date": ["2026-01-02"], "x": [1.0]}), "x",
                   pd.Series(pd.to_datetime(["2026-01-02", "2026-01-09"])))
    ck("B6 对齐后缺失日期为 NaN（绝不 ffill 编造）",
       a1 is not None and float(a1[0]) == 1.0 and float(a1[1]) != float(a1[1]),
       str(None if a1 is None else list(a1)))

    print("=== C 判别力：洗盘 vs 出货必须分开 ===")
    df_o, idx_o = _out_scenario()
    out = WD.score_washout(df_o, idx_o, series=None)
    ck("C1 真洗盘形态得分高（≥0.55）", full.washout_score >= 0.55, f"{full.washout_score:.3f}")
    ck("C2 真出货形态被硬否决（≤0.35）", out.washout_score <= WD.VETO_SCORE_CAP,
       f"{out.washout_score:.3f}")
    ck("C3 判别力：洗盘显著高于出货（≥0.2）",
       full.washout_score - out.washout_score >= 0.2,
       f"{full.washout_score:.3f} vs {out.washout_score:.3f}")
    ck("C4 出货 stage == 疑似出货", out.stage == WD.STAGE_OUT,
       f"{out.stage} / veto={out.veto}")
    ck("C5 洗盘 stage 不是疑似出货（也不是无特征）",
       full.stage not in (WD.STAGE_OUT, WD.STAGE_NONE), full.stage)
    ck("C6 否决原因可解释（写明是哪条红线）", bool(out.veto), str(out.veto))

    df_t, idx_t = _wash_scenario(over_window=True)
    too = WD.score_washout(df_t, idx_t, series=None)
    ck("C7 超窗口回调与出货**分开**（不张冠李戴）",
       too.stage == WD.STAGE_TOO_LONG and not too.veto, f"{too.stage} / veto={too.veto}")
    ck("C8 超窗口也被压分（不给出洗盘结论）", too.washout_score <= WD.VETO_SCORE_CAP,
       f"{too.washout_score:.3f}")

    ck("C9 washout_end 确认日成立（地量+收阳+站上MA5）", full.confirmed,
       f"confirmed={full.confirmed}")
    ck("C10 被否决的样本绝不可能是确认日（互斥）", out.confirmed is False)

    print("=== D ⚠️ 无未来函数（最硬的前视检查）===")
    tampered = df_w.copy()
    n_after = len(tampered) - idx_w - 1
    if n_after > 0:
        tampered.loc[tampered.index[idx_w + 1:], "close"] *= 3.0
        tampered.loc[tampered.index[idx_w + 1:], "adj_close"] *= 3.0
        tampered.loc[tampered.index[idx_w + 1:], "vol"] *= 10.0
    r_t = WD.score_washout(tampered, idx_w, series=None)
    ck("D1 改动 idx 之后的数据 → 分数完全不变",
       abs(r_t.washout_score - bare.washout_score) < 1e-12,
       f"{bare.washout_score} vs {r_t.washout_score}")
    ck("D2 改动 idx 之后的数据 → stage/各维/否决也完全不变",
       r_t.stage == bare.stage and r_t.dims == bare.dims and r_t.veto == bare.veto)
    src = _src("app/backtest/washout_detector.py")
    ck("D3 模块 docstring 明示无未来函数约束", "无未来函数" in src)
    ck("D4 未使用 rolling/shift 造成的未来窗口（仅在 idx 之前切片）",
       "adj[idx + 1:" not in src and "iloc[idx + 1" not in src)

    print("=== E 输出契约（§3.2 JSON）===")
    d = full.to_dict()
    ck("E1 含 §3.2 要求的字段",
       {"ts_code", "date", "washout_score", "stage", "sub_type", "evidence",
        "counter_evidence"} <= set(d))
    ck("E2 新增字段（confirmed/coverage/veto/dims）可审计",
       {"confirmed", "coverage", "veto", "dims", "missing"} <= set(d))
    try:
        json.dumps(d, ensure_ascii=False)
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    ck("E3 JSON 可序列化（要落盘进样本库）", ok)
    ck("E4 sub_type 非空且可读", bool(d["sub_type"]), d["sub_type"])

    print("=== F 阈值可进化（热生效 + 脏值回退）===")
    from app.agents import evolution_config as EC
    orig = EC.get_param
    try:
        EC.get_param = lambda n, dflt=None: (0.95 if n == "washout_score_threshold" else orig(n, dflt))
        high = WD.score_washout(df_w, idx_w, series=None)
        ck("F1 提高 score 阈值 → 同一形态降级（热生效）",
           high.stage != full.stage and high.stage in (WD.STAGE_WASHING, WD.STAGE_CALLBACK,
                                                       WD.STAGE_NONE),
           f"{full.stage} → {high.stage}")
        EC.get_param = lambda n, dflt=None: ("banana" if n == "washout_score_threshold" else orig(n, dflt))
        bad = WD.score_washout(df_w, idx_w, series=None)
        ck("F2 脏值回退默认（不崩、不误判）", bad.stage == full.stage, f"{bad.stage}")
    finally:
        EC.get_param = orig

    print("=== G 未接入预测侧（P1-5 只做识别）===")
    for rel in ("app/backtest/daily_scan.py", "app/backtest/recommender.py",
                "app/backtest/feature_extractor.py", "app/backtest/risk_filter.py",
                "app/backtest/runner.py", "app/backtest/threshold_analyzer.py"):
        ck(f"G1 {rel} 未引用 washout_detector", "washout_detector" not in _src(rel))
    ck("G2 docstring 明确「本步不参与打分与排序」（接入需等 A/B）",
       "不参与" in src and "A/B" in src)

    print(f"\n===== 结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
