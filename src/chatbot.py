"""聊天机器人核心逻辑模块。

管理对话状态、系统提示、消息历史，并协调 Ollama 客户端完成生成。
"""

from typing import List, Dict, Optional, Iterator

from src.ollama_client import OllamaClient
from src.utils import build_conversation_context, sanitize_model_name


DEFAULT_SYSTEM_PROMPT = (
    "你是一个友好、专业的 AI 助手。请用简洁清晰的中文回答用户的问题，"
    "如果用户使用其他语言，则用对应语言回复。回答时注重准确性和实用性，"
    "必要时可以分点说明。"
)


class ChatBot:
    """聊天机器人会话管理器。"""

    def __init__(
        self,
        client: Optional[OllamaClient] = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_context_messages: int = 20,
    ):
        self.client = client or OllamaClient()
        self.system_prompt = system_prompt
        self.max_context_messages = max_context_messages
        self._messages: List[Dict[str, str]] = []
        self._current_model: Optional[str] = None

    @property
    def messages(self) -> List[Dict[str, str]]:
        """返回完整消息历史（只读副本）。"""
        return list(self._messages)

    @property
    def current_model(self) -> str:
        return self._current_model or self.client.config.model

    def set_model(self, model: str) -> None:
        """切换当前使用的模型。"""
        self._current_model = sanitize_model_name(model)

    def set_system_prompt(self, prompt: str) -> None:
        """更新系统提示词。"""
        self.system_prompt = prompt.strip() or DEFAULT_SYSTEM_PROMPT

    def add_user_message(self, content: str) -> None:
        """添加用户消息到历史。"""
        content = content.strip()
        if content:
            self._messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        """添加助手消息到历史。"""
        if content:
            self._messages.append({"role": "assistant", "content": content})

    def clear_history(self) -> None:
        """清空对话历史。"""
        self._messages.clear()

    def reset(self) -> None:
        """完全重置会话状态。"""
        self.clear_history()
        self.system_prompt = DEFAULT_SYSTEM_PROMPT
        self._current_model = None

    def _build_api_messages(self) -> List[Dict[str, str]]:
        """构建发送给 API 的消息列表，包含系统提示和上下文裁剪。"""
        conversation = build_conversation_context(
            self._messages, max_context=self.max_context_messages
        )
        # 系统提示放在最前面
        return [{"role": "system", "content": self.system_prompt}] + conversation

    def generate_response(self, user_input: str) -> str:
        """同步生成回复。

        Args:
            user_input: 用户输入文本。

        Returns:
            助手回复文本。
        """
        self.add_user_message(user_input)
        api_messages = self._build_api_messages()

        response = self.client.chat(
            messages=api_messages,
            model=self._current_model,
        )
        self.add_assistant_message(response)
        return response

    def generate_response_stream(self, user_input: str) -> Iterator[str]:
        """流式生成回复，逐块 yield。

        Args:
            user_input: 用户输入文本。

        Yields:
            回复文本片段。
        """
        self.add_user_message(user_input)
        api_messages = self._build_api_messages()

        full_response = []
        for chunk in self.client.chat_stream(
            messages=api_messages,
            model=self._current_model,
        ):
            full_response.append(chunk)
            yield chunk

        self.add_assistant_message("".join(full_response))

    def get_available_models(self) -> List[str]:
        """获取可用模型列表。"""
        try:
            return self.client.get_model_names()
        except Exception:
            return [self.client.config.model]

    def export_history(self) -> List[Dict[str, str]]:
        """导出对话历史（包含系统提示）。"""
        return [{"role": "system", "content": self.system_prompt}] + self._messages

    def load_history(self, history: List[Dict[str, str]]) -> None:
        """从导出的历史中加载对话。"""
        self._messages.clear()
        for msg in history:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                self.system_prompt = content
            elif role in ("user", "assistant"):
                self._messages.append({"role": role, "content": content})
