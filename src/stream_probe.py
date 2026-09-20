"""量「服务端到客户端这一段还是不是真流式」的尺子。

为什么需要单独抽出来：这一段有两种都会骗人的观测方式——

1. **用 `requests.iter_lines()` 读 SSE**：它按 `chunk_size` 阻塞读（默认 512 字节），
   数据不满就等着。服务端每 50ms 推一块，到它手里会变成「每 200ms 来一批」——
   于是「谁在攒批」这件事根本量不出来（详见 docs/interview-notes.md 第 5 节）。
2. **只数「有几个 chunk 事件」**：一个 `proxy_buffering on` 的反代，照样能一次性
   把 9 个事件全给你，数量对得上、内容也对得上，但流式已经死了。

所以真正要量的不是「有没有分块」，而是**每一块的到达时刻**。本模块提供两件东西：

- `read_sse()`：裸 socket 发请求，返回每个事件的**真实到达时刻**（支持 http/https）；
- `judge_incremental()`：给定到达时刻与总耗时，判定是增量到达还是被攒批了。

判据本身也要被校验（`tests/test_stream_probe.py`）——一个永远返回「增量」的尺子，
和一个量出「增量」的尺子长得一模一样。

⚠️ 判据只在**不缓冲的读法**下才有意义。用 `requests` 读出来的时刻喂给它是无效的，
这一点写进了 `read_sse` 的 docstring，别绕过它。
"""

import json
import socket
import ssl
import time
from typing import Dict, List, Optional, Sequence, Tuple

# 判定的三种结果。刻意做成三态而不是布尔：
# 「量不出来」和「量出来是好的」必须分开，否则块数不足会被当成通过（假绿）。
INCREMENTAL = "INCREMENTAL"
BUFFERED = "BUFFERED"
INCONCLUSIVE = "INCONCLUSIVE"

DEFAULT_MIN_CHUNKS = 3      # 少于 3 块就没有「间隔」可言，只能判无法判定
DEFAULT_MIN_SPAN = 0.25     # 首块到末块至少隔这么久，才算「分批到达」
DEFAULT_FIRST_RATIO = 0.5   # 首块必须在总耗时的一半之前到达


def read_sse(
    host: str,
    port: int,
    payload: dict,
    *,
    path: str = "/chat",
    use_tls: bool = False,
    ca_file: Optional[str] = None,
    insecure: bool = False,
    timeout: float = 30.0,
    recv_size: int = 4096,
    extra_headers: Optional[Sequence[str]] = None,
) -> Tuple[str, List[Tuple[dict, float]], float]:
    """裸 socket 打 `POST <path>`，返回 `(响应头原文, [(事件, 到达时刻)], 总耗时)`。

    为什么是裸 socket：见模块 docstring 第 1 条。`recv()` 拿到什么就是什么，
    不经过任何客户端缓冲，所以记录的到达时刻是「内核把这段字节交给我们」的时刻。

    到达时刻是相对请求发出那一刻的秒数（单调时钟，不受系统时间调整影响），
    同一个事件块里的多个 `data:` 行共用块内首字节的到达时刻。

    证书（只有 `use_tls=True` 时相关）：
    - 默认按系统信任链校验 —— `https://<真实域名>` 就走这条；
    - `ca_file=<路径>`：只信这一张（`create_default_context` 在给了 cafile 时**不会**
      再加载系统根证书，正好是自签验收要的语义：那张证书是唯一可信的根）。
      **校验仍然开着**，只是换了一条信任链 —— 这是本地验 TLS 的推荐姿势；
    - `insecure=True`：彻底关掉校验。那等于放弃「对面是谁」这件事，
      只配用来在本机对自签证书量流式，别用在任何真实环境（public_check 会告警）。
    后两个参数存在的原因很实在：没有它们，`use_tls` 这条分支永远没法在本机执行 ——
    而「从没执行过的代码」正是本项目栽过最多的那类洞。
    """
    body = json.dumps(payload).encode("utf-8")
    host_header = host if port in (80, 443) else f"{host}:{port}"
    head_lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {host_header}",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        "Accept: text/event-stream",
        "Connection: close",
    ]
    if extra_headers:
        head_lines.extend(extra_headers)
    head = ("\r\n".join(head_lines) + "\r\n\r\n").encode("ascii")

    raw = socket.create_connection((host, port), timeout=timeout)
    if use_tls:
        ctx = ssl.create_default_context(cafile=ca_file)
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            # server_hostname 传 host：给了 cafile 时证书里的名字也要对上，
            # 所以自签证书得把 127.0.0.1 写进 SAN（IP:127.0.0.1），否则照样验不过。
            raw = ctx.wrap_socket(raw, server_hostname=host)
        except Exception:
            # 握手失败（最典型的就是证书验不过）时把底层 socket 收掉：
            # 「验签必须失败」本身是一条**要断言的**行为（tests/e2e/nginx_check.py 的 C 段
            # 用默认信任链去打自签证书），每次跑都泄一个 fd 就不好了。
            raw.close()
            raise
    raw.sendall(head + body)

    try:
        buf, headers, events, header_done = b"", "", [], False
        t0 = time.monotonic()
        while True:
            data = raw.recv(recv_size)
            if not data:
                break
            now = time.monotonic() - t0
            buf += data
            if not header_done and b"\r\n\r\n" in buf:
                raw_head, buf = buf.split(b"\r\n\r\n", 1)
                headers = raw_head.decode("utf-8", "replace")
                header_done = True
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line.startswith(b"data: "):
                    events.append((json.loads(line[6:].decode("utf-8")), round(now, 3)))
            if events and events[-1][0].get("done"):
                break
            if time.monotonic() - t0 > timeout:
                break
        total = round(time.monotonic() - t0, 3)
    finally:
        raw.close()
    return headers, events, total


def judge_incremental(
    arrivals: Sequence[float],
    total: float,
    *,
    min_chunks: int = DEFAULT_MIN_CHUNKS,
    min_span: float = DEFAULT_MIN_SPAN,
    first_ratio: float = DEFAULT_FIRST_RATIO,
) -> Tuple[str, Dict[str, object], str]:
    """判定这批到达时刻是「增量到达」还是「被攒批了」。

    返回 `(verdict, metrics, reason)`，verdict 取 `INCREMENTAL` / `BUFFERED` / `INCONCLUSIVE`。

    两条独立的判据，任何一条触发就是攒批：

    - **跨度**：首块到末块之间几乎没有间隔 → 这些块是一起到达的
      （典型值：9 块全落在 20ms 内，而正常应该是 8 × 50ms = 400ms）。
    - **首块位置**：首块直到总耗时的一半之后才出现 → 内容是在上游生成完之后
      一次性吐出来的（反代的 `proxy_buffering on` 正是这个形态）。

    为什么不用「块间隔必须都落在某个区间」：那是拿**本地假 Ollama 的节奏**当尺子，
    换个真实模型（每块间隔几百毫秒）就恒假红。跨度与首块位置都跟上游节奏无关，
    只跟「是不是攒完再发」有关。
    """
    n = len(arrivals)
    metrics: Dict[str, object] = {
        "chunks": n,
        "total": round(total, 3),
        "first": round(arrivals[0], 3) if n else None,
        "last": round(arrivals[-1], 3) if n else None,
        "span": round(arrivals[-1] - arrivals[0], 3) if n >= 2 else 0.0,
    }

    if n < min_chunks:
        return (
            INCONCLUSIVE,
            metrics,
            f"只观测到 {n} 个 chunk（少于 {min_chunks}），量不出「有没有攒批」——"
            "换一个会让模型说更长的提示词再测",
        )
    if metrics["span"] < min_span:
        return (
            BUFFERED,
            metrics,
            f"{n} 个 chunk 都挤在 {metrics['span']}s 内到达（首块 {metrics['first']}s、"
            f"末块 {metrics['last']}s）：它们是**一起**到达的，中间那层在攒批",
        )
    if float(metrics["first"] or 0.0) > first_ratio * max(total, 1e-9):
        return (
            BUFFERED,
            metrics,
            f"首块直到 {metrics['first']}s 才到达，而整条响应 {metrics['total']}s："
            "内容是生成完之后一次性吐出来的",
        )
    return (
        INCREMENTAL,
        metrics,
        f"{n} 个 chunk 分布在 {metrics['span']}s 里，首块 {metrics['first']}s 到达"
        f"（总耗时 {metrics['total']}s）",
    )
