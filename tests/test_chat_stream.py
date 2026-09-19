"""POST /chat 的**流式协议**行为测试。

和 test_chat_context.py 的分工：
- test_chat_context.py 管「喂给模型的内容对不对」（上下文组装 + 缓存读写顺序）；
- 本文件管「回给客户端的**字节**对不对」——SSE 分帧、`done` 事件、错误事件、
  以及流结束后落库的字段。

为什么原来这两块都没有覆盖：`tests/test_api.py` 里连一条 `POST /chat` 都没有
（grep 不到），`tests/e2e/smoke.py` 虽然打真 HTTP，但它只断言「有响应、有 chunk」，
不看分帧、不看 `done` 的字段、也不看 assistant 消息的 latency_ms 到底写没写。
于是「服务端 /chat 路由」是本仓库唯一一块**只靠手工看输出**的区域。

替身直接复用 test_chat_context.py 的有状态假 Session 与记录型缓存——**只留一份实现**，
两份早晚分叉（这条教训在本项目的 SSE 测试上已经踩过一次）。
"""

import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.deps import get_db
from api.main import app
from db.models import Conversation
from tests.test_chat_context import _ChatCase, _StatefulSession

SSE_PREFIX = "data: "


def _events(body: str):
    """把 SSE 响应体切成一个个事件（空行是分隔符），返回去掉 `data: ` 前缀的原文。"""
    frames = [f for f in body.split("\n\n") if f]
    assert all(f.startswith(SSE_PREFIX) for f in frames), "存在不以 data: 开头的事件：%r" % (frames,)
    return [f[len(SSE_PREFIX):] for f in frames]


def _payloads(body: str):
    return [json.loads(e) for e in _events(body)]


class _StreamCase(_ChatCase):
    """在 _ChatCase 的脚手架上加一层：能拿到**原始 HTTP 响应**。

    _ChatCase.run_rounds() 只回传「传给模型的消息」，那是为了上下文断言；
    这里要的是响应头、响应体、以及落库后的 session，所以另开一个入口。
    """

    def send(self, content="第一句", *, conversation_id=1, exists=True, stream=None):
        """打一次 POST /chat，返回 (响应, 假 Session)。

        exists=False 时假 Session 查不到会话 —— 用来复现 404 路径。
        """
        conv = None
        if exists:
            conv = Conversation(user_id=1, model_id=1, title="t")
            conv.id = conversation_id

        session = _StatefulSession(conv, "test-model:latest", [])
        app.dependency_overrides[get_db] = lambda: session

        import api.routers.chat as chat_mod

        chat_mod.cache = self.cache

        with patch(
            "src.ollama_client.OllamaClient.chat_stream", stream or self._fake_chat_stream
        ):
            resp = TestClient(app).post(
                "/chat", json={"conversation_id": conversation_id, "content": content}
            )
        return resp, session

    def _raising_stream(self, messages=None, model=None, **kwargs):
        """Ollama 断连：照 src/ollama_client.py 的契约抛 ConnectionError。

        写成生成器（函数体里有 yield）才能保证异常是在**迭代时**抛出 ——
        也就是在路由的 try 里面，而不是在调用 chat_stream() 的那一刻。
        """
        raise ConnectionError("Ollama 连接失败：Connection refused")
        yield  # pragma: no cover


class TestSSEFrameFormat(_StreamCase):
    """SSE 的分帧与响应头 —— 前端 EventSource 与 Nginx 都依赖它。"""

    def test_content_type_is_event_stream(self):
        resp, _ = self.send()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            resp.headers["content-type"].startswith("text/event-stream"),
            "Content-Type 是 %r，浏览器不会把它当 SSE 处理" % resp.headers["content-type"],
        )

    def test_every_frame_starts_with_data_and_ends_with_blank_line(self):
        body = self.send()[0].text
        self.assertTrue(body.endswith("\n\n"), "响应体没以空行结束：%r" % body[-20:])
        frames = [f for f in body.split("\n\n") if f]
        self.assertEqual(len(frames), 3, "两个回复块 + 一个 done，实际分帧=%r" % (frames,))
        for f in frames:
            self.assertTrue(f.startswith(SSE_PREFIX), "分帧没带 data: 前缀：%r" % (f,))
            self.assertNotIn("\n", f, "单个事件里混进了换行，客户端会把一帧读成两帧：%r" % (f,))

    def test_chunks_are_forwarded_verbatim_and_in_order(self):
        """透传的块内容和顺序都不能变 —— 拼接结果必须等于模型吐出的原文。"""
        payloads = _payloads(self.send()[0].text)
        self.assertEqual(payloads[-1].get("done"), True)
        chunks = [p["chunk"] for p in payloads[:-1]]
        self.assertEqual(chunks, ["收到", "了。"])
        self.assertEqual("".join(chunks), "收到了。")

    def test_no_buffering_headers_for_reverse_proxy(self):
        """上线要过 Nginx：不显式关缓冲，SSE 会被反代攒成一坨，流式就成了假的。

        这一条是**公网入口**那块唯一能在单测里钉住的契约（安全组/Nginx 配置本身
        没有本地可验证的手段，别把这条当成「Nginx 验过了」）。
        """
        resp, _ = self.send()
        self.assertEqual(resp.headers.get("x-accel-buffering"), "no")
        self.assertEqual(resp.headers.get("cache-control"), "no-cache")


class TestDoneEvent(_StreamCase):
    def test_last_event_is_done_with_int_latency(self):
        payloads = _payloads(self.send()[0].text)
        last = payloads[-1]
        self.assertEqual(
            set(last), {"done", "latency_ms"}, "done 事件字段变了：%r" % (last,)
        )
        self.assertIs(last["done"], True)
        self.assertIsInstance(last["latency_ms"], int)
        self.assertGreaterEqual(last["latency_ms"], 0)

    def test_done_is_last_and_appears_once(self):
        payloads = _payloads(self.send()[0].text)
        self.assertEqual(
            sum(1 for p in payloads if p.get("done")), 1, "done 出现了不止一次"
        )


class TestUnknownConversation(_StreamCase):
    def test_returns_404_and_not_500(self):
        resp, _ = self.send(exists=False)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()["detail"], "会话不存在")

    def test_no_side_effects_before_the_404(self):
        """404 必须发生在**任何副作用之前**：不落消息、不碰缓存。

        否则「打一个不存在的会话」会在库里留下一条孤儿 user 消息，
        而且这条消息谁都看不到（会话本身不存在）。
        """
        _, session = self.send(exists=False)
        self.assertEqual(session.rows, [], "404 之前就落了消息")
        self.assertEqual(self.cache.log, [], "404 之前就访问了缓存：%r" % (self.cache.log,))


class TestOllamaDisconnect(_StreamCase):
    def test_connection_error_becomes_error_event_not_500(self):
        """流已经开始后，HTTP 状态码就改不了了 —— 只能从流里报错。

        这也是为什么这个接口不能靠 500 传达失败：响应头早就发出去了。
        """
        resp, _ = self.send(stream=self._raising_stream)
        self.assertEqual(resp.status_code, 200, "断连不该变成 500（头已经发出去了）")
        payloads = _payloads(resp.text)
        self.assertTrue(
            any("error" in p for p in payloads),
            "流里没有 error 事件，客户端只会看到连接莫名结束：%r" % (payloads,),
        )
        self.assertIn("Connection refused", payloads[0]["error"])

    def test_no_assistant_message_and_no_done_event_on_disconnect(self):
        resp, session = self.send(stream=self._raising_stream)
        self.assertEqual(
            [m for m in session.rows if m.role == "assistant"],
            [],
            "断连了却落了 assistant 消息（内容还是不完整的）",
        )
        self.assertFalse(
            any(p.get("done") for p in _payloads(resp.text)),
            "断连却发了 done，客户端会以为回答完整",
        )


class TestLatencyPersisted(_StreamCase):
    def test_assistant_message_persists_latency_ms(self):
        """`latency_ms` 是这一层唯一的性能数据来源，写了没写必须被钉住。"""
        _, session = self.send()
        assistants = [m for m in session.rows if m.role == "assistant"]
        self.assertEqual(len(assistants), 1, "assistant 消息条数不对：%r" % (session.rows,))
        self.assertIsNotNone(assistants[0].latency_ms, "latency_ms 没落库")
        self.assertIsInstance(assistants[0].latency_ms, int)
        self.assertGreaterEqual(assistants[0].latency_ms, 0)

    def test_persisted_latency_matches_the_done_event(self):
        """流里报给前端的耗时，和落库的必须是**同一个数**。

        否则前端显示的耗时和事后从库里统计的对不上，而且这种偏差不会报错。
        """
        resp, session = self.send()
        assistant = [m for m in session.rows if m.role == "assistant"][0]
        done = _payloads(resp.text)[-1]
        self.assertEqual(assistant.latency_ms, done["latency_ms"])
        self.assertEqual(assistant.content, "收到了。", "落库的回复不是块的拼接结果")


if __name__ == "__main__":
    unittest.main()
