"""LLM 计量与预算闸门（llm_metering）— 对齐 plans/25 §四

## 为什么需要它（用户明确要求）

自证测试默认走「LLM 档」（完全复刻每日推荐），回放 N 天 = N × 单日 60~150 次 LLM 调用。
没有计量就无法：① 运行前告诉用户"大概多少钱"；② 超预算自动熔断；③ 事后对账。

## DeepSeek 到底能查到什么（事实，勿误解）

- **余额可查、按次费用不可查**：`GET https://api.deepseek.com/user/balance`（Bearer Key）
  只返回"还剩多少额度"（`is_available` + `balance_infos[].total_balance/...`），
  **官方没有 per-request / per-task 的账单查询接口**。
- **用量只能自己算**：`/chat/completions` 响应体 `usage` 含
  `prompt_tokens / completion_tokens / total_tokens /
   prompt_cache_hit_tokens / prompt_cache_miss_tokens` —— 这是唯一的计费数据源。
  它原先被 [`deepseek_chat()`](ai-quant-agent/backend/app/agents/llm_client.py:55) 丢弃，本模块负责接管。

## 本模块提供

  ① `record_usage()`     每次调用落 `data/llm_usage.jsonl`（token 明细 + 按价格表算出的费用）
  ② 价格表 `data/llm_pricing.json`：**人工维护**（含 as_of_date 与来源备注）。
     未填价时费用记 `None`、预估器明确报"价格未配置" —— **绝不用假价格糊弄用户**。
  ③ 三闸门预算（单任务 / 单日 / 累计）：每次调用前检查，超限阻断 LLM 调用（降级返回 None，
     与既有"LLM 失败即降级"契约一致），并置 `budget_blocked()` 标志供自证内核优雅停止。
  ④ `estimate_task()`    运行前预估：调用次数模型 × 实测平均 token × 价格表 → 低/中/高三档
  ⑤ `balance()` / `snapshot_balance()` / `reconcile()`  余额代理与事后对账

## 设计原则

- **计量是附属能力**：任何异常都不得影响 LLM 主流程（调用方一律 try/except 包裹）。
- **价格是钱**：价格表只允许人工改（`evolution_config` 里若不登记，进化大脑无从自动改价）。
- **不写死单价**：本文件不含任何具体单价，全部来自价格表；未配置就是"未知"，不是"免费"。
"""
from __future__ import annotations

import json
import math
import os
import threading
import time

import requests

from app.core.config import settings
from app.core.logger import logger

# 数据目录：backend/data/
DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data",
)
USAGE_FILE = os.path.join(DATA_DIR, "llm_usage.jsonl")
PRICING_FILE = os.path.join(DATA_DIR, "llm_pricing.json")
BUDGET_FILE = os.path.join(DATA_DIR, "llm_budget.json")
BALANCE_FILE = os.path.join(DATA_DIR, "llm_balance_snapshots.json")

BALANCE_URL = "https://api.deepseek.com/user/balance"
BALANCE_TIMEOUT = 15

# 单文件保护上限（防止 jsonl/台账无限膨胀）
MAX_USAGE_TAIL_LINES = 200000
MAX_LEDGER_ROWS = 2000
MAX_BALANCE_SNAPSHOTS = 500

_LOCK = threading.RLock()
_BLOCKED = {"flag": False, "reason": "", "at": ""}

# 每调用平均耗时（秒，粗估用；仅用于"预计耗时"，不影响钱）
DEFAULT_LATENCY_SEC = 8.0   # DeepSeek 长上下文 + 重试的单次平均耗时经验值


class BudgetExceeded(Exception):
    """预算熔断（保留给需要"硬失败"语义的调用方；LLM 路径走 ensure_budget 返回 False）。"""

    def __init__(self, gate: str, limit: float, spent: float, message: str = ""):
        self.gate, self.limit, self.spent = gate, limit, spent
        super().__init__(message or f"预算熔断[{gate}]：已花 {spent:.4f} / 上限 {limit:.4f}")


# ── 价格表 ─────────────────────────────────────────────────────
DEFAULT_PRICING = {
    "version": 2,
    "currency": "CNY",
    "unit": "per_1m_tokens",
    "verified": True,
    "as_of_date": "2026-09-22",
    "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
    "note": (
        "单价抓取自 DeepSeek 官方定价页（curl 实抓，2026-09-22）。峰谷定价：北京时间周一至周五 "
        "09:00-12:00、14:00-18:00 为高峰，其余（含周末与中国法定节假日）为空闲；空闲价 = 高峰价一半。"
        "价格属于钱的范畴，只允许人工修改，禁止进化大脑自动改价；请定期（建议 ≤30 天）复核 as_of_date。"
    ),
    "peak_windows_bj": [["09:00", "12:00"], ["14:00", "18:00"]],
    "peak_weekdays_only": True,
    "default_model": "deepseek-flash",
    # 官方 /models 只列 deepseek-flash / deepseek-v4-pro；deepseek-chat 是别名（服务端会返回真实 model）
    "aliases": {"deepseek-chat": "deepseek-flash", "deepseek-reasoner": "deepseek-v4-pro"},
    "models": {
        "deepseek-flash": {
            "version": "DeepSeek-V4.1-Flash",
            "off_peak": {"input_cache_hit_per_m": 0.02, "input_cache_miss_per_m": 1.0, "output_per_m": 4.0},
            "peak": {"input_cache_hit_per_m": 0.04, "input_cache_miss_per_m": 2.0, "output_per_m": 8.0},
        },
        "deepseek-v4-pro": {
            "version": "DeepSeek-V4-Pro-0813",
            "off_peak": {"input_cache_hit_per_m": 0.15, "input_cache_miss_per_m": 4.5, "output_per_m": 13.5},
            "peak": {"input_cache_hit_per_m": 0.30, "input_cache_miss_per_m": 9.0, "output_per_m": 27.0},
        },
    },
}

# ── 预算表 ─────────────────────────────────────────────────────
# 默认闸门**对齐账户真实余额量级**（实测 2026-09-22 余额 10.56 元）：
# 闸门若远大于余额，就形同虚设 —— 会出现"没触熔断但余额已烧穿"的假安全。
# ★ 2026-09-23 修正：原 per_day=3.0 元**不够一次完整扫描** —— 当天 189 次调用就花掉
#   3.0017 元（≈1.6 分/次，187 次带思维链），预算一满，`deepseek_chat()` 就**不再发请求直接
#   返回 None** → RefineAgent 大面积降级成"Agent 分析失败，按规则概率保留"（当天 32 条熔断日志）。
#   故默认值改由 settings 提供（.env 可覆盖，仅影响**首次建台账**的默认值）。
DEFAULT_BUDGET = {
    "version": 1,
    "currency": "CNY",
    "enabled": True,
    "limits": {
        "per_task_cny": float(getattr(settings, "llm_budget_per_task_cny", 5.0)),
        "per_day_cny": float(getattr(settings, "llm_budget_per_day_cny", 10.0)),
        "total_cny": float(getattr(settings, "llm_budget_total_cny", 50.0)),
    },
    "spent": {"total_cny": 0.0, "day": {}, "task": {}},
    "ledger": [],
}


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: str, data) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[llm_metering] 写入 {os.path.basename(path)} 失败: {exc}")
        return False


def load_pricing(create: bool = True) -> dict:
    """读价格表；缺失时落一份"未填价"模板（便于人工填写，而不是悄悄用假价）。"""
    p = _read_json(PRICING_FILE, None)
    if isinstance(p, dict) and "models" in p:
        return p
    if create:
        _write_json(PRICING_FILE, DEFAULT_PRICING)
    return json.loads(json.dumps(DEFAULT_PRICING))


def save_pricing(pricing: dict) -> bool:
    return _write_json(PRICING_FILE, pricing)


def resolve_model(model: str = "") -> str:
    """模型别名解析（官方 `/models` 只列 deepseek-flash / deepseek-v4-pro）。

    实测（2026-09-22）：请求 `model=deepseek-chat`，响应体里 `"model":"deepseek-flash"` ——
    所以**计费必须用响应体的 model**，否则会去价格表里查一个不存在的键 → 费用记成 None。
    """
    pricing = load_pricing()
    m = (model or "").strip()
    m = str((pricing.get("aliases") or {}).get(m, m))
    models = pricing.get("models") or {}
    if m and m in models:
        return m
    return str(pricing.get("default_model") or "deepseek-flash")


def current_period(ts: float | None = None) -> str:
    """当前计费时段 `"peak" | "off_peak"`（以北京时间为准；本机时区即 Asia/Shanghai）。

    官方口径：周一至周五 09:00-12:00、14:00-18:00 为高峰，其余时段（含周末与中国法定节假日全天）
    为空闲，空闲价 = 高峰价的一半。⇒ 自证这类批量任务应排到空闲时段，**直接省 50%**。
    """
    t = time.localtime(ts if ts is not None else time.time())
    pricing = load_pricing()
    if pricing.get("peak_weekdays_only", True) and t.tm_wday >= 5:
        return "off_peak"
    hhmm = f"{t.tm_hour:02d}:{t.tm_min:02d}"
    for lo, hi in (pricing.get("peak_windows_bj") or [["09:00", "12:00"], ["14:00", "18:00"]]):
        if str(lo) <= hhmm < str(hi):
            return "peak"
    return "off_peak"


def price_for(model: str = "", period: str | None = None) -> dict | None:
    """取某模型某时段的 3 个单价（元/百万 token）。任一未填 → None（"价格未知"，不是 0）。

    - 兼容旧版扁平结构（无 off_peak/peak 拆分）→ 视为时段无关
    - `period=None` → 用当前时段（`current_period()`）
    """
    pricing = load_pricing()
    models = pricing.get("models") or {}
    row = models.get(resolve_model(model)) or {}
    p = period or current_period()
    sub = row.get(p) if isinstance(row.get(p), dict) else None
    if sub is None and isinstance(row.get("off_peak"), dict):
        sub = row.get("off_peak")
    src = sub or row          # 旧版扁平结构：直接用 row（时段无关）
    need = ("input_cache_hit_per_m", "input_cache_miss_per_m", "output_per_m")
    if any(src.get(k) is None for k in need):
        return None
    try:
        return {k: float(src[k]) for k in need}
    except (TypeError, ValueError):
        return None


def pricing_status() -> dict:
    """价格表状态（供 UI/预估器明示'能不能算钱'、以及当前是否高峰时段）。"""
    pricing = load_pricing()
    return {
        "priced": price_for() is not None,
        "verified": bool(pricing.get("verified")),
        "as_of_date": pricing.get("as_of_date") or "",
        "currency": pricing.get("currency") or "CNY",
        "source": pricing.get("source") or "",
        "file": PRICING_FILE,
        "period_now": current_period(),
        "peak_windows_bj": pricing.get("peak_windows_bj") or [],
        "billing_models": sorted((pricing.get("models") or {}).keys()),
        "aliases": pricing.get("aliases") or {},
    }


# ── 费用计算 ───────────────────────────────────────────────────
def split_prompt_tokens(usage: dict) -> tuple[int, int]:
    """把 prompt_tokens 拆成 (缓存命中, 缓存未命中)。字段缺失时保守按"全部未命中"（会高估，不低估）。"""
    prompt = int(usage.get("prompt_tokens") or 0)
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is None and miss is None:
        return 0, prompt
    hit = int(hit or 0)
    miss = int(miss or 0)
    if hit + miss < prompt:            # 兜底：补齐差额
        miss += prompt - hit - miss
    elif hit + miss > prompt and hit + miss > 0:
        # 理论上不会发生；若发生按比例收窄（保证总和不超 prompt_tokens）
        scale = prompt / float(hit + miss)
        hit, miss = int(hit * scale), int(miss * scale)
    return max(hit, 0), max(miss, 0)


def cost_of(usage: dict | None, model: str = "", period: str | None = None,
            at: float | None = None) -> float | None:
    """单次调用费用（元），按**实际时段**计价（峰谷两套价）。

    Args:
        period: 显式指定 `"peak"/"off_peak"`；None → 按 `at`（或当前）时刻判定
        at: 计费时刻（epoch 秒），用于回填/复算历史记录
    价格未配置 → None（**不是 0**，避免"看起来免费"）。
    """
    if not usage:
        return None
    price = price_for(model, period or current_period(at))
    if price is None:
        return None
    hit, miss = split_prompt_tokens(usage)
    comp = int(usage.get("completion_tokens") or 0)
    cost = (
        hit / 1e6 * price["input_cache_hit_per_m"]
        + miss / 1e6 * price["input_cache_miss_per_m"]
        + comp / 1e6 * price["output_per_m"]
    )
    return round(cost, 8)


# ── 用量落盘 ───────────────────────────────────────────────────
def record_usage(source: str = "llm", model: str = "", usage: dict | None = None,
                 run_id: str = "", extra: dict | None = None) -> dict:
    """记录一次 LLM 调用（token 明细 + 费用），并累加预算台账。返回记录本身。

    - 落盘 `llm_usage.jsonl`（一行一次，便于 awk/grep 排查）
    - `cost_cny` 可能为 None（价格未配置）→ 只计 token，不累加钱
    """
    usage = usage or {}
    hit, miss = split_prompt_tokens(usage)
    period = current_period()
    prompt_n = int(usage.get("prompt_tokens") or 0)
    comp_n = int(usage.get("completion_tokens") or 0)
    # 思考模式的 reasoning_tokens 已包含在 completion_tokens 里（按"输出"计费），
    # 但单独留字段便于解释"为什么这次这么贵"。
    reasoning_n = int(usage.get("reasoning_tokens")
                      or (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source or "llm",
        "run_id": run_id or "",
        # model = 服务端返回的真实计费模型（实测 deepseek-chat 会被路由为 deepseek-flash）；
        # model_requested 保留原始请求名，便于排查别名路由与思考模式差异。
        "model": resolve_model(model),
        "model_requested": model or "",
        "period": period,
        "prompt_tokens": prompt_n,
        "completion_tokens": comp_n,
        "reasoning_tokens": reasoning_n,
        "total_tokens": int(usage.get("total_tokens") or 0),
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "cost_cny": cost_of({"prompt_tokens": prompt_n, "completion_tokens": comp_n,
                             "prompt_cache_hit_tokens": hit,
                             "prompt_cache_miss_tokens": miss}, model, period),
    }
    if extra:
        rec["extra"] = extra
    with _LOCK:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(USAGE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[llm_metering] usage 落盘失败: {exc}")
        if rec["cost_cny"] is not None:
            add_spend(float(rec["cost_cny"]), run_id=run_id, source=rec["source"])
    return rec


def _iter_usage(tail_lines: int = MAX_USAGE_TAIL_LINES):
    """逐行读用量明细（超大文件只读尾部 N 行，避免内存爆）。"""
    if not os.path.exists(USAGE_FILE):
        return
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-tail_lines:]:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[llm_metering] usage 读取失败: {exc}")


def summary(days: int = 30, run_id: str = "", source: str = "") -> dict:
    """用量/费用汇总（按天 + 按来源），供前端与报告展示、预估器校准。"""
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - max(days, 1) * 86400))
    out = {
        "window_days": days, "since": since,
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cache_hit_tokens": 0, "cache_miss_tokens": 0, "reasoning_tokens": 0,
        "cost_cny": 0.0, "unpriced_calls": 0,
        "by_day": {}, "by_source": {}, "by_period": {},
        "period_now": current_period(),
    }
    for r in _iter_usage():
        ts = str(r.get("ts") or "")
        if ts[:10] < since:
            continue
        if run_id and str(r.get("run_id") or "") != run_id:
            continue
        if source and not str(r.get("source") or "").startswith(source):
            continue
        out["calls"] += 1
        for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                  "cache_hit_tokens", "cache_miss_tokens", "reasoning_tokens"):
            out[k] += int(r.get(k) or 0)
        c = r.get("cost_cny")
        if c is None:
            out["unpriced_calls"] += 1
        else:
            out["cost_cny"] += float(c)
        day = ts[:10]
        d = out["by_day"].setdefault(day, {"calls": 0, "cost_cny": 0.0})
        d["calls"] += 1
        d["cost_cny"] = round(d["cost_cny"] + float(c or 0), 6)
        s = out["by_source"].setdefault(str(r.get("source") or "llm"), {"calls": 0, "cost_cny": 0.0})
        s["calls"] += 1
        s["cost_cny"] = round(s["cost_cny"] + float(c or 0), 6)
        # 峰谷拆分：让"空闲时段省了多少钱"可核对
        pd = out["by_period"].setdefault(str(r.get("period") or "unknown"), {"calls": 0, "cost_cny": 0.0})
        pd["calls"] += 1
        pd["cost_cny"] = round(pd["cost_cny"] + float(c or 0), 6)
    out["cost_cny"] = round(out["cost_cny"], 6)
    out["avg_cost_per_call"] = round(out["cost_cny"] / out["calls"], 8) if out["calls"] else None
    out["avg_prompt_tokens"] = round(out["prompt_tokens"] / out["calls"], 1) if out["calls"] else None
    out["avg_completion_tokens"] = round(out["completion_tokens"] / out["calls"], 1) if out["calls"] else None
    out["priced"] = price_for() is not None
    return out


def observed_stats(days: int = 30) -> dict:
    """近 N 天"单次调用"的实测画像（预估器的输入；无数据时给出标记）。"""
    s = summary(days=days)
    if not s["calls"]:
        return {"has_data": False, "days": days}
    return {
        "has_data": True,
        "days": days,
        "calls": s["calls"],
        "avg_prompt_tokens": s["avg_prompt_tokens"],
        "avg_completion_tokens": s["avg_completion_tokens"],
        "avg_cost_per_call": s["avg_cost_per_call"],
        "cache_hit_ratio": (round(s["cache_hit_tokens"] / float(s["prompt_tokens"]), 4)
                            if s["prompt_tokens"] else 0.0),
    }


# ── 预算闸门 ───────────────────────────────────────────────────
def load_budget(create: bool = True) -> dict:
    b = _read_json(BUDGET_FILE, None)
    if isinstance(b, dict) and "limits" in b:
        b.setdefault("spent", {"total_cny": 0.0, "day": {}, "task": {}})
        b["spent"].setdefault("total_cny", 0.0)
        b["spent"].setdefault("day", {})
        b["spent"].setdefault("task", {})
        b.setdefault("ledger", [])
        return b
    if create:
        _write_json(BUDGET_FILE, DEFAULT_BUDGET)
    return json.loads(json.dumps(DEFAULT_BUDGET))


def save_budget(b: dict) -> bool:
    return _write_json(BUDGET_FILE, b)


def set_limits(per_task_cny: float | None = None, per_day_cny: float | None = None,
               total_cny: float | None = None, enabled: bool | None = None) -> dict:
    """人工/API 设置三闸门（不放开价格表，价格只能人工改文件）。"""
    with _LOCK:
        b = load_budget()
        lim = b.setdefault("limits", {})
        if per_task_cny is not None:
            lim["per_task_cny"] = float(per_task_cny)
        if per_day_cny is not None:
            lim["per_day_cny"] = float(per_day_cny)
        if total_cny is not None:
            lim["total_cny"] = float(total_cny)
        if enabled is not None:
            b["enabled"] = bool(enabled)
        save_budget(b)
        return b


def begin_task(run_id: str, reset: bool = True) -> dict:
    """开始/续跑一个任务：初始化该任务的已花金额，并清除熔断标志。"""
    if not run_id:
        return spend_snapshot()
    with _LOCK:
        b = load_budget()
        if reset:
            b["spent"].setdefault("task", {})[run_id] = 0.0
            save_budget(b)
        _BLOCKED.update({"flag": False, "reason": "", "at": ""})
    return spend_snapshot(run_id=run_id)


def add_spend(amount: float, run_id: str = "", source: str = "") -> None:
    """累加已花金额（total / day / task 三处），并维护一段有限长度的台账。"""
    if amount is None or amount <= 0:
        return
    with _LOCK:
        b = load_budget()
        day = time.strftime("%Y%m%d")
        spent = b.setdefault("spent", {"total_cny": 0.0, "day": {}, "task": {}})
        spent["total_cny"] = round(float(spent.get("total_cny") or 0.0) + float(amount), 8)
        spent.setdefault("day", {})[day] = round(float(spent["day"].get(day) or 0.0) + float(amount), 8)
        if run_id:
            spent.setdefault("task", {})[run_id] = round(
                float(spent["task"].get(run_id) or 0.0) + float(amount), 8)
        ledger = b.setdefault("ledger", [])
        ledger.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "amount_cny": round(float(amount), 8),
                       "run_id": run_id, "source": source})
        if len(ledger) > MAX_LEDGER_ROWS:
            b["ledger"] = ledger[-MAX_LEDGER_ROWS:]
        save_budget(b)


def spend_snapshot(run_id: str = "") -> dict:
    b = load_budget()
    spent = b.get("spent") or {}
    day = time.strftime("%Y%m%d")
    return {
        "enabled": bool(b.get("enabled", True)),
        "limits": b.get("limits") or {},
        "total_cny": round(float(spent.get("total_cny") or 0.0), 8),
        "today_cny": round(float((spent.get("day") or {}).get(day) or 0.0), 8),
        "task_cny": (round(float((spent.get("task") or {}).get(run_id) or 0.0), 8)
                     if run_id else None),
        "run_id": run_id,
        "blocked": budget_blocked(),
    }


def check_budget(run_id: str = "") -> dict:
    """返回 `{ok, gate, limit, spent}`：ok=False 表示该闸门已破，需熔断。"""
    b = load_budget()
    if not bool(b.get("enabled", True)):
        return {"ok": True, "gate": "", "limit": None, "spent": None}
    lim = b.get("limits") or {}
    spent = b.get("spent") or {}
    day = time.strftime("%Y%m%d")

    def _cmp(gate: str, limit, used) -> dict:
        if limit is None:
            return {"ok": True, "gate": gate, "limit": None, "spent": used}
        if float(used) >= float(limit):
            return {"ok": False, "gate": gate, "limit": float(limit), "spent": float(used)}
        return {"ok": True, "gate": gate, "limit": float(limit), "spent": float(used)}

    if run_id:
        r = _cmp("per_task", lim.get("per_task_cny"), (spent.get("task") or {}).get(run_id) or 0.0)
        if not r["ok"]:
            return r
    r = _cmp("per_day", lim.get("per_day_cny"), (spent.get("day") or {}).get(day) or 0.0)
    if not r["ok"]:
        return r
    return _cmp("total", lim.get("total_cny"), spent.get("total_cny") or 0.0)


def ensure_budget(run_id: str = "") -> bool:
    """调用前闸门检查。返回 False = 已熔断（调用方应放弃本次 LLM 调用）。

    ★ 熔断**不是静默降级**：这里把"熔断事实 + 已花/上限 + 怎么调"上报事件流（去重），
    调用方也可用 `budget_blocked_brief()` 把原因写进自己的降级文案（见 refine_agent）。
    """
    r = check_budget(run_id=run_id)
    if r["ok"]:
        return True
    reason = (f"预算熔断[{r['gate']}]：已花 {r['spent']:.4f} / 上限 {r['limit']:.4f} "
              f"（run_id={run_id or '-'}）")
    _BLOCKED.update({"flag": True, "reason": reason, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "gate": r["gate"], "limit": r["limit"], "spent": r["spent"]})
    logger.error(f"[llm_metering] {reason}")
    _notify_block(r)
    return False


def _notify_block(r: dict) -> None:
    """熔断上报（去重：同闸门 + 已花金额每 0.1 元档只报一条，防刷屏）。

    为什么必须上报：预算打满后**所有** LLM 调用都会降级 —— 2026-09-23 实测日志里 32 条熔断，
    而前端大面积显示"Agent 分析失败，按规则概率保留"（看不出是"没钱了"）。
    """
    sig = f"{r['gate']}:{int(float(r['spent']) * 10)}"        # 每 0.1 元档一条
    if _BLOCKED.get("notified") == sig:
        return
    _BLOCKED["notified"] = sig
    hint = ("调大 llm_budget_per_day_cny（.env）或调用 set_limits(per_day_cny=…)；"
            "降本可用 DEEPSEEK_THINKING=false（实测约省 3.7 倍）或缩小候选数/精筛数量")
    try:
        from app.agents import evolution_events
        evolution_events.record_event(
            "WARNING", "llm_budget",
            f"LLM 预算熔断：{r['gate']} 已花 {r['spent']:.4f} / 上限 {r['limit']:.4f} 元；"
            f"此后 LLM 调用被阻断，Agent 将降级为「按规则概率保留」。修复：{hint}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[llm_metering] 熔断上报事件流失败（不影响主流程）: {exc}")


def budget_blocked_brief() -> str:
    """熔断简报（一句话，供 Agent 降级文案/前端直接展示；未熔断返回 ''）。"""
    if not _BLOCKED.get("flag"):
        return ""
    return (f"{_BLOCKED.get('gate')} 已用 {float(_BLOCKED.get('spent') or 0):.4f}/"
            f"{float(_BLOCKED.get('limit') or 0):.4f} 元")


def budget_blocked() -> bool:
    return bool(_BLOCKED["flag"])


def budget_blocked_reason() -> str:
    return str(_BLOCKED["reason"] or "")


def clear_budget_block() -> None:
    _BLOCKED.update({"flag": False, "reason": "", "at": "", "notified": ""})


def raise_if_blocked(run_id: str = "") -> None:
    """给需要"硬失败"语义的调用方（如自证内核）用：超预算直接抛异常。"""
    r = check_budget(run_id=run_id)
    if not r["ok"]:
        raise BudgetExceeded(r["gate"], float(r["limit"]), float(r["spent"]))


# ── 余额查询与对账 ─────────────────────────────────────────────
def balance(timeout: int = BALANCE_TIMEOUT) -> dict:
    """DeepSeek 余额查询（唯一官方"费用相关"接口：只能看剩多少，看不了花了多少）。"""
    if not settings.deepseek_api_key:
        return {"ok": False, "error": "未配置 DEEPSEEK_API_KEY"}
    try:
        r = requests.get(
            BALANCE_URL,
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}",
                     "Accept": "application/json"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json() or {}
        infos = data.get("balance_infos") or []
        main = infos[0] if infos else {}
        return {
            "ok": True,
            "is_available": bool(data.get("is_available")),
            "currency": main.get("currency", ""),
            "total_balance": main.get("total_balance"),
            "granted_balance": main.get("granted_balance"),
            "topped_up_balance": main.get("topped_up_balance"),
            "balance_infos": infos,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"余额查询失败: {exc}"}


def snapshot_balance(note: str = "", run_id: str = "") -> dict:
    """记录一次余额快照（对账用：运行前后各记一次，差额 ≈ 实际花费）。"""
    bal = balance()
    snap = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note, "run_id": run_id, **bal}
    with _LOCK:
        rows = _read_json(BALANCE_FILE, [])
        if not isinstance(rows, list):
            rows = []
        rows.append(snap)
        _write_json(BALANCE_FILE, rows[-MAX_BALANCE_SNAPSHOTS:])
    return snap


def snapshots() -> list:
    rows = _read_json(BALANCE_FILE, [])
    return rows if isinstance(rows, list) else []


def reconcile(run_id: str = "") -> dict:
    """对账：余额差额 vs token 计价花费（两者差异大 ⇒ 价格表过期或计费口径变了）。"""
    rows = [r for r in snapshots() if r.get("ok") and (not run_id or r.get("run_id") == run_id)]
    if len(rows) < 2:
        return {"ok": False, "error": "余额快照不足（至少需要运行前后各一次）"}
    first, last = rows[0], rows[-1]
    try:
        delta = float(first.get("total_balance")) - float(last.get("total_balance"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "余额字段解析失败"}
    s = summary(days=3650, run_id=run_id)
    est = float(s.get("cost_cny") or 0.0)
    diff_pct = (abs(delta - est) / delta * 100.0) if delta > 0 else None
    return {
        "ok": True, "run_id": run_id,
        "balance_delta_cny": round(delta, 6),
        "token_cost_cny": round(est, 6),
        "diff_cny": round(delta - est, 6),
        "diff_pct": None if diff_pct is None else round(diff_pct, 2),
        "warn": bool(diff_pct is not None and diff_pct > 20.0),
        "note": "余额差额含其它任务/控制台消耗；差异 >20% 时优先怀疑价格表过期",
    }


# ── 运行前预估 ─────────────────────────────────────────────────
def calls_per_day_estimate(candidates: int = 150, deep_top_n: int = 50, n_sectors: int = 5,
                           batch_size: int = 25, policy_sub_agents: int = 1,
                           attribute_failures: int = 20) -> dict:
    """单日 LLM 调用次数模型（与 [`run_agent_refine()`](ai-quant-agent/backend/app/agents/__init__.py:122)
    的真实结构对齐：L1 批量粗筛 / 政策解读 / L2 逐只精筛 / L3 决策 + 自证归因）。

    这些是**结构性估计**：真跑一轮后由 `observed_stats()` 校准 token，次数则由实际日志校准。
    """
    candidates = max(int(candidates or 0), 0)
    batch_size = max(int(batch_size or 25), 1)
    l1 = math.ceil(candidates / batch_size) if candidates else 0
    policy = max(int(n_sectors or 0), 0) * max(int(policy_sub_agents or 1), 1)
    deep = max(int(deep_top_n or 0), 0)
    decision = 1 if deep else 0
    attr = max(int(attribute_failures or 0), 0)
    breakdown = {"l1_batch_refine": l1, "policy": policy, "l2_deep": deep,
                 "l3_decision": decision, "verify_attribution": attr}
    return {"breakdown": breakdown, "total": sum(breakdown.values())}


def estimate_task(days: int = 1, candidates_per_day: int = 150, deep_top_n: int = 50,
                  n_sectors: int = 5, batch_size: int = 25, policy_sub_agents: int = 1,
                  attribute_failures: int = 20, prompt_tokens: float | None = None,
                  completion_tokens: float | None = None, cache_hit_ratio: float | None = None,
                  model: str = "", scan_seconds_per_day: float = 0.0,
                  latency_sec: float = DEFAULT_LATENCY_SEC) -> dict:
    """运行前预估（plans/25 §4.3）：调用次数 × 平均 token × 价格表 → 低/中/高三档 + 耗时。

    - `prompt_tokens/completion_tokens/cache_hit_ratio` 缺省时取近 30 天实测均值（`observed_stats`）；
      完全没有实测数据时用保守默认（输入 4000 / 输出 800 / 命中率 0），并在 warnings 里说明。
    - **峰谷分别报价**（官方空闲价 = 高峰价一半）：头条金额取**高峰价（保守上限）**，
      空闲时段金额单独给出 —— 这是"把自证排到 18:00 后/周末"的直接省钱杠杆。
    - 价格未配置 → `priced=False` + warning，**不猜价格**。
    """
    days = max(int(days or 0), 1)
    cpd = calls_per_day_estimate(candidates=candidates_per_day, deep_top_n=deep_top_n,
                                n_sectors=n_sectors, batch_size=batch_size,
                                policy_sub_agents=policy_sub_agents,
                                attribute_failures=attribute_failures)
    obs = observed_stats(days=30)
    warnings: list[str] = []
    if prompt_tokens is None:
        if obs.get("has_data"):
            prompt_tokens = float(obs["avg_prompt_tokens"] or 4000.0)
            warnings.append(f"单次 token 取近 30 天实测均值（样本 {obs['calls']} 次调用）")
        else:
            prompt_tokens = 4000.0
            warnings.append("无实测 token 数据 → 使用保守默认（输入 4000 / 输出 800 / 缓存命中率 0），"
                            "首轮跑完请用实测校准")
    if completion_tokens is None:
        completion_tokens = float(obs["avg_completion_tokens"] or 800.0) if obs.get("has_data") else 800.0
    if cache_hit_ratio is None:
        cache_hit_ratio = float(obs.get("cache_hit_ratio") or 0.0) if obs.get("has_data") else 0.0

    price_off = price_for(model, "off_peak")
    price_peak = price_for(model, "peak")
    priced = (price_off is not None) or (price_peak is not None)
    if not priced:
        warnings.append("价格表未填写（data/llm_pricing.json）→ 无法给出金额；"
                        "请查 DeepSeek 官方定价页填入单价并把 verified 置 true")
    # 思考模式（用户口径：deepseek-flash 就用思考模式）会让 token 明显变多 ——
    # 实测（2026-09-22，同一问题）：开启 prompt 61 / completion 37 / reasoning 31、¥0.000209；
    # 关闭 prompt 37 / completion 5 / reasoning 0、¥0.000057 ⇒ **约 3.7 倍**。
    if bool(settings.deepseek_thinking) and not obs.get("has_data"):
        warnings.append("当前启用思考模式（reasoning_tokens 计入输出、按输出价计费，实测同问题约为"
                        "关闭时的 3.7 倍）→ 下面的保守默认 token 可能偏低；首轮实跑后会用实测均值自动校准")
    # 时段（高峰/空闲）由**用户自行决定**：系统不做自动排期、不做高峰提示 ——
    # 只如实按实际时段计价，并在报价里同时给出两个数字（cost_by_period）供用户自己权衡。

    def _case(mult: float, price: dict | None) -> dict:
        calls = cpd["total"] * days * mult
        pt, ct = prompt_tokens * mult, completion_tokens * mult
        in_hit = pt * cache_hit_ratio
        in_miss = pt * (1.0 - cache_hit_ratio)
        cost = None
        if price is not None:
            cost = round(
                calls * in_hit / 1e6 * float(price["input_cache_hit_per_m"])
                + calls * in_miss / 1e6 * float(price["input_cache_miss_per_m"])
                + calls * ct / 1e6 * float(price["output_per_m"]), 4)
        return {
            "calls": int(round(calls)),
            "prompt_tokens": int(round(calls * pt)),
            "completion_tokens": int(round(calls * ct)),
            "cost_cny": cost,
        }

    by_period = {
        p: {"low": _case(0.7, pr), "mid": _case(1.0, pr), "high": _case(1.4, pr)}
        for p, pr in (("off_peak", price_off), ("peak", price_peak))
    }
    # 头条金额取**高峰价**（保守上限，绝不低估）；空闲时段金额单独给出（省钱杠杆）
    use = "peak" if price_peak is not None else "off_peak"
    low, mid, high = by_period[use]["low"], by_period[use]["mid"], by_period[use]["high"]
    elapsed_hours = round((mid["calls"] * latency_sec + days * max(scan_seconds_per_day, 0.0)) / 3600.0, 2)
    return {
        "days": days,
        "model": resolve_model(model),
        "calls_per_day": cpd,
        "calls_total": mid["calls"],
        "ranges": {"low": low, "mid": mid, "high": high},
        "cost_low_cny": low["cost_cny"], "cost_mid_cny": mid["cost_cny"], "cost_high_cny": high["cost_cny"],
        "cost_mid_off_peak_cny": by_period["off_peak"]["mid"]["cost_cny"],
        "cost_mid_peak_cny": by_period["peak"]["mid"]["cost_cny"],
        "cost_by_period": by_period,
        "headline_period": use,
        "period_now": current_period(),
        "estimated_hours": elapsed_hours,
        "assumptions": {
            "prompt_tokens_per_call": round(prompt_tokens, 1),
            "completion_tokens_per_call": round(completion_tokens, 1),
            "cache_hit_ratio": round(cache_hit_ratio, 4),
            "latency_sec_per_call": latency_sec,
            "candidates_per_day": candidates_per_day,
            "deep_top_n": deep_top_n,
            "range_multiplier": "low=0.7x / mid=1.0x / high=1.4x（次数与 token 同时缩放）",
            "headline_period": "peak（保守上限）；空闲时段金额见 cost_mid_off_peak_cny（时段由用户自选）",
            "thinking_mode": "enabled" if bool(settings.deepseek_thinking) else "disabled",
            "thinking_cost_note": "思考模式实测约 3.7×（同问题对比）；token 均值会按实测自动校准",
        },
        "priced": priced,
        "pricing": pricing_status(),
        "observed": obs,
        "budget": spend_snapshot(),
        "warnings": warnings,
    }


__all__ = [
    "BudgetExceeded", "record_usage", "summary", "observed_stats", "cost_of", "price_for",
    "pricing_status", "load_pricing", "save_pricing", "load_budget", "save_budget", "set_limits",
    "begin_task", "add_spend", "spend_snapshot", "check_budget", "ensure_budget",
    "budget_blocked", "budget_blocked_reason", "budget_blocked_brief",
    "clear_budget_block", "raise_if_blocked",
    "balance", "snapshot_balance", "snapshots", "reconcile",
    "calls_per_day_estimate", "estimate_task",
    "USAGE_FILE", "PRICING_FILE", "BUDGET_FILE", "BALANCE_FILE",
]
