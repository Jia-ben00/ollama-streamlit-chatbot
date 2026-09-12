"""应用配置管理模块。

通过环境变量和 .env 文件加载配置，提供类型安全的配置访问。
"""

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

# 加载 .env 文件（如果存在）
load_dotenv()


def _get_env(key: str, default: str = "") -> str:
    """读取环境变量，去除首尾空白。"""
    return os.getenv(key, default).strip()


def _get_env_float(key: str, default: float) -> float:
    """读取浮点型环境变量，失败时返回默认值。"""
    raw = _get_env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_env_int(key: str, default: int) -> int:
    """读取整型环境变量，失败时返回默认值。"""
    raw = _get_env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class OllamaConfig:
    """Ollama 服务连接配置。"""

    base_url: str = field(default_factory=lambda: _get_env("OLLAMA_BASE_URL", "http://localhost:11434"))
    model: str = field(default_factory=lambda: _get_env("OLLAMA_MODEL", "llama3.2"))
    timeout: int = field(default_factory=lambda: _get_env_int("OLLAMA_TIMEOUT", 120))


@dataclass
class GenerationConfig:
    """文本生成参数配置。"""

    temperature: float = field(default_factory=lambda: _get_env_float("TEMPERATURE", 0.7))
    top_p: float = field(default_factory=lambda: _get_env_float("TOP_P", 0.9))
    max_tokens: int = field(default_factory=lambda: _get_env_int("MAX_TOKENS", 2048))


@dataclass
class AppConfig:
    """应用整体配置。"""

    title: str = field(default_factory=lambda: _get_env("APP_TITLE", "Ollama 智能聊天助手"))
    theme: str = field(default_factory=lambda: _get_env("APP_THEME", "light"))
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)


# 全局配置单例
_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """获取全局配置单例。"""
    global _config
    if _config is None:
        _config = AppConfig()
    return _config


def reload_config() -> AppConfig:
    """重新加载配置（用于运行时参数变更）。"""
    global _config
    load_dotenv(override=True)
    _config = AppConfig()
    return _config
