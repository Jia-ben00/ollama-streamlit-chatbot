"""经真 nginx 的反代验收：量**到达时刻**，不看配置文本。

和其它几个验收脚本的分工（都在 tests/e2e/，别混）：

| 脚本 | 在哪跑 | 需要什么 | 回答什么 |
|---|---|---|---|
| `smoke.py` | 本机 | 真 MySQL + 假 Ollama | 代码拼起来能跑吗 |
| `.github/scripts/container_smoke.sh` | 服务器 / CI | Docker | 容器化部署成立吗 |
| `public_check.py` | **从外面**（你的笔记本） | 一个能访问的 URL | 上线后的公网入口成立吗 |
| `nginx_check.py`（本文件） | 有 Docker 的机器 | compose 已在跑 | **上云之前**：仓库那份 nginx 配置经真 nginx 之后，流式还是真流式吗 |

本文件里有三段，各自都要有反例（只有正例的「通过」什么都证明不了）：

- **A**：仓库的**明文**配置（`deploy/nginx/templates`）→ 真应用（chunked 分帧）；
- **B**：反例 —— 开着 `proxy_buffering`、上游换成靠关连接结束的 `plain_sse.py`，
  **必须**被判成 BUFFERED；
- **C**：仓库的 **HTTPS** 配置（`deploy/nginx/tls`）→ 真应用，自签证书、真 TLS 握手，
  再做两个反例：默认信任链**必须**拒绝这张自签证书（证明校验真的开着）、
  开着 buffering 的 TLS 变体**必须**被判成攒批（证明这把尺子在 TLS 下不瞎）。

为什么 C 段值得存在：TLS 长期是本项目**唯一一整个没验过的层**，当初的理由是
「ACME 签发要有域名 + 公网 IP」。那个理由只覆盖签发那一半 —— 「TLS 之后流式还活着吗」
和「这把尺子在 TLS 下量得出攒批吗」根本不需要域名，自签一张证书就能量。
（`read_sse` 的 `use_tls` 分支此前**从未被执行过**：没验过的代码就是没写过的代码。）
真正还没验的只剩 ACME 签发与续期那一段，见 docs/DEPLOY.md §6.2。

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

⚠️ 踩过的坑（三个，都是「看着在测、其实没测」）：
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
3. 修完上游之后**还是红**，而且症状一样。根因是 `wait_port()` 把「端口开着」
   当成了「就绪」：`docker run -p` 是 **docker-proxy 先把宿主机端口绑上**，
   容器里的 nginx 随后才（或压根没）起来 —— 于是探测以为就绪、接着连上去被 RST。
   **这和 container_smoke 那条「端口 ≠ 就绪」是同一个坑，只是换了层。**
   现在改成等「真的能收发 HTTP」（任何状态码都算，502 也算 nginx 在服务），
   并且失败时把**容器状态 + 日志**打出来 —— **失败要失败得好懂**。
   同时把 stub 从宿主机挪进同一个 compose 网络：少一个 `host.docker.internal` 环节，
   而且和生产里「上游是网络内服务名」同形。

⚠️ 这台机器上**没有 Docker**，所以本脚本在上云之前**从未真正执行过**——
CI 上的第一次运行就是它的第一次运行，而它连着红了两次（上面第 2、3 条）。
「本机验不了」不等于「可以先不验」：真正能兜住它的只有 CI 上的真跑。

退出码：0 通过；1 有断言不成立；2 环境不满足（没有 Docker / compose 没在跑）。
"""

import argparse
import os
import re
import shutil
import socket
import ssl
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
TEMPLATES_DIR = TEMPLATE.parent                      # 明文那一份
TEMPLATES_TLS_DIR = REPO / "deploy" / "nginx" / "tls"  # HTTPS 那一份
NGINX_IMAGE = os.getenv("NGINX_IMAGE", "nginx:1.27-alpine")
PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# HTTPS 模板写死的证书文件名（容器内 /etc/nginx/certs/ 下）。与
# deploy/nginx/tls/default.conf.template、deploy.sh、docs/DEPLOY.md §6.2 是同一组约定。
CERT_FILE = "fullchain.pem"
KEY_FILE = "privkey.pem"

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


def wait_http(port, path="/", seconds=30):
    """等「真的能收发 HTTP」，而不是「端口开着」。

    ⚠️ 这条是本项目栽过的老坑的同一形态：`docker run -p` 会让 docker-proxy
    **在容器里的进程还没起来（甚至已经退出）时就把宿主机端口绑上** ——
    所以 `port_open()` 为真不等于反代就绪，随后连上去会被 RST。
    这正是 container_smoke 那条「端口断言必须等」的同一个道理，只是换了层。
    任何 HTTP 响应（含 502）都算「nginx 在服务」，因为它已经能建连并回话。
    """
    for _ in range(int(seconds * 2)):
        try:
            requests.get(f"http://127.0.0.1:{port}{path}", timeout=2)
            return True
        except requests.RequestException:
            time.sleep(0.5)
    return False


def wait_redirect(port, seconds=30):
    """等明文入口开始回 3xx（TLS 段的就绪判据），返回那个响应。

    ⚠️ 这里不能用 `wait_http()`：`requests` 默认**跟随跳转**，跟到 https 之后容器里
    没有可信 CA、连接直接失败，于是「服务已经好了」被当成「还没起来」，
    一直等到超时 —— 又是一个「探针自己不成立」的形态。
    """
    for _ in range(int(seconds * 2)):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2,
                             allow_redirects=False)
            if r.status_code in (301, 302, 307, 308):
                return r
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return None


def find_openssl():
    """找 openssl：先 PATH，再兜 Windows 上 Git for Windows 的固定位置。

    TLS 段要签一张自签证书，而**镜像里没有 openssl**：`nginx:1.27-alpine` 与
    `alpine:3.20` 都实测 `openssl: not found`（基础镜像不带 CLI）。
    宿主机一定有：Linux/macOS 自带，Windows 装了 Git 就有。
    找不到就响亮地失败（见 main 里的提示），**不静默跳过** —— 跳过等于这一层又没验。
    """
    found = shutil.which("openssl")
    if found:
        return found
    for cand in (r"C:\Program Files\Git\usr\bin\openssl.exe",
                 r"C:\Program Files (x86)\Git\usr\bin\openssl.exe"):
        if Path(cand).exists():
            return cand
    return None


def make_self_signed(openssl, outdir):
    """签一张只给 localhost / 127.0.0.1 用的自签证书，写进 outdir。返回 (证书, 私钥)。"""
    cert, key = Path(outdir) / CERT_FILE, Path(outdir) / KEY_FILE
    p = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "2",
         "-subj", "/CN=localhost",
         # ⚠️ SAN 里必须带 IP:127.0.0.1。read_sse 用 `server_hostname=host` 校验，
         # 而 host 就是 127.0.0.1；现代客户端**不再回退到 CN**，只写 CN=localhost
         # 会得到 `IP address mismatch` —— 一个看着像「证书没生效」、
         # 其实是校验策略的报错，很容易往错的方向排查。
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        capture_output=True, text=True, errors="replace", timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"自签证书生成失败（rc={p.returncode}）：{p.stderr.strip()[-400:]}")
    if not (cert.exists() and key.exists()):
        raise RuntimeError("openssl 说成功，但证书或私钥文件不在")
    return cert, key


def diagnose(name):
    """失败时把容器的状态与日志原样带出来 —— 否则只剩一句看不懂的报错。"""
    state = sh(["docker", "inspect", "-f", "{{.State.Status}} exit={{.State.ExitCode}}", name],
               check_rc=False)
    print(f"       [{name}] state: {state or '(容器不在了，可能已 --rm 掉)'}")
    logs = sh(["docker", "logs", "--tail", "30", name], check_rc=False)
    if logs:
        for line in logs.splitlines()[-15:]:
            print(f"       [{name}] {line}")


def render(template_text, env):
    """按 nginx 镜像 envsubst 的语义渲染：单趟替换、只替换环境里存在的名字。"""
    return PLACEHOLDER_RE.sub(lambda m: env.get(m.group(1), m.group(0)), template_text)


def compose_network():
    """从 api 容器反查 compose 网络名与镜像名。

    反代容器要挂到同一个网络才解析得到服务名；镜像名同样是「问正在跑的 api」要来的，
    不硬编码 —— 硬编码 `ollama-streamlit-chatbot-api` 会在项目改名后静默失效。
    """
    cids = sh(["docker", "ps", "--filter", "label=com.docker.compose.service=api",
               "--format", "{{.ID}}"]).splitlines()
    if not cids:
        return None, None, None
    cid = cids[0]
    nets = sh(["docker", "inspect", "-f",
               "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}", cid]).split()
    image = sh(["docker", "inspect", "-f", "{{.Config.Image}}", cid])
    return cid, (nets[0] if nets else None), image


def start_nginx(name, network, port, *, mount_templates=None, mount_conf=None,
                mount_certs=None, env=None, extra_ports=()):
    """起一个反代容器。

    - `mount_templates`：挂**仓库里那份模板目录**到 /etc/nginx/templates，走镜像自己的
      envsubst —— 也就是上线时的真实路径（A、C 段都用它）；
    - `mount_conf`：直接挂一份渲染好的配置（反例用，B 段）；
    - `mount_certs`：HTTPS 段的证书目录。模板里写死读 /etc/nginx/certs/，所以挂载点固定；
    - `extra_ports`：同一个容器里还要映射的**其它**端口。HTTPS 那份配置同时监听明文口
      （只做 301）与 TLS 口，所以这里必须能映射两个。
    """
    cmd = ["docker", "run", "-d", "--rm", "--name", name, "--network", network,
           "-p", f"127.0.0.1:{port}:{port}"]
    for p in extra_ports:
        cmd += ["-p", f"127.0.0.1:{p}:{p}"]
    if mount_templates:
        cmd += ["-v", f"{mount_templates}:/etc/nginx/templates:ro"]
    if mount_conf:
        cmd += ["-v", f"{mount_conf}:/etc/nginx/conf.d/default.conf:ro"]
    if mount_certs:
        cmd += ["-v", f"{mount_certs}:/etc/nginx/certs:ro"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd.append(NGINX_IMAGE)
    cid = sh(cmd)
    if not cid:
        raise RuntimeError(f"{name} 没起来")
    return cid


def start_stub(name, network, image, port):
    """把裸 SSE 上游跑在**容器里、挂在同一个 compose 网络**上。

    为什么不跑在宿主机、让容器走 `host.docker.internal`：
    ① 容器的上游在生产里本来就是**网络内的服务名**（`api:8000`），
       跑在同一个网络里才和真实拓扑同形；
    ② 「容器 → 宿主机」要额外依赖 `--add-host host-gateway`，是另一个可能失效的环节
       （第一版就是这么挂的：nginx 起不来 → docker-proxy 已经把宿主机端口绑上了 →
       端口探测以为就绪 → 连上去被 RST）；
    ③ 复用 api 镜像就够了（`plain_sse.py` 只用标准库），不必再拉一个 python 镜像。
       api 镜像没有 ENTRYPOINT（只有 CMD），所以直接给参数就能覆盖启动命令。
    """
    sh(["docker", "run", "-d", "--rm", "--name", name, "--network", network,
        "-v", f"{(Path(__file__).parent / 'plain_sse.py').as_posix()}:/app/plain_sse.py:ro",
        "-e", "PLAIN_SSE_HOST=0.0.0.0", "-e", f"PLAIN_SSE_PORT={port}",
        image, "python", "/app/plain_sse.py"])


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
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="单次 SSE 读取的上限（秒）。与 read_sse 的默认值一致")
    ap.add_argument("--no-tls", action="store_true",
                    help="跳过 HTTPS（C / D 段）。跳过了就等于 TLS 这一层**没被验证** —— "
                         "只在确实跑不了时用（比如宿主上找不到 openssl），别在 CI 上用")
    args = ap.parse_args()

    # 自签证书得靠宿主上的 openssl：镜像里**没有**（nginx:alpine / alpine 基础镜像都实测
    # `openssl: not found`）。找不到就响亮地失败，不静默跳过 ——
    # 静默跳过会让「TLS 没验过」伪装成「全绿」。
    openssl = None if args.no_tls else find_openssl()
    if args.no_tls:
        print("[warn] --no-tls：跳过 HTTPS 段 —— TLS 这一层这次**没有**被验证")

    try:
        sh(["docker", "version", "--format", "{{.Server.Version}}"])
    except Exception as exc:
        print(f"[SKIP] 没有可用的 Docker：{exc}")
        return 2

    api_cid, network, image = compose_network()
    if not api_cid or not network or not image:
        print("[FAIL] 找不到正在运行的 api 容器。")
        print("       先起 compose（或先跑 `.github/scripts/container_smoke.sh`），本脚本要在它之上加反代。")
        return 2
    info(f"api 容器 {api_cid[:12]}  网络 {network}  镜像 {image}")

    port_a, port_b = free_port(), free_port()
    # stub 跑在容器里、挂在 compose 网络上，所以它的端口不需要在宿主机上腾出来。
    stub_port = args.stub_port or 11436
    workdir = Path(tempfile.mkdtemp(prefix="nginx_check_"))
    try:
        # ── 反例的上游：靠关连接结束的 SSE，跑在同一个 compose 网络里 ──────
        start_stub("nginx_check_stub", network, image, stub_port)
        stub_upstream = f"nginx_check_stub:{stub_port}"
        info(f"裸 SSE 上游（靠关连接结束）在 {stub_upstream}")

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
        # stub 也是一层容器、挂在同一个 compose 网络上，所以上游就是个服务名
        # （和生产里 `api:8000` 同形），不需要 host-gateway 那一套。
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

        start_nginx("nginx_check_b", network, port_b, mount_conf=conf_b.as_posix())
        # 等「能收发 HTTP」，不是等「端口开着」：docker-proxy 会先把宿主机端口绑上，
        # 而容器里的 nginx 可能已经因配置问题退出了（第一版就是这么被误导的）。
        if not wait_http(port_b, seconds=30):
            check("反例容器 B 起得来并能回话", False, "")
            diagnose("nginx_check_b")
            diagnose("nginx_check_stub")
            return 1

        print()
        print("── B（反例）：开 proxy_buffering → 裸 SSE 上游（靠关连接结束）──")
        try:
            _, events_b, total_b = read_sse("127.0.0.1", port_b, {}, path="/")
        except OSError as exc:
            # 裸 socket 被 RST 时不要抛栈了事 —— 把容器状态与日志带出来。
            # 否则只剩一句 `ConnectionResetError`，看日志得绕一圈才知道是上游打错了。
            check("反例链路读得通", False, f"{type(exc).__name__}: {exc}")
            diagnose("nginx_check_b")
            diagnose("nginx_check_stub")
            print()
            print(f"{len(failures)} 项失败：" + "; ".join(failures))
            return 1
        arrivals_b = [t for e, t in events_b if "chunk" in e]
        verdict_b, m_b, reason_b = judge_incremental(arrivals_b, total_b)
        show(m_b, reason_b)
        check("反例确实被判成攒批（证明这把尺子在这里量得出东西）",
              verdict_b == BUFFERED,
              f"实测判定={verdict_b}，期望 {BUFFERED}。"
              f"若为 {INCREMENTAL}：判据在这一层失效，A 的「通过」说明不了任何事；"
              f"若为 {INCONCLUSIVE}：块数不够，或 stub 没被连上（下面有容器日志）")
        if verdict_b != BUFFERED:
            diagnose("nginx_check_b")
            diagnose("nginx_check_stub")

        # ── C：HTTPS（TLS 终结）──────────────────────────────────────────
        #
        # 这一段回答两件**都不需要域名**的事：
        #   ① 经过真 TLS 握手之后，流式还是逐块到达的吗；
        #   ② 这把尺子在 TLS 下还量得出攒批吗 —— TLS 有记录层分帧，会改变 recv() 的
        #      切分方式，不测就只是猜（和 B 段「chunked 本来就不攒批」是同一类反直觉）。
        # 证书用自签的：TLS 终结这层的正确性与「证书是谁签的」无关；
        # 真正需要域名的是签发与续期（ACME）那一半，见 docs/DEPLOY.md §6.2。
        print()
        print("── C：仓库的 HTTPS 配置 → 真 TLS 握手 → api ──")
        if not openssl:
            check("HTTPS 段能跑（需要宿主上的 openssl 来签自签证书）", False,
                  "找不到 openssl。镜像里没有（nginx:alpine 与 alpine 基础镜像都实测不带 CLI），"
                  "只能借宿主的：Linux/macOS 自带，Windows 装了 Git 就有。"
                  "确实跑不了时用 --no-tls 显式跳过 —— 但那一跳就等于 TLS 这层没被验证")
            print()
            print(f"{len(failures)} 项失败：" + "; ".join(failures))
            return 1

        certs_dir = workdir / "certs"
        certs_dir.mkdir(exist_ok=True)
        try:
            cert, key = make_self_signed(openssl, certs_dir)
        except RuntimeError as exc:
            check("自签证书生成", False, str(exc))
            print()
            print(f"{len(failures)} 项失败：" + "; ".join(failures))
            return 1
        info(f"自签证书：{cert.name} + {key.name}（SAN 含 DNS:localhost / IP:127.0.0.1）")

        tls_template = TEMPLATES_TLS_DIR / "default.conf.template"
        port_c, port_c_plain = free_port(), free_port()
        env_c = {"SERVER_NAME": "_", "API_UPSTREAM": "api:8000",
                 "LISTEN_PORT": str(port_c_plain), "LISTEN_TLS_PORT": str(port_c)}
        start_nginx("nginx_check_c", network, port_c,
                    mount_templates=TEMPLATES_TLS_DIR.as_posix(),
                    mount_certs=certs_dir.as_posix(),
                    env=env_c, extra_ports=[port_c_plain])

        plain_c = wait_redirect(port_c_plain, seconds=30)
        if plain_c is None:
            check("HTTPS 反代容器 C 起得来（明文口开始回 301）", False, "")
            diagnose("nginx_check_c")
            print()
            print(f"{len(failures)} 项失败：" + "; ".join(failures))
            return 1
        loc = plain_c.headers.get("Location", "")
        # 本机这两个端口是临时挑的，所以只断言「跳到了 https 且路径没丢」；
        # 生产是 80 → 443，模板里 `https://$host$request_uri` 拼出来正好对。
        check("明文入口只做跳转：3xx → https 且路径保留",
              loc.startswith("https://") and loc.endswith("/health"),
              f"{plain_c.status_code} → {loc}")

        # 自检：镜像渲染出来的配置 == 我们本地渲染的。下面那份「TLS 攒批反例」由本地渲染生成，
        # 这一条保证它与真实路径同源（A 段同样的做法）。
        conf_c_inside = sh(["docker", "exec", "nginx_check_c",
                            "cat", "/etc/nginx/conf.d/default.conf"])
        ours_c = render(tls_template.read_text(encoding="utf-8"), env_c)
        check("HTTPS 模板经镜像渲染后与本地渲染逐字符一致",
              conf_c_inside.strip() == ours_c.strip(),
              "不一致 ⇒ 反例那份配置不代表真实路径，它的结论也就不算数")

        # ── C 的测量：真应用，经真 TLS ──────────────────────────────────
        h = requests.get(f"https://127.0.0.1:{port_c}/health", timeout=15, verify=str(cert))
        check("经真 TLS：/health 200（握手与证书校验都过了）", h.status_code == 200,
              f"HTTP {h.status_code}")

        r_c = requests.post(f"https://127.0.0.1:{port_c}/conversations", timeout=15,
                            verify=str(cert),
                            json={"title": "nginx_check_tls", "model_id": args.model_id,
                                  "user_id": args.user_id})
        if r_c.status_code not in (200, 201):
            check("经真 TLS 建会话", False, f"HTTP {r_c.status_code} {r_c.text[:160]}")
            print()
            print(f"{len(failures)} 项失败：" + "; ".join(failures))
            return 1
        conv_c = r_c.json()["id"]

        headers_c, events_c, total_c = read_sse(
            "127.0.0.1", port_c, {"conversation_id": conv_c, "content": args.prompt},
            use_tls=True, ca_file=str(cert))
        arrivals_c = [t for e, t in events_c if "chunk" in e]
        verdict_c, m_c, reason_c = judge_incremental(arrivals_c, total_c)
        show(m_c, reason_c)
        check("经真 TLS：回复仍是逐块到达的（TLS 没有把它攒起来）",
              verdict_c == INCREMENTAL,
              reason_c if verdict_c != INCREMENTAL
              else f"{m_c['chunks']} 块，跨度 {m_c['span']}s")

        # 反例①：默认信任链**必须**拒绝这张自签证书。
        # 如果它居然连上了，说明证书校验根本没开（或被人关掉了）—— 那 C 段那条「通过」
        # 只能证明「有人愿意跟我说话」，证明不了「对面是这个服务」。
        trust_err = None
        try:
            read_sse("127.0.0.1", port_c, {"conversation_id": conv_c, "content": args.prompt},
                     use_tls=True, timeout=args.timeout)
        except ssl.SSLError as exc:
            trust_err = exc
        check("反例：默认信任链拒绝自签证书（证明证书校验真的开着）",
              isinstance(trust_err, ssl.SSLCertVerificationError),
              f"期望 SSLCertVerificationError，实际="
              f"{type(trust_err).__name__ if trust_err else '连上了、没有任何报错'}"
              " —— 不报错就意味着任何中间人都能冒充这台服务器")

        # 反例②：把 HTTPS 模板的 proxy_buffering 打开、上游换成靠关连接结束的 stub，
        # 必须被判成攒批。与 B 段同一个道理，只是通道换成了 TLS。
        port_d, port_d_plain = free_port(), free_port()
        env_d = {"SERVER_NAME": "_", "API_UPSTREAM": stub_upstream,
                 "LISTEN_PORT": str(port_d_plain), "LISTEN_TLS_PORT": str(port_d)}
        conf_d_text = render(tls_template.read_text(encoding="utf-8"), env_d)
        buf_d = conf_d_text.replace("    proxy_buffering off;", "    proxy_buffering on;")
        check("HTTPS 反例配置确实改动了（否则反例不是反例）", buf_d != conf_d_text,
              "HTTPS 模板里找不到 `    proxy_buffering off;`，锚点失效")
        upstream_ok_d = f"proxy_pass http://{stub_upstream};" in buf_d
        check("HTTPS 反例的上游指向裸 SSE stub（不是真应用）", upstream_ok_d,
              f"反例必须打到 close-delimited 的 stub（{stub_upstream}）；"
              "打到真应用（chunked）上它永远不会被判成攒批")
        if buf_d == conf_d_text or not upstream_ok_d:
            print()
            print(f"{len(failures)} 项失败：" + "; ".join(failures))
            return 1

        tls_buf_dir = workdir / "tls_buf"
        tls_buf_dir.mkdir(exist_ok=True)
        (tls_buf_dir / "default.conf.template").write_bytes(buf_d.encode("utf-8"))
        start_nginx("nginx_check_d", network, port_d,
                    mount_templates=tls_buf_dir.as_posix(),
                    mount_certs=certs_dir.as_posix(),
                    env=env_d, extra_ports=[port_d_plain])
        if wait_redirect(port_d_plain, seconds=30) is None:
            check("反例容器 D 起得来并能回话", False, "")
            diagnose("nginx_check_d")
            diagnose("nginx_check_stub")
            return 1

        print()
        print("── D（反例）：TLS + proxy_buffering on → 裸 SSE 上游 ──")
        try:
            _, events_d, total_d = read_sse("127.0.0.1", port_d, {}, path="/",
                                            use_tls=True, ca_file=str(cert))
        except OSError as exc:
            check("反例链路读得通（经 TLS）", False, f"{type(exc).__name__}: {exc}")
            diagnose("nginx_check_d")
            diagnose("nginx_check_stub")
            print()
            print(f"{len(failures)} 项失败：" + "; ".join(failures))
            return 1
        arrivals_d = [t for e, t in events_d if "chunk" in e]
        verdict_d, m_d, reason_d = judge_incremental(arrivals_d, total_d)
        show(m_d, reason_d)
        check("反例：TLS 下开着 proxy_buffering 仍被判成攒批（尺子在 TLS 上不瞎）",
              verdict_d == BUFFERED,
              f"实测判定={verdict_d}，期望 {BUFFERED}。"
              f"若为 {INCREMENTAL}：判据在 TLS 这一层失效，C 的「通过」说明不了任何事；"
              f"若为 {INCONCLUSIVE}：块数不够，或 stub 没被连上（下面有容器日志）")
        if verdict_d != BUFFERED:
            diagnose("nginx_check_d")
            diagnose("nginx_check_stub")

        print()
        if failures:
            print(f"{len(failures)} 项失败：" + "; ".join(failures))
            return 1
        print("全部通过：明文与 HTTPS 两条路径上，流式都没有退化成攒批，"
              "且判据在两个反例上确实会红。")
        return 0
    finally:
        if not (args.keep and failures):
            cleanup(["nginx_check_a", "nginx_check_b", "nginx_check_c", "nginx_check_d",
                     "nginx_check_stub"])
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
