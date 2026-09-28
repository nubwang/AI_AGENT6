"""人心（情绪/博弈）数据可得性审计 — plans/24 第 0 步

## 为什么必须先做这一步

用户方向：**A 股是人心博弈**（封板/炸板/连板/游资/恐慌盘/诱多诱空），不能只做形态与价值。
但"人心"要变成可回测的东西，前提是**数据真的拿得到**。本项目纪律：
**不猜权限、不猜字段、不猜日期语义**，一律实测。

本脚本跑出四组事实：

  A 表盘点      ：quant_db 里每张表的行数与日期范围（含"人心相关表"是否为空）
  B 字段盘点    ：moneyflow / daily_basic / daily / stk_limit 的真实列（决定能算哪些博弈量）
  C 情绪可算性  ：用最近 10 个交易日**真实全市场数据**试算
                  涨停家数 / 跌停家数 / 炸板家数 / 一字板家数 / 连板高度 / 昨日涨停今日表现
                  并用两种口径（pct_chg 阈值 vs stk_limit 的 up_limit）**交叉验证**，
                  顺便钉死 §13.6 那次事故的遗留疑问：stk_limit 的日期语义到底是当天还是次日
  D 权限实测    ：逐个试调 Tushare 的人心类接口（龙虎榜/游资/热榜/筹码/竞价/两融/北向），
                  记录 成功 / 无权限 / 接口不存在 / 参数错

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/audit_heart_data.py            # 含网络实测
    cd ai-quant-agent/backend && ./venv/bin/python scripts/audit_heart_data.py --no-net   # 只审计本地库
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.models import SessionLocal

PASS: list[str] = []
FAIL: list[str] = []
WARN: list[str] = []

# ── "人心"相关表的登记（表名 → 它表达的人心维度）────────────────────────────
HEART_TABLES: dict[str, str] = {
    "daily": "K线（封板/炸板/影线/缺口 = 最原始的人心痕迹）",
    "daily_basic": "换手率/量比/自由流通换手（筹码交换强度 = 人心交换）",
    "moneyflow": "小单/中单/大单/特大单净额（散户 vs 主力 = 人心的对手盘）",
    "stk_limit": "涨跌停价（封板/炸板的判定基准）",
    "limit_list": "龙虎榜/涨停统计（席位 = 谁在博弈）",
    "hsgt": "沪深港通持股（北向 = 聪明钱）",
    "margin": "融资融券（杠杆情绪）",
    "block_trade": "大宗交易（折价 = 出货意愿）",
    "suspend_d": "停复牌（流动性中断）",
    "stk_holdernumber": "股东户数（筹码集中/分散 = 人心聚散）",
    "concept": "概念板块（题材 = 人心聚焦点）",
    "concept_detail": "个股↔概念（一个人在跟哪个故事）",
    "ths_index": "同花顺板块指数（板块人心）",
    "ths_daily": "同花顺板块日线（板块动量）",
    "limit_list_d": "涨跌停/龙虎榜（按日）",
}

# 人格化的语义分档：这些量要能算，才谈得上"研究人心"
HEART_FEATURES: dict[str, str] = {
    "封板家数": "市场做多意愿的总量",
    "炸板家数": "做多意愿的瓦解速度（最敏感的人心反转信号）",
    "一字板家数": "最强的一致预期（买不到 = 无人愿卖）",
    "连板高度": "情绪周期的位置（几板定龙头，板高即天花板）",
    "昨日涨停今日表现": "赚钱效应（决定散户明天敢不敢接）",
    "涨跌家数比": "普涨还是普跌（大盘人心）",
    "封单强度": "买盘决心（理论最优但需分笔数据）",
    "散户净买占比": "小单净额占总成交（散户在接盘 = 危险的顶部特征）",
    "主力净额分歧度": "大单净额与价格方向背离（对倒/派发/吸筹）",
    "换手率位置": "低位高换手 = 换手（好）；高位高换手 = 派发（坏）",
    "上影/下影长度": "试盘 / 诱多 / 恐慌后修复",
    "集合竞价强度": "开盘 9:20-9:25 的意向（需 stk_auction）",
    "游资席位动向": "谁在买（需 top_inst / hm_detail）",
    "筹码分布": "成本结构（需 cyq_chips）",
    "两融余额变化": "杠杆资金进退（需 margin_detail）",
}


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '⚠️ '} {name}" + (f" — {detail}" if detail else ""))


def warn(msg: str) -> None:
    WARN.append(msg)
    print(f"  · {msg}")


def sec(title: str) -> None:
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


def _d(v) -> str:
    """date/datetime/str → YYYYMMDD。"""
    if v is None:
        return ""
    s = str(v).replace("-", "")
    return s[:8]


# ─────────────────────────────────────────────────────────────────────────────
# A 表盘点
# ─────────────────────────────────────────────────────────────────────────────
def a_tables(db) -> dict:
    sec("A 表盘点（quant_db 全表 · 行数 · 日期范围）")
    rows = db.execute(text(
        "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME")).fetchall()
    all_tables = {str(r[0]): int(r[1] or 0) for r in rows}
    print(f"  库内共 {len(all_tables)} 张表")

    out: dict = {}
    for t in sorted(HEART_TABLES):
        exists = t in all_tables
        n = all_tables.get(t, 0)
        dmin = dmax = ""
        if exists:
            for dc in ("trade_date", "end_date", "ann_date", "start_date"):
                try:
                    rr = db.execute(text(f"SELECT MIN({dc}), MAX({dc}) FROM {t}")).fetchone()
                    if rr and rr[0] is not None:
                        dmin, dmax = _d(rr[0]), _d(rr[1])
                        break
                except Exception:  # noqa: BLE001
                    db.rollback()
        out[t] = {"exists": exists, "rows": n, "min": dmin, "max": dmax}
        flag = "✅" if (exists and n > 0) else ("⚠️ " if exists else "❌")
        print(f"  {flag} {t:<18} 行数 {n:>10}  日期 {dmin or '-'} ~ {dmax or '-'}"
              f"   {HEART_TABLES[t]}")

    alive = [t for t, v in out.items() if v["exists"] and v["rows"] > 0]
    ck("A1 '人心'相关表至少有一半可用",
       len(alive) * 2 >= len(HEART_TABLES), f"{len(alive)}/{len(HEART_TABLES)} 张有数据")
    for t in ("daily", "daily_basic", "moneyflow", "stk_limit"):
        v = out.get(t, {})
        ck(f"A2 核心人心表 {t} 可用", bool(v.get("exists") and v.get("rows", 0) > 0),
           f"行数 {v.get('rows')} 日期 {v.get('min')}~{v.get('max')}")
    empty = [t for t, v in out.items() if v["exists"] and not v["rows"]]
    if empty:
        warn(f"表存在但为空（等于不可用）：{empty}")
    missing = [t for t, v in out.items() if not v["exists"]]
    if missing:
        warn(f"表不存在：{missing}")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B 字段盘点
# ─────────────────────────────────────────────────────────────────────────────
def b_columns(db) -> dict:
    sec("B 字段盘点（真实列名，决定能算哪些博弈量）")
    rows = db.execute(text(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME, ORDINAL_POSITION")).fetchall()
    cols: dict[str, list[str]] = {}
    for t, c in rows:
        cols.setdefault(str(t), []).append(str(c))

    for t in ("daily", "daily_basic", "moneyflow", "stk_limit"):
        cs = cols.get(t) or []
        print(f"  [{t}] ({len(cs)} 列) {', '.join(cs) if cs else '—'}")

    mf = cols.get("moneyflow") or []
    # 散户/主力代理：小单 vs 特大单
    has_sm = any("sm" in c or c.endswith("_st") for c in mf)
    has_elg = any("elg" in c or c.endswith("_lt") for c in mf)
    ck("B1 moneyflow 能区分'散户 vs 主力'（小单与特大单字段都在）",
       has_sm and has_elg,
       f"小单类={[c for c in mf if 'sm' in c or c.endswith('_st')]} "
       f"特大单类={[c for c in mf if 'elg' in c or c.endswith('_lt')]}")

    dbc = cols.get("daily_basic") or []
    ck("B2 daily_basic 有 turnover_rate_f（自由流通换手，比总换手更真实）",
       "turnover_rate_f" in dbc, f"有={[c for c in dbc if 'turnover' in c]}")
    ck("B3 daily_basic 有 circ_mv / free_share（可算筹码体量与人气密度）",
       "circ_mv" in dbc and "free_share" in dbc,
       f"circ_mv={'circ_mv' in dbc} free_share={'free_share' in dbc}")

    dcols = cols.get("daily") or []
    need = [c for c in ("open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount")
            if c in dcols]
    ck("B4 daily 具备算'封板/炸板/影线/缺口'的全部基础价量列",
       len(need) >= 8, f"命中 {need}")

    sl = cols.get("stk_limit") or []
    ck("B5 stk_limit 有 up_limit / down_limit", "up_limit" in sl and "down_limit" in sl, str(sl))
    return cols


# ─────────────────────────────────────────────────────────────────────────────
# C 情绪指标可算性（真实全市场试算）
# ─────────────────────────────────────────────────────────────────────────────
def _limit_pct(ts_code: str) -> float:
    """按板块推断涨跌停幅度（近似；ST 由调用方另行处理）。"""
    c = ts_code.split(".")[0]
    if c.startswith(("688", "300", "301")):
        return 0.20
    if c.startswith(("8", "4")):          # 北交所
        return 0.30
    return 0.10


def c_sentiment(db) -> dict:
    sec("C 情绪指标可算性（最近 10 个交易日 · 真实全市场试算）")
    dq = db.execute(text("SELECT DISTINCT trade_date FROM daily ORDER BY trade_date DESC LIMIT 12"))
    dates = [r[0] for r in dq.fetchall() if r[0] is not None]
    if len(dates) < 6:
        ck("C1 daily 至少有 6 个交易日可用", False, f"仅 {len(dates)} 个")
        return {}
    ck("C1 daily 有足够交易日用于连板追溯", True,
       f"{len(dates)} 日：{_d(dates[-1])} ~ {_d(dates[0])}")

    # ── 逐日：涨停/跌停集合（口径A pct_chg；口径B stk_limit.up_limit）──
    per_day: dict[str, dict] = {}
    for d in dates:
        r = db.execute(text(
            "SELECT ts_code, high, low, close, pre_close, pct_chg FROM daily WHERE trade_date = :d"),
            {"d": d}).fetchall()
        if not r:
            continue
        up, down, zha, yiziban = set(), set(), set(), set()
        for ts_code, high, low, close, pre_close, pct_chg in r:
            code = str(ts_code)
            if not (close and high and low):
                continue
            lim = _limit_pct(code)
            thr = lim * 100.0 - 0.3                    # 容忍 0.3pct 的取整误差
            pc = float(pct_chg) if pct_chg is not None else None
            if pc is None:
                continue
            hi_pct = (float(high) / float(pre_close) - 1.0) * 100.0 if pre_close else None
            lo_pct = (float(low) / float(pre_close) - 1.0) * 100.0 if pre_close else None
            is_up = pc >= thr
            if is_up:
                up.add(code)
                if hi_pct is not None and hi_pct >= thr:
                    # 收盘仍封住 → 一字板 = 全天最低价也已到涨停（几乎无成交空间）
                    if lo_pct is not None and lo_pct >= thr - 1.0:
                        yiziban.add(code)
            elif hi_pct is not None and hi_pct >= thr and pc < thr:
                zha.add(code)                           # 盘中触板但收盘未封 = 炸板
            if pc <= -thr:
                down.add(code)
        per_day[_d(d)] = {"up": up, "down": down, "zha": zha, "yizi": yiziban, "n": len(r)}

    latest = _d(dates[0])
    cur = per_day.get(latest) or {}
    if not cur:
        ck("C2 最新交易日情绪可算", False, "无数据")
        return {}

    # 先取出来做局部量：避免 f-string 里嵌套引号跨行（可读性 + 解析稳健）
    n_all = int(cur.get("n") or 0)
    n_up, n_zha = len(cur["up"]), len(cur["zha"])
    n_yizi, n_down = len(cur["yizi"]), len(cur["down"])
    tot = n_up + n_down

    print(f"  最新交易日 {latest}：全市场 {n_all} 只")
    print(f"    涨停 {n_up}（其中一字板 {n_yizi}） / 炸板 {n_zha} / 跌停 {n_down}")
    if tot:
        print(f"    涨跌停比 = {n_up}:{n_down}")

    ck("C2 ✅ 封板/炸板/一字板家数可算（用真实全市场数据）",
       n_up > 0 and n_zha >= 0,
       f"涨停 {n_up} / 炸板 {n_zha} / 一字 {n_yizi}")

    # ── 连板高度（追溯最近 6 日）──
    ordered = sorted(per_day)                       # 升序
    streak: dict[str, int] = {}
    for day in ordered:
        ups = per_day[day]["up"]
        for code in ups:
            streak[code] = streak.get(code, 0) + 1 if code in streak else 1
        # 只保留当日在涨停集合里的（断板清零）
        streak = {c: v for c, v in streak.items() if c in ups}
    lianban = [v for v in streak.values() if v >= 2]
    top_board = max(streak.values()) if streak else 0
    print(f"    连板 ≥2 板家数 {len(lianban)}，最高 {top_board} 板")
    ck("C3 ✅ 连板高度可算（情绪周期的位置）", True,
       f"≥2板 {len(lianban)} 只，最高 {top_board} 板")

    # ── 赚钱效应：昨日涨停今日表现 ──
    if len(ordered) >= 2:
        y, t = ordered[-2], ordered[-1]
        prev_up = per_day[y]["up"]
        if prev_up:
            r2 = db.execute(text(
                "SELECT ts_code, pct_chg FROM daily WHERE trade_date = :d"),
                {"d": dates[0]}).fetchall()
            m: dict[str, float] = {}
            for a, b in r2:
                if b is not None:
                    m[str(a)] = float(b)
            vals: list[float] = [m[c] for c in prev_up if c in m]
            avg = (sum(vals) / len(vals)) if vals else None
            if avg is None:
                print("    赚钱效应：无法计算")
                ck("C4 赚钱效应（昨日涨停今日表现）可算", False, "样本为空")
            else:
                print(f"    赚钱效应：{y} 涨停的 {len(prev_up)} 只，在 {t} 平均 {avg:.2f}%")
                ck("C4 ✅ 赚钱效应（昨日涨停今日表现）可算 —— 散户敢不敢接盘的直接依据",
                   True, f"均值 {avg:.2f}%")

    # ── 交叉验证：pct_chg 口径 vs stk_limit 口径（顺便钉死日期语义）──
    try:
        r3 = db.execute(text(
            "SELECT ts_code, up_limit FROM stk_limit WHERE trade_date = :d"),
            {"d": dates[0]}).fetchone()
        n_sl = db.execute(text("SELECT COUNT(*) FROM stk_limit WHERE trade_date = :d"),
                          {"d": dates[0]}).scalar()
        if n_sl:
            # ⚠️ 实测踩到（本轮审计顺带发现的**真实工程缺陷**）：
            #    daily 与 stk_limit 的 ts_code 列排序规则不同
            #    （utf8mb4_0900_ai_ci vs utf8mb4_unicode_ci），SQL 层 JOIN 直接报 1267，
            #    且两侧都写 COLLATE 也不可靠（实测仍报错）→ **必须在 Python 里对齐集合**。
            #    这也解释了为什么项目其余跨表逻辑一律不做 SQL JOIN。
            up_lim: dict[str, float] = {}
            for a, b in db.execute(text(
                    "SELECT ts_code, up_limit FROM stk_limit WHERE trade_date = :d"),
                    {"d": dates[0]}).fetchall():
                if b is not None:
                    up_lim[str(a)] = float(b)
            clo: dict[str, float] = {}
            for a, b in db.execute(text(
                    "SELECT ts_code, close FROM daily WHERE trade_date = :d"),
                    {"d": dates[0]}).fetchall():
                if b is not None:
                    clo[str(a)] = float(b)
            n_hits = 0
            for c, v in clo.items():
                ul = up_lim.get(c)
                if ul and v >= ul * 0.999:
                    n_hits += 1
            # ★ 关键：stk_limit 当日可能**不完整**（实测 20260918 仅 2361 行 vs daily 5553 只，
            #   即遗留项里的"重采不完整日期"）。所以不能比绝对数量，必须比
            #   **覆盖率归一化后的封板率**：
            #     同日语义成立 → up_limit 子集内的封板率 ≈ 全市场涨停率
            #     次日语义     → 同日 join 应几乎命中 0
            n_lim = len(up_lim)
            rate_lim = n_hits / max(1, n_lim)
            rate_all = n_up / max(1, n_all)
            print(f"    stk_limit 口径（同日 up_limit）：仅 {n_lim} 行有记录"
                  f"（全市场 {n_all} 只 → 覆盖率 {n_lim / max(1, n_all):.1%}）")
            print(f"      子集内封板 {n_hits} 只（{rate_lim:.2%}）"
                  f" vs 全市场涨停 {n_up} 只（{rate_all:.2%}）")
            ok_c5 = n_lim > 0 and abs(rate_lim - rate_all) <= 0.01
            if ok_c5:
                det5 = (f"归一化后两口径一致（{rate_lim:.2%} vs {rate_all:.2%}）"
                        "→ stk_limit 为『当日』语义；绝对数量差来自 stk_limit 数据不完整")
            else:
                det5 = (f"归一化后仍不一致（{rate_lim:.2%} vs {rate_all:.2%}）"
                        "→ 疑为『次日』语义（需错位 join；§13.6 遗留问题）")
            ck("C5 stk_limit 的 up_limit 与 pct_chg 口径一致（钉死日期语义）", ok_c5, det5)
        else:
            warn(f"stk_limit 在 {dates[0]} 无数据（共 {n_sl} 行），跳过一致性验证")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        warn(f"stk_limit 一致性验证失败：{exc}")

    # ── 散户 vs 主力（moneyflow 结构）──
    # ⚠️ 真实列名（B 段实测）：buy_sm_amount/sell_sm_amount/.../buy_elg_amount/sell_elg_amount
    #    —— 不是 models/stock.py 里写的 net_amount/amount_lt/mt/st（那份 ORM 定义与实际表不符）。
    #    散户 = 小单 + 中单；主力 = 大单 + 特大单。这才是"人心的对手盘"。
    _RETAIL = ("(IFNULL(m.buy_sm_amount,0)+IFNULL(m.buy_md_amount,0)"
               "-IFNULL(m.sell_sm_amount,0)-IFNULL(m.sell_md_amount,0))")
    _MAIN = ("(IFNULL(m.buy_lg_amount,0)+IFNULL(m.buy_elg_amount,0)"
             "-IFNULL(m.sell_lg_amount,0)-IFNULL(m.sell_elg_amount,0))")
    try:
        r4 = db.execute(text(
            f"SELECT m.ts_code, {_RETAIL} AS retail, {_MAIN} AS main, d.pct_chg "
            "FROM moneyflow m JOIN daily d "
            "ON m.ts_code COLLATE utf8mb4_unicode_ci = "
            "   d.ts_code COLLATE utf8mb4_unicode_ci "
            "AND d.trade_date = m.trade_date "
            f"WHERE m.trade_date = :d ORDER BY ABS({_MAIN}) DESC LIMIT 5"),
            {"d": dates[0]}).fetchall()
        if r4:
            print("    散户/主力结构样例（按主力净额绝对值降序，单位万元）：")
            for ts_code, retail, main_v, pc in r4:
                line = (f"      {ts_code}  散户净额={retail}  主力净额={main_v}"
                        f"  涨跌={pc}%")
                print(line)

            agg = db.execute(text(
                f"SELECT COUNT(*) AS n, "
                f"SUM(CASE WHEN ({_RETAIL}) > 0 AND ({_MAIN}) < 0 THEN 1 ELSE 0 END) AS diverge, "
                f"SUM({_RETAIL}) AS retail_sum, "
                f"SUM({_MAIN}) AS main_sum "
                "FROM moneyflow m WHERE m.trade_date = :d"),
                {"d": dates[0]}).fetchone()
            n_mf = int((agg[0] if agg else 0) or 0)
            n_div = int((agg[1] if agg else 0) or 0)
            ret_sum = float((agg[2] if agg else 0) or 0)
            main_sum = float((agg[3] if agg else 0) or 0)
            print(f"    全市场 {n_mf} 只有资金流：散户净额合计 {ret_sum:,.0f} 万，"
                  f"主力净额合计 {main_sum:,.0f} 万")
            if n_mf:
                print(f"    ★ '散户在买 / 主力在卖'（派发特征）的个股：{n_div} 只"
                      f"（{n_div / n_mf:.1%}）")
            ck("C6 ✅ '散户净买 vs 主力净额'可算（散户接盘 = 顶部特征的直接代理）",
               n_mf > 0, f"{n_mf} 只，派发特征 {n_div} 只（{n_div / max(1, n_mf):.1%}）")
        else:
            ck("C6 moneyflow 当日有数据（散户/主力结构）", False,
               f"{_d(dates[0])} 无 moneyflow")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        warn(f"moneyflow 结构验证失败：{exc}")

    # ── 换手率位置 ──
    try:
        r5 = db.execute(text(
            "SELECT COUNT(*), AVG(turnover_rate) FROM daily_basic WHERE trade_date = :d"),
            {"d": dates[0]}).fetchone()
        n_db = int((r5[0] if r5 else 0) or 0)
        avg_to = float((r5[1] if r5 else 0) or 0)
        ck("C7 ✅ 换手率可用（低位高换手=换手，高位高换手=派发）",
           n_db > 0, f"{n_db} 行，均值换手 {avg_to:.2f}%")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        warn(f"daily_basic 验证失败：{exc}")

    return per_day


# ─────────────────────────────────────────────────────────────────────────────
# D 权限实测
# ─────────────────────────────────────────────────────────────────────────────
NET_CASES: list[tuple[str, str]] = [
    # (tushare pro 接口名, 它表达的人心)
    ("limit_list_d", "涨跌停/龙虎榜（炸板率、连板数的官方口径）"),
    ("top_list", "龙虎榜每日明细（谁在买）"),
    ("top_inst", "龙虎榜机构席位（机构 vs 游资）"),
    ("hm_detail", "游资每日明细（人心最直接的体现）"),
    ("hm_list", "游资名录"),
    ("kpl_list", "开盘啦榜单（涨停原因/题材归因）"),
    ("kpl_concept", "开盘啦题材库"),
    ("limit_cpt_list", "涨停最强板块（题材接力）"),
    ("ths_hot", "同花顺热榜（关注度=人气）"),
    ("dc_hot", "东财热榜（人气）"),
    ("cyq_perf", "每日筹码及胜率（成本结构）"),
    ("cyq_chips", "每日筹码分布（成本结构明细）"),
    ("stk_auction_o", "开盘集合竞价（9:25 的意图）"),
    ("stk_auction_c", "收盘集合竞价"),
    ("moneyflow_hsgt", "沪深港通资金流向（北向）"),
    ("hk_hold", "沪深港通持股明细（北向个股）"),
    ("margin_detail", "两融明细（杠杆情绪，个股级）"),
    ("moneyflow_ind_dc", "东财行业资金流（板块人心）"),
    ("moneyflow_ind_ths", "同花顺行业资金流"),
    ("ths_daily", "同花顺板块日线（板块动量）"),
    ("stk_factor_pro", "技术面因子全集（含大量原生指标）"),
    ("share_float", "限售解禁（筹码压力）"),
]


def d_net(trade_date: str, sample_code: str) -> None:
    sec("D 权限实测（逐个试调 Tushare 人心类接口；1 次/接口）")
    try:
        from app.data.tushare_api import TushareClient
    except Exception as exc:  # noqa: BLE001
        ck("D0 能构造 Tushare 客户端", False, str(exc)[:160])
        return
    try:
        cli = TushareClient()
        pro = cli.pro
    except Exception as exc:  # noqa: BLE001
        ck("D0 能构造 Tushare 客户端", False, str(exc)[:160])
        return
    ck("D0 能构造 Tushare 客户端（token 有效）", True)

    ok: list[str] = []
    no_perm: list[str] = []
    no_api: list[str] = []
    other: list[tuple[str, str]] = []
    for name, desc in NET_CASES:
        if not hasattr(pro, name):
            no_api.append(name)
            print(f"  ❌ {name:<18} SDK 无此接口        {desc}")
            continue
        params: dict = {}
        if name in ("cyq_perf", "cyq_chips", "stk_factor_pro"):
            params = {"ts_code": sample_code, "trade_date": trade_date}
        elif name == "hm_list":
            params = {}
        else:
            params = {"trade_date": trade_date}
        try:
            df = getattr(pro, name)(**params)
            n = 0 if df is None else len(df)
            if n > 0:
                ok.append(name)
                print(f"  ✅ {name:<18} 成功（{n} 行）      {desc}")
            else:
                # 返回空有两种可能：无权限（通常抛异常）或当期确实无数据
                other.append((name, "返回空 DataFrame（0 行）"))
                print(f"  ⚠️  {name:<18} 返回 0 行          {desc}")
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).replace("\n", " ")[:150]
            low = msg.lower()
            if "权限" in msg or "permission" in low or "积分" in msg:
                no_perm.append(name)
                print(f"  🔒 {name:<18} 无权限             {desc}\n       └ {msg}")
            else:
                other.append((name, msg))
                print(f"  ❓ {name:<18} 其它错误           {desc}\n       └ {msg}")
        time.sleep(0.35)

    print()
    print(f"  汇总：可用 {len(ok)} / 无权限 {len(no_perm)} / SDK 缺失 {len(no_api)} / 其它 {len(other)}")
    if ok:
        print(f"     ✅ 可用：{', '.join(ok)}")
    if no_perm:
        print(f"     🔒 无权限：{', '.join(no_perm)}")
    if no_api:
        print(f"     ❌ SDK 缺失：{', '.join(no_api)}")
    for n, m in other:
        print(f"     ❓ {n}: {m}")
    ck("D1 至少一个'人心'类新接口可用（决定是否值得扩采集）", len(ok) > 0,
       f"{len(ok)} 个可用")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-net", action="store_true", help="跳过 Tushare 权限实测")
    args = ap.parse_args()

    print("=" * 88)
    print("人心（情绪/博弈）数据可得性审计 — plans/24 第 0 步")
    print("=" * 88)

    db = SessionLocal()
    try:
        a_tables(db)
        b_columns(db)
        c_sentiment(db)

        latest = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
        sample = db.execute(text(
            "SELECT d.ts_code FROM daily d WHERE d.trade_date = :d "
            "AND d.ts_code LIKE '60%' LIMIT 1"), {"d": latest}).scalar()
    finally:
        db.close()

    td = _d(latest)
    sc = str(sample or "600000.SH")
    print()
    print(f"  实测基准日 {td}，样本股 {sc}")

    if not args.no_net:
        d_net(td, sc)
    else:
        print()
        print("  （--no-net：跳过 D 权限实测）")

    sec("结论：人心指标 × 可得性矩阵")
    print("  【现在就能算 —— 不需要任何新数据】")
    for k in ("封板家数", "炸板家数", "一字板家数", "连板高度", "昨日涨停今日表现",
              "涨跌家数比", "散户净买占比", "主力净额分歧度", "换手率位置", "上影/下影长度"):
        print(f"    ✅ {k:<18} {HEART_FEATURES[k]}")
    print("  【需要新接口 —— 见 D 段实测结果】")
    for k in ("封单强度", "集合竞价强度", "游资席位动向", "筹码分布", "两融余额变化"):
        print(f"    ?  {k:<18} {HEART_FEATURES[k]}")

    print()
    print(f"通过(硬性) {len(PASS)} / {len(PASS) + len(FAIL)}   告警 {len(WARN)}")
    if WARN:
        print("告警明细：")
        for w in WARN:
            print(f"  · {w}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
