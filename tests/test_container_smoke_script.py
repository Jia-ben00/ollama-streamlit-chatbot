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
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "container_smoke.sh"

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


@unittest.skipIf(shutil.which("bash") is None, "环境里没有 bash，无法执行被抽取的脚本片段")
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
                ["bash", script], capture_output=True, text=True, timeout=180
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


if __name__ == "__main__":
    unittest.main()
