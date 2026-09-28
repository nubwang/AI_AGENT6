"""验证：采集时序/幂等/字典表 upsert（plans/23 §2.5 V1/V2/V3/V5）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_collection_timing.py

覆盖：
  S1. 所有改动文件可编译
  V1. 每日采集主任务 = 17:30，另有 21:00 兜底；存在"数据就绪校验 + 自动重试"实现
  V3. stage1_basic 不再把 stock_basic/trade_cal 当一次性表；upsert 语义存在并**实测幂等**
  V2. 幂等守卫能区分『数据未推进』与『已扫描』
  V5. 无定时任务落在盘中时段（9:30-11:30 / 13:00-15:00）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


MAIN = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
WS = (BACKEND / "app" / "api" / "ws.py").read_text(encoding="utf-8")
PRED = (BACKEND / "app" / "api" / "predictions.py").read_text(encoding="utf-8")

# ── S1 编译 ──
print("[S1] 编译检查")
import py_compile  # noqa: E402

for rel in ("app/main.py", "app/api/ws.py", "app/api/predictions.py",
            "app/backtest/runner.py", "app/backtest/metrics.py",
            "app/backtest/attribution.py", "app/backtest/threshold_analyzer.py",
            "app/backtest/monitor.py", "app/backtest/pattern_miner.py"):
    try:
        py_compile.compile(str(BACKEND / rel), doraise=True)
        check(f"S1 编译 {rel}", True)
    except Exception as exc:  # noqa: BLE001
        check(f"S1 编译 {rel}", False, str(exc)[:120])

# ── V1 ──
print("\n[V1] 采集时序与就绪重试")
check("V1 主采集任务改为 17:30", "CronTrigger(hour=17, minute=30)" in MAIN)
check("V1 存在 21:00 兜底任务", 'daily_data_collection_night' in MAIN and "CronTrigger(hour=21, minute=0)" in MAIN)
check("V1 不再存在 16:30 采集", "CronTrigger(hour=16, minute=30)" not in MAIN)
check("V1 实现『应有数据日期』判定", "_expected_data_date" in MAIN and "trade_cal" in MAIN)
check("V1 实现数据未就绪 → 自动重试", "_schedule_collect_retry" in MAIN and "数据未就绪" in MAIN)
# plans/23 §十二：重试上限/间隔已改为可进化参数（读 evolution_config，带默认回退）
check("V1 重试上限/间隔已定义（可进化）",
      '_COLLECT_RETRY = {' in MAIN and '_evol_num("collect_retry_max", 3)' in MAIN
      and '_evol_num("collect_retry_delay_min", 30)' in MAIN)
check("V1 采集后做就绪校验再触发推荐",
      "get_latest_daily_date" in MAIN and "latest_s < expected" in MAIN)

# ── V3 ──
print("\n[V3] 字典表每日增量 + upsert 幂等")
check("V3 direct_apis 不再含 stock_basic/trade_cal",
      'direct_apis = ["index_basic"]' in WS and 'stock_basic"' not in WS.split("direct_apis = [")[1].split("]")[0])
check("V3 存在 upsert_dict_table", "def upsert_dict_table(" in WS)
check("V3 存在每日字典表同步", "def _sync_daily_dict_tables(" in WS)
check("V3 stage1 调用了每日同步", "await _sync_daily_dict_tables()" in WS)
check("V3 trade_cal 进入每日同步（防 2027 无日历）", '"trade_cal", "交易日历", tushare_client.trade_cal, "cal_date"' in WS)

# 实测 upsert 幂等性（临时表，测完删除）
print("\n[V3b] upsert 幂等性实测（真实写 MySQL）")
try:
    import pandas as _pd
    from sqlalchemy import text as _t
    from app.models import SessionLocal
    from app.api.ws import upsert_dict_table

    db = SessionLocal()
    try:
        db.execute(_t("DROP TABLE IF EXISTS _tmp_dict_upsert"))
        db.execute(_t("CREATE TABLE _tmp_dict_upsert ("
                      "id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
                      "ts_code VARCHAR(20), name VARCHAR(50))"))
        db.commit()
    finally:
        db.close()

    df1 = _pd.DataFrame({"ts_code": ["000001.SZ", "000002.SZ"], "name": ["A", "B"]})
    n1 = upsert_dict_table("_tmp_dict_upsert", df1, "ts_code")
    n2 = upsert_dict_table("_tmp_dict_upsert", df1, "ts_code")     # 重复同步同一批
    df2 = _pd.DataFrame({"ts_code": ["000003.SZ"], "name": ["C"]})  # 新增新股
    n3 = upsert_dict_table("_tmp_dict_upsert", df2, "ts_code")

    db = SessionLocal()
    try:
        total = db.execute(_t("SELECT COUNT(*) FROM _tmp_dict_upsert")).scalar()
        distinct = db.execute(_t("SELECT COUNT(DISTINCT ts_code) FROM _tmp_dict_upsert")).scalar()
        has_new = db.execute(_t("SELECT COUNT(*) FROM _tmp_dict_upsert WHERE ts_code='000003.SZ'")).scalar()
    finally:
        db.close()
    check("V3b 首次写入 2 行", n1 == 2 and total is not None, f"n1={n1}")
    check("V3b 重复同步不产生重复行（幂等）", total == 3 and distinct == 3,
          f"total={total}, distinct={distinct}")
    check("V3b 新增股票正常入库", has_new == 1, f"000003.SZ={has_new}")

    db = SessionLocal()
    try:
        db.execute(_t("DROP TABLE IF EXISTS _tmp_dict_upsert"))
        db.commit()
    finally:
        db.close()
except Exception as exc:  # noqa: BLE001
    check("V3b upsert 幂等性实测", False, f"{type(exc).__name__}: {exc}")

# ── V2 ──
print("\n[V2] 幂等守卫语义")
check("V2 区分 data_not_advanced", '"data_not_advanced"' in PRED)
check("V2 区分 already_scanned", '"already_scanned"' in PRED)
check("V2 返回体带 reason 字段", '"reason": reason' in PRED)

# ── V5 ──
print("\n[V5] 定时任务错峰（不得落在盘中）")
crons = re.findall(r"CronTrigger\(([^)]*)\)", MAIN)
bad = []
for c in crons:
    hour_m = re.search(r"hour=(\d+)", c)
    minute_m = re.search(r"minute=(\d+)", c)
    if not hour_m:
        continue
    h = int(hour_m.group(1))
    m = int(minute_m.group(1)) if minute_m else 0
    t = h * 60 + m
    in_morning = (9 * 60 + 30) <= t <= (11 * 60 + 30)
    in_afternoon = (13 * 60) <= t <= (15 * 60)
    if in_morning or in_afternoon:
        bad.append(f"{h:02d}:{m:02d}")
check("V5 无任务落在盘中时段（9:30-11:30 / 13:00-15:00）", not bad, f"越界: {bad}")
evening = [c for c in crons if (re.search(r"hour=(\d+)", c) and int(re.search(r"hour=(\d+)", c).group(1)) >= 18)]
check("V5 17:30 之后形成晚间链路（≥6 个任务）", len(evening) >= 6, f"晚间 cron 数={len(evening)}")

print(f"\n{'=' * 60}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
