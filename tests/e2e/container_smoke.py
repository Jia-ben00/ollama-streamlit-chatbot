"""容器冒烟：对着一套**真的在 Docker 里跑起来**的服务打断言。

和另外两个冒烟脚本的分工：

| 脚本 | 被测对象 | 能证明什么 | 跑在哪 |
|---|---|---|---|
| `tests/test_deploy_manifest.py` | 部署**文件** | 配置写对了（纯静态解析，不需要 Docker） | CI 每次 push |
| `tests/e2e/smoke.py` | 裸进程（uvicorn 直起） | 代码逻辑拼起来是通的 | 本地，手动 |
| `tests/e2e/container_smoke.py`（本文件） | **docker compose 整套** | 镜像能构建、容器之间能互通、**容器里的 MySQL 表真的是 utf8mb4** | CI 每次 push + 上线后验收 |

为什么必须单独有这一层：静态校验只能证明「文件里写了正确的规则」，证明不了
「规则真的生效」。这个项目上一轮补了 19 条部署清单静态校验，但那 19 条全是
「读文件比对文本」——它们拦住了配置写错，但拦不住这些只有真跑才暴露的问题：

- 镜像里到底有没有 `.env`（`.dockerignore` 规则写了 ≠ 生效了；CRLF 会让规则静默失效）；
- 容器能不能解析 `host.docker.internal`（Linux 上要 `extra_hosts`，本机是 Windows 永远复现不出来）；
- `REDIS_URL` 指向服务名这件事有没有真的生效（写成 127.0.0.1 不报错，只是永远不缓存）；
- MySQL 容器里 `--character-set-server=utf8mb4` 是否真的让表能存 4 字节字符。

本文件只做**从宿主机打发布端口**能做的断言（HTTP 层可见的一切）。
容器内部的检查（镜像内容、uid、端口映射）在编排脚本
`.github/scripts/container_smoke.sh` 里做，因为那些要用 docker CLI。

环境变量：
    API_BASE   默认 http://127.0.0.1:8000
"""

import os
import sys

import requests

# 复用 smoke.py 里的裸 socket 流式测量，而不是再写一份：
# 那段代码绕开了 requests 的 512 字节阻塞读（否则会把「服务端每 50ms 推一块」
# 误读成「每 450ms 来一批」）。这种测量手段一旦有两份实现，早晚会分叉，
# 而分叉之后其中一份就会给出错误的结论。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoke import sse_raw  # noqa: E402

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")
CHUNK_COUNT = 9  # 与 fake_ollama.CHUNKS 的长度一致

failures = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    s = requests.Session()

    try:
        s.get(f"{API_BASE}/", timeout=10)
    except requests.RequestException as exc:
        print(f"[FAIL] 连不上 {API_BASE}：{exc}")
        return 2

    # ── 1. 三个依赖探针 —— 本轮容器验证的核心证据 ──────────
    # 这三条各自对应一处「静态校验拦不住」的部署缺陷，是真正的验收点。
    h = s.get(f"{API_BASE}/health", timeout=10).json()
    checks = h.get("checks", {})

    check("容器能连上 MySQL 容器（compose 服务名解析）",
          checks.get("database") is True, f"checks={checks}")

    check("容器能连上 Redis 容器（REDIS_URL 指向服务名）",
          checks.get("redis") is True,
          "REDIS_URL 缺失/写成 127.0.0.1 时这里会是 False：容器里的 127.0.0.1 是它自己")

    check("容器能访问宿主机上的 Ollama（extra_hosts + host-gateway 生效）",
          checks.get("ollama") is True,
          "Linux 上缺 extra_hosts 会名字解析失败；本机 Windows 用 Docker Desktop 复现不出来")

    check("/health 整体状态是 ok", h.get("status") == "ok", f"status={h.get('status')}")

    r = s.get(f"{API_BASE}/health/ready", timeout=10)
    check("/health/ready 返回 200（三依赖全通才就绪）",
          r.status_code == 200, f"HTTP {r.status_code} {r.text[:160]}")

    check("/health 在依赖异常时也返回 200（存活探针语义，不受上面影响）",
          s.get(f"{API_BASE}/health", timeout=10).status_code == 200)

    # ── 2. 目录接口 ───────────────────────────────────────
    models = s.get(f"{API_BASE}/models", timeout=10).json()
    check("GET /models 返回容器内 MySQL 里的模型", len(models) >= 1,
          f"models={[m.get('name') for m in models]}")
    users = s.get(f"{API_BASE}/users", timeout=10).json()
    check("GET /users 返回容器内 MySQL 里的用户", len(users) >= 1,
          f"users={[u.get('username') for u in users]}")

    if not models or not users:
        print("\n[ABORT] 缺模型或用户，后续断言没有意义（种子数据没灌进去？）")
        return 1

    user_id, model_id = users[0]["id"], models[0]["id"]

    # ── 3. 建会话 ─────────────────────────────────────────
    r = s.post(f"{API_BASE}/conversations",
               json={"title": "容器冒烟会话", "model_id": model_id, "user_id": user_id},
               timeout=10)
    check("POST /conversations 创建成功（外键指向容器内的 users/models）",
          r.status_code == 201, f"HTTP {r.status_code} {r.text[:160]}")
    conv_id = r.json()["id"]

    emoji_conv = s.post(f"{API_BASE}/conversations",
                        json={"title": "emoji 会话", "model_id": model_id, "user_id": user_id},
                        timeout=10).json()["id"]

    # ── 4. 流式聊天 ───────────────────────────────────────
    prompt = "武汉今天天气怎么样？😀🔥"
    headers, events, total = sse_raw(conv_id, prompt, timeout=30.0)

    check("POST /chat 的 Content-Type 是 text/event-stream",
          "text/event-stream" in headers.lower(),
          next((l for l in headers.split("\r\n") if l.lower().startswith("content-type")), "?"))

    chunks = [e[0]["chunk"] for e in events if "chunk" in e[0]]
    arrivals = [e[1] for e in events if "chunk" in e[0]]
    done = [e for e in events if e[0].get("done")]

    check(f"流式返回 {CHUNK_COUNT} 个 chunk（容器 -> 宿主机假 Ollama -> 容器）",
          len(chunks) == CHUNK_COUNT, f"reply={''.join(chunks)!r}")
    check("流以 done 事件收尾且带 latency_ms",
          len(done) == 1 and done[0][0]["latency_ms"] >= 0,
          f"latency_ms={done[0][0]['latency_ms'] if done else 'N/A'}")

    # 攒批的判据：服务端每 50ms 推一块，若某处攒了批，间隔会变成 9×50ms = 450ms。
    # 上界取 0.30s 既排除攒批，又给 CI 这类慢机器留了余量（避免偶发误报）。
    gaps = [round(arrivals[i + 1] - arrivals[i], 3) for i in range(len(arrivals) - 1)]
    check("每块间隔贴合服务端节奏（容器链路没有攒批）",
          bool(gaps) and all(0.02 <= g <= 0.30 for g in gaps), f"gaps={gaps}")
    check("首块在 1 秒内到达（没有等生成完才返回）",
          bool(arrivals) and arrivals[0] < 1.0, f"首块 {arrivals[0] if arrivals else 'N/A'}s / 总 {total}s")

    # ── 5. 落库与游标分页 ─────────────────────────────────
    stored = s.get(f"{API_BASE}/conversations/{conv_id}/messages", timeout=10).json()
    check("消息角色顺序为 user -> assistant", [m["role"] for m in stored] == ["user", "assistant"],
          f"roles={[m['role'] for m in stored]}")
    check("user 消息内容原样落库", stored and stored[0]["content"] == prompt)
    check("assistant 消息写了 latency_ms", len(stored) > 1 and stored[1]["latency_ms"] is not None)

    page = s.get(f"{API_BASE}/conversations/{conv_id}/messages",
                 params={"before_id": stored[1]["id"], "limit": 1}, timeout=10).json()
    check("游标分页 before_id 生效",
          len(page) == 1 and page[0]["id"] < stored[1]["id"], f"ids={[m['id'] for m in page]}")

    # ── 6. emoji 经「容器内 MySQL」往返 ────────────────────
    # 这是 charset 修复的验收点：MySQL 容器若不接受 --character-set-server=utf8mb4，
    # 建出来的表默认字符集就不是 utf8mb4，写 4 字节 emoji 会报
    # 1366 Incorrect string value，这一条就会红。
    sse_raw(emoji_conv, "表情测试 🚀😀🔥", timeout=30.0)
    rows = s.get(f"{API_BASE}/conversations/{emoji_conv}/messages", timeout=10).json()
    check("emoji 经「容器内 MySQL」往返无损（表字符集真的是 utf8mb4）",
          bool(rows) and rows[0]["content"] == "表情测试 🚀😀🔥",
          repr(rows[0]["content"]) if rows else "空")

    # ── 7. 错误分支与归档 ─────────────────────────────────
    check("给不存在的会话发消息返回 404",
          s.post(f"{API_BASE}/chat", json={"conversation_id": 999999, "content": "hi"},
                 timeout=10).status_code == 404)
    check("空消息被 Pydantic 拦下返回 422",
          s.post(f"{API_BASE}/chat", json={"conversation_id": conv_id, "content": ""},
                 timeout=10).status_code == 422)
    check("PATCH 归档把 is_archived 置为 true",
          s.patch(f"{API_BASE}/conversations/{conv_id}", timeout=10).json()["is_archived"] is True)

    print()
    if failures:
        print(f"{len(failures)} 项失败：" + "; ".join(failures))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
