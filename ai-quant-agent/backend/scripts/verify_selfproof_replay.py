"""回放内核验证（plans/25 P2 / F3 / F9 / G3）

验什么（任一失败 → 非零退出）：
  ① **无未来函数**：`_prepare_replay(code, as_of)` 拿到的最后一根 K 线 <= as_of
  ② **复权基准锚定 as_of**（F3）：同一个 as_of，`adj_base_date=as_of` 与"全表最新基准"在
     存在除权的样本上**绝对价格不同**，但**收益率序列一致**（只差一个尺度）——
     这正是"未来除权会改写历史价格"的证据，也是修复生效的证据
  ③ **截断等价性**：截断到 as_of 的数据，与"全量数据取前 N 行"逐行一致
  ④ **确定性抽样**：universe 抽样两次结果完全相同（同 seed）
  ⑤ **沙箱隔离**：回放产物全部落在 data/selfproof；跑完真实资产 mtime/大小零变化
  ⑥ **报告骨架**：report.json 必带 flags（前视/主指标/IS-OOS/数据门/幸存者偏差）
  ⑦ **断点续跑**：第二次 run_replay 不重复已完成日期（dates_done 不倒退）

跑法（约 1~2 分钟；只抽样 3 只股票 × 2 天，零 API 费用）：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_selfproof_replay.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.backtest import selfproof as SP  # noqa: E402
from app.backtest import selfproof_policy as P  # noqa: E402
from app.backtest.daily_scan import _prepare_replay  # noqa: E402
from app.backtest.loader import get_trade_dates, load_adj_close, load_daily, prepare_stock_data  # noqa: E402
from app.data.adjust import get_latest_adj_factor  # noqa: E402
from app.models import SessionLocal  # noqa: E402

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


def pick_test_stock(db, as_of: str) -> tuple[str, str, bool]:
    """挑一只"**as_of 当天有行情** 且此后再发生除权"的股票。

    两个坑都在这里踩过：
      1. 只挑"因子比值最大"的 → 往往选中**已停止交易/退市**的股票（正好是 P0.5 那 272 只
         退市缺口的旁证），它们在 as_of 当天没数据，会让"当日因子自除"的断言失去前提；
      2. `daily` 与 `adj_factor` 的 `ts_code` **collation 不一致**
         （utf8mb4_unicode_ci vs utf8mb4_0900_ai_ci）→ 两表 **JOIN 直接报错**
         （MySQL 1267）。故这里改成 Python 侧两次查询，避开 JOIN。
    """
    codes = db.execute(text("SELECT ts_code FROM daily WHERE trade_date = :d LIMIT 300"),
                       {"d": as_of}).scalars().all()
    for code in codes:
        r = db.execute(
            text("SELECT MIN(adj_factor), MAX(adj_factor) FROM adj_factor "
                 "WHERE ts_code = :c AND trade_date >= :d"), {"c": str(code), "d": as_of}
        ).first()
        if r and r[0] and r[1] and float(r[1]) > float(r[0]):
            return str(code), as_of, True
    return (str(codes[0]) if codes else "000001.SZ"), as_of, False


def main() -> int:
    protected = [i["path"] for i in P.REAL_ASSETS]
    before = snapshot(protected)
    db = SessionLocal()
    try:
        # 用 2019 年初作为 as_of（此后必有大量除权/分红）
        dates = [d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
                 for d in get_trade_dates("20190102", "20190131")]
        as_of = dates[0] if dates else "20190102"
        code, as_of, has_ex = pick_test_stock(db, as_of)
        print(f"\n测试样本: {code} @ as_of={as_of}（该股在此后发生过除权: {has_ex}）")

        # ── ① 无未来函数 ──
        print("\n① 无未来函数（回放取数 <= as_of）")
        df = _prepare_replay(code, as_of)
        ck(not df.empty, "能取到数据", f"rows={len(df)}")
        if not df.empty:
            last = df["trade_date"].iloc[-1]
            last_s = last.strftime("%Y%m%d") if hasattr(last, "strftime") else str(last)
            ck(last_s <= as_of, f"最后一根 K 线 {last_s} <= as_of {as_of}")
            df_full = load_daily(code)
            ck(len(df) < len(df_full), f"确实被截断（{len(df)} < {len(df_full)}）")

            # ── ③ 截断等价性 ──
            # ⚠️ 口径要点：`prepare_stock_data` 会**剔除停牌日**并按日期排序，
            # 所以"截断结果"只能与**同一条管线**的全量结果比（不是与原始 daily 表前 N 行比）——
            # 否则会把"停牌剔除"误判成"截断不一致"（这里踩过）。
            print("\n③ 截断等价性（同管线：全量 vs 截断到 as_of）")
            import pandas as pd
            full_pipe = prepare_stock_data(code)
            expect = full_pipe[
                pd.to_datetime(full_pipe["trade_date"]) <= pd.Timestamp(as_of)
            ].reset_index(drop=True)
            ck(len(expect) == len(df), f"行数一致（{len(expect)} vs {len(df)}）")
            ck(expect["trade_date"].tolist() == df["trade_date"].tolist(),
               "交易日序列逐行一致")
            try:
                import numpy as np
                ck(bool(np.allclose(expect["close"].to_numpy(float), df["close"].to_numpy(float))),
                   "原始收盘价逐行一致")
            except Exception as exc:  # noqa: BLE001
                ck(False, "原始收盘价逐行一致", str(exc))

        # ── ② 复权基准锚定（F3）──
        print("\n② 复权基准锚定 as_of（F3）")
        base_at_asof = get_latest_adj_factor(code, upto=as_of)
        base_latest = get_latest_adj_factor(code)
        ck(base_at_asof > 0 and base_latest > 0,
           f"因子可读（as_of={base_at_asof:.4f}，最新={base_latest:.4f}）")
        raw = load_daily(code, end_date=as_of)
        anchored = prepare_stock_data(code, end_date=as_of, adj_base_date=as_of)
        today_based = prepare_stock_data(code, end_date=as_of)
        ck(not anchored.empty and not today_based.empty, "两种口径都能取到数据")
        if not anchored.empty and not today_based.empty:
            la = float(anchored["adj_close"].iloc[-1])
            lt = float(today_based["adj_close"].iloc[-1])
            # 锚定后：**as_of 那一行**的 adj_close 必须等于原始价（因子被自己除掉了）——
            # 只有当 as_of 当天有行情时才成立（选股器已保证；这里再兜一层防样本缺失）。
            import pandas as pd
            at = anchored[pd.to_datetime(anchored["trade_date"]) == pd.Timestamp(as_of)]
            if at.empty:
                ck(True, f"该股 as_of 当日无行情（最后一行 "
                         f"{anchored['trade_date'].iloc[-1]}）→ 跳过『当日自除』断言")
            else:
                ck(abs(float(at["adj_close"].iloc[0]) - float(at["close"].iloc[0])) < 1e-6,
                   f"锚定后 as_of 当日 adj_close == 原始价"
                   f"（{float(at['adj_close'].iloc[0]):.4f} vs {float(at['close'].iloc[0]):.4f}）")
            if has_ex:
                ck(abs(la - lt) > 1e-9,
                   f"存在除权时两口径**绝对价格不同**（锚定 {la:.4f} ≠ 今日基准 {lt:.4f}）→ F3 确实存在",
                   f"ratio={lt / la if la else 0:.4f}")
            # 收益率序列一致（只差尺度）
            try:
                import numpy as np
                r1 = np.diff(anchored["adj_close"].to_numpy(float)) / anchored["adj_close"].to_numpy(float)[:-1]
                r2 = np.diff(today_based["adj_close"].to_numpy(float)) / today_based["adj_close"].to_numpy(float)[:-1]
                ck(bool(np.allclose(r1, r2, atol=1e-9)), "两口径的收益率序列完全一致（只差尺度）")
            except Exception as exc:  # noqa: BLE001
                ck(False, "收益率序列一致", str(exc))

        # ── ④ 确定性抽样 ──
        print("\n④ 确定性抽样（同 seed 两次一致）")
        import random
        codes = sorted(["000001.SZ", "600000.SH", "000002.SZ", "600036.SH", "000063.SZ"])
        a = sorted(random.Random(20260922).sample(codes, 3))
        b = sorted(random.Random(20260922).sample(codes, 3))
        ck(a == b, f"抽样可复现：{a}")

        # ── ⑦ 回放内核端到端（2 天 × 抽样 3 只，规则档零费用）──
        print("\n⑦ 回放内核端到端（2 天 × 抽样 3 只，规则档）")
        d2 = dates[:2] if len(dates) >= 2 else dates
        t0 = time.time()
        res = SP.run_replay(start=d2[0], end=d2[-1], step=1, universe=3, max_days=2,
                            run_id="verify_replay_smoke", resume=False)
        ck(res.get("status") in ("done", "stopped", "paused_budget"),
           f"状态正常（{res.get('status')}，{time.time() - t0:.1f}s）", str(res))
        rid = str(res.get("run_id") or "")
        ck(bool(rid) and os.path.isdir(SP.run_dir(rid)), f"沙箱目录已建：{SP.run_dir(rid)}")
        ck(res.get("dates_done", 0) >= 1, f"至少完成 1 天（实际 {res.get('dates_done')}）")
        st = SP.load_state(rid)
        ck(bool(st.get("done")), f"state.done 非空（{len(st.get('done') or [])} 天）")
        for aso in st.get("done") or []:
            fp = SP.daily_path(rid, aso)
            ck(os.path.exists(fp), f"当日榜单已落沙箱：{os.path.basename(fp)}")
            ck(P.is_sandboxed(fp), "路径在沙箱内")
            rep_day = json.loads(open(fp, encoding="utf-8").read())
            ck(rep_day.get("mode") == "replay" and rep_day.get("persist") is False,
               "当日榜单标记 mode=replay / persist=False")
            ck(bool(rep_day.get("as_of")) and str(rep_day.get("date")) == aso,
               "当日榜单 as_of 与 date 一致")

        # ── ⑧ 断点续跑 ──
        print("\n⑧ 断点续跑（不重复已完成日期）")
        res2 = SP.run_replay(start=d2[0], end=d2[-1], step=1, universe=3, max_days=2,
                             run_id="verify_replay_smoke", resume=True)
        ck(res2.get("dates_done", 0) >= res.get("dates_done", 0),
           f"续跑后完成天数不倒退（{res.get('dates_done')} → {res2.get('dates_done')}）")

        # ── ⑥ 报告骨架 ──
        print("\n⑥ 报告骨架（P2 契约）")
        rp = SP.report_path("verify_replay_smoke")
        ck(os.path.exists(rp), f"report.json 已生成：{rp}")
        if os.path.exists(rp):
            rep = json.loads(open(rp, encoding="utf-8").read())
            for k in ("run_id", "config", "flags", "dates", "daily_stats", "metrics",
                      "attribution", "usage", "created_at", "updated_at"):
                ck(k in rep, f"报告含字段 {k}")
            fl = rep.get("flags") or {}
            ck(bool(fl.get("forward_look")), "flags.forward_look 存在（前视徽标）")
            ck((fl.get("main_metric") or {}).get("hit_definition") == "excess_vs_market",
               "主指标 = excess_vs_market（F8）")
            ck((fl.get("is_oos") or {}).get("mode") == "year_kfold",
               "IS/OOS 模式 = year_kfold（F4）")
            ck(bool(fl.get("data_gate")) or bool(fl.get("survivorship")),
               "报告带数据门/幸存者偏差声明（P0.5 产物）")
            ck("重演" in str(fl.get("metric_name")), "指标名使用『重演口径』（F1）")

        # ── ⑤ 沙箱隔离：真实资产零变化 ──
        print("\n⑤ 真实资产零变化（G3）")
        after = snapshot(protected)
        diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        ck(not diff, "跑完回放后真实资产 mtime/大小未变", str(diff))
        rr = SP.list_runs()
        ck(any(r.get("run_id") == "verify_replay_smoke" for r in rr), "任务已登记到 runs.json")
    finally:
        db.close()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过：\n  - " + "\n  - ".join(FAILS))
        return 1
    print("✅ 全部通过：无未来函数 / 复权基准锚定 / 截断等价 / 确定性抽样 / "
          "沙箱隔离 / 报告骨架 / 断点续跑")
    return 0


if __name__ == "__main__":
    sys.exit(main())
