"""共享状态存储（企业化第一步：多实例部署的基础设施）

存什么：登录限流计数、LLM 限流计数、jti 黑名单、token_version 缓存。
为什么：此前这些都在进程内存——起第二个实例，登出吊销时灵时不灵、限流翻倍失效。

后端选择：
- 配置 REDIS_URL → Redis（多实例共享，推荐生产）
- 未配置 / Redis 不可达 → 进程内存回退（单实例/离线 demo 可用，日志告警一次）

语义说明：计数器为固定窗口（每 key 一个窗口期，Redis INCR+EXPIRE），
与旧滑动窗口相比边界行为略有差异，对防暴力破解/防刷场景足够。
"""
import time

from backend.core.logger import get_logger

logger = get_logger(__name__)


class MemoryBackend:
    """进程内存回退实现（语义对齐 Redis 版）"""

    def __init__(self):
        self._kv: dict[str, tuple[str, float]] = {}       # key -> (value, expire_at)
        self._win: dict[str, tuple[int, float]] = {}      # key -> (count, window_start)

    async def incr_window(self, key: str, window_s: int) -> int:
        now = time.time()
        cnt, start = self._win.get(key, (0, 0))
        if now - start >= window_s:
            cnt, start = 0, now
        cnt += 1
        self._win[key] = (cnt, start)
        return cnt

    async def set_kv(self, key: str, value: str, ttl_s: int) -> None:
        self._kv[key] = (value, time.time() + ttl_s)

    async def get_kv(self, key: str) -> str | None:
        hit = self._kv.get(key)
        if hit is None:
            return None
        value, expire_at = hit
        if time.time() > expire_at:
            self._kv.pop(key, None)
            return None
        return value

    async def del_kv(self, key: str) -> None:
        self._kv.pop(key, None)

    async def in_set(self, key: str, value: str) -> bool:
        return await self.get_kv(f"{key}:{value}") is not None

    async def add_set(self, key: str, value: str, ttl_s: int) -> None:
        await self.set_kv(f"{key}:{value}", "1", ttl_s)

    async def clear_all(self) -> None:
        self._kv.clear()
        self._win.clear()


class _StateStore:
    """门面：Redis 优先，失败/未配置回退内存"""

    def __init__(self):
        self._redis = None
        self._memory = MemoryBackend()
        self._warned = False

    def _get_redis(self):
        if self._redis is not None:
            return self._redis
        from backend.config import get_settings
        url = get_settings().redis_url
        if not url:
            return None
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(url, socket_connect_timeout=1.0,
                                            socket_timeout=1.0, decode_responses=True)
            return self._redis
        except Exception as e:
            self._fallback_warn("init", e)
            return None

    def _fallback_warn(self, action: str, e: Exception) -> None:
        if not self._warned:
            logger.warning("state_store.redis_unavailable_fallback_memory",
                           action=action, error=str(e)[:120],
                           detail="多实例部署下限流/吊销将退化为单实例语义")
            self._warned = True

    async def _call(self, fn_name: str, memory_op, redis_op):
        r = self._get_redis()
        if r is None:
            return await memory_op()
        try:
            return await redis_op(r)
        except Exception as e:
            self._fallback_warn(fn_name, e)
            return await memory_op()

    # ── 公共 API ────────────────────────────────────────────────
    async def incr_window(self, key: str, window_s: int) -> int:
        async def rop(r):
            n = await r.incr(key)
            if n == 1:
                await r.expire(key, window_s)
            return int(n)
        return await self._call("incr_window",
                                lambda: self._memory.incr_window(key, window_s), rop)

    async def set_kv(self, key: str, value: str, ttl_s: int) -> None:
        async def rop(r):
            await r.set(key, value, ex=ttl_s)
        return await self._call("set_kv",
                                lambda: self._memory.set_kv(key, value, ttl_s), rop)

    async def get_kv(self, key: str) -> str | None:
        async def rop(r):
            v = await r.get(key)
            return v if v is None else str(v)
        return await self._call("get_kv", lambda: self._memory.get_kv(key), rop)

    async def del_kv(self, key: str) -> None:
        async def rop(r):
            await r.delete(key)
        return await self._call("del_kv", lambda: self._memory.del_kv(key), rop)

    async def in_set(self, key: str, value: str) -> bool:
        async def rop(r):
            return bool(await r.sismember(key, value))
        return await self._call("in_set", lambda: self._memory.in_set(key, value), rop)

    async def add_set(self, key: str, value: str, ttl_s: int) -> None:
        async def rop(r):
            # 集合成员无独立 TTL：以 key 级 TTL 近似（成员按批过期）；
            # 黑名单场景（7 天刷新令牌寿命）按天粒度过期可接受，内存回退版为成员级 TTL
            await r.sadd(key, value)
            await r.expire(key, ttl_s)
        return await self._call("add_set",
                                lambda: self._memory.add_set(key, value, ttl_s), rop)

    async def clear_all(self) -> None:
        """仅测试用：清空内存回退态（Redis 模式下不清远端）"""
        await self._memory.clear_all()


state_store = _StateStore()
