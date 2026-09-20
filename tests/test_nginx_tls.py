"""HTTPS 那一份反代模板的静态守卫。

和 `test_nginx_config.py` 的分工：那份管**明文**配置能不能渲染、SSE 指令在不在；
这份只管 HTTPS 变体**独有的**那几件事，以及两份模板之间**不许漂移**。

为什么值得单独一个文件：`deploy/nginx/tls/default.conf.template` 与
`deploy/nginx/templates/default.conf.template` 共用同一段 location（proxy_buffering、
超时、转发头这些「流式活不活」的关键指令）。两份文件必然要复制一次 ——
复制的东西一定会漂移，除非有东西钉住它。所以这里有一条**逐字节相同**的断言：
改了一份忘了另一份，CI 直接红，而不是上线后变成「HTTP 好好的、HTTPS 上聊天卡住」。

这里全都是「本机看不出来、上云那一刻才知道」的东西（证书路径、跳转顺序、
TLS 版本下限）。真跑那一层见 `tests/e2e/nginx_check.py` 的 C 段（真 nginx + 自签证书 +
到达时刻），本文件证明的是「文件里写了正确的规则」。
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

# 复用已经有的解析器，而不是再写一份 —— 两份早晚会分叉
from tests.test_deploy_manifest import parse_env_template
from tests.test_nginx_config import (
    PLACEHOLDER_RE,
    _compose_interpolate,
    placeholders,
    strip_comments,
)

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_HTTP = REPO / "deploy" / "nginx" / "templates" / "default.conf.template"
TEMPLATE_TLS = REPO / "deploy" / "nginx" / "tls" / "default.conf.template"
CERTS_DIR = REPO / "deploy" / "nginx" / "certs"
COMPOSE = REPO / "docker-compose.yml"
GITATTRIBUTES = REPO / ".gitattributes"
GITIGNORE = REPO / ".gitignore"

# 两份模板里标出「这一整段是共享的」的标记。**必须两边一模一样**，否则没法比对；
# 守卫会先确认标记存在（见 test_shared_region_markers_exist_in_both），
# 找不到就当成失败 —— 否则「标记被删掉」会让逐字节比对静默退化成「空 == 空」。
MARK_BEGIN = "# ==== 共享段开始 ===="
MARK_END = "# ==== 共享段结束 ===="

SHARED_RE = re.compile(re.escape(MARK_BEGIN) + r"(.*?)" + re.escape(MARK_END), re.S)

# 证书在**容器里**的固定路径。名字写死是刻意的：挂载点由 compose 固定，少一个能配错的
# 地方。（certbot 的 live/<域名>/ 正好就是这两个名字。）
CERT_FILE = "fullchain.pem"
KEY_FILE = "privkey.pem"

# 私钥/证书类文件一律不许进仓库。用后缀扫，而不是扫具体文件名：
# 有人叫 server.key / my-site.pem / chain.crt 都该被拦下。
SECRET_SUFFIXES = (".pem", ".key", ".crt", ".p12", ".pfx")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def shared_region(text: str):
    m = SHARED_RE.search(text)
    return None if m is None else m.group(1)


def server_blocks(text: str):
    """把 `server { ... }` 逐块切出来，用于判「块内」性质（如明文入口里有没有 proxy_pass）。

    只按行扫大括号深度 —— 足够读 nginx 配置（`{` / `}` 都在行尾或行首，且不嵌套在
    字符串里）。**必须传去掉注释的文本**（`strip_comments` 之后）：注释里出现的
    `{` 或 `server {` 会把切块切歪。
    """
    blocks, depth, buf = [], 0, []
    for line in text.splitlines():
        if not buf and depth == 0 and line.strip().startswith("server"):
            buf = [line]
            depth = line.count("{") - line.count("}")
            if depth == 0:  # 单行写完整段的极端情形
                blocks.append(line)
                buf = []
            continue
        if buf:
            buf.append(line)
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                blocks.append("\n".join(buf))
                buf, depth = [], 0
    return blocks


def secret_paths(paths):
    """从一串路径里挑出证书/私钥类文件。抽成函数是为了能对它本身做检查（见元测试）。"""
    return sorted(p for p in paths if p.lower().endswith(SECRET_SUFFIXES))


class TlsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http_text = read(TEMPLATE_HTTP)
        cls.tls_text = read(TEMPLATE_TLS)
        cls.tls_config = strip_comments(cls.tls_text)
        cls.compose = yaml.safe_load("\n".join(
            l for l in read(COMPOSE).splitlines() if not l.lstrip().startswith("#")))
        cls.proxy = (cls.compose.get("services") or {}).get("proxy")
        cls.dotenv = parse_env_template()


class TestTlsTemplateIsRenderable(TlsBase):
    def test_template_exists(self):
        self.assertTrue(TEMPLATE_TLS.exists(), f"缺少 HTTPS 模板：{TEMPLATE_TLS}")

    def test_placeholders_are_exactly_what_compose_provides(self):
        """少一个 → 渲染出 `listen ;`，nginx 起不来；多一个 → compose 没传，渲染成空值。"""
        self.assertEqual(
            placeholders(self.tls_config),
            {"SERVER_NAME", "API_UPSTREAM", "LISTEN_PORT", "LISTEN_TLS_PORT"},
            f"HTTPS 模板占位符与预期不符（实际 {sorted(placeholders(self.tls_config))}）",
        )

    def test_placeholder_names_are_uppercase_only(self):
        lower = sorted(n for n in placeholders(self.tls_config) if not n.isupper())
        self.assertEqual(lower, [],
                         f"HTTPS 模板里有小写占位符 {lower}：nginx 的内置变量都是小写，"
                         "envsubst 会把它们换成环境变量的值（通常为空），配置静默变形")

    def test_renders_without_leftover_placeholders(self):
        raw_env = {k: str(v).strip('"') for k, v in (self.proxy.get("environment") or {}).items()}
        # 必须两步：compose 先把自己文件里的 `${PROXY_HTTPS_PORT:-443}` 解析成 443
        # 再放进容器环境，而 nginx 镜像的 envsubst 是**单趟**替换（不递归）——
        # 少这一步会得到假红。
        resolved = {k: _compose_interpolate(v, self.dotenv) for k, v in raw_env.items()}
        rendered = PLACEHOLDER_RE.sub(
            lambda m: resolved.get(m.group(1), m.group(0)), self.tls_config)
        self.assertNotIn("${", rendered, f"渲染后仍有占位符残留：\n{rendered}")

    def test_no_crlf_on_disk(self):
        raw = TEMPLATE_TLS.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), 0,
                         "HTTPS 模板在磁盘上是 CRLF。nginx 读到行尾的 \\r 会直接报错退出；"
                         "先检查 .gitattributes 的 *.template 规则是否生效")


class TestTlsServerBlock(TlsBase):
    def test_listens_ssl_on_its_own_port(self):
        self.assertIn("listen ${LISTEN_TLS_PORT} ssl;", self.tls_config,
                      "443 上必须带 ssl —— 少了它就是在明文端口上提供「HTTPS」入口")

    def test_certificate_paths_are_the_fixed_ones(self):
        for path in (f"/etc/nginx/certs/{CERT_FILE}", f"/etc/nginx/certs/{KEY_FILE}"):
            with self.subTest(path=path):
                self.assertIn(path, self.tls_config,
                              f"模板没从固定路径读 {path}；证书名字是模板与 compose、"
                              "deploy.sh 三边约定好的，改一处就会错位")

    def test_tls_floor_is_1_2(self):
        m = re.search(r"ssl_protocols\s+([^;]+);", self.tls_config)
        self.assertIsNotNone(m, "没有显式声明 ssl_protocols —— 默认值随 nginx 版本变")
        protocols = m.group(1).split()
        self.assertIn("TLSv1.3", protocols)
        downgraded = [p for p in protocols if p in ("TLSv1", "TLSv1.1", "SSLv3", "SSLv2")]
        self.assertEqual(downgraded, [],
                         f"放开到了 {downgraded}：这些版本已被认为不安全，"
                         "而且放开它们通常没有真实需求")

    def test_plaintext_entry_only_redirects(self):
        """80 那个 server 必须**只**做跳转：一旦它也开始 proxy_pass，就等于留了一条明文后门。"""
        blocks = server_blocks(self.tls_config)
        self.assertGreaterEqual(len(blocks), 2, f"应该有两段 server，实际 {len(blocks)} 段")
        plain = blocks[0]
        self.assertIn("return 301 https://", plain, "明文入口没有跳到 HTTPS")
        self.assertNotIn("proxy_pass", plain,
                         "明文入口里出现了 proxy_pass —— 那意味着内容可以经明文拿到，"
                         "跳转就成了摆设")

    def test_acme_challenge_is_served_before_the_redirect(self):
        """ACME 的 http-01 挑战必须在跳转之前放行，否则证书续期会静默失败。

        续期失败的表现是「某天站点突然打不开」，而不是一条当场可见的报错 ——
        所以这条顺序值得钉住。
        """
        plain = server_blocks(self.tls_config)[0]
        self.assertIn("/.well-known/acme-challenge/", plain,
                      "明文入口没有放行 ACME 的挑战路径 —— 证书续期会失败")
        m = re.search(r"location[^{]*acme-challenge[^{]*\{([^}]*)\}", plain, re.S)
        self.assertIsNotNone(m, "找不到 acme-challenge 的 location 块")
        self.assertNotIn("return 301", m.group(1),
                         "挑战路径里也放了跳转：certbot 会拿到 301 而不是挑战文件")
        self.assertLess(plain.index("acme-challenge"), plain.index("location /"),
                        "挑战 location 必须排在兜底的 `location /` 之前")


class TestNoDriftBetweenVariants(TlsBase):
    def test_shared_region_markers_exist_in_both(self):
        """先证明「比对器看得见东西」，再谈它比出来的结果 —— 标记被删掉会让比对静默失效。"""
        for name, text in (("明文", self.http_text), ("HTTPS", self.tls_text)):
            with self.subTest(variant=name):
                self.assertIsNotNone(
                    shared_region(text),
                    f"{name}模板里找不到共享段标记（{MARK_BEGIN} / {MARK_END}）："
                    "没有它，『两份不许漂移』这条守卫会退化成空比对")
                self.assertIn("proxy_buffering off;", shared_region(text),
                              f"{name}模板的共享段里没有 body（标记画错位置了？）")

    def test_shared_region_is_byte_identical(self):
        """**这条是本次改动的核心。** 复制出来的配置一定会漂移，除非有东西钉住它。"""
        a, b = shared_region(self.http_text), shared_region(self.tls_text)
        self.assertEqual(
            a, b,
            "两份模板的共享段不再逐字节相同（proxy_buffering / 超时 / 转发头这些"
            "『流式活不活』的指令在里面）。要么把改动同步过去，要么把差异挪到共享段之外。")

    def test_both_variants_declare_the_same_sse_rules(self):
        """上面那条是逐字节比；这条是「人话版」的兜底：关键指令确实两边都在。"""
        for directive in ("proxy_buffering off;", "proxy_http_version 1.1;",
                          'proxy_set_header Connection "";', "proxy_read_timeout 300s;"):
            for name, text in (("明文", self.http_text), ("HTTPS", self.tls_text)):
                with self.subTest(directive=directive, variant=name):
                    self.assertIn(directive, text)


class TestComposeWiring(TlsBase):
    def test_config_source_is_switchable(self):
        mounts = " ".join(str(v) for v in (self.proxy.get("volumes") or []))
        self.assertIn("/etc/nginx/templates:ro", mounts,
                      "挂载点必须是 /etc/nginx/templates —— 那是官方镜像 envsubst 的约定位置")
        self.assertIn("NGINX_TEMPLATES_DIR", mounts,
                      "配置源没有可切换的入口：HTTPS 那份模板就没法启用")

    def test_certs_are_mounted_read_only(self):
        mounts = " ".join(str(v) for v in (self.proxy.get("volumes") or []))
        self.assertIn("/etc/nginx/certs:ro", mounts,
                      "证书目录没有（或不是只读）挂进反代容器 —— 模板里的 "
                      f"/etc/nginx/certs/{CERT_FILE} 会读不到")

    def test_https_port_is_mapped(self):
        ports = " ".join(str(p) for p in (self.proxy.get("ports") or []))
        self.assertIn("PROXY_HTTPS_PORT", ports,
                      "443 没有映射到宿主机：HTTPS 只会在容器里活着")

    def test_healthcheck_works_in_both_modes(self):
        """HTTPS 模式下 80 只回 301，探针不能只认明文。"""
        test = self.proxy["healthcheck"]["test"]
        joined = " ".join(str(x) for x in (test if isinstance(test, list) else [test]))
        self.assertIn("https://", joined,
                      "探针只探明文：HTTPS 模式下 80 是 301，容器会一直 unhealthy")
        self.assertIn("--no-check-certificate", joined,
                      "探针去探 HTTPS 却不信任自签/内网证书，会一直失败；"
                      "注意这个开关只应作用在这根探针上")

    def test_env_template_declares_the_tls_switches(self):
        for name in ("NGINX_TEMPLATES_DIR", "TLS_CERT_DIR", "PROXY_HTTPS_PORT"):
            with self.subTest(name=name):
                self.assertIn(name, self.dotenv,
                              f".env.prod.example 没列出 {name}：运维不知道有这个开关")


class TestSecretsStayOutOfTheRepo(TlsBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tracked = cls._git_ls_files()

    @staticmethod
    def _git_ls_files():
        git = shutil.which("git")
        if not git:
            return None
        p = subprocess.run([git, "ls-files"], cwd=str(REPO), capture_output=True,
                           text=True, errors="replace")
        return None if p.returncode != 0 else p.stdout.splitlines()

    def test_scanner_is_not_blind(self):
        """元测试：下面那条断言靠这个函数。先证明它认得出来，再谈仓库里没有。"""
        self.assertEqual(
            secret_paths(["deploy/nginx/certs/privkey.pem", "a/server.KEY", "b/chain.crt",
                          "src/main.py", "README.md"]),
            ["a/server.KEY", "b/chain.crt", "deploy/nginx/certs/privkey.pem"],
            "扫描器认不出证书/私钥文件 —— 它会永远返回空列表，断言也就永远成立")

    def test_no_certificate_or_key_files_are_tracked(self):
        if self.tracked is None:
            self.skipTest("环境里没有可用的 git，无法列出被跟踪的文件")
        found = secret_paths(self.tracked)
        self.assertEqual(found, [],
                         f"仓库里跟踪着证书/私钥：{found}。私钥泄漏是不可逆的 —— "
                         "删掉文件不够，还要当作已泄漏处理（重新签发）")

    def test_gitignore_covers_the_certs_dir(self):
        text = read(GITIGNORE)
        self.assertIn("deploy/nginx/certs/*", text,
                      "证书目录没被 .gitignore 挡住：本地生成一张证书就会被 git add 进来")
        self.assertIn("!deploy/nginx/certs/.gitkeep", text,
                      "占位文件也被忽略了，挂载点在仓库里就消失了")

    def test_certs_dir_is_kept_with_a_placeholder(self):
        self.assertTrue((CERTS_DIR / ".gitkeep").exists(),
                        "缺 deploy/nginx/certs/.gitkeep：目录本身不在仓库里，"
                        "compose 挂载时会被 Docker 以 root 身份临时创建（权限与预期不符）")


class TestDeliberateOmissions(TlsBase):
    """把「有意没做」也钉住 —— 免得哪天有人顺手加上去，而没人知道那是没验过的东西。"""

    def test_no_hsts_header(self):
        self.assertNotIn("Strict-Transport-Security", self.tls_config,
                         "加了 HSTS：浏览器一旦记住就很难回退（要等 max-age 过期，"
                         "期间站点直接打不开）。证书续期这条路跑顺之前不要加，见 DEPLOY.md §6.2")

    def test_http2_not_enabled(self):
        self.assertNotIn("http2", self.tls_config,
                         "开了 HTTP/2：多一层分帧与多路复用，而 SSE 在 h2 下的到达时刻"
                         "没量过。要先在 tests/e2e/nginx_check.py 里把它量出来")


if __name__ == "__main__":
    unittest.main()
