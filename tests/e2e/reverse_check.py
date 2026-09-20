"""反向对照：把缺陷「种回」源码，确认对应用例真的会红。

## 为什么需要它

「测试全绿」本身不能证明测试有效 —— 断言写松了、写成恒真条件，一样全绿。
唯一可靠的判据是：把缺陷种回去，看它会不会失败。

本脚本覆盖六组：

| 分组 | 被测对象 | 对应用例 |
|---|---|---|
| `chat_stream` | `api/routers/chat.py` 的流式协议（Content-Type / 防缓冲头 / SSE 空行分帧 / done 字段 / 404 校验 / 断连报错 / latency_ms 落库） | `tests/test_chat_stream.py` |
| `schema` | ORM 定义与快照（缺列 / 类型 / 枚举取值 / 可空性 / 索引 / 字符集声明 / 符号台账 / 快照少表） | `tests/test_schema_snapshot.py` |
| `container_smoke` | `.github/scripts/container_smoke.sh` 的端口断言等待逻辑 | `tests/test_container_smoke_script.py` |
| `stream_probe` | `src/stream_probe.py` 的「流式有没有退化成攒批」判据 | `tests/test_stream_probe.py` |
| `nginx` | `deploy/nginx/templates/default.conf.template` + compose 挂载 + `.gitattributes`（关缓冲 / HTTP1.1 / Connection 置空 / 超时 / 上游用服务名 / 占位符全大写 / 模板真挂上） | `tests/test_nginx_config.py` |
| `deploy_script` | `deploy.sh` 的每一条前置检查与分支（没装 docker / 无 compose 插件 / 守护进程不可用 / 缺 .env / 密码缺失或不合格 / 参数打错 / `--proxy` 与 API_BIND / `--no-pull` / 轮询上限） | `tests/test_deploy_script.py` |

`deploy_script` 这一组和 `container_smoke` 是同一类：**被测文件长期处于「谁也没执行过」的状态**。
`deploy.sh` 是文档里「上机第一步」，但 CI 直接跑 `container_smoke.sh`，静态校验只把它当文本读
（查行尾、查字符串）。第一次真跑（替身 docker）就抓到一个静态校验永远看不见的洞 ——
`.env` 少一行 `MYSQL_ROOT_PASSWORD=` 时，脚本**连一句输出都没有**就退出（`set -e` + `pipefail`）。
所以这里把每条检查逐个拆掉，确认真的有用例在看它。

最后两组守的都不是业务代码，而是**判据本身**：一个量不出东西的尺子，
和一个量出「一切正常」的尺子长得一模一样。`stream_probe` 那把尤其值得守——
它错的时候部署是「能用的」，只是从「一个个蹦字」变成「转圈等到最后出全文」，
没有报警、没有异常，只有到达时刻能看出来。

`nginx` 这一组守的是**公网入口**这一层的配置。它的静态守卫（`test_nginx_config.py`）
和 `container_smoke` 同属「本机看不出来、上云才知道」的那类：配置写错，本机没装
nginx 也跑不出任何症状，要等 CI 第 9 步在真 nginx 容器上量到达时刻才会暴露。
所以这里把每一句关键指令都种回一次，确认它真的被某条用例盯着。

## 设计上的三条硬要求

1. **fail-closed**：任何一个「种回缺陷后测试还是绿的」都算失败并以非 0 退出；
   锚点找不到时**直接中止**，绝不静默跳过（否则脚本会假装跑过）。
2. **按字节还原**：文本模式的 read/write 在 Windows 上会来回翻译 `\\n` 和 `\\r\\n`，
   往返一趟就可能改写文件（真的踩过：把 chat.py 写成 172 行 `\\r\\r\\n`）。
   所以原文件按 `read_bytes` 保存、`write_bytes` 还原，并校验 sha256。
3. **改一个、跑一个、立刻还原**：不留中间态。

这个脚本会**修改仓库里的源码文件**，所以它放 `tests/e2e/`（不匹配 test*.py，
不进 CI、不会被误跑）。运行期间不要同时编辑这些文件。

用法（在仓库根目录）：
    python tests/e2e/reverse_check.py                 # 跑全部分组
    python tests/e2e/reverse_check.py schema          # 只跑一组
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY = sys.executable

CHAT = REPO / "api" / "routers" / "chat.py"
MODELS = REPO / "db" / "models.py"
SNAPSHOT = REPO / "tests" / "data" / "practice_db_schema.json"

TS = "tests.test_chat_stream"
TSS = "tests.test_schema_snapshot"

CHUNK_YIELD = r'''                yield f"data: {json.dumps({'chunk': chunk}, ensure_ascii=False)}\n\n"'''
ERR_YIELD = r'''            yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"'''
DONE_YIELD = r'''        yield f"data: {json.dumps({'done': True, 'latency_ms': latency_ms}, ensure_ascii=False)}\n\n"'''

CONTENT_COL = "    content = Column(Text, nullable=False)"
ENUM_VALUES = '        Enum("system", "user", "assistant", native_enum=True), nullable=False'
LATENCY_COL = "    latency_ms = Column(INTEGER(unsigned=True), nullable=True)"
TOKEN_COL = '    token_count = Column(INTEGER(unsigned=True), nullable=False, server_default="0")'
CREATED_IDX = "    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)"
USERS_ARGS = '__tablename__ = "users"\n    __table_args__ = _table_args()'

# ── container_smoke 组的锚点 ─────────────────────────────────────
# 这一组被测的不是 Python 代码，而是 .github/scripts/container_smoke.sh 里
# api 端口那条断言的**等待逻辑**。它值得单独守，因为它本身就是一次真实事故的产物：
# api 在 compose 里 depends_on 另外两个 service_healthy，是最后一个启动的，
# 脚本常在它 Up 后不到 1 秒就断言端口 —— 那一刻 Docker 还没把端口绑定写进
# NetworkSettings.Ports，inspect 回来是 {}，于是 CI 假红一次（同提交重跑就绿）。
CS = REPO / ".github" / "scripts" / "container_smoke.sh"
TCS = "tests.test_container_smoke_script"

WAIT_LOOP = '''for _ in $(seq 1 "$PORT_WAIT_SECS"); do
  api_ports="$(docker inspect --format '{{json .NetworkSettings.Ports}}' "$apid" 2>/dev/null || true)"
  if echo "$api_ports" | grep -q 'HostPort'; then break; fi
  sleep 1
done
'''
WAIT_LOOP_ONCE = '''api_ports="$(docker inspect --format '{{json .NetworkSettings.Ports}}' "$apid" 2>/dev/null || true)"
'''
HEALTH_TCP_CHECK = r'''  echo "$ports" | grep -q 'tcp' \
    || die "$svc 的端口信息为空（$ports）——inspect 没拿到有效状态，「未暴露端口」这个结论不成立"
'''
API_PORT_DIE = r'''echo "$api_ports" | grep -q 'HostPort' \
  || die "等了 ${PORT_WAIT_SECS}s，api 仍没有映射到宿主机的端口，外部访问不到（ports=$api_ports）"'''

# 起假 Ollama **之前**那句「端口没被别人占」的探测（2026-09-20 起）。
#
# 它是被一次真实事故逼出来的：本机装着真 Ollama（占 127.0.0.1:11434），而 Windows
# 允许 `0.0.0.0:11434` 与它同时绑定，于是替身照样起来了 —— 容器却连到了真 Ollama。
# 表现是 /health 里 `ollama: true`（真 Ollama 答的 /api/tags）而 /chat 返回 404
# `model 'llama3.2' not found`，冒烟脚本只报「流式返回 0 个 chunk」。
# 这条变异把它拆成「恒判空闲」，于是那句前置检查永远不会触发。
BUSY_PROBE = r'''sys.exit(0 if s.connect_ex(('127.0.0.1', ${FAKE_PORT})) == 0 else 1)'''
BUSY_PROBE_DEAD = r'''sys.exit(1)'''

# ── stream_probe 组的锚点 ────────────────────────────────────────
# 这一组被测的是「公网入口那把尺子」本身：src/stream_probe.py 里判断
# 「回复还是不是逐块到达」的判据。为什么尺子也要做反向对照 ——
# 一把永远返回「流式正常」的尺子，会让 `proxy_buffering on` 这种部署一路绿灯，
# 而它的表现只是「从一个个蹦字变成转圈等到最后出全文」，功能上没人会报警。
PROBE = REPO / "src" / "stream_probe.py"
TSP = "tests.test_stream_probe"

SPAN_RULE = '    if metrics["span"] < min_span:'
FIRST_RULE = '    if float(metrics["first"] or 0.0) > first_ratio * max(total, 1e-9):'
INCONCLUSIVE_BRANCH = "            INCONCLUSIVE,\n            metrics,"

# ── nginx 组的锚点 ──────────────────────────────────────────────
# 这一组守「公网入口」那一层：deploy/nginx/templates/default.conf.template
# 里每一句关键指令，以及 compose 有没有把模板真挂进容器、.gitattributes 有没有
# 钉住 LF。它们共同的特点是 —— 错了本机毫无症状（本机没装 nginx），
# 只有上云 / CI 第 9 步才暴露，且暴露出来的现象（转圈等到最后出全文、
# 反代连不上 api、容器起不来）都不是一条能看懂的报错。
NGINX_TMPL = REPO / "deploy" / "nginx" / "templates" / "default.conf.template"
COMPOSE = REPO / "docker-compose.yml"
GITATTRIBUTES = REPO / ".gitattributes"
TNX = "tests.test_nginx_config"

BUFFERING_OFF = "proxy_buffering off;"
HTTP11 = "proxy_http_version 1.1;"
CONN_EMPTY = 'proxy_set_header Connection "";'
READ_TIMEOUT = "proxy_read_timeout 300s;"
PROXY_PASS = "proxy_pass http://${API_UPSTREAM};"
SERVER_NAME_LINE = "server_name ${SERVER_NAME};"
TEMPLATE_MOUNT = "./deploy/nginx/templates:/etc/nginx/templates:ro"
GITATTR_TEMPLATE_LF = "*.template text eol=lf"

# ── deploy_script 组的锚点 ──────────────────────────────────────
# 这一组和 container_smoke 是同一类：被测文件长期处于「谁也没执行过」的状态。
# deploy.sh 是文档里的「上机第一步」，但 CI 直接跑 container_smoke.sh，
# 静态校验（tests/test_deploy_manifest.py）只把它当**文本**读 —— 查行尾、查字符串。
# 第一次真跑（替身 docker）就抓到一个静态校验永远看不见的洞：
# `.env` 里少一行 MYSQL_ROOT_PASSWORD= 时，脚本连一句输出都没有就退出。
#
# 下面每条锚点都对应脚本里一个「要不要继续」的判断。逐个拆掉，确认真的有条用例在看它：
# 把部署脚本的守卫拆空，它就会「成功」地部署出一个后门全开的服务，然后打印部署完成。
DS = REPO / "deploy.sh"
TDS = "tests.test_deploy_script"

# ⚠️ 锚点必须打在**调用点**，不能打在 read_env 内部 —— 第一版打在内部，
#    种回去之后测试**没变红**，也就是那条用例压根没被验证过。原因值得记下来：
#
#    `set -e` 不会中止**命令替换的子壳**，子壳的退出码只取决于它的最后一条命令。
#    实测（同一份探针跑两个实现，结论一致）：
#      Linux bash 5.2.37（容器里）与 Cygwin bash 5.3.15（本机 Git Bash）
#      `echo "$(false; echo INSIDE)"`           → 打印 INSIDE 并继续（rc=0）
#      `x="$(false)"`（顶层赋值）               → 静默退出（rc=1）
#      `f(){ false; }; f` / `( false; echo X )` → 退出（rc=1）
#    于是：
#      · 历史形态 `PW="$(grep ... | head | cut)"` 是**顶层赋值** ⇒ 拿到失败状态 ⇒
#        errexit 让整个脚本静默退出（本组存在的理由）。
#      · 同一根管道包进以 `printf` 收尾的函数、再用 `PW="$(read_env ...)"` 调用 ⇒
#        子壳跑到底、返回 printf 的 0 ⇒ 失败被**转成空串**，脚本继续往下走。
#    两种坏法由**不同的东西**兜住：前者靠 `|| true`，后者靠调用方显式判空
#    `[[ -n "$PW" ]] || die`。所以拆掉 read_env 里的 `|| true` 行为完全不变
#    （实测），真正在接线的是调用方那句判空 —— 锚点因此打在这里。
PW_READ_CALL = r'''PW="$(read_env MYSQL_ROOT_PASSWORD)"'''
PW_READ_CALL_OLD = r'''PW="$(grep -E '^MYSQL_ROOT_PASSWORD=' .env | head -1 | cut -d= -f2-)"'''
PW_NOT_EMPTY = r'''[[ -n "$PW" ]] || die'''
PW_NOT_DEFAULT = r'''[[ "$PW" != "change-me" && "$PW" != "123456" ]] || die'''
PW_NO_AT = r'''if [[ "$PW" == *"@"* ]]; then'''
ENV_FILE_CHECK = r'''[[ -f .env ]] || die "缺少 .env。先执行：cp .env.prod.example .env 然后填好 MYSQL_ROOT_PASSWORD"'''
UNKNOWN_ARG = r'''    *) die "不认识的参数：$arg（只支持 --no-pull / --proxy）" ;;'''
NO_PULL_FLAG = r'''    --no-pull) PULL=0 ;;'''
BIND_LOOPBACK = r'''  if [[ "$BIND" != "127.0.0.1" ]]; then'''
PROFILE_UP = r'''  docker compose --profile proxy up -d --build'''
READY_PROMISE = r'''log "等待 API 就绪（轮询 http://127.0.0.1:8000/health/ready，最多 ${READY_TIMEOUT} 秒）"'''
READY_PROMISE_FIXED = r'''log "等待 API 就绪（轮询 http://127.0.0.1:8000/health/ready，最多 180 秒）"'''
READY_GUARD = r'''if [[ "$READY" != "1" ]]; then'''
DEPLOY_DONE = r'''log "部署完成"'''

GROUPS = {
    "chat_stream": [
        ("SSE Content-Type", CHAT,
         '        media_type="text/event-stream",',
         '        media_type="application/json",',
         TS + ".TestSSEFrameFormat.test_content_type_is_event_stream"),
        ("反代防缓冲头", CHAT,
         '        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},',
         "        headers={},",
         TS + ".TestSSEFrameFormat.test_no_buffering_headers_for_reverse_proxy"),
        ("SSE 空行分帧", CHAT, CHUNK_YIELD, CHUNK_YIELD.replace(r"\n\n", r"\n"),
         TS + ".TestSSEFrameFormat.test_every_frame_starts_with_data_and_ends_with_blank_line"),
        ("done 事件字段", CHAT, DONE_YIELD,
         r'''        yield f"data: {json.dumps({'done': True}, ensure_ascii=False)}\n\n"''',
         TS + ".TestDoneEvent.test_last_event_is_done_with_int_latency"),
        ("会话不存在 404", CHAT,
         '        raise HTTPException(status_code=404, detail="会话不存在")',
         "        pass  # 缺陷种回：不校验会话存在",
         TS + ".TestUnknownConversation.test_returns_404_and_not_500"),
        ("Ollama 断连报错事件", CHAT, ERR_YIELD,
         "            pass  # 缺陷种回：断连时不报错",
         TS + ".TestOllamaDisconnect.test_connection_error_becomes_error_event_not_500"),
        ("latency_ms 落库", CHAT,
         "            latency_ms=latency_ms,",
         "            # latency_ms=latency_ms,  # 缺陷种回",
         TS + ".TestLatencyPersisted.test_assistant_message_persists_latency_ms"),
    ],
    "schema": [
        ("缺列", MODELS, CONTENT_COL, "    # content 列被删掉了（缺陷种回）",
         TSS + ".TestTablesAndColumns.test_same_column_set_per_table"),
        ("类型不符", MODELS, CONTENT_COL, "    content = Column(String(255), nullable=False)",
         TSS + ".TestColumnTypes.test_base_types_match"),
        ("枚举取值不符", MODELS, ENUM_VALUES,
         '        Enum("system", "user", "asistant", native_enum=True), nullable=False',
         TSS + ".TestColumnTypes.test_base_types_match"),
        ("可空性不符", MODELS, LATENCY_COL,
         "    latency_ms = Column(INTEGER(unsigned=True), nullable=False)",
         TSS + ".TestColumnTypes.test_nullability_matches"),
        ("缺索引", MODELS, CREATED_IDX,
         "    created_at = Column(DateTime, nullable=False, server_default=func.now())",
         TSS + ".TestIndexes.test_index_shapes_match_both_directions"),
        ("缺字符集声明", MODELS, USERS_ARGS,
         '__tablename__ = "users"\n    __table_args__ = {}',
         TSS + ".TestCharsetDeclaration.test_orm_declares_charset_and_collation"),
        ("符号不一致（宽严台账）", MODELS, TOKEN_COL,
         '    token_count = Column(INTEGER(unsigned=False), nullable=False, server_default="0")',
         TSS + ".TestWidthLedger.test_width_diffs_match_the_ledger_exactly"),
        ("快照里少了一张表", SNAPSHOT, '  "messages": {', '  "messages_RENAMED": {',
         TSS + ".TestTablesAndColumns.test_same_table_set_both_directions"),
    ],
    "container_smoke": [
        ("端口断言不等就绪", CS, WAIT_LOOP, WAIT_LOOP_ONCE,
         TCS + ".TestContainerSmokePortWait.test_端口晚于容器就绪出现时应等到而不是误报"),
        ("丢空状态健全性检查", CS, HEALTH_TCP_CHECK, "",
         TCS + ".TestContainerSmokePortWait.test_inspect_拿到空状态时不能被当成未暴露"),
        ("端口缺失不再失败", CS, API_PORT_DIE,
         '''echo "$api_ports" | grep -q 'HostPort' || true''',
         TCS + ".TestContainerSmokePortWait.test_端口始终没有映射时应失败"),
        # 起替身前的端口占用检查被拆成「恒判空闲」：于是「端口上有人」这条分支
        # 再也不会走到，容器连到别人家的服务时脚本不再早停。
        ("端口守卫恒判空闲", CS, BUSY_PROBE, BUSY_PROBE_DEAD,
         TCS + ".TestFakeOllamaPortGuard.test_端口被占用时报忙"),
    ],
    "stream_probe": [
        # ① 不判「跨度」——最典型的假修法：块数、内容、Content-Type 全对，只是全挤在一起。
        #    注意钉的是**隔离样本**那条用例：现实样本往往同时触发「首块位置」判据，
        #    会把这个变异遮住（第一版就是这么漏过去的）。
        ("尺子不判跨度", PROBE, SPAN_RULE, "    if False:  # 缺陷种回：攒批不再判红",
         TSP + ".TestVerdicts.test_one_burst_early_in_the_stream_is_buffered"),
        # ② 块数不足时判「通过」——把「量不出来」说成「没问题」
        ("块数不足当通过", PROBE, INCONCLUSIVE_BRANCH,
         "            INCREMENTAL,\n            metrics,",
         TSP + ".TestVerdicts.test_too_few_chunks_is_inconclusive_not_pass"),
        # ③ 不判「首块位置」——生成完再一次性吐出来的那种形态就漏了
        ("尺子不判首块位置", PROBE, FIRST_RULE, "    if False:  # 缺陷种回：不判首块位置",
         TSP + ".TestVerdicts.test_content_flushed_only_at_the_end_is_buffered"),
        # ④ 阈值不接线：参数还在、还长得像配置，但判定读的是常量
        ("阈值参数不接线", PROBE, SPAN_RULE,
         '    if metrics["span"] < DEFAULT_MIN_SPAN:',
         TSP + ".TestRulerIsNotBlind.test_thresholds_actually_participate"),
    ],
    "nginx": [
        # 静态守卫守的是「文件里那句规则还在不在」。把每一句关键指令改掉一次，
        # 确认确实有某条用例在看它 —— 而不是「模板改坏了也没人发现」。
        ("不关反代缓冲", NGINX_TMPL, BUFFERING_OFF, "proxy_buffering on;",
         TNX + ".TestSseDirectives.test_buffering_is_off"),
        ("对上游退化成 HTTP/1.0", NGINX_TMPL, HTTP11, "proxy_http_version 1.0;",
         TNX + ".TestSseDirectives.test_upstream_connection_is_kept_alive"),
        ("Connection 头没置空", NGINX_TMPL, CONN_EMPTY,
         'proxy_set_header Connection "keep-alive";',
         TNX + ".TestSseDirectives.test_upstream_connection_is_kept_alive"),
        # 超时退回 nginx 默认值：模型稍慢就「聊到一半断开」，不是报错
        ("读超时退回短值", NGINX_TMPL, READ_TIMEOUT, "proxy_read_timeout 60s;",
         TNX + ".TestSseDirectives.test_timeouts_outlast_a_slow_model"),
        # 写死 127.0.0.1：容器里的 localhost 指容器自己，反代永远连不上 api
        ("上游写死 localhost", NGINX_TMPL, PROXY_PASS, "proxy_pass http://127.0.0.1:8000;",
         TNX + ".TestSseDirectives.test_proxies_to_the_compose_service_not_localhost"),
        # 占位符写成小写：envsubst 会把它替换成（通常为空的）环境变量，配置静默变形
        ("占位符写成小写", NGINX_TMPL, SERVER_NAME_LINE, "server_name ${server_name};",
         TNX + ".TestTemplateIsRenderable.test_placeholder_names_are_uppercase_only"),
        # 模板没挂进容器：容器跑的是镜像自带 default.conf，仓库模板形同虚设
        ("模板没挂进容器", COMPOSE, TEMPLATE_MOUNT,
         "/tmp/not-the-repo:/etc/nginx/templates:ro",
         TNX + ".TestComposeWiring.test_templates_are_mounted_from_the_repo"),
        # 行尾策略被改：CRLF 模板进 Linux 容器，nginx 直接报错退出
        ("模板不钉 LF", GITATTRIBUTES, GITATTR_TEMPLATE_LF,
         "*.template text eol=crlf",
         TNX + ".TestLineEndings.test_gitattributes_forces_lf_for_nginx_config"),
    ],
    "deploy_script": [
        # ① 密码行读不到时**静默退出** —— 本组存在的起点，也是锚点打过一次错的教训。
        #    种回历史形态（顶层裸管道）：grep 返回 1 + pipefail + set -e，
        #    赋值语句直接终止脚本，连 [FAIL] 都不打印。
        #    所以这条用例不能只断言"失败了"，必须断言输出里**出现了变量名** ——
        #    「退出码非 0」对静默退出和明确报错是一回事，只有"说了什么"能区分。
        ("密码行缺失时静默退出", DS, PW_READ_CALL, PW_READ_CALL_OLD,
         TDS + ".DeployScriptTest.test_env_without_password_line_fails_loudly"),
        # ②③④ 密码挡位：空值 / 模板默认值 / 含 @（会把 DATABASE_URL 的主机名解析坏）。
        #    这三条都属于「拆掉以后脚本照样跑完并说成功」——功能上看不出任何异常，
        #    只有公网被别人扫到才知道。所以必须有人在看。
        ("空密码不再拦住", DS, PW_NOT_EMPTY, "true || die",
         TDS + ".DeployScriptTest.test_empty_password_is_rejected"),
        ("默认占位密码不再拦住", DS, PW_NOT_DEFAULT, "true || die",
         TDS + ".DeployScriptTest.test_placeholder_passwords_are_rejected"),
        ("含 @ 的密码不再拦住", DS, PW_NO_AT, "if false; then",
         TDS + ".DeployScriptTest.test_password_with_at_is_rejected_with_the_reason"),
        # ⑤ 少 .env 时只说「缺少 .env」——没给能照抄的下一步
        ("缺 .env 不给修法", DS, ENV_FILE_CHECK,
         r'''[[ -f .env ]] || die "缺少 .env"''',
         TDS + ".DeployScriptTest.test_missing_env_tells_you_to_copy_the_template"),
        # ⑥ 参数打错一个字母就当没看见 → 静默按「不带反代」部署，入口根本没起来
        ("参数打错不报错", DS, UNKNOWN_ARG, "    *) : ;;",
         TDS + ".DeployScriptTest.test_unknown_flag_is_rejected"),
        # ⑦ --no-pull 变成空操作：CI / 重复部署会去 git pull，在分支分叉时把服务器带偏
        ("--no-pull 不生效", DS, NO_PULL_FLAG, "    --no-pull) : ;;",
         TDS + ".DeployScriptTest.test_no_pull_skips_git_pull"),
        # ⑧ 起了反代却不把 api 收回回环 → 8000 仍对整个公网开着，反代那一层被绕过。
        #    关键不只是"检查存在"，而是**在 up 之前**拒绝：否则留下的是个看起来成功的半配置部署。
        ("--proxy 不检查 API_BIND", DS, BIND_LOOPBACK, "  if false; then",
         TDS + ".DeployScriptTest.test_proxy_without_loopback_bind_is_refused_before_starting_anything"),
        # ⑨ 起反代时忘了 --profile proxy：compose 里 proxy 是 profile 服务，默认不参与。
        #    脚本会「成功」，只是入口是 8000 而不是反代 —— 光看输出看不出来。
        ("起反代却不带 profile", DS, PROFILE_UP, "  docker compose up -d --build",
         TDS + ".DeployScriptTest.test_proxy_starts_the_profile_and_uses_the_proxy_port"),
        # ⑩ 打印的「最多 N 秒」与实现脱钩。历史版本正是如此：写死 90 次 × sleep 2s，
        #    对外宣称 180 秒，而每次探测本身还要另算，真实上界能到分钟级。
        ("就绪承诺与实现脱钩", DS, READY_PROMISE, READY_PROMISE_FIXED,
         TDS + ".DeployScriptTest.test_ready_poll_honours_the_timeout_knob"),
        # ⑪ 一直不就绪却照常打印「部署完成」——最贵的一种失败：人以为上线了
        ("不就绪也报成功", DS, READY_GUARD, "if false; then",
         TDS + ".DeployScriptTest.test_never_ready_fails_with_diagnostics"),
        # ⑫ 部署脚本里出现 compose down -v = 删数据卷。这是一条数据安全不变量，
        #    容器冒烟自带 PRE_EXISTING 保护**不会**发现，只有这条用例拦得住。
        ("部署脚本会删数据卷", DS, DEPLOY_DONE,
         "docker compose down -v\n" + DEPLOY_DONE,
         TDS + ".DeployScriptTest.test_never_deletes_the_data_volume"),
    ],
}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def run_group(name: str, cases: list) -> bool:
    files = sorted({c[1] for c in cases})
    original_bytes = {f: f.read_bytes() for f in files}
    # 统一成 LF 再用于匹配：工作区在 Windows 上是 CRLF，而锚点都按 \n 写。
    # 不归一化就会出现「锚点找不到」—— 好在下面 fail-closed，会中止而不是静默跳过。
    originals = {f: b.decode("utf-8").replace("\r\n", "\n") for f, b in original_bytes.items()}
    orig_shas = {f: sha(f) for f in files}

    missing = [
        "%s @ %s" % (label, f.relative_to(REPO))
        for label, f, old, _, _ in cases
        if old not in originals[f]
    ]
    if missing:
        print("[中止] %s：以下锚点找不到，本组不做任何修改：" % name)
        for m in missing:
            print("   -", m)
        return False

    import os

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for d in (REPO / "db" / "__pycache__", REPO / "tests" / "__pycache__",
              REPO / "api" / "routers" / "__pycache__"):
        if d.exists():
            shutil.rmtree(d)

    rows = []
    all_red = True
    try:
        for label, target, old, new, test in cases:
            patched = originals[target].replace(old, new, 1)
            assert patched != originals[target], label
            target.write_bytes(patched.encode("utf-8"))   # 按字节写，避免二次翻译
            proc = subprocess.run(
                [PY, "-m", "unittest", test],
                cwd=str(REPO), capture_output=True, text=True, errors="replace", env=env,
            )
            detail = ""
            for line in (proc.stderr or "").splitlines():
                s = line.strip()
                if s.startswith(("AssertionError", "FAIL:", "ERROR:")):
                    detail = s
                    break
            red = proc.returncode != 0
            all_red &= red
            rows.append((label, test.rsplit(".", 1)[-1], red, detail[:88]))
            target.write_bytes(original_bytes[target])
    finally:
        for f in files:
            f.write_bytes(original_bytes[f])

    print("=" * 100)
    print("反向对照 · 分组 %s" % name)
    print("=" * 100)
    print("%-22s %-56s %-6s %s" % ("缺陷", "用例", "变红", "报错首行"))
    print("-" * 100)
    for label, test, red, detail in rows:
        print("%-22s %-56s %-6s %s" % (label, test, "是" if red else "否 ← 问题", detail))
    print("-" * 100)
    restored = all(sha(f) == orig_shas[f] for f in files)
    print("文件已按字节还原 : %s" % restored)
    if not restored:
        print("  ⚠️ 还原校验没过！别当成行尾噪音 —— 上一次就是这么发现源文件被写成 \\r\\r\\n 的。")
    print()
    return all_red and restored


def main(argv):
    names = argv[1:] or list(GROUPS)
    unknown = [n for n in names if n not in GROUPS]
    if unknown:
        print("未知分组：%s（可选：%s）" % (unknown, ", ".join(GROUPS)))
        return 2
    ok = all(run_group(n, GROUPS[n]) for n in names)
    print("结论：%s" % ("全部分组 全部变红且文件已还原" if ok else "有分组未达标，见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
