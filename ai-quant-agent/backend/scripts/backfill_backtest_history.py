"""历史回测结果全量回填 → 实验台账（EKB experiment）

## 为什么需要

`kb_ingest.ingest_backtest` 只在**当次回测跑完**时被调用（backtest_ctl / api/backtest），
所以 `data/backtest_results/*.json` 里躺着的几十份历史回测**没有进过知识库** ——
进化大脑看不到"我过去跑过哪些回测、口径是否可比、指标如何演变"。

本脚本把它们全量灌入（幂等：id = `experiment:backtest:<今日>:<口径指纹后8位>`）：

  - 每份回测 → 一条 experiment（含 metrics 快照 + 口径指纹 components）
  - 口径/数据集与上一份不同 → 自动标记 `invalid`（防自欺护栏，plans/21 §九.1）
  - 只读原始文件，不修改任何既有产物

## 用法

    cd backend && ./venv/bin/python scripts/backfill_backtest_history.py
    cd backend && ./venv/bin/python scripts/backfill_backtest_history.py --limit 10   # 只回填最近 N 份
    cd backend && ./venv/bin/python scripts/backfill_backtest_history.py --dry-run    # 只统计不写库
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.kb import kb_ingest, kb_store, kb_writer  # noqa: E402

RESULTS_DIR = os.path.join(kb_store.DATA_DIR, "backtest_results")


def _load(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="历史回测结果 → EKB 实验台账")
    ap.add_argument("--limit", type=int, default=0, help="只处理最近 N 份（0=全部）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    if not os.path.isdir(RESULTS_DIR):
        print(f"目录不存在: {RESULTS_DIR}")
        return 1
    files = [os.path.join(RESULTS_DIR, f) for f in os.listdir(RESULTS_DIR)
             if f.endswith(".json") and not f.endswith("_latest.json")]
    files.sort(key=lambda p: os.path.getmtime(p))
    if args.limit and args.limit > 0:
        files = files[-args.limit:]
    print(f"== 历史回测 {len(files)} 份（按时间正序，保证指纹前后可比）==")

    valid = invalid = skipped = 0
    for p in files:
        r = _load(p)
        if not isinstance(r, dict) or not (r.get("metrics") or r.get("performance")):
            skipped += 1
            print(f"  ⏭️  {os.path.basename(p)} 无指标字段，跳过")
            continue
        if args.dry_run:
            continue
        try:
            res = kb_ingest.ingest_backtest(r, source="backfill_history",
                                            period=r.get("config") or {})
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            print(f"  ⚠️  {os.path.basename(p)} 写入异常: {exc}")
            continue
        if res.get("valid"):
            valid += 1
        else:
            invalid += 1
        m = res.get("metrics") or {}
        print(f"  {'✅' if res.get('valid') else '⚠️'} {os.path.basename(p)} "
              f"digest={str(res.get('digest'))[-8:]} "
              f"hit_rate={m.get('hit_rate')} t1={m.get('t1_hit_rate')} "
              f"{'（口径变动→invalid）' if not res.get('valid') else ''}")
    if args.dry_run:
        print("\n[dry-run] 未写库")
        return 0

    kb_writer.flush_now()
    print(f"\n== 结果 ==\n  可比(有效) {valid} / 口径变动(invalid) {invalid} / 跳过 {skipped}")
    print("== 台账总览 ==")
    print("experiments:", kb_store.count("experiment"))
    for r in kb_store.group_count("experiment", "status")[:6]:
        print(f"  {r['k']}: {r['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
