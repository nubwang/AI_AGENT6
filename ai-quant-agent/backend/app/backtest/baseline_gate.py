"""基准门槛（baseline_gate）— plans/24 §11.16.4 的**代码化**

## 为什么要有这个模块

plans/24 用 16 节、15 次自我否证，才把下面这件事说清楚：

    「全区间年化好看」+「没扣成本」+「单一强势段撑着」= 结论不可信。

而这些教训如果只写在文档里，下一次还会犯。所以这里把它们固化成**可复用函数**：
任何"新特征 / 新形态 / 新择时"，都必须先过这道门。

## 四条准入门槛（**全部满足**才算 usable）

    a) 扣成本后年化 > 0
    b) t > 2                      —— 不是噪声
    c) 逐段超额 > 0（若提供基准）  —— 每段都要跑赢基准
    d) 逐段净收益 > 0             —— **四段全正**（2024 / 2025H1 / 2025H2 / 2026）

> 注意：门槛 d 是**故意设得很硬**的。实测结果是 ——
> **连基准自己（B1/B2）都过不了 d**（2026 段为负）。
> 这不是 bug：它正说明"用的时候必须带年份状态"，也说明
> **任何声称"长期稳定有效"的回测结论，在 2024–2026 这段数据上都不成立。**

## 已实测基准（来源 scripts/verify_size_baseline.py；**含退市股**、扣双边 0.50%）

    B1 保守基准：全市场等权（过滤后）· 持 20 日 ⇒ +11.1%/年（t +4.02）
    B2 上限参考：Q1 最小市值 20% 等权 · 持 20 日 ⇒ +17.7%/年（t +4.46）
    对照       ：我们的候选池（形态 + 条件概率打分）⇒ +2.5%/年

## 口径红线（写代码的检查清单，每次验证都要对照）

    1. 逐日截面（**不用全局分位**）        —— §11.8 的坑
    2. 扣成本（双边费率写进参数）          —— §11.11
    3. 按日等权（**不按记录计数**）        —— §11.2 的坑
    4. 年份/半年分解（**禁全区间年化**）    —— §11.13
    5. 样本外切分（方向只用前半段定）       —— §11.14
    6. 可交易性过滤（涨停/低价/低流动）     —— §11.12
    7. 同窗口对照（不拿 2.6 年对比 1.5 月） —— §9.4
    8. 命中率 × 负超额（高减低显著 ≠ 可交易）—— §10.4
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIZE_CACHE = os.path.join(BACKEND, "data", "size_baseline_daily.json")

SEGMENTS: tuple[str, ...] = ("2024", "2025H1", "2025H2", "2026")
REBAL_20 = 252.0 / 20.0          # 持 20 日的年换仓次数
# 已实测基准（写死为常量，便于在没有缓存时也能判定；数值来源见模块 docstring）
BASELINE_REF = {
    "B1": {"label": "保守基准：全市场等权 · 持 20 日", "per_year": REBAL_20},
    "B2": {"label": "上限参考：Q1 最小市值 20% 等权 · 持 20 日", "per_year": REBAL_20},
}
CHECKLIST: tuple[str, ...] = (
    "逐日截面（不用全局分位）",
    "扣成本（双边费率写进参数）",
    "按日等权（不按记录计数）",
    "年份/半年分解（禁全区间年化）",
    "样本外切分（方向只用前半段定）",
    "可交易性过滤（涨停/低价/低流动）",
    "同窗口对照（不拿长区间对比短样本）",
    "命中率 × 负超额（高减低显著 ≠ 可交易）",
)

# ★ 门槛 d 的开关（plans/24 §11.36.6 的结论 → §11.36.9 接线）
#   F3 滚动前推实测把这条说死了：**「样本外评估」与「四段净收益全正(d)」在定义上互斥**
#   —— 样本外窗按时间切在后面，天然缺前几段 ⇒ d 恒为「不可判定」，5 折里 **0 折可判**。
#   此时拿 d 去判定，等于**用「没有数据」当「不通过」**（§11.21.6 的同一条护栏）。
#   所以给一个开关，但**默认仍为 True**：默认路径与 §11.17 契约**逐字节一致，不放宽**。
REQUIRE_D_PARAM = "baseline_gate_require_d"
REQUIRE_D_DEFAULT = True


def require_segments() -> bool:
    """门槛 d（四段净收益全正）是否为**必需**条件。

    读可进化参数 `baseline_gate_require_d`；缺省 / 非法 / 读取失败 → `True`（保持原契约）。

    ⚠️ 置 `False` **只应在样本外 / 滚动前推（walk-forward）验证时使用**：那类窗口按时间切分，
    必然缺段，`d` 永不可判 —— 此时判定退化为 `a ∧ b ∧ b_hac`（**仍要求两档显著性**）。
    """
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param(REQUIRE_D_PARAM, REQUIRE_D_DEFAULT)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if not s:
                # ★ 空串 = 「未设置」⇒ 回退默认(True)。**不可**把空串当 False：
                #   那会在配置意外为空时**悄悄放宽门槛**，与"宁可严格"的原则相悖
                #   （验收脚本 [F] 组第一版就是被这一条抓出来的）。
                return REQUIRE_D_DEFAULT
            return s not in ("false", "0", "no")
        if isinstance(v, (int, float)):
            return bool(v)
    except Exception:  # noqa: BLE001
        pass
    return REQUIRE_D_DEFAULT


@dataclass
class Candidate:
    """一个待判定的候选策略（或基准）。"""
    name: str
    gross: float                      # 每期毛收益（日等权）
    t: float                          # 每期收益序列的 t
    per_year: float                   # 年换仓次数（252 / 持有期）
    by_seg: dict[str, float] = field(default_factory=dict)   # 各段每期毛收益
    n: int = 0
    # Newey-West t（修正重叠样本；plans/24 §11.28）。**默认 nan ⇒ 不参与任何判定**，
    # 只用于展示与对照（日序列缺失 / 样本不足时保持 nan，**不伪造**）。
    t_hac: float = float("nan")


SIZE_SERIES = os.path.join(BACKEND, "data", "size_baseline_series.json")
OURS_SERIES = os.path.join(BACKEND, "data", "ours_baseline_series.json")


def _hac_t(path: str, key: str, lag: int) -> float:
    """从**日序列**缓存补算 Newey-West t（plans/24 §11.28）。

    背景：`*_daily.json` 里存的是 **naive t**（未修正重叠），日序列另存在 `*_series.json`
    （由 verify_size_baseline.py / verify_ours_baseline.py 落盘）。
    样本 < 30 或日序列缺失 ⇒ 返回 nan（诚实：算不出就不给数）。
    """
    try:
        if not os.path.exists(path):
            return float("nan")
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        vals = list(((d or {}).get("series") or {}).get(key, {}).values())
        x = [float(v) for v in vals if isinstance(v, (int, float))]
        if len(x) < 30:
            return float("nan")
        from app.backtest.stats_correction import newey_west_t
        return float(newey_west_t(x, lag))
    except Exception:  # noqa: BLE001
        return float("nan")


def load_market_baseline(h: int = 20) -> dict[str, Candidate]:
    """从 `data/size_baseline_daily.json` 读已实测的 B1/B2（**不硬编码数字**，防漂移）。

    读不到缓存时返回空 dict（调用方应提示先跑 verify_size_baseline.py）。
    """
    if not os.path.exists(SIZE_CACHE):
        return {}
    try:
        with open(SIZE_CACHE, encoding="utf-8") as f:
            recs = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Candidate] = {}
    want = {"market_ew": "B1", "size": "B2"}
    for r in recs:
        if r.get("h") != h:
            continue
        key = None
        if r.get("kind") == "market_ew":
            key = "B1"
        elif r.get("kind") == "size" and r.get("q") == 0:
            key = "B2"
        if key is None:
            continue
        lab = ("保守基准：全市场等权 · 持 20 日"
               if key == "B1" else "上限参考：Q1 最小市值 20% 等权 · 持 20 日")
        skey = f"h{h}_market_ew" if key == "B1" else f"h{h}_size0"
        out[key] = Candidate(
            name=lab, gross=float(r.get("gross") or 0.0),
            t=float(r.get("t") or float("nan")),
            per_year=252.0 / float(h),
            by_seg={k: float(v) for k, v in (r.get("by_seg") or {}).items()},
            n=int(r.get("n") or 0),
            t_hac=_hac_t(SIZE_SERIES, skey, h - 1),
        )
    _ = want
    return out


OURS_CACHE = os.path.join(BACKEND, "data", "ours_baseline_daily.json")
# 滑点（§11.24 实测）：**单边** bp。默认 20bp = "按成交额分档"的中间档（结构性假设，非实测）。
SLIP_PARAM = "baseline_slip_bp"
SLIP_DEFAULT_BP = 20.0
FEE_DEFAULT = 0.005
# ⚠️ 两套口径必须分开（`daily_*.json` 的只数不稳定：早期 476 只 = 候选池，后期 30 只 = 已截断）：
#    top30 = 按 `rank` 取前 30（**实盘口径**，与 `daily_top_k` 一致）
#    all   = 不截断（**候选池口径**，含早期未截断的几百只）
OURS_LABEL = {
    "ours_top30": "我们的实盘推荐榜（规则榜前 30 + Agent 榜）",
    "ours_all": "我们的候选池全量（不截断）",
    "ours_rules_top30": "我们·规则榜前 30",
    "ours_agent": "我们·Agent 精筛榜（全部）",
}
TOP_N = 30                     # 与可进化参数 `daily_top_k` 的默认值一致


def load_ours(h: int = 20) -> dict[str, Candidate]:
    """从 `data/ours_baseline_daily.json` 读「我们自己」的口径（**不硬编码**，防漂移）。

    该缓存由 `scripts/verify_ours_baseline.py` 生成：它把**真实的每日推荐记录**
    （`daily_recommend/daily_*.json`）按与 §11.16 **完全相同的口径**
    （T+1 开盘买入、日等权、扣双边 0.50%、`|fwd| > 1.5` 剔除）算成分段收益。

    无缓存 / 解析失败返回空 dict（调用方应提示先跑脚本）。
    """
    if not os.path.exists(OURS_CACHE):
        return {}
    try:
        with open(OURS_CACHE, encoding="utf-8") as f:
            recs = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Candidate] = {}
    for r in (recs or []):
        if int(r.get("h") or 0) != h:
            continue
        kind = str(r.get("kind") or "")
        if kind not in OURS_LABEL:
            continue
        by: dict[str, float] = {}
        for k, v in (r.get("by_seg") or {}).items():
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isnan(fv):
                by[k] = fv
        out[kind] = Candidate(
            name=OURS_LABEL[kind], gross=float(r.get("gross") or 0.0),
            t=float(r.get("t") or float("nan")), per_year=252.0 / float(h),
            by_seg=by, n=int(r.get("n") or 0),
            t_hac=_hac_t(OURS_SERIES, f"h{h}_{kind}", h - 1))
    return out


def coverage(cand: Candidate) -> dict:
    """分段覆盖率 —— **区分「不通过」与「不可判定」**（本轮新增的方法论护栏）。

    §11.17 的 `judge()` 把"缺段"与"段为负"都算成 `d=False`。这对**小样本候选**
    （例如我们自己的推荐记录只有 2026-08~09）会产生**误导**：它不是"判定为差"，
    而是**根本没资格被判定** —— 把两者混为一谈，等于用"没有数据"当"证据"。

    所以：**判定前先看覆盖率**，`decidable=False` 时只能报 `not_decidable`。
    """
    have = [s for s in SEGMENTS
            if cand.by_seg.get(s) is not None
            and not math.isnan(float(cand.by_seg[s]))]
    missing = [s for s in SEGMENTS if s not in have]
    return {"n_have": len(have), "n_total": len(SEGMENTS), "missing": missing,
            "decidable": not missing}


def judge(cand: Candidate, fee: float,
          base: Candidate | None = None,
          slip_bp: float = 0.0) -> dict:
    """四条件判定。返回 dict（含每条的通过与否 + 原因列表）。

    - `fee`：双边总费率（如 0.005）
    - `base`：基准候选（用于条件 c）；不传则 c 记为 None（不参与 verdict）
    - `slip_bp`：**单边**滑点（bp）；总成本 = `fee + 2 × slip_bp/1e4`。
      默认 **0** 以保持 §11.17 的既有契约；**实盘口径请用 `judge_default()`**
      （默认 20bp，来源 §11.24）。

    > 为什么要把滑点塞进门槛（§11.24 实测）：单边 20bp 时 B1 从 +11.1%/年 降到 **+6.0%/年**；
    > 而**持 10 日的归零滑点只有 8.5bp** ⇒ 不带滑点的"过闸"是**假过闸**。
    """
    reasons: list[str] = []
    cost = fee + 2.0 * slip_bp / 1e4
    net = (cand.gross - cost) * cand.per_year
    a = net > 0
    if not a:
        reasons.append(f"a 不过：扣成本（费率 {fee:.2%} + 滑点 {2 * slip_bp:.0f}bp）后年化"
                       f" {net:+.1%} ≤ 0")
    b = (not math.isnan(cand.t)) and cand.t > 2.0
    if not b:
        tv = "nan" if math.isnan(cand.t) else f"{cand.t:+.2f}"
        reasons.append(f"b 不过：t = {tv} ≤ 2")

    # ★ `b_hac` = 「重叠修正后的第二档」（plans/24 §11.28 / §11.31）：
    #   naive `t` **未修正重叠样本**（§11.27 实测可把 3.08 放大到 4.02、§11.16 的 B1 4.02→1.15），
    #   所以**要上生产的东西必须两档都过** —— 这是一个**只加严、不放宽**的改动：
    #   条件 b 本身不变（既有契约与文字不受影响），`usable` 额外要求 `b_hac`。
    #   `t_hac` 缺失（nan，如样本 < 30）**不算过**：宁可"过不了"，不可"看不清还放行"。
    b_hac = (not math.isnan(cand.t_hac)) and cand.t_hac > 2.0
    if not b_hac:
        hv = "nan" if math.isnan(cand.t_hac) else f"{cand.t_hac:+.2f}"
        reasons.append(f"b_hac 不过：HAC t = {hv} ≤ 2（未修正重叠的第二档，§11.28）")

    seg_net: dict[str, float] = {}
    seg_ex: dict[str, float] = {}
    d = True
    for s in SEGMENTS:
        g = cand.by_seg.get(s)
        if g is None or math.isnan(g):
            d = False
            continue
        seg_net[s] = (g - cost) * cand.per_year
        if seg_net[s] <= 0:
            d = False
    if not d:
        bad = [f"{s} {v:+.1%}" for s, v in seg_net.items() if v <= 0]
        reasons.append("d 不过：分段净收益有非正值 → " + "、".join(bad or ["（缺段数据）"]))

    c: bool | None = None
    if base is not None:
        c = True
        for s in SEGMENTS:
            g, bg = cand.by_seg.get(s), base.by_seg.get(s)
            if g is None or bg is None or math.isnan(g) or math.isnan(bg):
                c = False
                continue
            seg_ex[s] = g - bg
            if seg_ex[s] <= 0:
                c = False
        if not c:
            bad = [f"{s} {v:+.3%}" for s, v in seg_ex.items() if v <= 0]
            reasons.append("c 不过：分段超额有非正值 → " + "、".join(bad or ["（缺段数据）"]))

    # ★ 判定 = a ∧ b ∧ **b_hac** ∧（d 若必需）∧ (c 非 False)
    #   `b_hac` 是 §11.28 新增的第二档；`d` 是否必需由 `require_segments()` 决定
    #   （默认必需 ⇒ 与 §11.17 契约一致；样本外/滚动前推场景可显式豁免，见 §11.36.9）。
    need_d = require_segments()
    ok = a and b and b_hac and (d or not need_d) and (c is not False)
    if not need_d and not d:
        reasons.append("d 已豁免（baseline_gate_require_d=false）：样本外/滚动窗口天然缺段，"
                       "判定改用 a ∧ b ∧ b_hac（plans/24 §11.36.6）")
    return {
        "name": cand.name, "gross": cand.gross, "net": net, "t": cand.t,
        "t_hac": cand.t_hac,
        "fee": fee, "slip_bp": slip_bp, "cost": cost,
        "a": a, "b": b, "b_hac": b_hac, "c": c, "d": d, "d_required": need_d,
        "seg_net": seg_net, "seg_ex": seg_ex,
        "verdict": "usable" if ok else "reject",
        "reasons": reasons,
    }


def default_slip_bp() -> float:
    """读可进化参数 `baseline_slip_bp`；失败或非法回退 `SLIP_DEFAULT_BP`（20bp，§11.24）。"""
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param(SLIP_PARAM, SLIP_DEFAULT_BP)
        f = float(v if v is not None else SLIP_DEFAULT_BP)
        return f if f >= 0 else SLIP_DEFAULT_BP
    except Exception:  # noqa: BLE001
        return SLIP_DEFAULT_BP


def judge_default(cand: Candidate, base: Candidate | None = None) -> dict:
    """**实盘口径**判定：费率 `FEE_DEFAULT`(0.5%) + 滑点 `default_slip_bp()`(默认 20bp)。

    为什么要有这个入口：`judge()` 默认 `slip_bp=0`（为了不破坏 §11.17 的既有契约），
    但 **`slip_bp=0` 不是实盘**。今后任何"新特征 / 新形态 / 新择时"要过闸，
    **一律用这个函数**；只在做"与 §11.17 对照"时才显式传 `slip_bp=0`。
    """
    return judge(cand, FEE_DEFAULT, base, default_slip_bp())


def fmt_judge(j: dict, per_year: float = REBAL_20) -> str:
    """把判定结果拼成一行文本（供脚本 / 日志打印）。"""
    m = lambda x: "✔" if x is True else ("—" if x is None else "✘")  # noqa: E731
    tv = "nan" if math.isnan(j["t"]) else f"{j['t']:+.2f}"
    slip = float(j.get("slip_bp") or 0.0)
    stag = f"slip{slip:g}bp" if slip else ""
    head = (f"{j['name'][:34]:<36}{j['net']:>+9.1%}{tv:>8}"
            f"{m(j['a']):>4}{m(j['b']):>4}{m(j['c']):>4}{m(j['d']):>4}{stag:>11}")
    tail = f"  {j['verdict']}"
    if j.get("d_required") is False:
        tail += " [d豁免: a∧b∧b_hac]"
    if j["reasons"]:
        tail += "  (" + "; ".join(j["reasons"])[:160] + ")"
    _ = per_year
    return head + tail
