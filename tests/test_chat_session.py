"""会话抽象层测试：把「界面会怎么调用数据」变成可断言的代码。

为什么这一层值得单独写测试：`app.py` 是 Streamlit 代码，它依赖 st.* 的运行时
（页面上下文、session_state），**没法写单元测试**。所以「界面点一下会发生什么」
这件事，只能通过它对数据源的调用来验证——也就是这一层。

换句话说：把界面逻辑收敛进 ChatSession，顺手就把界面的行为变成了可测的。
这是「抽象」最实际的收益，不只是「代码好看」。

两个实现分开测，重点不同：
- LocalChatSession 测「语义有没有变」，因为它要**完全兼容改造前的行为**；
- APIChatSession 测「和 HTTP 层的契约对不对」，尤其是那些 id/name 转换、
  以及失败时的降级（后端挂了不能让页面崩掉）。
"""

import unittest
from unittest.mock import MagicMock, patch

from src.api_client import APIUnreachable, ChatAPIError
from src.chatbot import ChatBot
from src.chat_session import APIChatSession, LocalChatSession, create_session


def _fake_chatbot(chunks=None, models=None, fail_after=None):
    """造一个 ChatBot，只把最底层的 OllamaClient 换成假的。

    注意换的是 `client.chat_stream`（真正发 HTTP 的那一层），**不是**
    `chatbot.generate_response_stream`。这个区别很重要：后者是领域逻辑
    （往消息列表里记 user / assistant），如果把它也替换掉，测出来的就不是
    真实行为了——第一版测试替身就是这么写的，结果「发完一轮历史应该有两条」
    直接失败，因为消息序列压根没人维护。**替身应该换掉外部依赖，
    而不是换掉被测对象本身。**

    `fail_after=n` 表示流到第 n 块时抛异常，用来验证「失败要撤回提问」。
    """
    client = MagicMock()
    client.config.model = "llama3.2"
    client.generation_config = MagicMock()

    chunks = chunks or ["你好"]

    def fake_stream(messages=None, model=None, **kwargs):
        for i, c in enumerate(chunks):
            if fail_after is not None and i == fail_after:
                raise ConnectionError("Ollama 掉线了")
            yield c

    client.chat_stream.side_effect = fake_stream
    client.get_model_names.return_value = models or ["llama3.2"]
    client.check_health.return_value = True
    return ChatBot(client=client)


class TestLocalChatSession(unittest.TestCase):
    """直连模式：必须和改造前的行为一模一样。"""

    def test_send_streams_chunks(self):
        session = LocalChatSession(_fake_chatbot(chunks=["武", "汉"]))
        self.assertEqual(list(session.send("天气")), ["武", "汉"])

    def test_history_grows_with_conversation(self):
        """发完一轮后，历史里应该是「用户 + 助手」两条。

        这是直连模式的语义：前端自己维护消息序列（后端模式里这件事归服务端管）。
        """
        session = LocalChatSession(_fake_chatbot(chunks=["回答"]))
        list(session.send("问题"))

        roles = [m["role"] for m in session.history()]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertEqual(session.history()[0]["content"], "问题")

    def test_failed_stream_rolls_back_user_message(self):
        """生成失败要撤回那句没得到回答的提问。

        不撤的话用户重试一次，上下文里就有两句一样的提问，模型的回答开始串味。
        这条行为原来写在界面里（app.py 直接 pop 私有列表），现在归会话层。
        """
        session = LocalChatSession(_fake_chatbot(chunks=["一", "二"], fail_after=1))

        with self.assertRaises(ConnectionError):
            list(session.send("会失败的问题"))

        self.assertEqual(session.history(), [])

    def test_clear_empties_history(self):
        session = LocalChatSession(_fake_chatbot(chunks=["回答"]))
        list(session.send("问题"))
        session.clear()
        self.assertEqual(session.history(), [])

    def test_totals_counts_messages_and_tokens(self):
        """统计口径：两种数据源都用同一套估算，数字不会因为换数据源而变。"""
        session = LocalChatSession(_fake_chatbot(chunks=["你好世界"]))
        list(session.send("你好"))

        count, tokens = session.totals()
        self.assertEqual(count, 2)
        self.assertGreater(tokens, 0)

    def test_export_includes_system_prompt(self):
        """导出格式要和改造前一致（system 打头），否则老的导出文件读不回来。"""
        session = LocalChatSession(_fake_chatbot())
        exported = session.export()
        self.assertEqual(exported[0]["role"], "system")

    def test_configure_writes_generation_params(self):
        bot = _fake_chatbot()
        session = LocalChatSession(bot)
        session.configure(0.7, 0.9, 512)

        gen = bot.client.generation_config
        self.assertEqual(gen.temperature, 0.7)
        self.assertEqual(gen.top_p, 0.9)
        self.assertEqual(gen.max_tokens, 512)

    def test_can_configure_true(self):
        self.assertTrue(LocalChatSession(_fake_chatbot()).can_configure())

    def test_health_reports_failure_message(self):
        """Ollama 连不上时要给出可执行的一句话，而不是抛异常。

        界面靠这个布尔值决定显示绿条还是红条——所以它**不该**抛异常，
        「连不上」是一种需要展示的正常状态。
        """
        bot = _fake_chatbot()
        bot.client.check_health = MagicMock(return_value=False)
        ok, message = LocalChatSession(bot).health()
        self.assertFalse(ok)
        self.assertIn("ollama serve", message)

    def test_health_swallows_exceptions(self):
        """health() 内部任何异常都等价于「连不上」，不能让它冒到界面上。"""
        bot = _fake_chatbot()
        bot.client.check_health = MagicMock(side_effect=RuntimeError("boom"))
        ok, _ = LocalChatSession(bot).health()
        self.assertFalse(ok)

    def test_last_latency_is_none(self):
        """直连模式拿不到服务端的耗时，如实返回 None（界面据此不显示那行）。"""
        self.assertIsNone(LocalChatSession(_fake_chatbot()).last_latency_ms)


def _api_client(**overrides):
    """造一个 ChatAPIClient 替身。"""
    client = MagicMock()
    client.list_models.return_value = [
        {"id": 1, "name": "llama3.2"},
        {"id": 2, "name": "qwen2:0.5b"},
    ]
    client.list_users.return_value = [{"id": 7, "username": "alice"}]
    client.list_conversations.return_value = []
    client.list_messages.return_value = []
    client.get_conversation.return_value = {"id": 5, "model_id": 1}
    client.create_conversation.return_value = {"id": 42}
    client.health.return_value = {
        "status": "ok",
        "checks": {"database": True, "redis": True, "ollama": True},
    }
    client.last_latency_ms = None
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


class TestAPIChatSession(unittest.TestCase):
    """后端模式：和 HTTP 层的契约、以及失败时的降级。"""

    def test_refresh_models_builds_name_to_id_map(self):
        """模型必须同时拿到「名字」（界面显示）和「id」（提交给后端）。"""
        session = APIChatSession(_api_client())
        names = session.refresh_models()

        self.assertEqual(names, ["llama3.2", "qwen2:0.5b"])
        self.assertEqual(session._model_ids["qwen2:0.5b"], 2)

    def test_create_conversation_passes_resolved_user(self):
        """user_id 未指定时要先解析出一个真实存在的用户。

        不解析的话，随便传的 user_id 会撞外键约束，返回一个没有上下文的 500
        （错误信息完全指不出「是没有这个用户」）。
        """
        client = _api_client()
        session = APIChatSession(client, user_id=None)

        conv_id = session.create_conversation("聊聊", "llama3.2")

        self.assertEqual(conv_id, 42)
        _, kwargs = client.create_conversation.call_args
        self.assertEqual(kwargs["user_id"], 7)
        self.assertEqual(kwargs["model_id"], 1)
        self.assertEqual(kwargs["title"], "聊聊")

    def test_create_conversation_without_any_user_gives_readable_error(self):
        """库里一个用户都没有时，要报「去初始化数据」，而不是撞外键约束。"""
        client = _api_client()
        client.list_users.return_value = []
        session = APIChatSession(client, user_id=None)

        with self.assertRaises(ChatAPIError) as ctx:
            session.create_conversation("聊聊", "llama3.2")
        self.assertIn("init_db", str(ctx.exception))

    def test_current_model_maps_id_back_to_name(self):
        """会话里存的是 model_id，界面要显示名字——这层转换是前端最容易被忽略的活。"""
        client = _api_client()
        session = APIChatSession(client, user_id=7, conversation_id=5)
        session.refresh_models()

        # 会话绑定 model_id=2
        client.get_conversation.return_value = {"id": 5, "model_id": 2}
        self.assertEqual(session.current_model(), "qwen2:0.5b")

    def test_current_model_never_raises_when_backend_down(self):
        """后端不可用时 current_model 必须返回空字符串，不能抛异常。

        这是界面测试抓出来的真实缺陷的回归用例。当时的现象是：切到「后端 API」
        数据源、而后端没启动，整页变成 Streamlit 的红色报错页——只因为标题栏
        要显示一行「当前模型」。

        修法就是让这个方法永不抛异常，而守它的位置放在这里（毫秒级），
        比靠界面测试守（每个用例几秒）划算得多。**界面测试用来发现「页面级」
        的问题，发现之后就该把它降级成单元测试守住。**
        """
        client = _api_client()
        client.list_models.side_effect = APIUnreachable("连不上")
        session = APIChatSession(client, user_id=7)
        self.assertEqual(session.current_model(), "")

        client2 = _api_client()
        client2.get_conversation.side_effect = ChatAPIError("会话没了")
        session2 = APIChatSession(client2, user_id=7, conversation_id=5)
        self.assertEqual(session2.current_model(), "")

    def test_set_model_updates_conversation(self):
        """换模型 = 改会话的 model_id（会写库的操作，不是改内存状态）。"""
        client = _api_client()
        session = APIChatSession(client, user_id=7, conversation_id=5)
        session.refresh_models()

        session.set_model("qwen2:0.5b")
        client.update_conversation.assert_called_once_with(5, model_id=2)

    def test_set_model_ignored_without_conversation(self):
        """没有会话时切模型应该是 no-op，不能抛异常（界面上控件还画得出来）。"""
        client = _api_client()
        session = APIChatSession(client, user_id=7)
        session.refresh_models()
        session.set_model("qwen2:0.5b")
        client.update_conversation.assert_not_called()

    def test_history_filters_out_system_role(self):
        """界面只渲染 user/assistant；system 消息不该出现在聊天气泡里。"""
        client = _api_client()
        client.list_messages.return_value = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ]
        session = APIChatSession(client, user_id=7, conversation_id=5)

        history = session.history()
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])

    def test_history_returns_empty_when_backend_errors(self):
        """会话被删 / 后端挂了：显示为空，而不是让整个页面崩掉。

        这条是「界面健壮性」的下限：一个会话查不到，不该导致用户
        连侧边栏都用不了。
        """
        client = _api_client()
        client.list_messages.side_effect = ChatAPIError("会话不存在")
        session = APIChatSession(client, user_id=7, conversation_id=5)
        self.assertEqual(session.history(), [])

    def test_clear_calls_delete_endpoint(self):
        client = _api_client()
        session = APIChatSession(client, user_id=7, conversation_id=5)
        session.clear()
        client.clear_messages.assert_called_once_with(5)

    def test_send_delegates_to_stream(self):
        """send 不做任何本地加工：用户消息和助手回复都由服务端落库。"""
        client = _api_client()
        client.chat_stream.return_value = iter(["你", "好"])
        session = APIChatSession(client, user_id=7, conversation_id=5)

        self.assertEqual(list(session.send("hi")), ["你", "好"])
        client.chat_stream.assert_called_once_with(5, "hi")

    def test_send_without_conversation_raises(self):
        session = APIChatSession(_api_client(), user_id=7)
        with self.assertRaises(ChatAPIError):
            list(session.send("hi"))

    def test_cannot_configure_generation_params(self):
        """后端模式下调参数是显式的「不支持」，界面据此置灰控件。

        做成显式能力（can_configure）而不是静默失败：用户调半天没反应，
        比控件是灰的、旁边写了一行原因，体验差得多。
        """
        session = APIChatSession(_api_client(), user_id=7, conversation_id=5)
        self.assertFalse(session.can_configure())

    def test_system_prompt_is_read_only(self):
        """后端模式下提示词不可改：set 是空操作，get 返回一句说明。"""
        session = APIChatSession(_api_client(), user_id=7, conversation_id=5)
        session.set_system_prompt("新的提示词")  # 不应抛异常
        self.assertIn("服务端", session.system_prompt)

    def test_health_ok(self):
        session = APIChatSession(_api_client(), user_id=7)
        ok, message = session.health()
        self.assertTrue(ok)
        self.assertIn("正常", message)

    def test_health_reports_which_dependency_is_down(self):
        """依赖挂掉时要指出是哪个——「服务异常」这种提示等于没说。"""
        client = _api_client()
        client.health.return_value = {
            "status": "degraded",
            "checks": {"database": True, "redis": False, "ollama": True},
        }
        ok, message = APIChatSession(client, user_id=7).health()
        self.assertFalse(ok)
        self.assertIn("redis", message)

    def test_health_handles_unreachable_backend(self):
        client = _api_client()
        client.health.side_effect = ChatAPIError("无法连接后端服务")
        ok, message = APIChatSession(client, user_id=7).health()
        self.assertFalse(ok)
        self.assertIn("无法连接", message)

    def test_last_latency_forwarded(self):
        """耗时以服务端为准（它才是真正调模型的那一环），前端只是转发。"""
        client = _api_client()
        client.last_latency_ms = 1372
        session = APIChatSession(client, user_id=7, conversation_id=5)
        self.assertEqual(session.last_latency_ms, 1372)

    def test_list_conversations_resolves_user_first(self):
        """user_id 为 None 时也要能列会话（先解析再查）。"""
        client = _api_client()
        client.list_conversations.return_value = [{"id": 1, "title": "T"}]
        session = APIChatSession(client, user_id=None)

        session.list_conversations()
        client.list_conversations.assert_called_once_with(7)


class TestFactory(unittest.TestCase):
    """create_session：界面只调这一个函数。"""

    def test_creates_local_by_default(self):
        with patch("src.chat_session.ChatBot"):
            session = create_session("local", local_chatbot=MagicMock())
        self.assertIsInstance(session, LocalChatSession)

    def test_creates_api(self):
        session = create_session("api", client=_api_client(), user_id=3)
        self.assertIsInstance(session, APIChatSession)
        self.assertEqual(session.user_id, 3)

    def test_local_requires_chatbot(self):
        with self.assertRaises(ValueError):
            create_session("local")


if __name__ == "__main__":
    unittest.main()
