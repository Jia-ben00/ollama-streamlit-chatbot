"""Ollama API 客户端模块。

封装与本地 Ollama 服务的通信，支持模型列表查询、流式聊天和普通聊天。
"""

import json
from typing import List, Dict, Any, Iterator, Optional

import requests

from src.config import OllamaConfig, GenerationConfig


class OllamaClient:
    """Ollama REST API 客户端。"""

    def __init__(
        self,
        config: Optional[OllamaConfig] = None,
        generation_config: Optional[GenerationConfig] = None,
    ):
        self.config = config or OllamaConfig()
        self.generation_config = generation_config or GenerationConfig()
        self._session = requests.Session()

    @property
    def base_url(self) -> str:
        return self.config.base_url.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json"}

    def list_models(self) -> List[Dict[str, Any]]:
        """获取本地可用模型列表。

        Returns:
            模型信息列表，每个元素包含 name、size、modified_at 等字段。

        Raises:
            ConnectionError: 无法连接 Ollama 服务。
            requests.HTTPError: HTTP 请求失败。
        """
        url = f"{self.base_url}/api/tags"
        try:
            resp = self._session.get(url, timeout=self.config.timeout)
            resp.raise_for_status()
            data = resp.json()
            return data.get("models", [])
        except requests.ConnectionError as exc:
            raise ConnectionError(
                f"无法连接 Ollama 服务 ({self.base_url})，请确认 Ollama 已启动。"
            ) from exc

    def get_model_names(self) -> List[str]:
        """获取可用模型名称列表。"""
        models = self.list_models()
        return [m.get("name", "") for m in models if m.get("name")]

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """发送聊天请求并返回完整回复。

        Args:
            messages: 对话消息列表，格式为 [{"role": "user"/"assistant"/"system", "content": "..."}]。
            model: 模型名称，默认使用配置中的模型。
            temperature: 采样温度。
            top_p: 核采样参数。
            max_tokens: 最大生成 token 数。

        Returns:
            模型生成的回复文本。

        Raises:
            ConnectionError: 无法连接 Ollama 服务。
            ValueError: 请求参数错误。
        """
        url = f"{self.base_url}/api/chat"
        payload = self._build_payload(messages, model, temperature, top_p, max_tokens)
        payload["stream"] = False

        try:
            resp = self._session.post(
                url, json=payload, headers=self._headers(), timeout=self.config.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")
        except requests.ConnectionError as exc:
            raise ConnectionError(
                f"无法连接 Ollama 服务 ({self.base_url})，请确认 Ollama 已启动。"
            ) from exc
        except requests.HTTPError as exc:
            detail = self._extract_error_detail(resp)
            raise ValueError(f"Ollama API 错误: {detail}") from exc

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Iterator[str]:
        """发送流式聊天请求，逐块 yield 回复文本。

        Args:
            messages: 对话消息列表。
            model: 模型名称。
            temperature: 采样温度。
            top_p: 核采样参数。
            max_tokens: 最大生成 token 数。

        Yields:
            每块回复文本片段。

        Raises:
            ConnectionError: 无法连接 Ollama 服务。
            ValueError: 请求参数错误。
        """
        url = f"{self.base_url}/api/chat"
        payload = self._build_payload(messages, model, temperature, top_p, max_tokens)
        payload["stream"] = True

        try:
            resp = self._session.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self.config.timeout,
                stream=True,
            )
            resp.raise_for_status()

            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if chunk.get("done") and not chunk.get("message", {}).get("content"):
                    break

                content = chunk.get("message", {}).get("content", "")
                if content:
                    yield content

                if chunk.get("done"):
                    break

        except requests.ConnectionError as exc:
            raise ConnectionError(
                f"无法连接 Ollama 服务 ({self.base_url})，请确认 Ollama 已启动。"
            ) from exc
        except requests.HTTPError as exc:
            raise ValueError(f"Ollama API 错误: HTTP {resp.status_code}") from exc

    def check_health(self) -> bool:
        """检查 Ollama 服务是否可用。"""
        try:
            resp = self._session.get(
                f"{self.base_url}/api/tags", timeout=5
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _build_payload(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str],
        temperature: Optional[float],
        top_p: Optional[float],
        max_tokens: Optional[int],
    ) -> Dict[str, Any]:
        """构建 API 请求 payload。"""
        return {
            "model": model or self.config.model,
            "messages": messages,
            "options": {
                "temperature": temperature if temperature is not None else self.generation_config.temperature,
                "top_p": top_p if top_p is not None else self.generation_config.top_p,
                "num_predict": max_tokens if max_tokens is not None else self.generation_config.max_tokens,
            },
        }

    @staticmethod
    def _extract_error_detail(resp: requests.Response) -> str:
        """从错误响应中提取可读的错误信息。"""
        try:
            data = resp.json()
            return data.get("error", f"HTTP {resp.status_code}")
        except (json.JSONDecodeError, ValueError):
            return f"HTTP {resp.status_code}: {resp.text[:200]}"
