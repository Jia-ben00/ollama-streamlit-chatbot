"""POST /chat 的上下文组装与缓存行为测试。

为什么这些用例要单独放一个文件，而不是加进 test_api.py：

test_api.py 用的是**无状态**的 Query 替身（`_FakeQuery`）—— 查什么返回什么
完全由测试预先写好。这种替身能验证「路由把参数拼对了没、响应格式对不对」，
但它天然看不见**顺序**：不管路由是先落库再查、还是先查再落库，
无状态替身都返回同一份预设数据。

而本文件要钉住的两个缺陷恰恰就是顺序问题：
  1. 上下文里当前用户消息重复一次；
  2. 缓存命中率恒为 0（写后立刻删）。
所以这里换成**有状态**的假 Session：`add()` + `commit()` 过的对象，
后续 `query()` 能查到 —— 于是「先落库再读历史」会真的把本次输入读出来，
缺陷就暴露了。

另外补一条缓存内容的完整性断言：防止用「只缓存本轮两条消息」这种
看起来能提高命中率、实际会丢掉更早历史的改法把缺陷"修"成另一种缺陷。
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.deps import get_db
from api.main import app
from db.models import Conversation, Message, Model

CONTEXT_LIMIT = 20


def _msg(mid, content, role="user"):
    """构造一条「已落库」的 Message。"""
    m = Message(conversation_id=1, role=role, content=content)
    m.id = mid
    return m


class _MessageQuery:
    """Message 查询替身：模拟 `.filter().order_by().limit().all()`，并**真的截断**。"""

    def __init__(self, session):
        self._session = session
        self._limit = None

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def all(self):
        rows = self._session.rows
        if self._limit is not None:
            rows = rows[-self._limit:]
        # 真实查询是 `id desc` + `limit`，路由里再 reversed() 翻回正序，
        # 所以这里同样返回倒序，让路由的 reversed() 生效。
        return list(reversed(rows))

    def first(self):
        rows = self.all()
        return rows[0] if rows else None


class _SingleQuery:
    """`.filter().first()` 型查询替身（Conversation / Model.name）。"""

    def __init__(self, first=None):
        self._first = first

    def filter(self, *args, **kwargs):
        return self

    def join(self, *args, **kwargs):
        return self

    def order_by(self, *args):
        return self

    def limit(self, *args):
        return self

    def first(self):
        return self._first


class _StatefulSession:
    """有状态假 Session —— 本文件的核心。

    `commit()` 会把 `add()` 进去的消息真正并入 `rows` 并分配 id，
    所以「本次输入落库了没」这件事对后续查询是可见的。
    """

    def __init__(self, conversation, model_name, existing_rows):
        self.conversation = conversation
        self.model_name = model_name
        self.rows = list(existing_rows)
        self._pending = []
        self._next_id = (max([r.id for r in self.rows]) if self.rows else 0) + 1
        self.committed = 0

    def query(self, *entities):
        ent = entities[0]
        # `db.query(Model.name)` 传进来的是 InstrumentedAttribute，取它的 .class_
        if getattr(ent, "class_", ent) is Model:
            return _SingleQuery(first=(self.model_name,))
        if ent is Conversation:
            return _SingleQuery(first=self.conversation)
        if ent is Message:
            return _MessageQuery(self)
        raise AssertionError("未预期的查询目标：%r" % (entities,))

    def add(self, obj):
        self._pending.append(obj)

    def commit(self):
        for obj in self._pending:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1
            self.rows.append(obj)
        self._pending = []
        self.committed += 1

    def close(self):
        pass


class _RecordingCache:
    """记录型缓存替身：接口与 cache.ConversationCache 一致，并记录调用序列。"""

    def __init__(self):
        self.enabled = True
        self.store = {}
        self.log = []

    def get_context(self, conversation_id):
        hit = conversation_id in self.store
        self.log.append("GET -> %s" % ("HIT" if hit else "MISS"))
        return self.store.get(conversation_id)

    def set_context(self, conversation_id, messages):
        self.log.append("SET(%d)" % len(messages))
        self.store[conversation_id] = list(messages)

    def invalidate(self, conversation_id):
        self.log.append("DEL")
        self.store.pop(conversation_id, None)


class _ChatCase(unittest.TestCase):
    """公共脚手架：装好假 Session / 假 Ollama / 记录型缓存，跑若干轮对话。"""

    def setUp(self):
        self.cache = _RecordingCache()
        self.sent_batches = []
        self.app = app

    def tearDown(self):
        app.dependency_overrides.clear()

    def _fake_chat_stream(self, messages=None, model=None, **kwargs):
        self.sent_batches.append(list(messages or []))
        yield "收到"
        yield "了。"

    def run_rounds(self, rounds, existing_rows=None, conversation_id=1):
        """跑 `rounds` 次 POST /chat，返回每轮传给 Ollama 的非 system 消息列表。"""
        conv = Conversation(user_id=1, model_id=1, title="t")
        conv.id = conversation_id
        session = _StatefulSession(
            conv, "test-model:latest", list(existing_rows or [])
        )
        app.dependency_overrides[get_db] = lambda: session

        import api.routers.chat as chat_mod
        chat_mod.cache = self.cache

        results = []
        with patch(
            "src.ollama_client.OllamaClient.chat_stream", self._fake_chat_stream
        ):
            client = TestClient(app)
            for i in range(1, rounds + 1):
                resp = client.post(
                    "/chat",
                    json={"conversation_id": conversation_id, "content": "第 %d 句" % i},
                )
                assert resp.status_code == 200, resp.text
                results.append(
                    [m for m in self.sent_batches[-1] if m["role"] != "system"]
                )
        return results


class TestContextHasNoDuplicate(_ChatCase):
    """上下文里当前用户输入只能出现一次。"""

    def test_current_input_appears_once_on_first_round(self):
        [msgs] = self.run_rounds(1)
        contents = [m["content"] for m in msgs]
        self.assertEqual(
            contents.count("第 1 句"),
            1,
            "当前用户输入在上下文里出现了 {} 次，应为 1 次；完整上下文={!r}".format(
                contents.count("第 1 句"), contents
            ),
        )

    def test_current_input_appears_once_with_existing_history(self):
        """已有历史时同样不能重复（历史里有别的 user 消息，不能误伤）。"""
        history = [_msg(1, "旧问题", "user"), _msg(2, "旧回答", "assistant")]
        [msgs] = self.run_rounds(1, existing_rows=history)
        contents = [m["content"] for m in msgs]
        self.assertEqual(contents.count("第 1 句"), 1, "本次输入重复：%r" % (contents,))
        self.assertEqual(contents.count("旧问题"), 1, "历史被重复：%r" % (contents,))
        self.assertEqual(contents, ["旧问题", "旧回答", "第 1 句"], "上下文顺序不对")

    def test_history_grows_and_stays_correct_across_rounds(self):
        """连续三轮：每轮上下文都应恰好等于「之前所有轮次 + 本次输入」。"""
        rounds = self.run_rounds(3)
        expected = [
            ["第 1 句"],
            ["第 1 句", "收到了。", "第 2 句"],
            ["第 1 句", "收到了。", "第 2 句", "收到了。", "第 3 句"],
        ]
        for idx, (got, want) in enumerate(zip(rounds, expected), start=1):
            self.assertEqual(
                [m["content"] for m in got],
                want,
                "第 %d 轮的上下文不对" % idx,
            )


class TestCacheActuallyHelps(_ChatCase):
    """缓存必须真的被复用 —— 命中率不能恒为 0。"""

    def test_second_round_hits_cache(self):
        self.run_rounds(3)
        self.assertEqual(
            self.cache.log[0],
            "GET -> MISS",
            "第一轮缓存应该是 MISS（冷启动），实际：%r" % (self.cache.log,),
        )
        gets = [x for x in self.cache.log if x.startswith("GET")]
        self.assertGreaterEqual(len(gets), 3)
        self.assertEqual(
            gets[1],
            "GET -> HIT",
            "第二轮仍是 MISS，说明缓存写了又被删掉，命中率恒为 0。序列：%r"
            % (self.cache.log,),
        )
        self.assertEqual(
            gets[2],
            "GET -> HIT",
            "第三轮仍是 MISS。序列：%r" % (self.cache.log,),
        )

    def test_cache_is_updated_not_invalidated_at_end_of_round(self):
        """一轮结束后缓存里应该**有**内容，而不是被删空。"""
        self.run_rounds(2)
        self.assertIn(
            1,
            self.cache.store,
            "两轮跑完后缓存里没有该会话 —— 说明末尾是 invalidate 而不是更新",
        )
        self.assertNotIn(
            "DEL",
            self.cache.log,
            "正常对话流程不应该删除缓存（删除只属于「清空消息」这类场景）",
        )

    def test_cached_context_keeps_full_history(self):
        """缓存内容必须含更早的对话，不能只剩本轮两条。

        这条是用来拦住「只 set 本轮消息」那种假修复的：那样命中率确实上去了，
        但模型的上下文会缺掉更早的对话。
        """
        self.run_rounds(2)
        cached = self.cache.store[1]
        contents = [m["content"] for m in cached]
        self.assertEqual(
            contents,
            ["第 1 句", "收到了。", "第 2 句", "收到了。"],
            "缓存里的上下文不完整：%r" % (contents,),
        )
        self.assertLessEqual(
            len(cached), CONTEXT_LIMIT, "缓存条数超过了 CONTEXT_LIMIT"
        )

    def test_cached_context_matches_persisted_history(self):
        """缓存与 DB 必须一致：把缓存当上下文用时，结果应与直接查库相同。"""
        history = [_msg(1, "旧问题", "user"), _msg(2, "旧回答", "assistant")]
        self.run_rounds(2, existing_rows=history)
        cached = [m["content"] for m in self.cache.store[1]]
        self.assertEqual(
            cached,
            ["旧问题", "旧回答", "第 1 句", "收到了。", "第 2 句", "收到了。"],
            "缓存内容与已落库的历史不一致：%r" % (cached,),
        )


class TestCacheDegradesGracefully(_ChatCase):
    """Redis 挂掉时功能不能挂 —— 回归保护，别在修缓存时破坏了降级路径。"""

    def test_chat_still_works_when_cache_disabled(self):
        conv = Conversation(user_id=1, model_id=1, title="t")
        conv.id = 1
        session = _StatefulSession(conv, "test-model:latest", [])
        app.dependency_overrides[get_db] = lambda: session

        class _Disabled:
            enabled = False

            def get_context(self, conversation_id):
                return None

            def set_context(self, conversation_id, messages):
                return None

            def invalidate(self, conversation_id):
                return None

        import api.routers.chat as chat_mod
        chat_mod.cache = _Disabled()

        with patch(
            "src.ollama_client.OllamaClient.chat_stream", self._fake_chat_stream
        ):
            resp = TestClient(app).post(
                "/chat", json={"conversation_id": 1, "content": "在吗"}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("收到", resp.text)


if __name__ == "__main__":
    unittest.main()
