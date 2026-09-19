"""后端 API 客户端（前端侧）。

这个模块是「前端怎么消费自己的后端」那一半。它和 `src/ollama_client.py` 是**对称**
的两个东西：

    OllamaClient    — 直连模式：这个进程直接跟 Ollama 说话，消息存在内存里；
    ChatAPIClient   — 后端模式：这个进程只跟自己的 FastAPI 说话，消息存在 MySQL 里。

两者形状一致（check_health / 取模型列表 / 流式产出文本片段），所以 `app.py` 可以
只依赖「这个形状」而不关心背后是谁——这就是「前后端分离」在代码里的具体样子。
面试官问「你为什么要多写一层客户端，让 Streamlit 直接调 API 不就行了？」
答案是：正因为多了一层，才能做到「换后端不用改界面」以及「这套客户端能被测试」。
Streamlit 的代码是没法写单测的（它依赖 st.* 的运行时），但这一层可以。

**最重要的一点是 `chat_stream()` 怎么读流。**

后端已经把 SSE 一块块推出来了，但前端如果读得不对，用户看到的仍然是「一段一段蹦」。
原因和 `src/ollama_client.py` 顶部记的那个坑**一模一样**：
`requests.iter_lines()` 默认 `chunk_size=512`，而底层 `read(n)` 的语义是「攒够
n 字节再返回」——后端一行 SSE（`data: {"chunk": "武汉"}\n\n`）只有几十字节，
于是要攒好几行才交出来一次。所以这里**必须**用同一个常量 `STREAM_READ_CHUNK`。

这个坑值得单独说，因为它有「换一层就重现一次」的性质：
- 第一次出现在服务端读 Ollama 的流（已修）；
- 第二次出现在前端读自己后端的流（就是这里）；
- 第三次可能出现在 Nginx 反代上——所以后端响应头里加了 `X-Accel-Buffering: no`。
**每一层缓冲都会把流式变成批式，而每一层都默认是缓冲的。**
"""

import json
import os
from typing import Any, Dict, Iterator, List, Optional

import requests

# 复用领域层已经验证过的读取粒度常量：前端这一侧同样存在「攒批」问题，
# 值必须一样小。写成 import 而不是各写各的，是为了让两处永远保持一致。
from src.ollama_client import STREAM_READ_CHUNK

# 默认指向本机后端。生产部署时用环境变量覆盖（容器里是 http://api:8000）。
DEFAULT_API_BASE = os.environ.get("CHATBOT_API_BASE", "http://127.0.0.1:8000")

# 超时用 (连接超时, 读取超时) 元组，而不是一个数字。
#
# 为什么不给流式请求设「总超时」：模型是一边生成一边吐字的，一条长回答可能持续
# 几分钟。如果设了 60s 的总超时，用户问一个复杂问题、模型正在认真思考（这时候
# 一个字节都还没吐），连接就在第 60 秒被自己掐断了——用户看到「回答到一半没了」。
#
# `read timeout` 的真实语义是「**两次读之间**的最大间隔」，不是总时长。所以设成
# 单块间隔的量级（几十秒）就够了：只要模型还在吐字，读超时就一直不会被触发；
# 真正会触发它的场景是「服务端卡死了，一个字节都不再推」——那才是该断开的时候。
DEFAULT_TIMEOUT = (5, 60)


class ChatAPIError(RuntimeError):
    """后端返回了错误（HTTP 4xx/5xx，或流里推了 error 事件）。

    单独定义异常类型，是为了让调用方能区分「业务错误」和「网络问题」：
    前者要提示用户改输入（比如会话不存在），后者要提示用户检查服务是否启动。
    全都抛 Exception 的话，上层只能靠解析字符串来判断，那是很脆的做法。
    """


class APIUnreachable(ChatAPIError):
    """连不上后端服务（进程没起 / 端口不对 / 网络不通）。"""


class ChatAPIClient:
    """会话服务 HTTP 客户端。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: tuple = DEFAULT_TIMEOUT,
    ):
        self.base_url = (base_url or DEFAULT_API_BASE).rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        # 最近一次流式对话的耗时（毫秒），由 chat_stream 在流结束后写入。
        #
        # 为什么用实例属性而不是让 chat_stream 把耗时 yield 出来：
        # chat_stream 的契约是「只产出回复文本片段」，调用方（界面）拿到就直接
        # 渲染。一旦为了带一个元数据就把产出物改成 (type, value) 结构，
        # 所有调用点都要跟着写解包和分支，为了一个次要信息污染了主契约。
        # 元数据走属性，主链路保持干净。
        self.last_latency_ms: Optional[int] = None

    # ── 内部工具 ──────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """发一个普通（非流式）请求，把网络错误和 HTTP 错误统一成上面两个异常。"""
        kwargs.setdefault("timeout", self.timeout)
        try:
            resp = self._session.request(method, self._url(path), **kwargs)
        except requests.ConnectionError as exc:
            raise APIUnreachable(
                f"无法连接后端服务（{self.base_url}）。请确认 API 已启动："
                f"uvicorn api.main:app --port 8000"
            ) from exc
        except requests.Timeout as exc:
            raise ChatAPIError(f"请求后端超时（{self.base_url}）") from exc

        if resp.status_code >= 400:
            raise ChatAPIError(_extract_detail(resp))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ── 健康与目录 ────────────────────────────────────
    def health(self) -> Dict[str, Any]:
        """存活 + 诊断：`{"status": "...", "checks": {database/redis/ollama}}`。

        注意这个接口后端**永远返回 200**（依赖挂了也 200，把状态写在 body 里）。
        所以这里不做 raise_for_status——「依赖不全」是一种正常可读状态，
        不是调用失败。要看能不能对外服务，用 `ready()`。
        """
        return self._request("GET", "/health")

    def ready(self) -> bool:
        """就绪探针：依赖全通返回 True，否则 False（后端会返回 503）。"""
        try:
            return self._request("GET", "/health/ready").get("status") == "ready"
        except ChatAPIError:
            return False

    def list_models(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """可选模型列表。前端建会话时必须用它拿 `model_id`。"""
        return self._request("GET", "/models", params={"active_only": active_only})

    def list_users(self) -> List[Dict[str, Any]]:
        """用户列表（演示用，真实系统应由鉴权提供「我是谁」）。"""
        return self._request("GET", "/users")

    # ── 会话 ──────────────────────────────────────────
    def create_conversation(self, title: str, model_id: int, user_id: int) -> Dict[str, Any]:
        """新建会话。返回的对象里带 `id`，后续对话都带上它。"""
        return self._request(
            "POST",
            "/conversations",
            json={"title": title, "model_id": model_id, "user_id": user_id},
        )

    def list_conversations(self, user_id: int) -> List[Dict[str, Any]]:
        """会话列表（每个会话带 `message_count`，后端一条 SQL 聚合出来的）。"""
        return self._request("GET", "/conversations", params={"user_id": user_id})

    def get_conversation(self, conversation_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/conversations/{conversation_id}")

    def list_messages(
        self,
        conversation_id: int,
        limit: int = 50,
        before_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """拉历史消息（时间正序）。

        `before_id` 是游标：传「当前看到的最老那条的 id」就能往前翻一页。
        前端做「加载更多」时用它，而不是算 offset——聊天记录会一直增长，
        offset 分页在有新消息插入时会漏行/重复行。
        """
        params: Dict[str, Any] = {"limit": limit}
        if before_id is not None:
            params["before_id"] = before_id
        return self._request(
            "GET", f"/conversations/{conversation_id}/messages", params=params
        )

    def update_conversation(self, conversation_id: int, **fields) -> Dict[str, Any]:
        """局部更新（换模型 / 改标题 / 归档）。只发传进来的字段。"""
        return self._request(
            "PATCH", f"/conversations/{conversation_id}", json=fields
        )

    def clear_messages(self, conversation_id: int) -> int:
        """清空会话消息，返回被删除的条数。"""
        body = self._request("DELETE", f"/conversations/{conversation_id}/messages")
        return (body or {}).get("deleted", 0)

    # ── 流式对话 ──────────────────────────────────────
    def chat_stream(self, conversation_id: int, content: str) -> Iterator[str]:
        """发一条消息并逐块产出回复文本。

        后端事件格式（SSE）：
            data: {"chunk": "武汉"}
            data: {"chunk": "今天"}
            data: {"done": true, "latency_ms": 1372}
        出错时：
            data: {"error": "无法连接 Ollama 服务"}

        Yields:
            回复文本片段（只产出文本，元数据见 `self.last_latency_ms`）。

        Raises:
            APIUnreachable: 连不上后端。
            ChatAPIError: 后端返回 4xx/5xx，或流中途推了 error 事件。
        """
        self.last_latency_ms = None

        try:
            resp = self._session.post(
                self._url("/chat"),
                json={"conversation_id": conversation_id, "content": content},
                headers={"Accept": "text/event-stream"},
                timeout=self.timeout,
                stream=True,  # 关键：不在返回时就把整个 body 读完
            )
        except requests.ConnectionError as exc:
            raise APIUnreachable(
                f"无法连接后端服务（{self.base_url}）。请确认 API 已启动："
                f"uvicorn api.main:app --port 8000"
            ) from exc
        except requests.Timeout as exc:
            raise ChatAPIError("请求后端超时") from exc

        try:
            # 先看状态码。404（会话不存在）这类错误在这里就能确定，
            # 不用等流——错误响应是普通 JSON，不是 SSE。
            if resp.status_code >= 400:
                raise ChatAPIError(_extract_detail(resp))

            for block in _iter_sse_data(resp):
                try:
                    event = json.loads(block)
                except json.JSONDecodeError:
                    # 不是 JSON 的 data 行：SSE 允许任意载荷，直接跳过而不是崩掉。
                    continue

                if "error" in event:
                    # 服务端已经明确告知失败，把它升级成异常，交给调用方的
                    # except 分支统一处理，而不是假装正常结束。
                    raise ChatAPIError(event["error"])

                if event.get("done"):
                    self.last_latency_ms = event.get("latency_ms")
                    return

                chunk = event.get("chunk")
                if chunk:
                    yield chunk
        finally:
            # 必须显式关闭。流式响应会把连接占住直到 body 读完；如果调用方提前
            # 断开遍历（用户在界面上点了「停止」），这里不 close 的话连接会一直
            # 挂在连接池里直到被回收。finally 保证「无论正常结束、异常、还是
            # 调用方中途放弃」，都会把连接还回去——服务端那边也会因为读到
            # 连接关闭而停止生成，省掉无谓的算力。
            resp.close()


def _iter_sse_data(resp: requests.Response) -> Iterator[str]:
    """把 HTTP 响应体按 SSE 协议切成一个个 `data:` 载荷。

    SSE 的最小结构是「一行行文本，空行表示一个事件结束」：
        data: 第一行
        data: 第二行
        <空行>

    本项目后端每个事件只发一行 data，所以实现可以很薄；但这里仍然按
    「累积到空行才产出」来写，而不是「遇到 data: 就当事件」——因为一旦将来
    后端把长内容分行发（SSE 规范允许），后者会把一个事件拆成两个。

    为什么不用现成的 SSE 库：这个解析逻辑只有十来行，而 `iter_lines` 的
    `chunk_size` 恰好是必须自己控制的那个参数（见模块顶部关于攒批的说明）。
    引一个库反而要先去确认它有没有暴露这个参数、默认值是多少——
    自己写这十几行，把控制权留在手上。
    """
    buffer: List[str] = []

    for raw in resp.iter_lines(chunk_size=STREAM_READ_CHUNK, decode_unicode=True):
        # 空行 = 一个事件结束。
        #
        # 这里必须写 `if not raw` 而不是 `if raw == ""`：`iter_lines` 在某些
        # requests 版本 / 场景下给的是 bytes（`decode_unicode` 并不总是生效），
        # 而 `b"" != ""` —— 写成 `== ""` 的话，空行会被当成普通行跳过，
        # 于是相邻几个事件的载荷会被拼进同一个 buffer，最后 join 成
        # `{"chunk": ...}\n{"done": ...}` 这种非法 JSON，整块被丢弃。
        # 症状是「流式完全没反应」，而数据其实一直在到达。
        # `not raw` 同时覆盖 None / "" / b""，是这里唯一正确的写法。
        if not raw:
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            continue

        line = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")

        # `:` 开头是注释（常用作心跳），不是数据。
        if line.startswith(":"):
            continue
        if not line.startswith("data:"):
            # event: / id: / retry: 这些字段本项目不用，直接忽略。
            continue

        # 规范里冒号后有一个可选空格，两个都要能吃。
        payload = line[len("data:") :]
        buffer.append(payload[1:] if payload.startswith(" ") else payload)

    # 流结束时没等到尾随空行（对端直接断开）也不能丢数据。
    if buffer:
        yield "\n".join(buffer)


def _extract_detail(resp: requests.Response) -> str:
    """从错误响应里抠出人能看懂的一句话。

    FastAPI 的错误体有两种形状，都要处理：
    - 业务错误（我们主动 raise HTTPException）：{"detail": "会话不存在"}
    - 校验错误（Pydantic 422）：{"detail": [{"loc": [...], "msg": "..."}]}
      这时候 detail 是列表，直接 str() 会打出一大坨 Python 字面量，
      所以取每项的 msg 拼成一句。
    """
    try:
        detail = resp.json().get("detail")
    except (json.JSONDecodeError, ValueError, AttributeError):
        return f"HTTP {resp.status_code}"

    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        msgs = [item.get("msg", "") for item in detail if isinstance(item, dict)]
        if msgs:
            return "；".join(m for m in msgs if m)
    return f"HTTP {resp.status_code}"
