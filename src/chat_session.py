"""会话数据源抽象：让界面代码不必知道「消息存在哪」。

后端化改造最容易做半截的地方就在这里。接口写完了、能 curl 通了，但界面还是
直连 Ollama——于是「前后端分离」只存在于 README 里。把界面接上去时会撞上一个
现实问题：**界面上有几十处 `chatbot.messages`、`chatbot.set_model()`，
难道要为了「走后端」再复制一整份界面代码？**

复制一份的代价不是「多写 200 行」，而是**以后每改一处界面都要改两个地方**，
很快就会只改一个。所以正确的做法是先抽出一个「会话」应该长什么样，
再让两套实现去满足它：

    ChatSession（这个接口）
      ├─ LocalChatSession — 直连模式：消息在内存里，进程退了就没了
      └─ APIChatSession   — 后端模式：消息在 MySQL 里，换台机器打开还在

界面只依赖上面那个形状，于是「数据存哪」变成了一个下拉框的事。

**这也是为什么这一层值得单独写文件、单独写测试**：它是唯一能在不启动
Streamlit 的前提下被测试的「界面逻辑」（Streamlit 的代码依赖 st.* 的运行时，
没法单测）。把「界面会怎么调用数据」收敛到这里，就等于把界面的行为变成可测的。

两个实现的能力差异是真实存在的，不打算抹平：
- 系统提示词 / 生成参数（temperature 等）：本地模式能改，后端模式当前**改不了**
  （`POST /chat` 只收 conversation_id + content，参数由服务端常量决定）。
  这是有意的：多端共用同一套参数，行为才一致。要做成可配置，得先给接口加字段。
- 模型：两种模式都可以切，但后端模式切的是「会话绑定的 model_id」，
  属于会写库的操作，不只是改内存状态。
"""

from abc import ABC, abstractmethod
from typing import Dict, Iterator, List, Optional, Tuple

from src.api_client import APIUnreachable, ChatAPIClient, ChatAPIError
from src.chatbot import ChatBot
from src.utils import estimate_tokens


class ChatSession(ABC):
    """界面依赖的「会话」形状。

    刻意定义成抽象类而不是 Protocol：这些方法会被界面大量调用，
    漏实现一个应该**在创建实例时就报错**，而不是等到某条代码路径被执行才发现。
    """

    #: 数据源标识，界面用它区分要不要显示 API 专属控件。
    kind: str = "abstract"
    #: 展示给用户的名称。
    label: str = "会话"

    # ── 能力与状态 ────────────────────────────────────
    @abstractmethod
    def health(self) -> Tuple[bool, str]:
        """返回 (是否可用, 给用户看的一句话说明)。"""

    @abstractmethod
    def list_models(self) -> List[str]:
        """可选模型名列表。"""

    @abstractmethod
    def current_model(self) -> str:
        """当前模型名。"""

    @abstractmethod
    def set_model(self, name: str) -> None:
        """切换当前模型。"""

    # ── 对话内容 ──────────────────────────────────────
    @abstractmethod
    def history(self) -> List[Dict[str, str]]:
        """完整历史（不含 system），按时间正序。"""

    @abstractmethod
    def send(self, text: str) -> Iterator[str]:
        """发送一条消息，逐块产出回复文本。"""

    @abstractmethod
    def clear(self) -> None:
        """清空对话内容（保留会话本身）。"""

    # ── 提示词与参数 ──────────────────────────────────
    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """当前系统提示词。"""

    @abstractmethod
    def set_system_prompt(self, prompt: str) -> None:
        """更新系统提示词。"""

    @abstractmethod
    def configure(self, temperature: float, top_p: float, max_tokens: int) -> None:
        """设置生成参数。

        后端模式当前是空实现——参数由服务端决定。**把这一点做成显式的空实现，
        而不是假装成功**：界面上会据此把控件置灰并说明原因，
        比让用户调了没反应强得多。
        """

    def can_configure(self) -> bool:
        """生成参数在当前数据源下是否可调。界面用它决定控件状态。"""
        return True

    @property
    def last_latency_ms(self) -> Optional[int]:
        """最近一次生成的耗时（毫秒），拿不到则为 None。

        默认返回 None，而不是抛 NotImplementedError：这是「有更好、没有也能过」
        的可选信息。界面据此决定要不要显示那行耗时，不需要知道是哪个数据源。
        """
        return None

    # ── 统计与导出 ────────────────────────────────────
    def totals(self) -> Tuple[int, int]:
        """返回 (消息数, 预估 token)。两种实现共用同一套估算，口径一致。"""
        msgs = self.history()
        return len(msgs), sum(estimate_tokens(m["content"]) for m in msgs)

    def export(self) -> List[Dict[str, str]]:
        """导出对话（含系统提示），格式与原 Streamlit 版本保持一致。"""
        return [{"role": "system", "content": self.system_prompt}] + self.history()


class LocalChatSession(ChatSession):
    """直连模式：消息只在这个进程的内存里。

    这就是改造前 Streamlit 的行为，原样保留。它没有被淘汰，因为它是「零依赖
    跑起来看一眼」的最短路径：不需要 MySQL、不需要启动后端，装完依赖就能聊。
    一个项目里两种模式并存不是技术债，而是「降低上手门槛」和「具备后端能力」
    这两件事本来就不该二选一。
    """

    kind = "local"
    label = "本地直连（内存）"

    def __init__(self, chatbot: ChatBot):
        self._bot = chatbot

    def health(self) -> Tuple[bool, str]:
        ok = False
        try:
            ok = self._bot.client.check_health()
        except Exception:  # noqa: BLE001 - 任何异常都等价于「连不上」
            ok = False
        if ok:
            return True, f"Ollama 已连接（{len(self._bot.get_available_models())} 个模型）"
        return False, "无法连接 Ollama，请确认已启动（ollama serve）"

    def list_models(self) -> List[str]:
        return self._bot.get_available_models()

    def current_model(self) -> str:
        return self._bot.current_model

    def set_model(self, name: str) -> None:
        self._bot.set_model(name)

    def history(self) -> List[Dict[str, str]]:
        return self._bot.messages

    def send(self, text: str) -> Iterator[str]:
        """包一层：流中途失败时，把刚才那句没得到回答的提问撤掉。

        这件事原来写在界面里（`app.py` 直接 `chatbot._messages.pop()`），
        属于界面越界去改领域对象的私有状态。现在两种数据源在这里统一了语义：

        - 直连模式：撤回内存里那条 —— 它本来就没被任何人记下来；
        - 后端模式：**不撤** —— 服务端已经把用户消息落库了。而且不该撤：
          用户确实说过这句话，刷新页面它应该还在历史里。差异是真实的，
          不是实现偷懒，所以由各自的实现决定，界面不用管。
        """
        try:
            yield from self._bot.generate_response_stream(text)
        except Exception:
            self._bot.drop_last_user_message()
            raise

    def clear(self) -> None:
        self._bot.clear_history()

    @property
    def system_prompt(self) -> str:
        return self._bot.system_prompt

    def set_system_prompt(self, prompt: str) -> None:
        self._bot.set_system_prompt(prompt)

    def configure(self, temperature: float, top_p: float, max_tokens: int) -> None:
        gen = self._bot.client.generation_config
        gen.temperature = temperature
        gen.top_p = top_p
        gen.max_tokens = int(max_tokens)


class APIChatSession(ChatSession):
    """后端模式：消息落在 MySQL 里，由 FastAPI 负责读写与调用 Ollama。

    和 LocalChatSession 的关键差别不在「多了几行网络代码」，而在**状态的所有者变了**：
    直连模式下「当前有哪些消息」是本地列表说了算；后端模式下这是服务端的真相，
    本地那份只是缓存。所以这里所有读操作都重新问服务端，不自己攒状态 ——
    攒了就会和数据库不一致（比如另一个标签页也在聊同一个会话）。
    """

    kind = "api"
    label = "后端 API（MySQL）"

    def __init__(
        self,
        client: ChatAPIClient,
        user_id: Optional[int] = None,
        conversation_id: Optional[int] = None,
    ):
        self._client = client
        # 允许为 None：真实系统里 user_id 来自登录态，这里没有鉴权，
        # 所以在真正需要它的时候（建会话）再去解析一个合法值。
        self.user_id = user_id
        self.conversation_id = conversation_id
        # name -> id 的映射：界面用名字展示（可读），提交时必须给 id。
        self._model_ids: Dict[str, int] = {}

    def resolve_user_id(self) -> Optional[int]:
        """确定用哪个用户建会话。

        真实系统里这个值来自登录态（Token / Session），前端不该自己挑。
        这里没有鉴权，所以退而求其次：没显式指定就取库里第一个用户。

        这一步看着像妥协，但它解决的是一类很具体的失败：**外键约束报出来的错
        完全指不出原因**。如果库里 id=1 的用户不存在（换了台机器、导入了别人的
        数据），`POST /conversations` 会撞 `conversations.user_id` 的外键约束，
        返回一个没有上下文的 500；而在业务层先解析出「实际存在哪个用户」，
        就能主动给出「请先初始化数据」这种可执行的提示。
        """
        if self.user_id is not None:
            return self.user_id
        try:
            users = self._client.list_users()
        except ChatAPIError:
            return None
        if users:
            self.user_id = users[0]["id"]
        return self.user_id

    # ── 会话管理（后端模式专属）──────────────────────
    def refresh_models(self) -> List[str]:
        """拉一次模型目录，建立 name -> id 映射。"""
        self._model_ids = {}
        names: List[str] = []
        for item in self._client.list_models():
            name = item.get("name", "")
            if not name:
                continue
            self._model_ids[name] = item["id"]
            names.append(name)
        return names

    def list_conversations(self) -> List[Dict]:
        if self.user_id is None:
            self.resolve_user_id()
        if self.user_id is None:
            return []
        return self._client.list_conversations(self.user_id)

    def open_conversation(self, conversation_id: int) -> None:
        """切换「当前会话」——不需要任何请求，只是换一个 id。"""
        self.conversation_id = conversation_id

    def create_conversation(self, title: str, model_name: str) -> int:
        """新建会话并把它设为当前会话，返回新会话的 id。"""
        user_id = self.resolve_user_id()
        if user_id is None:
            raise ChatAPIError(
                "服务端还没有任何用户，无法建会话。请先初始化数据：python -m db.init_db"
            )

        model_id = self._model_ids.get(model_name)
        if model_id is None:
            # 名字对不上 id 说明前端拿到的模型列表已经过期（比如库里刚加了模型），
            # 重拉一次再试，而不是直接报错让用户重启应用。
            self.refresh_models()
            model_id = self._model_ids.get(model_name)
        if model_id is None:
            raise ChatAPIError(f"模型 {model_name} 不在服务端模型列表里")

        conv = self._client.create_conversation(
            title=title[:200] or "新会话", model_id=model_id, user_id=user_id
        )
        self.conversation_id = conv["id"]
        return self.conversation_id

    def ensure_conversation(self, model_name: str) -> int:
        """没有当前会话就建一个（首次进入后端模式时用）。"""
        if self.conversation_id is None:
            return self.create_conversation("新会话", model_name)
        return self.conversation_id

    # ── ChatSession 实现 ─────────────────────────────
    def health(self) -> Tuple[bool, str]:
        try:
            body = self._client.health()
        except ChatAPIError as exc:
            return False, str(exc)

        checks = body.get("checks", {})
        if body.get("status") == "ok":
            return True, "后端 API 正常（数据库 / Redis / Ollama 全部可用）"

        # 依赖不全时，把挂掉的那个说出来——比笼统的「服务异常」有用得多。
        down = [name for name, ok in checks.items() if not ok]
        return False, f"后端可达，但依赖异常：{', '.join(down)}"

    def list_models(self) -> List[str]:
        if not self._model_ids:
            self.refresh_models()
        return list(self._model_ids.keys())

    def current_model(self) -> str:
        """当前模型名。从会话详情里读——因为模型的归属是「会话」，不是「用户偏好」。

        **这个方法必须永不抛异常。** 它只用来在标题栏显示一行字，拿不到就该显示
        「未选择」。界面在「刚切到后端数据源、后端还没连通」的那一瞬间就会调它——
        这里漏一个异常，整页就会变成 Streamlit 的红色报错页，
        而用户其实什么都没做错。

        （这不是假设：本项目的界面测试 `tests/test_app.py` 就是这么抓到它的——
        `current_model()` 内部调 `list_models()` 拉模型目录时没兜住连接失败。
        一个只用于展示的方法，不该有能力让整个页面崩掉。）
        """
        try:
            if self.conversation_id is None:
                names = self.list_models()
                return names[0] if names else ""
            conv = self._client.get_conversation(self.conversation_id)
        except ChatAPIError:
            return ""

        target_id = conv.get("model_id")
        for name, mid in self._model_ids.items():
            if mid == target_id:
                return name
        return ""

    def set_model(self, name: str) -> None:
        """换模型 = 改会话绑定的 model_id（会写库）。"""
        if self.conversation_id is None:
            return
        model_id = self._model_ids.get(name)
        if model_id is None:
            return
        self._client.update_conversation(self.conversation_id, model_id=model_id)

    def history(self) -> List[Dict[str, str]]:
        """从服务端拉历史。

        为什么不缓存：「当前有哪些消息」的真相在数据库里。本地攒一份就会
        和数据库不一致——同一用户在两个标签页聊同一个会话、或者后端刚写完一条
        消息，本地那份立刻就过期了。这里每次 rerun 拉一次（限 50 条），
        代价可以忽略，换来的是「界面永远不会显示一个不存在的对话」。
        """
        if self.conversation_id is None:
            return []
        try:
            rows = self._client.list_messages(self.conversation_id, limit=50)
        except ChatAPIError:
            # 会话被删了 / 后端挂了：显示为空，而不是让整个页面崩掉。
            return []
        return [
            {"role": r["role"], "content": r["content"]}
            for r in rows
            if r["role"] in ("user", "assistant")
        ]

    def send(self, text: str) -> Iterator[str]:
        """走后端流式接口。

        注意这里**不需要**自己做「先加用户消息、再追加助手回复」这两步：
        后端在 `/chat` 里已经把用户消息落库、把助手回复落库，前端只管显示。
        这正是「状态所有者变了」的具体体现——直连版本里这两步是前端在做的。
        """
        if self.conversation_id is None:
            raise ChatAPIError("还没有打开任何会话")
        return self._client.chat_stream(self.conversation_id, text)

    def clear(self) -> None:
        """清空消息（走 DELETE，会连带让服务端的上下文缓存失效）。"""
        if self.conversation_id is None:
            return
        self._client.clear_messages(self.conversation_id)

    @property
    def system_prompt(self) -> str:
        # 后端模式读不到服务端的提示词（接口没暴露），这里如实返回说明文字，
        # 让界面上的输入框显示「不可改」的原因，而不是假装有一个值。
        return "（后端模式：系统提示词由服务端统一提供，前端不可修改）"

    def set_system_prompt(self, prompt: str) -> None:
        """后端模式下不支持——空实现，界面上控件是禁用的。"""
        return None

    def configure(self, temperature: float, top_p: float, max_tokens: int) -> None:
        """后端模式下不支持——参数由服务端决定。"""
        return None

    def can_configure(self) -> bool:
        return False

    @property
    def last_latency_ms(self) -> Optional[int]:
        """转发服务端实测的生成耗时。

        这个数字是**服务端**算的（`api/routers/chat.py` 里用 perf_counter 夹住
        整段生成），而且会随 assistant 消息一起落库。所以前端拿到的不是自己
        掐的表，而是「服务端认为这次生成花了多久」——两边发生分歧时（比如网络慢），
        以服务端为准更有意义：它才是真正调用模型的那一环。
        """
        return self._client.last_latency_ms


def create_session(
    source: str,
    local_chatbot: Optional[ChatBot] = None,
    client: Optional[ChatAPIClient] = None,
    user_id: Optional[int] = None,
) -> ChatSession:
    """按数据源类型创建会话对象。界面只需调这一个函数。"""
    if source == "api":
        return APIChatSession(client=client or ChatAPIClient(), user_id=user_id or 1)
    if local_chatbot is None:
        raise ValueError("直连模式需要传入 ChatBot 实例")
    return LocalChatSession(local_chatbot)
