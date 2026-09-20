"""`tests/e2e/proxy_deploy_check.py` 的守卫：它自己安全吗、它真的被接进去了吗。

这个脚本干的事有风险：它会**改 `.env`**（配置源、证书目录、API_BIND），并且会让
`docker compose up` **重建 api 容器**。所以它必须自己回答两个问题，这里各守一组：

1. **改 .env 的代码可逆吗** —— `env_set` 是纯函数，不用 Docker 就能测：它只动目标那一行、
   保留注释与顺序、按原文件的行尾风格续写新行（`write_text` 会把 `\\n` 翻成 `\\r\\n`，
   往返一趟就改了字节，这是本项目实际踩过的坑），并且「设回去」应当逐字节还原。
2. **它有没有被真正接进验收链路，以及该拦的都拦得住吗** —— 静态断言：
   容器冒烟调它、且只在「服务是本次运行自己拉起的」才调（线上验收不能改别人的服务）、
   传了 bash 的宿主路径、找 openssl 失败就响亮退出、`.env` 在 `finally` 里按字节还原并校验 sha256。
"""

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
E2E = REPO / "tests" / "e2e"
SMOKE = REPO / ".github" / "scripts" / "container_smoke.sh"
CHECKER = E2E / "proxy_deploy_check.py"

sys.path.insert(0, str(E2E))

from proxy_deploy_check import env_get, env_set  # noqa: E402


class TestEnvEditingIsReversible(unittest.TestCase):
    """改 .env 这件事必须是可逆的 —— 它改的是「下一次部署会读到的东西」。"""

    def test_replaces_the_matching_key_only(self):
        text = "A=1\nB=2\n"
        self.assertEqual(env_set(text, "A", "9"), "A=9\nB=2\n")

    def test_a_key_that_is_a_prefix_of_another_is_not_touched(self):
        # 这是最容易写错的一种：用 `startswith("A")` 会把 AB= 一起改掉。
        text = "A=1\nAB=2\n"
        self.assertEqual(env_set(text, "A", "9"), "A=9\nAB=2\n")

    def test_appends_when_missing_and_keeps_lf(self):
        self.assertEqual(env_set("A=1\n", "B", "2"), "A=1\nB=2\n")

    def test_appends_a_newline_before_appending_when_file_has_none(self):
        # 文件末尾没有换行时要先补一个，否则会把最后一行和新增行粘成一行 ——
        # 那种坏法很隐蔽：被粘掉的是**另一条**配置。
        self.assertEqual(env_set("A=1", "B", "2"), "A=1\nB=2\n")

    def test_preserves_crlf_when_appending(self):
        self.assertEqual(env_set("A=1\r\n", "B", "2"), "A=1\r\nB=2\r\n")

    def test_preserves_crlf_when_replacing(self):
        self.assertEqual(env_set("A=1\r\nB=2\r\n", "A", "9"), "A=9\r\nB=2\r\n")

    def test_comments_and_order_are_preserved(self):
        text = "# 说明\nA=1\n\n# 另一段\nB=2\n"
        got = env_set(text, "B", "9")
        self.assertEqual(got, "# 说明\nA=1\n\n# 另一段\nB=9\n")

    def test_setting_an_existing_key_back_is_byte_identical(self):
        # 「设回去 == 原样」是这个脚本能安全当验收脚本用的前提。
        text = "# c\nA=1\nB=2\nC=3\n"
        back = env_set(env_set(env_set(text, "A", "x"), "B", "y"), "C", "z")
        self.assertEqual(env_set(env_set(env_set(back, "A", "1"), "B", "2"), "C", "3"), text)

    def test_env_get_reads_value_and_falls_back(self):
        text = "A=1\nB=\n"
        self.assertEqual(env_get(text, "A"), "1")
        self.assertEqual(env_get(text, "B"), "")
        self.assertEqual(env_get(text, "Z"), None)
        self.assertEqual(env_get(text, "Z", "default"), "default")


class TestCheckerIsWiredIntoTheSmoke(unittest.TestCase):
    """它被接进容器冒烟了吗？第 9 步起的是自己的 nginx 容器，**不会**启动 compose 的
    proxy 服务 —— 所以如果没人调它，`deploy.sh --proxy` 就还是「谁也没跑过」。"""

    # 真实的**调用行**。注意不能只搜文件名：冒烟脚本开头的「它会做什么」注释里
    # 也提到这个名字，用 index() 会命中那段注释 —— 断言就落在一个跟执行无关的位置上，
    # 这正是「守卫看起来在守、其实没守」的一种写法。
    INVOKE = '"$PYTHON" tests/e2e/proxy_deploy_check.py --owned'

    def setUp(self):
        self.smoke = SMOKE.read_text(encoding="utf-8")
        self.checker = CHECKER.read_text(encoding="utf-8")

    def test_smoke_invokes_the_checker_exactly_once(self):
        hits = [ln for ln in self.smoke.splitlines() if self.INVOKE in ln]
        self.assertEqual(len(hits), 1, f"应只有一处调用，实际 {len(hits)}")

    def test_it_is_gated_on_the_stack_not_pre_existing(self):
        # 线上验收时（服务已在跑）绝不能执行 —— 它会改 .env 并重建 api 容器。
        idx = self.smoke.index(self.INVOKE)
        head = self.smoke[:idx]
        gate = head.rindex('if [[ "$PRE_EXISTING" == "1" ]]')
        block = head[gate:idx]
        # 门控分支里必须是「跳过」，而不是「照跑」
        self.assertIn("跳过", block)
        self.assertIn("elif", block)  # PRE_EXISTING=1 → 跳过；否则再看 WITH_PROXY_DEPLOY

    def test_bash_is_handed_over_as_a_host_path(self):
        # 装了 WSL 之后 subprocess 里的 "bash" 与 shutil.which("bash") 会是两个不同的
        # bash（System32 的转发器 vs Git Bash）。所以要显式传绝对路径，
        # 并且在 MSYS 下用 cygpath 转成宿主看得懂的写法。
        self.assertIn("SMOKE_BASH", self.smoke)
        self.assertIn("cygpath", self.smoke)
        # 传递必须发生在调用那一行上（写在别处等于没传）
        call = [ln for ln in self.smoke.splitlines() if self.INVOKE in ln][0]
        self.assertIn("SMOKE_BASH=", call)

    def test_the_step_is_skippable_for_narrower_runs(self):
        self.assertIn("WITH_PROXY_DEPLOY", self.smoke)

    def test_cleanup_stops_the_proxy_profile_too(self):
        """收尾的 `down` 必须带 `--profile proxy`。

        不带的话 `proxy` 属于非激活 profile，`down` 不会去停它 —— 反代容器继续占着
        80/443，而且网络删不掉（实测 `Resource is still in use`），下一次 up 端口冲突。
        这条是**本轮的实测产物**：第 10 步第一次真跑之后，proxy 就是这么活下来的。
        """
        start = self.smoke.index("cleanup() {")
        end = self.smoke.index("trap cleanup EXIT")
        cleanup = self.smoke[start:end]
        self.assertIn("docker compose --profile proxy down", cleanup)
        # 不能对整段做 `assertNotIn("docker compose down -v")` —— KEEP=1 那句 warn 里
        # 就原样写着 `docker compose down -v`（给用户的提示），会被误判成「出现了裸 down」。
        # 要判的是**真的会被执行的那一行命令**：逐行看，凡是以 `docker compose down`
        # 开头的命令（注释与字符串不算）都必须带上 profile。
        bare = [ln.strip() for ln in cleanup.splitlines()
                if ln.strip().startswith("docker compose down")]
        self.assertEqual(bare, [], f"收尾里出现了不带 profile 的 down：{bare}")


class TestCheckerFailsClosed(unittest.TestCase):
    """一个「失败时不会失败」的检查比没有检查更危险 —— 逐条钉住它的失败路径。"""

    def setUp(self):
        self.src = CHECKER.read_text(encoding="utf-8")

    def test_missing_openssl_is_loud(self):
        # 镜像里没有 openssl；找不到宿主那份时必须响亮失败，不能静默跳过这一段
        # —— 跳过就等于 TLS 这一层又没验。
        self.assertRegex(self.src, r"if not openssl:\s*\n(?:.*\n)*?\s*die\(")

    def test_refuses_to_mutate_a_stack_it_does_not_own(self):
        self.assertIn("stack_is_running", self.src)
        self.assertRegex(self.src, r"if stack_is_running\(docker\) and not args\.owned:")

    def test_env_is_restored_in_finally_and_verified_by_sha256(self):
        self.assertIn("finally:", self.src)
        self.assertIn("sha256", self.src)
        # 还原必须在 finally 里：中途任何一步失败（包括 SystemExit）都不能把 .env
        # 留在 HTTPS 形态上，否则下一次 up 会悄悄用错配置。
        finally_block = self.src[self.src.index("finally:"):]
        self.assertIn("orig_env", finally_block)
        self.assertIn("sha256_bytes(ENV_FILE.read_bytes())", self.src)

    def test_restore_failure_is_reported_and_counts_as_failure(self):
        # 还原结果本身也是一条断言（在 failures 里），不是只打印一行日志。
        self.assertIn("`.env` 已按字节还原", self.src)

    def test_deploy_failure_stops_before_the_rest_of_the_checks(self):
        self.assertRegex(self.src, r"if p\.returncode != 0:\s*\n(?:.*\n)*?\s*return 1")

    def test_proxy_healthcheck_must_be_healthy_not_merely_running(self):
        # 「容器在跑」与「nginx 真的能应答」是两件事，而这颗探针本身就是被测对象。
        self.assertIn('"{{.State.Health.Status}}"', self.src)
        self.assertIn('== "healthy"', self.src)

    def test_redirect_is_asserted_on_scheme_and_path_only_when_https_is_off_443(self):
        # 模板用 `$host` 拼 Location，非 443 时它必然指到 443（模板里写明的前提）。
        # 所以「跟随跳转」这条只在 443 上成立 —— 脚本必须把这件事显式分支处理，
        # 而不是装作两种情况一样。
        self.assertRegex(self.src, r'if https_port == "443":')

    def test_ca_path_is_exercised_with_verification_still_on(self):
        # --insecure 会关掉证书校验；验收用的必须是 --ca（校验仍然开着）。
        self.assertIn('"--ca"', self.src)
        self.assertNotRegex(self.src, r'"--insecure"')

    def test_a_squatted_host_port_warns_instead_of_failing(self):
        """宿主上别的进程抢答入口端口 —— 不是本次部署的缺陷，但**必须说出来**。

        实测（2026-09-20）：本机 `Steam++.Accelerator.exe` 占着 IPv4 的 80/443，
        它回一个**没有 Server 头**的 404；容器内 `127.0.0.1/health` 明明是 301。
        「端口能连上」不等于「连到的是我们的服务」—— 所以判据必须是 `Server: nginx`，
        并且要能换一个真打到容器的地址继续验收，而不是把这件事判成部署失败
        （也不是装作没发生）。
        """
        self.assertIn("被别的进程抢答", self.src)
        self.assertIn('"nginx" in server.lower()', self.src)
        self.assertIn("warn(", self.src)
        # 只有**两个候选地址都**到不了我们时才判失败并停下
        self.assertIn("if entry_addr is None:", self.src)
        # 后续断言与 public_check 都必须用那个选中的地址，不能回头写死 127.0.0.1
        self.assertIn("entry_addr}/health", self.src)
        self.assertIn("entry_host", self.src)


class TestTheNewScriptIsNotPartOfTheMainTestSuite(unittest.TestCase):
    """`tests/e2e/` 下的脚本不能匹配 `test*.py`，否则会被主 CI 收集并尝试真跑。"""

    def test_checker_lives_in_e2e_and_is_not_named_test(self):
        self.assertTrue(CHECKER.exists())
        self.assertFalse(CHECKER.name.startswith("test"))

    def test_main_ci_discovery_does_not_pick_it_up(self):
        # 主 CI 跑的是 `python -m unittest discover tests`（pattern 默认 test*.py），
        # 所以它不会被收集到 —— 这里用文件名规则把它钉住。
        self.assertFalse(re.match(r"^test.*\.py$", CHECKER.name))


if __name__ == "__main__":
    unittest.main()
