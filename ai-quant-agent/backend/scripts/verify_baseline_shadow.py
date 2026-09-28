"""验收：基准影子对照（baseline_shadow）— plans/24 todo 18

它存在的**唯一目的**：证明「把打分选股改为不决定持仓权重」这一步
**不会以任何方式改动推荐结果** —— 也就是：

    (1) 默认关闭时，输出**逐字节不变**（连对象身份都不变）；
    (2) 开启后，也只**新增** baseline_shadow 键，top_picks 顺序与内容**一字不动**。

七组检查（A~G）：

    A  off 时逐字节不变（对象身份 + JSON 串）
    B  shadow 时字段齐备（portfolios / gate / reference / disclaimer）
    C  top_picks 顺序与内容一字不动（且是**同一 list 对象**）
    D  参数非法 / 空值 / 读取异常 ⇒ 一律回退 off（J1）
    E  幂等：连续两次结果一致
    F  与 trade_plan.attach_plans 串联后 top_picks 仍不受影响
    G  生产接线（静态）：predictions.py 已按开关串联

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_baseline_shadow.py
"""
from __future__ import annotations

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import evolution_config as evc  # noqa: E402
from app.backtest import baseline_shadow as bs  # noqa: E402
from app.backtest import trade_plan  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRED_FILE = os.path.join(BACKEND, "app", "api", "predictions.py")
DATE = "20260918"
CODES = ["000001.SZ", "600000.SH", "300750.SZ"]

_OK = {"pass": 0, "fail": 0}


def _ok(name: str, cond: bool, extra: str = "") -> None:
    _OK["pass" if cond else "fail"] += 1
    mark = "✔" if cond else "✘"
    print(f"  {mark} {name}" + (f"    {extra}" if extra else ""))


def _j(o) -> str:
    """稳定序列化（用于逐字节比较）。"""
    return json.dumps(o, ensure_ascii=False, sort_keys=True, default=str)


def _sample_report() -> dict:
    return {"date": DATE,
            "top_picks": [{"ts_code": c, "score": round(1.0 - i * 0.1, 3)}
                          for i, c in enumerate(CODES)]}


# ---------------------------------------------------------------- 参数注入
_FORCE: dict = {"v": None}       # None = 用真实配置；其它 = 强制返回值；异常 = 模拟读取失败
_real_get = evc.get_param


def _fake_get(name, default=None):
    if name == bs.PARAM and _FORCE["v"] is not None:
        v = _FORCE["v"]
        if isinstance(v, BaseException):
            raise v
        return v
    return _real_get(name, default)


evc.get_param = _fake_get        # baseline_shadow.mode() 内部走模块属性查找 ⇒ 生效


# ---------------------------------------------------------------- A
def group_a() -> None:
    print("=" * 100)
    print("A 关闭时逐字节不变（这是本项目对「新增开关」的硬要求）")
    print("=" * 100)
    for force in ("off", " OFF ", "", "off\n"):
        _FORCE["v"] = force
        rep = _sample_report()
        got = bs.attach_shadow(rep, DATE)
        _ok(f"参数={force!r:>8} → 同一对象（is）", got is rep)
        _ok(f"参数={force!r:>8} → JSON 串逐字节相同", _j(got) == _j(_sample_report()))
    # 非 dict 入参：与 _with_plans 的容错保持一致（None → {}）
    _FORCE["v"] = "off"
    _ok("入参 None → 返回 {}", bs.attach_shadow(None, DATE) == {})
    print()


# ---------------------------------------------------------------- B
def group_b() -> None:
    print("=" * 100)
    print(f"B 开启后字段齐备（真实日期 {DATE}）")
    print("=" * 100)
    _FORCE["v"] = "shadow"
    rep = _sample_report()
    got = bs.attach_shadow(rep, DATE)
    keys = set(got.keys()) - set(rep.keys())
    _ok("顶层只多一个键，且就是 baseline_shadow", keys == {bs.SHADOW_KEY}, f"多出: {keys}")
    sh = got.get(bs.SHADOW_KEY) or {}
    for k in ("mode", "date", "n", "hold_days", "fee_rt", "disclaimer",
              "portfolios", "gate", "reference", "notes"):
        _ok(f"字段 {k} 存在", k in sh)
    _ok("date 与请求一致", sh.get("date") == DATE)
    _ok("hold_days = 20 且 fee_rt = 0.005",
        sh.get("hold_days") == 20 and abs(float(sh.get("fee_rt") or 0) - 0.005) < 1e-9)
    _ok("disclaimer 含「不是推荐标的」", "不是推荐标的" in str(sh.get("disclaimer", "")))

    pf = sh.get("portfolios") or {}
    _ok("portfolios 含 market_ew（B1）", "market_ew" in pf)
    _ok("portfolios 含 size_q1（B2）", "size_q1" in pf)
    for k in ("market_ew", "size_q1"):
        d = pf.get(k) or {}
        _ok(f"{k} 抽出名单 {d.get('n_picked')} 只 / 池 {d.get('pool_size')} 只",
            int(d.get("n_picked") or 0) >= 30, f"note={d.get('note', '') or '—'}")

    gate = sh.get("gate") or {}
    _ok("gate 含 B1/B2（说明门槛真在跑）", {"B1", "B2"} <= set(gate.keys()))
    for k in ("B1", "B2"):
        g = gate.get(k) or {}
        if not g:
            continue
        _ok(f"{k} 判定字段齐备（a/b/c/d/verdict）",
            all(x in g for x in ("a", "b", "c", "d", "verdict")))
        _ok(f"{k} = {g.get('verdict')}（含 20bp 滑点后：连基准都过不了 d）",
            g.get("verdict") == "reject",
            f"年化净={float(g.get('net_per_year') or 0):+.1%}"
            f"（实盘口径：费率 0.5% + 滑点 {g.get('slip_bp')}bp —— §11.24）")
    _ok("gate 带 slip_bp 且 > 0（§11.24 的滑点已进门槛）",
        all(float((sh.get("gate", {}).get(k, {}) or {}).get("slip_bp") or 0) > 0
            for k in ("B1", "B2")))
    _ok("顶层带 slip_bp（口径透明）", float(sh.get("slip_bp") or 0) > 0,
        f"slip_bp={sh.get('slip_bp')}")
    ref = sh.get("reference") or {}
    _ok("reference 差距 = 6.8pp（§11.18 实测）",
        abs(float(ref.get("gap_pp") or 0) - 0.068) < 1e-6,
        f"ours={ref.get('ours_per_year')} random30={ref.get('random30_per_year')}")
    # ── 我们自己的口径（§11.21）：必须先判「可判定性」，再谈四条件 ──
    go = sh.get("gate_ours") or {}
    _ok("gate_ours 含「我们」的四条口径",
        {"ours_top30", "ours_all", "ours_rules_top30", "ours_agent"} <= set(go.keys()),
        f"实际 {sorted(go.keys())}")
    for k in ("ours_top30", "ours_all"):
        g = go.get(k) or {}
        if not g:
            continue
        _ok(f"{k} 记为 not_decidable（推荐记录只覆盖 2026 段）",
            g.get("verdict") == "not_decidable",
            f"段 {g.get('segments_have')}/{g.get('segments_total')}")
        _ok(f"{k} 明确列出缺失段", bool(g.get("segments_missing")),
            f"缺 {g.get('segments_missing')}")
        _ok(f"{k} 登记「不可判定 ≠ 判定为差」",
            any("不可判定" in str(r) for r in (g.get("reasons") or [])))
        _ok(f"{k} 带 by_seg（供展示分段）", isinstance(g.get("by_seg"), dict))
    _ok("notes 说明「我们」not_decidable 的原因",
        any("not_decidable" in n for n in (sh.get("notes") or [])))
    print("  样本（节选）：")
    for n in (sh.get("notes") or []):
        print(f"    · {n}")
    print()


# ---------------------------------------------------------------- C
def group_c() -> None:
    print("=" * 100)
    print("C top_picks 顺序与内容一字不动（本验收的核心）")
    print("=" * 100)
    _FORCE["v"] = "shadow"
    rep = _sample_report()
    got = bs.attach_shadow(rep, DATE)
    _ok("top_picks 是**同一个 list 对象**（连引用都没换）",
        got.get("top_picks") is rep.get("top_picks"))
    _ok("top_picks JSON 串逐字节相同", _j(got.get("top_picks")) == _j(rep.get("top_picks")))
    _ok("顺序未变（ts_code 序列一致）",
        [p["ts_code"] for p in got.get("top_picks", [])] == CODES)
    _ok("打分字段未被改写", _j(got.get("top_picks")) == _j(_sample_report()["top_picks"]))
    _ok("原 report 未被就地修改（无 baseline_shadow 残留）",
        bs.SHADOW_KEY not in rep)
    print()


# ---------------------------------------------------------------- D
def group_d() -> None:
    print("=" * 100)
    print("D 参数非法 / 空值 / 读取异常 ⇒ 一律回退 off（J1）")
    print("=" * 100)
    for force, why in (("replace", "未实现的模式"), ("SHADOW ", "大小写/空格容错=开启"),
                       ("", "空字符串"), (RuntimeError("boom"), "读配置抛异常"),
                       (None, "参数缺失/真实配置")):
        _FORCE["v"] = force
        rep = _sample_report()
        got = bs.attach_shadow(rep, DATE)
        on = got is not rep
        if force == "SHADOW ":
            _ok(f"{why}（{force!r}）→ 应开启", on)
        else:
            _ok(f"{why}（{force!r}）→ off，原样返回", not on)
    print()


# ---------------------------------------------------------------- E
def group_e() -> None:
    print("=" * 100)
    print("E 幂等：连续两次结果一致（索引化取样 ⇒ 确定性）")
    print("=" * 100)
    _FORCE["v"] = "shadow"
    a = bs.build_shadow(DATE)
    b = bs.build_shadow(DATE)
    _ok("两次 build_shadow 的 JSON 串相同", _j(a) == _j(b))
    r1 = bs.attach_shadow(_sample_report(), DATE)
    r2 = bs.attach_shadow(_sample_report(), DATE)
    _ok("两次 attach_shadow 的 JSON 串相同", _j(r1) == _j(r2))
    codes_a = list((a.get("portfolios", {}).get("market_ew", {}) or {}).get("codes") or [])
    _ok("B1 名单非空且为字符串代码列表",
        len(codes_a) == 50 and all(isinstance(c, str) for c in codes_a),
        f"共 {len(codes_a)} 只")
    print(f"  B1 名单前 5 只：{codes_a[:5]}")
    print()


# ---------------------------------------------------------------- F
def group_f() -> None:
    print("=" * 100)
    print("F 与 trade_plan.attach_plans 串联后 top_picks 仍不受影响（贴近生产路径）")
    print("=" * 100)
    _FORCE["v"] = "shadow"
    rep = _sample_report()
    p1 = trade_plan.attach_plans(copy.deepcopy(rep), cache_key=DATE)
    p2 = bs.attach_shadow(copy.deepcopy(p1), DATE)
    has_plan = any("plan" in x for x in (p1.get("top_picks") or []))
    _ok(f"attach_plans 已生效（{'含' if has_plan else '未含'} plan 字段，可能因无行情数据而静默跳过）",
        isinstance(p1, dict))
    _ok("串联后 top_picks JSON 串逐字节相同",
        _j(p2.get("top_picks")) == _j(p1.get("top_picks")))
    _ok("串联后仍只多 baseline_shadow 一个键",
        set(p2.keys()) - set(p1.keys()) == {bs.SHADOW_KEY})
    print()


# ---------------------------------------------------------------- G
def group_g() -> None:
    print("=" * 100)
    print("G 生产接线（静态检查 predictions.py）")
    print("=" * 100)
    if not os.path.exists(PRED_FILE):
        _ok("找到 predictions.py", False, PRED_FILE)
        print()
        return
    with open(PRED_FILE, encoding="utf-8") as f:
        src = f.read()
    n_def = src.count("def _with_baseline_shadow")
    n_use = src.count("_with_baseline_shadow(")
    _ok("定义了 _with_baseline_shadow（1 处）", n_def == 1, f"实际 {n_def}")
    _ok("在路由中串联（≥3 处：1 定义 + ≥2 调用）", n_use >= 3, f"实际 {n_use}")
    _ok("实现里调用了 baseline_shadow.attach_shadow",
        "baseline_shadow.attach_shadow" in src)
    _ok("实现里有静默降级（except 分支）",
        "生成影子对照失败" in src or "不影响榜单" in src)
    print()


def main() -> int:
    print("=" * 100)
    print("基准影子对照验收（plans/24 todo 18：把「打分选股」改为不决定持仓权重）")
    print("=" * 100)
    print(f"  参数名：{bs.PARAM}    模式：{list(bs.MODES)}    默认：{bs.MODE_OFF}")
    print()
    group_a()
    group_b()
    group_c()
    group_d()
    group_e()
    group_f()
    group_g()
    total = _OK["pass"] + _OK["fail"]
    print("=" * 100)
    print(f"结果：{_OK['pass']}/{total} 通过"
          + ("" if _OK["fail"] == 0 else f"，{_OK['fail']} 项失败 ✘"))
    print("=" * 100)
    print("判读：")
    print("  · A/D 通过 ⇒ 「默认关闭」是**可验证的事实**，不是承诺；")
    print("  · C 通过   ⇒ 影子对照**不可能**影响推荐结果（连 list 引用都未换）；")
    print("  · B/F 通过 ⇒ 开启后拿到的是可用的对照材料（B1/B2 + 门槛判定 + 差距）；")
    print("  · G 通过   ⇒ 接线确实在**生产代码路径**上，而非只在脚本里。")
    _FORCE["v"] = None
    return 1 if _OK["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
