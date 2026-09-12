"""聊天机器人核心逻辑单元测试。

使用 mock 替代真实 Ollama API 调用，确保测试可离线运行。
"""

import json
import unittest
from unittest.mock import patch, MagicMock

from src.chatbot import ChatBot, DEFAULT_SYSTEM_PROMPT
from src.ollama_client import OllamaClient
from src.utils import (
    truncate_text,
    count_words,
    extract_code_blocks,
    sanitize_model_name,
    build_conversation_context,
    estimate_tokens,
)


class TestUtils(unittest.TestCase):
    """工具函数测试。"""

    def test_truncate_text_short(self):
        self.assertEqual(truncate_text("hello", 10), "hello")

    def test_truncate_text_long(self):
        result = truncate_text("a" * 300, 200)
        self.assertTrue(result.endswith("..."))
        self.assertEqual(len(result), 203)

    def test_count_words_empty(self):
        self.assertEqual(count_words(""), 0)

    def test_count_words_chinese(self):
        self.assertEqual(count_words("你好世界"), 4)

    def test_count_words_english(self):
        self.assertEqual(count_words("hello world"), 2)

    def test_extract_code_blocks(self):
        text = "Here is code:\n```python\nprint('hi')\n```\nDone"
        blocks = extract_code_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["language"], "python")
        self.assertEqual(blocks[0]["code"], "print('hi')")

    def test_extract_code_blocks_multiple(self):
        text = "```js\nvar a=1;\n```\n```py\nprint(1)\n```"
        blocks = extract_code_blocks(text)
        self.assertEqual(len(blocks), 2)

    def test_sanitize_model_name(self):
        self.assertEqual(sanitize_model_name("llama3.2:latest"), "llama3.2:latest")
        self.assertEqual(sanitize_model_name("model; rm -rf /"), "modelrm-rf/")

    def test_build_conversation_context_under_limit(self):
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(5)]
        result = build_conversation_context(msgs, max_context=10)
        self.assertEqual(len(result), 5)

    def test_build_conversation_context_over_limit(self):
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(15)]
        result = build_conversation_context(msgs, max_context=10)
        self.assertEqual(len(result), 10)
        self.assertEqual(result[0]["content"], "msg5")

    def test_build_conversation_context_with_system(self):
        msgs = [{"role": "system", "content": "sys"}] + [
            {"role": "user", "content": f"msg{i}"} for i in range(15)
        ]
        result = build_conversation_context(msgs, max_context=10)
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(len(result), 11)  # system + 10 recent

    def test_estimate_tokens_empty(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_estimate_tokens_chinese(self):
        # 10 个中文字符 ≈ 6-7 tokens
        tokens = estimate_tokens("你好世界你好世界你好")
        self.assertGreater(tokens, 0)


class TestChatBot(unittest.TestCase):
    """聊天机器人测试（使用 mock 客户端）。"""

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.config = MagicMock()
        self.mock_client.config.model = "test-model"
        self.mock_client.generation_config = MagicMock()
        self.chatbot = ChatBot(client=self.mock_client)

    def test_initial_state(self):
        self.assertEqual(len(self.chatbot.messages), 0)
        self.assertEqual(self.chatbot.system_prompt, DEFAULT_SYSTEM_PROMPT)

    def test_add_user_message(self):
        self.chatbot.add_user_message("你好")
        self.assertEqual(len(self.chatbot.messages), 1)
        self.assertEqual(self.chatbot.messages[0]["role"], "user")

    def test_add_empty_message_ignored(self):
        self.chatbot.add_user_message("   ")
        self.assertEqual(len(self.chatbot.messages), 0)

    def test_clear_history(self):
        self.chatbot.add_user_message("test")
        self.chatbot.clear_history()
        self.assertEqual(len(self.chatbot.messages), 0)

    def test_set_model(self):
        self.chatbot.set_model("llama3.2:latest")
        self.assertEqual(self.chatbot.current_model, "llama3.2:latest")

    def test_set_system_prompt(self):
        self.chatbot.set_system_prompt("你是一个翻译官")
        self.assertEqual(self.chatbot.system_prompt, "你是一个翻译官")

    def test_set_empty_system_prompt_falls_back(self):
        self.chatbot.set_system_prompt("   ")
        self.assertEqual(self.chatbot.system_prompt, DEFAULT_SYSTEM_PROMPT)

    def test_generate_response(self):
        self.mock_client.chat.return_value = "你好！有什么可以帮你的？"
        response = self.chatbot.generate_response("你好")
        self.assertEqual(response, "你好！有什么可以帮你的？")
        self.assertEqual(len(self.chatbot.messages), 2)
        self.mock_client.chat.assert_called_once()

    def test_generate_response_includes_system_prompt(self):
        self.mock_client.chat.return_value = "ok"
        self.chatbot.generate_response("hi")
        call_args = self.mock_client.chat.call_args
        messages = call_args[1]["messages"] if "messages" in call_args[1] else call_args[0][0]
        self.assertEqual(messages[0]["role"], "system")

    def test_generate_response_stream(self):
        self.mock_client.chat_stream.return_value = iter(["你", "好", "！"])
        chunks = list(self.chatbot.generate_response_stream("hi"))
        self.assertEqual(chunks, ["你", "好", "！"])
        self.assertEqual(self.chatbot.messages[-1]["content"], "你好！")

    def test_reset(self):
        self.chatbot.add_user_message("test")
        self.chatbot.set_system_prompt("custom")
        self.chatbot.set_model("custom-model")
        self.chatbot.reset()
        self.assertEqual(len(self.chatbot.messages), 0)
        self.assertEqual(self.chatbot.system_prompt, DEFAULT_SYSTEM_PROMPT)

    def test_export_and_load_history(self):
        self.chatbot.add_user_message("问题")
        self.chatbot.add_assistant_message("回答")
        exported = self.chatbot.export_history()

        new_bot = ChatBot(client=self.mock_client)
        new_bot.load_history(exported)
        self.assertEqual(len(new_bot.messages), 2)
        self.assertEqual(new_bot.system_prompt, self.chatbot.system_prompt)

    def test_get_available_models_fallback(self):
        self.mock_client.get_model_names.side_effect = Exception("offline")
        models = self.chatbot.get_available_models()
        self.assertEqual(models, ["test-model"])


class TestOllamaClient(unittest.TestCase):
    """Ollama 客户端测试（mock HTTP 层）。"""

    def setUp(self):
        self.client = OllamaClient()

    @patch("src.ollama_client.requests.Session")
    def test_list_models_success(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session.get.return_value.status_code = 200
        mock_session.get.return_value.json.return_value = {
            "models": [{"name": "llama3.2", "size": 1234}]
        }
        mock_session_cls.return_value = mock_session

        client = OllamaClient()
        models = client.list_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["name"], "llama3.2")

    @patch("src.ollama_client.requests.Session")
    def test_chat_success(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session.post.return_value.status_code = 200
        mock_session.post.return_value.json.return_value = {
            "message": {"role": "assistant", "content": "Hello!"}
        }
        mock_session_cls.return_value = mock_session

        client = OllamaClient()
        response = client.chat([{"role": "user", "content": "Hi"}])
        self.assertEqual(response, "Hello!")

    @patch("src.ollama_client.requests.Session")
    def test_chat_connection_error(self, mock_session_cls):
        import requests as req
        mock_session = MagicMock()
        mock_session.post.side_effect = req.ConnectionError("refused")
        mock_session_cls.return_value = mock_session

        client = OllamaClient()
        with self.assertRaises(ConnectionError):
            client.chat([{"role": "user", "content": "Hi"}])

    def test_build_payload_defaults(self):
        payload = self.client._build_payload(
            [{"role": "user", "content": "hi"}],
            model=None,
            temperature=None,
            top_p=None,
            max_tokens=None,
        )
        self.assertEqual(payload["model"], self.client.config.model)
        self.assertIn("temperature", payload["options"])
        self.assertIn("top_p", payload["options"])
        self.assertIn("num_predict", payload["options"])

    def test_build_payload_overrides(self):
        payload = self.client._build_payload(
            [{"role": "user", "content": "hi"}],
            model="custom-model",
            temperature=0.5,
            top_p=0.8,
            max_tokens=1024,
        )
        self.assertEqual(payload["model"], "custom-model")
        self.assertEqual(payload["options"]["temperature"], 0.5)
        self.assertEqual(payload["options"]["top_p"], 0.8)
        self.assertEqual(payload["options"]["num_predict"], 1024)


if __name__ == "__main__":
    unittest.main()
