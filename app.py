"""Streamlit 网页版聊天机器人主应用。

功能特性：
- 多模型切换
- 系统提示词自定义
- 流式输出回复
- 对话历史管理（清空、导出）
- 生成参数调节（temperature、top_p、max_tokens）
- Ollama 服务健康状态检测
"""

import json
import sys
import os

# 确保项目根目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st

from src.config import get_config
from src.ollama_client import OllamaClient
from src.chatbot import ChatBot, DEFAULT_SYSTEM_PROMPT
from src.utils import format_timestamp, estimate_tokens


# ── 页面配置 ──────────────────────────────────────────────
config = get_config()
st.set_page_config(
    page_title=config.title,
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── 会话状态初始化 ────────────────────────────────────────
def init_session_state():
    """初始化 Streamlit 会话状态。"""
    if "chatbot" not in st.session_state:
        client = OllamaClient(
            config=config.ollama,
            generation_config=config.generation,
        )
        st.session_state.chatbot = ChatBot(client=client)
    if "ollama_available" not in st.session_state:
        st.session_state.ollama_available = None
    if "available_models" not in st.session_state:
        st.session_state.available_models = []


init_session_state()
chatbot: ChatBot = st.session_state.chatbot


# ── 侧边栏 ────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ 设置")

    # Ollama 连接状态
    st.subheader("服务状态")
    if st.button("🔄 检测连接", use_container_width=True):
        st.session_state.ollama_available = chatbot.client.check_health()
        if st.session_state.ollama_available:
            st.session_state.available_models = chatbot.get_available_models()
        else:
            st.session_state.available_models = []

    if st.session_state.ollama_available is True:
        st.success("✅ Ollama 服务已连接")
    elif st.session_state.ollama_available is False:
        st.error("❌ 无法连接 Ollama 服务")
        st.info("请确保本地已安装并启动 Ollama：\n```\nollama serve\n```")
    else:
        st.info("点击上方按钮检测连接")

    st.divider()

    # 模型选择
    st.subheader("模型设置")
    models = st.session_state.available_models or [config.ollama.model]
    selected_model = st.selectbox(
        "选择模型",
        options=models,
        index=0,
        help="从本地 Ollama 已安装的模型中选择",
    )
    if selected_model:
        chatbot.set_model(selected_model)

    st.divider()

    # 生成参数
    st.subheader("生成参数")
    temperature = st.slider(
        "Temperature（创造性）",
        min_value=0.0,
        max_value=2.0,
        value=float(config.generation.temperature),
        step=0.1,
        help="值越高回复越有创造性，越低越确定",
    )
    top_p = st.slider(
        "Top P（核采样）",
        min_value=0.1,
        max_value=1.0,
        value=float(config.generation.top_p),
        step=0.05,
        help="控制候选词的累积概率范围",
    )
    max_tokens = st.number_input(
        "Max Tokens（最大长度）",
        min_value=128,
        max_value=8192,
        value=int(config.generation.max_tokens),
        step=128,
        help="单次回复最大 token 数",
    )

    # 同步生成参数到客户端
    chatbot.client.generation_config.temperature = temperature
    chatbot.client.generation_config.top_p = top_p
    chatbot.client.generation_config.max_tokens = int(max_tokens)

    st.divider()

    # 系统提示词
    st.subheader("系统提示词")
    system_prompt = st.text_area(
        "自定义 AI 角色",
        value=chatbot.system_prompt,
        height=120,
        help="定义 AI 的行为风格和回答方式",
    )
    if st.button("应用提示词", use_container_width=True):
        chatbot.set_system_prompt(system_prompt)
        st.success("提示词已更新")

    st.divider()

    # 对话管理
    st.subheader("对话管理")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🗑️ 清空对话", use_container_width=True):
            chatbot.clear_history()
            st.rerun()
    with col2:
        if st.button("📥 导出记录", use_container_width=True):
            history_data = chatbot.export_history()
            st.download_button(
                label="下载 JSON",
                data=json.dumps(history_data, ensure_ascii=False, indent=2),
                file_name=f"chat_history_{format_timestamp().replace(' ', '_').replace(':', '-')}.json",
                mime="application/json",
                use_container_width=True,
            )

    # 统计信息
    st.divider()
    st.subheader("统计")
    msg_count = len(chatbot.messages)
    total_tokens = sum(estimate_tokens(m["content"]) for m in chatbot.messages)
    st.metric("消息数", msg_count)
    st.metric("预估 Token 数", total_tokens)


# ── 主界面 ────────────────────────────────────────────────
st.title(f"🤖 {config.title}")
st.caption(f"当前模型：`{chatbot.current_model}` ｜ 基于本地 Ollama 运行")

# 欢迎提示
if not chatbot.messages:
    st.info(
        "👋 欢迎！在下方输入框中开始对话吧。\n\n"
        "提示：请确保本地 Ollama 服务已启动（`ollama serve`），"
        "并已拉取模型（如 `ollama pull llama3.2`）。"
    )

# 消息展示区
for msg in chatbot.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 用户输入
user_input = st.chat_input("输入你的问题...", key="chat_input")

if user_input:
    # 显示用户消息
    with st.chat_message("user"):
        st.markdown(user_input)

    # 生成回复（流式）
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        full_response = ""

        try:
            for chunk in chatbot.generate_response_stream(user_input):
                full_response += chunk
                message_placeholder.markdown(full_response + "▌")
            message_placeholder.markdown(full_response)
        except ConnectionError as e:
            st.error(str(e))
            # 移除刚添加的用户消息，避免历史不一致
            if chatbot.messages and chatbot.messages[-1]["role"] == "user":
                chatbot._messages.pop()
        except Exception as e:
            st.error(f"生成回复时出错：{e}")
            if chatbot.messages and chatbot.messages[-1]["role"] == "user":
                chatbot._messages.pop()

    st.rerun()


# ── 页脚 ──────────────────────────────────────────────────
st.divider()
st.caption(
    "💡 本项目基于 Streamlit + Ollama 构建，所有推理均在本地完成，数据不上传云端。"
)
