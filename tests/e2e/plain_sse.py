"""最朴素的 SSE 上游：靠「关闭连接」表示响应结束（不发 chunked）。

它只服务于一件事：给 `nginx_check.py` 当**反例**的靶子。

为什么要单独造一个，而不是直接拿真应用当反例：
真应用（uvicorn）用 `Transfer-Encoding: chunked` 分帧，而 nginx **对 chunked 响应
本来就不攒批** —— 实测开不开 `proxy_buffering`、带不带 `X-Accel-Buffering`，
客户端观测到的到达时刻都一样。也就是说，拿真应用当靶子，反例根本红不了，
而「红不了的反例」等于没有反例。

这个上游把分帧换成最朴素的一种（HTTP/1.1 + Connection: close，没有 Content-Length、
也没有 chunked），nginx 在 `proxy_buffering on` 下就会把整条响应攒完再发 ——
这才是那个经典的失败形态。

输出形态与真应用保持一致：9 块、每块间隔 50ms、text/event-stream。
"""

import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNKS = ["武汉", "今天", "多云", "，", "22 度", "，", "适合", "出门", "。"]
DELAY = float(os.getenv("PLAIN_SSE_DELAY", "0.05"))
HOST = os.getenv("PLAIN_SSE_HOST", "127.0.0.1")
PORT = int(os.getenv("PLAIN_SSE_PORT", "11436"))


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 但显式声明 Connection: close —— 响应结束由「连接关闭」表示。
    # 刻意**不**用 chunked：那正是这个文件要与真应用区分开的地方。
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        """静音访问日志，避免污染测试输出。"""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        # 刻意不发 X-Accel-Buffering：这是 nginx 默认会攒批的那个形态
        self.end_headers()

        for c in CHUNKS:
            self.wfile.write(f'data: {{"chunk": "{c}"}}\n\n'.encode("utf-8"))
            self.wfile.flush()
            time.sleep(DELAY)
        self.wfile.write(b'data: {"done": true, "latency_ms": 0}\n\n')
        self.wfile.flush()


if __name__ == "__main__":
    print(f"plain SSE (close-delimited) on http://{HOST}:{PORT}，每块间隔 {DELAY}s")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
