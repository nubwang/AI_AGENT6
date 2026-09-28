"""Tushare API 限流器（支持同步+异步双模式, 并发安全）

Tushare 频率限制按"单接口"计(基础档多数接口 200/min)。因此按接口名提供**独立令牌桶**:
不同接口可并行各以自己的配额满速运行, 不再被"全局单桶"互相拖累——
原实现把所有接口塞进一条 200/min 管道(几十个接口争抢), 是采集慢 / 满屏"触发限流等待"的主因。
可选全局总保护桶(settings.tushare_rate_global>0 时启用)兜底账户级风控。
"""
import time
import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from app.core.config import settings
from app.core.logger import logger


class RateLimiter:
    """固定窗口令牌桶限流器.

    并发安全要点: 锁内只做"判断+计数", 睡眠统一放在锁外.
    这样即使多个协程/线程同时进入, 也不会因持锁睡眠而阻塞整个事件循环.
    """

    def __init__(self, max_per_minute: int = 200):
        self.max_per_minute = max(1, int(max_per_minute))
        self.tokens = self.max_per_minute
        self.last_refill = time.time()
        self.lock = threading.Lock()

    def _acquire(self) -> float:
        """尝试获取1个令牌. 返回需要等待的秒数(0表示已放行).

        令牌桶: 按固定速率(max_per_minute/60 每秒)持续补充, 平滑限流无突发。
        线程安全: 临界区极小(不加锁睡眠), 并发场景下对其他协程的阻塞可忽略。
        修复: 原固定窗口实现(每60s一次性补满200)在并发协程同时醒来时会瞬间
        发出200个请求, 跨越 Tushare 滑动窗口边界导致"频率超限"。
        """
        with self.lock:
            now = time.time()
            # 自上次补充以来的令牌增量(按 max_per_minute/60 每秒的速率)
            refill = (now - self.last_refill) * self.max_per_minute / 60.0
            self.tokens = min(self.max_per_minute, self.tokens + refill)
            self.last_refill = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0
            # 不足 1 个令牌: 返回补充 1 个令牌所需秒数
            return (1.0 - self.tokens) / (self.max_per_minute / 60.0)

    def wait_if_needed(self):
        """同步阻塞等待（用于同步代码，如backfill脚本）"""
        while True:
            wait = self._acquire()
            if wait <= 0:
                return
            logger.warning(f"触发限流，等待 {wait:.1f} 秒")
            time.sleep(wait)

    async def async_wait(self):
        """异步等待（用于FastAPI/WebSocket异步代码）"""
        while True:
            wait = self._acquire()
            if wait <= 0:
                return
            logger.warning(f"触发限流，异步等待 {wait:.1f} 秒")
            await asyncio.sleep(wait)

    def __enter__(self):
        self.wait_if_needed()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    async def __aenter__(self):
        await self.async_wait()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


# 未命名调用使用的默认桶(兼容旧逻辑, 配额=旧 tushare_rate_limit)
rate_limiter = RateLimiter(settings.tushare_rate_limit or 200)

# 可选全局总保护桶(0=关闭)。仅当担心账户级总限频时在 .env 设置 tushare_rate_global>0
_global_limit = int(getattr(settings, "tushare_rate_global", None) or 0)
_global_limiter = RateLimiter(_global_limit) if _global_limit > 0 else None

# 按接口名的独立限流桶注册表(懒创建)
_pool: dict[str, RateLimiter] = {}
_pool_lock = threading.Lock()


def get_limiter(name: str = "") -> RateLimiter:
    """按接口名返回独立限流桶(每接口配额=tushare_rate_per_api, 默认200/min)。

    同一接口的所有请求共享该接口桶 → 恰好不超该接口配额;
    不同接口用不同桶 → 并行满速, 互不拖累。
    name 为空时返回默认全局桶(兼容旧调用)。
    """
    if not name:
        return rate_limiter
    lm = _pool.get(name)
    if lm is None:
        q = max(1, int(getattr(settings, "tushare_rate_per_api", None) or 200))
        with _pool_lock:
            lm = _pool.get(name)
            if lm is None:
                lm = _pool.setdefault(name, RateLimiter(q))
    return lm


def _sync_wait_global_and(name: str) -> None:
    """同步: 先(可选)全局保护桶, 再接口桶"""
    if _global_limiter is not None:
        _global_limiter.wait_if_needed()
    get_limiter(name).wait_if_needed()


async def _async_wait_global_and(name: str) -> None:
    """异步: 先(可选)全局保护桶, 再接口桶"""
    if _global_limiter is not None:
        await _global_limiter.async_wait()
    await get_limiter(name).async_wait()


@contextmanager
def slimiter(name: str = ""):
    """同步上下文限流(按接口名), 用于 collector 等同步采集代码。"""
    _sync_wait_global_and(name)
    yield


@asynccontextmanager
async def alimiter(name: str = ""):
    """异步上下文限流(按接口名), 用于 ws 采集循环等异步代码。"""
    await _async_wait_global_and(name)
    yield
