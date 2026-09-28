"""P4 试跑放大：回放耗时**两点标定** → 外推全市场（plans/25 §十三 C 组 / P4）

为什么必须跑这个：
    P2 的 smoke 只跑了「抽样 3 只 × 2 天 = 2.4s」，这个数字**无法回答**
    「起始日自证到今天（全市场 × 250 天）要跑多久」。P2.5 要不要做并行化，
    完全取决于这条外推曲线 —— 所以先在中等规模上做两点标定，拿到
    「每只股票每天多少秒（斜率）」和「每天固定开销多少秒（截距）」，
    再外推，而不是拍脑袋。

方法（两点定直线）：
    t(u) = intercept + slope * u        # u = 抽样只数
    跑 u=small 与 u=large 各 days 天（同一起始日、同一 seed），
    解出 slope / intercept，再外推到 u=全市场 与 u=selfproof_universe_sample 默认值。

安全：
    - 全程 persist=False（不写真实榜单/台账）；
    - 落盘只在 data/selfproof/ 沙箱；
    - 规则档，零 LLM 费用。

跑法：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/selfproof_scale_trial.py
    # 更狠一点：40 只 × 30 天
    cd ai-quant-agent/backend && ./venv/bin/python scripts/selfproof_scale_trial.py --days 30 --large 40
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import selfproof as SP  # noqa: E402


def market_size() -> int:
    """stock_basic 行数 ≈ 当前可交易 + 已上市未退市股票数（受 F7 幸存者偏差影响，见数据门）。"""
    try:
        from sqlalchemy import text

        from app.models import SessionLocal
        db = SessionLocal()
        try:
            return int(db.execute(text("SELECT COUNT(*) FROM stock_basic")).scalar() or 0)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 读 stock_basic 失败（用 5400 兜底）: {exc}")
        return 5400


def one_run(label: str, start: str, days: int, universe: int, seed: int) -> dict:
    """跑一次回放并返回实测耗时（独立 run_id，resume=False 保证干净）。"""
    rid = f"scale_trial_{label}_{universe}x{days}"
    t0 = time.time()
    res = SP.run_replay(start=start, end="", step=1, universe=universe,
                        max_days=days, run_id=rid, resume=False, llm=False)
    wall = time.time() - t0
    n_done = int(res.get("dates_done") or 0)
    print(f"  {label}: universe={universe:>4d} days={n_done:>3d} "
          f"picks={res.get('picks_total'):>4d} 墙钟={wall:.1f}s "
          f"内核记账={res.get('elapsed_sec')}s")
    return {"label": label, "universe": universe, "days": n_done,
            "wall_sec": wall, "picks": int(res.get("picks_total") or 0),
            "run_id": rid, "status": res.get("status")}


def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20190102", help="as_of 起始日（YYYYMMDD）")
    ap.add_argument("--days", type=int, default=10, help="回放天数（两点标定共用）")
    ap.add_argument("--small", type=int, default=10, help="小样本只数")
    ap.add_argument("--large", type=int, default=40, help="大样本只数")
    ap.add_argument("--seed", type=int, default=20260922, help="抽样种子（两点必须一致）")
    args = ap.parse_args()

    n_market = market_size()
    try:
        default_u = int(SP.P._selfproof_param("selfproof_universe_sample", 0) or 0)
    except Exception:  # noqa: BLE001
        default_u = 0

    print(f"\n=== 两点标定：start={args.start} days={args.days} seed={args.seed} ===")
    print(f"  全市场股票数（stock_basic）≈ {n_market}"
          f" | 参数 selfproof_universe_sample 默认={default_u or '0（全市场）'}")

    a = one_run("small", args.start, args.days, args.small, args.seed)
    b = one_run("large", args.start, args.days, args.large, args.seed)

    du = b["universe"] - a["universe"]
    dt = b["wall_sec"] - a["wall_sec"]
    if a["days"] <= 0 or b["days"] <= 0 or du <= 0:
        print("\n❌ 标定失败：两次回放都需有产出天数")
        return 1

    slope = dt / du                          # 秒 / 只 /（days 天）
    intercept = a["wall_sec"] - slope * a["universe"]   # 秒 /（days 天）
    per_stock_day = slope / max(a["days"], 1)
    per_day_fixed = intercept / max(a["days"], 1)

    print("\n=== 线性模型 t(u) = intercept + slope·u（u=抽样只数，t=days 天墙钟秒）===")
    print(f"  斜率 slope = {slope:.4f} s/只 → **每只每天 {per_stock_day * 1000:.1f} ms**")
    print(f"  截距 intercept = {intercept:.2f} s → 每天固定开销 {per_day_fixed:.2f} s"
          f"（指数/基准/日历等，抽样只数无关）")

    print("\n=== 外推（以下为**估算**，单线程、规则档、不含 LLM）===")
    cases = [("默认抽样", default_u or n_market), ("全市场", n_market)]
    for label, u in cases:
        for days, tag in ((250, "1 年（≈250 交易日）"),):
            est = intercept * (days / max(a["days"], 1)) + slope * (days / max(a["days"], 1)) * u
            print(f"  {label:<6s} {u:>5d} 只 × {tag}: 约 {est / 60:.1f} 分钟（{est / 3600:.2f} 小时）")

    # 单日成本也能直接看：把 slope/intercept 归一到"每天"
    print("\n=== 判定（决定 P2.5 是否必须做）===")
    full_year_sec = (per_day_fixed + per_stock_day * n_market) * 250
    verdict = (
        "全市场 1 年单线程 ≈ %.1f 小时 → **必须做 P2.5 并行化**（否则用户等不起）"
        % (full_year_sec / 3600)
        if full_year_sec > 3600 else
        "全市场 1 年单线程 ≈ %.1f 分钟 → 可暂缓并行化，先把 P5/P6 做完"
        % (full_year_sec / 60)
    )
    print(f"  {verdict}")
    print("  注：线性外推有两处乐观偏差 —— (a) 特征缓存未接入，"
          "真实全市场斜率可能更陡；(b) 未计语料/新闻等 I/O 抖动。P2.5 落地后需复测。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
