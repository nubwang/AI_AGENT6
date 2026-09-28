"""Tavily 消息面必达验证（跨轮重试 / 欠账补做 / 进化大脑工单 / 配额可查）

## 为什么需要这个脚本

2026-09-23 09:46 的告警：
    [WARNING] [llm] Tavily 搜索失败: HTTPSConnectionPool(host='api.tavily.com', port=443):
    Max retries exceeded ... (Caused by SSLError(SSLZeroReturnError(6, 'TLS/SSL connection
    has been closed (EOF)')))

这不是代码 bug，而是**直连链路偶发被中断**（同一时刻 curl 可 200）。但原实现有四个硬伤，
前三个是"消息面被静默弄丢"，第四个是"故障不可诊断"：
  ① 零重试 → 一次瞬时中断就让这只股票的"消息面"判空（防烟雾弹/防利好出货整体降级）；
  ② 每次失败都 WARNING → 精筛逐只调用，同一句警告刷满日志；
  ③ **失败即静默略过**，没有欠账、没有补做、进化大脑也不知道（用户 2026-09-23 明确否决：
     "必须使用 Tavily 得有消息面，不能失败直接略过了，得重试，或者交给进化大脑修改代码错误"）；
  ④ "配额用尽(HTTP 432)"与"链路中断(SSLError)"不可区分 → 被问"是不是免费次数用完了"只能靠猜。

## 验什么（每条都有断言，任一失败 → 非零退出；离线全 mock，不消耗任何额度）

  ① 瞬时 TLS 中断 → 自动重试成功；成功后失败计数与熔断清零
  ② 跨轮重试耗尽 → 返回 []（上层契约不变）+ 连续失败计数 +1
  ③ 连续失败达阈值 → 熔断，**后续调用不再发起请求**，且调用方 query 记入欠账
  ④ 冷却走完 → 自动半开恢复
  ⑤ 429 退避重试可成功；401 **不重试**（不做跨轮），日志带服务端响应体
  ⑥ 未配置 TAVILY_API_KEY → 零请求直接返回 []
  ⑦ 代理与自定义端点确实传到 HTTP 层（TAVILY_PROXY / TAVILY_BASE_URL）
  ⑧ 响应解析边界（answer→综合摘要、results=None→[]）
  ⑨ 配额可查：432 不重试、日志点破"额度已用尽"；`tavily_usage()` 解析 used/limit/remain/exhausted
  ⑩ **消息面必达**（用户口径的核心）：
     - 跨轮重试能把"打 8 次都断"的链路救回来（tavily_rounds=3 × 3 次 = 9 次）；
     - 仍失败 → query 入欠账队列（字段可定位：query/kind/key/attempts/last_err）；
     - 同一条失败升级为 **ERROR 工单**进事件流，含复现命令 + 候选修复动作（进化大脑能直接改代码）；
     - 同一签名在熔断窗口内只报一次（不刷事件流）；
     - `tavily_drain_pending()` 补做成功后出队；
     - `tavily_required=False` 时才退回"静默降级"（开关语义有效）。

## 隔离说明（重要）

本脚本**不会**污染真实资产：`PENDING_FILE` 重定向到临时目录，
`evolution_events.record_event` 被替换成内存收集器（不写真实事件库）。

## 跑法

    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_tavily_resilience.py
    # 附加真实链路探测（1 次 search + 1 次 usage；usage 不计搜索额度）：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_tavily_resilience.py --live
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

import app.agents.evolution_events as EV  # noqa: E402
import app.agents.llm_client as LC  # noqa: E402

FAILS: list[str] = []
TICKETS: list[tuple] = []          # 捕获的"进化大脑工单"（不写真实事件库）


def ck(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label} {detail}")
        FAILS.append(label)


class FakeClock:
    """虚拟时钟：让退避/跨轮等待/熔断降温**瞬间走完**，并记录"到底睡了多少秒"。"""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start
        self.slept = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        seconds = float(seconds)
        self.slept += seconds
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def strftime(self, fmt: str, *args) -> str:
        """真实 time 模块有 strftime——被测代码用它给欠账打时间戳，
        所以 fake 也必须提供（漏了就在 ② 场景直接崩，测试当场发现，这是好事）。"""
        import time as _real
        return _real.strftime(fmt, *args) if args else _real.strftime(fmt)


class FakeResp:
    def __init__(self, status: int = 200, payload: dict | None = None, err: Exception | None = None,
                 text: str | None = None):
        self.status_code = status
        self._payload = payload or {}
        self._err = err
        # 真实响应一定有 .text —— 实现层要靠它把"401/432 的真实原因"带进日志，
        # 所以 fake 也必须提供（漏了这个属性会让测试掩盖真实行为，曾经踩过）
        self.text = text if text is not None else json.dumps(self._payload, ensure_ascii=False)

    def raise_for_status(self) -> None:
        if self._err is not None:
            raise self._err
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """按脚本返回响应/抛异常，并记录每一次请求（含 url 与 kwargs）。"""

    def __init__(self, script: list | None = None):
        self.calls: list[tuple] = []
        self.gets: list[tuple] = []
        self.script: list = list(script or [])
        self.get_script: list = []

    def push(self, *items) -> None:
        self.script.extend(items)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.script:
            return FakeResp(200, {"results": []})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if not self.get_script:
            return FakeResp(200, {})
        item = self.get_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class LogCapture(logging.Handler):
    """截获日志，用来断言"失败原因被说清楚了"（诊断性也是被测契约的一部分）。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


def capture() -> LogCapture:
    cap = LogCapture()
    LC.logger.addHandler(cap)
    return cap


def uncapture(cap: LogCapture) -> None:
    LC.logger.removeHandler(cap)


def ssl_err() -> requests.exceptions.SSLError:
    """复刻线上那句异常（SSLZeroReturnError 在 requests 里就是 SSLError）。"""
    return requests.exceptions.SSLError(
        "HTTPSConnectionPool(host='api.tavily.com', port=443): Max retries exceeded "
        "(Caused by SSLError(SSLZeroReturnError(6, 'TLS/SSL connection has been closed (EOF)')))"
    )


def set_cfg(**kw) -> None:
    for k, v in kw.items():
        setattr(LC.settings, k, v)


def reset(session: FakeSession, clock: FakeClock) -> None:
    """每个场景前把会话/时钟/熔断状态清干净（熔断与欠账都是模块级全局状态）。"""
    LC._TAVILY_SESSION = session
    LC.time = clock
    LC.tavily_reset_circuit()
    TICKETS.clear()
    for f in (LC.PENDING_FILE, LC.CACHE_FILE):
        try:
            os.remove(f)
        except OSError:
            pass


def sc_retry_then_ok(clock: FakeClock) -> None:
    print("\n① 瞬时 TLS 中断 → 重试成功")
    sess = FakeSession([ssl_err(), FakeResp(200, {
        "answer": "政策摘要",
        "results": [{"title": "T1", "url": "http://a", "content": "c1", "published_date": "2026-09-20"}],
    })])
    set_cfg(tavily_api_key="k", tavily_required=True, tavily_max_attempts=3, tavily_rounds=1,
            tavily_fail_threshold=3, tavily_circuit_cooldown=600, tavily_proxy="",
            tavily_base_url="", tavily_verify_ssl=True)
    reset(sess, clock)
    out = LC.tavily_search("测试查询")
    ck(len(sess.calls) == 2, "SSLError 后自动重试（共 2 次请求）", f"实际 {len(sess.calls)}")
    ck(clock.slept > 0, "重试前有退避等待", f"slept={clock.slept}")
    ck(len(out) == 2, "返回 2 条（综合摘要 + 1 条结果）", f"实际 {len(out)}")
    ck(out[0]["title"] == "综合摘要" and out[0]["content"] == "政策摘要", "answer 归入「综合摘要」")
    ck(out[1]["published_date"] == "2026-09-20", "results 的 published_date 正确映射")
    h = LC.tavily_health()
    ck(h["fail_streak"] == 0 and not h["circuit_open"], "成功后失败计数与熔断均清零", str(h))
    ck(h["pending"] == 0, "成功路径不产生欠账", str(h["pending"]))


def sc_fail_contract(clock: FakeClock) -> None:
    print("\n② 一轮重试耗尽 → 返回 []（契约不变）+ 失败计数")
    sess = FakeSession([ssl_err(), ssl_err(), ssl_err()])
    set_cfg(tavily_max_attempts=3, tavily_rounds=1, tavily_fail_threshold=3,
            tavily_circuit_cooldown=600)
    reset(sess, clock)
    out = LC.tavily_search("测试查询")
    ck(out == [], "全部失败时返回空列表（不抛异常）")
    ck(len(sess.calls) == 3, "单轮请求次数 = tavily_max_attempts", f"实际 {len(sess.calls)}")
    h = LC.tavily_health()
    ck(h["fail_streak"] == 1 and not h["circuit_open"], "第 1 次失败：计数 +1、未熔断", str(h))


def sc_circuit_opens(clock: FakeClock) -> None:
    print("\n③ 连续失败达阈值 → 熔断；期间不发请求但 query 仍进欠账")
    sess = FakeSession([ssl_err(), ssl_err(), ssl_err()])
    set_cfg(tavily_max_attempts=1, tavily_rounds=1, tavily_fail_threshold=3,
            tavily_circuit_cooldown=600)
    reset(sess, clock)
    LC.tavily_search("第1次")
    LC.tavily_search("第2次")
    LC.tavily_search("第3次")           # 连到阈值 → 开熔断
    h = LC.tavily_health()
    ck(h["circuit_open"] and h["fail_streak"] == 3, "连续 3 次失败后熔断开启", str(h))
    before = len(sess.calls)
    out = LC.tavily_search("熔断期内")
    ck(out == [] and len(sess.calls) == before,
       "熔断期内不再发起任何 HTTP 请求（日志刷屏的根因被消除）",
       f"before={before} after={len(sess.calls)}")
    ck(LC.tavily_health()["pending"] >= 1, "熔断期内失败的 query 仍记入欠账（不静默丢）")
    ck(LC.tavily_health()["cooldown_remain_s"] > 0, "健康度能报告剩余降温秒数")


def sc_half_open_recover(clock: FakeClock) -> None:
    print("\n④ 冷却走完 → 自动半开恢复")
    print("   （承上：熔断仍开启，推进虚拟时钟越过冷却）")
    clock.advance(600)                  # 冷却自然走完
    sess: FakeSession = LC._TAVILY_SESSION  # type: ignore[assignment]
    sess.push(FakeResp(200, {"results": [{"title": "T", "url": "u", "content": "c"}]}))
    out = LC.tavily_search("冷却后")
    ck(len(out) == 1, "冷却结束后自动恢复请求并拿到结果", f"实际 {len(out)}")
    h = LC.tavily_health()
    ck(not h["circuit_open"] and h["fail_streak"] == 0, "恢复后熔断关闭、计数清零", str(h))


def sc_http_status(clock: FakeClock) -> None:
    print("\n⑤ 429 重试 / 401 不重试")
    sess = FakeSession([FakeResp(429), FakeResp(200, {"results": [{"title": "T"}]})])
    set_cfg(tavily_max_attempts=3, tavily_rounds=1, tavily_fail_threshold=3,
            tavily_circuit_cooldown=600)
    reset(sess, clock)
    out = LC.tavily_search("限流后恢复")
    ck(len(sess.calls) == 2 and len(out) == 1, "429 → 退避重试后成功", f"calls={len(sess.calls)}")
    # 401 即使配了 3 轮也不该跨轮（重试无意义）
    sess2 = FakeSession([FakeResp(401, text='{"detail":"Invalid API key"}')])
    set_cfg(tavily_rounds=3)
    reset(sess2, clock)
    out2 = LC.tavily_search("key 无效")
    ck(out2 == [] and len(sess2.calls) == 1, "401 → 只请求 1 次（不重试、不跨轮）",
       f"calls={len(sess2.calls)}")
    last = LC.tavily_health()["last_error"]
    ck("401" in last and "Invalid API key" in last,
       "失败原因带上服务端响应体（可诊断，不用靠猜）", last)
    ck(any("401" in (t[2] or "") for t in TICKETS), "401 也生成了进化大脑工单（含 key 修复建议）")
    LC.tavily_reset_circuit()
    set_cfg(tavily_rounds=1)


def sc_no_key(clock: FakeClock) -> None:
    print("\n⑥ 未配置 key → 零请求")
    sess = FakeSession([FakeResp(200, {"results": [{"title": "T"}]})])
    set_cfg(tavily_api_key="")
    reset(sess, clock)
    out = LC.tavily_search("无 key")
    ck(out == [] and len(sess.calls) == 0, "未配置 TAVILY_API_KEY 时直接返回 [] 且不发请求",
       f"calls={len(sess.calls)}")


def sc_proxy_endpoint(clock: FakeClock) -> None:
    print("\n⑦ 代理 / 自定义端点确实传到 HTTP 层")
    sess = FakeSession([FakeResp(200, {"results": []})])
    set_cfg(tavily_api_key="k", tavily_proxy="http://127.0.0.1:7890",
            tavily_base_url="https://gw.example.com/", tavily_verify_ssl=True)
    reset(sess, clock)
    LC.tavily_search("走代理")
    url, kw = sess.calls[-1]
    ck(url == "https://gw.example.com/search", "TAVILY_BASE_URL 生效", url)
    ck(kw.get("proxies") == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
       "TAVILY_PROXY 生效", str(kw.get("proxies")))
    ck(kw.get("verify") is True, "默认校验证书")
    h = LC.tavily_health()
    ck(h["via_proxy"] and h["endpoint"].endswith("/search") and h["required"] is True,
       "健康度反映代理/端点/required", str(h))
    set_cfg(tavily_proxy="", tavily_base_url="")


def sc_parse(clock: FakeClock) -> None:
    print("\n⑧ 响应解析边界")
    sess = FakeSession([FakeResp(200, {}), FakeResp(200, {"results": None})])
    set_cfg(tavily_api_key="k", tavily_max_attempts=2, tavily_rounds=1)
    reset(sess, clock)
    ck(LC.tavily_search("空响应") == [], "空响应 → []")
    ck(LC.tavily_search("results=None") == [], "results=None → []（不抛异常）")


def sc_quota(clock: FakeClock) -> None:
    print("\n⑨ 配额可查：432 ≠ 网络故障")
    # ── ① 432 = 额度用尽：必须**不重试**且日志点破，否则会被误诊成 TLS/网络问题 ──
    sess = FakeSession([FakeResp(432, text='{"detail":"Your plan\'s monthly usage limit has been reached"}')])
    set_cfg(tavily_api_key="k", tavily_max_attempts=3, tavily_rounds=3,
            tavily_fail_threshold=3, tavily_circuit_cooldown=600)
    reset(sess, clock)
    cap = capture()
    try:
        out = LC.tavily_search("配额用尽")
    finally:
        uncapture(cap)
    ck(out == [] and len(sess.calls) == 1, "432（配额用尽）不重试、不跨轮、返回 []",
       f"calls={len(sess.calls)}")
    ck("432" in LC.tavily_health()["last_error"], "last_error 记录 432 与响应体原文")
    msgs = cap.messages()
    ck("额度已用尽" in msgs and "432" in msgs,
       "日志明确点破「额度用尽」，不会被误诊成网络故障", msgs[-200:])
    LC.tavily_reset_circuit()

    # ── ② /usage 解析（未用尽）──
    sess2 = FakeSession()
    sess2.get_script = [FakeResp(200, {"account": {
        "current_plan": "Researcher", "plan_usage": 570, "plan_limit": 1000,
    }})]
    set_cfg(tavily_api_key="k")
    LC._TAVILY_SESSION = sess2
    u = LC.tavily_usage()
    ck(bool(u.get("ok")) and u["used"] == 570 and u["limit"] == 1000 and u["remain"] == 430,
       "tavily_usage() 解析 used/limit/remain",
       str({k: u.get(k) for k in ("plan", "used", "limit", "remain")}))
    ck(u["exhausted"] is False, "未用尽时 exhausted=False")
    url, kw = sess2.gets[-1]
    ck(url.endswith("/usage") and str(kw["headers"]["Authorization"]).startswith("Bearer "),
       "/usage 路径与 Bearer 认证正确", f"{url} {kw.get('headers')}")

    # ── ③ 用尽判定（供告警使用）──
    sess2.get_script.append(FakeResp(200, {"account": {
        "current_plan": "Researcher", "plan_usage": 1000, "plan_limit": 1000,
    }}))
    u2 = LC.tavily_usage()
    ck(u2["exhausted"] and u2["remain"] == 0, "用尽时 exhausted=True（可据此提前告警）", str(u2.get("remain")))

    # ── ④ 未配置 key / 异常 → 返回 ok=False，不抛异常 ──
    set_cfg(tavily_api_key="")
    ck(LC.tavily_usage().get("ok") is False, "未配置 key 时返回 ok=False（不抛异常）")
    set_cfg(tavily_api_key="k")
    sess2.get_script.append(ssl_err())
    ck(LC.tavily_usage().get("ok") is False, "usage 查询异常时返回 ok=False（不拖垮调用方）")


def sc_required_no_silent_skip(clock: FakeClock) -> None:
    print("\n⑩ 消息面必达：跨轮重试 / 欠账补做 / 进化大脑工单")
    # ── ① 跨轮重试把"前 8 次都断"的链路救回来（3 轮 × 3 次 = 第 9 次成功）──
    sess = FakeSession([ssl_err()] * 8 + [FakeResp(200, {"results": [{"title": "救回来了"}]})])
    set_cfg(tavily_api_key="k", tavily_required=True, tavily_max_attempts=3, tavily_rounds=3,
            tavily_round_gap_s=5.0, tavily_deadline_s=60, tavily_fail_threshold=3,
            tavily_circuit_cooldown=600)
    reset(sess, clock)
    out = LC.tavily_search("跨轮救回", kind="stock_news", key="000001.SZ")
    ck(len(sess.calls) == 9 and len(out) == 1,
       "跨轮重试：第 9 次拿到结果（3 轮 × 3 次）", f"calls={len(sess.calls)} out={len(out)}")
    ck(LC.tavily_health()["pending"] == 0, "成功路径不留欠账")
    ck(not TICKETS, "成功路径不产生进化大脑工单")

    # ── ② 全部失败 → 欠账（可定位字段）+ 工单（复现命令 + 候选修复）──
    sess2 = FakeSession([ssl_err()] * 4)
    set_cfg(tavily_max_attempts=2, tavily_rounds=2)
    reset(sess2, clock)
    out2 = LC.tavily_search("拿不到消息面", kind="sector_policy", key="半导体")
    ck(out2 == [] and len(sess2.calls) == 4, "两轮共 4 次请求后仍失败 → []（契约不变）",
       f"calls={len(sess2.calls)}")
    tasks = LC.tavily_pending_tasks()
    ck(len(tasks) == 1, "失败 query 进入欠账队列（不再静默略过）", str(len(tasks)))
    t = tasks[0] if tasks else {}
    ck(t.get("query") == "拿不到消息面" and t.get("kind") == "sector_policy"
       and t.get("key") == "半导体" and int(t.get("attempts") or 0) == 1,
       "欠账字段可定位（query/kind/key/attempts）", str(t))
    ck("SSLError" in str(t.get("last_err")), "欠账记录了失败原因", str(t.get("last_err"))[:80])
    ck(len(TICKETS) == 1 and TICKETS[0][0] == "ERROR" and TICKETS[0][1] == "llm_tavily",
       "失败升级为 ERROR 工单进事件流（source=llm_tavily）", str(TICKETS[:1])[:120])
    tmsg = TICKETS[0][2] if TICKETS else ""
    ck("verify_tavily_resilience.py --live" in tmsg, "工单含**可复现命令**（进化大脑能自己复现）")
    ck("TAVILY_PROXY" in tmsg and "tavily_rounds" in tmsg,
       "工单含**候选修复动作**（改 .env / 改重试参数）", tmsg[:160])

    # ── ③ 同一签名在熔断窗口内只报一次（不刷事件流）──
    # ⚠️ 必须继续往脚本里喂失败：FakeSession 脚本耗尽后会回一个"成功"响应，
    #    那样这一步会静默变成"成功路径"，欠账/去重都测不出来（第一版就踩了这个坑）
    sess2.push(*([ssl_err()] * 4))
    LC.tavily_search("又一条断的")
    ck(len(TICKETS) == 1, "同签名失败在熔断窗口内只报一条工单（去重）", f"tickets={len(TICKETS)}")
    ck(LC.tavily_health()["pending"] == 2, "第二条失败也进了欠账（欠账不因去重而丢）")

    # ── ④ 补做：tavily_drain_pending() 成功即出队 ──
    sess3 = FakeSession([FakeResp(200, {"results": [{"title": "补做成功"}]})] * 2)
    LC._TAVILY_SESSION = sess3
    set_cfg(tavily_max_attempts=2, tavily_rounds=2)
    res = LC.tavily_drain_pending(limit=5)
    ck(res["tried"] == 2 and res["ok"] == 2 and res["left"] == 0,
       "欠账补做成功并出队（tried/ok/left）", str(res))

    # ── ⑤ 开关语义：tavily_required=False 才退回静默降级 ──
    reset(FakeSession([ssl_err()]), clock)
    set_cfg(tavily_max_attempts=1, tavily_rounds=1, tavily_required=False)
    out3 = LC.tavily_search("关掉必需开关")
    ck(out3 == [] and LC.tavily_health()["pending"] == 0 and not TICKETS,
       "tavily_required=False 时不入欠账、不报工单（退回旧行为，语义有效）")
    set_cfg(tavily_required=True)


def sc_cache(clock: FakeClock) -> None:
    print("\n⑪ 消息面缓存：补做回来的消息面要能被复用（也省额度）")
    sess = FakeSession([FakeResp(200, {"results": [{"title": "缓存源"}]})])
    set_cfg(tavily_api_key="k", tavily_required=True, tavily_max_attempts=1, tavily_rounds=1,
            tavily_cache_ttl_s=3600)
    reset(sess, clock)
    a = LC.tavily_search("同一条 query")
    b = LC.tavily_search("同一条 query")
    ck(len(sess.calls) == 1 and a == b and len(b) == 1,
       "TTL 内同 query 只打一次外部请求（第二次命中缓存）", f"calls={len(sess.calls)}")
    ck(os.path.exists(LC.CACHE_FILE), "缓存落盘（跨进程/当日内可复用）")
    # 参数不同 → 键不同，不会串味
    sess.push(FakeResp(200, {"results": [{"title": "1"}]}))
    c = LC.tavily_search("同一条 query", max_results=1)
    ck(len(sess.calls) == 2 and len(c) == 1,
       "max_results 参与缓存键（不会用 5 条缓存糊弄 1 条的调用）", f"calls={len(sess.calls)}")
    # TTL=0 → 关闭缓存，必须回源
    set_cfg(tavily_cache_ttl_s=0)
    sess.push(FakeResp(200, {"results": [{"title": "回源"}]}))
    LC.tavily_search("同一条 query")
    ck(len(sess.calls) == 3, "tavily_cache_ttl_s=0 时关闭缓存（回源请求）", f"calls={len(sess.calls)}")
    set_cfg(tavily_cache_ttl_s=21600)


def live_probe() -> None:
    print("\n🔎 真实链路探测（--live）：1 次真实 search + 1 次 usage（usage 不计搜索额度）")
    LC._TAVILY_SESSION = None          # 用真实 Session（连接池）
    LC.tavily_reset_circuit()
    if not LC.settings.tavily_api_key:
        print("  ⚠️ 未配置 TAVILY_API_KEY，跳过真实探测")
        return
    u = LC.tavily_usage()
    if u.get("ok"):
        print(f"  账户配额: 套餐={u['plan']}  已用={u['used']:.0f}/{u['limit']:.0f}  "
              f"剩余={u['remain']:.0f}  已用尽={u['exhausted']}")
    else:
        print(f"  配额查询失败（不影响搜索本身）: {u.get('error')}")
    out = LC.tavily_search("国务院 政策 支持 产业", max_results=3, kind="policy")
    print(f"  结果条数: {len(out)}")
    for r in out[:3]:
        print(f"   - {r.get('title', '')[:60]}")
    print(f"  健康度: {LC.tavily_health()}")
    print("  " + ("✅ 真实链路可用" if out else "⚠️ 本次真实请求未取到结果（看上面的 WARNING 日志定位）"))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="tavily_verify_")
    real = {
        "time": LC.time,
        "session": LC._TAVILY_SESSION,
        "pending_file": LC.PENDING_FILE,
        "cache_file": LC.CACHE_FILE,
        "issue_sig": LC._TAVILY_ISSUE_SIG,
        "issue_ts": LC._TAVILY_ISSUE_TS,
        "record_event": EV.record_event,
        "cfg": {k: getattr(LC.settings, k) for k in (
            "tavily_api_key", "tavily_proxy", "tavily_base_url", "tavily_timeout",
            "tavily_max_attempts", "tavily_rounds", "tavily_round_gap_s", "tavily_deadline_s",
            "tavily_pending_max", "tavily_drain_limit", "tavily_cache_ttl_s",
            "tavily_fail_threshold", "tavily_circuit_cooldown",
            "tavily_verify_ssl", "tavily_required",
        )},
    }
    # 隔离：欠账/缓存写临时文件（不碰 backend/data）；工单只进内存列表（不写真实事件库）
    LC.PENDING_FILE = os.path.join(tmp, "tavily_pending.json")
    LC.CACHE_FILE = os.path.join(tmp, "tavily_cache.json")
    EV.record_event = lambda level, source, message, count=1: TICKETS.append((level, source, message))
    print("Tavily 消息面必达验证（全 mock，不消耗真实额度；不污染 data/ 与事件库）")
    clock = FakeClock()
    try:
        sc_retry_then_ok(clock)
        sc_fail_contract(clock)
        sc_circuit_opens(clock)
        sc_half_open_recover(clock)
        sc_http_status(clock)
        sc_no_key(clock)
        sc_proxy_endpoint(clock)
        sc_parse(clock)
        sc_quota(clock)
        sc_required_no_silent_skip(clock)
        sc_cache(clock)
    finally:
        LC.time, LC._TAVILY_SESSION = real["time"], real["session"]
        LC.PENDING_FILE = real["pending_file"]
        LC.CACHE_FILE = real["cache_file"]
        LC._TAVILY_ISSUE_SIG, LC._TAVILY_ISSUE_TS = real["issue_sig"], real["issue_ts"]
        EV.record_event = real["record_event"]
        for k, v in real["cfg"].items():
            setattr(LC.settings, k, v)
        LC.tavily_reset_circuit()
        TICKETS.clear()
        shutil.rmtree(tmp, ignore_errors=True)
    # ⚠️ 真实探测必须在"配置恢复"之后：离线场景里 key 被替换成假值 "k"，
    #    若在里面探测会拿到 401（曾经踩过：误以为是链路问题）
    if "--live" in sys.argv:
        live_probe()

    print("\n" + ("=" * 60))
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过（消息面必达：跨轮重试→欠账补做→进化大脑工单；配额与链路故障可区分）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
