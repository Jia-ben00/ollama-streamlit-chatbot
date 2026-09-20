"""deploy.sh 的行为守卫 —— 用替身 docker 真跑，不是文本比对。

为什么需要它
------------
`deploy.sh` 是文档里「上机第一步」的脚本（docs/DEPLOY.md §7），但在本文件出现之前，
**没有任何东西执行过它**：CI 直接跑 `.github/scripts/container_smoke.sh`，
`tests/test_deploy_manifest.py` 只把它当文本读（查行尾、查字符串）。
本仓库已经栽过两次同类洞（`tests/e2e/nginx_check.py`、容器冒烟的端口等待）——
**「读起来没问题」和「跑起来没问题」是两件事**。

第一次真跑就抓到一个（静态校验永远看不见的）：`.env` 里缺 `MYSQL_ROOT_PASSWORD=`
那一行时，脚本会**连一行输出都没有**地退出 —— 见
`test_env_without_password_line_fails_loudly`。

做法
----
造一个替身 `docker` 放在 PATH 最前面，只回答 deploy.sh 真正会问的几个问题
（--version / info / compose version / compose up / compose logs / compose exec），
于是能在**没有任何 Docker** 的机器上、用秒级时间、确定性地走完它的每条分支。
替身还把每次调用写进日志，所以断言可以问「脚本到底做了什么」，
而不只是看它打印了什么 —— 比如「`--proxy` 时有没有真的带 `--profile proxy`」，
光看输出看不出来。

边界（重要）
------------
本文件只钉「脚本自己的判断逻辑」。**容器真的起不起得来**是另一层，
由 container_smoke.sh 第 5 步在 CI 的真实 Docker 上跑同一条命令来验。
两层不可互相替代：这里覆盖「环境凑不出来」的分支（没装 docker、就绪失败、参数打错），
那边覆盖「环境是对的」时的真实行为。

两个实测记录（都影响本文件怎么写，别当注释看）
--------------------------------------------
① **从 Windows Python 直接给子进程换 PATH，会把 MSYS bash 搞挂。**
   本机（Git for Windows 的 msys + 沙箱）实测：PATH 里前置一个目录 →
   `bash` 起不来 / 卡死（`child_copy: cygheap read copy failed, Win32 error 299`，
   实测 2/2 挂住、单次 74 秒）；PATH 完全不动 → 10/10 稳定。
   而**在壳内改 PATH**（`bash -c 'export PATH=...; exec bash deploy.sh'`）
   → 6/6 稳定。所以这里统一用后者：给子进程的 `PATH` 保持原样，
   只有 `STUB_PATH_PREFIX` 这一个变量进去。
   这不是 deploy.sh 的问题，是「在这台机器上怎么起 bash」的问题 ——
   但踩过一次就记下来，免得下次又在同一个地方卡几分钟。

   还有一个更细的坑，是同一次排查里量出来的：**会话里的第一个命令替换如果是 bash 脚本，会挂。**
   deploy.sh 第二行就是 `cd "$(dirname "$0")"`，所以 dirname 正好是那个「第一个」。
   实测：那个位置放 `#!/usr/bin/env bash` 的脚本 → 两次都挂（82 秒、输出为空）；
   换成**真 dirname 二进制的副本** → 3 秒正常退出。
   其余替身（docker / curl / git）也是 bash 脚本，但它们是被 deploy.sh 在**后面**调用的，
   一直都很稳 —— 问题只出在第一次。所以下面 core 目录里放的是二进制副本。

② 本机用 `grep -E '^X=' file | cut ...` 读 **CRLF** 的 `.env`，取到的值**不带 CR**
   （msys 的 grep 会吃掉行尾 CR），而 GNU grep 不会。所以「本机量到 CRLF 没问题」
   推不出「Linux 上也没问题」。这里刻意不测 CR 相关行为：deploy.sh 只用取到的值做校验、
   **从不把它注入容器**（注入是 compose 自己读 `.env` 完成的），没有证据说它是缺陷；
   而基于一个只在 msys 成立的观测去加检查，只会变成一个拦正确部署的假警报。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPLOY_SH = REPO / "deploy.sh"
BASH = shutil.which("bash")

# 单次 deploy.sh 的上限。正常路径 ~2 秒；这条只是兜底：
# 万一又出现「bash 起不来」，宁可响亮地失败，也不要挂住整个测试套件
# （没有这个看门狗时，本地实测被拖了 4 分半）。
RUN_TIMEOUT = 60

# ── 替身 ────────────────────────────────────────────────────────────────
# 每个替身都把调用追加到 $STUB_LOG，断言因此能检查「做了什么」。
STUB_DOCKER = r"""#!/usr/bin/env bash
# 替身 docker：只回答 deploy.sh 真正会问的问题。
printf 'docker %s\n' "$*" >> "$STUB_LOG"
case "${1:-}" in
  --version) echo "Docker version 27.9.9, build stub"; exit 0 ;;
  info)      exit "${STUB_INFO_RC:-0}" ;;
esac
if [[ "${1:-}" == "compose" ]]; then
  shift
  case "${1:-}" in
    version) echo "${STUB_COMPOSE_VERSION:-2.29.0}"; exit "${STUB_COMPOSE_RC:-0}" ;;
    up)      echo "stub: compose up"; exit 0 ;;
    logs)    echo "stub: 最近日志（替身输出，不是真日志）"; exit 0 ;;
    ps)      echo "stub: ps"; exit 0 ;;
    exec)
      # deploy.sh 靠 exec 进容器发 HTTP 请求判断就绪。按被探的路径回话：
      #   /health/ready → STUB_READY_CODE（默认 200；设成别的值模拟「永远不就绪」）
      #   /health       → 一段 JSON
      if [[ "$*" == *"health/ready"* ]]; then
        printf '%s\n' "${STUB_READY_CODE:-200}"
      else
        printf '%s\n' '{"status":"ok","mysql":true,"redis":true,"ollama":false}'
      fi
      exit 0 ;;
  esac
  exit 0
fi
exit 0
"""

# curl 一律失败：脚本末尾要查公网 IP，真发请求会等超时（还依赖网络）。
# 失败路径本来就是「打印 <服务器公网IP> 占位」，正好把它变成确定性的。
STUB_CURL = "#!/usr/bin/env bash\nexit 1\n"

STUB_GIT = r"""#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "$STUB_LOG"
exit 0
"""

# 刻意**不**做 dirname 替身脚本：它是本次会话里 bash fork 出去执行的第一个外部命令，
# 实测那个位置放脚本会卡死整个 bash（见文件顶部 ① 的第二段）。需要时用真二进制的副本。
GOOD_ENV = "MYSQL_ROOT_PASSWORD=abc123def456\nAPI_PORT=8000\n"

# 仓库里那两份反代模板的**替身**：只要能被 deploy.sh 的「这一层是明文还是 HTTPS」
# 判据（在模板里找 `ssl_certificate`）区分开就够了，不需要真配置。
PLAIN_TEMPLATE = (b"server {\n"
                  b"    listen ${LISTEN_PORT};\n"
                  b"    location / { proxy_pass http://${API_UPSTREAM}; }\n"
                  b"}\n")
TLS_TEMPLATE = (b"server {\n"
                b"    listen ${LISTEN_PORT};\n"
                b"    return 301 https://$host$request_uri;\n"
                b"}\n"
                b"server {\n"
                b"    listen ${LISTEN_TLS_PORT} ssl;\n"
                b"    ssl_certificate     /etc/nginx/certs/fullchain.pem;\n"
                b"    ssl_certificate_key /etc/nginx/certs/privkey.pem;\n"
                b"}\n")
# HTTPS 模式的 .env：三个开关（配置源、证书目录、443 端口）都到位。
TLS_ENV = ("MYSQL_ROOT_PASSWORD=abc123def456\nAPI_BIND=127.0.0.1\nAPI_PORT=8000\n"
           "PROXY_HTTP_PORT=80\nPROXY_HTTPS_PORT=443\n"
           "NGINX_TEMPLATES_DIR=./deploy/nginx/tls\nTLS_CERT_DIR=./certs\n")


def msys_path(p):
    """Windows 路径 → MSYS 能认的 POSIX 形态；Linux 上原样返回。

    少这一步会得到**最难查的一种失败**：脚本说「没装 Docker」，
    而替身 docker 明明就在那儿。原因是 PATH 用 `:` 分隔，而 `C:/foo` 里的
    盘符冒号会被当成分隔符 —— 于是 `C:/.../stub` 被切成了 `C` 和 `/.../stub`
    两个不存在的条目，替身目录根本没上 PATH。实测确认过（`C:/...` 找不到、
    `/c/...` 能找到）。这是本项目的老熟人了：**"拿不到结果"被当成"结果就是没有"**。
    """
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":  # C:/... → /c/...
        return "/" + s[0].lower() + s[2:]
    return s


@unittest.skipUnless(BASH, "找不到 bash（Windows 上装了 Git Bash 才有）")
class DeployScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deploy_script_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copy(DEPLOY_SH, self.tmp / "deploy.sh")

        # ① 替身目录（覆盖 docker / curl / git）
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        self._exe(self.stub, "docker", STUB_DOCKER)
        self._exe(self.stub, "curl", STUB_CURL)
        self._exe(self.stub, "git", STUB_GIT)

        self.calls_log = self.tmp / "calls.log"
        self.out_file = self.tmp / "run.out"

        # 让临时目录与仓库**同形**：deploy.sh 检查「配置源目录在不在」
        # （挂到不存在的目录上时，容器会去服务镜像自带的默认站点，也是 200，极难排查），
        # 所以这里把明文那份模板补上。HTTPS 那份由各用例按需造（见 _templates_tree）。
        plain = self.tmp / "deploy" / "nginx" / "templates"
        plain.mkdir(parents=True)
        (plain / "default.conf.template").write_bytes(PLAIN_TEMPLATE)

    # ── 基础设施 ────────────────────────────────────────────────────────
    def _exe(self, directory, name, text):
        p = directory / name
        p.write_bytes(text.encode("utf-8"))
        p.chmod(0o755)

    def write_env(self, text):
        """写 .env。一律按字节写 —— 避免 Windows 上被偷偷翻成 CRLF。"""
        (self.tmp / ".env").write_bytes(text.encode("utf-8"))

    def make_git_repo(self):
        """.git 目录存在时 deploy.sh 才会去 git pull。"""
        (self.tmp / ".git").mkdir()

    def _core_without_docker(self, e):
        """构造「这台机器上没装 docker」：摘掉所有含 docker 的 PATH 目录，补一个 dirname。

        CI（ubuntu runner）自带 docker，所以只有把装着 docker 的目录整个从 PATH 里摘掉，
        `command -v docker` 才会失败。而 `dirname` 恰好也在那个目录里（/usr/bin），
        所以必须自己补一个 —— 用真二进制的**副本**，理由见文件顶部 ① 的第二段。
        本机（Windows）没有 docker，于是什么都不会被摘掉，PATH 保持原样（实测最稳的写法）。
        """
        kept, dropped = [], []
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if d and os.path.exists(os.path.join(d, "docker")):
                dropped.append(d)
                continue
            kept.append(d)
        if not dropped:
            return None  # 这台机器本来就没 docker，PATH 不用动
        e["PATH"] = os.pathsep.join(kept)
        real = shutil.which("dirname")
        if not real:
            self.skipTest("摘掉 PATH 后连 dirname 都没了（构建不出这个场景）")
        core = self.tmp / "core"
        core.mkdir(exist_ok=True)
        shutil.copy(real, core / "dirname")
        (core / "dirname").chmod(0o755)
        return core

    def run_deploy(self, *args, ready="200", env=None, no_docker=False):
        e = dict(os.environ)
        # ⚠️ 刻意**不动 PATH**：从 Windows 侧换 PATH 会让 msys bash 起不来（见模块 docstring ①）。
        # 替身目录改成在壳内前置，只通过这一个变量传进去。
        prefix = self.stub
        if no_docker:
            prefix = self._core_without_docker(e)
        e["STUB_PATH_PREFIX"] = (msys_path(prefix) + ":") if prefix else ""
        e["STUB_LOG"] = str(self.calls_log)
        e["STUB_READY_CODE"] = ready
        if env:
            e.update({k: str(v) for k, v in env.items()})

        # 输出写到文件而不是管道：万一子进程卡住，管道会连带把父进程一起挂住
        # （Python 在 timeout 后 drain 管道是没有上限的）。
        #
        # 内层 bash 用**绝对路径**：Linux 上为了藏掉 docker 会把 /usr/bin 从 PATH 里摘掉，
        # 而 bash 自己也在那里 —— 那时 `exec bash` 会变成 command not found，
        # 报错会伪装成"脚本坏了"。绝对路径绕开这一层。
        # `bash -c 'script' ARG0 ARG1...`：$0 是下面的绝对路径，$@ 是剩余参数。
        argv = [BASH, "-c",
                '[ -n "${STUB_PATH_PREFIX:-}" ] && export PATH="${STUB_PATH_PREFIX}$PATH"; '
                'exec "$0" "$@"',
                msys_path(BASH), "deploy.sh", *args]
        started = time.monotonic()
        with open(self.out_file, "wb") as fh:
            proc = subprocess.Popen(argv, cwd=str(self.tmp), env=e, stdout=fh,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            try:
                rc = proc.wait(timeout=RUN_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc)
                rc = None
        elapsed = time.monotonic() - started

        proc.returncode = rc
        proc.elapsed = elapsed
        proc.output = self.out_file.read_text(encoding="utf-8", errors="replace")
        proc.calls = (self.calls_log.read_text(encoding="utf-8", errors="replace")
                      if self.calls_log.exists() else "")
        if rc is None:
            self.fail("deploy.sh 在 %d 秒内没有结束（挂住了）。输出：\n%s"
                      % (RUN_TIMEOUT, proc.output[:800]))
        return proc

    @staticmethod
    def _kill_tree(proc):
        """连子进程一起收掉，否则残留的 bash 会一直占着输出文件。"""
        if sys.platform == "win32":
            subprocess.run([r"C:\Windows\System32\taskkill.exe", "/F", "/T", "/PID",
                            str(proc.pid)], capture_output=True)
        else:
            proc.kill()
            proc.wait(timeout=10)

    # ── 前置检查：环境凑不出来时的每一条分支 ────────────────────────────
    def test_missing_docker_points_at_the_install_command(self):
        self.write_env(GOOD_ENV)
        p = self.run_deploy(no_docker=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("没装 Docker", p.output)
        self.assertIn("get.docker.com", p.output, "只报「没装 Docker」不够，要给能照抄的命令")

    def test_missing_compose_plugin_is_reported(self):
        """`docker-compose`（带横杠）不是 `docker compose`，很多机器只有前者。"""
        self.write_env(GOOD_ENV)
        p = self.run_deploy(env={"STUB_COMPOSE_RC": 1})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("docker compose 插件不可用", p.output)

    def test_unavailable_daemon_explains_the_docker_group(self):
        """守护进程没跑 / 用户不在 docker 组 —— 两者报错长得一样，得都提示。"""
        self.write_env(GOOD_ENV)
        p = self.run_deploy(env={"STUB_INFO_RC": 1})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("usermod -aG docker", p.output)

    def test_missing_env_tells_you_to_copy_the_template(self):
        p = self.run_deploy()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("cp .env.prod.example .env", p.output)
        self.assertNotIn("up -d", p.calls, "缺 .env 时不该已经去起服务了")

    # ── 密码：这一段是本文件存在的主要理由 ──────────────────────────────
    def test_env_without_password_line_fails_loudly(self):
        """**第一次真跑就抓到的洞**（静态校验永远看不见）。

        `.env` 存在、但没有 `MYSQL_ROOT_PASSWORD=` 这一行时：
        `PW="$(grep ... | cut ...)"` 里的 grep 返回 1，而脚本开头是
        `set -euo pipefail` —— pipefail 让整条管道失败，赋值语句直接触发 `set -e`
        **退出**。现象是连一句 [FAIL] 都没有、退出码 1，输出在"检查前置条件"之后就断了，
        比它自己想避免的"丢一句 deploy failed"更难查。
        （同一段里 API_PORT 那处写了 `|| true`，所以只有密码这处会静默退出。）
        """
        self.write_env("API_PORT=8000\n")  # 有 .env，但没有密码行
        p = self.run_deploy()
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(p.output.strip(), "失败时不能一行输出都没有")
        self.assertIn("MYSQL_ROOT_PASSWORD", p.output,
                      "必须点名是哪个变量缺了，否则用户只能猜是哪一步断的")

    def test_export_style_line_is_also_caught(self):
        """`export MYSQL_ROOT_PASSWORD=...` 是很多人写 .env 的习惯，这里读不到。

        脚本只认 `^MYSQL_ROOT_PASSWORD=`，所以它读不到 —— 关键是**要报错**，
        而不是静默把它当成"没配"（或者更糟：静默退出）。
        """
        self.write_env("export MYSQL_ROOT_PASSWORD=abc123def456\n")
        p = self.run_deploy()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("MYSQL_ROOT_PASSWORD", p.output)

    def test_empty_password_is_rejected(self):
        self.write_env("MYSQL_ROOT_PASSWORD=\n")
        p = self.run_deploy()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("MYSQL_ROOT_PASSWORD", p.output)
        self.assertIn("openssl rand", p.output, "要给出生成强密码的命令")

    def test_placeholder_passwords_are_rejected(self):
        for bad in ("change-me", "123456"):
            with self.subTest(password=bad):
                self.calls_log.unlink(missing_ok=True)
                self.write_env("MYSQL_ROOT_PASSWORD=%s\n" % bad)
                p = self.run_deploy()
                self.assertNotEqual(p.returncode, 0, "%s 是模板默认值，公网部署必须拦下" % bad)
                self.assertNotIn("up -d", p.calls)

    def test_password_with_at_is_rejected_with_the_reason(self):
        """`@` 会破坏 DATABASE_URL 解析（host 变成 xxx@mysql），报错必须说清原因。"""
        self.write_env("MYSQL_ROOT_PASSWORD=ab@cd\n")
        p = self.run_deploy()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("DATABASE_URL", p.output)
        self.assertNotIn("up -d", p.calls)

    # ── 参数 ────────────────────────────────────────────────────────────
    def test_unknown_flag_is_rejected(self):
        """参数打错一个字母就静默按默认部署，等于部署了个别的东西还说成功。"""
        self.write_env(GOOD_ENV)
        p = self.run_deploy("--prox")  # 少一个字母
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--prox", p.output)
        self.assertNotIn("up -d", p.calls)

    # ── 正常路径 ────────────────────────────────────────────────────────
    def test_happy_path_starts_services_and_prints_next_steps(self):
        self.write_env(GOOD_ENV)
        p = self.run_deploy()
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("docker compose up -d --build", p.calls)
        self.assertIn("部署完成", p.output)
        # 只打印"成功"不够：还得给出照抄得动的下一步，且端口要对。
        self.assertIn("http://127.0.0.1:8000/health", p.output)
        self.assertIn("/health/ready", p.output)
        # 构建前必须提醒 pip 源：国内网络下官方源那一层实测 3.7 小时，换个镜像约 2 分钟。
        # （顺序没法从输出里断言 —— up 的命令只出现在 calls 里，不在脚本自己的输出里；
        #  这条只钉「提醒在里面」，位置由 deploy.sh 里那段注释说明。）
        self.assertIn("PIP_INDEX_URL", p.output,
                      "没提醒构建会走哪个 pip 源，国内主机就会被那一层闷住几小时")
        self.assertIn("public_check.py --url", p.output,
                      "本机 curl 通 ≠ 公网可访问；收尾必须把人引到公网验收脚本")

    def test_api_port_from_env_is_honoured(self):
        self.write_env("MYSQL_ROOT_PASSWORD=abc123def456\nAPI_PORT=9000\n")
        p = self.run_deploy()
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("9000", p.output)

    def test_never_deletes_the_data_volume(self):
        """部署脚本**永远**不能出现 down/down -v —— 那会删掉 mysql 数据卷。

        这是一条数据安全不变量：将来有人为了"清干净再起"往脚本里加一句 down -v，
        本仓库的容器冒烟**不会**报错（它自己有 PRE_EXISTING 保护），只有这里拦得住。
        """
        self.write_env(GOOD_ENV)
        p = self.run_deploy()
        self.assertNotIn("compose down", p.calls, "部署脚本不许停服务，更不许带 -v 删卷")

    def test_never_ready_fails_with_diagnostics(self):
        """一直不就绪时：要打印日志、要指向排查文档，且**不能**装作成功。"""
        self.write_env(GOOD_ENV)
        p = self.run_deploy(ready="0", env={"READY_TIMEOUT": 2, "READY_INTERVAL": 1})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("docker compose logs", p.calls, "失败时必须留下容器日志")
        self.assertIn("DEPLOY.md", p.output, "要指向排查文档，别让人对着空气猜")

    def test_ready_poll_honours_the_timeout_knob(self):
        """打印的「最多 N 秒」必须**就是**实际上界。

        以前这里是写死的 90 次 × sleep 2s，却对外宣称"最多 180 秒"；
        而每次探测本身（exec 起 python + 3 秒超时）还要另算，真实上界能到分钟级。
        现在次数由 READY_TIMEOUT / READY_INTERVAL 算出来 —— 两个断言一起钉住它：
        打印的值要对，**实际耗时也要真的跟着变**（否则只是把字符串改成了变量名）。
        """
        self.write_env(GOOD_ENV)
        p = self.run_deploy(ready="0", env={"READY_TIMEOUT": 4, "READY_INTERVAL": 1})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("最多 4 秒", p.output)
        self.assertLess(p.elapsed, 60, "READY_TIMEOUT=4 却跑了很久，说明轮询没用这个开关")

    # ── 反代（--proxy）──────────────────────────────────────────────────
    def test_proxy_without_loopback_bind_is_refused_before_starting_anything(self):
        """启用反代却不把 API 绑到回环 = 8000 仍对整个公网开着，反代白配。

        关键是**在 up 之前**就拒绝：否则会留下一个"半配置"的部署，
        看起来是成功的，实际有个没被保护的后门。
        """
        self.write_env(GOOD_ENV)  # 没有 API_BIND → compose 默认 0.0.0.0
        p = self.run_deploy("--proxy")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("API_BIND", p.output)
        self.assertIn("127.0.0.1", p.output)
        # 前置检查本身会调 compose（version / info），所以只能断言"没起来服务"。
        self.assertNotIn("up -d", p.calls, "拒绝了就不该再起服务")

    def test_proxy_starts_the_profile_and_uses_the_proxy_port(self):
        self.write_env("MYSQL_ROOT_PASSWORD=abc123def456\nAPI_BIND=127.0.0.1\n"
                       "API_PORT=8000\nPROXY_HTTP_PORT=8080\n")
        p = self.run_deploy("--proxy")
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("--profile proxy", p.calls,
                      "少了 --profile proxy，反代根本不会参与，脚本却会说自己成功了")
        self.assertIn("up -d --build", p.calls)
        # 入口提示必须换成反代端口，否则用户会去安全组开错端口。
        self.assertIn("8080", p.output)
        self.assertIn("反代已启用", p.output)

    def test_proxy_explains_that_the_api_port_is_not_public(self):
        self.write_env("MYSQL_ROOT_PASSWORD=abc123def456\nAPI_BIND=127.0.0.1\n"
                       "PROXY_HTTP_PORT=80\n")
        p = self.run_deploy("--proxy")
        self.assertEqual(p.returncode, 0, p.output)
        # 别让人再手滑把 8000 放行到公网。
        self.assertIn("不要", p.output)
        self.assertIn("8000", p.output)

    # ── HTTPS（TLS 模式的识别 + 证书前置检查）────────────────────────────
    def _templates_tree(self, *, tls=True, certs=("fullchain.pem", "privkey.pem")):
        """造出 deploy.sh 会去看的那几样东西（都用**相对路径**）。

        deploy.sh 第二行 cd 到自己所在的目录（测试里就是 tmp），所以它眼里的 cwd
        与仓库根目录同形 —— 相对路径这样才走得通，也和真实部署一致。
        """
        if tls:
            d = self.tmp / "deploy" / "nginx" / "tls"
            d.mkdir(parents=True, exist_ok=True)
            (d / "default.conf.template").write_bytes(TLS_TEMPLATE)
        if certs:
            c = self.tmp / "certs"
            c.mkdir(exist_ok=True)
            for name in certs:
                (c / name).write_bytes(b"-----BEGIN STUB-----\nstub\n-----END STUB-----\n")

    def test_tls_mode_is_detected_from_the_config_not_the_dir_name(self):
        """HTTPS 模式的判据是「待渲染的模板里有没有 ssl_certificate」。

        用配置内容而不是目录名：目录名随便起，而「我到底在对外提供明文还是 TLS」
        只有配置能回答。识别对了，收尾提示才会给出 https:// 与 443。
        """
        self._templates_tree()
        self.write_env(TLS_ENV)
        p = self.run_deploy("--proxy")
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("HTTPS 反代", p.output)
        self.assertIn("https://", p.output)
        self.assertIn("443", p.output)
        # 80 也不能被忘掉：跳转和 ACME 挑战都还在那儿，证书续期靠它。
        self.assertIn("80", p.output)
        self.assertIn("--profile proxy", p.calls)

    def test_tls_without_the_private_key_is_refused_before_starting(self):
        """证书缺一个就拒绝启动，而且是**在 up 之前**。

        少了 privkey.pem，nginx 会在容器里当场退出；用户看到的却是「反代起不来 + 一串
        看不懂的 nginx 日志」，还得先等构建跑完。拦在前面，并顺手给出两种解法。
        """
        self._templates_tree(certs=("fullchain.pem",))
        self.write_env(TLS_ENV)
        p = self.run_deploy("--proxy")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("privkey.pem", p.output)
        self.assertIn("TLS_CERT_DIR", p.output)
        self.assertNotIn("up -d", p.calls, "证书不齐就不该再起服务 —— 半配置的部署最难查")

    def test_tls_without_any_certificates_is_refused(self):
        self._templates_tree(certs=())
        self.write_env(TLS_ENV)
        p = self.run_deploy("--proxy")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("fullchain.pem", p.output)
        self.assertNotIn("up -d", p.calls)

    def test_missing_templates_dir_is_refused(self):
        """配置源目录不存在 = 挂到一个空目录上，容器会去服务镜像自带的默认站点。

        那种失败**也是 200**（只是 /health 变成 404），日志里只有一句 envsubst 没找到
        模板 —— 很容易被当成「反代起来了，只是健康检查路径不对」。
        """
        self._templates_tree()
        self.write_env(TLS_ENV.replace("./deploy/nginx/tls", "./deploy/nginx/tlss"))
        p = self.run_deploy("--proxy")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("tlss", p.output)
        self.assertNotIn("up -d", p.calls)

    def test_plaintext_mode_does_not_require_certificates(self):
        """证书检查只在 HTTPS 模式生效 —— 明文部署不该被它拦住（那是假警报）。"""
        self.write_env("MYSQL_ROOT_PASSWORD=abc123def456\nAPI_BIND=127.0.0.1\n"
                       "API_PORT=8000\nPROXY_HTTP_PORT=80\n")
        p = self.run_deploy("--proxy")
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("反代已启用", p.output)
        # 没写 NGINX_TEMPLATES_DIR 时走默认值 ./deploy/nginx/templates（setUp 里造了）。

    # ── 拉代码 ──────────────────────────────────────────────────────────
    def test_pull_runs_git_pull_ff_only(self):
        self.write_env(GOOD_ENV)
        self.make_git_repo()
        p = self.run_deploy()
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("git pull --ff-only", p.calls)

    def test_no_pull_skips_git_pull(self):
        """`--no-pull` 是 CI 和重复部署用的；它必须**真的**不拉。"""
        self.write_env(GOOD_ENV)
        self.make_git_repo()
        p = self.run_deploy("--no-pull")
        self.assertEqual(p.returncode, 0, p.output)
        self.assertNotIn("git pull", p.calls)
        self.assertIn("up -d --build", p.calls)


if __name__ == "__main__":
    unittest.main()
