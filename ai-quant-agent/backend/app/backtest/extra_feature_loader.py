"""现有全部数据增强特征加载（extra_feature_loader）

对齐规划：plans/04-回测引擎.md 4.7.0（统计范围覆盖全部已采集数据表）
  原则：**只利用现有数据库已采集的表，不采集缺失表**。

  从全部现有数据表按"启动点 t0"加载增强特征（共 25 个），供 feature_extractor 使用：
    daily_basic      → turnover_rate / pe_ttm / pb
    moneyflow        → net_amount_ratio / large_order_ratio / amount_ratio
    fina_indicator   → roe / gross_margin / profit_growth(netprofit_yoy) / ocf_ps
    stk_holdernumber → holder_change
    income           → revenue_growth（营收同比）
    balancesheet     → debt_ratio（资产负债率）
    fina_mainbz      → mainbz_concentration（主营集中度）
    top10_holders    → top10_ratio（前十大股东持股占比）
    pledge_stat      → pledge_ratio（股权质押比例）
    dividend         → event_dividend（近180日分红）
    repurchase       → event_repurchase（近180日回购）
    stk_rewards      → event_rewards（近180日股权激励）
    stk_holdertrade  → event_holdertrade（近90日股东增减持方向）
    forecast         → event_forecast（业绩预告方向）
    express          → event_express（业绩快报方向）
    block_trade      → event_block（近90日大宗折溢价率）
    weekly           → wk_trend（周线4周动量）
    monthly          → mo_trend（月线3月动量）
    index_daily      → market_env（上证指数前20日动量，模块级缓存）
    namechange       → is_ever_st（历史是否 ST）

  严格未来函数控制：只用 t0 之前（含当日）已发生/已披露的数据
    - 日频表（daily_basic/moneyflow/block_trade/weekly/monthly/index_daily）：trade_date <= t0
    - 披露表（fina_indicator/income/balancesheet/top10_holders/pledge_stat/
      dividend/repurchase/stk_rewards/stk_holdertrade/forecast/express/namechange）：ann_date <= t0
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.backtest.mirror_cache import MIRRORED, mirror_query
from app.models import SessionLocal
from app.core.logger import logger

DEFAULT_LOOKBACK = 20          # moneyflow 均值窗口（交易日）
INDEX_CODE = "000001.SH"       # 上证指数（大盘环境）
# 上证指数落盘缓存（index_daily 无 ts_code 索引，按 ts_code 全表扫极慢；落盘避免每次回测重扫）
MARKET_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "market_env_000001.SH.csv",
)

_MF_BUY_COLS = ["buy_sm_amount", "buy_md_amount", "buy_lg_amount", "buy_elg_amount"]
_MF_SELL_COLS = ["sell_sm_amount", "sell_md_amount", "sell_lg_amount", "sell_elg_amount"]

# 业绩预告类型 → 方向
_FORECAST_POS = {"预增", "略增", "续盈", "扭亏", "减亏", "预盈", "大增"}
_FORECAST_NEG = {"首亏", "续亏", "预减", "略减", "增亏", "大减"}

# 上证指数日线缓存（市场环境，模块级避免重复查询）
_INDEX_CACHE: dict = {"loaded": False, "dates": None, "close": None}


def _mirror_on() -> bool:
    """是否启用本地镜像缓存（plans/23 §4.5 W4；可进化参数 extra_mirror_enabled）。"""
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param("extra_mirror_enabled", True)
        return bool(v) if not isinstance(v, str) else v.strip().lower() not in ("0", "false", "no")
    except Exception:  # noqa: BLE001
        return True


def _query(ts_code: str, table: str, cols: list[str], order_col: str,
           limit: int | None = None) -> pd.DataFrame:
    """按 ts_code 查询一张表（升序）。失败返回空。

    W4 提速：大表（daily_basic/moneyflow）优先走**本地 SQLite 镜像**
    （实测 MySQL 冷读 1.8s/次 → 本地镜像 ~20ms；索引 988MB 装不进 buffer pool）。
    镜像不可用/未开启 → 自动回退 MySQL（行为与优化前完全一致）。
    """
    if _mirror_on() and table in MIRRORED:
        try:
            mdf = mirror_query(ts_code, table, cols, order_col, limit)
            if mdf is not None:
                return mdf
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"extra_feature_loader: 镜像读取 {table}({ts_code}) 失败，回退 MySQL: {exc}")
    db = SessionLocal()
    try:
        sql = f"SELECT {', '.join(cols)} FROM {table} WHERE ts_code='{ts_code}' ORDER BY {order_col}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        df = pd.read_sql(text(sql), db.connection())
        return df
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"extra_feature_loader: {table}({ts_code}) 加载失败: {exc}")
        return pd.DataFrame()
    finally:
        db.close()


def _to_float(v) -> float:
    try:
        if v is None or pd.isna(v):
            return np.nan
        return float(v)
    except Exception:  # noqa: BLE001
        return np.nan


def _parse_dates(series) -> np.ndarray:
    """将混合格式日期列统一为 datetime64[ns] 数组。

    兼容库中并存的两类日期：
      - '20250415'（字符串或数值）
      - '2025-04-15 17:04:37'（datetime 字符串）

    避免 pandas 2.x 推断单一格式失败抛异常（format='%Y%m%d' 遇不一致值），
    也避免纯数字 '20250415' 被 pandas 按纳秒时间戳误解析。
    """
    try:
        s = pd.Series(series).copy()
        if s.dtype.kind in "biuf":
            # 数值型：20250415.0 → '20250415'
            s = s.astype("Int64").astype(str).str.replace(r"\.0$", "", regex=True)
        else:
            s = s.astype(str).str.strip()
        # 纯数字 YYYYMMDD → YYYY-MM-DD（统一格式后再解析，避免纳秒误解析）
        s = s.str.replace(r"^(\d{4})(\d{2})(\d{2})$", r"\1-\2-\3", regex=True)
        # format='mixed'：逐个元素推断格式，兼容 '2025-04-15' 与 '2025-04-15 17:04:37' 并存
        return pd.to_datetime(s, format="mixed", errors="coerce").to_numpy(dtype="datetime64[ns]")
    except Exception:  # noqa: BLE001
        return pd.to_datetime(pd.Series(series), format="mixed", errors="coerce").to_numpy(dtype="datetime64[ns]")


def _last_row_before(df: pd.DataFrame, date_col: str, t0_ts) -> pd.Series | None:
    """取 df 中 date_col <= t0 的最后一行（df 需按 date_col 升序）。"""
    if df is None or df.empty:
        return None
    dts = _parse_dates(df[date_col])
    idx = int(np.searchsorted(dts, t0_ts, side="right")) - 1
    if idx < 0:
        return None
    return df.iloc[idx]


def _last_n_before(df: pd.DataFrame, date_col: str, t0_ts, n: int) -> pd.DataFrame:
    """取 df 中 date_col <= t0 的最后 n 行（df 需升序）。"""
    if df is None or df.empty:
        return pd.DataFrame()
    dts = _parse_dates(df[date_col])
    idx = int(np.searchsorted(dts, t0_ts, side="right"))  # 首个 > t0 的位置
    start = max(0, idx - n)
    return df.iloc[start:idx]


def _events_in_window(df: pd.DataFrame, date_col: str, t0_ts, window_days: int) -> pd.DataFrame:
    """取 df 中 t0-window_days <= date_col <= t0 的记录。"""
    if df is None or df.empty:
        return pd.DataFrame()
    dts = _parse_dates(df[date_col])
    lo = t0_ts - np.timedelta64(window_days, "D")
    idx = int(np.searchsorted(dts, lo, side="left"))
    hi = int(np.searchsorted(dts, t0_ts, side="right"))
    return df.iloc[idx:hi]


def _momentum_last_n(df: pd.DataFrame, date_col: str, close_col: str, t0_ts, n: int) -> float:
    """df 中 date_col <= t0 的最后 n 个 close 的动量（last/first - 1）。"""
    win = _last_n_before(df, date_col, t0_ts, n)
    if len(win) < 2:
        return np.nan
    closes = pd.to_numeric(win[close_col], errors="coerce").to_numpy()
    closes = closes[~np.isnan(closes)]
    if len(closes) < 2 or closes[0] == 0:
        return np.nan
    return float(closes[-1] / closes[0] - 1.0)


def _load_index_daily(force_refresh: bool = False) -> None:
    """加载上证指数日线（供 market_env）。优先读落盘缓存；缺失时查库并写盘（首次约 40s，仅一次）。"""
    if _INDEX_CACHE["loaded"] and not force_refresh:
        return
    df = pd.DataFrame()
    if not force_refresh and os.path.exists(MARKET_CACHE_FILE):
        try:
            df = pd.read_csv(MARKET_CACHE_FILE, parse_dates=["trade_date"])
            logger.info(f"上证指数使用落盘缓存: {MARKET_CACHE_FILE}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"读取上证指数缓存失败: {exc}")
            df = pd.DataFrame()
    if df.empty:
        db = SessionLocal()
        try:
            df = pd.read_sql(
                text(f"SELECT trade_date, close FROM index_daily WHERE ts_code='{INDEX_CODE}' ORDER BY trade_date"),
                db.connection(),
            )
            if not df.empty:
                try:
                    df.to_csv(MARKET_CACHE_FILE, index=False)
                    logger.info(f"上证指数首次加载并写缓存: {len(df)} 条")
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"写上证指数缓存失败: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"加载指数 {INDEX_CODE} 失败: {exc}")
        finally:
            db.close()
    if not df.empty:
        _INDEX_CACHE["dates"] = _parse_dates(df["trade_date"])
        _INDEX_CACHE["close"] = pd.to_numeric(df["close"], errors="coerce").to_numpy()
    _INDEX_CACHE["loaded"] = True


# 月线缓存（monthly 无索引，进程级全量读入一次，避免逐股无索引全表扫）
_MONTHLY_CACHE: dict = {"loaded": False, "data": {}}


def _get_monthly(ts_code: str) -> pd.DataFrame:
    """按 ts_code 返回月线 DataFrame（进程级全量缓存）。"""
    if not _MONTHLY_CACHE["loaded"]:
        db = SessionLocal()
        try:
            raw = pd.read_sql(
                text("SELECT ts_code, trade_date, close FROM monthly ORDER BY ts_code, trade_date"),
                db.connection(),
            )
            data: dict[str, pd.DataFrame] = {}
            for code, g in raw.groupby("ts_code"):
                gg = g[["trade_date", "close"]].copy()
                gg["trade_date"] = _parse_dates(gg["trade_date"])
                data[str(code)] = gg.reset_index(drop=True)
            _MONTHLY_CACHE["data"] = data
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"加载 monthly 全量失败: {exc}")
        finally:
            db.close()
            _MONTHLY_CACHE["loaded"] = True
    return _MONTHLY_CACHE["data"].get(str(ts_code), pd.DataFrame())


def _market_env(t0_ts, lookback: int = 20) -> float:
    """上证指数 t0 前 lookback 个交易日动量。"""
    _load_index_daily()
    if _INDEX_CACHE["dates"] is None:
        return np.nan
    dts = _INDEX_CACHE["dates"]
    closes = _INDEX_CACHE["close"]
    hi = int(np.searchsorted(dts, t0_ts, side="right"))
    start = max(0, hi - lookback)
    if hi - start < 2:
        return np.nan
    seg = closes[start:hi]
    seg = seg[~np.isnan(seg)]
    if len(seg) < 2 or seg[0] == 0:
        return np.nan
    return float(seg[-1] / seg[0] - 1.0)


def build_extra_map(ts_code: str, df: pd.DataFrame, t0_idxs: list[int] | None = None,
                    lookback: int = DEFAULT_LOOKBACK) -> dict[int, dict]:
    """为一只股票构建 {t0_idx: {feature: value}} 映射（只用于传入 t0_idxs 的点）。

    Args:
        ts_code: 股票代码
        df: prepare_stock_data 输出（用于取 t0 日期与 block_trade 折价对比）
        t0_idxs: 需要提取特征的启动点下标列表（None = 全部交易日，较慢）
        lookback: moneyflow 均值窗口

    Returns:
        {t0_idx: {feature: value, ...}}（25 个增强特征）
    """
    if df is None or df.empty:
        return {}
    if not t0_idxs:
        t0_idxs = list(range(len(df)))

    dates = _parse_dates(df["trade_date"])
    df_close = pd.to_numeric(df["close"], errors="coerce").to_numpy() if "close" in df.columns else None

    # ── 一次性加载该股各现有表（升序）──
    # 用户补充：daily_basic 增加 circ_mv（流通市值，人均持股金额=市值/股东数）
    dbb = _query(ts_code, "daily_basic",
                 ["trade_date", "turnover_rate", "pe_ttm", "pb", "circ_mv"], "trade_date")
    mf = _query(ts_code, "moneyflow",
                ["trade_date"] + _MF_BUY_COLS + _MF_SELL_COLS + ["net_mf_amount"], "trade_date")
    # 用户补充：fina_indicator 增加 bps（每股净资产）+ eps（每股收益，供"即将ST"预判）
    # Bug 修复：fina/holders/income/balance/top10 消费方均按 ann_date 过滤（_last_row_before 等
    # 依赖 searchsorted 有序），必须 ORDER BY ann_date；否则跨报告期 ann_date 与 end_date 顺序
    # 不一致会取错"最新一期"财务数据（含未来函数）。
    fina = _query(ts_code, "fina_indicator",
                  ["ann_date", "end_date", "roe", "grossprofit_margin", "netprofit_yoy", "ocfps",
                   "bps", "eps"], "ann_date")
    holders = _query(ts_code, "stk_holdernumber", ["ann_date", "end_date", "holder_num"], "ann_date")
    income = _query(ts_code, "income", ["ann_date", "end_date", "revenue"], "ann_date")
    balance = _query(ts_code, "balancesheet",
                     ["ann_date", "end_date", "total_assets", "total_liab"], "ann_date")
    mainbz = _query(ts_code, "fina_mainbz", ["end_date", "bz_item", "bz_sales"], "end_date")
    top10 = _query(ts_code, "top10_holders", ["ann_date", "end_date", "hold_ratio"], "ann_date")
    pledge = _query(ts_code, "pledge_stat", ["end_date", "pledge_ratio"], "end_date")
    dividend = _query(ts_code, "dividend", ["ann_date", "ex_date"], "ann_date")
    repurchase = _query(ts_code, "repurchase", ["ann_date"], "ann_date")
    rewards = _query(ts_code, "stk_rewards", ["ann_date"], "ann_date")
    holdertrade = _query(ts_code, "stk_holdertrade", ["ann_date", "in_de", "change_ratio"], "ann_date")
    forecast = _query(ts_code, "forecast", ["ann_date", "type"], "ann_date")
    express = _query(ts_code, "express", ["ann_date", "yoy_net_profit"], "ann_date")
    block = _query(ts_code, "block_trade", ["trade_date", "price"], "trade_date")
    weekly = _query(ts_code, "weekly", ["trade_date", "close"], "trade_date")
    monthly = _get_monthly(ts_code)
    names = _query(ts_code, "namechange", ["name", "start_date"], "start_date")

    # 历史是否 ST
    ever_st = 1.0 if not names.empty and any("ST" in str(n).upper() for n in names.get("name", [])) else 0.0

    # 预处理 moneyflow
    mf_clean = pd.DataFrame()
    if not mf.empty:
        buy = pd.to_numeric(mf[_MF_BUY_COLS].sum(axis=1), errors="coerce")
        sell = pd.to_numeric(mf[_MF_SELL_COLS].sum(axis=1), errors="coerce")
        net_mf = pd.to_numeric(mf["net_mf_amount"], errors="coerce")
        elg_net = (pd.to_numeric(mf["buy_elg_amount"], errors="coerce")
                   - pd.to_numeric(mf["sell_elg_amount"], errors="coerce"))
        tot = buy + sell
        tot_safe = np.where(tot == 0, np.nan, tot)
        mf_clean = mf[["trade_date"]].copy()
        mf_clean["trade_date"] = _parse_dates(mf_clean["trade_date"])
        mf_clean["net_amount_ratio"] = net_mf / tot_safe
        mf_clean["large_order_ratio"] = elg_net / tot_safe
        mf_clean["total_amount"] = tot
        mf_clean = mf_clean.sort_values("trade_date").reset_index(drop=True)

    result: dict[int, dict] = {}
    for idx in t0_idxs:
        if idx < 0 or idx >= len(dates):
            continue
        t0_ts = dates[idx]
        extra: dict[str, float] = {}

        # ── daily_basic ──
        row = _last_row_before(dbb, "trade_date", t0_ts)
        extra["turnover_rate"] = _to_float(row.get("turnover_rate")) if row is not None else np.nan
        extra["pe_percentile"] = _to_float(row.get("pe_ttm")) if row is not None else np.nan
        extra["pb_percentile"] = _to_float(row.get("pb")) if row is not None else np.nan
        # 用户补充：流通市值（万元），供人均持股金额计算
        circ_mv = _to_float(row.get("circ_mv")) if row is not None else np.nan

        # ── moneyflow ──
        if not mf_clean.empty:
            win = _last_n_before(mf_clean, "trade_date", t0_ts, lookback)
            if not win.empty:
                extra["net_amount_ratio"] = float(np.nanmean(win["net_amount_ratio"])) \
                    if win["net_amount_ratio"].notna().any() else np.nan
                extra["large_order_ratio"] = float(np.nanmean(win["large_order_ratio"])) \
                    if win["large_order_ratio"].notna().any() else np.nan
                last_sum = win["total_amount"].iloc[-1]
                avg_sum = float(np.nanmean(win["total_amount"])) if win["total_amount"].notna().any() else np.nan
                extra["amount_ratio"] = float(last_sum) / avg_sum if avg_sum and avg_sum == avg_sum else np.nan
            else:
                extra["net_amount_ratio"] = extra["large_order_ratio"] = extra["amount_ratio"] = np.nan
        else:
            extra["net_amount_ratio"] = extra["large_order_ratio"] = extra["amount_ratio"] = np.nan

        # ── fina_indicator（ann_date <= t0 最近一期）──
        frow = _last_row_before(fina, "ann_date", t0_ts)
        if frow is not None:
            extra["roe"] = _to_float(frow.get("roe"))
            extra["gross_margin"] = _to_float(frow.get("grossprofit_margin"))
            extra["profit_growth"] = _to_float(frow.get("netprofit_yoy"))
            extra["ocf_ps"] = _to_float(frow.get("ocfps"))
            extra["bps"] = _to_float(frow.get("bps"))   # 用户补充：每股净资产
            extra["eps"] = _to_float(frow.get("eps"))   # 每股收益（ST 预判用）
        else:
            extra["roe"] = extra["gross_margin"] = extra["profit_growth"] = extra["ocf_ps"] = np.nan
            extra["bps"] = np.nan
            extra["eps"] = np.nan
        # 用户补充：即将触发 ST/*ST 预判（回测样本与每日推荐均应排除）
        #   A股 *ST 触发条件（可用数据近似）：
        #     - 最近一期每股净资产 bps < 0（净资产为负 → *ST 硬触发）
        #     - 最近两期每股收益 eps 连续为负（连续亏损 → ST 风险）
        #   历史已 ST（is_ever_st）由 risk_filter.is_st 另行排除。
        frows = _last_n_before(fina, "ann_date", t0_ts, 2)
        eps_vals = [_to_float(r.get("eps")) for r in frows.to_dict("records")] if not frows.empty else []
        eps_vals = [v for v in eps_vals if v == v]
        st_risk = 0.0
        bpsv = _to_float(extra.get("bps")) if extra.get("bps") is not None else np.nan
        if bpsv == bpsv and bpsv < 0:
            st_risk = 1.0                      # 净资产为负（*ST 硬触发）
        elif len(eps_vals) >= 2 and all(v < 0 for v in eps_vals[-2:]):
            st_risk = 1.0                      # 连续两期亏损（ST 风险）
        extra["st_risk_flag"] = st_risk

        # ── stk_holdernumber（股东人数变化 + 人均持股金额 + 筹码集中度）──
        h_rows = _last_n_before(holders, "ann_date", t0_ts, 2)
        if len(h_rows) >= 2:
            cur = _to_float(h_rows["holder_num"].iloc[-1])
            prev = _to_float(h_rows["holder_num"].iloc[-2])
            extra["holder_change"] = (-(cur - prev) / prev) if (cur == cur and prev == prev and prev != 0) else np.nan
        else:
            extra["holder_change"] = np.nan
        # 用户补充：人均持股金额（流通市值/股东人数，万元/人）+ 筹码集中度（股东数倒数）
        h_last = _last_row_before(holders, "ann_date", t0_ts)
        holder_num = _to_float(h_last.get("holder_num")) if h_last is not None else np.nan
        if holder_num == holder_num and holder_num > 0 and circ_mv == circ_mv and circ_mv > 0:
            extra["avg_hold_amount"] = float(circ_mv / holder_num)
            extra["holder_concentration"] = float(1.0 / holder_num)
        else:
            extra["avg_hold_amount"] = np.nan
            extra["holder_concentration"] = np.nan

        # ── income：营收同比（最近两期）──
        inc_rows = _last_n_before(income, "ann_date", t0_ts, 2)
        if len(inc_rows) >= 2:
            cur = _to_float(inc_rows["revenue"].iloc[-1])
            prev = _to_float(inc_rows["revenue"].iloc[-2])
            extra["revenue_growth"] = ((cur - prev) / abs(prev)) if (cur == cur and prev == prev and prev != 0) else np.nan
        else:
            extra["revenue_growth"] = np.nan

        # ── balancesheet：资产负债率 ──
        brow = _last_row_before(balance, "ann_date", t0_ts)
        if brow is not None:
            ta = _to_float(brow.get("total_assets"))
            tl = _to_float(brow.get("total_liab"))
            extra["debt_ratio"] = (tl / ta) if (ta == ta and tl == tl and ta != 0) else np.nan
        else:
            extra["debt_ratio"] = np.nan

        # ── fina_mainbz：主营集中度（最近期第一大占比）──
        mb_rows = _last_n_before(mainbz, "end_date", t0_ts, 1) if not mainbz.empty else pd.DataFrame()
        if not mb_rows.empty:
            sales = pd.to_numeric(mb_rows["bz_sales"], errors="coerce")
            sales = sales[~sales.isna()]
            if len(sales) and sales.sum() != 0:
                extra["mainbz_concentration"] = float(sales.max() / sales.sum())
            else:
                extra["mainbz_concentration"] = np.nan
        else:
            extra["mainbz_concentration"] = np.nan

        # ── top10_holders：前十大股东持股占比 ──
        trow = _last_row_before(top10, "ann_date", t0_ts)
        if trow is not None:
            # 用最近一期全部股东之和（按 end_date 分组）
            # 关键：end_date 可能混合格式（'20250331' vs '2025-03-31 17:04:37'）且可能带时间分量，
            # 必须统一解析并截断到"日期"后再比较，否则同报告期匹配失败导致 top10_ratio 失真。
            end = trow.get("end_date")
            if end is not None:
                ends = _parse_dates(top10["end_date"]).astype("datetime64[D]")
                target = _parse_dates(pd.Series([end]))[0].astype("datetime64[D]")
                if not pd.isna(target):
                    same = top10[ends == target]
                    ratios = pd.to_numeric(same["hold_ratio"], errors="coerce")
                    extra["top10_ratio"] = float(ratios.sum()) if ratios.notna().any() else np.nan
                else:
                    extra["top10_ratio"] = np.nan
            else:
                extra["top10_ratio"] = np.nan
        else:
            extra["top10_ratio"] = np.nan

        # ── pledge_stat：股权质押比例 ──
        prow = _last_row_before(pledge, "end_date", t0_ts)
        extra["pledge_ratio"] = _to_float(prow.get("pledge_ratio")) if prow is not None else np.nan

        # ── 事件类（近 window 日）──
        extra["event_dividend"] = 1.0 if not _events_in_window(dividend, "ann_date", t0_ts, 180).empty else 0.0
        extra["event_repurchase"] = 1.0 if not _events_in_window(repurchase, "ann_date", t0_ts, 180).empty else 0.0
        extra["event_rewards"] = 1.0 if not _events_in_window(rewards, "ann_date", t0_ts, 180).empty else 0.0
        ht = _events_in_window(holdertrade, "ann_date", t0_ts, 90)
        if not ht.empty:
            signs = np.where(ht["in_de"].astype(str).str.upper().str.contains("IN"), 1.0, -1.0)
            cr = pd.to_numeric(ht["change_ratio"], errors="coerce").to_numpy()
            cr = np.nan_to_num(cr)
            net_dir = float(np.sum(signs * cr))
            extra["event_holdertrade"] = 1.0 if net_dir > 1e-6 else (-1.0 if net_dir < -1e-6 else 0.0)
        else:
            extra["event_holdertrade"] = 0.0
        frow_f = _last_row_before(forecast, "ann_date", t0_ts)
        if frow_f is not None:
            ft = str(frow_f.get("type") or "")
            extra["event_forecast"] = 1.0 if ft in _FORECAST_POS else (-1.0 if ft in _FORECAST_NEG else 0.0)
        else:
            extra["event_forecast"] = 0.0
        erow = _last_row_before(express, "ann_date", t0_ts)
        if erow is not None:
            yoy = _to_float(erow.get("yoy_net_profit"))
            extra["event_express"] = 1.0 if yoy > 0 else (-1.0 if yoy < 0 else 0.0) if yoy == yoy else 0.0
        else:
            extra["event_express"] = 0.0

        # ── block_trade：近90日大宗折溢价率 ──
        bwin = _events_in_window(block, "trade_date", t0_ts, 90)
        if not bwin.empty and df_close is not None:
            premiums = []
            for _, brow_b in bwin.iterrows():
                bdate = _parse_dates(pd.Series([brow_b["trade_date"]]))[0]
                j = int(np.searchsorted(dates, bdate, side="left"))
                if 0 <= j < len(dates) and dates[j] == bdate:
                    c = df_close[j]
                    p = _to_float(brow_b.get("price"))
                    if p == p and c == c and c != 0:
                        premiums.append(p / c - 1.0)
            extra["event_block"] = float(np.mean(premiums)) if premiums else np.nan
        else:
            extra["event_block"] = np.nan

        # ── 中期趋势 ──
        extra["wk_trend"] = _momentum_last_n(weekly, "trade_date", "close", t0_ts, 5)
        extra["mo_trend"] = _momentum_last_n(monthly, "trade_date", "close", t0_ts, 4)

        # ── 大盘环境 ──
        extra["market_env"] = _market_env(t0_ts)

        # ── 历史 ST ──
        extra["is_ever_st"] = ever_st

        result[idx] = extra
    return result
