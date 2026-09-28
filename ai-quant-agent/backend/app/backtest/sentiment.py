"""市场情绪温度（sentiment）— plans/24 人心博弈层 H1

## 为什么需要它

plans/23 §15.6 实测：图形本身不赚钱 —— 样本在 D+1 开盘口径下 T+5 期望 **−5.55%**，
止损出场占 **78.9%**。plans/24 的判断：缺的是"**该不该出手**"这一层，
即"当前处在情绪周期的哪个位置"。

本模块把"人心"变成每日一行、可回测的读数（plans/24 §2.2 已实测全部可算）。

## 产出（每个交易日一行）

| 字段           | 含义                                     | 人心解读                       |
| -------------- | ---------------------------------------- | ------------------------------ |
| n_stocks       | 全市场股票数                             | 样本基数                       |
| n_up           | 涨停家数                                 | 做多意愿总量                   |
| n_down         | 跌停家数                                 | 恐慌总量                       |
| n_zha          | 炸板家数（盘中触板、收盘未封）           | **做多意愿的瓦解速度**         |
| n_yizi         | 一字板家数（全天未打开）                 | 最强一致预期（无人愿卖）       |
| seal_ratio     | 封板率 = 涨停 /(涨停+炸板)                | 封板决心                       |
| zha_rate       | 炸板率 = 炸板 /(涨停+炸板)                | 情绪碎裂度（越大越危险）       |
| n_lb2          | 连板 ≥2 家数                             | 情绪延续性                     |
| max_board      | 最高连板高度                             | **情绪天花板**                 |
| money_effect   | 赚钱效应 = 昨日涨停股今日平均涨幅         | **散户明天敢不敢接盘**         |
| up_down_ratio  | 涨停/跌停 比                             | 多空力量对比                   |
| main_net       | 主力净额合计（万元）                     | 主力在进还是出                 |

⚠️ `retail_net` **恒等于 −main_net**（plans/24 §3.5 实测的恒等式），
本模块**不输出**该字段，以避免调用方误把两者当独立特征（完全共线）。

## ⚠️ 无未来函数（本模块最重要的设计）

上表所有字段都是 **T 日收盘后**才可知的数据，因此：
**只能用于 T+1 及之后的开仓决策，绝不能用于 T 日盘中。**

落盘时每行带 `usable_from = 下一个交易日`；取值函数 `sentiment_asof(date)`
会**强制**按此规则返回（若 `date < usable_from` 则该行不可用），
从代码层面挡住"拿今天收盘的情绪去做今天开盘的决策"这类未来函数。

## ⚠️ 日期格式实测（踩过一次，必须记住）

`daily.trade_date` 等列**实际是字符串（YYYYMMDD）**，不是 DATE 类型：
    MIN(trade_date) 返回 '20100104'（8 位纯数字），
    而 `WHERE trade_date <= '2024-12-31'` 因**字符串比较**（'0' > '-'）而**全部失效**
    → 实测 2023/2024 年区间查询返回 0 行，但单边 `>= '2024-01-01'` 却有 356 万行。
（[`models/stock.py`](ai-quant-agent/backend/app/models/stock.py:1) 声明为 `Column(Date)`，
与实际表不符 —— 这是本模块实测发现的第 2 处"ORM 声明 ≠ 实际表"。）

**因此：所有日期参数必须以 `YYYYMMDD` 字符串传入**（如 '20240101'），
绝不能用 'YYYY-MM-DD'，否则区间过滤会**静默失效**（不报错、只返回错数据）。

## 数据口径（plans/24 §2.4-③ 实测钉死）

全市场封板统计**必须用 `pct_chg` 口径**，不能用 `stk_limit`：
后者的覆盖率实测仅 **42.5%**（20260918：2361 行 / 全市场 5553 只），
且 `daily` × `stk_limit` 因 `ts_code` 排序规则冲突（`utf8mb4_0900_ai_ci` vs
`utf8mb4_unicode_ci`）**在 SQL 层无法 JOIN**（报 1267）。`stk_limit` 仅用于个股级校验。

## 用法

    cd ai-quant-agent/backend && ./venv/bin/python -m app.backtest.sentiment          # 近 3 年
    cd ai-quant-agent/backend && ./venv/bin/python -m app.backtest.sentiment --years 8
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.logger import logger
from app.models import SessionLocal

SENTIMENT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "sentiment_daily.json",
)

DEFAULT_YEARS = 3
ZHA_TOL = 0.3      # 涨停判定容忍（pct_chg 的取整误差）
YIZI_TOL = 1.0     # 一字板判定：最低价也需贴近涨停（全天未打开）

# 输出字段顺序（便于人工核对）
FIELDS = ("n_stocks", "n_up", "n_down", "n_zha", "n_yizi", "seal_ratio", "zha_rate",
          "n_lb2", "max_board", "money_effect", "up_down_ratio", "main_net", "usable_from")


def _limit_pct(ts_code: str) -> float:
    """按板块推断涨跌停幅度。

    近似口径（与 audit_heart_data 一致）：主板 10% / 双创 20% / 北交所 30%。
    ST 股实际为 5%，本模块不区分（ST 已在推荐链路被 risk_filter 排除；
    回测层面 ST 造成的少量误判不影响"市场级情绪"的统计意义）。
    """
    c = str(ts_code).split(".")[0]
    if c.startswith(("688", "300", "301")):
        return 0.20
    if c.startswith(("8", "4")):
        return 0.30
    return 0.10


def _year_spans(d0: str, d1: str) -> list[tuple[str, str]]:
    """把日期区间按自然年切段（分段查询，控制单次内存）。

    ⚠️ 必须用 `YYYYMMDD` 字符串（表列为字符串类型，见模块 docstring 的日期格式警告）。
    8 位定长数字串的字典序 == 数值序，所以 max/min 直接可用。
    """
    spans: list[tuple[str, str]] = []
    for y in range(int(d0[:4]), int(d1[:4]) + 1):
        a = max(f"{y}0101", d0)
        b = min(f"{y}1231", d1)
        if a <= b:
            spans.append((a, b))
    return spans


def _load_daily(d0: str, d1: str) -> pd.DataFrame:
    """分段读取 daily 的价量核心列（只取算情绪必需的 7 列，省内存）。"""
    parts: list[pd.DataFrame] = []
    sql = ("SELECT trade_date, ts_code, high, low, close, pre_close, pct_chg "
           "FROM daily WHERE trade_date >= :a AND trade_date <= :b")
    with SessionLocal() as db:
        conn = db.connection()
        for a, b in _year_spans(d0, d1):
            df = pd.read_sql(text(sql), conn, params={"a": a, "b": b})
            if df is not None and not df.empty:
                parts.append(df)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    for c in ("high", "low", "close", "pre_close", "pct_chg"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def _load_moneyflow(d0: str, d1: str) -> pd.DataFrame:
    """分段读取 moneyflow，聚合成每日主力净额合计（万元）。

    主力 = 大单 + 特大单。⚠️ 散户 = 小单 + 中单，且恒等于 −主力（§24.3.5），故不输出。
    真实列名以 information_schema 实测为准（不是 models/stock.py 那份 ORM 定义）。
    """
    _MAIN = ("(IFNULL(buy_lg_amount,0)+IFNULL(buy_elg_amount,0)"
             "-IFNULL(sell_lg_amount,0)-IFNULL(sell_elg_amount,0))")
    sql = (f"SELECT trade_date, SUM({_MAIN}) AS main_net "
           "FROM moneyflow WHERE trade_date >= :a AND trade_date <= :b "
           "GROUP BY trade_date")
    parts: list[pd.DataFrame] = []
    with SessionLocal() as db:
        conn = db.connection()
        for a, b in _year_spans(d0, d1):
            df = pd.read_sql(text(sql), conn, params={"a": a, "b": b})
            if df is not None and not df.empty:
                parts.append(df)
    if not parts:
        return pd.DataFrame(columns=["trade_date", "main_net"])
    out = pd.concat(parts, ignore_index=True)
    out["main_net"] = pd.to_numeric(out["main_net"], errors="coerce")
    return out


def _flag_market(df: pd.DataFrame) -> pd.DataFrame:
    """给每行打上 涨停/跌停/炸板/一字板 标记（向量化）。"""
    d = df.copy()
    lim = d["ts_code"].astype(str).map(_limit_pct).to_numpy(dtype="float64")
    thr = lim * 100.0 - ZHA_TOL                       # 涨停阈值（pct_chg 口径）
    pc = d["pct_chg"].to_numpy(dtype="float64")
    pre = d["pre_close"].to_numpy(dtype="float64")
    hi = d["high"].to_numpy(dtype="float64")
    lo = d["low"].to_numpy(dtype="float64")

    with np.errstate(divide="ignore", invalid="ignore"):
        hi_pct = np.where(pre > 0, (hi / pre - 1.0) * 100.0, np.nan)
        lo_pct = np.where(pre > 0, (lo / pre - 1.0) * 100.0, np.nan)

    valid = np.isfinite(pc)
    is_up = valid & (pc >= thr)
    is_down = valid & (pc <= -thr)
    # 炸板：盘中摸到涨停但收盘没封住
    is_zha = valid & (~is_up) & np.isfinite(hi_pct) & (hi_pct >= thr)
    # 一字板：涨停且全天最低价也贴着涨停（没有给出成交机会）
    is_yizi = is_up & np.isfinite(lo_pct) & (lo_pct >= thr - YIZI_TOL)

    d["_up"] = is_up
    d["_down"] = is_down
    d["_zha"] = is_zha
    d["_yizi"] = is_yizi
    return d


def build_sentiment(years: int = DEFAULT_YEARS, d0: str = "", d1: str = "",
                    save: bool = True) -> dict:
    """构建情绪温度日线。返回 {range, days: {date: {...}}, meta}。"""
    t_start = time.time()
    if not d1:
        with SessionLocal() as db:
            mx = db.execute(text("SELECT MAX(trade_date) FROM daily")).scalar()
        d1 = str(mx).replace("-", "") if mx else ""
    if not d0:
        y = int(d1[:4]) - int(years) + 1
        d0 = f"{y}0101"
    if not d0 or not d1:
        return {"ok": False, "msg": "无法确定日期区间（daily 为空？）"}

    raw = _load_daily(d0, d1)
    if raw.empty:
        return {"ok": False, "msg": f"daily 在 {d0}~{d1} 无数据"}
    flagged = _flag_market(raw)

    # ── 按日聚合基础计数 ──
    g = flagged.groupby("trade_date", sort=True)
    base = pd.DataFrame({
        "n_stocks": g.size(),
        "n_up": g["_up"].sum(),
        "n_down": g["_down"].sum(),
        "n_zha": g["_zha"].sum(),
        "n_yizi": g["_yizi"].sum(),
    }).reset_index()
    base["trade_date"] = base["trade_date"].astype(str).str.replace("-", "", regex=False)

    # ── 连板高度：需要跨日追溯，按日维护 streak ──
    up_rows = flagged.loc[flagged["_up"], ["trade_date", "ts_code"]].copy()
    up_rows["trade_date"] = up_rows["trade_date"].astype(str).str.replace("-", "", regex=False)
    up_by_day: dict[str, set] = {}
    for day, grp in up_rows.groupby("trade_date", sort=True):
        up_by_day[str(day)] = set(grp["ts_code"].astype(str))

    # ── 赚钱效应：昨日涨停股今日的平均涨幅（需要"当日 pct_chg"映射）──
    pct = flagged[["trade_date", "ts_code", "pct_chg"]].copy()
    pct["trade_date"] = pct["trade_date"].astype(str).str.replace("-", "", regex=False)
    pct["ts_code"] = pct["ts_code"].astype(str)
    pct["day_i"] = pct["trade_date"].astype("int32")

    days_sorted = sorted(up_by_day)
    streak: dict[str, int] = {}
    n_lb2: dict[str, int] = {}
    max_board: dict[str, int] = {}
    for day in days_sorted:
        ups = up_by_day[day]
        for c in ups:
            streak[c] = streak.get(c, 0) + 1
        streak = {c: v for c, v in streak.items() if c in ups}   # 断板清零
        n_lb2[day] = sum(1 for v in streak.values() if v >= 2)
        max_board[day] = max(streak.values()) if streak else 0

    # 赚钱效应：把"昨日涨停"映射到"下一交易日"，再与当日 pct_chg 对齐
    idx_of = {d: i for i, d in enumerate(days_sorted)}
    prev_up = []           # (next_day, ts_code)
    for day in days_sorted:
        i = idx_of[day]
        if i + 1 >= len(days_sorted):
            continue
        nxt = days_sorted[i + 1]
        for c in up_by_day[day]:
            prev_up.append((nxt, c))
    money_effect: dict[str, float] = {}
    if prev_up:
        pu = pd.DataFrame(prev_up, columns=["trade_date", "ts_code"])
        m = pu.merge(pct, on=["trade_date", "ts_code"], how="inner")
        if not m.empty:
            agg = m.groupby("trade_date")["pct_chg"].mean()
            money_effect = {str(k): round(float(v), 3) for k, v in agg.items()}

    # ── 主力净额 ──
    mf = _load_moneyflow(d0, d1)
    main_net: dict[str, float | None] = {}
    if not mf.empty:
        mf = mf.copy()
        mf["trade_date"] = mf["trade_date"].astype(str).str.replace("-", "", regex=False)
        for _, r in mf.iterrows():
            v = r["main_net"]
            main_net[str(r["trade_date"])] = round(float(v), 1) if pd.notna(v) else None

    # ── 组装每日读数 ──
    days: dict[str, dict] = {}
    for i, r in base.iterrows():
        day = str(r["trade_date"])
        n_up, n_zha = int(r["n_up"]), int(r["n_zha"])
        denom = n_up + n_zha
        nxt = days_sorted[idx_of[day] + 1] if day in idx_of and idx_of[day] + 1 < len(days_sorted) else ""
        n_down = int(r["n_down"])
        days[day] = {
            "n_stocks": int(r["n_stocks"]),
            "n_up": n_up,
            "n_down": n_down,
            "n_zha": n_zha,
            "n_yizi": int(r["n_yizi"]),
            "seal_ratio": round(n_up / denom, 4) if denom else None,
            "zha_rate": round(n_zha / denom, 4) if denom else None,
            "n_lb2": int(n_lb2.get(day, 0)),
            "max_board": int(max_board.get(day, 0)),
            "money_effect": money_effect.get(day),
            "up_down_ratio": round(n_up / n_down, 3) if n_down else None,
            "main_net": main_net.get(day),
            # ★ 无未来函数：本行数据 T 日收盘后可知 → usable_from = 下一交易日
            "usable_from": nxt,
        }

    out = {
        "ok": True,
        "range": [d0, d1],
        "n_days": len(days),
        "days": days,
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": round(time.time() - t_start, 1),
            "price_basis": "pct_chg（不用 stk_limit：覆盖率仅 42.5%，且无法 JOIN；plans/24 §2.4-③）",
            "no_lookahead": "全部字段 T 日收盘后可知，仅可用于 T+1 及之后（见 usable_from）",
        },
    }
    if save:
        save_sentiment(out)
    logger.info(f"[sentiment] 构建完成 {d0}~{d1}：{len(days)} 个交易日，"
                f"耗时 {out['meta']['elapsed_sec']}s")
    return out


def save_sentiment(data: dict, path: str = SENTIMENT_FILE) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[sentiment] 落盘失败：{exc}")


def load_sentiment(path: str = SENTIMENT_FILE) -> dict | None:
    """加载已固化的情绪日线。无/损坏返回 None（不影响主流程）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def sentiment_asof(date: str, path: str = SENTIMENT_FILE) -> dict | None:
    """取"在 date **开盘前** 可合法使用"的最近一行情绪读数。

    ★ 防未来函数的核心入口。判定规则（两条同时满足）：

      1. `k < date`   —— 产生日必须**严格早于**决策日。
         这一条挡住"拿今天收盘的情绪做今天开盘的决策"（哪怕数据里已经有今天）。
      2. `usable_from` 为空 **或** `usable_from <= date`
         —— 有明确下一交易日时，必须已到那天；
            最新一行没有"下一交易日"（数据到此为止），视为**可用于其后任意日**。

    ⚠️ 为什么规则 2 要允许 `usable_from` 为空：
      实盘时数据只到昨天，昨天那行没有"下一交易日"可填。若因此跳过它，
      `sentiment_asof(今天)` 会返回**前天**的读数 → 实盘永远滞后一天（真实缺陷）。
      配合规则 1（严格 `<`），既不会未来函数，也能拿到最新一天的读数。
    """
    data = load_sentiment(path)
    if not data or not data.get("days"):
        return None
    d = str(date).replace("-", "")[:8]
    best_key = ""
    for k, v in data["days"].items():
        key = str(k)
        if not (key < d):                      # 规则 1：严格早于决策日
            continue
        uf = str(v.get("usable_from") or "")
        if uf and uf > d:                      # 规则 2：明确的下一交易日还没到
            continue
        if key > best_key:
            best_key = key
    if not best_key:
        return None
    row = dict(data["days"][best_key])
    row["_source_date"] = best_key             # 该读数的产生日（严格 < date）
    return row


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="构建市场情绪温度日线（plans/24 H1）")
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS)
    ap.add_argument("--start", default="", help="YYYYMMDD")
    ap.add_argument("--end", default="", help="YYYYMMDD")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    r = build_sentiment(years=args.years, d0=args.start, d1=args.end, save=not args.no_save)
    if not r.get("ok"):
        print(f"失败：{r.get('msg')}")
        return 1

    days = r["days"]
    keys = sorted(days)
    print("=" * 88)
    print(f"情绪温度日线：{r['range'][0]} ~ {r['range'][1]}，共 {len(keys)} 个交易日"
          f"（耗时 {r['meta']['elapsed_sec']}s）")
    print("=" * 88)
    hdr = (f"{'日期':<10}{'涨停':>6}{'炸板':>6}{'一字':>6}{'跌停':>6}"
           f"{'封板率':>9}{'连板≥2':>8}{'最高板':>8}{'赚钱效应':>10}{'主力净额(万)':>14}")
    print(hdr)
    print("-" * len(hdr))
    for k in keys[-10:]:
        v = days[k]
        me = v["money_effect"]
        mn = v["main_net"]
        print(f"{k:<10}{v['n_up']:>6}{v['n_zha']:>6}{v['n_yizi']:>6}{v['n_down']:>6}"
              f"{(v['seal_ratio'] if v['seal_ratio'] is not None else 0):>9.3f}"
              f"{v['n_lb2']:>8}{v['max_board']:>8}"
              f"{(me if me is not None else 0):>10.2f}"
              f"{(mn if mn is not None else 0):>14,.0f}")

    # 概览：情绪冷热分布（用封板率与赚钱效应看区间）
    zr = [v["zha_rate"] for v in days.values() if v["zha_rate"] is not None]
    me = [v["money_effect"] for v in days.values() if v["money_effect"] is not None]
    mb = [v["max_board"] for v in days.values()]
    if zr:
        print()
        print(f"区间概览：炸板率 中位 {np.median(zr):.3f} / P90 {np.percentile(zr, 90):.3f}"
              f" ｜ 赚钱效应 中位 {np.median(me):.2f}% ｜ 最高板 中位 {np.median(mb):.0f} 板")
    print()
    print("★ 无未来函数：以上任一行仅可用于其 usable_from（下一交易日）及之后")
    print(f"已落盘：{SENTIMENT_FILE}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
