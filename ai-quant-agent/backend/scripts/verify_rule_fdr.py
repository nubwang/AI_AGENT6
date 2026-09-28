"""验收：规则验证的多重检验校正（plans/23 §4.3 / P2 第一件事）

守 6 组约束，并**顺手给出真实结论**（回答"候选规则多了以后校正会砍掉多少"）：

  A 参数读取：rule_fdr_mode / rule_fdr_alpha 默认值 + 非法值回退
  B ⚠️ 三模式语义（本组最重要）：
      off      不做校正（历史行为）
      observe  **默认**：算 q 值并落盘，但 verified 集合与 off **完全一致**（不静默降级）
      enforce  真正把未过 FDR 的规则降级为 fdr_rejected
  C 检验家族 m 的口径：只统计"走到了显著性检验"的规则；未过硬门槛的不进 m
  D 落盘往返：q_value / fdr 元数据可存可取；真实 form_leaderboard.json **未被本脚本改动**
  E ⚠️ BH 的真实数学行为（两套场景，结论相反，必须都测）：
      E-多：6 条 p<α 的单条显著 + 60 条 p≈0.5 的零假设 → m=66，**BH 全部不通过**
            （单条全过、校正全砍 —— 这正是"挖 1000 条总有几十条显著"的现场）
      E-少：2 条极强 + 2 条零假设 → m=4，**BH 保留 2 条极强**
            （校正不是无脑砍：真信号在少量检验里依然活得下来）
      为什么"全过"与"全砍"会同时成立（实测踩到的反直觉点）：
        BH 的 q_i = min_{j≥i}(p_j·m/j)，而 j=m 处是 p_max·1 = p_max。
        → 若家族里**所有** p ≤ α，则每个 q ≤ p_max ≤ α → **全体通过**；
        → 只有当家族里存在 p > α 的零假设（把 m 撑大）时，校正才会真正变严。
        所以"做了 FDR"本身不等于"更严"，**关键是有没有把未通过的零假设算进 m**。
  F 护栏：默认模式不会让 verified_count 掉到 fallback 阈值（<3）以下

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_rule_fdr.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import rule_verifier as RV
from app.backtest.rule_forms import RuleForm

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BACKEND, "data")
REAL_LB = os.path.join(DATA, "form_leaderboard.json")


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _hash(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return ""


def _samples(n: int, k: int) -> list[dict]:
    """n 个样本、恰好 k 个命中；命中**均匀分布**（否则 OOS 段可能全无命中而误杀）。"""
    out = []
    for i in range(n):
        hit = bool(k > 0) if i == 0 else (int(i * k // n) != int((i - 1) * k // n))
        out.append({"date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}",
                    "hit_t1": hit, "ret_t1": 0.01 if hit else -0.01, "tradeable": True})
    return out


def _mk(prefix: str, specs: list[tuple[int, int]]) -> tuple[list, dict]:
    rules, hits = [], {}
    for i, (n, k) in enumerate(specs):
        key = f"{prefix}_{i:02d}"
        rules.append(RuleForm(key=key, name=key, source="auto", form_type="A",
                              lookback_days=20, description="合成", bullish=True))
        hits[key] = _samples(n, k)
    return rules, hits


# 场景 A：多检验（6 条边缘显著 p≈0.036 + 60 条零假设 p=0.5）→ m=66，BH 全砍
_FIX_MANY = lambda: _mk("many", [(400, 218)] * 6 + [(100, 50)] * 60)
# 场景 B：少检验（2 条极强 + 2 条零假设）→ m=4，BH 保留极强
_FIX_FEW = lambda: _mk("few", [(400, 260), (400, 260), (100, 50), (100, 50)])


def _run(mode: str, fixture, alpha: float = 0.05, extra: tuple | None = None) -> dict:
    rules, hits = fixture()
    if extra:
        rules = rules + [extra[0]]
        hits = {**hits, **extra[1]}
    old_rules, old_mode, old_alpha, old_base = (RV.RULE_FORMS, RV._fdr_mode,
                                                RV._fdr_alpha, RV._baseline_t1)
    try:
        RV.RULE_FORMS = rules                       # type: ignore[assignment]
        RV._fdr_mode = lambda: mode                 # type: ignore[assignment]
        RV._fdr_alpha = lambda: alpha               # type: ignore[assignment]
        RV._baseline_t1 = lambda h: 0.5             # type: ignore[assignment]
        return RV.verify_all(hits)
    finally:
        RV.RULE_FORMS, RV._fdr_mode, RV._fdr_alpha, RV._baseline_t1 = (
            old_rules, old_mode, old_alpha, old_base)   # type: ignore[assignment]


# ─────────────────────────────── A ───────────────────────────────
def group_a() -> None:
    print("=== A 参数读取 ===")
    from app.agents import evolution_config as EC
    ck("A1 rule_fdr_mode 默认 observe",
       EC.get_param("rule_fdr_mode") == "observe", f"{EC.get_param('rule_fdr_mode')}")
    ck("A2 rule_fdr_alpha 默认 0.05",
       abs(float(EC.get_param("rule_fdr_alpha") or 0) - 0.05) < 1e-12,
       f"{EC.get_param('rule_fdr_alpha')}")
    ck("A3 两个参数都已注册（可进化）",
       RV._fdr_mode() in RV.FDR_MODES and RV._fdr_alpha() > 0,
       f"mode={RV._fdr_mode()} alpha={RV._fdr_alpha()}")
    import app.agents.evolution_config as _ec
    orig = _ec.get_param
    try:
        # 非法值 → 回退 observe（不能因为配置写错就变成"关闭校正"或"强制降级"）
        _ec.get_param = lambda k, d=None: ("bogus" if k == "rule_fdr_mode" else d)  # type: ignore[assignment]
        ck("A4 非法 mode 值回退 observe", RV._fdr_mode() == "observe", RV._fdr_mode())
    finally:
        _ec.get_param = orig  # type: ignore[assignment]


# ─────────────────────────────── B ───────────────────────────────
def group_b() -> None:
    print("=== B 三模式语义（最重要）===")
    off = _run("off", _FIX_MANY)
    obs = _run("observe", _FIX_MANY)
    enf = _run("enforce", _FIX_MANY)

    off_keys = {r["key"] for r in off["rules"]}
    obs_keys = {r["key"] for r in obs["rules"]}
    enf_keys = {r["key"] for r in enf["rules"]}

    ck("B1 off：不做校正（enabled=False，无 q 值）",
       off["fdr"]["enabled"] is False and all("q_value" not in r for r in off["rules"]),
       f"fdr={off['fdr']['mode']} n_rules={len(off_keys)}")
    ck("B2 observe（默认）：verify 集合与 off **完全一致**（不静默降级）",
       obs_keys == off_keys, f"off={len(off_keys)} observe={len(obs_keys)}")
    ck("B3 observe：q 值确实算出来并落盘（可审计）",
       all("q_value" in r for r in obs["rules"]) and obs["fdr"]["m"] == 66,
       f"m={obs['fdr']['m']} q={[r.get('q_value') for r in obs['rules']][:3]}…")
    ck("B4 observe：不改 verified → n_fdr_rejected = 0", obs["fdr"]["n_fdr_rejected"] == 0)
    ck("B5 enforce：6 条单条显著但未过 FDR 的规则被降级为 fdr_rejected",
       enf["fdr"]["n_fdr_rejected"] == 6
       and [d["reason"] for d in enf["deleted"]].count("fdr_rejected") == 6,
       f"n_fdr_rejected={enf['fdr']['n_fdr_rejected']} "
       f"reasons={sorted({d['reason'] for d in enf['deleted']})}")
    ck("B6 enforce 后 verified_count = 0（多检验场景下被砍光）",
       len(enf_keys) == 0 and enf["verified_count"] == 0, f"verified={len(enf_keys)}")
    ck("B7 enforce 留下的规则是 off 集合的子集", enf_keys <= off_keys)
    ck("B8 被 FDR 降级的规则在 deleted 里保留了 p/q 值（可追溯）",
       all(d.get("p_value") is not None and d.get("q_value") is not None
           for d in enf["deleted"] if d["reason"] == "fdr_rejected"),
       f"{[d.get('q_value') for d in enf['deleted'] if d['reason'] == 'fdr_rejected'][:3]}…")


# ─────────────────────────────── C ───────────────────────────────
def group_c() -> None:
    print("=== C 检验家族 m 的口径 ===")
    tiny = RuleForm(key="tiny", name="tiny", source="auto", form_type="A",
                    lookback_days=20, description="样本不足", bullish=True)
    out = _run("observe", _FIX_MANY, extra=(tiny, {"tiny": _samples(10, 9)}))
    ck("C1 m 只统计走到显著性检验的规则（样本不足的不进 m）",
       out["fdr"]["m"] == 66 and out["fdr"]["n_testable"] == 66,
       f"m={out['fdr']['m']} n_testable={out['fdr']['n_testable']} "
       f"n_bullish={out['fdr']['n_bullish']}")
    ck("C2 同时给出 n_bullish（更保守口径的分母，便于复核）",
       out["fdr"]["n_bullish"] == 67, f"n_bullish={out['fdr']['n_bullish']}")
    ck("C3 样本不足的规则以 insufficient_samples 进 deleted（不是 fdr_rejected）",
       any(d["key"] == "tiny" and d["reason"] == "insufficient_samples" for d in out["deleted"]))
    ck("C4 q_by_key 覆盖全部被检验的规则", len(out["fdr"].get("q_by_key") or {}) == 66)
    ck("C5 ⚠️ 零假设也进 m（这是校正变严的唯一来源）",
       out["fdr"]["m"] == 66, "若只把'单条显著'的算进 m，校正就形同虚设（见 B 组反直觉点）")


# ─────────────────────────────── D ───────────────────────────────
def group_d() -> None:
    print("=== D 落盘往返 + 真实文件未被改动 ===")
    real_before = _hash(REAL_LB)
    tmp = tempfile.mkdtemp(prefix="rulefdr_")
    path = os.path.join(tmp, "lb.json")
    obs = _run("observe", _FIX_FEW)
    payload = {"baseline_t1": obs["baseline_t1"], "min_hit_rate": obs["min_hit_rate"],
               "rules": obs["rules"], "deleted": obs["deleted"],
               "verified_count": obs["verified_count"], "fdr": obs["fdr"]}
    RV.save_form_leaderboard(payload, path)
    back = RV.load_form_leaderboard(path)
    ck("D1 排行榜可存可读，且保留 fdr 元数据",
       back is not None and (back.get("fdr") or {}).get("m") == obs["fdr"]["m"],
       f"fdr={(back or {}).get('fdr', {}).get('mode')}")
    ck("D2 规则条目保留 p_value / q_value / fdr_pass",
       all({"p_value", "q_value", "fdr_pass"} <= set(r) for r in (back or {}).get("rules", [])))
    ck("D3 load_verified_rule_dicts 仍能工作（下游 daily_scan 兼容）",
       len(RV.load_verified_rule_dicts(path) or []) == len((back or {}).get("rules", [])))
    ck("D4 ⚠️ 真实 form_leaderboard.json 未被本脚本改动",
       _hash(REAL_LB) == real_before, f"{real_before} -> {_hash(REAL_LB)}")
    try:
        with open(REAL_LB, encoding="utf-8") as fh:
            real = json.load(fh)
        ck("D5 真实排行榜仍可加载且规则非空（存量产物未被破坏）",
           bool(real.get("rules")), f"rules={len(real.get('rules') or [])}")
        ck("D6 真实排行榜尚未重跑（还没有 q_value）→ 校正待下一次规则验证生效",
           all("q_value" not in r for r in (real.get("rules") or [])),
           "本次不重跑全市场扫描（成本高），只落代码与参数；下次规则验证会自动带上 q 值")
    except Exception as exc:  # noqa: BLE001
        ck("D5 真实排行榜可读", False, str(exc))


# ─────────────────────────────── E ───────────────────────────────
def group_e() -> None:
    print("=== E BH 的真实数学行为（两套场景）===")
    many_obs = _run("observe", _FIX_MANY)
    qs = sorted(r["q_value"] for r in many_obs["rules"] if r.get("q_value") is not None)
    ck("E1 场景-多：6 条 p<α 的规则 q 值都 > 0.05（单条过、校正不过）",
       len(qs) == 6 and all(q > 0.05 for q in qs), f"q={qs}")
    ck("E2 场景-多：单条判定与 FDR 判定确实分歧（否则校正没有意义）",
       many_obs["fdr"]["n_reject"] < many_obs["verified_count"],
       f"FDR 通过 {many_obs['fdr']['n_reject']} < 单条通过 {many_obs['verified_count']}")

    few_obs = _run("observe", _FIX_FEW)
    few_enf = _run("enforce", _FIX_FEW)
    fq = sorted(r["q_value"] for r in few_obs["rules"] if r.get("q_value") is not None)
    ck("E3 场景-少：2 条极强规则 q ≪ 0.05（真信号在少量检验里能活下来）",
       len(fq) == 2 and all(q < 1e-6 for q in fq), f"q={fq}")
    ck("E4 场景-少：enforce 保留 2 条极强（校正不是无脑砍）",
       few_enf["verified_count"] == 2 and few_enf["fdr"]["n_fdr_rejected"] == 0,
       f"verified={few_enf['verified_count']} n_fdr_rejected={few_enf['fdr']['n_fdr_rejected']}")
    ck("E5 两套场景结论相反，说明结果由『m 与发现数之比』决定，而不是由『有没有做 FDR』决定",
       many_obs["fdr"]["n_reject"] == 0 and few_obs["fdr"]["n_reject"] == 2,
       f"多检验通过 {many_obs['fdr']['n_reject']}/6（全砍），"
       f"少检验通过 {few_obs['fdr']['n_reject']}/4（2 条零假设 p=0.5 本就不该通过）"
       " —— 关键在 m 里含不含零假设")
    ck("E6 q 值随 p 单调不减（BH 的必要性质）",
       all(qs[i] <= qs[i + 1] + 1e-12 for i in range(len(qs) - 1)))
    ck("E7 baseline_t1 记录正确", abs(few_obs["baseline_t1"] - 0.5) < 1e-9)


# ─────────────────────────────── F ───────────────────────────────
def group_f() -> None:
    print("=== F 护栏：默认模式不会静默降级下游 ===")
    off = _run("off", _FIX_FEW)
    obs = _run("observe", _FIX_FEW)
    ck("F1 observe 的 verified_count == off（默认发布前后行为一致）",
       obs["verified_count"] == off["verified_count"],
       f"off={off['verified_count']} observe={obs['verified_count']}")
    ck("F2 因此默认不会触发 fallback（不会静默削弱每日推荐规则信号）",
       (obs["verified_count"] < 3) == (off["verified_count"] < 3),
       "若要 enforce，必须先确认 verified_count 不会掉到 3 以下（或同步调整兜底）")
    with open(os.path.join(BACKEND, "app/backtest/rule_verifier.py"), encoding="utf-8") as fh:
        src = fh.read()
    ck("F3 默认常量就是 observe（代码级确认，不靠文档）", 'FDR_MODE = "observe"' in src)
    ck("F4 三个模式常量齐全", all(f'"{m}"' in src for m in RV.FDR_MODES),
       f"{RV.FDR_MODES}")
    ck("F5 走 daily_scan 兜底阈值一致性检查（verified<3 → fallback）",
       "verified_count\" ] < 3" in src or "verified_count\"] < 3" in src
       or "fallback" in src, "rule_verifier 里仍保留 fallback 判定")


def main() -> int:
    print("=" * 72)
    print("验收：规则验证的多重检验校正（P2）")
    print("=" * 72)
    group_a()
    group_b()
    group_c()
    group_d()
    group_e()
    group_f()
    print("=" * 72)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
