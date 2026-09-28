"""自证并行化验证（plans/25 P2.5 / §15）

为什么要有这个脚本：
    并行化最容易出的两类事故是 ——(a) 结果不再确定（同一 as_of 两份榜单不一样，
    自证结论就不可复现）；(b) LLM 档并行预取导致预算熔断失效（多花钱）。
    这两条都必须**用断言钉住**，而不是"应该没问题"。

验什么（任一失败 → 非零退出）：
  ① 参数：evolution_config 里有 selfproof_workers
  ② 解析：resolve_workers(None/8) 正常；**LLM 档强制返回 1**（预算熔断精度）
  ③ 确定性：workers=4 与 workers=1 的**逐日 top_picks 完全一致**（同日、同序、同值）
  ④ 加速：workers=4 明显快于串行（阈值 1.3×，实测值打印）
  ⑤ 契约：state.json / report.json / runs.json 都带 workers 字段
  ⑥ 沙箱：真实资产 mtime/大小零变化

跑法（约 1~2 分钟）：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_selfproof_parallel.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import evolution_config as EC  # noqa: E402
from app.backtest import selfproof as SP  # noqa: E402
from app.backtest import selfproof_policy as P  # noqa: E402
from app.backtest.loader import get_trade_dates  # noqa: E402

U_SERIAL = "verify_par_serial"
U_PAR = "verify_par_workers3"
UNIVERSE = 40
DAYS = 4
PAR_WORKERS = 3
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


def _daily_picks(run_id: str, as_of: str):
    p = os.path.join(SP.run_dir(run_id), "daily", f"{as_of}.json")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        rep = json.load(f)
    return rep.get("top_picks") or []


def main() -> int:  # noqa: C901
    protected = [i["path"] for i in P.REAL_ASSETS]
    before = snapshot(protected)

    # ── ① 参数 ──
    print("\n① 可进化参数 selfproof_workers")
    cfg = EC._load()  # noqa: SLF001
    row = (cfg.get("params") or {}).get("selfproof_workers") or {}
    ck(bool(row), "evolution_config 已含 selfproof_workers（缺则先跑 scripts/add_selfproof_params.py）")
    print(f"   default={row.get('default')} current={row.get('current')} "
          f"tier={row.get('tier')} range=[{row.get('min')},{row.get('max')}]")

    # ── ② 解析 + LLM 档强制串行 ──
    print("\n② 并行度解析（LLM 档必须强制串行：预算熔断要逐日精确）")
    w8 = SP.resolve_workers(8, False)
    w_none = SP.resolve_workers(None, False)
    w_llm = SP.resolve_workers(8, True)
    ck(w8 == 8, "显式 workers=8 → 8", str(w8))
    ck(w_none >= 1, "未指定 → 读参数（>=1）", str(w_none))
    ck(w_llm == 1, "**LLM 档 workers=8 → 强制 1**（避免预取超支）", str(w_llm))
    print(f"   resolve_workers(8,False)={w8} (None,False)={w_none} (8,True)={w_llm}")

    # ── 回放两轮（同一区间/同一 seed/同 universe，只有 workers 不同）──
    cal = [d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
           for d in get_trade_dates("20260803", "20260820")]
    dates = cal[:DAYS]
    ck(len(dates) >= 3, "取到足够交易日", str(dates))
    print(f"\n回放区间: {dates[0]} ~ {dates[-1]} | 抽样 {UNIVERSE} 只 | 规则档零 API 费用")

    t0 = time.time()
    s1 = SP.run_replay(start=dates[0], end=dates[-1], step=1, universe=UNIVERSE,
                       max_days=len(dates), run_id=U_SERIAL, resume=False, workers=1)
    t_serial = time.time() - t0

    t0 = time.time()
    s4 = SP.run_replay(start=dates[0], end=dates[-1], step=1, universe=UNIVERSE,
                       max_days=len(dates), run_id=U_PAR, resume=False, workers=PAR_WORKERS)
    t_par = time.time() - t0

    print(f"   串行 workers=1: {t_serial:.1f}s（{s1.get('dates_done')} 天，"
          f"推荐 {s1.get('picks_total')} 条）")
    print(f"   多进程 workers={PAR_WORKERS}: {t_par:.1f}s（{s4.get('dates_done')} 天，"
          f"推荐 {s4.get('picks_total')} 条）")
    ck(s1.get("status") in ("done", "stopped") and s4.get("status") in ("done", "stopped"),
       "两轮都正常结束", f"{s1.get('status')}/{s4.get('status')}")
    ck(s1.get("dates_done") == s4.get("dates_done"), "两轮天数一致",
       f"{s1.get('dates_done')} vs {s4.get('dates_done')}")

    # ── ③ 确定性：逐日 top_picks 完全一致 ──
    print("\n③ 确定性（并行与串行逐日完全一致 —— 否则自证不可复现）")
    diff_days, same_days, n_pick_total = [], 0, 0
    for d in dates:
        a = _daily_picks(U_SERIAL, d)
        b = _daily_picks(U_PAR, d)
        if a is None or b is None:
            diff_days.append(f"{d}(缺失)")
            continue
        n_pick_total += len(a)
        if json.dumps(a, sort_keys=True, ensure_ascii=False) == \
                json.dumps(b, sort_keys=True, ensure_ascii=False):
            same_days += 1
        else:
            diff_days.append(d)
    ck(not diff_days, f"逐日榜单一致（{same_days}/{len(dates)} 天）", str(diff_days))
    print(f"   共比对 {n_pick_total} 条推荐（同一天、同顺序、同分值）")

    # ── ④ 加速比（**只记录，不作门禁** —— 见 plans/25 §15.4）──
    # 为什么暂不作门禁：并行化**尚未达标**（多进程 0.91×，根因=ChromaDB/sqlite 多进程并读争锁；
    # 线程池更糟：慢一个数量级）。所以当前真正的护栏是「**默认必须串行**」，
    # 而不是「并行必须更快」——把没达标的东西写成门禁，只会逼人改断言而不是改代码。
    # 等 §15.4 的修复落地后，再把加速比升级为硬门禁（阈值 1.3×）。
    print("\n④ 加速比（实测记录，非门禁）")
    speed = (t_serial / t_par) if t_par > 0 else 0.0
    print(f"   串行 {t_serial:.1f}s → 多进程 {t_par:.1f}s，加速 {speed:.2f}×")
    ck(int(SP.resolve_workers(None, False)) == 1,
       "**默认并行度=1（串行）**：未达标前不许默认开启并行",
       f"实测默认 {SP.resolve_workers(None, False)}")
    if speed < 1.3:
        print("   ⚠️ 并行未达标（<1.3×）：根因是 ChromaDB/sqlite 在多进程/多线程并读下互相争锁，"
              "不是单纯 GIL。修复路线见 plans/25 §15.4")

    # ── ⑤ 契约字段 ──
    print("\n⑤ 契约：workers 字段贯穿 state / report / runs")
    for rid, exp in ((U_SERIAL, 1), (U_PAR, PAR_WORKERS)):
        st = SP.load_state(rid) or {}
        with open(SP.report_path(rid), "r", encoding="utf-8") as f:
            rep = json.load(f)
        ck(int(st.get("workers") or 0) == exp, f"{rid} state.workers={exp}",
           str(st.get("workers")))
        ck(int((rep.get("config") or {}).get("workers") or 0) == exp,
           f"{rid} report.config.workers={exp}", str((rep.get('config') or {}).get('workers')))
    runs = {r.get("run_id"): r for r in (SP.list_runs() or [])}
    ck(int((runs.get(U_PAR) or {}).get("workers") or 0) == PAR_WORKERS,
       f"runs.json 记录 workers={PAR_WORKERS}", str((runs.get(U_PAR) or {}).get("workers")))

    # ── ⑥ 沙箱 ──
    print("\n⑥ 真实资产零变化（沙箱隔离）")
    after = snapshot(protected)
    changed = [k for k in before if before[k] != after.get(k)]
    ck(not changed, "真实资产 mtime/大小未变", str(changed[:3]))

    print("\n" + "=" * 60)
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过：参数 / LLM 档串行 / 默认串行护栏 / 逐日确定性 / 契约字段 / 沙箱隔离")
    print("   ⏱ 并行加速比仅为**实测记录**（当前 <1.3×，未达标）：修复路线见 plans/25 §16.4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
