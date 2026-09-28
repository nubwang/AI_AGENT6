"""自证测试护栏验证（plans/25 P0 / §五 G3 / §十三 F1·F2·F4·F8）

验什么（任一失败 → 非零退出）：
  ① 保护区清单覆盖规划点名的真实资产（monitor_state / daily_recommend / decision_records /
     verify_kb / reflect_kb / evolution_config / evolution_ledger）
  ② 写入守卫：`assert_not_real_asset()` 对保护区一律抛错；沙箱路径放行；前缀绕过（../）也拦住
  ③ `write_json_guarded()` 真的写不进保护区（并且不会误改文件）
  ④ 前视清单覆盖 F2 点名的每一项（tavily / policy_kb / reflect_kb / verify_kb / EKB / news_kb /
     attribution_kb / condition_table / probability_table / feature_normalizer /
     form_leaderboard / entry_guidance / 形态模式库）
  ⑤ 主指标继承宪法口径 = excess_vs_market（F8）
  ⑥ 术语护栏：禁用表述已登记（F1）
  ⑦ IS/OOS：ratio 在 (0.5, 0.95)；year_kfold 可复现；冻结年份优先
  ⑧ 护栏模块自身**不写任何真实资产**（跑完对比 mtime 快照）

跑法：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_selfproof_isolation.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import selfproof_policy as P  # noqa: E402

FAILS: list[str] = []


def ck(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label} {detail}")
        FAILS.append(label)


def snapshot(paths: list[str]) -> dict:
    out = {}
    for p in paths:
        try:
            st = os.stat(p)
            out[p] = (st.st_mtime, st.st_size)
        except OSError:
            out[p] = None
    return out


def main() -> int:
    protected = [i["path"] for i in P.REAL_ASSETS]
    before = snapshot(protected)

    # ── ① 保护区清单完整性 ──
    print("\n① 保护区清单")
    need_keys = ["monitor_state.json", "daily_recommend", "decision_records.json",
                 "verify_kb.json", "reflect_kb.json", "evolution_config.json",
                 "evolution_ledger.json"]
    joined = " | ".join(protected)
    for k in need_keys:
        ck(k in joined, f"保护区覆盖 {k}")
    ck(len(P.SHARED_ASSETS_WITH_SOURCE) >= 1
       and any(i.get("require_source") == "selfproof" for i in P.SHARED_ASSETS_WITH_SOURCE),
       "共享资产（事件流）要求 source=selfproof")

    # ── ② 写入守卫 ──
    print("\n② 写入守卫")
    for item in P.REAL_ASSETS:
        p = item["path"]
        raised = False
        try:
            P.assert_not_real_asset(os.path.join(p, "x.json") if item["kind"] == "dir" else p)
        except P.RealAssetWriteError:
            raised = True
        ck(raised, f"拦截写入 {os.path.basename(p)}")
    # 沙箱放行
    ok_sandbox = True
    for cand in ("daily/20260503.json", "runs/run1/report.json", "regime_match.json"):
        try:
            P.assert_not_real_asset(P.sandbox_path(*cand.split("/")))
        except Exception:  # noqa: BLE001
            ok_sandbox = False
    ck(ok_sandbox, "沙箱路径全部放行")
    ck(P.is_sandboxed(P.sandbox_path("a", "b.json")), "is_sandboxed 判定正确")
    # 「../」解析后指向真实资产 → 必须拦住
    ok_up = False
    try:
        P.assert_not_real_asset(os.path.join(P.SANDBOX_ROOT, "..", "monitor_state.json"))
    except P.RealAssetWriteError:
        ok_up = True
    ck(ok_up, "「data/selfproof/../monitor_state.json」被拦截（normpath 生效）")
    # 反向检查：同名前缀目录**不能**被误判为保护区（避免过度拦截导致回放写不进沙箱）
    not_over_blocked = True
    try:
        P.assert_not_real_asset(os.path.join(P.DATA_DIR, "daily_recommend_evil", "x.json"))
    except P.RealAssetWriteError:
        not_over_blocked = False
    ck(not_over_blocked, "同名前缀目录不被误判（避免过度拦截）")

    # ── ③ 带护栏写盘 ──
    print("\n③ 带护栏写盘")
    target = os.path.join(P.DATA_DIR, "monitor_state.json")
    raised = False
    try:
        P.write_json_guarded(target, {"evil": True})
    except P.RealAssetWriteError:
        raised = True
    ck(raised, "write_json_guarded 对真实资产**抛错**（不静默返回 False，避免掩盖 bug）")
    tmp_probe = P.sandbox_path("_verify_probe.json")
    ok2 = P.write_json_guarded(tmp_probe, {"ok": True})
    ck(ok2 is True and os.path.exists(tmp_probe), "write_json_guarded 允许写沙箱")
    try:
        os.remove(tmp_probe)
    except OSError:
        pass

    # ── ④ 前视清单完整性（F2）──
    print("\n④ 前视清单（F2：注入 LLM 的知识可否 as-of 过滤）")
    keys = {i["key"] for i in P.FORWARD_LOOK_ITEMS}
    for k in ("tavily", "policy_kb", "reflect_kb", "verify_kb", "ekb", "news_kb",
              "attribution_kb", "condition_table", "probability_table",
              "feature_normalizer", "form_leaderboard", "entry_guidance", "pattern_library"):
        ck(k in keys, f"清单覆盖 {k}")
    for it in P.FORWARD_LOOK_ITEMS:
        if it["filterable"] and it["action"] in ("filter", "asof_rebuild"):
            ck(bool(it.get("field")), f"{it['key']} 声明了过滤字段")
    flags = P.forward_look_flags(as_of="20260503")
    ck("tavily" in flags["forbidden"], "Tavily 在回放中被判为 forbidden（必须关闭）")
    ck(bool(flags["badged"]) and not flags["ok"],
       "不可过滤项被登记为 badged 且 ok=False（报告必须显式声明）",
       json.dumps(flags["badged"], ensure_ascii=False))

    # ── ⑤ 主指标（F8）──
    print("\n⑤ 主指标口径")
    mm = P.main_metric()
    ck(mm["hit_definition"] == "excess_vs_market",
       "主指标 = excess_vs_market（继承 evolution_config.main_metric）", str(mm))
    ck(mm["immutable"] is True, "主指标标记 immutable（自证不得自改口径）")

    # ── ⑥ 术语护栏（F1）──
    print("\n⑥ 术语护栏")
    ck("完全复刻每日推荐" in P.TERMINOLOGY["forbidden_claims"],
       "禁用表述已登记（回放≠复刻当年系统）")
    ck("重演" in P.TERMINOLOGY["replay_metric_name"], "指标名使用『重演口径』",
       P.TERMINOLOGY["replay_metric_name"])
    ck("不等于" in P.TERMINOLOGY["canonical_statement"], "口径声明含『不等于当年成绩』")

    # ── ⑦ IS/OOS（F4）──
    print("\n⑦ IS/OOS 分离")
    r = P.split_ratio()
    ck(0.5 < r < 0.95, f"ratio 落在护栏区间 (0.5,0.95)：{r}")
    kf_a = [y for y in range(2015, 2027) if P.is_oos_year(y, fold=0)]
    kf_b = [y for y in range(2015, 2027) if P.is_oos_year(y, fold=0)]
    ck(kf_a == kf_b and len(kf_a) >= 1, "year_kfold 可复现且非空", str(kf_a))
    ck("解耦" in P.oos_note(), "OOS 与实盘反馈解耦已声明")

    # ── ⑧ 护栏模块自身不写真实资产 ──
    print("\n⑧ 护栏模块自身零副作用")
    after = snapshot(protected)
    ck(before == after, "运行前后真实资产 mtime/大小未变",
       str({k: (before[k], after[k]) for k in before if before[k] != after[k]}))

    print("\n" + "=" * 60)
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过：\n  - " + "\n  - ".join(FAILS))
        return 1
    print("✅ 全部通过：保护区 / 写入守卫 / 前视清单 / 主指标 / 术语 / IS-OOS / 零副作用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
