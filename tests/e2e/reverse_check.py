"""反向对照：把缺陷「种回」源码，确认对应用例真的会红。

## 为什么需要它

「测试全绿」本身不能证明测试有效 —— 断言写松了、写成恒真条件，一样全绿。
唯一可靠的判据是：把缺陷种回去，看它会不会失败。

本脚本覆盖四组：

| 分组 | 被测对象 | 对应用例 |
|---|---|---|
| `chat_stream` | `api/routers/chat.py` 的流式协议（Content-Type / 防缓冲头 / SSE 空行分帧 / done 字段 / 404 校验 / 断连报错 / latency_ms 落库） | `tests/test_chat_stream.py` |
| `schema` | ORM 定义与快照（缺列 / 类型 / 枚举取值 / 可空性 / 索引 / 字符集声明 / 符号台账 / 快照少表） | `tests/test_schema_snapshot.py` |
| `container_smoke` | `.github/scripts/container_smoke.sh` 的端口断言等待逻辑 | `tests/test_container_smoke_script.py` |
| `stream_probe` | `src/stream_probe.py` 的「流式有没有退化成攒批」判据 | `tests/test_stream_probe.py` |

最后两组守的都不是业务代码，而是**判据本身**：一个量不出东西的尺子，
和一个量出「一切正常」的尺子长得一模一样。`stream_probe` 那把尤其值得守——
它错的时候部署是「能用的」，只是从「一个个蹦字」变成「转圈等到最后出全文」，
没有报警、没有异常，只有到达时刻能看出来。

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
