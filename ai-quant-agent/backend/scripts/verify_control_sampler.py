"""验收：T4 第二步「同口径对照组」+ W4 镜像缓存（plans/23 §4.1 L2 / §4.5）

覆盖：
  A 对照组采样（control_sampler）
    A1 无 T+5 数据（太新）→ 返回 None（不能瞎算）
    A2 真实交易日 → 返回 date/n/avg_t5/pos_ratio，且与 MySQL 逐行复算一致
    A3 "有启动迹象"（前 20 日涨幅 > warmup）的股票必须被排除
    A4 磁盘缓存命中（第二次调用不再查库；用 SQL 计数验证）
  B 对照聚合（pool_controls）→ 按样本数加权，数学正确
  C 判定（metrics.compare_to_control）
    C1 样本组显著更优 → passed=True
    C2 样本组无差异 → passed=False（这是"形态层无 alpha"的诚实结论路径）
    C3 样本组更差 → passed=False
  D 接线
    D1 runner 使用对照组作为 benchmark（benchmark_source=="control" 分支代码存在）
    D2 runner 写出 performance["vs_control"]
    D3 参数 bt_control_sample_n / bt_control_warmup_pct 已在 evolution_config.json 注册
  E 镜像缓存（mirror_cache）
    E1 配置含 numeric 列 + DDL 用 REAL 亲和性（否则下游 sum 会变字符串拼接 → 静默 NaN）
    E2 镜像未就绪时 mirror_query 返回 None（回退 MySQL，不抛异常）
    E3 镜像数据与 MySQL 逐行一致（若镜像已构建）
    E4 extra_feature_loader 走镜像且开关可关闭

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_control_sampler.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

import pandas as pd

from app.backtest import control_sampler as CS
from app.backtest import mirror_cache as MC
from app.backtest.metrics import compare_to_control
from app.models import SessionLocal

PASS, FAIL = [], []


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _latest_trade_date() -> str:
    db = SessionLocal()
    try:
        r = db.execute(text("SELECT MAX(trade_date) FROM daily")).fetchone()
        return str(r[0]) if r and r[0] else ""
    finally:
        db.close()


def _d_count(date_str: str) -> int:
    db = SessionLocal()
    try:
        r = db.execute(text("SELECT COUNT(*) FROM daily WHERE trade_date=:d AND close>0"),
                       {"d": date_str}).fetchone()
        return int(r[0] or 0)
    finally:
        db.close()


def main() -> int:
    print("=== A 对照组采样 ===")
    # A1：未来日期（无 T+5）→ None
    far = "20991231"
    ck("A1 无 T+5 数据 → None（不瞎算）", CS.control_t5(far) is None, f"control_t5({far})=None")

    db = SessionLocal()
    try:
        # 找一个"前后都有数据"的交易日（保证 T+5 可算）
        row = db.execute(text(
            "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date<='20260820' "
            "ORDER BY cal_date DESC LIMIT 1")).fetchone()
        mid = str(row[0]) if row else ""
    finally:
        db.close()

    ck("A2a 取到可算 T+5 的交易日", bool(mid), mid)
    c1 = CS.control_t5(mid, sample_n=200) if mid else None
    ck("A2b 对照组返回结构完整",
       bool(c1) and set(c1.keys()) >= {"date", "n", "avg_t5", "pos_ratio", "t5_list"},
       json.dumps({k: v for k, v in (c1 or {}).items() if k != "t5_list"}, ensure_ascii=False))
    if c1:
        n_ok = c1["n"] == len(c1["t5_list"])
        # 缓存里 t5_list 四舍五入到 6 位，聚合量用的是未舍入值 → 容差取 1e-4（1e-9 过严）
        ratio_ok = abs(c1["pos_ratio"] - sum(1 for r in c1["t5_list"] if r > 0) / c1["n"]) < 1e-4
        avg_ok = abs(c1["avg_t5"] - sum(c1["t5_list"]) / c1["n"]) < 1e-4
        ck("A2c n/pos_ratio/avg_t5 自洽", n_ok and ratio_ok and avg_ok)
        ck("A2d 抽样数 ≤ 配置上限", c1["n"] <= 200, f"n={c1['n']}")

    # A3：排除"有启动迹象"（前 20 日涨幅 > warmup）
    if mid:
        c_lo = CS.control_t5(mid, sample_n=300, warmup_pct=0.0)     # 全部排除阈值 0 → 只留没涨的
        c_hi = CS.control_t5(mid, sample_n=300, warmup_pct=0.70)    # 宽松 → 留更多
        ok = (c_lo and c_hi and c_lo["n"] <= c_hi["n"])
        ck("A3 warmup 越严 → 对照样本越少（排除有启动迹象者）", bool(ok),
           f"warmup0.0 n={(c_lo or {}).get('n')} ≤ warmup0.70 n={(c_hi or {}).get('n')}")

    # A4：磁盘缓存命中（第二次调用不打库）
    if mid:
        CS.controls_for_dates([mid], sample_n=200)  # 第一次：可能算
        import app.models as M
        from sqlalchemy import event
        cnt = {"n": 0}

        def _cnt(*a, **k):
            cnt["n"] += 1

        event.listen(M.engine, "before_cursor_execute", _cnt)
        try:
            CS.controls_for_dates([mid], sample_n=200)  # 第二次：应命中缓存
        finally:
            event.remove(M.engine, "before_cursor_execute", _cnt)
        ck("A4 日期已在缓存 → 不再查库", cnt["n"] == 0, f"SQL 次数={cnt['n']}")
        ck("A4b 缓存文件已落盘", os.path.exists(CS.CONTROL_FILE), CS.CONTROL_FILE)

    print("=== B 对照聚合 ===")
    fake = {
        "20260105": {"n": 100, "pos_ratio": 0.40, "avg_t5": -0.01},
        "20260106": {"n": 300, "pos_ratio": 0.60, "avg_t5": 0.01},
    }
    p = CS.pool_controls(fake, ["20260105", "20260106"])
    exp_ratio = (0.40 * 100 + 0.60 * 300) / 400
    exp_avg = (-0.01 * 100 + 0.01 * 300) / 400
    ck("B1 按样本数加权（pos_ratio）", abs(p["pos_ratio"] - exp_ratio) < 1e-9,
       f"{p['pos_ratio']:.4f} == {exp_ratio:.4f}")
    ck("B2 按样本数加权（avg_t5）", abs(p["avg_t5"] - exp_avg) < 1e-9)
    ck("B3 n 汇总正确", p["n"] == 400 and p["dates"] == 2)
    ck("B4 空对照 → n=0（不抛异常）", CS.pool_controls({}, ["20260101"])["n"] == 0)

    print("=== C 判定（compare_to_control） ===")
    sample_good = [0.05] * 60 + [-0.01] * 40        # 60% 正收益
    r1 = compare_to_control(sample_good, control_pos_ratio=0.40, control_n=1000)
    ck("C1 显著更优 → passed", r1["passed"], r1["message"])
    r2 = compare_to_control(sample_good, control_pos_ratio=0.60, control_n=1000)
    ck("C2 与对照持平 → 不通过（无 alpha）", not r2["passed"], r2["message"])
    r3 = compare_to_control(sample_good, control_pos_ratio=0.75, control_n=1000)
    ck("C3 比对照更差 → 不通过", not r3["passed"])
    r4 = compare_to_control([], control_pos_ratio=0.4, control_n=100)
    ck("C4 空样本 → 不通过且不抛异常", (not r4["passed"]) and r4["sample_n"] == 0)
    ck("C5 门槛来自可进化参数（min_gap 有值）", r1["min_gap"] > 0, f"min_gap={r1['min_gap']}")

    print("=== D 接线 ===")
    runner_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "app", "backtest", "runner.py"), encoding="utf-8").read()
    ck("D1 runner 用同口径对照作基准（benchmark_source=control）",
       "benchmark_source" in runner_src and 'bench_src = "control"' in runner_src)
    ck("D2 runner 写出 vs_control", '"vs_control"' in runner_src and "compare_to_control" in runner_src)
    ck("D3 runner 读可进化参数 bt_control_*",
       "bt_control_sample_n" in runner_src and "bt_control_warmup_pct" in runner_src)

    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "evolution_config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    params = cfg.get("params", {})
    ck("D4 bt_control_sample_n 已注册", "bt_control_sample_n" in params,
       f"current={params.get('bt_control_sample_n', {}).get('current')}")
    ck("D5 bt_control_warmup_pct 已注册", "bt_control_warmup_pct" in params)
    ck("D6 二者均为 Tier1 热生效", all(params.get(k, {}).get("hot_reload") for k in
                                 ("bt_control_sample_n", "bt_control_warmup_pct")))

    print("=== E 镜像缓存（W4） ===")
    # numeric 列可选（dividend/repurchase/stk_rewards/forecast 无数值列）；
    # 但**声明了 numeric 的表必须真的含有这些列**（防配置写错）
    bad_cfg = [t for t, c in MC.MIRRORED.items()
               if any(x not in c["cols"] for x in (c.get("numeric") or []))]
    ck("E1a numeric 配置自洽（声明的数值列都在 cols 里）", not bad_cfg, f"bad={bad_cfg}")
    ck("E1a2 已禁用表不会被当成可用镜像",
       all(not MC.is_ready(t) for t, c in MC.MIRRORED.items() if c.get("disabled")))
    ddl = MC._ddl("moneyflow")
    ck("E1b DDL 数值列 REAL / 键列 TEXT",
       "buy_elg_amount REAL" in ddl and "ts_code TEXT" in ddl and "trade_date TEXT" in ddl)
    ck("E2 未就绪表 → mirror_query 返回 None（回退 MySQL）",
       MC.mirror_query("000001.SZ", "not_a_table", ["x"]) is None)
    st = MC.stats()
    print(f"  镜像状态: {json.dumps(st, ensure_ascii=False)}")
    for t in MC.enabled_tables():
        if not MC.is_ready(t):
            print(f"  ⏭  {t} 镜像未构建（跳过 E3）")
            continue
        code = "000001.SZ"
        mdf = MC.mirror_query(code, t, MC.MIRRORED[t]["cols"], MC.MIRRORED[t]["date_col"])
        # 镜像是**快照**：只保证"截至 src_max_date"与 MySQL 一致；
        # 之后的增量行（例如 17:30 自动采集刚写入的今日数据）由下次增量同步补齐。
        snap = str((st.get("tables", {}).get(t) or {}).get("src_max_date") or "")
        db = SessionLocal()
        try:
            cols = ", ".join(MC.MIRRORED[t]["cols"])
            dc_raw = str(MC.MIRRORED[t]["date_col"])
            # 源表日期列**混存两种格式**（'20250415' 与 '2025-04-15 17:04:37'），
            # 直接字符串比较会得出错误结论（实测 dividend 53 vs 46）→ 统一归一化后再比。
            # 归一化方式与 loader `_parse_dates` 一致：取前 10 位再去掉 '-'。
            norm = f"REPLACE(SUBSTRING({dc_raw}, 1, 10), '-', '')"
            # NULL 日期：MySQL 的 `<=` 比较结果是 NULL（该行不会被选出），
            # 镜像里却有这些行（dividend 存在 ann_date 为空的记录）→
            # 两侧统一"排除空日期"再比，否则会出现 mirror=53 vs mysql=46 的假失败。
            cond = f" AND {dc_raw} IS NOT NULL AND {norm} <= '{snap}'" if snap else ""
            sql = (f"SELECT {cols} FROM {t} WHERE ts_code='{code}'{cond} "
                   f"ORDER BY {dc_raw}")
            mydf = pd.read_sql(text(sql), db.connection())
        finally:
            db.close()
        if mdf is not None and len(mdf):
            _nd = pd.to_numeric(
                mdf[dc_raw].astype(str).str.slice(0, 10).str.replace("-", "", regex=False),
                errors="coerce")
            keep_mask = _nd.notna() & (_nd.notna() if not snap else (_nd <= float(snap)))
            mdf = mdf.loc[keep_mask].reset_index(drop=True)
        same_rows = bool(mdf is not None and len(mdf) == len(mydf))
        ck(f"E3 {t} 镜像与 MySQL 行数一致（快照日 {snap}）", same_rows,
           f"mirror={0 if mdf is None else len(mdf)} mysql={len(mydf)}")
        dc = str(MC.MIRRORED[t]["date_col"])
        numeric_cols = MC.MIRRORED[t].get("numeric") or []
        if same_rows and mdf is not None and len(mydf):
            d1 = pd.to_numeric(mdf[dc].astype(str), errors="coerce").tolist()
            d2 = pd.to_numeric(mydf[dc].astype(str), errors="coerce").tolist()
            if len(d1) != len(set(d1)):
                ck(f"E3b {t} 日期列有重复，跳过行序比对", True)
            else:
                ck(f"E3b {t} 行序一致（升序同源）", d1 == d2)
            # 数值比对只在"日期列在结果内唯一"时有效：top10_holders 等同一天有多行，
            # ORDER BY 日期不保证两库行序一致（属断言口径问题，不是镜像问题）
            dup = bool(pd.Series(d1).duplicated().any())
            if not numeric_cols:
                ck(f"E3c {t} 无数值列（跳过数值比对）", True)
            elif dup:
                ck(f"E3c {t} 日期列有重复（多行/日），跳过逐行数值比对", True,
                   f"dup_dates={len(d1) - len(set(d1))}")
            else:
                numc = str(numeric_cols[0])
                a = pd.to_numeric(mdf[numc], errors="coerce")
                b = pd.to_numeric(mydf[numc], errors="coerce")
                okv = bool((abs(a - b) < 1e-6).all() or bool((a.isna() & b.isna()).all()))
                ck(f"E3c {t}.{numc} 数值一致", okv,
                   f"max_diff={float((abs(a - b)).max()):.6g}" if len(a) else "")

    xfl = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app", "backtest", "extra_feature_loader.py"), encoding="utf-8").read()
    ck("E4a extra_feature_loader 走镜像", "mirror_query(" in xfl and "MIRRORED" in xfl)
    ck("E4b 可开关（extra_mirror_enabled）", "extra_mirror_enabled" in xfl)

    print(f"\n===== 结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
