"""失败案例外部证据包（failure_context）

用户想法（原话）：
    "进化大脑在分析每日推荐为啥会失败的时候，其中一个方面要从以往的大盘或者个股日线
     以及个股资料中找原因，就像回测一样，找出原因"

## 为什么必须做（现状的漏洞）

改造前，失败归因喂给 LLM 的只有两样东西：**推荐依据（形态/相似度/条件分/规则）** +
**T+1 涨幅**。LLM 因此看不到：

  - 同期**大盘**是涨是跌 → 个股没涨可能只是大盘系统性下跌，属"外因"；
  - 个股**推荐后的真实走势** → 是"跌停放量出货"还是"缩量横盘洗盘"？性质完全不同；
  - 期间有没有**利空事件** → 业绩预减/股东减持/高质押/大宗折价。

看不到这些的后果很具体：把**外因**误判成**内因**，进化大脑据此去改形态识别、
相似度阈值、条件概率表 → 越改越偏（这是"回测好看、实盘失效"的另一种翻版）。

## 本模块做什么

只做**证据收集**（三类，全部来自已采集的表，只读、逐块可降级）：

  ① market  大盘：上证/深证/创业板 在 [D, D+N] 的真实涨跌 + D 前 20 日市场环境
  ② stock   个股日线：[D+1, D+N] 涨跌/最大回撤/跌停天数/放量下跌/跌破MA20/跳空低开/停牌缺口
  ③ profile 个股资料：业绩预告·快报方向、持股变动、质押、分红/回购/激励、大宗折溢价、股东户数

判断"外因 / 内因"仍由 LLM 做，但本模块给出**结构化判据与先验**（cause_prior），
并要求 LLM 在证据不足时明确说"证据不足"，**不许用推测填坑**。

## ⚠️ 未来函数护栏（重要）

本模块**故意使用推荐日之后的数据**——这是事后复盘的用途，不是预测。
因此它**只能**被归因/进化决策侧调用（daily_verify / reflect_agent），
**绝不能**被每日推荐或特征/打分侧调用（daily_scan / feature_extractor /
extra_feature_loader / recommender / risk_filter / vector_store）。
`scripts/verify_failure_evidence.py` 有一条断言专门守这个边界。

## 降级原则

任何一块拿不到（表缺失/无权限/停牌/代码不存在）→ 记为 `missing`，
面板返回空 dict，**绝不抛异常**打断归因主流程。
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

# ── 配置 ──────────────────────────────────────────────────────
DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
INDEX_PANEL_FILE = os.path.join(DATA_DIR, "index_panel.json")
INDEX_PANEL_TTL = 12 * 3600          # 指数面板缓存有效期（秒）

# 参与"大盘"判定的指数（首选第一个作为基准 bench）
INDEX_CODES: tuple[tuple[str, str], ...] = (
    ("000001.SH", "上证综指"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
)
BENCH_CODE = "000001.SH"

DEFAULT_DAYS = 5                     # 复盘窗口（交易日）
DEFAULT_LOOKBACK = 20                # 推荐日前的市场/量能参照窗口
EVENT_DAYS = 120                     # 个股资料事件回看天数
HOLDER_DAYS = 90                     # 股东增减持/大宗交易回看天数

# 判据阈值（写死在此处而非散落在 prompt 里，便于复现与审计）
MKT_DOWN = -0.02                     # 大盘同期 ≤ -2% → 系统性下跌
REL_MATCH = 0.03                     # 个股与大盘差距 ≤ 3pct → 跟跌（外因主导）
REL_UNDERPERF = -0.05                # 跑输大盘 ≥ 5pct → 更像选股/择时问题（内因）
VOL_SURGE = 1.5                      # 放量倍数（相对前 20 日均量）
GAP_DOWN = -0.02                     # 跳空低开阈值
PLEDGE_HIGH = 0.30                   # 高质押阈值
SEALED_DOWN_MIN = 1                  # 跌停 ≥1 天即标记

_NEG_FORECAST = {"首亏", "续亏", "预减", "略减", "增亏", "大减"}
_POS_FORECAST = {"预增", "略增", "续盈", "扭亏", "减亏", "预盈", "大增"}

_INDEX_PANEL: dict = {"loaded": False, "series": {}}


# ── 可进化参数 ────────────────────────────────────────────────
def evidence_enabled() -> bool:
    """是否给失败归因附带外部证据（可进化参数 verify_evidence_enabled，默认开）。"""
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param("verify_evidence_enabled", True)
        return bool(v) if not isinstance(v, str) else v.strip().lower() not in ("0", "false", "no")
    except Exception:  # noqa: BLE001
        return True


def evidence_days() -> int:
    """复盘窗口（交易日，可进化参数 verify_evidence_days，默认 5）。"""
    try:
        from app.agents import evolution_config
        raw = evolution_config.get_param("verify_evidence_days", DEFAULT_DAYS)
        return max(1, min(20, int(float(raw if raw is not None else DEFAULT_DAYS))))
    except Exception:  # noqa: BLE001
        return DEFAULT_DAYS


# ── 工具 ──────────────────────────────────────────────────────
def _norm_date(v) -> str:
    """任意日期表示 → 'YYYYMMDD'（失败返回 ''）。"""
    try:
        s = str(v).strip()
        if not s or s.lower() == "nan":
            return ""
        s = s.split(" ")[0].replace("-", "").replace("/", "")
        return s if len(s) == 8 and s.isdigit() else ""
    except Exception:  # noqa: BLE001
        return ""


def _f(v):
    """安全转 float（失败 NaN）。"""
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return np.nan
        return float(v)
    except Exception:  # noqa: BLE001
        return np.nan


def _pct(v) -> str:
    v = _f(v)
    return "—" if v != v else f"{v * 100:+.2f}%"


def _count_trade_days(d0: str, d1: str) -> int:
    """(d0, d1] 的交易日个数（trade_cal）。失败返回 0。"""
    if not d0 or not d1 or d1 <= d0:
        return 0
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT COUNT(*) FROM trade_cal WHERE is_open=1 AND cal_date>:a AND cal_date<=:b"),
            {"a": d0, "b": d1}).scalar()
        return int(row or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[failure_context] 交易日计数失败 {d0}~{d1}: {exc}")
        return 0
    finally:
        db.close()


# ── ① 大盘面板 ────────────────────────────────────────────────
def _load_index_panel(force: bool = False) -> dict[str, list]:
    """加载 3 个指数的日线（缓存到 data/index_panel.json，避免 index_daily 无索引全表扫）。

    返回 {code: [[date, close], ...]}（按日期升序）。失败返回 {}。
    """
    if _INDEX_PANEL["loaded"] and not force:
        return _INDEX_PANEL["series"]
    cached = None
    if not force and os.path.exists(INDEX_PANEL_FILE):
        try:
            with open(INDEX_PANEL_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[failure_context] 指数面板缓存读取失败: {exc}")
    old_series = cached.get("series") if isinstance(cached, dict) else None
    built_at = (float(cached.get("built_at") or 0) if isinstance(cached, dict) else 0.0)
    if old_series and (time.time() - built_at) < INDEX_PANEL_TTL:
        _INDEX_PANEL["series"] = old_series
        _INDEX_PANEL["loaded"] = True
        return _INDEX_PANEL["series"]

    codes = ", ".join(f"'{c}'" for c, _ in INDEX_CODES)
    db = SessionLocal()
    series: dict[str, list] = {}
    try:
        df = pd.read_sql(
            text(f"SELECT ts_code, trade_date, close FROM index_daily WHERE ts_code IN ({codes})"),
            db.connection())
        for code, g in df.groupby("ts_code"):
            g = g.copy()
            g["d"] = g["trade_date"].map(_norm_date)
            g["c"] = pd.to_numeric(g["close"], errors="coerce")
            g = g[(g["d"] != "") & g["c"].notna()].sort_values("d")
            series[str(code)] = [[d, float(c)] for d, c in zip(g["d"], g["c"])]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[failure_context] index_daily 加载失败: {exc}")
    finally:
        db.close()

    if series:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(INDEX_PANEL_FILE, "w", encoding="utf-8") as f:
                json.dump({"built_at": time.time(), "series": series}, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[failure_context] 指数面板缓存写入失败: {exc}")
    elif old_series:
        # 重建失败（库不可达/超时）→ 宁可用过期缓存，也不让"大盘证据"整块消失
        logger.warning("[failure_context] index_daily 重建为空，回退过期缓存（结果可能略旧）")
        series = old_series
    if series:
        logger.info(f"[failure_context] 指数面板就绪（{len(series)} 个指数）")
    _INDEX_PANEL["series"] = series
    _INDEX_PANEL["loaded"] = True
    return series


def _window_ret(rows: list, d0: str, days: int) -> dict | None:
    """从 [(date, close)] 取 [d0, d0+days] 的区间涨跌。

    与 daily_verify 的"D+1 买入口径"保持一致：基准 = d0 **之后的第 1 个**交易日收盘，
    终点 = 再往后 days 个交易日收盘。这样"大盘涨跌"与"个股收益"是同一口径，可相减。
    """
    if not rows or not d0:
        return None
    ds = [r[0] for r in rows]
    lo = 0
    hi = len(ds)
    while lo < hi:                       # 二分：首个 >= d0 的位置
        mid = (lo + hi) // 2
        if ds[mid] < d0:
            lo = mid + 1
        else:
            hi = mid
    buy_i = lo + 1
    if buy_i >= len(rows):
        return None
    # 自适应窗口：能走多少算多少（T+1 当日验证时窗口必然没走完，
    # 若强行要求满 days 天，则"大盘证据"在最需要它的次日快反馈里永远缺席）
    end_i = min(buy_i + days, len(rows) - 1)
    if end_i <= buy_i:
        return None
    base = float(rows[buy_i][1])
    end = float(rows[end_i][1])
    if base <= 0:
        return None
    return {
        "ret": end / base - 1.0,
        "actual_days": end_i - buy_i,
        "complete": end_i >= buy_i + days,
        "start_date": rows[buy_i][0],
        "end_date": rows[end_i][0],
    }


def market_panel(rec_date: str, days: int | None = None, lookback: int = DEFAULT_LOOKBACK) -> dict:
    """大盘/指数面板：[D, D+N] 各指数涨跌 + D 前 lookback 日环境 + 是否系统性下跌。"""
    days = days or evidence_days()
    rec_date = _norm_date(rec_date)
    out: dict = {"indices": {}, "bench_code": BENCH_CODE, "missing": []}
    if not rec_date:
        out["missing"].append("rec_date 非法")
        return out
    series = _load_index_panel()
    if not series:
        out["missing"].append("index_daily 无数据（大盘证据缺失）")
        return out
    for code, name in INDEX_CODES:
        rows = series.get(code) or []
        w = _window_ret(rows, rec_date, days)
        pre = None
        # 推荐日前的市场环境（前 lookback 个交易日动量）
        try:
            ds = [r[0] for r in rows]
            k = 0
            for k, d in enumerate(ds):
                if d >= rec_date:
                    break
            start = max(0, k - lookback)
            if k - start >= 2:
                pre = float(rows[k - 1][1]) / float(rows[start][1]) - 1.0
        except Exception:  # noqa: BLE001
            pre = None
        if w is None:
            out["missing"].append(f"{name}({code}) 窗口数据不足")
            continue
        out["indices"][code] = {
            "name": name,
            "ret": round(w["ret"], 4),
            "pre_lookback_ret": None if pre is None or pre != pre else round(pre, 4),
            "actual_days": w["actual_days"],
            "complete": w["complete"],
            "start_date": w["start_date"],
            "end_date": w["end_date"],
        }
    bench = out["indices"].get(BENCH_CODE)
    out["bench_ret"] = bench.get("ret") if bench else None
    out["bench_pre_ret"] = bench.get("pre_lookback_ret") if bench else None
    out["market_down"] = bool(bench and bench["ret"] is not None and bench["ret"] <= MKT_DOWN)
    out["window_complete"] = bool(bench and bench.get("complete"))
    out["window"] = f"{days}个交易日（实际已走完 {bench.get('actual_days') if bench else '?'} 天）"
    return out


# ── ② 个股日线面板 ────────────────────────────────────────────
def stock_panel(ts_code: str, rec_date: str, days: int | None = None, df=None) -> dict:
    """个股 [D+1, D+N] 的真实走势（涨跌/回撤/跌停/放量下跌/破MA20/跳空低开/停牌缺口）。"""
    days = days or evidence_days()
    rec_date = _norm_date(rec_date)
    out: dict = {"ts_code": ts_code, "days": days, "missing": [], "flags": []}
    if not rec_date:
        out["missing"].append("rec_date 非法")
        return out
    if df is None:
        try:
            from app.backtest.loader import prepare_stock_data
            df = prepare_stock_data(ts_code)
        except Exception as exc:  # noqa: BLE001
            out["missing"].append(f"个股日线加载失败: {exc}")
            return out
    if df is None or len(df) == 0:
        out["missing"].append("个股日线为空（停牌/退市/未采集）")
        return out

    try:
        dates = df["trade_date"].dt.strftime("%Y%m%d").tolist()
        i0 = None
        for i, d in enumerate(dates):
            if d >= rec_date:
                i0 = i
                break
        if i0 is None:
            out["missing"].append(f"推荐日 {rec_date} 之后无日线（未采集/停牌）")
            return out
        buy_i = i0 + 1
        if buy_i >= len(df):
            out["missing"].append("推荐日之后尚无交易日（窗口未到）")
            return out
        end_i = min(len(df) - 1, buy_i + days)

        adj = pd.to_numeric(df["adj_close"], errors="coerce").to_numpy()
        pct = (pd.to_numeric(df["pct_chg"], errors="coerce").to_numpy()
               if "pct_chg" in df.columns else np.full(len(df), np.nan))
        vol = (pd.to_numeric(df["vol"], errors="coerce").to_numpy()
               if "vol" in df.columns else np.full(len(df), np.nan))
        sealed_dn = (pd.to_numeric(df["is_sealed_down"], errors="coerce").fillna(0).to_numpy()
                     if "is_sealed_down" in df.columns else np.zeros(len(df)))

        base = float(adj[buy_i])
        if not base or base != base or base <= 0:
            out["missing"].append("买入日收盘价非法")
            return out

        if end_i <= buy_i:
            out["missing"].append("推荐后尚无完整交易日（复盘窗口未走完）")
            return out

        win = range(buy_i, end_i + 1)
        out["buy_date"] = dates[buy_i]
        out["end_date"] = dates[end_i]
        out["window_days_actual"] = end_i - buy_i
        out["window_complete"] = end_i >= buy_i + days
        out["t1_ret"] = round(float(adj[buy_i + 1]) / base - 1.0, 4)
        out["tN_ret"] = round(float(adj[end_i]) / base - 1.0, 4)
        # 含买入日（基准 0%）的累计收益序列 → 峰值/最深浮亏/真实回撤
        seq = [float(adj[j]) for j in win if adj[j] == adj[j]]
        if len(seq) >= 2:
            rets = [v / base - 1.0 for v in seq]
            out["max_gain"] = round(max(rets), 4)
            out["max_loss"] = round(min(rets), 4)
            pk = seq[0]
            mdd = 0.0
            for v in seq:
                pk = max(pk, v)
                if pk > 0:
                    mdd = min(mdd, v / pk - 1.0)
            out["max_drawdown"] = round(mdd, 4)
            # 冲高回落：终点距窗口最高点（判断"给过机会又还回去"）
            ymax = max(seq)
            out["give_back"] = round(float(adj[end_i]) / ymax - 1.0, 4) if ymax > 0 else None

        # 跌停 / 跌停附近
        sealed = int(sum(1 for j in win if sealed_dn[j] >= 1))
        limit_dn = int(sum(1 for j in win if pct[j] == pct[j] and float(pct[j]) <= -9.5))
        out["sealed_down_days"] = sealed or limit_dn
        if out["sealed_down_days"] >= SEALED_DOWN_MIN:
            out["flags"].append(f"窗口内跌停/近跌停 {out['sealed_down_days']} 天")

        # 放量下跌：vol > 1.5× 推荐日前 20 日均量 且当日下跌
        if buy_i >= 20:
            vbase = float(np.nanmean(vol[buy_i - 20:buy_i])) if np.isfinite(vol[buy_i - 20:buy_i]).any() else np.nan
            if vbase == vbase and vbase > 0:
                surge_dn = int(sum(1 for j in win
                                   if vol[j] == vol[j] and float(vol[j]) > VOL_SURGE * vbase
                                   and pct[j] == pct[j] and float(pct[j]) < 0))
                out["vol_surge_down_days"] = surge_dn
                if surge_dn >= 1:
                    out["flags"].append(f"放量下跌 {surge_dn} 天（量能>{VOL_SURGE}×均量）")

        # 破 MA20：买入日在上方、窗口内跌破
        if buy_i >= 19:
            ma20 = pd.Series(adj).rolling(20).mean().to_numpy()
            above_at_buy = adj[buy_i] == adj[buy_i] and ma20[buy_i] == ma20[buy_i] and adj[buy_i] > ma20[buy_i]
            broke = any(adj[j] == adj[j] and ma20[j] == ma20[j] and adj[j] < ma20[j] for j in win)
            out["ma20_above_at_buy"] = bool(above_at_buy)
            out["broke_ma20"] = bool(above_at_buy and broke)
            if out["broke_ma20"]:
                out["flags"].append("跌破 MA20（多头结构破坏）")

        # 跳空低开
        if "open" in df.columns and "pre_close" in df.columns:
            op = pd.to_numeric(df["open"], errors="coerce").to_numpy()
            pc = pd.to_numeric(df["pre_close"], errors="coerce").to_numpy()
            gaps = int(sum(1 for j in win
                           if op[j] == op[j] and pc[j] == pc[j] and pc[j] > 0
                           and (float(op[j]) / float(pc[j]) - 1.0) <= GAP_DOWN))
            out["gap_down_days"] = gaps
            if gaps >= 1:
                out["flags"].append(f"跳空低开 {gaps} 天（利空/恐慌开盘）")

        # 停牌/数据缺口：应有交易日 vs 实际行数
        need = _count_trade_days(rec_date, dates[end_i])
        got = end_i - i0
        if need > 0:
            out["suspend_gap_days"] = max(0, need - got)
            if out["suspend_gap_days"] >= 2:
                out["flags"].append(f"停牌/数据缺口 {out['suspend_gap_days']} 天")
    except Exception as exc:  # noqa: BLE001
        out["missing"].append(f"个股面板计算失败: {exc}")
    return out


# ── ③ 个股资料面板 ────────────────────────────────────────────
def profile_panel(ts_code: str, rec_date: str, df=None, event_days: int = EVENT_DAYS) -> dict:
    """个股资料事件：业绩预告·快报、持股变动、质押、分红/回购/激励、大宗折溢价、股东户数。"""
    from app.backtest.extra_feature_loader import _events_in_window, _last_row_before, _query, _parse_dates

    rec_date = _norm_date(rec_date)
    out: dict = {"events": [], "negatives": [], "positives": [], "missing": []}
    if not rec_date or not ts_code:
        out["missing"].append("参数非法")
        return out
    # 注意：必须用 numpy.datetime64（extra_feature_loader 的 _parse_dates 返回
    # datetime64[ns] 数组，与之 searchsorted 比较时传 pandas.Timestamp 会抛 TypeError）
    t0 = pd.Timestamp(rec_date).to_datetime64()

    # 业绩预告 / 快报（"业绩变脸"是最典型的个股利空）
    try:
        fc = _query(ts_code, "forecast", ["ann_date", "type"], "ann_date")
        row = _last_row_before(fc, "ann_date", t0) if not fc.empty else None
        if row is not None:
            ft = str(row.get("type") or "").strip()
            if ft:
                out["forecast_type"] = ft
                out["events"].append(f"业绩预告：{ft}（公告 {_norm_date(row.get('ann_date'))}）")
                if ft in _NEG_FORECAST:
                    out["negatives"].append(f"业绩预告{ft}")
                elif ft in _POS_FORECAST:
                    out["positives"].append(f"业绩预告{ft}")
        ex = _query(ts_code, "express", ["ann_date", "yoy_net_profit"], "ann_date")
        erow = _last_row_before(ex, "ann_date", t0) if not ex.empty else None
        if erow is not None:
            yoy = _f(erow.get("yoy_net_profit"))
            if yoy == yoy:
                out["express_yoy"] = round(yoy, 4)
                out["events"].append(f"业绩快报净利同比 {yoy * 100:+.1f}%")
                if yoy < -0.3:
                    out["negatives"].append(f"业绩快报净利同比{yoy * 100:.0f}%")
    except Exception as exc:  # noqa: BLE001
        out["missing"].append(f"业绩预告/快报: {exc}")

    # 股东增减持（近 90 日净方向）
    try:
        ht = _query(ts_code, "stk_holdertrade", ["ann_date", "in_de", "change_ratio"], "ann_date")
        win = _events_in_window(ht, "ann_date", t0, HOLDER_DAYS) if not ht.empty else pd.DataFrame()
        if not win.empty:
            signs = np.where(win["in_de"].astype(str).str.upper().str.contains("IN"), 1.0, -1.0)
            cr = pd.to_numeric(win["change_ratio"], errors="coerce").to_numpy()
            net = float(np.nansum(signs * cr)) if len(cr) else 0.0
            out["holdertrade_net"] = round(net, 4)
            dec = int(sum(1 for i, s in enumerate(signs) if s < 0 and cr[i] == cr[i] and cr[i] > 0))
            out["events"].append(f"近{HOLDER_DAYS}日股东增减持：净{'增持' if net > 0 else '减持'} {abs(net) * 100:.2f}%（减持{dec}笔）")
            if net < 0:
                out["negatives"].append("股东净减持")
            elif net > 0:
                out["positives"].append("股东净增持")
    except Exception as exc:  # noqa: BLE001
        out["missing"].append(f"股东增减持: {exc}")

    # 股权质押（高质押 = 下跌易触发平仓，属结构性风险）
    try:
        pl = _query(ts_code, "pledge_stat", ["end_date", "pledge_ratio"], "end_date")
        prow = _last_row_before(pl, "end_date", t0) if not pl.empty else None
        if prow is not None:
            pr = _f(prow.get("pledge_ratio"))
            if pr == pr:
                out["pledge_ratio"] = round(pr / 100.0 if pr > 1 else pr, 4)
                out["events"].append(f"股权质押比例 {out['pledge_ratio'] * 100:.1f}%")
                if out["pledge_ratio"] >= PLEDGE_HIGH:
                    out["negatives"].append(f"高质押{out['pledge_ratio'] * 100:.0f}%")
    except Exception as exc:  # noqa: BLE001
        out["missing"].append(f"质押: {exc}")

    # 股东户数变化（户数激增=筹码分散，属走弱信号）
    try:
        hn = _query(ts_code, "stk_holdernumber", ["ann_date", "end_date", "holder_num"], "ann_date")
        if not hn.empty:
            dts = _parse_dates(hn["ann_date"])
            idx = int(np.searchsorted(dts, t0, side="right"))
            if idx >= 2:
                cur = _f(hn["holder_num"].iloc[idx - 1])
                prev = _f(hn["holder_num"].iloc[idx - 2])
                if cur == cur and prev == prev and prev > 0:
                    chg = cur / prev - 1.0
                    out["holder_change"] = round(chg, 4)
                    out["events"].append(f"股东户数变化 {chg * 100:+.1f}%（{'筹码分散' if chg > 0 else '筹码集中'}）")
                    if chg > 0.15:
                        out["negatives"].append("股东户数激增")
    except Exception as exc:  # noqa: BLE001
        out["missing"].append(f"股东户数: {exc}")

    # 分红 / 回购 / 激励（正向事件）
    for table, label in (("dividend", "分红"), ("repurchase", "回购"), ("stk_rewards", "股权激励")):
        try:
            d = _query(ts_code, table, ["ann_date"], "ann_date")
            if not d.empty and not _events_in_window(d, "ann_date", t0, 180).empty:
                out["positives"].append(f"近180日{label}")
                out["events"].append(f"近180日{label}公告")
        except Exception:  # noqa: BLE001
            pass

    # 大宗交易折溢价（大幅折价 = 有资金急着出货）
    try:
        bq = _query(ts_code, "block_trade", ["trade_date", "price"], "trade_date")
        bwin = _events_in_window(bq, "trade_date", t0, HOLDER_DAYS) if not bq.empty else pd.DataFrame()
        if not bwin.empty:
            out["block_count"] = int(len(bwin))
            prem = []
            if df is not None and len(df):
                closes = {_norm_date(d): float(c) for d, c
                          in zip(df["trade_date"].dt.strftime("%Y%m%d"), pd.to_numeric(df["close"], errors="coerce"))
                          if c == c}
                for _, r in bwin.iterrows():
                    c = closes.get(_norm_date(r["trade_date"]))
                    p = _f(r.get("price"))
                    if c and p == p:
                        prem.append(p / c - 1.0)
            out["events"].append(f"近{HOLDER_DAYS}日大宗交易 {len(bwin)} 笔")
            if prem:
                out["block_premium"] = round(float(np.mean(prem)), 4)
                if out["block_premium"] <= -0.05:
                    out["negatives"].append(f"大宗折价{out['block_premium'] * 100:.1f}%")
    except Exception as exc:  # noqa: BLE001
        out["missing"].append(f"大宗交易: {exc}")

    return out


# ── 汇总 ──────────────────────────────────────────────────────
def _cause_prior(mkt: dict, stk: dict, prof: dict) -> str:
    """先验判据（不替代 LLM 判断，只给一个可审计的起点）。"""
    if stk.get("missing") or stk.get("tN_ret") is None or mkt.get("bench_ret") is None:
        return "insufficient_data"
    bench = float(mkt["bench_ret"])
    ret = float(stk["tN_ret"])
    rel = ret - bench
    if ret < 0 and bench <= MKT_DOWN and rel >= -REL_MATCH:
        return "external_market"          # 大盘系统性下跌、个股只是跟跌
    if rel <= REL_UNDERPERF:
        return "internal_logic"           # 显著跑输大盘：选股/择时/形态更像有问题
    if ret < 0 and prof.get("negatives"):
        return "external_news"            # 期有利空事件
    if stk.get("broke_ma20") or stk.get("sealed_down_days"):
        return "internal_technical"       # 技术结构破坏（可能是形态判断失误）
    return "mixed"


def build_evidence(ts_code: str, rec_date: str, days: int | None = None,
                   enabled: bool | None = None) -> dict:
    """组装三类证据 + 先验判据。任何异常都降级，不抛。"""
    days = days or evidence_days()
    enabled = evidence_enabled() if enabled is None else bool(enabled)
    ev: dict = {
        "ts_code": ts_code, "rec_date": _norm_date(rec_date), "days": days,
        "enabled": enabled, "market": {}, "stock": {}, "profile": {},
        "missing": [], "flags": [], "cause_prior": "disabled",
    }
    if not enabled:
        return ev

    df = None
    try:
        from app.backtest.loader import prepare_stock_data
        df = prepare_stock_data(ts_code)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[failure_context] {ts_code} 日线准备失败: {exc}")

    try:
        ev["market"] = market_panel(ev["rec_date"], days)
    except Exception as exc:  # noqa: BLE001
        ev["missing"].append(f"大盘面板失败: {exc}")
    try:
        ev["stock"] = stock_panel(ts_code, ev["rec_date"], days, df=df)
    except Exception as exc:  # noqa: BLE001
        ev["missing"].append(f"个股面板失败: {exc}")
    try:
        ev["profile"] = profile_panel(ts_code, ev["rec_date"], df=df)
    except Exception as exc:  # noqa: BLE001
        ev["missing"].append(f"资料面板失败: {exc}")

    for part in ("market", "stock", "profile"):
        ev["missing"].extend(ev[part].get("missing") or [])
    ev["flags"] = list(ev["stock"].get("flags") or [])
    if ev["profile"].get("negatives"):
        ev["flags"].extend([f"利空：{n}" for n in ev["profile"]["negatives"]])
    ev["cause_prior"] = _cause_prior(ev["market"], ev["stock"], ev["profile"])
    return ev


def to_text(ev: dict) -> str:
    """证据包 → 紧凑文本（供 LLM 归因 prompt；控制在 ~20 行内省 token）。"""
    if not ev or not ev.get("enabled"):
        return "（外部证据未启用）"
    lines: list[str] = []
    mkt = ev.get("market") or {}
    stk = ev.get("stock") or {}
    prof = ev.get("profile") or {}

    lines.append(f"【大盘】复盘窗口目标 {ev.get('days')} 个交易日（与个股收益同口径）")
    for code, info in (mkt.get("indices") or {}).items():
        pre = info.get("pre_lookback_ret")
        lines.append(
            f"  - {info.get('name')}({code})：同期 {_pct(info.get('ret'))}"
            + (f"（已走 {info.get('actual_days')} 天）" if not info.get("complete") else "")
            + (f"，推荐前20日 {_pct(pre)}" if pre is not None else "")
        )
    if mkt.get("market_down"):
        lines.append("  ⚠️ 大盘同期系统性下跌（≤-2%）→ 个股「没涨」可能主要由此解释")
    if not mkt.get("window_complete"):
        lines.append("  ⚠️ 复盘窗口尚未走完：以上为**阶段性**证据，不足以断言长期失效")

    lines.append("【个股日线】买入日 " + str(stk.get("buy_date") or "?") + " → 终点 " + str(stk.get("end_date") or "?"))
    if stk.get("missing"):
        lines.append("  数据缺失：" + "；".join(stk["missing"]))
    else:
        lines.append(
            f"  区间收益 {_pct(stk.get('tN_ret'))}（T+1 {_pct(stk.get('t1_ret'))}，"
            f"已走 {stk.get('window_days_actual')} 天）"
            f"，期间最大涨幅 {_pct(stk.get('max_gain'))}，最大回撤 {_pct(stk.get('max_drawdown'))}"
        )
        if stk.get("give_back") is not None:
            lines.append(f"  冲高回落：距窗口最高点 {_pct(stk.get('give_back'))}")
        if stk.get("sealed_down_days"):
            lines.append(f"  跌停/近跌停 {stk['sealed_down_days']} 天")
        if stk.get("vol_surge_down_days"):
            lines.append(f"  放量下跌 {stk['vol_surge_down_days']} 天")
        if stk.get("broke_ma20"):
            lines.append("  跌破 MA20（买入日尚在上方）")
        if stk.get("gap_down_days"):
            lines.append(f"  跳空低开 {stk['gap_down_days']} 天")
        if stk.get("suspend_gap_days"):
            lines.append(f"  停牌/数据缺口 {stk['suspend_gap_days']} 天")

    lines.append("【个股资料】")
    events = prof.get("events") or []
    lines.append("  " + ("；".join(events) if events else "窗口内无重大事件（未采集到的表不算「无事件」，见缺失项）"))
    if prof.get("missing"):
        lines.append("  数据缺失：" + "；".join(prof["missing"]))
    if prof.get("negatives"):
        lines.append("  利空：" + "、".join(prof["negatives"]))
    if prof.get("positives"):
        lines.append("  利多：" + "、".join(prof["positives"]))

    if ev.get("flags"):
        lines.append("【自动标记】" + "；".join(ev["flags"]))
    if ev.get("missing"):
        lines.append("【证据缺口】" + "；".join(sorted(set(ev["missing"]))[:6]))
    lines.append(f"【先验判据】cause_prior={ev.get('cause_prior')}"
                 "（仅作起点，须结合以上证据自行判断，不可直接照抄）")
    return "\n".join(lines)


def summarize(ev: dict) -> str:
    """一行摘要（入库/聚合统计用）。"""
    mkt = ev.get("market") or {}
    stk = ev.get("stock") or {}
    return (f"prior={ev.get('cause_prior')} "
            f"bench={_pct(mkt.get('bench_ret'))} "
            f"stock={_pct(stk.get('tN_ret'))} "
            f"flags={'|'.join(ev.get('flags') or []) or '无'}")


__all__ = [
    "build_evidence", "to_text", "summarize", "market_panel", "stock_panel",
    "profile_panel", "evidence_enabled", "evidence_days", "INDEX_CODES",
]
