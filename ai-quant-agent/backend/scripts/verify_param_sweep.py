"""验收：纪律参数扫描 + 证据护栏 + 纪律影子实验（plans/23 §14.11 / P1-7b）

守 8 组约束，并**顺手给出真实结论**（回答"追高上限到底该不该收到 1%"）：

  A 扫描数学：成交率单调、手算均值一致
  B Welch t：同分布 p 大、位移大 p 小、样本不足返回 None、接受 list
  C dropped_vs_kept：区间划分正确、样本不足有说明
  D ⚠️ 证据护栏（本组最重要）：全局检验不通过时 **拒绝生成提案**；参数绝不被改动
  E 真实数据分组结论：突破有依据、低吸**反向** → 全局参数不该动
  F 纪律影子：kind 分派、放弃组更差才 applied、**反向一律回滚**、样本不足强制回滚
  G 护栏：扫描模块无 set_param、配置文件未被脚本改动
  H 喂数：replay_one 输出同口径字段、record_buy_verdict 无影子时是 no-op

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_param_sweep.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import types

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import evolution_shadow as ES
from app.backtest import param_sweep as PS

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BACKEND, "data")
CONFIG = os.path.join(DATA, "evolution_config.json")


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _hash(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return ""


def _syn(gaps, t5s) -> list[dict]:
    return [{"ts_code": "X", "date": "20260101", "gap": float(g), "t5": float(t)}
            for g, t in zip(gaps, t5s)]


# ─────────────────────────────── A ───────────────────────────────
def group_a() -> None:
    print("=== A 扫描数学 ===")
    gaps = [0.0, 0.005, 0.02, 0.04, 0.08]
    t5s = [0.01, 0.02, 0.03, 0.04, 0.05]
    s = _syn(gaps, t5s)
    sw = PS.sweep(s, (0.01, 0.02, 0.05, 0.10))
    rows = sw["rows"]
    ck("A1 成交率随阈值单调不减",
       all(rows[i]["keep_rate"] <= rows[i + 1]["keep_rate"] for i in range(len(rows) - 1)),
       f"keep={[r['keep_rate'] for r in rows]}")
    # 阈值 0.02 → 保留 gap ∈ {0, 0.005, 0.02} → t5 {0.01,0.02,0.03} → 均值 0.02
    ck("A2 手算均值一致（阈值 2% → 0.02）",
       abs(rows[1]["mean_t5"] - 0.02) < 1e-9, f"mean={rows[1]['mean_t5']}")
    # 阈值 0.10 → 全部保留 → 均值 = 0.03
    ck("A3 阈值放到最大时等于全体均值",
       abs(rows[-1]["mean_t5"] - float(np.mean(t5s))) < 1e-9,
       f"mean={rows[-1]['mean_t5']}")
    ck("A4 成交率的分母是全部样本（收紧的机会成本可见）",
       rows[0]["keep_rate"] == 0.4 and sw["n_total"] == 5,
       f"keep_rate={rows[0]['keep_rate']} n_total={sw['n_total']}")
    ck("A5 空样本不抛异常", PS.sweep([], (0.01,))["n_total"] == 0)


# ─────────────────────────────── B ───────────────────────────────
def group_b() -> None:
    print("=== B Welch t ===")
    rng = np.random.default_rng(3)
    a = rng.normal(0.0, 0.05, 2000)
    b = rng.normal(0.0, 0.05, 2000)
    r_same = PS.welch_t(a, b)
    ck("B1 同分布 → p 大（不误报）", (r_same["p"] or 0) > 0.05, f"p={r_same['p']}")
    c = rng.normal(-0.05, 0.05, 2000)
    r_diff = PS.welch_t(c, b)
    # ⚠️ 注意 p 可能正好是 0.0（t 很大时正态尾概率下溢）—— 写成 `p or 1` 会把
    # 0.0 当成假值从而把"最显著"误判为"不显著"（本脚本自己踩过这个坑，留作提醒）
    p_d = r_diff["p"]
    ck("B2 明显位移 → p 极小且 t<0",
       p_d is not None and p_d < 1e-6 and (r_diff["t"] or 0) < 0,
       f"t={r_diff['t']} p={p_d}")
    ck("B2b 退化保护：两组各自完全同值 → 判「无法检验」而不是「显著」（防误应用）",
       PS.welch_t([0.02] * 80, [-0.10] * 80)["p"] == 1.0
       and PS.welch_t([0.02] * 80, [-0.10] * 80).get("degenerate") is True,
       "方差≈0 时算出来的 t 会达到 1e16、p=0 → 必须拦住")
    ck("B3 样本不足返回 p=None", PS.welch_t([1.0], [2.0, 3.0])["p"] is None)
    ck("B4 接受 list 输入", PS.welch_t([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])["p"] is not None)
    ck("B5 自由度大于 0 且均值方向正确",
       r_diff["df"] > 0 and (r_diff["mean_a"] or 0) < (r_diff["mean_b"] or 0),
       f"df={r_diff['df']} mean_a={r_diff['mean_a']} mean_b={r_diff['mean_b']}")


# ─────────────────────────────── C ───────────────────────────────
def group_c() -> None:
    print("=== C dropped_vs_kept 分组 ===")
    # gap: 0.005(x3) / 0.02(x2) / 0.05(x2)；new=1% old=3% → 放弃 [0.02,0.03] 的 2 个
    s = _syn([0.005, 0.005, 0.005, 0.02, 0.02, 0.05, 0.05],
             [0.1, 0.1, 0.1, -0.1, -0.1, 0.2, 0.2])
    t = PS.dropped_vs_kept(s, 0.01, 0.03)
    ck("C1 被放弃组只取 gap ∈ (new, old]", t["dropped_n"] == 2,
       f"dropped_n={t['dropped_n']}")
    ck("C2 保留组取 gap ≤ new（1% 以上但超 3% 的不算被放弃）", t["kept_n"] == 3,
       f"kept_n={t['kept_n']}")
    ck("C3 均值计算正确", abs(t["dropped_mean"] + 0.1) < 1e-9 and abs(t["kept_mean"] - 0.1) < 1e-9,
       f"dropped={t['dropped_mean']} kept={t['kept_mean']}")
    ck("C4 样本不足时不硬给结论", "样本不足" in PS.dropped_vs_kept(s, 0.01, 0.03)["conclusion"]
       or PS.dropped_vs_kept(s, 0.01, 0.03)["ok"] is False,
       f"n={t['dropped_n']}/{t['kept_n']} min={PS.MIN_GROUP_N}")


# ─────────────────────────────── D ───────────────────────────────
def group_d() -> None:
    print("=== D 证据护栏（最重要）===")
    with open(os.path.join(BACKEND, "app/backtest/param_sweep.py"), encoding="utf-8") as fh:
        src = fh.read()
    ck("D1 扫描/提案模块里没有任何 set_param 调用", "set_param(" not in src,
       "提案 ≠ 生效，写入路径必须由 confirm_proposal/影子结算独占")

    before = PS.__dict__ and None
    cur_before = _param_now()
    res = PS.propose()          # write 默认 False；先看证据护栏
    ck("D2 全局检验不通过时**拒绝生成提案**",
       res.get("ok") is False and res.get("stage") == "evidence_guard",
       f"stage={res.get('stage')} reason={str(res.get('reason'))[:90]}")
    ck("D3 拒绝理由里带上了统计量（不是黑箱拒绝）",
       "p=" in str(res.get("reason")), str(res.get("reason"))[:120])
    ev = res.get("evidence") or {}
    ck("D4 拒绝同时也给出分组结论（供人判断改哪里）",
       ev.get("by_entry_type", {}).get("significant_entries") == ["breakout"],
       f"显著分组={ev.get('by_entry_type', {}).get('significant_entries')}")
    ck("D5 参数值在 probe 前后完全不变", _param_now() == cur_before,
       f"{cur_before} -> {_param_now()}")

    # 合成"有依据"的场景 → 应能走到 dry_run（不写库）
    rng = np.random.default_rng(11)
    good = _syn(rng.uniform(0.0, 0.01, 3000), rng.normal(0.01, 0.05, 3000))
    good += _syn(rng.uniform(0.011, 0.03, 1200), rng.normal(-0.06, 0.05, 1200))
    ok_res = PS.propose(write=False, samples=good)
    ck("D6 合成「有依据」数据时能通过证据护栏",
       ok_res.get("stage") in ("dry_run", "constitution"),
       f"stage={ok_res.get('stage')} reason={str(ok_res.get('reason'))[:80]}")
    rec = ok_res.get("record") or {}
    ck("D7 提案记录结构正确（param/new/old/status=pending + 证据）",
       bool(rec.get("param") == PS.TARGET_PARAM and rec.get("status") == "pending"
            and rec.get("evidence") and rec.get("test")),
       f"param={rec.get('param')} status={rec.get('status')}")
    ck("D8 dry_run 不写库（没有 id 字段）", "id" not in rec, f"keys={sorted(rec)[:6]}")
    ck("D9 有依据场景下 old 取自当前配置值而非硬编码",
       abs(float(rec.get("old") or 0) - float(_param_now() or 0)) < 1e-9,
       f"old={rec.get('old')} current={_param_now()}")


def _param_now():
    from app.agents import evolution_config
    return evolution_config.get_param(PS.TARGET_PARAM)


# ─────────────────────────────── E ───────────────────────────────
def group_e() -> None:
    print("=== E 真实数据：分组结论 ===")
    samples = {t: PS.load_samples(only_triggered=t) for t in PS.ENTRY_TYPES}
    n_all = len(PS.load_samples())
    ck("E1 样本量足够（>50000）", n_all > 50000, f"n={n_all}")
    by = PS.analyze_by_entry_type(samples=samples)
    rows = {r["entry"]: r for r in by["rows"]}
    bk = rows.get("breakout", {})
    dp = rows.get("dip", {})
    print("    分组结果：" + " | ".join(
        f"{r['entry']} n={r['n']} 放弃={r.get('dropped_mean')} 保留={r.get('kept_mean')} "
        f"p={r.get('p')} {r.get('conclusion')}" for r in by["rows"]))
    ck("E2 突破买入：被放弃的高开机会显著更差（收紧有依据）",
       bk.get("conclusion") == "收紧有依据" and (bk.get("p") or 1) < 0.01,
       f"p={bk.get('p')} dropped={bk.get('dropped_mean')} kept={bk.get('kept_mean')}")
    ck("E3 低吸：方向**相反**（被放弃的反而更好）",
       (dp.get("dropped_mean") or 0) > (dp.get("kept_mean") or 0),
       f"dropped={dp.get('dropped_mean')} kept={dp.get('kept_mean')}")
    ck("E4 方向不一致 → 全局参数不该动（direction_consistent=False）",
       by["direction_consistent"] is False,
       "同一阈值在不同入场类型上方向相反 = 该参数注入位置错了")
    g = PS.dropped_vs_kept(PS.load_samples(), PS.PROPOSED_NEW, PS.CURRENT_DEFAULT)
    ck("E5 全局口径下收紧**无统计依据**（推翻 §14.8 的直接外推）",
       g["conclusion"] == "收紧无统计依据" and (g["p"] or 0) > 0.05,
       f"p={g['p']} 放弃组={g['dropped_mean']} 保留组={g['kept_mean']} "
       f"（代价：放弃 {g['dropped_rate']*100:.1f}% 的成交）")
    sw = PS.sweep(PS.load_samples())
    ck("E6 收紧到 1% 的收益改善极小（<0.05pct）",
       abs(next(r for r in sw["rows"] if r["chase"] == 0.01)["mean_t5"]
           - next(r for r in sw["rows"] if r["chase"] == 0.03)["mean_t5"]) < 0.0005,
       "改善幅度远小于放弃 8% 成交的机会成本")


# ─────────────────────────────── F ───────────────────────────────
def group_f() -> None:
    print("=== F 纪律影子（kind 分派）===")
    tmp = tempfile.mkdtemp(prefix="shadow_verify_")
    calls: list = []

    class _Cfg:
        CONFIG_FILE = os.path.join(tmp, "cfg.json")
        @staticmethod
        def get(name, default=None):
            return 0.03 if name == PS.TARGET_PARAM else default
        @staticmethod
        def get_param(name):
            return 0.03 if name == PS.TARGET_PARAM else None
        @staticmethod
        def set_param(name, val):
            calls.append((name, val))
            return True
        @staticmethod
        def params_table():
            return {}

    orig_file, orig_cfg = ES.SHADOW_FILE, ES.evolution_config
    orig_baseline = ES.record_baseline
    stub_mods = {}
    try:
        ES.SHADOW_FILE = os.path.join(tmp, "shadow.json")
        ES.evolution_config = _Cfg  # type: ignore[assignment]
        ES.record_baseline = lambda *a, **k: None  # type: ignore[assignment]
        for name in ("evolution_ledger", "evolution_guard", "self_improver"):
            m = types.ModuleType(f"app.agents.{name}")
            m.open_entry = lambda *a, **k: None       # type: ignore[attr-defined]
            m.close_entry = lambda *a, **k: None      # type: ignore[attr-defined]
            m.record_apply = lambda *a, **k: None     # type: ignore[attr-defined]
            m._backup_versions = lambda *a, **k: ""   # type: ignore[attr-defined]
            m._audit = lambda *a, **k: None           # type: ignore[attr-defined]
            stub_mods[f"app.agents.{name}"] = sys.modules.get(f"app.agents.{name}")
            sys.modules[f"app.agents.{name}"] = m

        # F1 kind 落库 + 分派
        r = ES.start_shadow(PS.TARGET_PARAM, 0.03, 0.01, kind="discipline")
        ck("F1 start_shadow 支持 kind=discipline",
           bool(r.get("ok")) and ES.shadow_status()[PS.TARGET_PARAM]["kind"] == "discipline",
           f"{r}")

        # F2 无数据 → running（不结算）
        s0 = ES.settle_shadow(PS.TARGET_PARAM)
        ck("F2 样本不足时不结算（保持 running）",
           s0.get("status") == "running" and s0.get("ok") is False, f"{s0}")

        # F3 放弃组明显更差 → applied 且真的 set_param（加扰动，避免退化方差）
        jit = np.random.default_rng(5).normal(0, 0.01, 80)
        for i in range(80):
            ES.record_buy_verdict(PS.TARGET_PARAM, "A", "20260101", 0.005, 0.02 + float(jit[i]))
        for i in range(80):
            ES.record_buy_verdict(PS.TARGET_PARAM, "B", "20260101", 0.02, -0.10 + float(jit[i]))
        s1 = ES.settle_shadow(PS.TARGET_PARAM)
        p1 = s1.get("p")
        ck("F3 放弃组显著更差 → applied 且 set_param 被调用",
           bool(s1.get("status") == "applied" and len(calls) == 1
                and calls[-1] == (PS.TARGET_PARAM, 0.01)
                and p1 is not None and p1 < 0.05),
           f"status={s1.get('status')} p={p1} calls={calls}")
    finally:
        for k, v in stub_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        ES.SHADOW_FILE, ES.evolution_config = orig_file, orig_cfg
        ES.record_baseline = orig_baseline  # type: ignore[assignment]

    # F4 反向（被放弃的更好）→ rolled_back，绝不生效（不做反向投机）
    tmp2 = tempfile.mkdtemp(prefix="shadow_verify2_")
    calls2: list = []

    class _Cfg2(_Cfg):
        CONFIG_FILE = os.path.join(tmp2, "cfg.json")
        @staticmethod
        def set_param(name, val):
            calls2.append((name, val))
            return True

    orig_file = ES.SHADOW_FILE
    orig_cfg = ES.evolution_config
    try:
        ES.SHADOW_FILE = os.path.join(tmp2, "shadow.json")
        ES.evolution_config = _Cfg2  # type: ignore[assignment]
        ES.start_shadow(PS.TARGET_PARAM, 0.03, 0.01, kind="discipline")
        jit2 = np.random.default_rng(6).normal(0, 0.01, 80)
        for i in range(80):
            ES.record_buy_verdict(PS.TARGET_PARAM, "A", "20260101", 0.005, -0.10 + float(jit2[i]))
        for i in range(80):
            ES.record_buy_verdict(PS.TARGET_PARAM, "B", "20260101", 0.02, 0.05 + float(jit2[i]))
        s2 = ES.settle_shadow(PS.TARGET_PARAM)
        ck("F4 被放弃的更好 → 回滚，且**没有** set_param（不做反向投机）",
           s2.get("status") == "rolled_back" and not calls2,
           f"status={s2.get('status')} calls={calls2}")
        ck("F5 policy 类影子仍走原评估器（kind 默认 policy，不受影响）",
           bool(ES.start_shadow("__policy_probe__", 1, 2).get("ok")
                and ES.shadow_status()["__policy_probe__"].get("kind") == "policy"))
    finally:
        ES.SHADOW_FILE, ES.evolution_config = orig_file, orig_cfg

    # F6 record_buy_verdict 无影子时 no-op（不能因为没实验就报错/写文件）
    ES.SHADOW_FILE = os.path.join(tmp2, "shadow.json")  # 里面没有该参数
    try:
        ES.record_buy_verdict("__nonexistent__", "A", "2026", 0.01, 0.0)
        ck("F6 无影子时 record_buy_verdict 是 no-op", True)
    except Exception as exc:  # noqa: BLE001
        ck("F6 无影子时 record_buy_verdict 是 no-op", False, str(exc))
    finally:
        ES.SHADOW_FILE = orig_file
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(tmp2, ignore_errors=True)


# ─────────────────────────────── G ───────────────────────────────
def group_g() -> None:
    print("=== G 护栏 ===")
    ck("G1 参数当前值仍是 0.03（本脚本从未改过它）",
       abs(float(_param_now() or 0) - 0.03) < 1e-12, f"current={_param_now()}")
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            cfg = json.load(fh)
        v = cfg["params"][PS.TARGET_PARAM]["current"]
        ck("G2 配置文件里 plan_max_chase_pct 未被改动", abs(float(v) - 0.03) < 1e-12, f"json={v}")
        ck("G3 该参数仍是 Tier1 + shadow_eligible（影子通道可用）",
           cfg["params"][PS.TARGET_PARAM]["tier"] == 1
           and cfg["params"][PS.TARGET_PARAM]["shadow_eligible"] is True)
    except Exception as exc:  # noqa: BLE001
        ck("G2 配置可读", False, str(exc))


# ─────────────────────────────── H ───────────────────────────────
def group_h() -> None:
    print("=== H 喂数（plan_tracker → 纪律影子）===")
    from app.backtest import plan_tracker as PT
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04",
                            "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"])
    df = pd.DataFrame({
        "trade_date": dates,
        "open": [10.0, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0, 11.1],
        "high": [10.2, 10.6, 10.7, 10.8, 10.9, 11.0, 11.1, 11.2],
        "low": [9.9, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0],
        "close": [10.0, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0, 11.2],
    })
    rec = {"date": "20260101", "ts_code": "T", "t0_close": 10.0,
           "plan": {"buy_low": 9.8, "buy_high": 10.6, "stop_loss": 9.0,
                    "take_profit": [{"price": 11.5}], "hold_days": 5}}
    out = PT.replay_one(rec, df)
    ck("H1 回放输出带上同口径字段 t1_gap / t5_from_open",
       out is not None and out.get("t1_gap") is not None and out.get("t5_from_open") is not None,
       f"gap={out and out.get('t1_gap')} t5={out and out.get('t5_from_open')}")
    ck("H2 t1_gap = D+1 开盘 / D 收盘 − 1（10.5/10.0−1 = 5%）",
       out is not None and abs(out["t1_gap"] - 0.05) < 1e-9, f"gap={out and out.get('t1_gap')}")
    ck("H3 既有回放语义未被破坏（filled/exit_reason 仍在）",
       out is not None and "filled" in out and "exit_reason" in out)
    with open(os.path.join(BACKEND, "app/backtest/plan_tracker.py"), encoding="utf-8") as fh:
        src = fh.read()
    ck("H4 replay_pending 已接入喂数（record_buy_verdict）",
       "record_buy_verdict" in src and "replay_pending" in src)


def main() -> int:
    print("=" * 72)
    print("验收：纪律参数扫描 + 证据护栏 + 纪律影子（P1-7b）")
    print("=" * 72)
    h_before = _hash(CONFIG)
    group_a()
    group_b()
    group_c()
    group_d()
    group_e()
    group_f()
    group_g()
    group_h()
    print("=" * 72)
    ck("Z1 脚本运行前后 evolution_config.json 内容未变",
       h_before == _hash(CONFIG), f"{h_before} -> {_hash(CONFIG)}")
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
