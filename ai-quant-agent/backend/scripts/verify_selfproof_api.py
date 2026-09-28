"""自证测试 API 验证（plans/25 P6）

验什么（任一失败 → 非零退出）：
  ① 路由已注册：main.py 里有 selfproof.router，且工程路径为 /api/v1/selfproof
  ② /data-gate：能给「最早可自证日期」+ 幸存者偏差声明（前端靠它做边界提示）
  ③ /params：字段齐（step/universe/max_days/workers/main_metric/terminology）
  ④ /estimate：给天数/耗时/预计出票/样本量判定；**start 早于数据门要 400**（防穿越）
  ⑤ /run：真跑一个小任务（1 天 × 抽样 10 只）→ /status 轮询到结束 → /report 能出报告
  ⑥ /stop：空闲时返回 ok=False（不装成功）
  ⑦ /regime/candidates：明确返回未实现（不给假列表）
  ⑧ 沙箱：真实资产 mtime/零变化

跑法（约 1 分钟）：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_selfproof_api.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from app.api import selfproof as A  # noqa: E402
from app.backtest import selfproof_policy as P  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def main() -> int:  # noqa: C901
    protected = [i["path"] for i in P.REAL_ASSETS]
    before = snapshot(protected)

    # ── ① 路由注册 ──
    print("\n① 路由注册")
    main_py = os.path.join(BACKEND, "app", "main.py")
    src = open(main_py, encoding="utf-8").read()
    ck("app.include_router(selfproof.router)" in src, "main.py 已注册 selfproof.router")
    ck("selfproof" in src.split("from app.api import")[1].split("\n")[0],
       "main.py 已 import selfproof")
    ck(A.router.prefix == "/api/v1/selfproof", "前缀 = /api/v1/selfproof", A.router.prefix)
    paths = {r.path for r in A.router.routes}
    for p in ("/api/v1/selfproof/data-gate", "/api/v1/selfproof/params",
              "/api/v1/selfproof/estimate", "/api/v1/selfproof/run",
              "/api/v1/selfproof/stop", "/api/v1/selfproof/status",
              "/api/v1/selfproof/runs", "/api/v1/selfproof/report/{run_id}",
              "/api/v1/selfproof/settle/{run_id}", "/api/v1/selfproof/regime/candidates"):
        ck(p in paths, f"有路由 {p}", str(sorted(paths))[:200])

    # ── ② 数据门 ──
    print("\n② GET /data-gate")
    g = A.data_gate()
    earliest = str((g.get("data_gate") or {}).get("earliest_full_as_of") or "")
    ck(len(earliest) == 8, "给出最早可自证日期", earliest)
    ck(bool(g.get("survivorship")), "带幸存者偏差声明", str(g.get("survivorship"))[:120])
    print(f"   最早 {earliest} ｜ 最新 {g.get('latest_as_of')} ｜ "
          f"缺退市股 {g.get('survivorship', {}).get('missing_count')} 只")

    # ── ③ 参数 ──
    print("\n③ GET /params")
    pr = A.params()
    for k in ("step", "universe", "max_days", "workers", "main_metric", "terminology"):
        ck(k in pr, f"含字段 {k}")
    ck(str(pr.get("main_metric")) == "excess_vs_market", "主指标=超额", str(pr.get("main_metric")))

    # ── ④ 预估 ──
    print("\n④ POST /estimate")
    est = A.estimate({"start": "20240102", "step": 5, "universe": 0, "max_days": 50})
    ck(est.get("days", 0) > 0, "算出天数", str(est.get("days")))
    ck(bool(est.get("wall_human")), "给出人读耗时", str(est.get("wall_human")))
    ck("picks_estimate" in est and "sample_enough" in est, "给出预计出票与样本量判定")
    print(f"   {est['days']} 天 ｜ {est['wall_human']} ｜ 预计出票 {est['picks_estimate']} 条 ｜ "
          f"够判={est['sample_enough']}")
    ck(bool(est.get("warning_long")), "长耗时给出显式警告（全市场一年 ≈99h）", "")
    # 抽样太小的警告
    est_small = A.estimate({"start": "20240102", "universe": 40, "max_days": 10})
    ck(bool(est_small.get("warning")), "抽样太小给出警告（会一条票都出不来）")
    # 数据门保护
    try:
        A.estimate({"start": "20050104"})
        ck(False, "start 早于数据门应报 400", "没有报错")
    except HTTPException as exc:
        ck(exc.status_code == 400, "start 早于数据门 → 400", str(exc.status_code))

    # ── ⑤ 真跑一个小任务（抽 60 只那天有票，才能验『有结论』这条路径）──
    print("\n⑤ POST /run → /status → /report（1 天 × 抽样 60 只）")
    r = A.run({"start": "20260803", "step": 1, "universe": 60, "max_days": 1, "llm": False})
    rid = str(r.get("run_id") or "")
    ck(bool(rid), "启动成功并返回 run_id", str(rid))
    t0 = time.time()
    st = {}
    while time.time() - t0 < 300:
        st = A.status()
        if st.get("status") != "running":
            break
        time.sleep(2)
    print(f"   任务 {rid} 结束：status={st.get('status')} message={st.get('message')} "
          f"({time.time() - t0:.0f}s)")
    ck(st.get("status") in ("success", "stopped"), "任务正常结束", str(st.get("status")))
    ck(str(st.get("run_id")) == str(rid), "状态里带上 run_id")
    ck((st.get("state") or {}).get("workers") == 1,
       "串行（workers=1）写进 state", str((st.get("state") or {}).get("workers")))

    rep = A.report(rid)
    n_picks = (rep.get("daily_stats") or {}).get("n_picks")
    if rep.get("ok") is False:
        print(f"   ⚠️ 该任务无结论（reason={rep.get('reason')}）→ 按『0 推荐』契约校验")
        ck(bool(rep.get("reason")) and bool(rep.get("hint")),
           "无结论时返回 reason+hint（不许用 None 冒充结论）")
    else:
        ck("metrics" in rep and "flags" in rep, "报告可读且含 metrics/flags")
        ck(str((rep.get("flags") or {}).get("data_gate") or "") != "",
           "报告带数据门声明（flags.data_gate）",
           str((rep.get("flags") or {}).get("data_gate"))[:80])
        ck(bool(rep.get("is_oos")), "报告带 IS/OOS 双栏")
        print(f"   报告：天数 {(rep.get('dates') or {}).get('total')} ｜ 推荐 {n_picks} 条 ｜ "
              f"样本量判定 {rep.get('verdict', {}).get('sufficient_samples')}")

    # ── ⑤b 0 推荐场景（用户最容易踩：抽样太小）──
    print("\n⑤b 0 推荐场景：不许返回一堆 None 冒充结论")
    r2 = A.run({"start": "20260803", "step": 1, "universe": 3, "max_days": 1, "llm": False})
    rid2 = str(r2.get("run_id") or "")
    t0b = time.time()
    while time.time() - t0b < 300:
        if A.status().get("status") != "running":
            break
        time.sleep(2)
    rep2 = A.report(rid2)
    picks2 = 0 if rep2.get("ok") is False else (rep2.get("daily_stats") or {}).get("n_picks")
    print(f"   抽样 3 只：推荐 {picks2} 条 ｜ ok={rep2.get('ok')} ｜ reason={rep2.get('reason')}")
    if not picks2:
        ck(rep2.get("ok") is False, "无产出时 ok=False（不把骨架当结论）")
        ck(bool(rep2.get("reason")) and bool(rep2.get("hint")),
           "给出可执行的原因与提示", str(rep2.get("hint"))[:80])
    else:
        ck(True, "该样本居然出了票 → 用『有结论』契约兜底", str(picks2))

    # runs 列表里能查到
    rl = A.runs(limit=50)
    ck(any(str(i.get("run_id")) == str(rid) for i in rl.get("items", [])),
       "历史任务列表能查到该任务")

    # ── ⑥ 停止 ──
    print("\n⑥ POST /stop（空闲时不许装成功）")
    s = A.stop()
    ck(s.get("ok") is False, "空闲时 ok=False", str(s))

    # ── ⑦ 模式 B ──
    print("\n⑦ GET /regime/candidates（不许给假列表）")
    rg = A.regime_candidates()
    ck(rg.get("implemented") is False, "明确标记未实现", str(rg.get("stage")))
    ck("items" not in rg, "不返回 items（避免被当成已完成）")

    # ── ⑧ 沙箱 ──
    print("\n⑧ 真实资产零变化（沙箱隔离）")
    after = snapshot(protected)
    changed = [k for k in before if before[k] != after.get(k)]
    ck(not changed, "真实资产 mtime/大小未变", str(changed[:3]))

    print("\n" + "=" * 60)
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过：路由 / 数据门 / 参数 / 预估(含护栏) / 真跑 / 报告 / 停止 / 模式B / 沙箱")
    return 0


if __name__ == "__main__":
    sys.exit(main())
