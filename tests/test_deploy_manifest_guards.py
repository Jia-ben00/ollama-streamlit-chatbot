"""元测试：证明 `test_deploy_manifest` 里的守卫真的会「咬」。

一个从不失败的测试等于没有测试。`test_deploy_manifest.py` 断言的那些不变量
（.env 不许进镜像、api 必须透传代码会读的环境变量、Linux 要有 extra_hosts …），
如果哪天有人把断言写松了、或者写成了恒真条件，测试依然全绿 —— 而问题照旧上线。

所以这里做一次**反向对照**：把每个要防的缺陷人为种回去，确认对应用例会失败。

为什么这招可行：那个模块是**纯文件解析**的，路径来自模块级常量（COMPOSE /
DOCKERIGNORE）。把常量指向「被改坏的副本」，就能在不装 Docker 的情况下验证守卫。
这是「测测试本身」——代价很小（毫秒级），收益是那些断言不会悄悄失效。

种子缺陷一览（每一条都对应一个真实踩过的坑）：
  1. api 少透传 REDIS_URL        -> 容器里缓存静默失效（永远不会缓存）
  2. 缺 extra_hosts              -> Linux 上 host.docker.internal 无法解析
  3. 透传没人读的变量             -> 无效配置，改了也没用
  4. 字符集写成 MYSQL_CHARSET     -> mysql 镜像不支持该变量，被静默忽略
  5. 把 3306 映射到宿主机         -> 公网数据库端口 = 交给全世界的扫描器
  6. .dockerignore 不排除 .env    -> 数据库密码被烤进镜像层
  7. 排除掉 Dockerfile 要 COPY 的文件 -> 镜像构建直接失败
  8. 去掉 .gitattributes 的 eol=lf   -> Windows 检出成 CRLF，.dockerignore 规则静默失效
"""

import io
import tempfile
import unittest
from pathlib import Path

import tests.test_deploy_manifest as manifest

REPO = Path(__file__).resolve().parents[1]


class GuardBiteBase(unittest.TestCase):
    """提供「改坏副本 -> 跑目标用例 -> 看它是否报错」的能力。"""

    @classmethod
    def setUpClass(cls):
        cls.compose_text = manifest.COMPOSE.read_text(encoding="utf-8")
        cls.ignore_text = manifest.DOCKERIGNORE.read_text(encoding="utf-8")
        cls.gitattributes_text = manifest.GITATTRIBUTES.read_text(encoding="utf-8")
        cls._tmp = tempfile.TemporaryDirectory(prefix="deploy_guard_")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run_target(
        self, test_name, compose_patch=None, ignore_patch=None, gitattributes_patch=None
    ) -> bool:
        """跑目标用例，返回是否**通过**（我们期望它失败）。"""
        tmp = Path(self._tmp.name)
        compose_path = manifest.COMPOSE
        ignore_path = manifest.DOCKERIGNORE
        gitattributes_path = manifest.GITATTRIBUTES
        if compose_patch is not None:
            compose_path = tmp / f"compose_{abs(hash(test_name))}.yml"
            compose_path.write_text(compose_patch(self.compose_text), encoding="utf-8")
        if ignore_patch is not None:
            ignore_path = tmp / f"ignore_{abs(hash(test_name))}"
            ignore_path.write_text(ignore_patch(self.ignore_text), encoding="utf-8")
        if gitattributes_patch is not None:
            gitattributes_path = tmp / f"gitattributes_{abs(hash(test_name))}"
            gitattributes_path.write_text(
                gitattributes_patch(self.gitattributes_text), encoding="utf-8"
            )

        originals = (manifest.COMPOSE, manifest.DOCKERIGNORE, manifest.GITATTRIBUTES)
        manifest.COMPOSE, manifest.DOCKERIGNORE, manifest.GITATTRIBUTES = (
            compose_path,
            ignore_path,
            gitattributes_path,
        )
        try:
            suite = unittest.TestLoader().loadTestsFromName(
                f"tests.test_deploy_manifest.{test_name}"
            )
            return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite).wasSuccessful()
        finally:
            manifest.COMPOSE, manifest.DOCKERIGNORE, manifest.GITATTRIBUTES = originals

    def assert_bites(self, test_name, **patches):
        passed = self._run_target(test_name, **patches)
        self.assertFalse(
            passed,
            f"把缺陷种回去之后 {test_name} 居然还通过 —— 这条守卫是无效的，"
            "它不会拦住真实的问题",
        )


class TestGuardsBite(GuardBiteBase):
    def test_missing_redis_url_is_caught(self):
        self.assert_bites(
            "TestEnvWiring.test_api_receives_every_env_var_the_code_reads",
            compose_patch=lambda t: t.replace('      REDIS_URL: "redis://redis:6379/0"\n', ""),
        )

    def test_missing_extra_hosts_is_caught(self):
        self.assert_bites(
            "TestEnvWiring.test_host_docker_internal_requires_extra_hosts",
            compose_patch=lambda t: t.replace(
                '    extra_hosts:\n      - "host.docker.internal:host-gateway"\n', ""
            ),
        )

    def test_dead_env_entry_is_caught(self):
        self.assert_bites(
            "TestEnvWiring.test_api_env_has_no_dead_entries",
            compose_patch=lambda t: t.replace(
                '      MAX_TOKENS: "${MAX_TOKENS:-2048}"',
                '      MAX_TOKENS: "${MAX_TOKENS:-2048}"\n      NOBODY_READS_ME: "1"',
            ),
        )

    def test_unsupported_charset_env_var_is_caught(self):
        self.assert_bites(
            "TestEnvWiring.test_mysql_charset_is_set_via_mysqld_command",
            compose_patch=lambda t: t.replace(
                "    command:\n      - --character-set-server=utf8mb4"
                "\n      - --collation-server=utf8mb4_0900_ai_ci",
                "    environment:\n      MYSQL_CHARSET: utf8mb4",
            ),
        )

    def test_public_database_port_is_caught(self):
        self.assert_bites(
            "TestComposeTopology.test_only_api_publishes_ports",
            compose_patch=lambda t: t.replace(
                "    volumes:\n      - mysql_data:/var/lib/mysql",
                '    ports:\n      - "3306:3306"\n    volumes:\n      - mysql_data:/var/lib/mysql',
            ),
        )

    def test_env_file_leak_into_image_is_caught(self):
        self.assert_bites(
            "TestDockerignore.test_credentials_and_vcs_are_excluded",
            ignore_patch=lambda t: t.replace(".env\n.env.*\n", ""),
        )

    def test_copy_of_ignored_file_is_caught(self):
        self.assert_bites(
            "TestDockerfile.test_copy_sources_are_not_dockerignored",
            ignore_patch=lambda t: t + "\nrequirements.txt\n",
        )

    def test_missing_lf_declaration_is_caught(self):
        """去掉 .gitattributes 里的 eol=lf 声明 -> 守卫应报错。"""
        self.assert_bites(
            "TestLineEndings.test_gitattributes_forces_lf_for_container_files",
            gitattributes_patch=lambda t: t.replace(".dockerignore text eol=lf\n", ""),
        )


if __name__ == "__main__":
    unittest.main()
