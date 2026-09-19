"""前端 API 客户端测试（全部 mock requests，离线跑，进 CI）。

这一层的测试价值和别的层不太一样：`src/api_client.py` 是**唯一**同时踩在两个
不可控依赖上的代码——HTTP 网络、以及 SSE 的逐行协议解析。界面(Streamlit)是
没法写单测的（它依赖 st.* 的运行时状态），所以「前端逻辑对不对」这件事，
只能在这一层把它钉住。

其中最重要的一条是 `test_stream_uses_small_chunk_size`：它锁住的是那个
「攒批」的坑不复发。这个坑有换一层就重现的性质（服务端读 Ollama 一次，
前端读后端又一次），而它的表现是**功能看起来正常、只是变慢了**——
没有测试的话，某次「顺手把 chunk_size 去掉、用默认值」的改动不会被任何人发现，
直到用户抱怨「字怎么一段一段蹦」。所以这里不测「能不能跑通」，测「粒度对不对」。
"""

import json
import unittest
from unittest.mock import patch

import requests

from src.api_client import (
    APIUnreachable,
    ChatAPIClient,
    ChatAPIError,
    _extract_detail,
)
from src.ollama_client import STREAM_READ_CHUNK


class _FakeResponse:
    """HTTP 响应替身：只实现客户端真正用到的几个成员。"""

    def __init__(self, lines=None, status=200, json_body=None, raw_lines=None):
        self._lines = list(lines) if lines is not None else []
        self._raw = raw_lines  # 想测 bytes 分支时用
        self.status_code = status
        self._json = json_body
        self.closed = False
        self.content = b"{}"
        self.seen_chunk_size = "NOT CALLED"

    def iter_lines(self, chunk_size=None, decode_unicode=False):
        self.seen_chunk_size = chunk_size
        if self._raw is not None:
            for line in self._raw:
                yield line
            return
        for line in self._lines:
            yield line

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def close(self):
        self.closed = True


def _sse(*events) -> list:
    """把若干事件对象编成 SSE 文本行（末尾带空行分隔）。"""
    lines = []
    for ev in events:
        lines.append("data: " + json.dumps(ev, ensure_ascii=False))
        lines.append("")
    return lines


class _ClientBase(unittest.TestCase):
    def setUp(self):
        patcher = patch("src.api_client.requests.Session")
        self.mock_session_cls = patcher.start()
        self.addCleanup(patcher.stop)
        self.session = self.mock_session_cls.return_value

    def stream_client(self, resp, base_url="http://127.0.0.1:8000"):
        """造一个 client，并让它的 POST /chat 返回给定响应。"""
        self.session.post.return_value = resp
        return ChatAPIClient(base_url=base_url)

    def json_client(self, resp, method="GET"):
        self.session.request.return_value = resp
        return ChatAPIClient(base_url="http://127.0.0.1:8000")


class TestStreamParsing(_ClientBase):
    """SSE 消费：解析、元数据、异常、资源释放。"""

    def test_yields_chunks_in_order(self):
        resp = _FakeResponse(
            lines=_sse(
                {"chunk": "武汉"},
                {"chunk": "今天"},
                {"chunk": "多云"},
                {"done": True, "latency_ms": 1372},
            )
        )
        client = self.stream_client(resp)

        got = list(client.chat_stream(1, "天气怎么样"))
        self.assertEqual(got, ["武汉", "今天", "多云"])
        self.assertEqual(client.last_latency_ms, 1372)

    def test_stream_uses_small_chunk_size(self):
        """回归测试：读取粒度必须是 STREAM_READ_CHUNK，不能是 requests 的默认值。

        requests 的 `iter_lines()` 默认 `chunk_size=512`，而 `read(n)` 的语义是
        「攒够 n 字节再返回」——一个 SSE 事件只有几十字节，用默认值就会攒好几块
        才交出来一次，用户看到的是「文字一段一段蹦」。修掉过两次（服务端一次、
        前端一次），所以在这里钉一颗钉子。

        断言用 `< 128` 而不是 `== 1`：粒度小就够了，具体值允许调整；
        真正不能接受的是「退化成默认的 512」。
        """
        resp = _FakeResponse(lines=_sse({"chunk": "a"}, {"done": True, "latency_ms": 1}))
        client = self.stream_client(resp)
        list(client.chat_stream(1, "hi"))

        self.assertNotEqual(resp.seen_chunk_size, "NOT CALLED", "必须显式传 chunk_size")
        self.assertIsNotNone(resp.seen_chunk_size)
        self.assertLess(resp.seen_chunk_size, 128, f"读取粒度退化了：{resp.seen_chunk_size}")
        self.assertEqual(resp.seen_chunk_size, STREAM_READ_CHUNK)

    def test_error_event_raises(self):
        """流里推 error 事件要升级成异常，不能装作正常结束。"""
        resp = _FakeResponse(
            lines=_sse(
                {"chunk": "我先说半句"},
                {"error": "无法连接 Ollama 服务"},
                {"done": True},
            )
        )
        client = self.stream_client(resp)

        collected = []
        with self.assertRaises(ChatAPIError) as ctx:
            for piece in client.chat_stream(1, "hi"):
                collected.append(piece)

        # 已经产出的部分保留（界面上那半句该留在屏幕上，不该被抹掉）
        self.assertEqual(collected, ["我先说半句"])
        self.assertIn("Ollama", str(ctx.exception))

    def test_http_404_before_stream_raises_detail(self):
        """404 在流开始前就该被拦下，错误体是普通 JSON（不是 SSE）。"""
        resp = _FakeResponse(status=404, json_body={"detail": "会话不存在"})
        client = self.stream_client(resp)

        with self.assertRaises(ChatAPIError) as ctx:
            list(client.chat_stream(999, "hi"))
        self.assertEqual(str(ctx.exception), "会话不存在")

    def test_connection_error_maps_to_unreachable(self):
        """连不上后端要给出「去启动 API」这种可执行的提示，而不是抛裸异常。"""
        self.session.post.side_effect = requests.ConnectionError("refused")
        client = ChatAPIClient(base_url="http://127.0.0.1:8000")

        with self.assertRaises(APIUnreachable) as ctx:
            list(client.chat_stream(1, "hi"))
        self.assertIn("uvicorn", str(ctx.exception))

    def test_response_is_closed_on_early_abort(self):
        """用户中途停止（提前 break）也必须关闭响应，否则连接泄漏。

        这是生成器里 `finally` 的价值：调用方 break 时，生成器被 close，
        finally 照样执行。没有它，被放弃的连接会一直挂在池子里。
        """
        resp = _FakeResponse(
            lines=_sse(
                {"chunk": "第一块"},
                {"chunk": "第二块"},
                {"done": True, "latency_ms": 1},
            )
        )
        client = self.stream_client(resp)

        gen = client.chat_stream(1, "hi")
        self.assertEqual(next(gen), "第一块")
        gen.close()  # 模拟用户点「停止」

        self.assertTrue(resp.closed, "提前中断后响应必须被关闭")

    def test_response_is_closed_on_normal_end(self):
        resp = _FakeResponse(lines=_sse({"chunk": "a"}, {"done": True, "latency_ms": 5}))
        client = self.stream_client(resp)
        list(client.chat_stream(1, "hi"))
        self.assertTrue(resp.closed)

    def test_multiline_data_event_is_joined(self):
        """SSE 规范允许一个事件有多行 data，要拼成一个载荷而不是拆成两个。"""
        body = [
            'data: {"chunk":',
            'data:  "hello"}',
            "",
            'data: {"done": true, "latency_ms": 3}',
            "",
        ]
        resp = _FakeResponse(lines=body)
        client = self.stream_client(resp)
        self.assertEqual(list(client.chat_stream(1, "hi")), ["hello"])

    def test_comment_and_unknown_fields_ignored(self):
        """`: 心跳` 和 event:/id: 字段要忽略，不能污染 chunk。"""
        body = [
            ": keep-alive",
            "event: message",
            "id: 42",
            'data: {"chunk": "x"}',
            "",
            'data: {"done": true, "latency_ms": 2}',
            "",
        ]
        resp = _FakeResponse(lines=body)
        client = self.stream_client(resp)
        self.assertEqual(list(client.chat_stream(1, "hi")), ["x"])

    def test_trailing_event_without_blank_line_is_not_lost(self):
        """对端直接断开、没发最后一个空行时，最后一块也不能丢。"""
        resp = _FakeResponse(lines=['data: {"chunk": "结尾"}'])
        client = self.stream_client(resp)
        self.assertEqual(list(client.chat_stream(1, "hi")), ["结尾"])

    def test_bytes_lines_are_decoded(self):
        """decode_unicode 在某些版本/场景下仍可能给 bytes，要能兜住。"""
        resp = _FakeResponse(
            raw_lines=[b'data: {"chunk": "\\u4f60\\u597d"}', b"", b'data: {"done": true}']
        )
        client = self.stream_client(resp)
        self.assertEqual(list(client.chat_stream(1, "hi")), ["你好"])

    def test_non_json_data_line_is_skipped(self):
        """data 行不是 JSON 时跳过，不能把整个流打断。"""
        resp = _FakeResponse(
            lines=["data: 这不是JSON", "", 'data: {"chunk": "ok"}', "", 'data: {"done": true}']
        )
        client = self.stream_client(resp)
        self.assertEqual(list(client.chat_stream(1, "hi")), ["ok"])

    def test_latency_reset_between_calls(self):
        """上一次的耗时不能残留到下一次（否则界面会显示过期的数字）。"""
        resp = _FakeResponse(lines=_sse({"done": True, "latency_ms": 999}))
        client = self.stream_client(resp)
        list(client.chat_stream(1, "a"))
        self.assertEqual(client.last_latency_ms, 999)

        # 第二次请求：流里没有 done 事件（比如被中途中断）
        self.session.post.return_value = _FakeResponse(lines=[":"])
        list(client.chat_stream(1, "b"))
        self.assertIsNone(client.last_latency_ms)


class TestNonStreamEndpoints(_ClientBase):
    """非流式接口：路径、参数、错误映射。"""

    def test_health_does_not_raise_when_degraded(self):
        """`/health` 永远 200（把依赖状态写在 body 里），所以不能因为
        status=degraded 就抛异常——那不是调用失败，是「可用但有依赖挂了」。"""
        resp = _FakeResponse(
            json_body={
                "status": "degraded",
                "checks": {"database": True, "redis": False, "ollama": True},
            }
        )
        client = self.json_client(resp)

        body = client.health()
        self.assertEqual(body["status"], "degraded")
        self.assertFalse(body["checks"]["redis"])

    def test_ready_false_on_503(self):
        """就绪探针返回 503 时应得到 False，而不是抛异常。"""
        resp = _FakeResponse(status=503, json_body={"detail": "not ready"})
        client = self.json_client(resp)
        self.assertFalse(client.ready())

    def test_list_messages_passes_cursor(self):
        """游标分页参数要真的带上（否则「加载更多」会一直返回同一页）。"""
        resp = _FakeResponse(json_body=[])
        client = self.json_client(resp)
        client.list_messages(7, limit=20, before_id=42)

        _, kwargs = self.session.request.call_args
        self.assertEqual(kwargs["params"], {"limit": 20, "before_id": 42})

    def test_list_messages_omits_cursor_when_none(self):
        """首次加载不带游标——带 None 会让后端把它当成一个真实的 id 过滤条件。"""
        resp = _FakeResponse(json_body=[])
        client = self.json_client(resp)
        client.list_messages(7)

        _, kwargs = self.session.request.call_args
        self.assertEqual(kwargs["params"], {"limit": 50})

    def test_clear_messages_returns_count(self):
        resp = _FakeResponse(json_body={"conversation_id": 3, "deleted": 12})
        client = self.json_client(resp)
        self.assertEqual(client.clear_messages(3), 12)

    def test_update_conversation_sends_only_given_fields(self):
        """换模型时只能发 model_id，不能顺手把 title 也发过去（那会覆盖别人的修改）。"""
        resp = _FakeResponse(json_body={"id": 3})
        client = self.json_client(resp)
        client.update_conversation(3, model_id=2)

        args, kwargs = self.session.request.call_args
        self.assertEqual(args[0], "PATCH")
        self.assertEqual(kwargs["json"], {"model_id": 2})

    def test_connection_error_on_json_call(self):
        self.session.request.side_effect = requests.ConnectionError("refused")
        client = ChatAPIClient(base_url="http://127.0.0.1:8000")
        with self.assertRaises(APIUnreachable):
            client.list_models()

    def test_http_error_carries_detail(self):
        resp = _FakeResponse(status=400, json_body={"detail": "模型不存在"})
        client = self.json_client(resp)
        with self.assertRaises(ChatAPIError) as ctx:
            client.update_conversation(1, model_id=999)
        self.assertEqual(str(ctx.exception), "模型不存在")

    def test_204_returns_none(self):
        """204 没有 body，不能去解析 JSON（会抛异常）。"""
        resp = _FakeResponse(status=204)
        resp.content = b""
        client = self.json_client(resp)
        self.assertIsNone(client.create_conversation("t", 1, 1))


class TestExtractDetail(unittest.TestCase):
    """错误体解析：FastAPI 有两种错误形状，都得变成一句人话。"""

    def test_string_detail(self):
        resp = _FakeResponse(json_body={"detail": "会话不存在"})
        self.assertEqual(_extract_detail(resp), "会话不存在")

    def test_validation_error_list_detail(self):
        """422 的 detail 是列表；直接 str() 会打出一大坨 Python 字面量。"""
        resp = _FakeResponse(
            json_body={
                "detail": [
                    {
                        "loc": ["body", "content"],
                        "msg": "String should have at least 1 character",
                        "type": "string_too_short",
                    }
                ]
            }
        )
        detail = _extract_detail(resp)
        self.assertIn("at least 1 character", detail)
        self.assertNotIn("loc", detail)

    def test_non_json_body_falls_back_to_status(self):
        resp = _FakeResponse(status=502)
        self.assertEqual(_extract_detail(resp), "HTTP 502")


if __name__ == "__main__":
    unittest.main()
