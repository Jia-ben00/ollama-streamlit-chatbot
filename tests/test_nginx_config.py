"""反代配置的静态守卫。

这一层和 `test_deploy_manifest.py` 是同一个思路：反代配置里出问题，**本机多半看不出来**——
本机没装 nginx 也没装 Docker，配置写错了只有上云那一刻才知道。所以把能静态判定的部分
挪到提交前：

- 模板里的占位符名字、数量对不对（少一个 → 容器里渲染出 `listen ;`，nginx 直接退出）
- 关键指令在不在（`proxy_buffering off` / `proxy_http_version 1.1` / 超时）
- compose 有没有把模板挂进去、有没有把占位符对应的环境变量传进去
  （**从模板反推**，而不是硬编码一份清单 —— 硬编码的清单会随模板漂移然后失效）
- 行尾是不是 LF（CRLF 会让 nginx 解析失败，且只在 Linux 上暴露）

跑不到的是「nginx 到底会不会把流攒批」——那是运行时行为，见 `tests/e2e/nginx_check.py`
（在真 nginx 容器上量到达时刻）。两者的分工和本项目其它地方一致：
**静态校验证明「文件里写了正确的规则」，真跑证明「规则真的生效」。**
"""

import re
import unittest
from pathlib import Path

import yaml

# 复用部署清单那份 .env 解析器，而不是再写一个 —— 两份解析器早晚会分叉
from tests.test_deploy_manifest import parse_env_template

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "deploy" / "nginx" / "templates" / "default.conf.template"
COMPOSE = REPO / "docker-compose.yml"
GITATTRIBUTES = REPO / ".gitattributes"

# 模板里出现的占位符，全大写。**必须全大写**不是风格问题：nginx 的内置变量
# （$host / $remote_addr / $scheme …）全是小写，而容器的环境变量惯例是全大写，
# 两者天然不撞名。哪天有人写了 ${host}，nginx 渲染出来的 Host 头会变成环境变量的值。
PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

EXPECTED_PLACEHOLDERS = {"SERVER_NAME", "API_UPSTREAM", "LISTEN_PORT"}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def placeholders(text: str) -> set:
    return set(PLACEHOLDER_RE.findall(text))


def strip_comments(text: str) -> str:
    """去掉整行注释：注释里可能为了讲清楚而写出占位符的样子，不该被当成真配置。"""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


# compose 自己的变量插值（`${NAME}` / `${NAME:-default}`）——和 nginx 的 envsubst 不是一回事
COMPOSE_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _compose_interpolate(value: str, env: dict, rounds: int = 5) -> str:
    """按 compose 的语义把 `${NAME:-default}` 解析掉（够用即可，不追求完整语义）。

    必须做这一步，否则会得到**假红**：compose 是先把值解析好再注入容器的，
    而 nginx 镜像的 envsubst 只做单趟替换。
    """
    for _ in range(rounds):
        new = COMPOSE_VAR_RE.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), value)
        if new == value:
            return new
        value = new
    return value


class NginxConfigBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(TEMPLATE)
        cls.config = strip_comments(cls.text)
        cls.compose = yaml.safe_load("\n".join(
            l for l in read(COMPOSE).splitlines() if not l.lstrip().startswith("#")))
        cls.proxy = (cls.compose.get("services") or {}).get("proxy")
        cls.dotenv = parse_env_template()


class TestTemplateIsRenderable(NginxConfigBase):
    """渲染不出来 = 容器起不来，而且是上云那一刻才知道。"""

    def test_template_exists(self):
        self.assertTrue(TEMPLATE.exists(), f"缺少反代模板：{TEMPLATE}")

    def test_placeholder_names_are_uppercase_only(self):
        names = placeholders(self.config)
        lower = sorted(n for n in names if not n.isupper())
        self.assertEqual(
            lower, [],
            f"模板里有小写占位符 {lower}。nginx 的内置变量都是小写，"
            "envsubst 会把它们替换成环境变量的值（通常是空的），配置会静默变形",
        )

    def test_placeholder_set_is_exactly_what_compose_provides(self):
        """多一个少一个都要红：少了渲染出空值，多了说明 compose 没传（渲染成空）。"""
        self.assertEqual(
            placeholders(self.config), EXPECTED_PLACEHOLDERS,
            f"模板占位符与预期不符（实际 {sorted(placeholders(self.config))}）",
        )

    def test_every_placeholder_resolves_with_compose_env(self):
        """按 compose 的真实语义渲染一遍，结果里不该再有占位符残留。

        注意是**两步**，漏掉第一步就会得到假红：compose 会先把自己文件里的
        `${PROXY_HTTP_PORT:-80}` 解析成 `80` 再放进容器环境，
        而 nginx 镜像的 envsubst 是**单趟**替换（不递归）——所以
        `LISTEN_PORT: "${PROXY_HTTP_PORT:-80}"` 到容器里已经是 `80` 了。
        """
        raw_env = {k: str(v).strip('"') for k, v in (self.proxy.get("environment") or {}).items()}

        first_pass = {k: _compose_interpolate(v, self.dotenv) for k, v in raw_env.items()}

        missing = sorted(placeholders(self.config) - set(first_pass))
        self.assertEqual(missing, [], f"compose 的 proxy.environment 没有提供：{missing}")

        rendered = PLACEHOLDER_RE.sub(
            lambda m: first_pass.get(m.group(1), m.group(0)), self.config)
        self.assertNotIn("${", rendered, f"渲染后仍有占位符残留：\n{rendered}")

    def test_scanner_is_not_blind(self):
        """元测试：上面几条依赖「扫描器能看见占位符」。先证明它看得见，再谈它扫到的是空集。"""
        self.assertTrue(placeholders("${A} ${b}"), "扫描器连明显的占位符都没扫到 —— 断言会永远成立")
        self.assertEqual(placeholders("$host $remote_addr"), set(),
                         "扫描器把 nginx 的内置变量也当成了占位符")


class TestSseDirectives(NginxConfigBase):
    """这几条是「流式不会退化成一次性」在配置侧的全部依据。"""

    def test_buffering_is_off(self):
        self.assertIn("proxy_buffering off;", self.config,
                      "没有关掉 proxy_buffering：上游若不是 chunked 分帧，"
                      "nginx 会把 SSE 攒成一批再发（实测见 docs/DEPLOY.md §6.1）")

    def test_upstream_connection_is_kept_alive(self):
        self.assertIn("proxy_http_version 1.1;", self.config,
                      "SSE 是长连接，必须对上游用 HTTP/1.1")
        self.assertIn('proxy_set_header Connection "";', self.config,
                      "Connection 头没置空的话，nginx 不会对上游保持长连接")

    def test_timeouts_outlast_a_slow_model(self):
        """默认 60s 会在长回复上掐断上游 —— 表现为「聊到一半断开」，不是报错。"""
        for directive in ("proxy_read_timeout", "proxy_send_timeout"):
            with self.subTest(directive=directive):
                m = re.search(rf"{directive}\s+(\d+)s;", self.config)
                self.assertIsNotNone(m, f"没有设置 {directive}")
                self.assertGreaterEqual(int(m.group(1)), 300,
                                        f"{directive}={m.group(1)}s 比 OLLAMA_TIMEOUT(120s) 只高一点，"
                                        "模型稍慢就会被掐断")

    def test_proxies_to_the_compose_service_not_localhost(self):
        """容器里的 127.0.0.1 指容器自己 —— 写死它，反代永远连不上 api。"""
        self.assertIn("proxy_pass http://${API_UPSTREAM};", self.config)
        upstream = str((self.proxy.get("environment") or {}).get("API_UPSTREAM", ""))
        self.assertIn("api:", upstream, f"API_UPSTREAM={upstream!r} 应该用服务名 api")


class TestComposeWiring(NginxConfigBase):
    def test_proxy_service_exists_and_is_opt_in(self):
        self.assertIsNotNone(self.proxy, "compose 里没有 proxy 服务")
        self.assertIn("proxy", self.proxy.get("profiles") or [],
                      "proxy 必须放在 profiles 里：默认 up 不该多占 80 端口、多拉镜像")

    def test_templates_are_mounted_from_the_repo(self):
        """配置的单一来源必须是仓库里那个模板 —— 手工在服务器上写 nginx.conf，
        下一次 git pull 之后两者就会漂移，而且没人会发现。"""
        mounts = [str(v) for v in (self.proxy.get("volumes") or [])]
        joined = " ".join(mounts)
        self.assertIn("./deploy/nginx/templates", joined)
        self.assertIn("/etc/nginx/templates", joined,
                      "挂到别处 nginx 官方镜像的 envsubst 不会处理它")
        self.assertIn(":ro", joined, "配置是只读挂载，容器不该能改它")

    def test_proxy_waits_for_the_api(self):
        depends = self.proxy.get("depends_on") or {}
        self.assertEqual((depends.get("api") or {}).get("condition"), "service_healthy",
                         "反代不该在 api 就绪前接流量")


class TestLineEndings(unittest.TestCase):
    def test_gitattributes_forces_lf_for_nginx_config(self):
        """CRLF 会让 nginx 解析失败 —— 而这只在 Linux 上暴露（本机是 Windows）。"""
        text = read(GITATTRIBUTES)
        for pattern in ("*.template", "*.conf"):
            with self.subTest(pattern=pattern):
                found = re.search(rf"^{re.escape(pattern)}\s+(\S.*)$", text, re.MULTILINE)
                self.assertIsNotNone(found, f".gitattributes 没有声明 {pattern} 的换行策略")
                self.assertIn("eol=lf", found.group(1), f"{pattern} 必须声明 eol=lf")

    def test_template_on_disk_has_no_crlf(self):
        raw = TEMPLATE.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), 0,
                         "模板在磁盘上是 CRLF。nginx 读到行尾的 \\r 会直接报错退出；"
                         "先检查 .gitattributes 的 *.template 规则是否生效")


if __name__ == "__main__":
    unittest.main()
