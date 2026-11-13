"""可控的本地 HTTP 服务：用于演示与测试「可恢复字节传输」。

刻意不用第三方服务或真实供应商账号。服务器支持：

* 强 ETag 与 ``Range``（含开放式区间），``If-Match`` / ``If-Range``；
* 302 重定向到实际对象路径（模拟供应商跳转到存储节点）；
* 可切换的故障行为：
    - ``ignore_range``      —— 无视 Range，直接 200 整个对象；
    - ``drop_range_at``     —— 第 N 个带 Range 的 GET 在正文某字节处断连；
    - ``forge_content_range`` —— 伪造成错误的 Content-Range；
    - ``short_body``        —— 206 头齐全但正文少发若干字节后关闭；
    - ``force_416``         —— 对所有带 Range 的 GET 返回 416；
    - ``weak_etag`` / ``omit_etag`` —— 弱 ETag 或干脆没有 ETag；
    - ``delay``             —— 每个响应前停顿，用于制造进程争用；
* 记录每个实际请求（方法、路径、Range、If-Match、状态码、发出字节数、
  是否截断），便于演示打印与断言。

对象按“路径 -> 当前版本(body, etag)”保存，``set_object`` 可在同一路径上
替换成同长度的新版本（ETag 随之改变），用来模拟断点处供应商换了版本。
"""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def strong_etag(body: bytes) -> str:
    """由内容派生的强 ETag（带引号，无 W/ 前缀）。"""
    return '"' + hashlib.sha256(body).hexdigest() + '"'


@dataclass
class ServedObject:
    body: bytes
    etag: str


@dataclass
class Faults:
    """当前生效的故障脚本（全部可通过服务实例属性改写）。"""

    ignore_range: bool = False
    force_416: bool = False
    drop_range_at: tuple[int, int] | None = None   # (第几个 Range GET, 正文多少字节后断)
    drop_count: int = 0                            # >0 表示还允许断几次
    forge_content_range: tuple[int, int, int] | None = None  # (start_delta, end_delta, total_delta)
    short_body: int | None = None                  # 206 少发这么多字节后正常关闭
    long_body: int | None = None                   # 206 声明并发送多出这么多字节
    fallback_200_wrong_length: int | None = None   # 200 整对象长度被改写
    weak_etag: bool = False
    omit_etag: bool = False
    delay: float = 0.0
    fail_status: int | None = None                 # 对所有 GET/HEAD 返回此状态
    swap_on_next_range: bytes | None = None        # 下一次 Range GET 前换掉对象版本
    head_unsupported: bool = False                 # 对 HEAD 返回 405（退回 GET 探测）
    reject_conditionals: bool = False              # 带 If-Match/If-Range 的 GET 一律 412（无版本变化）


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.objects: dict[str, ServedObject] = {}
        self.redirects: dict[str, str] = {}
        self.faults = Faults()
        self.requests: list[dict] = []
        self.range_get_count = 0
        self.drops_done = 0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ControllableSupplier/1.0"

    # 无需打印默认访问日志（演示自己打印请求记录）
    def log_message(self, fmt, *args):  # noqa: D401
        return

    # ------------------------------------------------------------------ #
    @property
    def state(self) -> _State:
        return self.server.state

    def _record(self, method: str, path: str, status: int, *, sent: int,
                content_range: str | None, truncated: bool) -> int:
        st = self.state
        with st.lock:
            st.requests.append({
                "id": len(st.requests) + 1,
                "method": method,
                "path": path,
                "range": self.headers.get("Range"),
                "if_match": self.headers.get("If-Match"),
                "if_range": self.headers.get("If-Range"),
                "status": status,
                "content_range": content_range,
                "bytes_sent": sent,
                "truncated": truncated,
                "at": time.strftime("%H:%M:%S", time.localtime()),
            })
            return len(st.requests)

    def _etag_for(self, obj: ServedObject) -> str | None:
        f = self.state.faults
        if f.omit_etag:
            return None
        if f.weak_etag:
            return "W/" + obj.etag
        return obj.etag

    def _resolve(self, path: str) -> tuple[str, ServedObject | None]:
        st = self.state
        seen = set()
        while path in st.redirects and path not in seen:
            seen.add(path)
            path = st.redirects[path]
        return path, st.objects.get(path)

    def _send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    # ------------------------------------------------------------------ #
    def do_HEAD(self) -> None:  # noqa: N802
        st = self.state
        with st.lock:
            f = st.faults
            if f.fail_status:
                self._record("HEAD", self.path, f.fail_status, sent=0,
                             content_range=None, truncated=False)
                self.send_response(f.fail_status)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return
            if f.delay:
                time.sleep(f.delay)
            if f.head_unsupported:
                self._record("HEAD", self.path, 405, sent=0,
                             content_range=None, truncated=False)
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.send_header("Allow", "GET")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return
            if self.path in st.redirects:
                self._record("HEAD", self.path, 302, sent=0,
                             content_range=None, truncated=False)
                self._send_redirect(st.redirects[self.path])
                return
            resolved, obj = self._resolve(self.path)
            if obj is None:
                self._record("HEAD", self.path, 404, sent=0,
                             content_range=None, truncated=False)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return
            etag = self._etag_for(obj)
            self._record("HEAD", resolved, 200, sent=0,
                         content_range=None, truncated=False)
            self.send_response(200)
            self.send_header("Content-Length", str(len(obj.body)))
            self.send_header("Accept-Ranges", "bytes")
            if etag is not None:
                self.send_header("ETag", etag)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        st = self.state
        with st.lock:
            f = st.faults
            is_range = self.headers.get("Range") is not None
            if is_range:
                st.range_get_count += 1
            rid = st.range_get_count if is_range else None

            if f.delay:
                time.sleep(f.delay)

            if (f.reject_conditionals
                    and (self.headers.get("If-Match") is not None
                         or self.headers.get("If-Range") is not None)):
                self._record("GET", self.path, 412, sent=0,
                             content_range=None, truncated=False)
                self.send_response(412)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return

            if f.fail_status:
                self._record("GET", self.path, f.fail_status, sent=0,
                             content_range=None, truncated=False)
                self.send_response(f.fail_status)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return

            if self.path in st.redirects:
                self._record("GET", self.path, 302, sent=0,
                             content_range=None, truncated=False)
                self._send_redirect(st.redirects[self.path])
                return

            resolved, obj = self._resolve(self.path)
            if obj is None:
                self._record("GET", self.path, 404, sent=0,
                             content_range=None, truncated=False)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return

            # 模拟“断点处供应商换了对象版本”：在本次 Range GET 置头之前替换
            if is_range and f.swap_on_next_range is not None:
                new_body = f.swap_on_next_range
                f.swap_on_next_range = None
                obj = ServedObject(new_body, strong_etag(new_body))
                st.objects[resolved] = obj
                obj = st.objects[resolved]

            etag = self._etag_for(obj)
            total = len(obj.body)

            # 条件请求：If-Match 不匹配 -> 412；If-Range 不匹配 -> 退回整对象
            # （ignore_range 模拟的是“笨节点”：它连同条件头一起忽略）
            if_match = self.headers.get("If-Match")
            if (if_match is not None and etag is not None
                    and if_match != etag and not f.ignore_range):
                self._record("GET", resolved, 412, sent=0,
                             content_range=None, truncated=False)
                self.send_response(412)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return

            if_range = self.headers.get("If-Range")
            honor_range = is_range
            if (if_range is not None and etag is not None
                    and if_range != etag and not f.ignore_range):
                honor_range = False
            if not honor_range:
                self._send_full(resolved, obj, etag, kind="full")
                return

            if f.force_416:
                self._record("GET", resolved, 416, sent=0,
                             content_range=f"bytes */{total}", truncated=False)
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return

            start_s, end_s = _parse_range(self.headers["Range"], total)
            if start_s is None:
                self._record("GET", resolved, 416, sent=0,
                             content_range=f"bytes */{total}", truncated=False)
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return

            start, end = start_s, end_s
            cr_start, cr_end, cr_total = start, end, total
            if f.forge_content_range is not None:
                ds, de, dt = f.forge_content_range
                cr_start, cr_end, cr_total = start + ds, end + de, total + dt

            if f.ignore_range:
                self._send_full(resolved, obj, etag, kind="ignored-range")
                return

            segment = obj.body[start:end + 1]
            length = len(segment)

            # 故障：短体（头正确，正文少发）
            short = f.short_body
            drop = None
            if f.drop_range_at is not None and f.drop_count > 0:
                nth, nbytes = f.drop_range_at
                if rid is not None and rid >= nth:
                    drop = min(nbytes, length - 1) if length > 1 else 0
            if drop is not None:
                body_len = drop
                f.drop_count -= 1
                st.drops_done += 1
            elif short is not None:
                body_len = max(0, length - short)
            else:
                body_len = length

            declared_len = length
            if f.long_body is not None and drop is None and short is None:
                declared_len = length + f.long_body

            sent_bytes = (declared_len if (f.long_body is not None
                                           and drop is None and short is None)
                          else body_len)
            content_range = f"bytes {cr_start}-{cr_end}/{cr_total}"
            self._record("GET", resolved, 206, sent=sent_bytes,
                         content_range=content_range,
                         truncated=(body_len != length))
            self.send_response(206)
            self.send_header("Content-Range", content_range)
            self.send_header("Content-Length", str(declared_len))
            self.send_header("Accept-Ranges", "bytes")
            if etag is not None:
                self.send_header("ETag", etag)
            self.send_header("Connection", "close")
            self.end_headers()
            if f.long_body is not None and drop is None and short is None:
                # 超长体：头与正文同时变长
                self.wfile.write(segment + b"X" * f.long_body)
            else:
                self.wfile.write(segment[:body_len])
            self.wfile.flush()
            self.close_connection = True
            if body_len < length:
                _abort_connection(self.connection)

    def _send_full(self, resolved: str, obj: ServedObject, etag: str | None,
                   *, kind: str) -> None:
        st = self.state
        body = obj.body
        self._record("GET", resolved, 200, sent=len(body),
                     content_range=None, truncated=False)
        self.send_response(200)
        declared = len(body)
        if st.faults.fallback_200_wrong_length is not None:
            declared = st.faults.fallback_200_wrong_length
        self.send_header("Content-Length", str(declared))
        self.send_header("Accept-Ranges", "bytes")
        if etag is not None:
            self.send_header("ETag", etag)
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True


def _parse_range(value: str, total: int) -> tuple[int | None, int | None]:
    if not value.startswith("bytes="):
        return None, None
    spec = value[len("bytes="):].split(",")[0].strip()
    if "-" not in spec:
        return None, None
    a, _, b = spec.partition("-")
    try:
        if a == "":
            n = int(b)
            if n <= 0:
                return None, None
            return max(total - n, 0), total - 1
        start = int(a)
        end = int(b) if b else total - 1
    except ValueError:
        return None, None
    if start < 0 or start >= total or end < start:
        return None, None
    return start, min(end, total - 1)


def _abort_connection(conn) -> None:
    """强行断开底层连接，让客户端看到正文未完成。"""
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


class ControllableServer:
    """在后台线程里运行的本地 HTTP 服务。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.state = _State()
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.state = self.state
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()

    @property
    def host(self) -> str:
        return self._httpd.server_address[0]

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    # ---- 对象与重定向 -------------------------------------------------
    def add_object(self, path: str, body: bytes) -> str:
        with self.state.lock:
            self.state.objects[path] = ServedObject(body, strong_etag(body))
            return self.state.objects[path].etag

    def set_object(self, path: str, body: bytes) -> str:
        return self.add_object(path, body)

    def object_body(self, path: str) -> bytes:
        with self.state.lock:
            return self.state.objects[path].body

    def add_redirect(self, path: str, target_path: str) -> None:
        with self.state.lock:
            self.state.redirects[path] = target_path

    # ---- 故障脚本 ------------------------------------------------------
    @property
    def faults(self) -> Faults:
        return self.state.faults

    def reset_faults(self) -> None:
        self.state.faults = Faults()

    def drop_range_once(self, nth_range_get: int, after_bytes: int) -> None:
        self.state.faults.drop_range_at = (nth_range_get, after_bytes)
        self.state.faults.drop_count = 1

    def clear_requests(self) -> None:
        with self.state.lock:
            self.state.requests.clear()

    def requests(self) -> list[dict]:
        with self.state.lock:
            return [dict(r) for r in self.state.requests]

    def summary(self) -> list[str]:
        lines = []
        for r in self.requests():
            rng = r["range"] or "-"
            lines.append(
                f"#{r['id']:>2} {r['method']:<4} {r['path']:<28} "
                f"Range={rng:<18} If-Match={r['if_match'] or '-'} "
                f"-> {r['status']} sent={r['bytes_sent']}"
                + (" [断连]" if r["truncated"] else "")
            )
        return lines

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "ControllableServer":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


if __name__ == "__main__":
    # 手工探索用：python demo/http_support_server.py <body-file> [port]
    import sys
    from pathlib import Path

    body_file = Path(sys.argv[1])
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    srv = ControllableServer(port=port)
    body = body_file.read_bytes()
    srv.add_object("/objects/demo.tif", body)
    print(json.dumps({"url": srv.url("/objects/demo.tif"),
                      "etag": strong_etag(body),
                      "port": srv.port}, ensure_ascii=False))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
