"""Redis 会话上下文缓存。

面试必问三连：缓存什么、为什么是它、什么时候失效。下面逐个说清。

**缓存什么？**
「会话的最近 N 条消息」——也就是发给 Ollama 时需要的对话上下文。聊天是流式的，
每次用户发一句，都要把「系统提示 + 最近 20 条历史」拼起来再发给模型。如果每次
都去 MySQL 里 SELECT 最近 20 条，一个用户聊 100 句就是 100 次查询；而且聊天是
高频、连续的操作，DB 的压力大部分来自这里。

**为什么是 Redis 而不是 DB？**
Redis 是内存 KV，读延迟亚毫秒级；MySQL 是磁盘 + 行锁，读一条也要走网络 + 可能
落盘。聊天上下文的读写是「热路径」，用 Redis 把这一层从 DB 里剥出来，DB 只负责
「持久化落库」，Redis 负责「给模型喂上下文」，各司其职。

**什么时候失效？**
1. TTL 过期：设了 ttl=1800（半小时）。用户半小时不说话，缓存自动清掉。
   这样 Redis 不会无限膨胀，也不需要人工清理「僵尸会话」。
2. 主动更新（写穿）：每轮对话结束时，把本轮新增的「用户输入 + 模型回复」并入上下文
   再写回（见 api/routers/chat.py）。如果只设 TTL 不主动更新，命中的请求会看不到
   「用户刚发的消息」，也就是脏读。

   ⚠️ 这里踩过一个坑：最初的实现写成「落库后 invalidate()（删除缓存）」。但
   get_context 在每轮**开头**、invalidate 在每轮**末尾**，于是每次请求进来时缓存
   必然是空的——写进去的值从没被任何一次读到过，**命中率恒为 0，这层缓存等于没接**。
   正确做法是**写穿（write-through）**，不是删除：删除只是把「一直不命中」换成
   「命中但内容过期」。invalidate 现在只服务于「清空会话消息」这类真正要丢弃的场景。

**为什么这层要薄？**
这个项目 Redis 只承担「上下文缓存」这一个明确职责，不搞分布式锁、不做消息队列。
P0 里 Redis 只有 2 小时预算，把一件事做透、能讲清「为什么」，比堆一堆用不上的
特性值钱得多。

注意：本模块只 import redis 但不强制要求服务在线。连接失败时降级为「不缓存」，
聊天功能照样能跑（只是每次都查 DB）。这是「缓存是加速项、不是正确性依赖」的
体现——面试官会喜欢「缓存挂了服务不能挂」这个意识。
"""

import json
import logging
import os
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# Redis 连接串，从环境变量读，默认本机。
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

# 用 redis-py 提供的基础连接池。生产里会换成异步客户端（aioredis），
# 但 FastAPI 同步端点 + 同步 redis-py 对这个量级完全够用，先不引入复杂度。
try:
    import redis
except ImportError:  # redis 未安装时，Cache 退化为空实现，不阻塞主流程。
    redis = None


class ConversationCache:
    """会话上下文的 Redis 缓存封装。

    存的是 JSON 序列化后的消息列表，key 形如 `chat:conv:{id}:context`。
    """

    def __init__(self, url: str = REDIS_URL, ttl: int = 1800):
        self.ttl = ttl
        self._client: Optional[object] = None
        if redis is not None:
            try:
                self._client = redis.Redis.from_url(url, decode_responses=True)
                # 连接是懒建立的，这里 ping 一下确认服务可达；失败则置空走降级。
                self._client.ping()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis 不可用，缓存降级为不启用：%s", exc)
                self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _key(self, conversation_id: int) -> str:
        return f"chat:conv:{conversation_id}:context"

    def get_context(self, conversation_id: int) -> Optional[List[Dict[str, str]]]:
        """读取会话上下文，未命中返回 None。"""
        if not self.enabled:
            return None
        try:
            raw = self._client.get(self._key(conversation_id))
            return json.loads(raw) if raw else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("读缓存失败，回退 DB：%s", exc)
            return None

    def set_context(self, conversation_id: int, messages: List[Dict[str, str]]) -> None:
        """写入会话上下文，带 TTL。"""
        if not self.enabled:
            return
        try:
            self._client.setex(self._key(conversation_id), self.ttl, json.dumps(messages))
        except Exception as exc:  # noqa: BLE001
            logger.warning("写缓存失败，忽略（不影响主流程）：%s", exc)

    def invalidate(self, conversation_id: int) -> None:
        """主动失效：仅用于「清空会话消息」这类场景，见 api/routers/conversations.py。

        注意：常规对话**不用**它 —— 每轮结束用的是写穿（set_context），
        用 invalidate 会让下轮开头必然 MISS，命中率恒为 0。
        """
        if not self.enabled:
            return
        try:
            self._client.delete(self._key(conversation_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("失效缓存失败：%s", exc)


# 全局单例，FastAPI 依赖里注入。
cache = ConversationCache()
