"""敌对环境：一个「故意攒批」的替身反向代理。

用来回答一个平时验不了的问题：**如果中间那层把流攒成一坨再发，我们的验收脚本
能发现吗？** 本机没有 Docker / 没有 Nginx，而这个失败模式恰恰只在「中间多一层反代」
时才会出现——不做个替身，就只能等上了公网才发现，而且发现不了也不会报错
（功能是好的，只是「一个字一个字蹦」变成了「转圈等到最后出全文」）。

所以这里刻意复现 `proxy_buffering on` 的形态：

- **把上游整个响应读完再一次性发给客户端**（真正的原因）；
- 像 Nginx 一样**消费掉 `X-Accel-Buffering`**，不转发给下游——
  应用发的那个头是给反代看的，不会到浏览器手里。这条容易搞反：
  于是「公网侧断言这个头存在」本身就是个错判据，只有**到达时刻**靠得住；
- 不转发上游的 `Transfer-Encoding: chunked`，改用 `Content-Length`
  （攒完了才知道长度，这本身就是攒批的痕迹）。

用法：
    python tests/e2e/buffering_proxy.py                 # 监听 8100 -> 转发到 8000
    python tests/e2e/public_check.py --url http://127.0.0.1:8100   # 应当**红**在流式那条

对照实验（两条都要跑，缺一条就没有说服力）：
    直连  http://127.0.0.1:8000  -> 流式判定 INCREMENTAL
    经它  http://127.0.0.1:8100  -> 流式判定 BUFFERED
"""

import http.client
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

LISTEN_PORT = int(os.getenv("BUFFERING_PROXY_PORT", "8100"))
UPSTREAM = os.getenv("BUFFERING_PROXY_UPSTREAM", "http://127.0.0.1:8000")
_up = urlparse(UPSTREAM)
UP_HOST, UP_PORT = _up.hostname or "127.0.0.1", _up.port or 80

# 这些头要么由本层自己决定，要么是 Nginx 会消费掉的，都不能原样转发。
DROP_HEADERS = {
    "content-length", "transfer-encoding", "connection",
    "x-accel-buffering",     # ← Nginx 读它、用它，但不会传给客户端
    "server", "date",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 关掉默认的逐请求噪声
        pass

    def _proxy(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        fwd = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("host", "connection", "content-length", "accept-encoding")
        }
        fwd["Content-Length"] = str(len(body))

        t0 = time.monotonic()
        conn = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=60)
        try:
            conn.request(self.command, self.path, body=body, headers=fwd)
            resp = conn.getresponse()
            # ★ 就是这一行：把整个 body 读完（= 攒批）才继续往下走。
            #   上游每 50ms 推一块，在这里全被吞掉，客户端只能等到最后。
            payload = resp.read()
            status, up_headers = resp.status, resp.getheaders()
        finally:
            conn.close()
        buffered_for = time.monotonic() - t0

        self.send_response(status)
        for k, v in up_headers:
            if k.lower() not in DROP_HEADERS:
                self.send_header(k, v)
        self.send_header("X-Proxy", "buffering-stub")
        self.send_header("Content-Length", str(len(payload)))  # 攒完才知道长度
        self.end_headers()
        self.wfile.write(payload)

        if self.path.startswith("/chat"):
            print(f"[buffering-proxy] {self.command} {self.path}：攒了 {len(payload)} 字节、"
                  f"憋了 {buffered_for:.2f}s 之后一次性发出", flush=True)

    do_GET = _proxy
    do_POST = _proxy
    do_PATCH = _proxy
    do_DELETE = _proxy


def main() -> int:
    print(f"攒批替身反代：http://127.0.0.1:{LISTEN_PORT}  ->  {UPSTREAM}")
    print("（它会像 `proxy_buffering on` 一样把响应攒完再发，别拿它当生产配置）")
    ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
