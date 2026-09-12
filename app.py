"""Streamlit 网页版 AI 工具箱主应用。

包含两个功能模块：
1. 智能聊天 — 基于 Ollama 本地大模型的对话系统
2. 情感分析 — 基于 PyTorch BiLSTM 的文本情感分类
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
    page_title="AI 工具箱 - 聊天与情感分析",
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
    if "sentiment_predictor" not in st.session_state:
        st.session_state.sentiment_predictor = None


init_session_state()
chatbot: ChatBot = st.session_state.chatbot


# ── 侧边栏：模式选择 ──────────────────────────────────────
with st.sidebar:
    st.title("⚙️ 功能导航")
    mode = st.radio(
        "选择功能模块",
        options=["💬 智能聊天", "😊 情感分析"],
        index=0,
        help="切换不同的 AI 功能",
    )
    st.divider()


# ══════════════════════════════════════════════════════════
# 模块一：智能聊天
# ══════════════════════════════════════════════════════════
if mode == "💬 智能聊天":
    with st.sidebar:
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
        )
        top_p = st.slider(
            "Top P（核采样）",
            min_value=0.1,
            max_value=1.0,
            value=float(config.generation.top_p),
            step=0.05,
        )
        max_tokens = st.number_input(
            "Max Tokens（最大长度）",
            min_value=128,
            max_value=8192,
            value=int(config.generation.max_tokens),
            step=128,
        )
        chatbot.client.generation_config.temperature = temperature
        chatbot.client.generation_config.top_p = top_p
        chatbot.client.generation_config.max_tokens = int(max_tokens)

        st.divider()

        # 系统提示词
        st.subheader("系统提示词")
        system_prompt = st.text_area(
            "自定义 AI 角色",
            value=chatbot.system_prompt,
            height=100,
        )
        if st.button("应用提示词", use_container_width=True):
            chatbot.set_system_prompt(system_prompt)
            st.success("提示词已更新")

        st.divider()

        # 对话管理
        st.subheader("对话管理")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🗑️ 清空", use_container_width=True):
                chatbot.clear_history()
                st.rerun()
        with col2:
            if st.button("📥 导出", use_container_width=True):
                history_data = chatbot.export_history()
                st.download_button(
                    label="下载 JSON",
                    data=json.dumps(history_data, ensure_ascii=False, indent=2),
                    file_name=f"chat_history_{format_timestamp().replace(' ', '_').replace(':', '-')}.json",
                    mime="application/json",
                    use_container_width=True,
                )

        st.divider()
        st.subheader("统计")
        st.metric("消息数", len(chatbot.messages))
        st.metric("预估 Token", sum(estimate_tokens(m["content"]) for m in chatbot.messages))

    # ── 聊天主界面 ──
    st.title(f"💬 {config.title}")
    st.caption(f"当前模型：`{chatbot.current_model}` ｜ 基于本地 Ollama 运行")

    if not chatbot.messages:
        st.info(
            "👋 欢迎！在下方输入框中开始对话吧。\n\n"
            "提示：请确保本地 Ollama 服务已启动（`ollama serve`），"
            "并已拉取模型（如 `ollama pull llama3.2`）。"
        )

    for msg in chatbot.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    user_input = st.chat_input("输入你的问题...", key="chat_input")

    if user_input:
        with st.chat_message("user"):
            st.markdown(user_input)

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
                if chatbot.messages and chatbot.messages[-1]["role"] == "user":
                    chatbot._messages.pop()
            except Exception as e:
                st.error(f"生成回复时出错：{e}")
                if chatbot.messages and chatbot.messages[-1]["role"] == "user":
                    chatbot._messages.pop()
        st.rerun()

    st.divider()
    st.caption("💡 基于 Streamlit + Ollama 构建，所有推理均在本地完成。")


# ══════════════════════════════════════════════════════════
# 模块二：情感分析
# ══════════════════════════════════════════════════════════
else:  # mode == "😊 情感分析"
    with st.sidebar:
        st.subheader("模型信息")
        st.info(
            "**BiLSTM 情感分类模型**\n\n"
            "- 框架：PyTorch\n"
            "- 架构：Embedding + 双向 LSTM + 全连接层\n"
            "- 参数量：约 73.8 万\n"
            "- 测试集准确率：97.0%\n"
            "- 数据：1166 条影评标注数据"
        )
        st.divider()
        st.subheader("使用说明")
        st.markdown(
            "在右侧输入英文文本，模型将判断其情感倾向（正面/负面），"
            "并输出置信度概率。\n\n"
            "适用于影评、评论、反馈等文本的情感分析。"
        )

    # ── 情感分析主界面 ──
    st.title("😊 文本情感分析")
    st.caption("基于 PyTorch BiLSTM 模型 ｜ 本地推理 ｜ 二分类（正面/负面）")

    # 懒加载模型
    if st.session_state.sentiment_predictor is None:
        try:
            from sentiment_analysis.predict import SentimentPredictor
            with st.spinner("正在加载情感分析模型..."):
                st.session_state.sentiment_predictor = SentimentPredictor()
            st.success("✅ 模型加载成功")
        except Exception as e:
            st.error(f"模型加载失败：{e}")
            st.info("请先运行 `python -m sentiment_analysis.train` 训练模型。")
            st.stop()

    predictor = st.session_state.sentiment_predictor

    # 示例文本
    examples = [
        "This movie is absolutely wonderful and I loved every minute of it",
        "This movie is terrible and I hated every minute of it",
        "The acting was superb but the story was a bit slow",
        "I cannot believe I wasted two hours on this garbage",
        "A heartwarming tale that restores your faith in humanity",
        "One of the best films I have ever seen, truly inspiring",
        "A complete waste of time and money, avoid at all costs",
    ]

    # 输入区域
    st.subheader("📝 输入文本")
    input_text = st.text_area(
        "输入需要分析的英文文本（支持多行）",
        height=100,
        placeholder="例如：This movie is absolutely wonderful...",
        key="sentiment_input",
    )

    col_a, col_b, col_c = st.columns([1, 1, 3])
    with col_a:
        analyze_clicked = st.button("🔍 分析情感", use_container_width=True, type="primary")
    with col_b:
        example_idx = st.selectbox(
            "加载示例",
            options=range(len(examples)),
            format_func=lambda i: f"示例 {i+1}",
            key="example_selector",
        )
    with col_c:
        if st.button("📋 使用此示例", use_container_width=True):
            st.session_state.sentiment_input = examples[example_idx]
            st.rerun()

    # 分析结果
    if analyze_clicked and input_text.strip():
        with st.spinner("正在分析..."):
            result = predictor.predict(input_text.strip())

        st.subheader("📊 分析结果")

        # 结果卡片
        is_positive = result["predicted_label"] == 1
        emoji = "😊" if is_positive else "😞"
        label_text = "正面 (Positive)" if is_positive else "负面 (Negative)"
        color = "green" if is_positive else "red"

        col1, col2 = st.columns(2)
        with col1:
            st.metric("预测结果", f"{emoji} {label_text}")
        with col2:
            st.metric("置信度", f"{result['confidence']:.2%}")

        # 概率条形图
        st.subheader("类别概率分布")
        for cls_name, prob in result["probabilities"].items():
            is_pred = cls_name == result["predicted_class"]
            label = f"**{cls_name}**" if is_pred else cls_name
            st.markdown(f"{label}  —  `{prob:.2%}`")
            st.progress(prob)

        # 原始文本回显
        with st.expander("查看输入文本"):
            st.write(input_text.strip())

    elif analyze_clicked:
        st.warning("请先输入要分析的文本。")

    # 批量分析
    st.divider()
    with st.expander("📦 批量分析（每行一条）"):
        batch_text = st.text_area(
            "输入多条文本，每行一条",
            height=120,
            placeholder="文本1\n文本2\n文本3",
        )
        if st.button("批量分析", use_container_width=True):
            if batch_text.strip():
                lines = [l.strip() for l in batch_text.strip().split("\n") if l.strip()]
                results = predictor.predict_batch(lines)
                st.dataframe(
                    [
                        {
                            "文本": r["text"][:50] + ("..." if len(r["text"]) > 50 else ""),
                            "预测": r["predicted_class"],
                            "置信度": f"{r['confidence']:.2%}",
                        }
                        for r in results
                    ],
                    use_container_width=True,
                )
            else:
                st.warning("请输入至少一条文本。")

    st.divider()
    st.caption("🧠 基于 PyTorch BiLSTM 模型，训练数据为英文影评，适用于英文文本情感分析。")
