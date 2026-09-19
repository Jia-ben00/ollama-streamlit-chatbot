"""经真 nginx 的反代验收：量**到达时刻**，不看配置文本。

和其它几个验收脚本的分工（都在 tests/e2e/，别混）：

| 脚本 | 在哪跑 | 需要什么 | 回答什么 |
|---|---|---|---|
| `smoke.py` | 本机 | 真 MySQL + 假 Ollama | 代码拼起来能跑吗 |
| `.github/scripts/container_smoke.sh` | 服务器 / CI | Docker | 容器化部署成立吗 |
| `public_check.py` | **从外面**（你的笔记本） | 一个能访问的 URL | 上线后的公网入口成立吗 |
| `nginx_check.py`（本文件） | 有 Docker 的机器 | compose 已在跑 | **上云之前**：仓库那份 nginx 配置经真 nginx 之后，流式还是真流式吗 |

和 `public_check.py` 的区别只在于「位置」：它要你先把服务上线、再从另一台机器打进来；
本脚本用**同一个 nginx 镜像、同一份仓库模板**在本地/CI 起一个反代容器接上去量，
所以上云之前就能把这一层验掉。

为什么必须带一个「反例」
------------------------
只断言「经 nginx 是增量的」，证明不了一件事：**这把尺子在这里是不是恒真**。
所以脚本会起第二个反代（配置里把 `proxy_buffering` 打开），并要求它**必须量出 BUFFERED**。
如果那个也判成增量，说明尺子在这一层量不出东西，A 的「通过」也就说明不了任何事。
这和 `tests/test_stream_probe.py` 里的能力检查是同一个道理，只是搬到了运行时。

反例为什么用 `plain_sse.py` 而不是真应用
----------------------------------------
实测（nginx 1.27.4，同一台机器只改一个变量）：**nginx 对 chunked 分帧的响应本来就不攒批**，
而真应用（uvicorn）正是 chunked —— 拿它当反例，开着缓冲也照样是增量的，反例红不了。
`plain_sse.py` 用最朴素的分帧（靠关连接表示结束），那才是这个经典的失败形态。

⚠️ 踩过的坑（两个，都是「看着在测、其实没测」）：
1. 第一版反例想用 `proxy_hide_header X-Accel-Buffering` 去「摘掉」应用的防缓冲头。
   那个指令只影响**发给客户端**的响应头，而 nginx 是在上游模块里读到这个头当场就关掉缓冲的 ——
   看着把变量摘掉了，其实一点没摘。于是几个变体全是 INCREMENTAL，像是「配置怎么写都行」。
   **受控变量必须真的被控制住**，否则实验结论是假的。
2. 反例要改的是**两个**变量：`proxy_buffering` 和**上游**。第一版只改了前者、
   直接复用 A 的渲染结果（上游还是 `api:8000`），于是 B 打到的仍是真应用 ——
   而真应用是 chunked 分帧，nginx 对它本来就不攒批，反例永远红不了。
   失败形态还不是一条好懂的「判定=INCREMENTAL」，而是 `ConnectionResetError`
   （`POST /` 打真应用收到 405 后连接被直接切断）。
   现在有一条显式断言钉住「反例的上游必须是 stub」。

⚠️ 这台机器上**没有 Docker**，所以本脚本在上云之前**从未真正执行过**——
CI 上的第一次运行就是它的第一次运行，而它红了（上面第 2 条）。
「本机验不了」不等于「可以先不验」：真正能兜住它的只有 CI 上的真跑。

退出码：0 通过；1 有断言不成立；2 环境不满足（没有 Docker / compose 没在跑）。
"""

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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

import requests  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

TEMPLATE = REPO / "deploy" / "nginx" / "templates" / "default.conf.template"
NGINX_IMAGE = os.getenv("NGINX_IMAGE", "nginx:1.27-alpine")
PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def info(msg):
    print(f"       {msg}")


def show(metrics, reason):
    info(reason)
    info(f"块数={metrics['chunks']} 跨度={metrics['span']}s "
         f"首块={metrics['first']}s 总={metrics['total']}s")


def sh(cmd, *, check_rc=True, timeout=180):
    """跑一条 docker 命令。fail-closed：失败就把 stderr 原样带出来。"""
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    if check_rc and p.returncode != 0:
        raise RuntimeError(f"命令失败（rc={p.returncode}）：{' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout.strip()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.5)
    r = s.connect_ex((host, port)) == 0
    s.close()
    return r


def wait_port(port, seconds=30):
    for _ in range(int(seconds * 2)):
        if port_open(port):
            return True
        time.sleep(0.5)
    return False


def render(template_text, env):
    """按 nginx 镜像 envsubst 的语义渲染：单趟替换、只替换环境里存在的名字。"""
    return PLACEHOLDER_RE.sub(lambda m: env.get(m.group(1), m.group(0)), template_text)


def compose_network():
    """从 api 容器反查 compose 网络名 —— 反代容器要挂到同一个网络才解析得到服务名。"""
    cids = sh(["docker", "ps", "--filter", "label=com.docker.compose.service=api",
               "--format", "{{.ID}}"]).splitlines()
    if not cids:
        return None, None
    cid = cids[0]
    nets = sh(["docker", "inspect", "-f",
               "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}", cid]).split()
    return cid, (nets[0] if nets else None)


def start_nginx(name, network, port, *, mount_templates=None, mount_conf=None, env=None,
                add_host_gateway=False):
    cmd = ["docker", "run", "-d", "--rm", "--name", name, "--network", network,
           "-p", f"127.0.0.1:{port}:{port}"]
    if add_host_gateway:
        cmd += ["--add-host", "host.docker.internal:host-gateway"]
    if mount_templates:
        cmd += ["-v", f"{mount_templates}:/etc/nginx/templates:ro"]
    if mount_conf:
        cmd += ["-v", f"{mount_conf}:/etc/nginx/conf.d/default.conf:ro"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd.append(NGINX_IMAGE)
    cid = sh(cmd)
    if not cid:
        raise RuntimeError(f"{name} 没起来")
    return cid


def cleanup(names):
    for n in names:
        sh(["docker", "rm", "-f", n], check_rc=False)


def main():
    ap = argparse.ArgumentParser(description="经真 nginx 的反代验收")
    ap.add_argument("--user-id", type=int, default=1)
    ap.add_argument("--model-id", type=int, default=1)
    ap.add_argument("--prompt", default="请用大约 50 个字介绍你自己，分几句话说完。")
    ap.add_argument("--stub-port", type=int, default=None,
                    help="反例用的裸 SSE 上游端口（默认自动挑一个空闲端口）")
    ap.add_argument("--keep", action="store_true", help="失败时保留容器便于排查")
    args = ap.parse_args()

    try:
        sh(["docker", "version", "--format", "{{.Server.Version}}"])
    except Exception as exc:
        print(f"[SKIP] 没有可用的 Docker：{exc}")
        return 2

    api_cid, network = compose_network()
    if not api_cid or not network:
        print("[FAIL] 找不到正在运行的 api 容器。")
        print("       先起 compose（或先跑 `.github/scripts/container_smoke.sh`），本脚本要在它之上加反代。")
        return 2
    info(f"api 容器 {api_cid[:12]}  网络 {network}")

    port_a, port_b = free_port(), free_port()
    stub_port = args.stub_port or free_port()
    workdir = Path(tempfile.mkdtemp(prefix="nginx_check_"))
    stub = None
    try:
        # ── 反例的上游：靠关连接结束的 SSE ────────────────────────────────
        env = dict(os.environ, PLAIN_SSE_HOST="0.0.0.0", PLAIN_SSE_PORT=str(stub_port))
        stub = subprocess.Popen([sys.executable, str(Path(__file__).parent / "plain_sse.py")],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port(stub_port, 15):
            check("裸 SSE 上游起得来（反例的前提）", False, f"端口 {stub_port} 没监听")
            return 1
        info(f"裸 SSE 上游（靠关连接结束）在 0.0.0.0:{stub_port}")

        # ── A：仓库里的配置，走镜像自己的 templates 机制（= 上线时的真实路径）──
        start_nginx("nginx_check_a", network, port_a,
                    mount_templates=TEMPLATE.parent.as_posix(),
                    env={"SERVER_NAME": "_", "API_UPSTREAM": "api:8000", "LISTEN_PORT": str(port_a)})
        if not wait_port(port_a, 30):
            check("反代容器 A 起得来", False, sh(["docker", "logs", "--tail", "20", "nginx_check_a"],
                                                check_rc=False))
            return 1
        for _ in range(20):
            try:
                if requests.get(f"http://127.0.0.1:{port_a}/health", timeout=3).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)

        # 自检：镜像渲染出来的配置，必须和我们自己的渲染一致 ——
        # B 用的是我们渲染的文本，这一条保证 B 的配置与真实路径同源。
        conf_a_inside = sh(["docker", "exec", "nginx_check_a",
                            "cat", "/etc/nginx/conf.d/default.conf"])
        ours = render(TEMPLATE.read_text(encoding="utf-8"),
                      {"SERVER_NAME": "_", "API_UPSTREAM": "api:8000", "LISTEN_PORT": str(port_a)})
        check("镜像渲染的配置与本地渲染逐字符一致（反例的渲染器可信）",
              conf_a_inside.strip() == ours.strip(),
              "不一致 ⇒ 我们对 envsubst 的模拟有出入，反例那份配置就不代表真实路径")

        # ── A 的测量：真应用（chunked 分帧）经真 nginx ────────────────────
        print()
        print("── A：仓库配置 → api（真应用，chunked 分帧）──")
        r = requests.post(f"http://127.0.0.1:{port_a}/conversations", timeout=15,
                          json={"title": "nginx_check", "model_id": args.model_id,
                                "user_id": args.user_id})
        if r.status_code not in (200, 201):
            check("经反代建会话", False, f"HTTP {r.status_code} {r.text[:160]}")
            return 1
        conv_id = r.json()["id"]

        headers, events, total = read_sse("127.0.0.1", port_a,
                                          {"conversation_id": conv_id, "content": args.prompt})
        arrivals = [t for e, t in events if "chunk" in e]
        verdict_a, m_a, reason_a = judge_incremental(arrivals, total)
        show(m_a, reason_a)
        check("经真 nginx：回复仍是逐块到达的", verdict_a == INCREMENTAL,
              reason_a if verdict_a != INCREMENTAL
              else f"{m_a['chunks']} 块，跨度 {m_a['span']}s")
        check("响应头里看不到 X-Accel-Buffering（nginx 会消费掉它）",
              "x-accel-buffering" not in headers.lower(),
              "这一条是**记录事实**，不是要求：应用侧那个头发不到客户端，"
              "所以公网侧只能靠到达时刻判断")

        # ── B：反例 —— 开着 proxy_buffering，上游靠关连接结束 ─────────────
        #
        # ⚠️ 反例要改的是**两个**变量：缓冲开关，和上游。只改前者会得到一个
        # 「看着像反例、其实不是」的东西 —— 第一版就是这么写的：直接复用 A 的渲染结果
        # （上游 `api:8000`），于是 B 打到的还是真应用。真应用是 chunked 分帧，
        # 而 nginx 对 chunked 本来就不攒批 —— 反例永远红不了，且失败形态是
        # `ConnectionResetError`（POST / 打真应用收到 405 后连接被直接切断），
        # 而不是一条「判定=INCREMENTAL」的好看报错。CI 上真跑第一次才暴露。
        #
        # stub 跑在宿主机上，容器里只能走 host.docker.internal（下面 docker run
        # 带了 host-gateway），所以上游地址得跟着换成它。
        stub_upstream = f"host.docker.internal:{stub_port}"
        conf_b_text = render(TEMPLATE.read_text(encoding="utf-8"),
                             {"SERVER_NAME": "_", "API_UPSTREAM": stub_upstream,
                              "LISTEN_PORT": str(port_b)})
        buf_conf = conf_b_text.replace("    proxy_buffering off;", "    proxy_buffering on;")
        check("反例配置确实改动了（否则反例不是反例）", buf_conf != conf_b_text,
              "模板里找不到 `    proxy_buffering off;`，锚点失效")
        if buf_conf == conf_b_text:
            return 1
        # 这一条就是上面那个坑的守卫：反例的上游必须是 close-delimited 的 stub，
        # 打到 chunked 的真应用上它红不了，反例也就失去了意义。
        upstream_ok = f"proxy_pass http://{stub_upstream};" in buf_conf
        check("反例的上游指向裸 SSE stub（不是真应用）", upstream_ok,
              f"反例必须打到 close-delimited 的 stub（{stub_upstream}）；"
              "打到真应用（chunked）上它永远不会被判成攒批")
        if not upstream_ok:
            # 前提已经破了，继续跑只会得到一个看不懂的报错，不如在这里停住。
            print()
            print(f"{len(failures)} 项失败：" + "; ".join(failures))
            return 1
        conf_b = workdir / "default.conf"
        conf_b.write_bytes(buf_conf.encode("utf-8"))

        start_nginx("nginx_check_b", network, port_b, mount_conf=conf_b.as_posix(),
                    add_host_gateway=True)
        if not wait_port(port_b, 30):
            check("反例容器 B 起得来", False, sh(["docker", "logs", "--tail", "20", "nginx_check_b"],
                                                check_rc=False))
            return 1

        print()
        print("── B（反例）：开 proxy_buffering → 裸 SSE 上游（靠关连接结束）──")
        _, events_b, total_b = read_sse("127.0.0.1", port_b, {}, path="/")
        arrivals_b = [t for e, t in events_b if "chunk" in e]
        verdict_b, m_b, reason_b = judge_incremental(arrivals_b, total_b)
        show(m_b, reason_b)
        check("反例确实被判成攒批（证明这把尺子在这里量得出东西）",
              verdict_b == BUFFERED,
              f"实测判定={verdict_b}，期望 {BUFFERED}。"
              f"若为 {INCREMENTAL}：判据在这一层失效，A 的「通过」说明不了任何事；"
              f"若为 {INCONCLUSIVE}：块数不够，把上游的块数或间隔调大")

        print()
        if failures:
            print(f"{len(failures)} 项失败：" + "; ".join(failures))
            return 1
        print("全部通过：经真 nginx 流式没有退化成攒批，且判据在反例上确实会红。")
        return 0
    finally:
        if not (args.keep and failures):
            cleanup(["nginx_check_a", "nginx_check_b"])
        if stub:
            stub.terminate()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
