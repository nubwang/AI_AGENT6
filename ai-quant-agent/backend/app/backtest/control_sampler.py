"""对照组采样（control_sampler）— plans/23 §2.4 T4 第二步

**要解决的根本问题**
现有回测样本全部来自"3 倍行情段"（[`surge_scanner`](ai-quant-agent/backend/app/backtest/surge_scanner.py:1) 召回），
连 failure_A 都要求涨 50%~150% → 正负样本的 T+5 正收益率都在 80% 左右
（实测 success 0.807 vs failure_A 0.788），标签几乎零区分度，任何"命中率"都无法证伪。

**第一步（已完成）**：同交易日**全市场等权** T+5 基准 → 给出"选择偏差量级"。
**第二步（本模块）**：构建**同口径对照组** —— 在同一交易日，取"**没有任何启动迹象**"的股票
（排除 t0 前 20 日累计涨幅 > 15% 者），用完全相同的 T+5 窗口计算收益。

于是可以回答三个关键问题：
  1. 我们的样本组（启动信号命中）T+5 是否**显著优于**同交易日无启动迹象的股票？（真实 alpha）
  2. 两组 T+5 正收益率差（gap）是否达到"标签有效"门槛？（`bt_discrimination_min_gap`）
  3. 若无效 → 说明"启动形态"这一层目前不产生信息，必须回到特征/标签重做（而不是继续调参）。

缓存：按交易日固化到 `data/control_samples.json`（同一交易日只采一次）。
"""
from __future__ import annotations

import json
import os
import time

from sqlalchemy import text

from app.core.logger import logger
from app.models import SessionLocal

CONTROL_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "control_samples.json",
)
DEFAULT_SAMPLE_N = 400        # 每个交易日抽样候选数（抽完还要过滤）
DEFAULT_WARMUP_PCT = 0.15     # t0 前 20 日累计涨幅超过此值 → 视为"已有启动迹象"，排除
DEFAULT_LOOKBACK = 20         # 启动迹象观察窗口（交易日）
DEFAULT_COST_PCT = 0.0015     # 与 metrics/plan_tracker 口径一致


def _nth_before(db, date_str: str, n: int) -> str:
    """d 之前第 n 个交易日（YYYYMMDD），不足返回 ''。"""
    row = db.execute(text(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date<:d "
        "ORDER BY cal_date DESC LIMIT 1 OFFSET :off"),
        {"d": date_str, "off": max(0, n - 1)}).fetchone()
    return str(row[0]) if row else ""


def _nth_after(db, date_str: str, n: int) -> str:
    row = db.execute(text(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>:d "
        "ORDER BY cal_date LIMIT 1 OFFSET :off"),
        {"d": date_str, "off": max(0, n - 1)}).fetchone()
    return str(row[0]) if row else ""


def control_t5(date_str: str, sample_n: int = DEFAULT_SAMPLE_N,
               warmup_pct: float = DEFAULT_WARMUP_PCT,
               cost_pct: float = DEFAULT_COST_PCT) -> dict | None:
    """某交易日的对照组（无启动迹象股票）T+5 收益统计。

    Returns:
        {"date","n","avg_t5","pos_ratio","t5_list"}；无法计算返回 None。
    """
    db = SessionLocal()
    try:
        d20 = _nth_before(db, date_str, DEFAULT_LOOKBACK)
        d5 = _nth_after(db, date_str, 5)
        if not d5:
            return None
        # 候选：当日有行情、价格有效
        rows = db.execute(text(
            "SELECT d0.ts_code, d0.close, d20.close AS c20, d5.close AS c5 "
            "FROM (SELECT ts_code, close FROM daily WHERE trade_date=:d AND close>0 "
            "      ORDER BY RAND() LIMIT :n) d0 "
            "JOIN daily d5 ON d5.ts_code=d0.ts_code AND d5.trade_date=:d5 "
            "LEFT JOIN daily d20 ON d20.ts_code=d0.ts_code AND d20.trade_date=:d20"),
            {"d": date_str, "n": int(sample_n), "d5": d5, "d20": d20 or date_str}).fetchall()
        rets: list[float] = []
        for _code, c0, c20, c5 in rows:
            try:
                if not c0 or not c5:
                    continue
                base = float(c0)
                if base <= 0:
                    continue
                # 排除"已有启动迹象"：t0 前 20 日涨幅超过 warmup_pct
                if c20 and float(c20) > 0 and (base / float(c20) - 1.0) > warmup_pct:
                    continue
                rets.append(float(c5) / base - 1.0 - float(cost_pct))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        if not rets:
            return None
        n = len(rets)
        return {
            "date": date_str,
            "n": n,
            "avg_t5": sum(rets) / n,
            "pos_ratio": sum(1 for r in rets if r > 0) / n,
            "t5_list": [round(r, 6) for r in rets],
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"control_t5({date_str}) 失败: {exc}")
        return None
    finally:
        db.close()


def controls_for_dates(dates: list[str], sample_n: int = DEFAULT_SAMPLE_N,
                       warmup_pct: float = DEFAULT_WARMUP_PCT,
                       cost_pct: float = DEFAULT_COST_PCT,
                       max_new: int = 400) -> dict[str, dict]:
    """批量取对照组（带磁盘缓存）。

    Args:
        max_new: 单次最多新算多少个交易日（防首次回测卡太久；其余下次回测继续补）
    """
    uniq = sorted({str(d) for d in (dates or []) if d})
    if not uniq:
        return {}
    cache: dict = {}
    try:
        if os.path.exists(CONTROL_FILE):
            with open(CONTROL_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f) or {}
    except Exception:  # noqa: BLE001
        cache = {}

    key_suffix = f"|n{sample_n}|w{warmup_pct}|c{cost_pct}"
    out: dict[str, dict] = {}
    missing: list[str] = []
    for d in uniq:
        v = cache.get(d + key_suffix)
        if isinstance(v, dict):
            out[d] = v
        else:
            missing.append(d)

    if missing:
        todo = missing[:max_new]
        t0 = time.time()
        ok = 0
        for d in todo:
            r = control_t5(d, sample_n=sample_n, warmup_pct=warmup_pct, cost_pct=cost_pct)
            if not r:
                continue
            out[d] = r
            cache[d + key_suffix] = r
            ok += 1
        try:
            os.makedirs(os.path.dirname(CONTROL_FILE), exist_ok=True)
            with open(CONTROL_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"对照组缓存写入失败: {exc}")
        logger.info(
            f"对照组采样完成：{ok}/{len(todo)} 个交易日（{time.time() - t0:.1f}s，"
            f"缓存累计 {len(cache)} 条，本次未算 {max(0, len(missing) - len(todo))} 个，下次回测继续补）"
        )
    return out


def control_t5_list(dates: list[str], **kw) -> list[float | None]:
    """把对照组按日期展开成"逐样本"的对照收益序列（供 compute_metrics 的 benchmark_t5）。

    配对语义：样本组第 i 个样本 → 用**同一交易日**的对照组均值做参照，
    逐日对齐，避免"用全样本平均去比单个日期"的时间错配。
    """
    ctl = controls_for_dates(dates, **kw)
    out: list[float | None] = []
    for d in dates:
        c = ctl.get(str(d))
        out.append(float(c["avg_t5"]) if isinstance(c, dict) and c.get("avg_t5") is not None else None)
    return out


def pool_controls(ctl: dict[str, dict], dates: list[str] | None = None) -> dict:
    """把逐交易日的对照组聚合成**一个总对照**（按样本数加权）。

    用于 [`compare_to_control`](ai-quant-agent/backend/app/backtest/metrics.py:359) 的比例检验：
    对照组只提供每日聚合值（n / pos_ratio），必须先加权汇总再与样本组做 z 检验。

    Returns:
        {"n","pos_ratio","avg_t5","dates"}；无有效对照时 n=0。
    """
    keys = [str(d) for d in dates] if dates else sorted(ctl.keys())
    n = 0
    pos_sum = 0.0
    ret_sum = 0.0
    used = 0
    for k in keys:
        v = ctl.get(k)
        if not isinstance(v, dict):
            continue
        try:
            ni = int(v.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if ni <= 0:
            continue
        n += ni
        pos_sum += float(v.get("pos_ratio") or 0.0) * ni
        ret_sum += float(v.get("avg_t5") or 0.0) * ni
        used += 1
    if n <= 0:
        return {"n": 0, "pos_ratio": 0.0, "avg_t5": 0.0, "dates": 0}
    return {"n": n, "pos_ratio": pos_sum / n, "avg_t5": ret_sum / n, "dates": used}


__all__ = [
    "control_t5", "controls_for_dates", "control_t5_list", "pool_controls", "CONTROL_FILE",
]
