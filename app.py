"""Streamlit 网页版 AI 工具箱主应用。

包含两个功能模块：
1. 智能聊天 — 基于 Ollama 本地大模型的对话系统
2. 情感分析 — 基于 PyTorch BiLSTM 的文本情感分类
"""

import json
import sys
import os

# 确保项目根目录在 Python 路径中
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJECT_ROOT)

# 如果项目目录下有 lib/ 文件夹（本地安装的 PyTorch 等依赖），也加入路径
_LIB_DIR = os.path.join(_PROJECT_ROOT, "lib")
if os.path.isdir(_LIB_DIR):
    sys.path.insert(0, _LIB_DIR)

import streamlit as st

from src.api_client import APIUnreachable, ChatAPIClient, ChatAPIError
from src.chat_session import APIChatSession, ChatSession, LocalChatSession
from src.config import get_config
from src.ollama_client import OllamaClient
from src.chatbot import ChatBot
from src.utils import format_timestamp


# 数据源标识 → 界面标签。用标识（local/api）而不是标签做值，
# 是为了让「界面文案」和「代码分支条件」解耦：以后改文案不用动逻辑。
SESSION_LABELS = {
    "local": "💾 本地直连（内存）",
    "api": "🗄️ 后端 API（MySQL）",
}


def get_session(source: str) -> ChatSession:
    """按数据源取出会话对象（懒创建 + 跨 rerun 保持）。

    注意这里的 session_state 不是为了「缓存」，而是**必需品**：
    Streamlit 每次交互都会把整个脚本从头到尾重新执行一遍，普通局部变量
    活不过一轮 rerun。会话对象（尤其是那段对话历史）必须挂在
    session_state 上，否则用户每发一条消息、界面刷新一次，
    之前的对话就全丢了。这是写 Streamlit 应用要过的第一关。
    """
    if source == "api":
        if "api_session" not in st.session_state:
            st.session_state.api_session = APIChatSession(client=ChatAPIClient())
        return st.session_state.api_session
    return LocalChatSession(st.session_state.chatbot)


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

    # 界面层状态：两个数据源共用同一套键，切换数据源时整体重置（见侧边栏）。
    # 用 setdefault 而不是逐个 if，是为了新增状态时不会再漏掉一处。
    for key, default in (
        ("active_source", None),      # 当前数据源，用于检测「刚刚切换了」
        ("service_ok", None),         # None=还没检测 / True / False
        ("service_message", None),    # 给用户看的一句话
        ("chat_models", []),          # 模型名列表（每轮 rerun 都请求一次太浪费）
        ("export_data", None),        # 待下载的导出内容
    ):
        st.session_state.setdefault(key, default)

    if "sentiment_predictor" not in st.session_state:
        st.session_state.sentiment_predictor = None
    if "chinese_predictor" not in st.session_state:
        st.session_state.chinese_predictor = None


init_session_state()


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
        # ── 数据源 ──
        st.subheader("数据源")
        source = st.radio(
            "对话数据存在哪里",
            options=["local", "api"],
            format_func=lambda s: SESSION_LABELS[s],
            index=0,
            key="data_source",
            help=(
                "本地直连：消息只在当前进程的内存里，关掉就没了（改造前的行为）。\n\n"
                "后端 API：消息由 FastAPI 落进 MySQL，换台机器打开还在。"
            ),
        )
        session = get_session(source)

        # 刚切换数据源：把上一个数据源的检测结果、模型列表、导出内容清掉。
        # 不清的话会出现「拿 Ollama 的模型列表去建后端会话」这种串台。
        if st.session_state.active_source != source:
            st.session_state.active_source = source
            st.session_state.service_ok = None
            st.session_state.service_message = None
            st.session_state.chat_models = []
            st.session_state.export_data = None

        st.divider()

        # ── 服务状态 ──
        # 「服务状态」在两种数据源下含义不同，所以它属于数据源、不属于聊天模块：
        #   直连模式 → Ollama 通不通（通了才能生成）
        #   后端模式 → API 通不通 + 它的三个依赖（数据库 / Redis / Ollama）通不通
        # 界面代码不用关心这个差别，session.health() 返回统一的一句话。
        st.subheader("服务状态")

        # 首次进入（或刚切换数据源）自动检测一次，省掉用户一次点击。
        if st.session_state.service_ok is None:
            _ok, _msg = session.health()
            st.session_state.service_ok = _ok
            st.session_state.service_message = _msg

        if st.button("🔄 检测连接", use_container_width=True):
            ok, message = session.health()
            st.session_state.service_ok = ok
            st.session_state.service_message = message
            st.session_state.chat_models = session.list_models() if ok else []

        if st.session_state.service_ok:
            st.success("✅ " + (st.session_state.service_message or "连接正常"))
        else:
            st.error("❌ " + (st.session_state.service_message or "连接失败"))
            if session.kind == "api":
                st.info("请先启动后端：\n```\nuvicorn api.main:app --port 8000\n```")
            else:
                st.info("请确保本地已安装并启动 Ollama：\n```\nollama serve\n```")

        st.divider()

        # ── 后端模式专属：服务端会话管理 ──
        if session.kind == "api":
            st.subheader("会话（服务端）")
            if st.session_state.service_ok:
                try:
                    convs = session.list_conversations()
                except ChatAPIError as exc:
                    convs = []
                    st.warning(str(exc))

                if convs:
                    labels = [
                        f"#{c['id']} · {c['title']}（{c['message_count']} 条）"
                        for c in convs
                    ]
                    index = next(
                        (i for i, c in enumerate(convs)
                         if c["id"] == session.conversation_id),
                        0,
                    )
                    picked = st.selectbox(
                        "选择会话",
                        options=range(len(labels)),
                        format_func=lambda i: labels[i],
                        index=index,
                        key="conv_picker",
                    )
                    session.open_conversation(convs[picked]["id"])
                else:
                    st.info("还没有会话，新建一个开始聊。")

                with st.form("new_conversation", clear_on_submit=True):
                    title = st.text_input("新会话标题", value="新会话")
                    if st.form_submit_button("➕ 新建会话", use_container_width=True):
                        available = st.session_state.chat_models or session.list_models()
                        if not available:
                            st.error("服务端没有可用模型，无法建会话。")
                        else:
                            try:
                                session.create_conversation(title, available[0])
                                st.session_state.chat_models = available
                                st.rerun()
                            except ChatAPIError as exc:
                                st.error(str(exc))
            else:
                st.caption("后端不可用，先看上面的服务状态。")

            st.divider()

        # ── 模型设置 ──
        st.subheader("模型设置")
        models = st.session_state.chat_models
        if not models and st.session_state.service_ok:
            try:
                models = session.list_models()
                st.session_state.chat_models = models
            except Exception:  # noqa: BLE001 - 取不到模型列表不该让页面崩掉
                models = []

        if models:
            current = session.current_model()
            if current not in models:
                # 会话绑定的模型已下线 / 默认模型不在本地：落到第一个可用的，
                # 而不是让下拉框空着（空着的选择器用户根本没法操作）。
                session.set_model(models[0])
                current = models[0]
            selected_model = st.selectbox(
                "选择模型",
                options=models,
                index=models.index(current) if current in models else 0,
                help="直连模式=本地 Ollama 已装的模型；后端模式=服务端 models 表里的模型",
                key=f"model_picker_{source}",
            )
            if selected_model:
                session.set_model(selected_model)
        else:
            st.warning("⚠️ 没有可用模型")
            if session.kind == "local":
                st.info("请先拉取模型，例如：\n```\nollama pull deepseek-r1:1.5b\n```")
                st.caption(f"当前配置的默认模型：`{config.ollama.model}`")

        st.divider()

        # ── 生成参数 ──
        st.subheader("生成参数")
        configurable = session.can_configure()
        if not configurable:
            st.caption("后端模式下生成参数由服务端统一提供，前端不可调（保证多端行为一致）。")

        temperature = st.slider(
            "Temperature（创造性）",
            min_value=0.0,
            max_value=2.0,
            value=float(config.generation.temperature),
            step=0.1,
            disabled=not configurable,
            key=f"temperature_{source}",
        )
        top_p = st.slider(
            "Top P（核采样）",
            min_value=0.1,
            max_value=1.0,
            value=float(config.generation.top_p),
            step=0.05,
            disabled=not configurable,
            key=f"top_p_{source}",
        )
        max_tokens = st.number_input(
            "Max Tokens（最大长度）",
            min_value=128,
            max_value=8192,
            value=int(config.generation.max_tokens),
            step=128,
            disabled=not configurable,
            key=f"max_tokens_{source}",
        )
        session.configure(temperature, top_p, int(max_tokens))

        st.divider()

        # ── 系统提示词 ──
        st.subheader("系统提示词")
        if not configurable:
            st.caption("后端模式下提示词由服务端常量提供（api/routers/chat.py）。")
        system_prompt = st.text_area(
            "自定义 AI 角色",
            value=session.system_prompt,
            height=100,
            disabled=not configurable,
            key=f"system_prompt_{source}",
        )
        if st.button("应用提示词", use_container_width=True, disabled=not configurable):
            session.set_system_prompt(system_prompt)
            st.success("提示词已更新")

        st.divider()

        # ── 对话管理 ──
        st.subheader("对话管理")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🗑️ 清空", use_container_width=True):
                session.clear()
                st.rerun()
        with col2:
            if st.button("📥 导出", use_container_width=True):
                st.session_state.export_data = session.export()

        # 下载按钮必须挂在 session_state 上，而不是写在「导出」按钮的分支里。
        #
        # 原因：Streamlit 里点击按钮会触发一次整页 rerun，下一轮 st.button 返回
        # False，分支不再进入，那个 download_button 就随着重绘消失了——用户永远
        # 点不到它。把内容先存进 session_state，再无条件渲染下载按钮，
        # 才能让「导出」这个动作跨轮存活。（原版直连实现就踩了这个坑。）
        if st.session_state.export_data is not None:
            st.download_button(
                label="下载 JSON",
                data=json.dumps(
                    st.session_state.export_data, ensure_ascii=False, indent=2
                ),
                file_name=f"chat_history_{format_timestamp().replace(' ', '_').replace(':', '-')}.json",
                mime="application/json",
                use_container_width=True,
            )

        st.divider()
        st.subheader("统计")
        message_count, token_count = session.totals()
        st.metric("消息数", message_count)
        st.metric("预估 Token", token_count)

    # ── 聊天主界面 ──
    #
    # 注意这一整段里**没有任何**「消息存在哪」的判断：history() / send() / clear()
    # 都交给 session 对象，界面只管画。这就是抽出 ChatSession 的价值——
    # 下面这些代码在「本地直连」和「后端 API」两种模式下跑的是同一份。
    st.title(f"💬 {config.title}")
    st.caption(
        f"当前模型：`{session.current_model() or '未选择'}` ｜ "
        f"数据源：{SESSION_LABELS[session.kind]}"
    )

    history = session.history()
    no_api_conversation = session.kind == "api" and session.conversation_id is None

    if not history:
        if no_api_conversation:
            st.info("👈 在左侧「会话（服务端）」里新建一个会话，就能开始对话了。")
        else:
            st.info(
                "👋 欢迎！在下方输入框中开始对话吧。\n\n"
                "提示：请确保本地 Ollama 服务已启动（`ollama serve`），"
                "并已拉取模型（如 `ollama pull llama3.2`）。"
            )

    for msg in history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    user_input = st.chat_input(
        "输入你的问题...", key="chat_input", disabled=no_api_conversation
    )

    if user_input:
        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            message_placeholder = st.empty()
            full_response = ""
            try:
                for chunk in session.send(user_input):
                    full_response += chunk
                    message_placeholder.markdown(full_response + "▌")
                message_placeholder.markdown(full_response)

                # 后端模式能拿到服务端实测的生成耗时（latency_ms 是落库字段），
                # 顺手显示出来——这是「后端化」白捡的可观测性：
                # 直连模式想知道耗时，得自己在界面里掐表。
                latency = session.last_latency_ms
                if latency:
                    st.caption(f"本次生成耗时 {latency / 1000:.2f}s")
            except APIUnreachable as e:
                # 连不上后端：可能是没启动 API，也可能是地址配错了。
                st.error(str(e))
            except ChatAPIError as e:
                st.error(f"后端返回错误：{e}")
            except ConnectionError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"生成回复时出错：{e}")
        st.rerun()

    st.divider()
    st.caption("💡 基于 Streamlit + FastAPI + Ollama 构建，所有推理均在本地完成。")


# ══════════════════════════════════════════════════════════
# 模块二：情感分析
# ══════════════════════════════════════════════════════════
else:  # mode == "😊 情感分析"
    with st.sidebar:
        st.subheader("语言选择")
        lang = st.radio(
            "分析语言",
            options=["🇨🇳 中文", "🇬🇧 English"],
            index=0,
            help="选择情感分析模型的语言",
        )
        is_chinese = lang.startswith("🇨🇳")

        st.divider()
        st.subheader("模型信息")
        if is_chinese:
            st.info(
                "**中文 BiLSTM 情感分类模型**\n\n"
                "- 框架：PyTorch\n"
                "- 架构：Embedding + 双向 LSTM + 全连接层\n"
                "- 分词：字符级分词\n"
                "- 数据：中文评论标注数据\n"
                "- 适用：中文评论、反馈、影评等"
            )
        else:
            st.info(
                "**English BiLSTM Sentiment Model**\n\n"
                "- Framework: PyTorch\n"
                "- Architecture: Embedding + BiLSTM + FC\n"
                "- Parameters: ~738K\n"
                "- Test Accuracy: 97.0%\n"
                "- Data: 1166 movie reviews"
            )

    # ── 情感分析主界面 ──
    if is_chinese:
        st.title("😊 中文文本情感分析")
        st.caption("基于 PyTorch BiLSTM 模型 ｜ 字符级分词 ｜ 二分类（正面/负面）")
    else:
        st.title("😊 Text Sentiment Analysis")
        st.caption("PyTorch BiLSTM | Local inference | Binary classification")

    # 懒加载对应语言的模型
    predictor_key = "chinese_predictor" if is_chinese else "sentiment_predictor"
    if st.session_state.get(predictor_key) is None:
        try:
            if is_chinese:
                from sentiment_analysis.predict import ChineseSentimentPredictor
                with st.spinner("正在加载中文情感分析模型..."):
                    st.session_state.chinese_predictor = ChineseSentimentPredictor()
            else:
                from sentiment_analysis.predict import SentimentPredictor
                with st.spinner("Loading sentiment model..."):
                    st.session_state.sentiment_predictor = SentimentPredictor()
            st.success("✅ 模型加载成功")
        except Exception as e:
            st.error(f"模型加载失败：{e}")
            if is_chinese:
                st.info("请先运行 `python -m sentiment_analysis.train_chinese` 训练中文模型。")
            else:
                st.info("请先运行 `python -m sentiment_analysis.train` 训练模型。")
            st.stop()

    predictor = st.session_state[predictor_key]

    # 示例文本
    if is_chinese:
        examples = [
            "这家酒店环境很好，房间干净整洁，服务员态度热情",
            "外卖送餐速度很慢，饭菜都凉了，味道也一般",
            "这部电影太精彩了，剧情紧凑，演员演技在线",
            "商品质量很差，和描述的不一样，物流也慢",
            "餐厅环境优雅，菜品美味，服务周到",
            "手机卡顿严重，拍照效果差，电池续航短",
        ]
        placeholder_text = "例如：这家酒店环境很好，服务很热情..."
        input_label = "输入需要分析的中文文本"
        analyze_label = "🔍 分析情感"
        example_label = "加载示例"
        use_example_label = "📋 使用此示例"
        result_title = "📊 分析结果"
        batch_title = "📦 批量分析（每行一条）"
        batch_placeholder = "文本1\n文本2\n文本3"
        footer_text = "🧠 基于 PyTorch BiLSTM 中文情感模型，适用于中文评论、反馈、影评等文本。"
    else:
        examples = [
            "This movie is absolutely wonderful and I loved every minute of it",
            "This movie is terrible and I hated every minute of it",
            "The acting was superb but the story was a bit slow",
            "I cannot believe I wasted two hours on this garbage",
            "A heartwarming tale that restores your faith in humanity",
            "A complete waste of time and money, avoid at all costs",
        ]
        placeholder_text = "e.g., This movie is absolutely wonderful..."
        input_label = "Enter text to analyze"
        analyze_label = "🔍 Analyze"
        example_label = "Examples"
        use_example_label = "📋 Use example"
        result_title = "📊 Result"
        batch_title = "📦 Batch analysis (one per line)"
        batch_placeholder = "text1\ntext2\ntext3"
        footer_text = "🧠 PyTorch BiLSTM model trained on English movie reviews."

    # 输入区域
    st.subheader("📝 " + (input_label if is_chinese else input_label))
    input_text = st.text_area(
        input_label,
        height=100,
        placeholder=placeholder_text,
        key=f"sentiment_input_{'zh' if is_chinese else 'en'}",
    )

    col_a, col_b, col_c = st.columns([1, 1, 3])
    with col_a:
        analyze_clicked = st.button(analyze_label, use_container_width=True, type="primary")
    with col_b:
        example_idx = st.selectbox(
            example_label,
            options=range(len(examples)),
            format_func=lambda i: f"{'示例' if is_chinese else 'Example'} {i+1}",
            key=f"example_selector_{'zh' if is_chinese else 'en'}",
        )
    with col_c:
        if st.button(use_example_label, use_container_width=True):
            st.session_state[f"sentiment_input_{'zh' if is_chinese else 'en'}"] = examples[example_idx]
            st.rerun()

    # 分析结果
    if analyze_clicked and input_text.strip():
        with st.spinner("正在分析..." if is_chinese else "Analyzing..."):
            result = predictor.predict(input_text.strip())

        st.subheader(result_title)

        is_positive = result["predicted_label"] == 1
        emoji = "😊" if is_positive else "😞"
        label_text = "正面 (Positive)" if is_positive else "负面 (Negative)"

        col1, col2 = st.columns(2)
        with col1:
            st.metric("预测结果" if is_chinese else "Prediction", f"{emoji} {label_text}")
        with col2:
            st.metric("置信度" if is_chinese else "Confidence", f"{result['confidence']:.2%}")

        # 概率条形图
        st.subheader("类别概率分布" if is_chinese else "Probability distribution")
        for cls_name, prob in result["probabilities"].items():
            is_pred = cls_name == result["predicted_class"]
            label = f"**{cls_name}**" if is_pred else cls_name
            st.markdown(f"{label}  —  `{prob:.2%}`")
            st.progress(prob)

        with st.expander("查看输入文本" if is_chinese else "View input"):
            st.write(input_text.strip())

    elif analyze_clicked:
        st.warning("请先输入要分析的文本。" if is_chinese else "Please enter text first.")

    # 批量分析
    st.divider()
    with st.expander(batch_title):
        batch_text = st.text_area(
            "输入多条文本，每行一条" if is_chinese else "Enter multiple texts, one per line",
            height=120,
            placeholder=batch_placeholder,
            key=f"batch_input_{'zh' if is_chinese else 'en'}",
        )
        if st.button("批量分析" if is_chinese else "Analyze batch", use_container_width=True):
            if batch_text.strip():
                lines = [l.strip() for l in batch_text.strip().split("\n") if l.strip()]
                results = predictor.predict_batch(lines)
                st.dataframe(
                    [
                        {
                            "文本" if is_chinese else "Text": r["text"][:50] + ("..." if len(r["text"]) > 50 else ""),
                            "预测" if is_chinese else "Prediction": r["predicted_class"],
                            "置信度" if is_chinese else "Confidence": f"{r['confidence']:.2%}",
                        }
                        for r in results
                    ],
                    use_container_width=True,
                )
            else:
                st.warning("请输入至少一条文本。" if is_chinese else "Please enter at least one text.")

    st.divider()
    st.caption(footer_text)
