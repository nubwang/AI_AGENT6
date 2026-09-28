"""LLM 客户端（llm_client）

提供两类外部能力：
  1. DeepSeek（openai 兼容 API）：所有 Agent 的语义判断/抽取/分级/决策
  2. Tavily 搜索：消息面验证（候选股近期新闻/公告/题材）

设计原则（对齐 plans/03-Agent系统.md）：
  - 超时/重试/JSON 解析降级：失败不抛致命异常，返回 None/空，由上层 Agent 降级处理
  - JSON 输出：优先 response_format json_object，失败时从文本提取 ```json 块
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter

from app.core.config import settings
from app.core.logger import logger

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"

_MAX_RETRY = 2
_TIMEOUT = 45

# ── Tavily 链路韧性 ─────────────────────────────────────────────
# 现象（2026-09-23 09:46 实际告警）：
#   `requests` 访问 api.tavily.com 偶发 `SSLError: SSLZeroReturnError`（TLS 连接被链路上
#   某处中断/重置），并非本项目代码问题——实测同一时刻 curl 直连可 200，说明是**间歇性**的。
# 原实现的两个毛病：
#   ① **零重试**：一次瞬时 TLS 中断 = 这只股票的消息面直接判空（防烟雾弹/防出货整体降级）；
#   ② **每次失败都 WARNING**：精筛逐只调用 → 同一句警告刷满日志，真问题被淹没。
# 处理三件事（**不改变上层契约**：失败仍返回 []，Agent 按"无消息面"降级）：
#   ① Session 复用连接池：TLS 握手是最容易被中断的一步，复用连接直接减少握手次数；
#   ② 指数退避重试：SSL/连接中断多为瞬时，重试常成功；429/5xx 同理；
#   ③ 连续失败熔断降温：连续 N 次失败后暂停 M 秒（期间只打 debug），避免对着已断链路
#      无效打点 + 日志刷屏。
# ★ 追加（用户口径 2026-09-23："必须使用 Tavily 得有消息面，不能失败直接略过了，得重试，
#   或者交给进化大脑修改代码错误"）：
#   ④ **跨轮重试**：tavily_rounds 轮 × tavily_max_attempts 次/轮（轮间 tavily_round_gap_s，
#      总时限 tavily_deadline_s）—— 瞬时 TLS 抖动常持续数秒，一轮打完就放弃太早；
#   ⑤ **欠账队列**：仍失败 → query 入 data/tavily_pending.json，`tavily_drain_pending()` 补做；
#   ⑥ **进化大脑工单**：同一条失败升级为 ERROR 事件（含复现命令 + 候选修复动作），
#      code_writer 闲时自进化读 `evolution_events.recent_errors()` 即可动手改代码。
_TAVILY_SESSION: requests.Session | None = None
_TAVILY_LOCK = threading.Lock()
_TAVILY_FAIL_STREAK = 0
_TAVILY_OPEN_UNTIL = 0.0
_TAVILY_LAST_ERR = ""  # 最近一次失败摘要（供 health/诊断一眼看到"到底为什么失败"）


def _tavily_session() -> requests.Session:
    """全局复用 Session（连接池）。

    `max_retries=0` 是刻意的：重试统一由 `tavily_search()` 的退避循环控制。
    urllib3 的自动重试**不覆盖 TLS 握手阶段的失败**，靠它会出现"设了重试却毫无作用"的错觉。
    """
    global _TAVILY_SESSION
    if _TAVILY_SESSION is None:
        with _TAVILY_LOCK:
            if _TAVILY_SESSION is None:
                s = requests.Session()
                adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _TAVILY_SESSION = s
    return _TAVILY_SESSION


def _tavily_base() -> str:
    """Tavily 根地址（支持自建网关/镜像，默认官方 api.tavily.com）。"""
    base = (settings.tavily_base_url or "").strip().rstrip("/")
    return base if base else TAVILY_URL.rsplit("/search", 1)[0]


def _tavily_endpoint() -> str:
    """Tavily 搜索端点（`/search`）。"""
    return f"{_tavily_base()}/search"


def _tavily_proxies() -> dict | None:
    """显式代理；留空返回 None → 交给 requests 的 trust_env（读 HTTPS_PROXY 等）。"""
    p = (settings.tavily_proxy or "").strip()
    return {"http": p, "https": p} if p else None


def _tavily_circuit() -> tuple[bool, int, float]:
    """返回 (熔断是否开启, 连续失败次数, 剩余降温秒数)。"""
    with _TAVILY_LOCK:
        remain = _TAVILY_OPEN_UNTIL - time.time()
        return remain > 0, _TAVILY_FAIL_STREAK, max(0.0, remain)


def _tavily_mark_ok() -> None:
    global _TAVILY_FAIL_STREAK, _TAVILY_OPEN_UNTIL, _TAVILY_LAST_ERR
    with _TAVILY_LOCK:
        _TAVILY_FAIL_STREAK = 0
        _TAVILY_OPEN_UNTIL = 0.0
        _TAVILY_LAST_ERR = ""


def _tavily_mark_fail(cooldown: float, threshold: int, err: Exception | None = None) -> tuple[int, bool]:
    """记一次失败；连续失败达阈值 → 开熔断。返回 (累计连续失败数, 是否已熔断)。"""
    global _TAVILY_FAIL_STREAK, _TAVILY_OPEN_UNTIL, _TAVILY_LAST_ERR
    with _TAVILY_LOCK:
        _TAVILY_FAIL_STREAK += 1
        if err is not None:
            _TAVILY_LAST_ERR = f"{type(err).__name__}: {err}"[:300]
        opened = _TAVILY_FAIL_STREAK >= threshold
        if opened:
            _TAVILY_OPEN_UNTIL = time.time() + cooldown
        return _TAVILY_FAIL_STREAK, opened


def tavily_reset_circuit() -> None:
    """手动复位熔断状态（诊断脚本/运维用）。"""
    _tavily_mark_ok()


def tavily_health() -> dict:
    """Tavily 链路健康度（**不发搜索请求**，供诊断脚本/接口展示）。"""
    opened, streak, remain = _tavily_circuit()
    return {
        "configured": bool(settings.tavily_api_key),
        "required": bool(settings.tavily_required),   # 消息面是否必需（失败会入欠账+开工单）
        "endpoint": _tavily_endpoint(),
        "via_proxy": bool(_tavily_proxies()),
        "circuit_open": opened,
        "fail_streak": streak,
        "cooldown_remain_s": round(remain, 1),
        "pending": tavily_pending_size(),             # 欠账条数（拿不到的消息面）
        "last_error": _TAVILY_LAST_ERR,
    }


def tavily_usage() -> dict:
    """查询 Tavily 账户配额（`GET /usage`，1 次请求，不计搜索额度）。

    为什么要有它：**"配额用尽"与"链路中断"是完全不同的两件事，但都被上层看成"消息面没有"**——
      超配额 → HTTP **432**（`plan limit exceeded`）；
      链路中断 → `SSLError: SSLZeroReturnError`（实测 2026-09-23 那批告警全是这种）。
    实际排查时曾被问"是不是免费次数用完了"，当时只能靠猜；现在 `used/limit/remain` 一眼可见。
    失败返回 `{"ok": False, "error": ...}`，**绝不抛异常、不影响主流程**。
    """
    if not settings.tavily_api_key:
        return {"ok": False, "error": "未配置 TAVILY_API_KEY"}
    try:
        r = _tavily_session().get(
            f"{_tavily_base()}/usage",
            # 该端点认 Bearer；body 里的 api_key 对它无效
            headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
            timeout=(min(8, int(settings.tavily_timeout or 30)), int(settings.tavily_timeout or 30)),
            proxies=_tavily_proxies(), verify=bool(settings.tavily_verify_ssl),
        )
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code, "error": (r.text or "")[:200]}
        d = r.json() or {}
        acct = d.get("account") or {}
        used = float(acct.get("plan_usage") or 0)
        limit = acct.get("plan_limit")
        return {
            "ok": True,
            "plan": acct.get("current_plan"),
            "used": used,
            "limit": float(limit) if limit else None,
            "remain": (float(limit) - used) if limit else None,
            "exhausted": bool(limit) and used >= float(limit),
            "raw": d,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _extract_json(text: str):
    """从 LLM 返回文本中尽力提取 JSON 对象/数组。失败返回 None。"""
    if not text:
        return None
    text = text.strip()
    # 去掉 markdown 代码围栏
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # 直接尝试
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    # 提取首个 { ... } 或 [ ... ]
    for start, end in (("{", "}"), ("[", "]")):
        i = text.find(start)
        j = text.rfind(end)
        if i != -1 and j > i:
            try:
                return json.loads(text[i : j + 1])
            except Exception:  # noqa: BLE001
                continue
    return None


def _metering():
    """延迟导入 LLM 计量模块（plans/25 §四）。

    延迟导入的原因（两条都成立）：
      1. `app.agents` 包初始化期 llm_client 与各 Agent 之间存在相互引用，模块级 import
         有循环导入风险；
      2. 计量属于"附属能力"，任何异常都不允许影响 LLM 主流程。
    失败返回 None（调用方跳过计量）。
    """
    try:
        from app.agents import llm_metering
        return llm_metering
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[llm] 计量模块不可用（费用将不被记录）: {exc}")
        return None


def deepseek_chat(
    messages: list[dict],
    *,
    json_mode: bool = True,
    temperature: float = 0.2,
    model: str | None = None,
    source: str = "llm",
    run_id: str = "",
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> dict | None:
    """调用 DeepSeek，返回解析后的 JSON（dict/list）。失败返回 None。

    Args:
        messages: OpenAI 风格消息 [{"role": "...", "content": "..."}]
        json_mode: 请求 JSON 输出（尽量让模型返回结构化结果）
        source: 调用来源标签（计量归因用，如 "selfproof/20260503/refine"）
        run_id: 自证任务号（计量归因 + 单任务预算闸门；日常调用留空）
        thinking: 思考模式开关。None → 取 `settings.deepseek_thinking`（默认 **False**）
        reasoning_effort: 思考强度 low/high/max（仅在 thinking=True 时有意义）

    预算闸门（plans/25 §4.4）：超预算 → **阻断本次调用并返回 None**（与既有"LLM 失败即
    降级"契约一致，不给主流程引入新的异常类型），同时由 `llm_metering.budget_blocked()`
    暴露标志，供自证内核检测到之后优雅停止与断点续跑。

    思考模式（plans/25 §14.3，官方文档 `/guides/thinking_mode`）：
      - 官方**默认开启**且 effort 默认 `high`；`reasoning_tokens` 计入 `completion_tokens`
        并按**输出价**计费（输出单价是输入未命中价的 4 倍）→ 不显式声明会让成本数倍膨胀；
      - 思考模式下 `temperature` / `presence_penalty` / `frequency_penalty` **不生效**；
      - 故本函数**始终显式写入** `thinking.type`（默认 disabled），开启时才带 `reasoning_effort`，
        并在开启时**不传 temperature**（避免"以为设了 0.2，其实被忽略"）。
    """
    if not settings.deepseek_api_key:
        logger.warning("[llm] 未配置 DEEPSEEK_API_KEY")
        return None
    _mtr = _metering()
    if _mtr is not None:
        try:
            if not _mtr.ensure_budget(run_id=run_id):
                logger.error(f"[llm] 预算熔断，跳过调用（source={source}）: {_mtr.budget_blocked_reason()}")
                return None
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[llm] 预算检查异常（忽略，不阻断）: {exc}")
    use_thinking = bool(settings.deepseek_thinking if thinking is None else thinking)
    effort = str(reasoning_effort or settings.deepseek_reasoning_effort or "low").strip().lower()
    payload = {
        "model": model or settings.deepseek_model,
        "messages": messages,
        "max_tokens": 4096,
        "stream": False,
        # 显式声明思考模式（官方默认开启 → 必须显式关闭才省成本）
        "thinking": {"type": "enabled" if use_thinking else "disabled"},
    }
    if use_thinking:
        payload["reasoning_effort"] = effort if effort in ("low", "high", "max") else "low"
        # 思考模式下 temperature 不生效（官方明确），故不传 —— 免得"看起来设了、其实没有"
    else:
        payload["temperature"] = temperature
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    last_err = None
    for attempt in range(_MAX_RETRY + 1):
        try:
            r = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=_TIMEOUT)
            if r.status_code == 429:
                time.sleep(2 + attempt)
                continue
            r.raise_for_status()
            data = r.json()
            # 先取消息体：计量要记录"实际是否产出思维链"，**必须在计量之前**取
            # （曾经把这行放在计量之后 → 引用未定义变量 → 被下面的 except 吞掉 → 计量静默失效）
            msg = data["choices"][0]["message"] or {}
            content = msg.get("content") or ""
            thinking_observed = bool(msg.get("reasoning_content"))
            # 计量（plans/25 §4.2）：`usage` 是唯一计费数据源，原先被丢弃 → 无法预估/熔断/对账。
            # ★ 用**响应体的 model** 而不是请求的 model 计费：实测请求 `deepseek-chat` 时服务端返回
            #   `"model":"deepseek-flash"`，若按请求名查价格表会查不到 → 费用被记成 None（漏计费）。
            # 计量失败不影响本次结果，但**必须 WARNING 级**并说清后果（漏计费）：
            # 用 debug 级 + 宽泛 except 曾让计量静默失效而无人察觉，这是踩过的坑。
            if _mtr is not None:
                try:
                    _mtr.record_usage(
                        source=source, model=str(data.get("model") or payload["model"]),
                        usage=data.get("usage"), run_id=run_id,
                        extra={"thinking_requested": "enabled" if use_thinking else "disabled",
                               "reasoning_content_present": thinking_observed},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[llm] usage 计量失败（本次结果不受影响，但会漏计费）: {exc}")
            parsed = _extract_json(content)
            if parsed is None and json_mode:
                # 模型没按 JSON 返回时，尝试宽松提取；仍失败记录原文便于排查
                logger.debug(f"[llm] JSON 解析失败，原始返回: {content[:200]}")
            return parsed
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < _MAX_RETRY:
                time.sleep(1 + attempt)
    logger.warning(f"[llm] DeepSeek 调用失败: {last_err}")
    return None


# ── 消息面"欠账"队列（消息面必需：拿不到就记账、稍后补做，绝不静默丢）──────────
# 用户口径（2026-09-23）："必须使用 Tavily 得有消息面，不能失败直接略过了"。
# 于是失败不再等于"这只股票没有消息面"，而是四件事：
#   ① 同一次调用内**跨轮重试**（tavily_rounds × tavily_max_attempts，见 tavily_search）；
#   ② 仍失败 → 把 query 记进本队列（欠账 data/tavily_pending.json），由
#      `tavily_drain_pending()` 在扫描收尾/闲时补做；
#   ③ 同一条失败升级为 **ERROR 工单**进事件流（`_tavily_ticket`）交给进化大脑：
#      `code_writer`（闲时自进化写代码）读的正是 `evolution_events.recent_errors()`；
#   ④ 熔断只是"降温"（默认 60s），期间失败的 query 照样进欠账，不会整天没有消息面。
DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data",
)
PENDING_FILE = os.path.join(DATA_DIR, "tavily_pending.json")
MAX_TASK_ATTEMPTS = 50        # 单条欠账最多挂多少次（防无限堆积）

_TAVILY_ISSUE_SIG = ""        # 上次工单签名（同签名在熔断窗口内只报一次，防刷事件流）
_TAVILY_ISSUE_TS = 0.0


def _pending_cap() -> int:
    return max(1, int(settings.tavily_pending_max or 200))


# ── 消息面缓存（让"补做回来的消息面"真的被用上，并省额度）──────────────────
# 为什么必须有：欠账补做若只是"把请求打出去"，拿到的结果没人用就白补了。
# 缓存让同一 query（行业政策、盘前兜底、同一只股票二次精筛）在 TTL 内直接命中。
CACHE_FILE = os.path.join(DATA_DIR, "tavily_cache.json")
CACHE_MAX = 500               # 最多保留多少条 query（超了丢最旧）


def _cache_ttl() -> float:
    return max(0.0, float(settings.tavily_cache_ttl_s or 0))


def _cache_load() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[llm] 读消息面缓存失败（按空处理）: {exc}")
        return {}


def _cache_key(query: str, max_results: int) -> str:
    """缓存键含 max_results：否则"要 1 条的调用"会命中"要 5 条"的缓存，条数对不上。"""
    return f"{query}||{int(max_results)}"


def _cache_get(cache_key: str) -> list[dict] | None:
    """命中且未过期 → 返回结果副本；否则 None。"""
    ttl = _cache_ttl()
    if ttl <= 0:
        return None
    with _TAVILY_LOCK:
        row = _cache_load().get(cache_key)
    if not isinstance(row, dict) or not row.get("results"):
        return None
    if time.time() - float(row.get("ts") or 0) > ttl:
        return None
    return [dict(r) for r in row["results"]]


def _cache_put(cache_key: str, results: list[dict]) -> None:
    if _cache_ttl() <= 0 or not results:
        return
    try:
        with _TAVILY_LOCK:
            data = _cache_load()
            data[cache_key] = {"ts": time.time(), "results": results}
            if len(data) > CACHE_MAX:      # 丢最旧：按 ts 排序后截断
                keep = sorted(data.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0))
                data = dict(keep[-CACHE_MAX:])
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = f"{CACHE_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, CACHE_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[llm] 写消息面缓存失败（不影响主流程）: {exc}")


def _pending_load() -> list[dict]:
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[llm] 读欠账队列失败（按空处理）: {exc}")
        return []


def _pending_save(items: list[dict]) -> None:
    """原子写（临时文件 + replace）：避免进程被杀时留下半截 JSON。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = f"{PENDING_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items[:_pending_cap()], f, ensure_ascii=False, indent=1)
        os.replace(tmp, PENDING_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[llm] 写欠账队列失败（补做会丢，但不影响主流程）: {exc}")


def _pending_add(query: str, kind: str, key: str, max_results: int,
                 search_depth: str, err: Exception | None) -> int:
    """记一条欠账（同 query 去重，只累加 attempts）。返回当前欠账条数。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    err_s = f"{type(err).__name__}: {err}"[:200] if err is not None else ""
    with _TAVILY_LOCK:
        items = _pending_load()
        for it in items:
            if it.get("query") == query:
                it["attempts"] = min(MAX_TASK_ATTEMPTS, int(it.get("attempts") or 0) + 1)
                it["last_try"], it["last_err"] = now, err_s
                _pending_save(items)
                return len(items)
        items.append({"query": query, "kind": kind or "news", "key": key or "",
                      "max_results": max_results, "search_depth": search_depth,
                      "attempts": 1, "first_seen": now, "last_try": now, "last_err": err_s})
        _pending_save(items)
        return len(items)


def _pending_remove(query: str) -> None:
    with _TAVILY_LOCK:
        _pending_save([it for it in _pending_load() if it.get("query") != query])


def tavily_pending_size() -> int:
    """当前欠账条数（"拿不到消息面的 query"有多少条在排队）。"""
    return len(_pending_load())


def tavily_pending_tasks() -> list[dict]:
    """欠账明细（供扫描收尾/闲时任务/进化大脑查看哪些消息面一直没拿到）。"""
    return _pending_load()


def tavily_drain_pending(limit: int = 5) -> dict:
    """补做欠账：重试队列里的 query，成功即出队。返回 {"tried","ok","left"}。

    建议调用点：每日扫描收尾、闲时进化任务（消息面必需 → 欠账要被主动还清）。
    补做**再失败**无需本函数处理：`tavily_search` 会把该 query 重新入队（同 query 去重累加）。
    """
    tasks = _pending_load()[: max(1, int(limit))]
    ok = 0
    for it in tasks:
        q = str(it.get("query") or "").strip()
        if not q:
            _pending_remove(q)
            continue
        out = tavily_search(q, max_results=int(it.get("max_results") or 5),
                            search_depth=str(it.get("search_depth") or "basic"),
                            kind=str(it.get("kind") or "news"), key=str(it.get("key") or ""))
        if out:
            ok += 1
            _pending_remove(q)
    return {"tried": len(tasks), "ok": ok, "left": len(_pending_load())}


def _tavily_fix_hints(err: Exception) -> str:
    """按失败类型给出"候选修复动作"，让进化大脑能直接改代码/配置而不是干瞪眼。"""
    s = str(err)
    if "432" in s:
        return ("① 套餐额度用尽：升级 Tavily 套餐或等下个计费周期（tavily_usage() 看 used/limit）"
                "② 压降调用量（max_results / 减少 query）③ 增加备用消息源回退")
    if "401" in s or "403" in s:
        return "① 检查 .env 的 TAVILY_API_KEY 是否失效/被轮换 ② 确认账号未欠费封禁"
    if isinstance(err, (requests.exceptions.SSLError, requests.exceptions.ConnectionError)) \
            or "SSL" in s:
        return ("① .env 设 TAVILY_PROXY=http://127.0.0.1:7890 走更稳出口 ② 调大 tavily_rounds / "
                "tavily_deadline_s 加强跨轮重试 ③ 检查代理/防火墙/DNS 与 IPv6 路由")
    if isinstance(err, requests.exceptions.Timeout):
        return "① 调大 tavily_timeout ② 调大 tavily_rounds（跨轮重试）③ 排查出口带宽/限速"
    if any(c in s for c in ("500", "502", "503", "504")):
        return "① 服务端故障，跨轮重试已覆盖，必要时调大 tavily_rounds ② 关注 Tavily 状态页"
    return "① 复核 tavily_* 参数 ② 用 scripts/verify_tavily_resilience.py --live 复现后定位"


def _tavily_ticket(err: Exception, tried: int, streak: int, opened: bool,
                   pending_n: int, query: str) -> None:
    """把"消息面拿不到"升级成**进化大脑可消费的 ERROR 工单**（plans/18、20 闲时自进化）。

    为什么写事件流而不只是写日志：`code_writer` 修 bug 时读的是
    `evolution_events.recent_errors(hours, limit)`；只写日志等于等人去翻文件，
    而带上"复现命令 + 候选修复动作"，进化大脑才能自己改代码/配置把它修掉。
    同一签名在熔断窗口内只报一次，避免事件流被同一句话淹掉。
    """
    global _TAVILY_ISSUE_SIG, _TAVILY_ISSUE_TS
    sig = f"{type(err).__name__}:{str(err)[:80]}"
    now = time.time()
    with _TAVILY_LOCK:
        if sig == _TAVILY_ISSUE_SIG and \
                now - _TAVILY_ISSUE_TS < float(settings.tavily_circuit_cooldown or 60):
            return
        _TAVILY_ISSUE_SIG, _TAVILY_ISSUE_TS = sig, now
    try:
        from app.agents import evolution_events
        evolution_events.record_event(
            "ERROR", "llm_tavily",
            f"消息面（Tavily）拿不到：query={query[:60]!r} 本次已请求 {tried} 次"
            f"（连续失败 {streak} 次{'，已熔断降温' if opened else ''}），欠账 {pending_n} 条；"
            f"错误={type(err).__name__}: {str(err)[:160]}；"
            f"复现=cd backend && ./venv/bin/python scripts/verify_tavily_resilience.py --live；"
            f"候选修复：{_tavily_fix_hints(err)}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[llm] 失败工单上报事件流失败（不影响主流程）: {exc}")


def _tavily_parse(data: dict) -> list[dict]:
    """把 Tavily 原始响应转成统一结构（answer → 综合摘要 + results）。"""
    results: list[dict] = []
    answer = data.get("answer")
    if answer:
        results.append({"title": "综合摘要", "url": "", "content": answer, "published_date": ""})
    for item in data.get("results", []) or []:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "content": item.get("content", ""),
            "published_date": item.get("published_date", ""),
        })
    return results


def tavily_search(query: str, max_results: int = 5, search_depth: str = "basic",
                  *, kind: str = "news", key: str = "") -> list[dict]:
    """Tavily 实时搜索，返回结构化新闻/网页结果列表。**不抛异常**。

    ★ 消息面**必需**（`tavily_required`，默认开）—— 用户口径 2026-09-23：
      "必须使用 Tavily 得有消息面，不能失败直接略过了，得重试，或者交给进化大脑修改代码错误"。
      因此失败**不等于**"这只股票没有消息面"，而是：

        ① **跨轮重试**：`tavily_rounds` 轮 × `tavily_max_attempts` 次/轮（轮间 `tavily_round_gap_s`，
           总时限 `tavily_deadline_s`）—— 瞬时 TLS 抖动常持续数秒，一轮打完就放弃太早
           （2026-09-23 那批 `SSLZeroReturnError` 就是这种）；
        ② **欠账队列**：仍拿不到 → query 入 `data/tavily_pending.json`（`tavily_pending_tasks()`），
           稍后由 `tavily_drain_pending()` 补做，绝不静默当作"没有消息面"；
        ③ **进化大脑工单**：同一条失败升级为 ERROR 事件（含复现命令 + 候选修复动作），
           `code_writer` 闲时自进化读 `evolution_events.recent_errors()` 就能动手改代码/配置；
        ④ 只有 4xx（401 key 失效 / 432 配额用尽 / 400 参数错）**不重试不跨轮**——重试无意义；
           熔断也只是"降温"（默认 60s），期间失败的 query 照样进欠账。

    参数：
        kind/key: 记录"这条 query 是给谁问的"（如 kind="stock_news", key="000001.SZ"），
                  便于补做时定位、也便于进化大脑归因。
    """
    if not settings.tavily_api_key:
        logger.debug("[llm] 未配置 TAVILY_API_KEY，消息面验证跳过")
        return []
    cached = _cache_get(_cache_key(query, max_results))
    if cached is not None:
        # 缓存也让"补做回来的消息面"被真正复用（ТTL 内同 query 不再打外部请求）
        logger.debug(f"[llm] 消息面命中缓存（TTL {_cache_ttl():.0f}s）: {query[:40]!r}")
        return cached
    required = bool(settings.tavily_required)
    opened, streak, remain = _tavily_circuit()
    timeout = max(1, int(settings.tavily_timeout or 30))
    attempts = max(1, int(settings.tavily_max_attempts or 3))
    rounds = max(1, int(settings.tavily_rounds or 3))
    round_gap = max(0.0, float(settings.tavily_round_gap_s or 5.0))
    deadline_s = max(0.0, float(settings.tavily_deadline_s or 60))
    threshold = max(1, int(settings.tavily_fail_threshold or 3))
    cooldown = float(settings.tavily_circuit_cooldown or 60)
    if opened:
        # 熔断只降温、不放弃：把 query 记进欠账，稍后补做（绝不当成"没有消息面"）
        n = _pending_add(query, kind, key, max_results, search_depth, None) if required else 0
        logger.debug(f"[llm] Tavily 熔断降温中（连续失败 {streak} 次，{remain:.0f}s 后自动恢复）；"
                     f"本次 query 已记入欠账（当前 {n} 条），稍后自动补做")
        return []
    payload = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": True,
    }
    deadline = time.time() + deadline_s
    last_err: Exception | None = None
    tried = 0
    for rnd in range(rounds):
        fatal = False
        for attempt in range(attempts):
            tried += 1
            try:
                r = _tavily_session().post(
                    # (连接, 读取) 分开设超时：连接阶段卡住就快速失败交给退避重试，
                    # 读取阶段给足 tavily_timeout（实测正常 2~3s，曾观测到握手成功但响应超时）
                    _tavily_endpoint(), json=payload, timeout=(min(8, timeout), timeout),
                    proxies=_tavily_proxies(), verify=bool(settings.tavily_verify_ssl),
                )
                if r.status_code >= 400:
                    # **必须带上响应体**：Tavily 会在 body 里说明原因（key 无效/超配额/参数错），
                    # 只留一句 "401" 会让人无从下手（排查时真的被这一点卡住过）
                    body = (r.text or "").strip().replace("\n", " ")[:200]
                    last_err = requests.exceptions.HTTPError(f"HTTP {r.status_code}: {body}")
                    if r.status_code != 429 and r.status_code < 500:
                        # 401/403/432/400 等：退避重试与跨轮重试都无意义 → 立刻失败退出
                        fatal = True
                        break
                else:
                    data = r.json()
                    _tavily_mark_ok()
                    if streak:  # 从"连续失败"恢复：留痕一条 info，便于确认自愈
                        logger.info(f"[llm] Tavily 已恢复（此前连续失败 {streak} 次）")
                    parsed_out = _tavily_parse(data)
                    # 落缓存：TTL 内同 query+max_results 直接命中（省额度，也让补做结果被复用）
                    _cache_put(_cache_key(query, max_results), parsed_out)
                    return parsed_out
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            backoff = min(0.6 * (3 ** attempt), 8.0)
            if attempt < attempts - 1 and time.time() + backoff < deadline:
                time.sleep(backoff)
        if fatal or rnd >= rounds - 1 or time.time() + round_gap >= deadline:
            break
        time.sleep(round_gap)      # 跨轮：给瞬时抖动几秒恢复时间再打下一轮
    streak, opened = _tavily_mark_fail(cooldown, threshold, last_err)
    pending_n = _pending_add(query, kind, key, max_results, search_depth, last_err) if required else 0
    hint = ""
    if isinstance(last_err, (requests.exceptions.SSLError, requests.exceptions.ConnectionError)) \
            or "SSL" in str(last_err):
        hint = "｜疑似 TLS/网络中断（SSLZeroReturnError 常见于链路重置，多为瞬时）"
    elif "432" in str(last_err):
        # 432 = 配额用尽：**不是链路问题，重试无用**，必须点破，否则会被误诊为网络故障
        hint = ("｜Tavily 套餐额度已用尽（HTTP 432）：`tavily_usage()` 看 used/limit，"
                "需升级套餐或等下个计费周期")
    elif "401" in str(last_err) or "403" in str(last_err):
        hint = "｜API key 失效/无权限，需检查 .env 的 TAVILY_API_KEY"
    msg = (f"[llm] Tavily 搜索失败（连续第 {streak} 次，本次共请求 {tried} 次）: "
           f"{type(last_err).__name__}: {last_err}{hint}")
    if required:
        msg += f"｜已记入欠账（当前 {pending_n} 条，可 tavily_drain_pending() 补做）"
    else:
        msg += "｜消息面降级为「无消息面」（tavily_required=False）"
    if opened:
        msg += f"｜已熔断 {cooldown:.0f}s 降温，期间失败的 query 仍会记入欠账"
    logger.warning(msg)
    if required and last_err is not None:
        # 交给进化大脑：ERROR 事件 + 复现命令 + 候选修复动作（去重，防刷事件流）
        _tavily_ticket(last_err, tried, streak, opened, pending_n, query)
    return []
