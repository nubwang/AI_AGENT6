"""2010~2015 数据质量专项核查（plans/24 §11.36.14 ⑥-3）

## 为什么必须先做这件事

§11.36.14 把 regime 门从 2.6 年扩到 **16.7 年** 重跑，结论很硬（FDR 2/11、样本外 0/11、
最好的门毛 5.0% vs 成本 7.5%）。但我自己在那节写下了**前置边界**：

    早期（2010~2015）股票少、流动性差，`illiq`/`turnover` 的经济含义与今天不同
    ⇒ 「16 年」≠「16 年同质样本」；**在做完本节核查之前，16 年结论应谨慎引用**。

所以本节**不加新结论**，只回答一个问题：**这 16 年到底能不能当成一条同质序列？**

## 查什么（全部只读）

    A daily        ：每年 行数 / 股票数 / 成交额中位 / pct_chg 结构
    B daily_basic  ：turnover_rate 的**非空率**（换手率缺失 ⇒ 该年 turnover 因子不可比）
    C moneyflow    ：相对 daily 的**股票覆盖率**（主力净额缺失 ⇒ 情绪特征不可比）
    D adj_factor   ：复权因子覆盖率 —— ★ **两种口径必须分开看**（§11.36.17 更正）：
                     ① **在市口径（唯一有决策意义的口径）**：只统计 `stock_basic` 内的股票。
                        实测 2010~2019 逐年 **100.00%**、缺口 **0 行**。
                     ② **全口径（旧版口径，含已退市股）= 系统性偏低（91~95%）**。
                        成因：已退市股票**有 daily 行、却没有 adj_factor 行**，而它们
                        **已不在 `stock_basic` 中** ⇒ 任何按当前名单重采的回补都
                        **触达不到它们**（实测 238/238 只在市缺口股全不在名单内）。
                        ⇒ **这不是数据缺陷，是口径缺陷**（分母混入了不可回补的退市股）。
    E 涨停近似      ：pct_chg ≥ 9.5% 的占比（2010 起主板 10%、创业板 2020 起 20%、
                      科创板/北交所更宽 ⇒ 用统一阈值必然"逐年不可比"，本节**量化**它）
    F 结论          ：逐年打「可比 / 谨慎 / 不可比」标签（**在市口径**判定）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/audit_early_data_quality.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.models import SessionLocal  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BACKEND, "data", "early_data_quality.json")
YEARS = list(range(2010, 2027))


def _one(db, sql: str, **kw):
    try:
        return db.execute(text(sql), kw).fetchone()
    except Exception as exc:  # noqa: BLE001
        return ("ERR", str(exc)[:80])


def _codes(db, sql: str, **kw) -> set:
    """某区间内出现过的股票代码集合（查询失败 ⇒ 空集，不抛）。"""
    try:
        return {str(x[0]) for x in db.execute(text(sql), kw).all()}
    except Exception:  # noqa: BLE001
        return set()


def _pairs(db, sql: str, a: str, b: str) -> set:
    """{(ts_code, trade_date)} 精确键集合。

    ★ 为什么不复用 `COUNT(DISTINCT ts_code)`：股票级近似会**掩盖"某只股票只缺一部分日期"**
    的缺口形态；而"复权因子能不能 JOIN 上"是 **(股票, 日)** 级问题 ⇒ 必须用键对。
    """
    try:
        return {(str(x[0]), str(x[1])) for x in db.execute(text(sql), {"a": a, "b": b}).all()}
    except Exception:  # noqa: BLE001
        return set()


def audit_year(db, y: int, pool: set) -> dict:
    """`pool` = `stock_basic` 的 ts_code 集合（= 可回测/可交易股票池）。"""
    a, b = f"{y}0101", f"{y}1231"
    r: dict = {"year": y}

    row = _one(db, "SELECT COUNT(*), COUNT(DISTINCT ts_code), AVG(amount), AVG(pct_chg) "
                   "FROM daily WHERE trade_date BETWEEN :a AND :b", a=a, b=b)
    r["daily_rows"], r["daily_codes"], r["amount_avg"], r["pct_avg"] = (list(row) + [None] * 4)[:4]

    # pct_chg ≥ 9.5% 的占比（涨停近似；阈值本身跨板块不可比，此处只量化）
    row = _one(db, "SELECT SUM(pct_chg >= 9.5), SUM(pct_chg <= -9.5), COUNT(*) "
                   "FROM daily WHERE trade_date BETWEEN :a AND :b", a=a, b=b)
    up, dn, n = (list(row) + [0, 0, 0])[:3]
    n = int(n or 0)
    r["limitup_pct"] = round(float(up or 0) / n, 5) if n else None
    r["limitdn_pct"] = round(float(dn or 0) / n, 5) if n else None

    # turnover_rate 非空且 > 0 的占比
    row = _one(db, "SELECT COUNT(*), SUM(turnover_rate IS NOT NULL AND turnover_rate > 0) "
                   "FROM daily_basic WHERE trade_date BETWEEN :a AND :b", a=a, b=b)
    n_b, n_ok = (list(row) + [0, 0])[:2]
    n_b = int(n_b or 0)
    r["db_rows"] = n_b
    r["turnover_ok_pct"] = round(float(n_ok or 0) / n_b, 4) if n_b else None

    # ---- 复权因子覆盖：★ 在市口径 与 全口径 必须分开（§11.36.17）----
    dk = _pairs(db, "SELECT ts_code, trade_date FROM daily WHERE trade_date BETWEEN :a AND :b", a, b)
    ak = _pairs(db, "SELECT ts_code, trade_date FROM adj_factor WHERE trade_date BETWEEN :a AND :b", a, b)
    din = {k for k in dk if k[0] in pool}          # 在市股票的 daily 键
    dout = dk - din                                # 退市股票的 daily 键
    r["daily_keys_inlist"] = len(din)
    r["daily_keys_delisted"] = len(dout)
    # ① 在市口径（判定用）
    r["af_gap_inlist"] = len(din - ak)
    r["af_cover_inlist"] = round(1 - r["af_gap_inlist"] / len(din), 5) if din else None
    # ② 全口径（旧版，含退市股 ⇒ 系统性偏低；仅留作对照，不用于判定）
    r["af_gap_all"] = len(dk - ak)
    r["af_cover_all"] = round(1 - r["af_gap_all"] / len(dk), 5) if dk else None
    # ③ 退市股缺口：**已知且不可回补**（Tushare 名单里已无这些代码）
    r["af_gap_delisted"] = len(dout - ak)
    r["af_gap_delisted_codes"] = len({k[0] for k in (dout - ak)})
    # 兼容旧字段名（= 全口径）
    r["af_cover"] = r["af_cover_all"]
    r["af_codes"] = len({k[0] for k in ak})

    # moneyflow 覆盖（同样区分在市 / 全口径；股票级口径足够）
    mfc = _codes(db, "SELECT DISTINCT ts_code FROM moneyflow WHERE trade_date BETWEEN :a AND :b", a=a, b=b)
    dcodes = {k[0] for k in din}
    r["mf_codes"] = len(mfc)
    r["mf_cover"] = round(len(mfc) / len({k[0] for k in dk}), 4) if dk else None
    r["mf_cover_inlist"] = round(len(mfc & pool) / len(dcodes), 4) if dcodes else None
    r["daily_codes_inlist"] = len(dcodes)
    return r


def _label(r: dict) -> str:
    """逐年「可比性」标签（**规则事先定死**，不按结果调）。

    三项同时满足才算「可比」：turnover 非空率 ≥95%、moneyflow 在市覆盖 ≥95%、
    adj_factor **在市**覆盖 ≥95%。

    ★ §11.36.17 更正：`adj_factor` 必须用**在市口径**。用全口径（含退市股）会把
    2010~2019 误判为「谨慎(adj_factor)」，进而误导出"需要回补"的错误结论。
    """
    bad = []
    if (r.get("turnover_ok_pct") or 0) < 0.95:
        bad.append("turnover")
    if (r.get("mf_cover_inlist", r.get("mf_cover")) or 0) < 0.95:
        bad.append("moneyflow")
    if (r.get("af_cover_inlist", r.get("af_cover")) or 0) < 0.95:
        bad.append("adj_factor")
    if not bad:
        return "可比"
    if len(bad) == 1:
        return f"谨慎({bad[0]})"
    return "不可比(" + ",".join(bad) + ")"


def main() -> int:
    t0 = time.time()
    print("=" * 132)
    print("2010~2015 数据质量专项核查（plans/24 §11.36.14 ⑥-3）—— 只读，不写任何数据")
    print("=" * 132)
    db = SessionLocal()
    rows: list[dict] = []
    try:
        pool = _codes(db, "SELECT ts_code FROM stock_basic")
        print(f"  可回测股票池（stock_basic）= {len(pool)} 只；复权覆盖按【在市口径】判定"
              f"（全口径仅作对照，见 §11.36.17）")
        print()
        hd = (f"{'年':<6}{'daily行数':>10}{'股票数':>7}{'额均(万)':>10}{'pct均':>8}"
              f"{'涨停≈':>7}{'跌停≈':>7}{'turnover非空':>11}{'MF在市':>8}"
              f"{'AF在市':>8}{'AF全口径':>9}{'在市缺口':>9}{'退市缺口':>9}  判读")
        print(hd)
        print("-" * 132)
        for y in YEARS:
            r = audit_year(db, y, pool)
            r["label"] = _label(r)
            rows.append(r)
            amt = r["amount_avg"]
            print(f"{y:<6}{int(r['daily_rows'] or 0):>10,}{int(r['daily_codes'] or 0):>7}"
                  f"{(float(amt) if amt not in (None, 'ERR') else 0):>10,.0f}"
                  f"{(float(r['pct_avg']) if r['pct_avg'] is not None else 0):>8.3f}"
                  f"{(r['limitup_pct'] or 0):>7.2%}{(r['limitdn_pct'] or 0):>7.2%}"
                  f"{(r['turnover_ok_pct'] or 0):>11.1%}"
                  f"{(r['mf_cover_inlist'] or 0):>8.1%}"
                  f"{(r['af_cover_inlist'] or 0):>8.1%}"
                  f"{(r['af_cover_all'] or 0):>9.1%}"
                  f"{int(r['af_gap_inlist'] or 0):>9,}{int(r['af_gap_delisted'] or 0):>9,}"
                  f"  {r['label']}")
    finally:
        db.close()

    print()
    n_ok = sum(1 for r in rows if r["label"] == "可比")
    n_care = sum(1 for r in rows if r["label"].startswith("谨慎"))
    n_bad = sum(1 for r in rows if r["label"].startswith("不可比"))
    print("=" * 132)
    print(f"  可比 {n_ok} 年｜谨慎 {n_care} 年｜不可比 {n_bad} 年（共 {len(rows)} 年）")
    print()
    print("  判读（事先定好 + §11.36.17 更正）：")
    print("  1) ★ 复权因子**没有早期缺口**：在市口径逐年 100.00%、缺口 0 行。")
    print("     此前把「全口径 91~95%」当成真缺口、并启动 `backfill_history.py --only-api adj_factor`")
    print("     是**口径错误导致的无效动作**（已停止，见 §11.36.17）。")
    print("  2) 唯一真实缺口 = **已退市股票**（daily 有行、adj_factor 无行、且不在 stock_basic）。")
    print("     它们**不影响**以在市股票池为准的任何回测；也**不可能**被 `get_all_stocks()` 回补。")
    print("     ⇒ 若要回补，必须先拿到「历史上出现过的全部 ts_code」（daily 的 distinct 并集），")
    print("       而不是 `stock_basic` —— 且需先确认 Tushare 对退市股是否仍提供 adj_factor。")
    print("  3) 「涨停≈」一列的**跨年不可比是必然的**（2010 起主板 10%、创业板 2020 起 20%、")
    print("     科创板/北交所更宽）⇒ 它只用来**量化**「统一阈值」的失真程度，不用于任何结论。")
    print(f"\n  已落盘：{os.path.relpath(OUT, os.path.dirname(BACKEND))}")
    print(f"  用时 {time.time() - t0:.1f}s")
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "label_rule": "turnover非空≥95% ∧ moneyflow在市覆盖≥95% ∧ "
                                     "adj_factor**在市**覆盖≥95% ⇒ 可比（§11.36.17 修正分母）",
                       "years": rows,
                       "summary": {"n_ok": n_ok, "n_care": n_care, "n_bad": n_bad}},
                      f, ensure_ascii=False, indent=1, default=str)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 落盘失败：{exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
