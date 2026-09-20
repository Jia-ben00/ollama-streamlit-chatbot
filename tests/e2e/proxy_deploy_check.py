"""用 `deploy.sh --proxy` 真起一次反代（HTTPS 形态），再从宿主机验收入口。

## 为什么单独有这么一个脚本

compose 里那个 `proxy` 服务（`profiles: ["proxy"]`）和 `deploy.sh --proxy` 这条分支，
在本脚本出现之前**谁也没启动过**：

  · `container_smoke.sh` 第 5 步刻意**不加** `--proxy` —— 注释里写了理由：第 9 步的
    `nginx_check.py` 起的是**它自己的** nginx 容器，不需要 compose 里那个 proxy 服务；
  · `tests/test_deploy_script.py` 用替身 `docker` 验的是**分支逻辑**，不是真容器；
  · `tests/test_nginx_tls.py` 验的是**编排文件里的文本**。

而 `bash deploy.sh --proxy` 正是云主机上的**第一条命令**。所以这里补上，一次回答四件事：

  1. `deploy.sh --proxy` 全程真跑：证书前置检查放行 → `docker compose --profile proxy
     up -d --build` → 收尾提示真的按 HTTPS 形态打印；
  2. compose 的 proxy 服务真的起来了，而且**那颗 healthcheck 真的通过** ——
     它是「先探明文、失败再探 HTTPS」的双模式写法（见 docker-compose.yml），
     TLS 形态下这条 `||` 分支此前从没被执行过；
  3. 明文口回的 301 **跟随之后真的能到达 HTTPS**。HTTPS 模板里那行注释明说过：
     它用 `$host`（不含端口），所以「把 HTTPS 放在非 443 端口上」时 Location 会指错端口 ——
     而此前唯一的验收脚本（`nginx_check.py`）为了不占 443，恰好就是那种情形，
     所以它当时只断言了 scheme 与 path。这里保持默认 80/443，把整跳补齐；
  4. 从宿主机（也就是「外面」）用 `public_check.py --ca` 打一遍入口 —— 让 `--ca`
     这条分支第一次真跑（它此前同样从没被执行过）。

## 它是**会改 .env 的**，所以默认拒绝在别人的部署上跑

第 2 步要把 `.env` 改成 HTTPS 形态（配置源、证书目录、`API_BIND=127.0.0.1`），
这会让 `docker compose up` **重建 api 容器**。所以在正式部署上不能跑它 ——
`--owned` 是显式的「这套服务是调用方刚拉起来的，我可以动」，
`container_smoke.sh` 只在「本次运行自己起的服务」时才带这个参数。
不论成功失败，`.env` 都按**字节**还原并校验 sha256。

用法：

    python tests/e2e/proxy_deploy_check.py --owned     # 服务是刚拉起来的，允许重建
    python tests/e2e/proxy_deploy_check.py             # 服务已在跑 → 拒绝执行

退出码：0 全部通过；1 有失败项。
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 复用 nginx_check.py 里那两个**已经真跑过**的函数，不另写一份等价的 ——
# 两份「等价」的证书生成早晚会漂移，而漂移的那一刻没人会被提醒。
from nginx_check import CERT_FILE, KEY_FILE, find_openssl, make_self_signed  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ENV_FILE = REPO / ".env"
DEPLOY_SH = REPO / "deploy.sh"
PUBLIC_CHECK = Path(__file__).resolve().parent / "public_check.py"

# HTTPS 那份配置源（相对仓库根，写进 .env 由 compose 读）
TLS_TEMPLATES_DIR = "./deploy/nginx/tls"
# proxy 服务的固定健康检查名（compose 会把它算进 docker inspect 的 .State.Health）
PROXY_SERVICE = "proxy"

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def warn(name, extra=""):
    """降级但不致命：显式打出来，不计入失败。

    门槛要高 —— 只有当「这个状态在设计上就允许、且不是本次部署造成的」时才配用。
    这里唯一的使用场景是：宿主上别的进程抢占了入口端口（见下面那段）。
    """
    print(f"[WARN] {name}" + (f" | {extra}" if extra else ""))


def info(msg):
    print(f"       {msg}")


def die(msg):
    print(f"\n[FAIL] {msg}", file=sys.stderr)
    raise SystemExit(1)


def _run(cmd, timeout=900):
    return subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                          errors="replace", timeout=timeout)


# ── 找可执行文件：找不到就响亮失败，绝不静默跳过 ────────────────────

def find_docker():
    found = shutil.which("docker")
    if found:
        return found
    for cand in (r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",):
        if Path(cand).exists():
            return cand
    return None


def resolve_bash():
    """解析出**那个** bash，并守住 `C:\\Windows\\System32\\bash.exe` 这个坑。

    装了 WSL 之后 System32 里会多出一个 bash.exe（它是个转发器）；而 Windows 的
    CreateProcess 把 System32 排在 PATH **之前**，于是 `subprocess.run(["bash", ...])`
    命中的是它，`shutil.which("bash")` 拿到的却是 Git Bash —— **守卫和被测对象用了
    两个不同的 bash**。这里用绝对路径，并且显式拒绝 System32 那个。
    """
    cand = os.environ.get("SMOKE_BASH") or shutil.which("bash")
    if cand and not Path(cand).exists():
        # Git Bash 的 `$BASH` 是 POSIX 写法（`/usr/bin/bash`），Windows 上的 Python
        # 看不见它。**静默退回自动探测是不行的** —— 那正是「守卫和被测对象用了两个
        # 不同的 bash」那个坑的第二次现身。所以这里把这件事说出来，再退回探测。
        print(f"       [提示] SMOKE_BASH={cand} 在本机（{sys.platform}）看不到，"
              f"退回自动探测（调用方应传宿主路径，MSYS 下用 `cygpath -w`）")
        cand = None
    if not cand:
        for c in (r"C:\Program Files\Git\bin\bash.exe",
                  r"C:\Program Files\Git\usr\bin\bash.exe",
                  r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(c).exists():
                cand = c
                break
    if not cand:
        cand = shutil.which("bash")
    if not cand or not Path(cand).exists():
        die("找不到可用的 bash（跑 deploy.sh 需要它）。MSYS/Git Bash、Linux/macOS 都行；"
            "也可以显式传 SMOKE_BASH=<绝对路径>。")
    if re.search(r"[\\/]System32[\\/]bash\.exe$", cand, re.I):
        die(f"解析到的 bash 是 {cand} —— 那是 WSL 转发器，不是 Git Bash。"
            "用它的后果是在另一个文件系统里找 deploy.sh。请显式传 SMOKE_BASH=<Git Bash 绝对路径>。")
    return cand


# ── .env 的读写：按字节，别翻行尾 ──────────────────────────────────

def env_get(text, key, default=None):
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return default


def env_set(text, key, value):
    """替换或追加一行，**不碰其它行**（注释、顺序、行尾风格都保留）。

    行尾按原文件风格续写：`Path.write_text` 在 Windows 上会把 `\\n` 翻成 `\\r\\n`，
    同一份 `.env` 往返一趟就改了字节 —— 所以这里全程按文本原样改、最后按字节写回。
    """
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}="):
            nl = "\r\n" if ln.endswith("\r\n") else "\n"
            lines[i] = f"{key}={value}{nl}"
            return "".join(lines)
    nl = "\r\n" if "\r\n" in text else "\n"
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    lines.append(f"{key}={value}{nl}")
    return "".join(lines)


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


# ── docker 侧的小工具 ──────────────────────────────────────────────

def stack_is_running(docker):
    p = subprocess.run([docker, "compose", "ps", "-q"], cwd=str(REPO),
                       capture_output=True, text=True, errors="replace")
    return bool(p.stdout.strip())


def proxy_container_ids(docker):
    p = subprocess.run([docker, "ps", "--filter",
                        f"label=com.docker.compose.service={PROXY_SERVICE}",
                        "--format", "{{.ID}}"],
                       capture_output=True, text=True, errors="replace")
    return p.stdout.split()


def inspect(docker, cid, fmt):
    p = subprocess.run([docker, "inspect", "-f", fmt, cid],
                       capture_output=True, text=True, errors="replace")
    return p.stdout.strip() if p.returncode == 0 else ""


def wait_proxy_healthy(docker, seconds=120):
    """等 proxy 容器的 healthcheck 变成 healthy。返回 (状态, 容器 id)。

    ⚠️ 这里必须等到 **healthy**，不能只等 `docker ps` 里出现它：
    「容器在跑」与「nginx 真的能应答」是两件事，而那颗探针本身就是被测对象 ——
    nginx 配置错时容器会一直在 starting / unhealthy，恰恰是我们要看见的结果。
    """
    for _ in range(int(seconds)):
        cids = proxy_container_ids(docker)
        if cids:
            st = inspect(docker, cids[0], "{{.State.Health.Status}}")
            if st == "healthy":
                return st, cids[0]
            if st in ("unhealthy", "none"):
                return st, cids[0]
        time.sleep(1)
    return "timeout", ""


def dump_proxy_diagnostics(docker):
    for cid in proxy_container_ids(docker):
        info(f"--- proxy 容器 {cid[:12]} 状态与日志 ---")
        info(inspect(docker, cid, "{{.State.Status}} health={{.State.Health.Status}}"))
        p = subprocess.run([docker, "logs", "--tail", "40", cid],
                           capture_output=True, text=True, errors="replace")
        for line in (p.stdout + p.stderr).splitlines()[-25:]:
            info(f"    {line}")


def main():
    ap = argparse.ArgumentParser(description="真跑 deploy.sh --proxy（HTTPS）并验收入口")
    ap.add_argument("--owned", action="store_true",
                    help="这套服务是调用方刚拉起来的，允许我改 .env 并重建 api 容器。"
                         "不带它时：若检测到服务已在跑就直接拒绝（在正式部署上跑会把它改坏）")
    ap.add_argument("--timeout", type=float, default=900.0, help="deploy.sh 的等待上限（秒）")
    args = ap.parse_args()

    docker = find_docker()
    if not docker:
        die("找不到 docker。这个脚本要在有 Docker 的机器上跑。")
    bash = resolve_bash()
    openssl = find_openssl()
    if not openssl:
        # 与 nginx_check.py 同一套理由：镜像里没有 openssl，只能借宿主的；
        # 找不到就响亮失败 —— 静默跳过等于这一层又没验。
        die("找不到 openssl（签自签证书要用它）。镜像里没有，得用宿主机的："
            "Linux/macOS 自带；Windows 装了 Git for Windows 就有。")

    if not ENV_FILE.exists():
        die("没有 .env。先 `cp .env.prod.example .env` 并填好 MYSQL_ROOT_PASSWORD。")

    if stack_is_running(docker) and not args.owned:
        die("检测到这套服务已经在跑。本脚本会改 .env 并**重建 api 容器**，"
            "在正式部署上跑会把它改坏。\n"
            "      要在自己拉起来的服务上跑：加 --owned\n"
            "      要在云主机上做验收：只跑 container_smoke.sh（第 10 步会自动跳过）")

    orig_env = ENV_FILE.read_bytes()
    orig_sha = sha256_bytes(orig_env)
    orig_text = orig_env.decode("utf-8")
    certdir = None
    restored = False

    try:
        # ── 1. 自签一张证书（SAN 必须含 IP:127.0.0.1，理由见 nginx_check.make_self_signed）──
        print("── 1. 准备自签证书与 HTTPS 形态的 .env ──")
        certdir = tempfile.mkdtemp(prefix="proxy_deploy_certs_")
        cert, key = make_self_signed(openssl, certdir)
        info(f"自签证书：{cert} / {key}")
        cert_sha = sha256_bytes(cert.read_bytes())[:12]

        # ── 2. 改 .env：配置源 / 证书目录 / API_BIND ──
        # API_BIND 必须回到 127.0.0.1：`deploy.sh --proxy` 会检查这一条并**拒绝**在
        # 0.0.0.0 上拉起反代（反代就是入口，api 不该再对外暴露一份）。
        text = env_set(orig_text, "NGINX_TEMPLATES_DIR", TLS_TEMPLATES_DIR)
        text = env_set(text, "TLS_CERT_DIR", certdir)
        text = env_set(text, "API_BIND", "127.0.0.1")
        ENV_FILE.write_bytes(text.encode("utf-8"))

        http_port = env_get(text, "PROXY_HTTP_PORT", "80") or "80"
        https_port = env_get(text, "PROXY_HTTPS_PORT", "443") or "443"
        info(f"。env：NGINX_TEMPLATES_DIR={TLS_TEMPLATES_DIR}  TLS_CERT_DIR={certdir}")
        info(f"。env：API_BIND=127.0.0.1  入口端口 {http_port}(301) / {https_port}(TLS)")
        info(f"证书 sha256={cert_sha}…（用于确认挂进容器的是这一张）")

        # ── 3. 真跑云主机上的第一条命令 ──
        print("\n── 2. 真跑 deploy.sh --no-pull --proxy（HTTPS 形态）──")
        p = _run([bash, "deploy.sh", "--no-pull", "--proxy"], timeout=args.timeout)
        out = p.stdout + p.stderr
        for line in out.splitlines():
            info(f"    {line}")
        check("deploy.sh --proxy 退出码 0", p.returncode == 0,
              f"rc={p.returncode}" if p.returncode else "")
        if p.returncode != 0:
            print("\ndeploy.sh 自己失败了，后面的检查不再有意义。", file=sys.stderr)
            return 1

        # 收尾提示必须按 HTTPS 形态打印 —— 否则说明 TLS_MODE 没被识别出来，
        # 那么「跑了 --proxy」这件事就跟没跑一样（反代起来了，但可能还是明文）。
        check("deploy.sh 识别出 HTTPS 形态（收尾提示为「反代已启用（HTTPS）」）",
              "反代已启用（HTTPS）" in out)
        check("deploy.sh 打印的验收命令用了 https 与域名/证书提示",
              "public_check.py --url https://" in out)

        # ── 4. compose 的 proxy 服务真的起来了，且 healthcheck 通过 ──
        print("\n── 3. proxy 服务与它的双模式 healthcheck ──")
        status, cid = wait_proxy_healthy(docker)
        check("compose 的 proxy 服务起来了（profiles: [\"proxy\"] 真的生效）", bool(cid),
              f"container={cid[:12]}" if cid else "docker ps 里找不到带 service=proxy 标签的容器")
        if not cid:
            dump_proxy_diagnostics(docker)
            return 1
        check("proxy 的 healthcheck 通过（TLS 形态下那条 `||` 分支真的被执行了）",
              status == "healthy", f"health={status}")
        if status != "healthy":
            dump_proxy_diagnostics(docker)
            return 1

        # ── 5. 明文口只跳转，而且**跟过去真的到得了 HTTPS** ──
        print("\n── 4. 入口：明文 301 → HTTPS ──")
        # 先在两个候选地址上各探一次，挑一个**真的打到我们 nginx** 的继续。
        #
        # 为什么必须这么做：宿主机上这个端口可能被**别的进程**占着。Windows 允许两个
        # 进程同时绑 `0.0.0.0:80`（本机实测：Steam++/Watt Toolkit 就在 80/443 上，
        # 它回一个**没有 Server 头的 404**），于是「端口能连上」根本不等于「连到的是
        # 我们的服务」—— 这正是本项目反复出现的那一类假信号。容器里一切正常，
        # 宿主侧却被别人抢答；两者都「有响应」，性质完全不同。
        # 判据取 `Server: nginx/...`；127.0.0.1（IPv4）不行就换 localhost（IPv6 回环，
        # 通常只有 Docker 在听）。
        entry_addr = None
        conflicts = []
        for cand in (f"127.0.0.1:{http_port}", f"localhost:{http_port}"):
            try:
                r = requests.get(f"http://{cand}/health", timeout=5, allow_redirects=False)
            except requests.RequestException as exc:
                conflicts.append(f"{cand} 连不上（{type(exc).__name__}）")
                continue
            server = r.headers.get("Server", "")
            if "nginx" in server.lower():
                entry_addr = cand
                info(f"入口地址取 {cand}（Server: {server}）")
                break
            conflicts.append(f"{cand} 被别的进程抢答：HTTP {r.status_code}，"
                             f"Server={server or '(无)'}")
        if entry_addr is None:
            check("宿主上有一个地址能打到我们的 nginx", False,
                  "; ".join(conflicts) or "两个地址都没有应答")
            return 1
        if conflicts:
            warn("宿主上另有进程占着入口端口（不是本次部署的问题，但会让你从本机自测时看错东西）",
                 "; ".join(conflicts))
            info("     查法：netstat -ano | findstr :80（Windows）/ ss -ltnp | grep ':80'")
            info(f"     本次改用 {entry_addr} 继续 —— 同一个宿主、同一个容器，"
                 f"验收结论不受影响")

        r = requests.get(f"http://{entry_addr}/health", timeout=10, allow_redirects=False)
        check(f"明文 :{http_port} 不直接服务，只回 3xx", r.status_code in (301, 302, 307, 308),
              f"HTTP {r.status_code}")
        loc = r.headers.get("Location", "")
        check("跳转目标是 https 且保留了原路径",
              loc.startswith("https://") and loc.endswith("/health"), loc)
        # 这里只断言 scheme 与 path、不照抄端口：模板用的是 `$host`（不含端口），
        # 所以「HTTPS 不在 443 上」时 Location 必然指到 443 —— 那是模板里写明过的前提，
        # 不是缺陷。默认布局（80→443）下下面这条整跳断言才有意义。
        if https_port == "443":
            try:
                r2 = requests.get(f"http://{entry_addr}/health", timeout=10,
                                  allow_redirects=True, verify=str(cert))
                check("跟随 301 之后真的到达 HTTPS 并拿到 200（用自签证书校验）",
                      r2.status_code == 200, f"HTTP {r2.status_code}（落在 {r2.url}）")
            except requests.RequestException as exc:
                check("跟随 301 之后真的到达 HTTPS 并拿到 200（用自签证书校验）", False,
                      f"{type(exc).__name__}: {exc}")
        else:
            info(f"HTTPS 端口是 {https_port}（非 443），跳过「跟随 301」这条 —— "
                 f"模板用 $host 拼 Location，非 443 时它必然指到 443，这是模板里写明的前提")

        # ── 6. 从宿主机（=「外面」）用 public_check.py --ca 打一遍 ──
        print("\n── 5. 从宿主机验收入口：public_check.py --ca ──")
        entry_host = entry_addr.rsplit(":", 1)[0]
        # --no-ports：本机 3306 上往往真有个 MySQL 在跑，那道「内部端口没暴露」的断言
        # 会被误杀；端口暴露那件事由 container_smoke.sh 第 6/8 步在容器视角断言。
        pc = _run([sys.executable, str(PUBLIC_CHECK),
                   "--url", f"https://{entry_host}:{https_port}",
                   "--ca", str(cert), "--no-ports"], timeout=300)
        pc_out = pc.stdout + pc.stderr
        for line in pc_out.splitlines():
            info(f"    {line}")
        check("public_check.py --ca 退出码 0（入口验收全过）", pc.returncode == 0,
              f"rc={pc.returncode}")
        # 这一条同时证明 `--ca` 这条分支真被执行过：它会把信任根换成那张自签证书，
        # 而**证书校验仍然开着**（--insecure 才会关）。输出里那行正是在报这件事。
        check("public_check.py 报了「证书校验：开（信任根换成 ...）」",
              "信任根换成" in pc_out)

    finally:
        # ── 7. 无条件按字节还原 .env，并校验 sha256 ──
        # 还原放在 finally：中途任何一步失败（包括 SystemExit）都不能把 .env 留在
        # HTTPS 形态上 —— 那会让下一次 `docker compose up` 悄悄用错配置。
        try:
            ENV_FILE.write_bytes(orig_env)
            back = sha256_bytes(ENV_FILE.read_bytes())
            restored = back == orig_sha
        except OSError as exc:
            print(f"\n[FAIL] 还原 .env 失败：{exc}", file=sys.stderr)
            restored = False
        if certdir:
            shutil.rmtree(certdir, ignore_errors=True)

    print()
    check("`.env` 已按字节还原（sha256 一致）", restored,
          "不一致 ⇒ 下一次部署会用到错的配置，先手动 git diff .env 检查")

    if failures:
        print(f"{len(failures)} 项失败：" + "; ".join(failures))
        print("\n下一步：加 KEEP=1 重跑 container_smoke.sh 保留现场，"
              "或直接 python tests/e2e/proxy_deploy_check.py --owned 单独跑这一段。")
        return 1
    print("全部通过：`deploy.sh --proxy` 这条路径（含 TLS）真的能起、能跳转、能被外面验收。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
