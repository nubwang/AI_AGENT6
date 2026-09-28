"""市场基准（benchmark）— 对照组的可计算替代（plans/23 §2.4 T4 第一步）

**为什么需要它**：
现有回测样本全部来自"事后已爆发的 3 倍行情段"，连 failure_A 都要求涨 50%~150%
→ 正负样本的 T+5 正收益率都接近 80%（实测 success 0.807 vs failure_A 0.788），
"命中率/超额"因此无法衡量（这正是 T3/T4 的根因）。

**本模块做什么（第一步）**：
给出**同一交易日的全市场等权 T+5 基准**，让"条件集内收益"有一个可解释的对照：
  > "这批样本 T+5 平均 +14%，而同期市场只有 +1.2%" → 真实超额 +12.8pct（且可给显著性）
这样 `performance.lift_t5_pos` 才第一次有了意义。

**不做什么**：它还不是"同形态未爆发"的严格对照组（那需要按形态分层采样，属 P1 第二步）。
本模块的好处是：**口径明确、可复现、零人工标注、当日可用**。

缓存：结果按交易日固化到 `data/market_benchmark.json`（同一交易日只算一次）。
"""
from __future__ import annotations

import json
import os
import time

from sqlalchemy import text

from app.core.logger import logger
from app.models import SessionLocal

BENCH_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "market_benchmark.json",
)
DEFAULT_SAMPLE_N = 300      # 每日抽样股票数（等权基准，300 只足以代表市场均值）
DEFAULT_COST_PCT = 0.0015   # 与 plan_tracker/metrics 口径一致（双边佣金+印花税）
_T5_OFFSET = 5              # T+5


def _load_cache() -> dict:
    try:
        if os.path.exists(BENCH_FILE):
            with open(BENCH_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(BENCH_FILE), exist_ok=True)
        with open(BENCH_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"基准缓存写入失败: {exc}")


def _nth_trade_date(db, date_str: str, n: int = _T5_OFFSET) -> str:
    """d 之后第 n 个交易日（YYYYMMDD），不足返回 ''。"""
    row = db.execute(text(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>:d "
        "ORDER BY cal_date LIMIT :n OFFSET :off"),
        {"d": date_str, "n": 1, "off": n - 1}).fetchone()
    return str(row[0]) if row else ""


def market_t5(date_str: str, sample_n: int = DEFAULT_SAMPLE_N,
              cost_pct: float = DEFAULT_COST_PCT) -> float | None:
    """某交易日的全市场等权 T+5 收益（抽样 sample_n 只，含成本）。失败返回 None。"""
    db = SessionLocal()
    try:
        d5 = _nth_trade_date(db, date_str, _T5_OFFSET)
        if not d5:
            return None
        row = db.execute(text(
            "SELECT AVG(d5.close / d0.close - 1.0) FROM ("
            "  SELECT ts_code, close FROM daily WHERE trade_date=:d AND close>0"
            "  ORDER BY RAND() LIMIT :n"
            ") d0 JOIN daily d5 ON d5.ts_code=d0.ts_code AND d5.trade_date=:d5"),
            {"d": date_str, "n": int(sample_n), "d5": d5}).scalar()
        if row is None:
            return None
        return float(row) - float(cost_pct)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"market_t5({date_str}) 失败: {exc}")
        return None
    finally:
        db.close()


def benchmark_for_dates(dates: list[str], sample_n: int = DEFAULT_SAMPLE_N,
                        cost_pct: float = DEFAULT_COST_PCT) -> dict[str, float]:
    """批量取多交易日的市场基准（带磁盘缓存）。

    Args:
        dates: 交易日列表（YYYYMMDD，可重复/乱序）
        sample_n: 每日抽样股票数
        cost_pct: 单笔往返成本

    Returns:
        {date: t5_return}
    """
    uniq = sorted({str(d) for d in (dates or []) if d})
    if not uniq:
        return {}
    cache = _load_cache()
    key_suffix = f"|n{sample_n}|c{cost_pct}"
    out: dict[str, float] = {}
    missing: list[str] = []
    for d in uniq:
        v = cache.get(d + key_suffix)
        if isinstance(v, (int, float)):
            out[d] = float(v)
        else:
            missing.append(d)

    if missing:
        t0 = time.time()
        ok = 0
        for d in missing:
            v = market_t5(d, sample_n=sample_n, cost_pct=cost_pct)
            if v is None:
                continue
            out[d] = float(v)
            cache[d + key_suffix] = float(v)
            ok += 1
        _save_cache(cache)
        logger.info(f"市场基准计算完成：{ok}/{len(missing)} 个交易日（{time.time() - t0:.1f}s，缓存累计 {len(cache)} 条）")
    return out


__all__ = ["market_t5", "benchmark_for_dates", "BENCH_FILE"]
