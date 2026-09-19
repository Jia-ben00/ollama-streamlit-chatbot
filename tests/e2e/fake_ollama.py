"""假 Ollama 服务：为端到端联调提供「协议一致、输出确定」的模型服务替身。

为什么要有它：
1. CI / 别人的机器上不一定装了 Ollama，更不一定有模型权重（拉一个 3B 模型要几 GB）；
2. 真实模型每次输出都不一样，没法写断言；
3. 它可以**精确控制每块的间隔**（默认 50ms），用来验证「SSE 到底是不是真流式」。

协议按 Ollama 的真实行为实现：
- GET  /api/tags  -> {"models": [{"name": "llama3.2", ...}]}
- POST /api/chat  -> NDJSON，逐行 {"message": {"role": "assistant", "content": "..."}, "done": false}
                     最后一行 {"message": {"content": ""}, "done": true}

用法：
    python tests/e2e/fake_ollama.py            # 监听 127.0.0.1:11435
    FAKE_OLLAMA_PORT=11500 python tests/e2e/fake_ollama.py
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("FAKE_OLLAMA_PORT", "11435"))

# 固定输出：断言可以写死，不怕模型"发挥"。
CHUNKS = ["武汉", "今天", "多云", "，", "22 度", "，", "适合", "出门", "。"]
DELAY = float(os.getenv("FAKE_OLLAMA_DELAY", "0.05"))  # 每块之间的间隔（秒）


class Handler(BaseHTTPRequestHandler):
    # 用 HTTP/1.0 + 不带 Content-Length，靠关闭连接表示 body 结束。
    # 这样不需要手写 chunked 编码，客户端（requests）也能正常按流读取。
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        """静音访问日志，避免污染测试输出。"""

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            body = json.dumps({"models": [{"name": "llama3.2", "size": 2019393189}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.path.startswith("/api/chat"):
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()

        for c in CHUNKS:
            line = json.dumps(
                {
                    "model": payload.get("model", "llama3.2"),
                    "created_at": "2026-01-01T00:00:00Z",
                    "message": {"role": "assistant", "content": c},
                    "done": False,
                },
                ensure_ascii=False,
            )
            self.wfile.write((line + "\n").encode("utf-8"))
            self.wfile.flush()  # 关键：立刻 flush，模拟真实流式
            time.sleep(DELAY)

        final = json.dumps(
            {"model": "llama3.2", "message": {"role": "assistant", "content": ""}, "done": True},
            ensure_ascii=False,
        )
        self.wfile.write((final + "\n").encode("utf-8"))
        self.wfile.flush()


if __name__ == "__main__":
    print(f"fake ollama on http://127.0.0.1:{PORT} (每块间隔 {DELAY}s)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
