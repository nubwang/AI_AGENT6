"""验收：市场情绪温度日线（plans/24 人心博弈层 H1）

被验收对象：
    [`sentiment.py`](ai-quant-agent/backend/app/backtest/sentiment.py:1)
    产物 `backend/data/sentiment_daily.json`

分组：
  A 结构完整性（字段/日期格式/交易日数）
  B 交叉验证 —— 用**独立 SQL 重算**同一天，与模块结果逐项对比
  C ★ 无未来函数（含"篡改数据"测试 + 最新一行的实盘语义）
  D 连板逻辑抽查（最高板必须能被独立回溯复现）
  E 赚钱效应抽查（独立重算均值）
  F 护栏（共线字段不许出现；不许被生产链路引用）
  G 日期格式护栏（把今天踩到的"字符串列静默失效"坑固化）

前置：若 data/sentiment_daily.json 不存在，请先运行
    cd ai-quant-agent/backend && ./venv/bin/python -m app.backtest.sentiment
本脚本**只读**已固化产物，不重建（重建约 6 分钟）。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.backtest import sentiment as ST
from app.models import SessionLocal

PASS, FAIL = [], []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(BACKEND, "app")

# 独立 SQL 版口径：按板块分档（与模块 _limit_pct 同义，但用 SQL 重写一遍）
_SQL_FLAGS = """
SELECT
  SUM(CASE WHEN lim = 20 AND pct_chg >= 19.7 THEN 1
           WHEN lim = 30 AND pct_chg >= 29.7 THEN 1
           WHEN lim = 10 AND pct_chg >=  9.7 THEN 1 ELSE 0 END) AS n_up,
  SUM(CASE WHEN lim = 20 AND pct_chg <= -19.7 THEN 1
           WHEN lim = 30 AND pct_chg <= -29.7 THEN 1
           WHEN lim = 10 AND pct_chg <=  -9.7 THEN 1 ELSE 0 END) AS n_down,
  SUM(CASE WHEN pct_chg < (lim - 0.3)
            AND (high / pre_close - 1) * 100 >= (lim - 0.3) THEN 1 ELSE 0 END) AS n_zha,
  COUNT(*) AS n_stocks
FROM (
  SELECT pct_chg, high, pre_close,
    CASE WHEN ts_code LIKE '688%' OR ts_code LIKE '300%'
           OR ts_code LIKE '301%' THEN 20
         WHEN ts_code LIKE '8%' OR ts_code LIKE '4%' THEN 30
         ELSE 10 END AS lim
  FROM daily
  WHERE trade_date = :d AND pre_close > 0 AND pct_chg IS NOT NULL
) t
"""


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '⚠️ '} {name}" + (f" — {detail}" if detail else ""))


def group_a(data: dict) -> list[str]:
    print("=== A 结构完整性 ===")
    days = data.get("days") or {}
    keys = sorted(days)
    ck("A1 产物存在且含 days", bool(days), f"{len(days)} 个交易日")
    ck("A2 区间覆盖多年（≥ 1 年交易日数 240）", len(days) >= 240,
       f"{data.get('range')} → {len(days)} 日")
    need = {"n_stocks", "n_up", "n_down", "n_zha", "n_yizi", "seal_ratio", "zha_rate",
            "n_lb2", "max_board", "money_effect", "up_down_ratio", "main_net", "usable_from"}
    last = days[keys[-1]]
    miss = need - set(last)
    ck("A3 字段齐全（含 usable_from）", not miss, f"缺失={sorted(miss) or '无'}")
    ck("A4 日期键为 8 位 YYYYMMDD 字符串",
       all(len(k) == 8 and k.isdigit() for k in keys), f"样例 {keys[-1]}")
    ck("A5 n_up/n_zha 非负且 n_stocks 合理",
       all(days[k]["n_up"] >= 0 and days[k]["n_zha"] >= 0 and days[k]["n_stocks"] > 100
           for k in keys), f"末日 n_stocks={last['n_stocks']}")
    ck("A6 seal_ratio + zha_rate 逻辑一致（=1 或有分母）",
       all((v["seal_ratio"] is None) == (v["zha_rate"] is None) for v in days.values()))
    return keys


def group_b(data: dict, keys: list[str]) -> None:
    print("=== B 交叉验证：独立 SQL 重算 vs 模块结果 ===")
    days = data["days"]
    db = SessionLocal()
    try:
        checked = 0
        for d in (keys[-1], keys[len(keys) // 2], keys[0]):
            r = db.execute(text(_SQL_FLAGS), {"d": d}).fetchone()
            if not r:
                continue
            n_up, n_down, n_zha, n_stocks = (int(r[0] or 0), int(r[1] or 0),
                                             int(r[2] or 0), int(r[3] or 0))
            v = days[d]
            ok = (v["n_up"] == n_up and v["n_down"] == n_down
                  and v["n_zha"] == n_zha and v["n_stocks"] == n_stocks)
            ck(f"B 独立重算一致（{d}）", ok,
               f"涨停 {v['n_up']}(模块)/{n_up}(SQL) 炸板 {v['n_zha']}/{n_zha} "
               f"跌停 {v['n_down']}/{n_down} 基数 {v['n_stocks']}/{n_stocks}")
            checked += 1
        ck("B 至少交叉验证了 1 个交易日", checked > 0, f"{checked} 天")
    finally:
        db.close()


def group_c(data: dict, keys: list[str]) -> None:
    print("=== C ★ 无未来函数 ===")
    days = data["days"]
    # C1 usable_from 必须等于"下一个交易日"（最后一行可为空）
    bad = []
    for i, k in enumerate(keys):
        uf = str(days[k].get("usable_from") or "")
        want = keys[i + 1] if i + 1 < len(keys) else ""
        if uf != want:
            bad.append((k, uf, want))
    ck("C1 每行 usable_from == 下一交易日（末日可为空）", not bad,
       f"不符={bad[:3] or '无'}")
    ck("C2 最后一行 usable_from 为空（数据到此为止，无下一交易日）",
       str(days[keys[-1]].get("usable_from") or "") == "")

    # C3 sentiment_asof(T) 的 source_date 必须严格早于 T
    t = keys[-1]
    row = ST.sentiment_asof(t)
    ck("C3 sentiment_asof(末日) 返回严格早于该日的读数",
       bool(row) and str(row["_source_date"]) < t,
       f"source={row.get('_source_date') if row else None} < {t}")

    # C4 对区间中段某日：返回的应是它的前一日
    mid = keys[len(keys) // 2]
    r2 = ST.sentiment_asof(mid)
    expect = keys[keys.index(mid) - 1]
    ck("C4 sentiment_asof(某日) 返回前一交易日（不越界）",
       bool(r2) and r2["_source_date"] == expect,
       f"got={r2.get('_source_date') if r2 else None} want={expect}")

    # C5 ★ 篡改测试：把当日 n_up 改成 99999，asof(当日) 必须拿不到它
    tmp = os.path.join(BACKEND, "data", "_sentiment_tampered.json")
    try:
        mut = json.loads(json.dumps(data))
        mut["days"][t]["n_up"] = 99999
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mut, f, ensure_ascii=False)
        r3 = ST.sentiment_asof(t, path=tmp)
        ck("C5 ★ 篡改当日数据后，asof(当日) 仍拿不到它（无未来函数）",
           bool(r3) and r3["n_up"] != 99999,
           f"n_up={r3['n_up'] if r3 else None}（应为篡改前的值，不是 99999）")
        # C6 但 asof(更晚的日期) 应当能拿到（证明"可用于其后任意日"）
        r4 = ST.sentiment_asof("99999999", path=tmp)
        ck("C6 更晚日期可拿到最新一行（实盘不滞后一天）",
           bool(r4) and r4["n_up"] == 99999 and r4["_source_date"] == t,
           f"source={r4.get('_source_date') if r4 else None} n_up={r4['n_up'] if r4 else None}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    # C7 早于数据起点 → 返回 None（不许拿"最早一行"当昨天的读数）
    ck("C7 早于数据起点的日期返回 None", ST.sentiment_asof("20000101") is None)


def _up_set(db, d: str) -> set:
    """独立 SQL 版的当日涨停集合。"""
    q = ("SELECT ts_code FROM daily WHERE trade_date = :d AND pre_close > 0 "
         "AND pct_chg IS NOT NULL AND pct_chg >= (CASE "
         "  WHEN ts_code LIKE '688%' OR ts_code LIKE '300%' OR ts_code LIKE '301%' THEN 19.7 "
         "  WHEN ts_code LIKE '8%' OR ts_code LIKE '4%' THEN 29.7 ELSE 9.7 END)")
    return {str(a) for a, in db.execute(text(q), {"d": d}).fetchall()}


def group_d(data: dict, keys: list[str]) -> None:
    print("=== D 连板逻辑抽查（独立回溯）===")
    days = data["days"]
    # 取区间内 max_board 最大的那一天
    day = max(keys, key=lambda k: days[k]["max_board"])
    mb = int(days[day]["max_board"])
    idx = keys.index(day)
    db = SessionLocal()
    try:
        if idx + 1 < mb:
            ck("D1 该日之前交易日足够回溯", False, f"{day} 需 {mb} 天但只有 {idx + 1} 天")
            return
        window = keys[idx - mb + 1: idx + 1]           # mb 天（含当日）
        inter = None
        for d in window:
            s = _up_set(db, d)
            inter = s if inter is None else (inter & s)
        ck(f"D1 存在连续 {mb} 板个股（{day}，最高板={mb}）",
           bool(inter), f"交集 {len(inter or [])} 只")
        # 反证：mb+1 天不可能
        if idx - mb >= 0:
            w2 = keys[idx - mb: idx + 1]
            inter2 = None
            for d in w2:
                s = _up_set(db, d)
                inter2 = s if inter2 is None else (inter2 & s)
            ck(f"D2 不存在连续 {mb + 1} 板（证明 max_board 不是虚高）",
               not inter2, f"交集 {len(inter2 or [])} 只")
    finally:
        db.close()


def _money_effect_of(db, prev_d: str, d: str) -> tuple[float | None, int]:
    """独立重算赚钱效应：昨日涨停集合 在 d 日的 pct_chg 均值。"""
    pu = _up_set(db, prev_d)
    q = ("SELECT ts_code, pct_chg FROM daily WHERE trade_date = :d "
         "AND pct_chg IS NOT NULL")
    m = {str(a): float(b) for a, b in db.execute(text(q), {"d": d}).fetchall()}
    vals = [m[c] for c in pu if c in m]
    if not vals:
        return None, len(pu)
    return sum(vals) / len(vals), len(pu)


def group_e(data: dict, keys: list[str]) -> None:
    print("=== E 赚钱效应抽查（独立重算）===")
    days = data["days"]
    t = keys[-1]
    prev = keys[-2]
    db = SessionLocal()
    try:
        want, n_pu = _money_effect_of(db, prev, t)
        got = days[t]["money_effect"]
        ck("E1 赚钱效应与独立重算一致（末日）",
           got is not None and want is not None and abs(got - want) < 0.02,
           f"模块 {got} vs 独立 {round(want, 3) if want else None}（昨日涨停 {n_pu} 只）")

        # ⚠️ 不要用"±11% 之类"的区间断言：昨日涨停股今日**可以再涨停**
        #    （双创 20% / 北交所 30%），均值天然能超过主板 10%。
        #    正确的做法是：对**极值日**做独立重算（强验证），而不是拿想象的界去凑。
        vals = [(days[k]["money_effect"], k) for k in keys
                if days[k]["money_effect"] is not None]
        lo = min(vals)[0]
        hi = max(vals)[0]
        ck("E2 所有赚钱效应取值有限（非 NaN/Inf）",
           all(v is not None and abs(v) < 100 for v, _ in vals),
           f"min {lo:.2f}% / max {hi:.2f}%（上限受 30% 板影响，不设人为区间）")

        best = max(vals, key=lambda x: abs(x[0]))[1]
        i = keys.index(best)
        if i > 0:
            w2, n2 = _money_effect_of(db, keys[i - 1], best)
            g2 = days[best]["money_effect"]
            ck(f"E3 ★ 极值日（{best}）独立重算一致",
               w2 is not None and g2 is not None and abs(g2 - w2) < 0.02,
               f"模块 {g2} vs 独立 {round(w2, 3) if w2 else None}（|值|最大，{n2} 只）")
    finally:
        db.close()


def group_f(data: dict) -> None:
    print("=== F 护栏 ===")
    last = next(iter(data["days"].values()))
    ck("F1 产物不含 retail_net（避免与 main_net 共线，plans/24 §3.5）",
       "retail_net" not in last, f"字段={sorted(last)}")
    ck("F2 产物声明了数据口径与无未来函数",
       "pct_chg" in str(data.get("meta", {}).get("price_basis", ""))
       and "usable_from" in str(data.get("meta", {}).get("no_lookahead", "")),
       "meta.price_basis / meta.no_lookahead")
    # 是否已被生产链路引用（H1 阶段应为"未引用"，防悄悄上线）
    refs = []
    for root, _dirs, files in os.walk(APP_DIR):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn)
            if os.path.abspath(p) == os.path.abspath(ST.__file__):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    txt = f.read()
            except Exception:  # noqa: BLE001
                continue
            if "backtest import sentiment" in txt or "backtest.sentiment" in txt:
                refs.append(os.path.relpath(p, BACKEND))
    ck("F3 H1 阶段情绪模块尚未被生产链路引用（防悄悄上线）", not refs,
       f"引用方={refs or '无'}")


def group_g(db) -> None:
    print("=== G 日期格式护栏（今天实测踩到的坑）===")
    mn = db.execute(text("SELECT MIN(trade_date) FROM daily")).scalar()
    s = str(mn)
    ck("G1 daily.trade_date 是 8 位数字串（不是 DATE 类型）",
       len(s) == 8 and s.isdigit(),
       f"MIN={s} —— 若为 '2010-01-04' 则本护栏与模块假设都需重审")
    n1 = db.execute(text("SELECT COUNT(*) FROM daily WHERE trade_date >= '2024-01-01' "
                         "AND trade_date <= '2024-12-31'")).scalar()
    n2 = db.execute(text("SELECT COUNT(*) FROM daily WHERE trade_date >= '20240101' "
                         "AND trade_date <= '20241231'")).scalar()
    ck("G2 ★ 带 '-' 的区间查询会**静默失效**（返回 0 行，不报错）",
       int(n1 or 0) == 0 and int(n2 or 0) > 0,
       f"'2024-01-01'~'2024-12-31' → {n1} 行；'20240101'~'20241231' → {n2} 行")
    ck("G3 模块使用的 YYYYMMDD 格式确实有效", int(n2 or 0) > 0, f"{n2} 行")


def main() -> int:
    print("=" * 88)
    print("验收：市场情绪温度日线（plans/24 H1）")
    print("=" * 88)
    data = ST.load_sentiment()
    if not data or not data.get("days"):
        print("⚠️  未找到产物 data/sentiment_daily.json")
        print("   请先运行： ./venv/bin/python -m app.backtest.sentiment")
        return 1

    keys = group_a(data)
    print()
    group_b(data, keys)
    print()
    group_c(data, keys)
    print()
    group_d(data, keys)
    print()
    group_e(data, keys)
    print()
    group_f(data)
    print()
    db = SessionLocal()
    try:
        group_g(db)
    finally:
        db.close()

    print()
    print("=" * 88)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("未通过：")
        for f in FAIL:
            print(f"  ⚠️  {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
