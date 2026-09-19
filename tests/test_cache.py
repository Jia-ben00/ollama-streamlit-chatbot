"""Redis 缓存层测试：用「假 redis」验证缓存逻辑与降级行为。

为什么不用真 Redis：CI 环境没有 Redis 服务，而且这层要测的重点不是「redis-py 能不能连」，
而是我自己的封装逻辑——TTL 有没有设、主动失效有没有生效、缓存挂了会不会拖垮主流程。
用一个内存字典冒充 redis 客户端，就能把这四条全覆盖，且跑得飞快、零外部依赖。

（真 Redis 的连通性在云上由 docker-compose 的 healthcheck 保证，不需要单元测试重复验证。）
"""

import unittest
from unittest.mock import patch

from cache import ConversationCache


class FakeRedis:
    """内存版 redis 客户端，只实现 cache.py 用到的那几个方法。"""

    def __init__(self):
        self.store = {}
        self.ttl_seen = {}
        self.deleted = []
        self.fail_on = None  # 置成 "get"/"setex"/"delete" 可模拟某操作抛异常

    def ping(self):
        return True

    def get(self, key):
        if self.fail_on == "get":
            raise RuntimeError("boom: redis 读失败")
        return self.store.get(key)

    def setex(self, key, ttl, value):
        if self.fail_on == "setex":
            raise RuntimeError("boom: redis 写失败")
        self.store[key] = value
        self.ttl_seen[key] = ttl

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)


def _make_cache(client: FakeRedis, ttl: int = 1800) -> ConversationCache:
    """构造一个把真实 redis 换成 FakeRedis 的缓存实例。"""

    class _RedisFacade:
        @staticmethod
        def from_url(url, **kwargs):
            client.url = url
            client.kwargs = kwargs
            return client

    with patch("cache.redis") as fake_module:
        fake_module.Redis.from_url = _RedisFacade.from_url
        return ConversationCache(url="redis://fake:6379/0", ttl=ttl)


class TestConversationCache(unittest.TestCase):
    """缓存三问的代码级验证：缓存什么 / 为什么 / 什么时候失效。"""

    def test_roundtrip_and_ttl(self):
        """写入后能读回同样的上下文，且 TTL 被正确设置。"""
        client = FakeRedis()
        cache = _make_cache(client)

        self.assertTrue(cache.enabled)
        self.assertIsNone(cache.get_context(42))  # 未命中返回 None

        msgs = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在"}]
        cache.set_context(42, msgs)

        self.assertEqual(cache.get_context(42), msgs)
        # TTL 语义：半小时不说话的会话，缓存自动过期，Redis 不会无限膨胀。
        self.assertEqual(client.ttl_seen["chat:conv:42:context"], 1800)

    def test_invalidate_on_new_message(self):
        """主动失效：新消息落库后缓存必须被清掉，否则会喂给模型旧上下文。"""
        client = FakeRedis()
        cache = _make_cache(client)

        cache.set_context(7, [{"role": "user", "content": "旧"}])
        cache.invalidate(7)

        self.assertIsNone(cache.get_context(7))
        self.assertIn("chat:conv:7:context", client.deleted)

    def test_disabled_when_redis_unavailable(self):
        """Redis 未安装 / 连不上时，整层降级为空实现，不抛异常。"""
        with patch("cache.redis", None):
            cache = ConversationCache()

        self.assertFalse(cache.enabled)
        # 四个方法全部可安全调用，主流程（聊天）不受影响。
        self.assertIsNone(cache.get_context(1))
        cache.set_context(1, [{"role": "user", "content": "x"}])
        cache.invalidate(1)

    def test_read_write_errors_are_swallowed(self):
        """缓存读写抛异常时必须被吞掉并回退 DB —— 缓存是加速项，不是正确性依赖。"""
        client = FakeRedis()
        cache = _make_cache(client)

        client.fail_on = "setex"
        cache.set_context(5, [{"role": "user", "content": "x"}])  # 不抛

        client.fail_on = "get"
        self.assertIsNone(cache.get_context(5))  # 读失败 → 回退 DB（返回 None）

        client.fail_on = "delete"
        cache.invalidate(5)  # 不抛


if __name__ == "__main__":
    unittest.main()
