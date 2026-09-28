"""基准影子对照（baseline_shadow）— plans/24 §11.18 / §11.19 的**只读接线**

## 它做什么（一句话）

在**不改动推荐结果**的前提下，给榜单**附加**一个 `baseline_shadow` 字段：

    - 当日 B1 / B2 名单（索引化组合，来自 baseline_portfolio）
    - §11.17 四条件门槛判定（来自 baseline_gate、已实测基准缓存）
    - §11.18 的同窗口差距（我们 vs 基准 vs 随机 30 只）

## 三条硬约束（写进代码，不靠自觉）

1. **默认关闭**：可进化参数 `daily_baseline_mode` 默认 `off`
   ⇒ 返回值与开启前**逐字节相同**（连**对象身份**都不变，见 `attach_shadow`）；
2. **只增不改**：开启后也只**新增** `baseline_shadow` 一个键 ——
   `top_picks` 的**顺序与内容一字不动**（用浅拷贝，不重建任何列表）；
3. **静默降级**：任何异常都原样返回原 report（与 `_with_plans` 同一策略），
   绝不影响榜单展示。

## 为什么不直接把持仓权重换掉（本节最需要解释的事）

§11.18 已定量证明：同日同口径下，"随机抽 30 只"每年比我们的打分选股多 **6.8pp**。

但 **"证明打分是负 alpha" ≠ "可以立刻把权重换掉"** ——
换权重会改变持仓只数、行业暴露与回撤路径，必须先有独立的 A/B 与样本外验证
（项目纪律：**先验证后生效**；plans/24 §11.16.4 使用规则第 1 条）。

所以本轮只做**影子记录**：把对照与判定摆到台面上，
让"是否切权重"变成**有依据的决策**，而不是拍脑袋 ——
这正是 todo「把打分选股改为不决定持仓权重（只记录/提示）+ 留参数开关」的落地形态。

## 诚实边界（必须与展示一起出现）

- 我们的候选池**没有落盘 by_seg** ⇒ 本模块**只对 B1/B2 下门槛判定**，
  **不对"我们自己"下判定**（避免用不完整数据给出看似严谨的结论）；
- B1/B2 是**索引化组合，不是推荐标的**（§11.19.1）：
  展示时必须与 `top_picks` 区分标注，否则会被误读为"另一份推荐"；
- 未扣滑点、未处理买入日一字板；小市值组（B2）滑点最重（§11.17 已登记）。

用法：
    from app.backtest import baseline_shadow
    rep = baseline_shadow.attach_shadow(report, "20260918")   # off 时**原样返回**
"""
from __future__ import annotations

import math

from app.core.logger import logger
from app.backtest import baseline_gate, baseline_portfolio

# 可进化参数名（注册于 data/evolution_config.json，默认 off）
PARAM = "daily_baseline_mode"
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODES: tuple[str, ...] = (MODE_OFF, MODE_SHADOW)
SHADOW_KEY = "baseline_shadow"
N_DEFAULT = baseline_portfolio.N_DEFAULT
HOLD_DAYS = baseline_portfolio.HOLD_DAYS
FEE_RT = 0.005                    # 双边总费率，与 §11.16 / §11.17 / §11.19 一致

DISCLAIMER = ("影子对照：只记录、不参与推荐决策。B1/B2 是**索引化组合**（无信息选择），"
              "不是推荐标的；top_picks 未受任何影响。")

# ★ t 口径披露（§11.28）：判定用的 `t` = naive（未修正重叠样本）；`t_hac` 只作对照。
# 放在**模块常量**并在 `build_shadow` 初始化 ⇒ **即使缺缓存也一定存在**
# （口径声明不该随数据可用性消失；否则前端那一行会静默空白）。
T_BASIS = ("`t` = naive（未修正重叠，**判定用 b**）；"
           "`t_hac` = Newey-West（lag = 持有期 − 1，**判定用 b_hac**）——"
           "**§11.31 起 `usable` 需 b ∧ b_hac（两档并列，只加严、不放宽）**")

# §11.18.4 / §11.16.2 的实测参考（**不是实时计算**；重算见 scripts/verify_baseline_sampling.py）
REF_OURS_PER_YEAR = 0.025         # 我们的候选池（形态 + 条件概率打分）
REF_RANDOM30_PER_YEAR = 0.093     # 同窗口「随机抽 30 只」
REF_B2_PER_YEAR = 0.177           # Q1 最小市值 20% 等权
REF_SOURCE = "plans/24 §11.18.4、§11.16.2（同窗口、持 20 日、扣双边 0.500%）"


def mode() -> str:
    """读可进化参数 `daily_baseline_mode`；非法值/读失败一律回退 `off`（J1）。"""
    try:
        from app.agents import evolution_config
        raw = evolution_config.get_param(PARAM, MODE_OFF)
        m = str(raw if raw is not None else MODE_OFF).strip().lower()
        return m if m in MODES else MODE_OFF
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[baseline_shadow] 读参数失败，回退 off: {exc}")
        return MODE_OFF


def enabled() -> bool:
    """影子对照是否开启（唯一判据）。"""
    return mode() == MODE_SHADOW


def _gate_block(out: dict) -> None:
    """填 out['gate']（B1/B2 的四条件判定）与 out['gate_ours']（**我们自己的**）。

    对「我们」有一条硬规矩（§11.21）：**先判可判定性，再判四条件** ——
    因为 `judge()` 把"缺段"与"段为负"都算成 `d=False`，
    而我们只有 2026 段的推荐记录 ⇒ 直接判会得到 `reject`，那是**缺段所致**，
    不是"判定为差"。所以 `gate_ours` 里 `decidable=False` 时 verdict 记 `not_decidable`。
    """
    base = baseline_gate.load_market_baseline(HOLD_DAYS)
    if not base:
        out["notes"].append(
            "缺 data/size_baseline_daily.json ⇒ 跳过门槛判定"
            "（先跑 scripts/verify_size_baseline.py）")
        return
    b1 = base.get("B1")
    for key in ("B1", "B2"):
        cand = base.get(key)
        if cand is None:
            continue
        # 基准自身不比自身（v.s. §11.17 的做法：超额恒为 0 会让 c 假性不过）
        # ★ 用 `judge_default()`：费率 **+ 滑点**（§11.24）—— 不带滑点的判定是"假过闸"。
        j = baseline_gate.judge_default(cand, None if key == "B1" else b1)
        out["gate"][key] = {
            "label": cand.name,
            "net_per_year": round(float(j["net"]), 6),
            "gross_per_period": round(float(cand.gross), 6),
            "t": None if math.isnan(cand.t) else round(float(cand.t), 3),
            # ★ `t_hac` = Newey-West（lag = 持有时长 − 1），修正**重叠样本**导致的 t 膨胀（§11.28）。
            # **默认不参与判定**（a/b/c/d 仍用 naive `t`），只做口径披露。
            "t_hac": None if math.isnan(cand.t_hac) else round(float(cand.t_hac), 3),
            "fee": j["fee"], "slip_bp": j["slip_bp"],
            "a": j["a"], "b": j["b"], "b_hac": bool(j["b_hac"]),
            "c": j["c"], "d": j["d"],
            "verdict": j["verdict"],
            "seg_net_per_year": {k: round(float(v), 6) for k, v in j["seg_net"].items()},
            "reasons": list(j["reasons"]),
        }
    # ── 我们自己的口径（若已落盘）──
    ours = baseline_gate.load_ours(HOLD_DAYS)
    if not ours:
        out["notes"].append(
            "缺 data/ours_baseline_daily.json ⇒ 未判「我们」"
            "（先跑 scripts/verify_ours_baseline.py）")
        return
    for kind, cand in ours.items():
        cov = baseline_gate.coverage(cand)
        item: dict = {
            "label": cand.name,
            "gross_per_period": round(float(cand.gross), 6),
            "net_per_year": round(float((cand.gross - FEE_RT) * (252.0 / HOLD_DAYS)), 6),
            "t": None if math.isnan(cand.t) else round(float(cand.t), 3),
            "t_hac": None if math.isnan(cand.t_hac) else round(float(cand.t_hac), 3),
            "n": int(cand.n),
            "segments_have": cov["n_have"], "segments_total": cov["n_total"],
            "segments_missing": list(cov["missing"]),
            "decidable": bool(cov["decidable"]),
            "by_seg": {k: round(float(v), 6) for k, v in cand.by_seg.items()},
        }
        if cov["decidable"]:
            j = baseline_gate.judge_default(cand, b1)
            item.update({"a": j["a"], "b": j["b"], "b_hac": bool(j["b_hac"]),
                         "c": j["c"], "d": j["d"],
                         "fee": j["fee"], "slip_bp": j["slip_bp"],
                         "verdict": j["verdict"], "reasons": list(j["reasons"])})
        else:
            miss = "、".join(cov["missing"])
            item["verdict"] = "not_decidable"
            item["reasons"] = [
                f"缺 {len(cov['missing'])} 段（{miss}）⇒ **不可判定**，"
                "不是「判定为差」（避免拿没有数据当证据，见 §11.21）"]
        out["gate_ours"][kind] = item
    if not any(bool(v.get("decidable")) for v in out["gate_ours"].values()):
        out["notes"].append(
            "「我们」的推荐记录只覆盖 2026 段 ⇒ 门槛对我们 not_decidable"
            "（缺 2024 / 2025 段；宁缺不造，不用今天的参数回测过去）")
    # ★ t 口径披露（§11.28）：**判定用的 `t` 未修正重叠样本**；`t_hac` 只作对照。
    out["t_basis"] = T_BASIS   # 与 build_shadow 的初始化共用同一常量（防两处漂移）


def build_shadow(date: str, n: int = N_DEFAULT) -> dict:
    """构造影子对照内容（**只读**：不写盘、不改任何推荐决策）。"""
    d = str(date or "")
    out: dict = {
        "mode": MODE_SHADOW,
        "date": d,
        "n": int(n),
        "hold_days": HOLD_DAYS,
        "fee_rt": FEE_RT,
        "slip_bp": float(baseline_gate.default_slip_bp()),
        "disclaimer": DISCLAIMER,
        # ★ 口径声明**顶层就有**（不依赖 size_baseline/ours_baseline 缓存是否存在）
        "t_basis": T_BASIS,
        "portfolios": {},
        "gate": {},
        "gate_ours": {},
        "reference": {
            "ours_per_year": REF_OURS_PER_YEAR,
            "random30_per_year": REF_RANDOM30_PER_YEAR,
            "size_q1_per_year": REF_B2_PER_YEAR,
            "gap_pp": round(REF_RANDOM30_PER_YEAR - REF_OURS_PER_YEAR, 6),
            "source": REF_SOURCE,
            "note": "我们的 +2.5% 是**实测参考**（非实时计算）；"
                    "重算见 scripts/verify_baseline_sampling.py",
        },
        "notes": [],
    }
    if not d:
        out["notes"].append("缺少日期 ⇒ 未生成名单（仅返回口径声明）")
        _gate_block(out)
        return out

    for m in ("market_ew", "size_q1"):
        try:
            rec = baseline_portfolio.build_baseline_picks(d, n=n, mode=m)
        except Exception as exc:  # noqa: BLE001
            out["notes"].append(f"{m} 名单生成失败: {exc}")
            continue
        out["portfolios"][m] = {
            "label": rec.get("label", m),
            "pool_size": rec.get("pool_size", 0),
            "n_picked": rec.get("n_picked", 0),
            "note": rec.get("note", ""),
            "codes": [p["ts_code"] for p in rec.get("picks", [])],
        }
    if not out["portfolios"]:
        out["notes"].append("当日名单为空（未采集该日数据 / 全部被过滤）")

    _gate_block(out)
    out["notes"].append("B1/B2 为对照基准，**不是推荐**；判定口径见 §11.17 四条件（a/b/c/d）")
    return out


def attach_shadow(report: dict | None, date: str = "") -> dict:
    """给榜单**附加**影子对照（仅在开关开启时）；关闭时**原样返回同一对象**。

    这是本模块唯一的对外入口，也是"默认关闭 ⇒ 逐字节不变"的保证点：
    - 开关关闭：`return report`（同一个对象，JSON 串一字不差）
    - 开关开启：`dict(report)` 浅拷贝后只加一个键 ⇒ `top_picks` 是**同一个 list 对象**
    - 任何异常：`return report`（静默降级，同 `_with_plans` 的策略）
    """
    if not isinstance(report, dict):
        return report or {}
    if not enabled():
        return report
    try:
        d = date or str(report.get("date") or "")
        rep = dict(report)
        rep[SHADOW_KEY] = build_shadow(d)
        return rep
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[baseline_shadow] 生成失败（不影响榜单）: {exc}")
        return report


__all__ = [
    "PARAM", "MODE_OFF", "MODE_SHADOW", "MODES", "SHADOW_KEY",
    "DISCLAIMER", "mode", "enabled", "build_shadow", "attach_shadow",
]
