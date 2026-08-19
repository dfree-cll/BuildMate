"""共享状态存储单元测试（内存回退路径；Redis 路径语义对齐，CI 默认无 Redis）"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.state_store import state_store, MemoryBackend


async def test_fixed_window_counter():
    await state_store.clear_all()
    counts = [await state_store.incr_window("t:win", 60) for _ in range(5)]
    assert counts == [1, 2, 3, 4, 5]


async def test_kv_ttl_and_delete():
    await state_store.clear_all()
    await state_store.set_kv("t:k", "v1", 60)
    assert await state_store.get_kv("t:k") == "v1"
    await state_store.del_kv("t:k")
    assert await state_store.get_kv("t:k") is None


async def test_set_membership():
    await state_store.clear_all()
    assert not await state_store.in_set("t:set", "a")
    await state_store.add_set("t:set", "a", 60)
    assert await state_store.in_set("t:set", "a")
    assert not await state_store.in_set("t:set", "b")


async def test_memory_backend_expiry():
    import time
    b = MemoryBackend()
    b._kv["exp"] = ("v", time.time() - 1)   # 已过期
    assert await b.get_kv("exp") is None
    b._kv["exp2"] = ("v2", time.time() + 60)
    assert await b.get_kv("exp2") == "v2"


async def test_memory_backend_window_reset():
    import time
    b = MemoryBackend()
    b._win["k"] = (5, time.time() - 61)   # 窗口已过
    assert await b.incr_window("k", 60) == 1   # 重置而非累加
