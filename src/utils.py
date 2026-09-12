"""通用工具函数模块。"""

import re
from datetime import datetime
from typing import List, Dict, Any


def format_timestamp(dt: datetime | None = None) -> str:
    """格式化时间戳为可读字符串。"""
    if dt is None:
        dt = datetime.now()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def truncate_text(text: str, max_length: int = 200) -> str:
    """截断过长文本并添加省略号。"""
    if len(text) <= max_length:
        return text
    return text[:max_length].rstrip() + "..."


def count_words(text: str) -> int:
    """统计文本中的词数（中文按字符计，英文按空格分词）。"""
    # 移除多余空白
    text = text.strip()
    if not text:
        return 0
    # 简单统计：中文字符 + 英文单词
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    return chinese_chars + english_words


def extract_code_blocks(text: str) -> List[Dict[str, str]]:
    """从 Markdown 文本中提取代码块。

    返回列表，每个元素包含 language 和 code 字段。
    """
    pattern = r"```(\w*)\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    return [
        {"language": lang if lang else "text", "code": code.strip()}
        for lang, code in matches
    ]


def sanitize_model_name(name: str) -> str:
    """清理模型名称，移除版本标签中的危险字符。"""
    return re.sub(r"[^a-zA-Z0-9._\-:/]", "", name)


def build_conversation_context(messages: List[Dict[str, str]], max_context: int = 10) -> List[Dict[str, str]]:
    """构建对话上下文，保留最近的 N 条消息。

    Args:
        messages: 完整消息列表，每条包含 role 和 content。
        max_context: 最大保留消息数。

    Returns:
        裁剪后的消息列表。
    """
    if len(messages) <= max_context:
        return list(messages)
    # 始终保留系统提示（如果有），然后取最近的消息
    system_msgs = [m for m in messages if m.get("role") == "system"]
    recent = messages[-max_context:]
    # 避免重复包含 system 消息
    recent_non_system = [m for m in recent if m.get("role") != "system"]
    return system_msgs + recent_non_system


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数量（约 1 token ≈ 4 英文字符 ≈ 1.5 中文字符）。"""
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)
