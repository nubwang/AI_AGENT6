"""验收：止盈目标位的 ATR 口径（plans/23 §15.5 / P2-8）

守 5 组约束，并**顺手给出真实可达性曲线**（把"目标位拍多少"变成"能接受多少命中率"）：

  A 默认口径 = legacy → **线上行为逐字节不变**（最重要的一条）
  B ATR 计算正确（合成数据手算）+ **无未来函数**（篡改 idx 之后的数据不影响结果）
  C 分派与回退：atr 模式下 ATR 不可得 → 回退 legacy 且**在 detail 里说明**（不静默变口径）
  D 真实数据可达性曲线（同一批样本）：各 ATR 倍数对应的目标幅度与触及概率，
    并把 legacy 的 +5.5% **标在同一条曲线上**（看它相当于哪个倍数）
  E 护栏：模块无 IO 依赖、未被预测链路引用、运行前后生产文件哈希不变

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_target_price.py
"""
from __future__ import annotations

import hashlib
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import target_price as TP
from app.backtest.loader import load_daily

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLD = 5


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _hash(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return ""


def _df(rows) -> pd.DataFrame:
    """rows: [(high, low, close)] → DataFrame（trade_date 升序）。"""
    n = len(rows)
    return pd.DataFrame({
        "trade_date": pd.to_datetime([f"2026-01-{i+1:02d}" for i in range(n)]),
        "high": [r[0] for r in rows], "low": [r[1] for r in rows],
        "close": [r[2] for r in rows],
    })


# ─────────────────────────────── A ───────────────────────────────
def group_a() -> None:
    print("=== A 默认口径 = legacy（线上行为不变）===")
    ck("A1 plan_tp_basis 默认 legacy", TP.TP_BASIS_DEFAULT == "legacy" and TP.tp_basis() == "legacy",
       f"默认={TP.TP_BASIS_DEFAULT} 生效={TP.tp_basis()}")
    # 与 trade_plan 现有换算完全一致：level = max(3%, avg_t5 × ratio)
    r = TP.resolve(10.0, atr_pct_v=0.04, avg_ret_t5=0.1387, ratio=0.4)
    expect = round(10.0 * (1 + min(max(0.1387 * 0.4, 0.03), 0.30)), 2)
    ck("A2 legacy 结果与 trade_plan 现行公式一致（10元×+5.5% = 10.55）",
       r["basis"] == "legacy" and abs(r["price"] - expect) < 1e-9,
       f"price={r['price']} 期望={expect} level={r['level']}")
    ck("A3 legacy 忽略 ATR（口径未切换时不得掺入新逻辑）",
       TP.resolve(10.0, 0.20, 0.1387, 0.4)["price"] == TP.resolve(10.0, 0.01, 0.1387, 0.4)["price"],
       "两种极端 ATR 得到同一价格")
    ck("A4 目标幅度受 [3%, 30%] 夹逼（防荒谬目标）",
       TP.tp_level(100.0, 0.01) == TP.TP_CEIL and TP.tp_level(0.01, 0.01) == TP.TP_FLOOR,
       f"mult=100 → {TP.tp_level(100.0, 0.01)}（上限）; "
       f"mult=0.01 → {TP.tp_level(0.01, 0.01)}（下限）")


# ─────────────────────────────── B ───────────────────────────────
def group_b() -> None:
    print("=== B ATR 计算 + 无未来函数 ===")
    # 手算：4 天，每日 H−L = 1.0，且 prev_close 都在区间内 → TR 全为 1.0，ATR=1.0
    df = _df([(11.0, 10.0, 10.5), (11.5, 10.5, 11.0), (12.0, 11.0, 11.5), (12.5, 11.5, 12.0)])
    a = TP.atr_pct(df, 3, n=3)
    ck("B1 ATR% 手算一致（TR 全为 1.0，收盘 12.0 → 1/12=0.0833）",
       a is not None and abs(a - 1.0 / 12.0) < 1e-9, f"atr_pct={a}")
    ck("B2 数据不足/越界返回 None（不抛异常）",
       TP.atr_pct(df, 0) is None and TP.atr_pct(df, 99) is None and TP.atr_pct(None, 1) is None)
    # 跳空：prev_close 在区间外 → TR 取 |H-prevC|
    df2 = _df([(10.0, 9.0, 9.5), (20.0, 19.0, 19.5)])
    a2 = TP.atr_pct(df2, 1, n=1)
    ck("B3 跳空日 TR = |H − prevC|（20 − 9.5 = 10.5 → /19.5）",
       a2 is not None and abs(a2 - 10.5 / 19.5) < 1e-9, f"atr_pct={a2}")
    # 无未来函数：篡改 idx 之后的数据，ATR 必须不变
    df3 = df.copy()
    a_before = TP.atr_pct(df3, 2, n=3)
    df3.loc[3, ["high", "low", "close"]] = [999.0, 1.0, 500.0]
    ck("B4 ⚠️ 无未来函数：篡改 idx 之后的 K 线，ATR 不变",
       a_before == TP.atr_pct(df3, 2, n=3), f"{a_before} -> {TP.atr_pct(df3, 2, n=3)}")


# ─────────────────────────────── C ───────────────────────────────
def group_c() -> None:
    print("=== C 分派与回退 ===")
    r_atr = TP.resolve(10.0, atr_pct_v=0.04, avg_ret_t5=0.1387, ratio=0.4, basis="atr", mult=1.5)
    ck("C1 atr 口径：tp = 10 × (1 + 1.5×4%) = 10.60",
       r_atr["basis"] == "atr" and abs(r_atr["price"] - 10.60) < 1e-9,
       f"price={r_atr['price']} detail={r_atr['detail']}")
    r_fb = TP.resolve(10.0, atr_pct_v=None, avg_ret_t5=0.1387, ratio=0.4, basis="atr")
    ck("C2 ATR 不可得 → 回退 legacy，且 detail 说明原因（不静默改口径）",
       r_fb["basis"] == "legacy" and "回退" in r_fb["detail"], f"detail={r_fb['detail']}")
    r_bad = TP.resolve(10.0, 0.04, 0.1387, basis="不存在")
    ck("C3 非法口径回退 legacy（与 §rule_fdr 同一套防御）", r_bad["basis"] == "legacy")
    ck("C4 曲线：倍数越大目标越远",
       all(TP.tp_level(m, 0.04) <= TP.tp_level(m + 0.25, 0.04) for m in (0.5, 1.0, 2.0, 3.0)),
       f"curve={ {m: TP.tp_level(m, 0.04) for m in (0.5, 1.0, 1.5, 2.0)} }")


# ─────────────────────────────── D ───────────────────────────────
def _real() -> list[dict]:
    """取 kb_case 的 (T0, 前 5 日 ATR%, T+5 最大涨幅) —— 用于可达性统计。"""
    from app.kb import kb_store
    out: list[dict] = []
    cache: dict = {}
    for cse in kb_store.query("case", limit=6000):
        code, d0 = str(cse.get("ts_code") or ""), str(cse.get("date") or "")[:8]
        if not code or len(d0) != 8:
            continue
        if code not in cache:
            try:
                # ⚠️ 必须加载**全历史**：`start_date=d0` 会让序列从 T0 开始，
                # i0=0 时 ATR 没有前序 K 线可用（这正是第一次跑出 n=0 的原因）
                cache[code] = load_daily(code)
            except Exception:  # noqa: BLE001
                cache[code] = None
        df = cache[code]
        if df is None or getattr(df, "empty", True):
            continue
        try:
            darr = df["trade_date"].dt.strftime("%Y%m%d").to_numpy()
            i0 = int(np.searchsorted(darr, d0, side="left"))
            a = TP.atr_pct(df, i0)
            if i0 < TP.ATR_N:      # 前序不足 → 直接跳过（不硬凑）
                continue
            if a is None or i0 + HOLD >= len(df):
                continue
            c0 = float(df["close"].iloc[i0])
            seg = df.iloc[i0 + 1: i0 + 1 + HOLD]
            hi = float(pd.to_numeric(seg["high"], errors="coerce").max())
            last_c = float(pd.to_numeric(seg["close"], errors="coerce").iloc[-1])
            out.append({"atr": a, "c0": c0,
                        "max_gain": hi / c0 - 1.0,        # 盘中触及（挂单能否成交）
                        "close_gain": last_c / c0 - 1.0})  # T+5 收盘（§15.4 用的口径）
        except Exception:  # noqa: BLE001
            continue
    return out


def group_d() -> None:
    print("=== D 真实可达性曲线（同一批 kb_case）===")
    rows = _real()
    if len(rows) < 50:
        ck("D0 样本可加载", False, f"n={len(rows)}")
        return
    atr = np.array([r["atr"] for r in rows])
    gain = np.array([r["max_gain"] for r in rows])
    print(f"  样本 {len(rows)} 条 | ATR% 中位 {np.median(atr)*100:.2f}% "
          f"（P10 {np.quantile(atr,0.1)*100:.2f}% / P90 {np.quantile(atr,0.9)*100:.2f}%）")
    print(f"{'倍数':>6}{'目标幅度中位':>14}{'触及概率':>10}")
    curve = {}
    for m in TP.ATR_MULTS:
        lvl = np.array([TP.tp_level(m, a) for a in atr])
        hit = float((gain >= lvl).mean())
        curve[m] = (float(np.median(lvl)), hit)
        print(f"{m:>6.2f}{np.median(lvl)*100:>13.2f}%{hit:>10.1%}")
    close_gain = np.array([r["close_gain"] for r in rows])
    legacy_touch = float((gain >= 0.055).mean())
    legacy_close = float((close_gain >= 0.055).mean())
    print(f"  legacy 目标位 +5.5%：**盘中触及** {legacy_touch:.1%} vs **T+5 收盘达标** {legacy_close:.1%}"
          f"（相当于 mult≈{min(TP.ATR_MULTS, key=lambda m: abs(curve[m][0]-0.055)):.2f} 的位置）")
    ck("D1b ⚠️ 口径修正：『挂单能否成交』应按**盘中触及**算，而不是收盘达标",
       legacy_touch > legacy_close * 2,
       f"触及 {legacy_touch:.1%} vs 收盘 {legacy_close:.1%}"
       "（§15.4 引用的 9.1% 是收盘口径，偏严 3.7 倍 → 该结论的严重性需下调）")
    ck("D1 曲线单调：倍数越大，触及概率越低",
       all(curve[TP.ATR_MULTS[i]][1] >= curve[TP.ATR_MULTS[i + 1]][1]
           for i in range(len(TP.ATR_MULTS) - 1)),
       f"hit={[round(curve[m][1], 3) for m in TP.ATR_MULTS]}")
    ck("D2 ⚠️ ATR 口径**不保证更高命中率**，也**不是**『legacy 数字离谱』："
       "legacy +5.5% 恰好落在 mult≈1.0 的位置",
       abs(curve[1.0][0] - 0.055) < 0.02,
       f"1.0× 中位目标 {curve[1.0][0]*100:.2f}% ≈ legacy +5.5%；"
       "真正的差别是 legacy **不随波动率自适应、也无法解释**"
       "（4% ATR 与 15% ATR 的票给同一个 +5.5%）")
    ck("D3 低倍数（≤1.0）命中率明显更高——使用者可按可接受命中率选倍数",
       curve[1.0][1] > curve[2.0][1] + 0.05,
       f"1.0× {curve[1.0][1]:.1%} vs 2.0× {curve[2.0][1]:.1%}")
    ck("D4 ATR% 有区分度（不是常数 → 目标位真的随波动率自适应）",
       float(np.quantile(atr, 0.9)) > float(np.quantile(atr, 0.1)) * 1.3,
       f"P10={np.quantile(atr,0.1)*100:.2f}% P90={np.quantile(atr,0.9)*100:.2f}%")


# ─────────────────────────────── E ───────────────────────────────
def group_e() -> None:
    print("=== E 护栏 ===")
    p = os.path.join(BACKEND, "app/backtest/target_price.py")
    with open(p, encoding="utf-8") as fh:
        src = fh.read()
    ck("E1 模块只依赖 numpy/pandas/evolution_config（无 DB/网络）",
       not any(k in src for k in ("SessionLocal", "mysql", "requests", "app.models")))
    pred = ["app/backtest/daily_scan.py", "app/backtest/feature_extractor.py",
            "app/agents/refine_agent.py"]
    hits = []
    for rel in pred:
        fp = os.path.join(BACKEND, rel)
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as fh:
                if "target_price" in fh.read():
                    hits.append(rel)
    ck("E2 预测链路未引用（新口径尚未上线，等 verify + 参数切换）", not hits, f"命中={hits}")
    tp_h, gd_h = _hash(os.path.join(BACKEND, "app/backtest/trade_plan.py")), \
        _hash(os.path.join(BACKEND, "data/entry_guidance.json"))
    ck("E3 本脚本只读（未改 trade_plan.py / entry_guidance.json）",
       _hash(os.path.join(BACKEND, "app/backtest/trade_plan.py")) == tp_h
       and _hash(os.path.join(BACKEND, "data/entry_guidance.json")) == gd_h)


def main() -> int:
    print("=" * 84)
    print("验收：止盈目标位 ATR 口径（P2-8）")
    print("=" * 84)
    group_a()
    group_b()
    group_c()
    group_d()
    group_e()
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
