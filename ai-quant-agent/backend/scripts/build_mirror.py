"""构建/更新本地镜像缓存（plans/23 §4.5 W4 回测提速）

用法：
    cd ai-quant-agent/backend
    ./venv/bin/python scripts/build_mirror.py              # 增量同步（推荐，秒级~分钟级）
    ./venv/bin/python scripts/build_mirror.py --rebuild    # 全量重建（首次约 5~15 分钟）
    ./venv/bin/python scripts/build_mirror.py --stats      # 仅查看状态

背景（实测）：回测逐股查 daily_basic/moneyflow，MySQL 冷读 1.8~2.0s/股（索引 988MB/965MB
装不进 buffer pool），单股 ~4s → 全量回测 ~80 分钟。镜像到本地 SQLite 后单股 ~20ms。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.mirror_cache import ensure_mirror, stats, MIRRORED


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", default="", help="逗号分隔（默认全部）：daily_basic,moneyflow")
    ap.add_argument("--rebuild", action="store_true", help="全量重建（默认增量）")
    ap.add_argument("--stats", action="store_true", help="仅打印状态")
    args = ap.parse_args()

    if args.stats:
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
        return 0

    tables = [t.strip() for t in args.tables.split(",") if t.strip()] or list(MIRRORED.keys())
    print(f"镜像目标：{tables}（rebuild={args.rebuild}）")
    t0 = time.time()
    res = ensure_mirror(tables, force=args.rebuild)
    for t, r in (res.get("tables") or {}).items():
        print(f"  {t}: rows={r.get('rows')} added={r.get('added')} "
              f"{r.get('seconds')}s err={r.get('error')}")
    print(f"总耗时 {time.time() - t0:.1f}s")
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
    return 1 if res.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
