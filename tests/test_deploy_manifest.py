"""部署清单的静态一致性校验。

这个文件故意不叫「测试业务逻辑」，它测的是**交付物本身**：`docker-compose.yml` /
`Dockerfile` / `.dockerignore` / `.env.prod.example` 之间是否自洽。

为什么值得单独写一组测试：这一层的特点是「本机永远看不出来」——

- 本机没装 Docker，compose 写错了也跑不到；
- 就算装了 Docker Desktop（Mac/Windows），`host.docker.internal` 默认能解析，
  而云主机是 Linux，同一个文件到那边就报 `Name or service not known`；
- compose 的 `.env` 只做**文件内插值**、不注入容器，漏透传的变量在本地绝对发现不了。

结果就是：这些问题只会在「上云的第一小时」暴露，而那时你已经付了服务器钱、
还在电话里跟面试官说"我项目能跑"。所以把它们挪到**提交前**拦截。

关键设计：不是硬编码一份「应该传哪些变量」的清单（那种清单会随代码漂移、然后失效），
而是**从源码里反推**——扫描 `api/ db/ src/ cache.py` 里所有 `os.getenv("X")` 写法，
凡是代码会读的变量，容器就必须拿得到。加了新配置忘了改 compose，测试立刻红。

纯文件解析，不需要 Docker / MySQL / Redis，所以可以进 CI。
"""

import fnmatch
import re
import unittest
from pathlib import Path
from urllib.parse import urlparse

import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "docker-compose.yml"
DOCKERFILE = REPO / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"
ENV_TEMPLATE = REPO / ".env.prod.example"
GITATTRIBUTES = REPO / ".gitattributes"

# compose 里的变量插值：${NAME} 或 ${NAME:-default}
INTERP_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-[^}]*)?\}")

# 代码里读环境变量的三种写法
ENV_READ_RE = re.compile(
    r"""(?:os\.environ\.get|os\.getenv|_get_env(?:_int|_float)?)\(\s*["']([A-Za-z0-9_]+)["']"""
)

# 只会被 Streamlit 界面读、API 容器不需要的变量。
# UI_ONLY：界面标题/主题；CHATBOT_API_BASE：界面要连的后端地址 ——
# 容器自己就是那个后端，不需要知道自己的公网地址。
UI_ONLY_ENV = {"APP_TITLE", "APP_THEME", "CHATBOT_API_BASE"}

# 会被打进 API 镜像的源码（app.py / sentiment_analysis 已被 .dockerignore 排除）
API_SOURCE_ROOTS = ["api", "db", "src", "cache.py"]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_yaml_comments(text: str) -> str:
    """去掉整行注释，避免注释里出现的 ${VAR} 被当成真配置。"""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def parse_env_template() -> dict:
    """把 .env.prod.example 解析成 {KEY: VALUE}（忽略注释与空行）。"""
    result = {}
    for line in _read(ENV_TEMPLATE).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def read_dockerignore() -> list:
    patterns = []
    for line in _read(DOCKERIGNORE).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def is_dockerignored(rel_path: str, patterns: list) -> bool:
    """近似判断仓库相对路径是否被 .dockerignore 排除（够用即可，不追求完整语义）。"""
    ignored = False
    parts = rel_path.replace("\\", "/").split("/")
    for raw in patterns:
        negated = raw.startswith("!")
        pat = raw[1:] if negated else raw
        pat = pat.rstrip("/")
        if not pat:
            continue
        hit = (
            fnmatch.fnmatch(rel_path, pat)
            or fnmatch.fnmatch(rel_path, pat + "/*")
            or any(fnmatch.fnmatch(part, pat) for part in parts)
        )
        if hit:
            ignored = not negated
    return ignored


def dockerfile_instructions() -> list:
    """返回 Dockerfile 的逻辑行（已合并 \\ 续行），例如 ['FROM python:3.11-slim AS builder', ...]。"""
    lines = []
    buffer = ""
    for raw in _read(DOCKERFILE).splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1].strip() + " "
            continue
        buffer += line.strip()
        lines.append(buffer.strip())
        buffer = ""
    if buffer.strip():
        lines.append(buffer.strip())
    return lines


def dockerfile_copy_sources() -> list:
    """Dockerfile 中所有从**构建上下文**COPY 的源路径（跳过 --from= 的跨阶段拷贝）。"""
    sources = []
    for instr in dockerfile_instructions():
        parts = instr.split()
        if not parts or parts[0].upper() != "COPY":
            continue
        args = [p for p in parts[1:] if not p.startswith("--")]
        flags = [p for p in parts[1:] if p.startswith("--")]
        if any(f.startswith("--from=") for f in flags):
            continue  # 来自构建阶段，不是上下文
        if len(args) < 2:
            continue
        sources.extend(args[:-1])  # 最后一个是目标路径
    return sources


def code_env_vars() -> set:
    """扫描 API 侧源码，收集代码会读取的所有环境变量名。"""
    found = set()
    for root in API_SOURCE_ROOTS:
        target = REPO / root
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for f in files:
            found.update(ENV_READ_RE.findall(_read(f)))
    return found


class DeployManifestBase(unittest.TestCase):
    """把清单解析放在类级别，避免每个用例重复读盘。"""

    @classmethod
    def setUpClass(cls):
        cls.compose_text = _read(COMPOSE)
        cls.compose = yaml.safe_load(_strip_yaml_comments(cls.compose_text))
        cls.services = cls.compose["services"]
        cls.env_example = parse_env_template()
        cls.dockerignore = read_dockerignore()
        cls.api_env = cls.services["api"].get("environment") or {}


class TestFilesPresent(DeployManifestBase):
    def test_all_deploy_artifacts_exist(self):
        for path in (COMPOSE, DOCKERFILE, DOCKERIGNORE, ENV_TEMPLATE, REPO / "deploy.sh"):
            with self.subTest(path=path.name):
                self.assertTrue(path.exists(), f"缺少部署产物：{path.name}")


class TestDockerignore(DeployManifestBase):
    """没有 .dockerignore 时，COPY . . 会把一切拷进镜像 —— 包括 .env 里的数据库密码。"""

    def test_credentials_and_vcs_are_excluded(self):
        # deploy.sh 要求先 `cp .env.prod.example .env` 再 build，
        # 所以构建上下文里**一定**有 .env。不排除它，密码就被烤进镜像层了。
        must_exclude = {
            ".env": "MySQL root 密码会被烤进镜像层（可被 docker history / 导出镜像读出）",
            ".git": "整个版本历史进镜像，白撑体积，还可能带上历史提交里的凭据",
            "__pycache__": "与本机 Python 版本绑定的缓存，进容器反而可能出错",
            ".venv": "几百 MB 的虚拟环境，把构建上下文和每次 build 拖慢",
        }
        for name, why in must_exclude.items():
            with self.subTest(excluded=name):
                self.assertTrue(
                    is_dockerignored(name, self.dockerignore),
                    f".dockerignore 没有排除 {name}：{why}",
                )

    def test_image_drops_the_heavy_ui_code(self):
        # Streamlit 界面跑在用户本机；情感分析要 torch。两者都不该进 API 镜像。
        for name in ("app.py", "sentiment_analysis", "tests"):
            with self.subTest(excluded=name):
                self.assertTrue(is_dockerignored(name, self.dockerignore), f"{name} 不该进镜像")


class TestLineEndings(unittest.TestCase):
    """容器相关文件的行尾必须是 LF —— 这是「Windows 上检出、别处构建」才会暴露的坑。"""

    def test_gitattributes_forces_lf_for_container_files(self):
        text = GITATTRIBUTES.read_text(encoding="utf-8")
        for pattern in ("Dockerfile", ".dockerignore"):
            with self.subTest(pattern=pattern):
                found = re.search(rf"^{re.escape(pattern)}\s+(\S.*)$", text, re.MULTILINE)
                self.assertIsNotNone(
                    found,
                    f".gitattributes 没有声明 {pattern} 的换行策略。"
                    "在 Windows 上检出时它会是 CRLF —— .dockerignore 的规则会静默失效"
                    "（模式带 \\r 匹配不上），Dockerfile 的 RUN 续行会被截断",
                )
                self.assertIn("eol=lf", found.group(1), f"{pattern} 必须声明 eol=lf")


class TestDockerfile(DeployManifestBase):
    def test_copy_sources_exist(self):
        sources = dockerfile_copy_sources()
        self.assertTrue(sources, "Dockerfile 里没解析出任何 COPY，检查解析逻辑")
        for src in sources:
            with self.subTest(src=src):
                if src == ".":
                    continue
                self.assertTrue((REPO / src).exists(), f"COPY 的源不存在：{src}")

    def test_copy_sources_are_not_dockerignored(self):
        """COPY 一个被 .dockerignore 排除的文件，会在 build 时报 not found —— 自相矛盾。"""
        for src in dockerfile_copy_sources():
            with self.subTest(src=src):
                if src == ".":
                    continue
                self.assertFalse(
                    is_dockerignored(src, self.dockerignore),
                    f"Dockerfile COPY {src}，但 .dockerignore 又排除了它 —— 镜像构建会失败",
                )

    def test_runtime_stage_drops_root(self):
        instructions = dockerfile_instructions()
        users = [i.split(None, 1)[1].strip() for i in instructions if i.split()[:1] == ["USER"]]
        self.assertTrue(users, "Dockerfile 里没有 USER，容器会以 root 运行")
        self.assertNotIn(users[-1], ("root", "0"), "最后一个 USER 仍是 root")

        last_from = max(i for i, x in enumerate(instructions) if x.split()[:1] == ["FROM"])
        last_user = max(i for i, x in enumerate(instructions) if x.split()[:1] == ["USER"])
        self.assertGreater(last_user, last_from, "USER 出现在运行时阶段之前，等于没生效")

    def test_exposed_port_matches_compose(self):
        exposed = {
            i.split()[1]
            for i in dockerfile_instructions()
            if i.split()[:1] == ["EXPOSE"]
        }
        api_ports = self.services["api"].get("ports") or []
        self.assertTrue(api_ports, "compose 里 api 没有发布端口")
        container_side = str(api_ports[0]).split(":")[-1]
        self.assertIn(container_side, exposed, f"compose 映射到 {container_side}，但 Dockerfile 没 EXPOSE")


class TestComposeTopology(DeployManifestBase):
    # 允许发布端口的服务。只有这两个：api 是应用本身（直连调试 / 容器冒烟要用），
    # proxy 是对外入口（它存在的意义就是对外）。其余一律不许。
    PORT_PUBLISHING_SERVICES = {"api", "proxy"}

    def test_internal_services_never_publish_ports(self):
        """3306/6379 一旦发布到宿主机，公网上就是「数据库直接对外开放」。

        白名单是**显式**的，不是「除了 proxy 都算」—— 后者将来谁把 3306 加进 proxy
        也没人发现。
        """
        for name, svc in self.services.items():
            with self.subTest(service=name):
                if name in self.PORT_PUBLISHING_SERVICES:
                    continue
                self.assertNotIn(
                    "ports", svc,
                    f"{name} 不该把端口发布到宿主机；如果确实需要，把它加进 "
                    "PORT_PUBLISHING_SERVICES 并说明为什么安全",
                )

    def test_proxy_publishes_only_the_entry_port(self):
        """反代是入口，发布端口是对的 —— 但不能顺手把内部端口也带出去。

        端口是模板化的，所以不能靠「字符串里有没有 3306」来判断：真正的约束是
        **它只引用自己的那两个入口端口变量**（HTTP 与 HTTPS）。混进 API_PORT
        （或别的服务端口）就等于把内部端口一起开到公网，而这在 compose 里看起来完全正常。
        """
        proxy = self.services.get("proxy")
        if proxy is None:
            self.skipTest("compose 里没有 proxy 服务")
        specs = [str(x) for x in (proxy.get("ports") or [])]
        self.assertTrue(specs, "proxy 没有发布端口，那它就不算入口")
        allowed = {"PROXY_HTTP_PORT", "PROXY_HTTPS_PORT"}
        for spec in specs:
            with self.subTest(port=spec):
                names = {m[0] for m in INTERP_RE.findall(spec)}
                extra = sorted(names - allowed)
                self.assertEqual(
                    extra, [],
                    f"proxy 的端口映射引用了 {extra}；只该引用 {sorted(allowed)} 里的变量",
                )
                self.assertEqual(
                    len(names), 1,
                    f"这一条映射引用了 {sorted(names)} 个变量：每条只该绑定一个入口端口",
                )
                for internal in ("3306", "6379"):
                    self.assertNotIn(internal, spec, f"proxy 的映射里出现了内部端口 {internal}")

    def test_service_healthy_targets_have_healthcheck(self):
        """写了 condition: service_healthy，目标服务就必须真的定义 healthcheck。"""
        for name, svc in self.services.items():
            depends = svc.get("depends_on") or {}
            if not isinstance(depends, dict):
                continue
            for target, cfg in depends.items():
                if isinstance(cfg, dict) and cfg.get("condition") == "service_healthy":
                    with self.subTest(service=name, target=target):
                        self.assertIn("healthcheck", self.services[target], f"{target} 缺 healthcheck")

    def test_api_healthcheck_probes_liveness_not_readiness(self):
        """容器 healthcheck 探 /health（永远 200），不探 /health/ready —— 否则依赖抖动会引发重启循环。"""
        hc = self.services["api"]["healthcheck"]
        text = " ".join(hc["test"])
        self.assertIn("/health", text)
        self.assertNotIn("/health/ready", text)


class TestEnvWiring(DeployManifestBase):
    """compose 的 .env 只用于文件内插值，不会注入容器 —— 漏透传 = 配置静默失效。"""

    def test_api_receives_every_env_var_the_code_reads(self):
        required = code_env_vars() - UI_ONLY_ENV
        missing = sorted(required - set(self.api_env))
        self.assertEqual(
            missing,
            [],
            "这些环境变量代码会读、但 compose 的 api.environment 没透传 "
            f"（在 .env 里改了不会有任何效果）：{missing}",
        )

    def test_api_env_has_no_dead_entries(self):
        """反向检查：传给容器但没人读的变量 = 无效配置。"""
        known = code_env_vars() | {"MYSQL_ROOT_PASSWORD"}
        dead = sorted(set(self.api_env) - known)
        self.assertEqual(dead, [], f"api.environment 里这些变量没有任何代码读取：{dead}")

    def test_database_url_points_at_the_service_name(self):
        url = str(self.api_env["DATABASE_URL"])
        self.assertIn("@mysql:", url, "容器里必须用服务名 mysql，不能用 127.0.0.1")
        self.assertIn("charset=utf8mb4", url, "连接字符集必须是 utf8mb4，否则 emoji 存不进")

    def test_redis_url_points_at_the_service_name(self):
        """写成 127.0.0.1 不会报错，只会永远不缓存 —— 最难发现的那类坑。"""
        parsed = urlparse(str(self.api_env["REDIS_URL"]))
        self.assertEqual(
            parsed.hostname,
            "redis",
            f"REDIS_URL 的主机名是 {parsed.hostname!r}；容器里 127.0.0.1 指容器自己，"
            "必须是服务名 redis，否则缓存静默失效",
        )

    def test_host_docker_internal_requires_extra_hosts(self):
        """Linux 上这个别名不会自动解析（Mac/Windows 的 Docker Desktop 才自带）。"""
        uses = [k for k, v in self.api_env.items() if "host.docker.internal" in str(v)]
        if not uses:
            self.skipTest("没有用到 host.docker.internal")
        extra = self.services["api"].get("extra_hosts") or []
        joined = " ".join(str(x) for x in extra)
        self.assertIn(
            "host.docker.internal:host-gateway",
            joined,
            f"{uses} 用到了 host.docker.internal，但 api 没有 extra_hosts 映射 —— "
            "Linux 上会直接 Name or service not known",
        )

    def test_compose_vars_are_declared_in_env_template(self):
        """凡是 compose 会插值的变量，示例模板里都得有，否则运维不知道要配什么。"""
        names = {m[0] for m in INTERP_RE.findall(_strip_yaml_comments(self.compose_text))}
        undeclared = sorted(n for n in names if n not in self.env_example)
        self.assertEqual(undeclared, [], f"compose 用了这些变量，但 .env.prod.example 没列出：{undeclared}")

    def test_required_vars_are_nonempty_in_template(self):
        """没有默认值的变量是「必须填」的；模板里留空会让 deploy.sh 的检查失效。"""
        missing = []
        for name, default in INTERP_RE.findall(_strip_yaml_comments(self.compose_text)):
            if default:  # 有 :- 默认值，允许留空
                continue
            if not self.env_example.get(name, "").strip():
                missing.append(name)
        self.assertEqual(missing, [], f"这些变量没有默认值，模板里必须给出可填的样例值：{missing}")

    def test_mysql_charset_is_set_via_mysqld_command(self):
        """mysql 镜像不支持 MYSQL_CHARSET 环境变量，写了会被静默忽略。"""
        mysql = self.services["mysql"]
        env = mysql.get("environment") or {}
        for bogus in ("MYSQL_CHARSET", "MYSQL_COLLATION"):
            with self.subTest(var=bogus):
                self.assertNotIn(
                    bogus, env,
                    f"{bogus} 不是 mysql 官方镜像支持的变量，会被静默忽略；"
                    "应改用 command 透传 mysqld 参数",
                )
        cmd = mysql.get("command")
        self.assertIsNotNone(cmd, "mysql 服务没有通过 command 指定字符集，建库会依赖服务器默认值")
        joined = " ".join(str(x) for x in (cmd if isinstance(cmd, list) else [cmd]))
        self.assertIn("character-set-server=utf8mb4", joined)


if __name__ == "__main__":
    unittest.main()
