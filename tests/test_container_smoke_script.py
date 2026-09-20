"""容器冒烟脚本里「③ 端口」那段的守卫 —— 把时序脆弱点变成确定性用例。

背景（真在 CI 上踩过）
--------------------
`.github/scripts/container_smoke.sh` 里 api 端口那条断言原本是「立刻 inspect，
没有 HostPort 就失败」。而 api 在 compose 里 `depends_on: {mysql,redis}:
service_healthy`，是三个容器里最后一个启动的；脚本常常在它 Up 后不到 1 秒就走到
断言处，那一刻 Docker 还没把端口绑定写进 NetworkSettings.Ports，inspect 回来的
是 `{}`。于是出现一次假红 —— 同一次提交、同一脚本，原样重跑就绿了。

为什么需要这条守卫
------------------
「改了之后 CI 绿了」证明不了任何事：这条断言本来就 flaky，绿是它的常态。
要证明等待逻辑成立，需要**确定性**的场景：
  - 端口晚于容器就绪出现  → 应该等到并放行（不误报）
  - 端口始终没有映射      → 应该 fail-closed 报错（不静默通过）
  - inspect 拿到空状态    → 不能把「什么都没读到」当成「没有暴露」，那是假绿

做法
----
抽出 `container_smoke.sh` 里 ③ 段的**原文**，前面接一个 stub 的 `docker()` 函数，
交给 bash 执行。唯一改动是把 `PORT_WAIT_SECS` 从 30 缩成 3（几秒内跑完），
并且**断言这处替换真的发生过** —— 否则跑的可能是别的东西。

注意这里 stub 的是命令本身（bash 函数覆盖），不是 PATH 上的可执行文件，
所以 Windows / Linux 行为一致，不需要 exec 权限。
"""

import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "container_smoke.sh"

# 用**解析出来的绝对路径**执行，不要给 subprocess 传裸名字 "bash"。
#
# 实测（2026-09-20，本机装完 WSL2 之后暴露出来的）：同一个字符串，两套解析会找到
# **两个不同的 bash**——
#   shutil.which("bash")          → ...\PortableGit\...\usr\bin\bash.EXE  （能用 rc=0）
#   subprocess.run(["bash", ...]) → C:\Windows\System32\bash.exe          （WSL 转发器 rc=1）
# 原因是 Windows 的 CreateProcess 搜索顺序把 System32 排在 PATH **之前**：装了 WSL2
# （哪怕一个发行版都没装）就会凭空多出 System32\bash.exe，把 PATH 上的 Git Bash 顶掉。
# 报错还完全看不出是「找错了 bash」：
#   <3>WSL (550 - Relay) ERROR: CreateProcessCommon:818: execvpe(/bin/bash) failed
# 最坏的地方是它和下面那句 skipIf 用的是**两套解析**：守卫说「这台机器有 bash」，
# 实际执行的却是另一个 —— 守卫和被测对象必须是同一个东西，所以这里把解析结果存下来共用。
BASH = shutil.which("bash")

# ③ 段的首尾锚点。锚点一旦变化，抽取必须报错而不是静默返回半截内容。
ANCHOR_START = "# ③ 数据库"
ANCHOR_END = 'ok "api 已向宿主机暴露端口'

WAIT_VAR = "PORT_WAIT_SECS"
WAIT_DEFAULT = f"{WAIT_VAR}=30"
WAIT_FOR_TEST = f"{WAIT_VAR}=3"

# 覆盖 docker 命令用的替身：compose ps -q <svc> 给假 id，inspect 按 MODE 返回。
STUB = r"""
set -euo pipefail
die() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ok] %s\n' "$*"; }
STATE="__STATE__"
echo 0 > "$STATE"
MODE="__MODE__"
docker() {
  if [ "${1:-}" = "compose" ] && [ "${2:-}" = "ps" ] && [ "${3:-}" = "-q" ]; then
    echo "cid-${4:-}"; return 0
  fi
  if [ "${1:-}" = "inspect" ]; then
    cid="${4:-}"
    # 计数必须**按容器分开**：如果用一个全局计数，api 之前的 mysql/redis 已经
    # 各 probe 过一次，等轮到 api 时计数早就越过阈值了 —— 于是「慢就绪」这个
    # 场景压根触发不了，看似在测等待，其实一次都没等（反向对照时被这一点骗过）。
    n=$(cat "$STATE.$cid" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" > "$STATE.$cid"
    if [ "$cid" = "cid-mysql" ] && [ "$MODE" = "mysql_empty" ]; then
      echo '{}'; return 0
    fi
    if [ "$cid" = "cid-api" ]; then
      if [ "$MODE" = "api_slow" ] && [ "$n" -ge 2 ]; then
        echo '{"8000/tcp":[{"HostIp":"0.0.0.0","HostPort":"8000"}]}'
      else
        echo '{}'
      fi
      return 0
    fi
    echo '{"3306/tcp":null}'
    return 0
  fi
  return 0
}
"""


def extract_port_section(text):
    """从 container_smoke.sh 全文里取出 ③ 段（含首尾锚点行）。

    锚点找不到就抛 LookupError —— 这是 fail-closed：静默返回空列表会让下面几条
    用例在「什么都没测」的情况下通过，比没有守卫更坏。
    """
    lines = text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if start is None and line.startswith(ANCHOR_START):
            start = i
        if start is not None and line.startswith(ANCHOR_END):
            end = i
            break
    if start is None or end is None:
        raise LookupError(
            "在 %s 里找不到 ③ 段（start=%r end=%r）。"
            "锚点变了就来更新本文件，不要静默跳过。"
            % (SCRIPT.name, start, end)
        )
    return lines[start:end + 1]


@unittest.skipIf(BASH is None, "环境里没有 bash，无法执行被抽取的脚本片段")
class TestContainerSmokePortWait(unittest.TestCase):
    """api 端口断言必须「等」：这是它从假红变成可靠断言的唯一区别。"""

    def _run_case(self, mode):
        body = "\n".join(extract_port_section(SCRIPT.read_text(encoding="utf-8")))
        # 断言替换前提：上限是单一来源，且本测试确实改动到了它。
        self.assertEqual(
            body.count(WAIT_DEFAULT), 1,
            "③ 段里应恰好有一处 %s（等待上限的唯一来源），实际 %d 处"
            % (WAIT_DEFAULT, body.count(WAIT_DEFAULT)),
        )
        body = body.replace(WAIT_DEFAULT, WAIT_FOR_TEST)

        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "probe_count")
            script = os.path.join(tmp, "case.sh")
            stub = STUB.replace("__STATE__", state).replace("__MODE__", mode)
            with open(script, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(stub + "\n" + body + "\n")
            proc = subprocess.run(
                [BASH, script], capture_output=True, text=True, timeout=180
            )
        return proc.returncode, proc.stdout + proc.stderr

    def test_端口晚于容器就绪出现时应等到而不是误报(self):
        rc, out = self._run_case("api_slow")
        self.assertEqual(rc, 0, "第 2 次 probe 就能拿到端口，却判成了失败：\n" + out)
        self.assertIn("api 已向宿主机暴露端口", out)

    def test_端口始终没有映射时应失败(self):
        rc, out = self._run_case("api_never")
        self.assertNotEqual(rc, 0, "端口始终没有映射，居然通过了：\n" + out)
        self.assertIn("api 仍没有映射到宿主机的端口", out)

    def test_inspect_拿到空状态时不能被当成未暴露(self):
        rc, out = self._run_case("mysql_empty")
        self.assertNotEqual(rc, 0, "mysql 的 inspect 返回 {} 时放行了 —— 这是假绿：\n" + out)
        self.assertIn("端口信息为空", out)


class TestAnchorExtractionIsFailClosed(unittest.TestCase):
    """锚点漂移必须炸出来。抽取逻辑本身没有 bash 依赖，所以不随上面跳过。"""

    def test_缺少起始锚点时报错(self):
        with self.assertRaises(LookupError):
            extract_port_section('ok "api 已向宿主机暴露端口"\n')

    def test_缺少结束锚点时报错(self):
        with self.assertRaises(LookupError):
            extract_port_section("# ③ 数据库 / 缓存端口没有映射到宿主机\nsome line\n")

    def test_真实脚本里能抽到完整一段(self):
        seg = extract_port_section(SCRIPT.read_text(encoding="utf-8"))
        self.assertGreater(len(seg), 10, "抽出来的 ③ 段太短，锚点可能只匹配到一半")
        joined = "\n".join(seg)
        self.assertIn("docker inspect", joined)
        self.assertIn(WAIT_DEFAULT, joined)


FAKE_BUSY_START = "port_busy() {"
FAKE_BUSY_END = "}"


def msys_path(p):
    """Windows 路径 → MSYS 能认的 POSIX 形态；Linux 上原样返回。

    （和 `tests/test_deploy_script.py` 里同名函数一样，各留一份：两个文件互不依赖，
    这几行也不值得为它建一个公共模块。少这一步，写进脚本的 `C:\\...\\python.exe`
    在 bash 里根本执行不了 —— 而报错会伪装成「脚本坏了」。）
    """
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":  # C:/... → /c/...
        return "/" + s[0].lower() + s[2:]
    return s


def extract_port_busy(text):
    """抽出脚本里 `port_busy()` 那个函数原文。

    为什么单独守它：它是「起假 Ollama 之前先确认端口没被别人占」的那句判断。
    本机实测过一起真实事故 —— 宿主机装着真 Ollama（占 127.0.0.1:11434），
    而 Windows 允许 `0.0.0.0:11434` 与它共存，于是替身照样起来了，
    容器却连到了真 Ollama（/health 里 `ollama: true`，/chat 返回 404），
    冒烟脚本只报「流式返回 0 个 chunk」。这个函数就是那次事故的补丁。

    锚点找不到就抛 LookupError（fail-closed），理由同上面那个抽取函数。
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(FAKE_BUSY_START)), None)
    if start is None:
        raise LookupError("在 %s 里找不到 %r" % (SCRIPT.name, FAKE_BUSY_START))
    end = next((i for i in range(start + 1, len(lines)) if lines[i] == FAKE_BUSY_END), None)
    if end is None:
        raise LookupError("找到了 %r 但找不到它的结尾 `}`，抽取会返回半截内容" % FAKE_BUSY_START)
    return "\n".join(lines[start:end + 1])


@unittest.skipIf(BASH is None, "环境里没有 bash，无法执行被抽取的脚本片段")
class TestFakeOllamaPortGuard(unittest.TestCase):
    """端口守卫必须**真的**能区分「有人占着」和「空着」—— 两种结果都要出现。"""

    def _probe(self, port):
        body = extract_port_busy(SCRIPT.read_text(encoding="utf-8"))
        # 抽出来的是真脚本原文，只补上它依赖的两个变量。
        script = (
            "set -u\n"
            "FAKE_PORT=%d\n"
            "PYTHON=%s\n" % (port, shlex.quote(msys_path(sys.executable)))
            + body + "\n"
            "if port_busy; then echo BUSY; else echo FREE; fi\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.sh")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(script)
            proc = subprocess.run([BASH, path], capture_output=True, text=True,
                                  errors="replace", timeout=60)
        return proc.returncode, (proc.stdout or "").strip()

    def test_端口被占用时报忙(self):
        """正例：真开一个监听端口，port_busy 必须判成占用。"""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        port = srv.getsockname()[1]
        rc, out = self._probe(port)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, "BUSY", "端口上确实有服务在听，却判成了空闲：%s" % out)

    def test_端口空着时报空闲(self):
        """反例：端口空着时必须判成空闲，否则脚本会拦正确部署（假警报）。"""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        rc, out = self._probe(port)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, "FREE", "端口空着却判成占用，会把正常部署拦下来：%s" % out)

    def test_抽出来的是真的端口探测而不是常量(self):
        """能力检查：抽到的原文里必须真有 socket 探测，否则上面两条毫无意义。"""
        body = extract_port_busy(SCRIPT.read_text(encoding="utf-8"))
        self.assertIn("connect_ex", body)
        self.assertIn("FAKE_PORT", body, "端口没接线到变量，探测的会是写死的端口")

    def test_脚本必须两处都验假_Ollama_的身份(self):
        """这两处是 2026-09-20 那起「容器连到真 Ollama」事故的补丁。

        纯文本断言，只起「被静默删掉」的报警作用 —— 真正的行为由上面两条 +
        `container_smoke.sh` 真跑覆盖。删掉它们的人应该看到这里红。
        """
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("指纹已核对", text, "宿主机侧没有核对假 Ollama 的指纹")
        self.assertIn("容器确实连到假 Ollama", text,
                      "少了容器侧的指纹断言 —— 容器走的路径和宿主机不同，"
                      "只验宿主机那侧挡不住「容器连到了别的 Ollama」")


if __name__ == "__main__":
    unittest.main()
