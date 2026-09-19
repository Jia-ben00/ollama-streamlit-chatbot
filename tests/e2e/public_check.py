"""公网入口验收：对着一个「从外面看得到的地址」跑，逐项断言。

和另外两个验收脚本的分工（三个都在 tests/e2e/，别混）：

| 脚本 | 在哪跑 | 回答什么问题 |
|---|---|---|
| `smoke.py` | 本机 | 这套代码拼起来能跑吗（真 MySQL + 假 Ollama） |
| `.github/scripts/container_smoke.sh` | 服务器上 | **容器化部署**成立吗（非 root、无凭据、端口不外露） |
| `public_check.py`（本文件） | **从外面**（你的笔记本） | **公网入口**成立吗（经 Nginx/HTTPS 之后，流式还是真流式吗） |

为什么非要单独有一个：前两个都跑在「本机 / 服务器自己」这一侧，中间没有真实反代。
而 `proxy_buffering on` 这类配置问题只在**多一层**时出现，且一旦出现**功能是好的**——
只是「一个字一个字蹦」变成「转圈等到最后出全文」，没人会报警。

用法（把 URL 换成你的域名或公网 IP）：

    python tests/e2e/public_check.py --url https://your-domain.com
    python tests/e2e/public_check.py --url http://1.2.3.4:8000 --no-ports
    python tests/e2e/public_check.py --url http://127.0.0.1:8100   # 经攒批替身 —— 应当红

退出码：0 全部通过；1 有失败项；2 连不上目标。
"""

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.stream_probe import (  # noqa: E402
    BUFFERED,
    INCONCLUSIVE,
    INCREMENTAL,
    judge_incremental,
    read_sse,
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

USE_COLOR = sys.stdout.isatty()
failures = []
skips = []

# 让模型多说几句：块数太少就没法判断「有没有攒批」（判据会返回 INCONCLUSIVE）
PROMPT = "请用大约 50 个字介绍你自己，分几句话说完。"


def check(name: str, cond: bool, extra: str = "") -> None:
    mark = ("\033[32mPASS\033[0m" if cond else "\033[31mFAIL\033[0m") if USE_COLOR \
        else ("PASS" if cond else "FAIL")
    print(f"[{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def warn(name: str, extra: str = "") -> None:
    """降级但不致命的问题：显式打出来，但不计入失败（否则部署形态会被误杀）。

    用它的门槛要高：只有当「这个状态在设计上就允许」时才配用 warn。
    """
    mark = ("\033[33mWARN\033[0m" if USE_COLOR else "WARN")
    print(f"[{mark}] {name}" + (f" | {extra}" if extra else ""))


def skip(name: str, why: str) -> None:
    print(f"[SKIP] {name} | {why}")
    skips.append(name)


def tcp_probe(host: str, port: int, timeout: float = 5.0) -> str:
    """探一个 TCP 端口，返回 `open` / `closed` / `filtered`。

    三者要分清，因为「连不上」并不总是好消息：
    - `open`     ：三次握手成功，确实有服务在听；
    - `closed`   ：明确收到 RST——主机在，端口没人听（预期中的「没暴露」长这样）；
    - `filtered` ：超时 / 不可达——包被丢了，或者网络根本到不了。
    """
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return "open"
    except ConnectionRefusedError:
        return "closed"
    except (socket.timeout, OSError):
        return "filtered"


def check_ports(host: str, ports, control_port: int) -> None:
    """断言内部端口没暴露到公网 —— 并且先证明这把探针「量得出东西」。

    控制组是关键：如果探针连一个**已知开放**的端口都连不上，那它对其他端口报的
    「连不上」就毫无意义（可能只是我这边的网络不通）。所以先探控制端口，
    自检不过就直接报失败，而不是把后面那几个「连不上」当成「安全」。
    """
    print()
    print(f"── 端口暴露检查（目标 {host}）──")
    ctl = tcp_probe(host, control_port)
    if ctl != "open":
        check(f"探针自检：能连上已知开放的控制端口 {control_port}", False,
              f"实际={ctl} —— 探针本身失效，**不能**据此断定其他端口是关的")
        return
    check(f"探针自检：能连上已知开放的控制端口 {control_port}", True, f"{ctl}")

    for p in ports:
        state = tcp_probe(host, p)
        check(f"端口 {p} 从公网不可用", state != "open",
              f"{state}" + ("（被丢包或网络不通，两者这里分不清，但都不是可用状态）"
                            if state == "filtered" else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="公网入口验收")
    ap.add_argument("--url", default=os.getenv("PUBLIC_URL", "http://127.0.0.1:8000"),
                    help="被验收的地址（经 Nginx 的域名，或直连的 IP:端口）")
    ap.add_argument("--user-id", type=int, default=1)
    ap.add_argument("--model-id", type=int, default=1)
    ap.add_argument("--ports", default="3306,6379", help="期望「从公网不可用」的端口，逗号分隔")
    ap.add_argument("--control-port", type=int, default=None,
                    help="端口探针的控制组端口（默认取 --url 的端口）。"
                         "本地自测时可故意指一个关着的端口，用来验证「探针自检」真的会拦住假绿")
    ap.add_argument("--no-ports", action="store_true", help="跳过端口检查（本地自测时用）")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    base = args.url.rstrip("/")
    parsed = urlparse(base)
    use_tls = parsed.scheme == "https"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if use_tls else 80)

    print(f"验收目标：{base}   (host={host} port={port} tls={use_tls})")
    print()

    s = requests.Session()

    # ── 0. 通不通 ────────────────────────────────────────
    try:
        r = s.get(f"{base}/", timeout=10)
    except requests.RequestException as exc:
        print(f"[FAIL] 连不上 {base}：{exc}")
        print("       先确认安全组已放行，且服务在跑。")
        return 2
    check("GET / 返回服务信息", r.status_code == 200
          and r.json().get("service") == "ollama-chatbot-api", f"HTTP {r.status_code}")

    # ── 1. 健康 ──────────────────────────────────────────
    h = s.get(f"{base}/health", timeout=15)
    check("GET /health 返回 200（存活探针）", h.status_code == 200, f"HTTP {h.status_code}")
    body = h.json().get("checks", {})
    check("依赖：数据库可用（能不能干活的前提）", body.get("database") is True,
          f"checks={body}")
    check("依赖：Ollama 可用（否则 /chat 必然失败）", body.get("ollama") is True)

    ready = s.get(f"{base}/health/ready", timeout=15)
    if ready.status_code == 200:
        check("GET /health/ready 返回 200（就绪，可以接流量）", True)
    elif body.get("database") is True and body.get("ollama") is True:
        # 只有 Redis 不可用：缓存降级、聊天照样能用（设计如此），所以这里不判失败——
        # 判失败会让「本机或小机器没装 Redis」的部署被误杀。
        # 但在**生产**上跑出这条，基本就是 REDIS_URL 配错了（写 127.0.0.1，容器里那是它自己）。
        warn("GET /health/ready 返回 503",
             "只因 Redis 不可用：缓存降级、聊天仍可用。若这是生产环境，"
             "八成是 REDIS_URL 写成了 127.0.0.1，见 docs/DEPLOY.md §7")
    else:
        check("GET /health/ready 返回 200", False,
              f"HTTP {ready.status_code} checks={body} —— 依赖没齐，后面的流式检查基本没戏")

    # ── 2. 建一个会话 ────────────────────────────────────
    title = "公网验收 " + time.strftime("%H:%M:%S")
    r = s.post(f"{base}/conversations", timeout=15,
               json={"title": title, "model_id": args.model_id, "user_id": args.user_id})
    if r.status_code not in (200, 201):
        check("POST /conversations 建会话", False, f"HTTP {r.status_code} {r.text[:160]}")
        print("\n建不了会话，后面的流式检查做不了。"
              "常见原因：user_id / model_id 在库里不存在（换个 id 试）。")
        return 1
    conv_id = r.json()["id"]
    check("POST /conversations 建会话", True, f"id={conv_id}")

    # ── 3. 流式：这一段才是公网入口的真正风险 ─────────────
    print()
    print("── 流式检查（经反代之后，回复还是「逐块到达」吗）──")
    headers, events, total = read_sse(host, port, {"conversation_id": conv_id, "content": PROMPT},
                                      use_tls=use_tls, timeout=args.timeout)

    ct = next((l for l in headers.split("\r\n") if l.lower().startswith("content-type")), "?")
    check("Content-Type 是 text/event-stream", "text/event-stream" in headers.lower(), ct)

    chunks = [e[0]["chunk"] for e in events if "chunk" in e[0]]
    arrivals = [e[1] for e in events if "chunk" in e[0]]
    done = [e for e in events if e[0].get("done")]

    if not chunks:
        check("流里有 chunk 事件", False,
              f"一个都没收到；原始事件={[e[0] for e in events][:3]}")
        print("\n拿不到 chunk，先看服务端日志与 OLLAMA_BASE_URL 是否可达。")
        return 1

    verdict, metrics, reason = judge_incremental(arrivals, total)
    check("回复是逐块到达的（中间那层没攒批）", verdict == INCREMENTAL, reason)

    if verdict != INCREMENTAL:
        print()
        print("  ⚠️ 这正是「公网入口」最容易翻车的一处，排查顺序：")
        print("     1) 反代配置里要有 `proxy_buffering off;`（见 docs/DEPLOY.md §6）")
        print("     2) 直连应用端口再跑一次本脚本：若直连 INCREMENTAL、经反代 BUFFERED，")
        print("        就锁定在反代这一层；两层都 BUFFERED，那是应用没真透传 Ollama 的流。")
        print("     3) 别指望拿 `X-Accel-Buffering` 响应头来判断：那个头是发给反代看的，")
        print("        经 Nginx 之后很可能根本到不了客户端。唯一靠得住的是到达时刻。")

    check("流以 done 事件收尾", len(done) == 1, f"done 事件数={len(done)}")
    if done:
        check("done 事件带 latency_ms", isinstance(done[0][0].get("latency_ms"), int),
              f"latency_ms={done[0][0].get('latency_ms')}")

    print(f"       观测到 {metrics['chunks']} 块，分布在 {metrics['span']}s 内，"
          f"首块 {metrics['first']}s 到达，整条 {metrics['total']}s")

    # ── 4. 落库（顺带确认这次验收没白跑）──────────────────
    detail = s.get(f"{base}/conversations/{conv_id}", timeout=15).json()
    check("这次对话真的落库了（message_count = 2）", detail.get("message_count") == 2,
          f"message_count={detail.get('message_count')}")

    # ── 5. 端口暴露 ──────────────────────────────────────
    if args.no_ports:
        skip("端口暴露检查", "--no-ports")
    else:
        ports = [int(p) for p in args.ports.split(",") if p.strip()]
        check_ports(host, ports, control_port=args.control_port or port)

    print()
    if failures:
        print(f"{len(failures)} 项失败：" + "; ".join(failures))
        return 1
    print("全部通过" + (f"（跳过 {len(skips)} 项）" if skips else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
