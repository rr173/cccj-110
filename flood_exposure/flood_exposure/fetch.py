"""远程大文件的「可恢复字节传输」。

供应商把水深 GeoTIFF 放在 HTTP 上，连接会频繁中断。本模块把一次下载变成
**可核对、可恢复、可追溯**的字节搬运，而不是“进度条到 100% 就算完成”：

* 首次探测对象总长度与**强** ETag；续传只接受请求 URL、最终 URL、强 ETag、
  总长度四项身份完全一致的本地进度；
* 每个已确认块记录区间与该区间内容的 SHA256。进程重启时重新读取临时文件
  里的真实块内容逐个核对，长度或摘要不对的块丢弃并重新获取；
* 每次分段请求都带 ``If-Match`` / ``If-Range`` 条件；206 响应的
  ``Content-Range`` 起止、总长度、``Content-Length`` 与真实读到的字节数
  全部核对；正文中途断开不会留下“已确认块”；
* 服务器忽略 Range 返回 200 时，绝不把整对象写进某个偏移，而是丢弃本轮
  分段进度、从 0 流式重传，并再次核对强 ETag / 长度 / 最终 URL；
* 416 只有在本地所有区间已按摘要校验且临时文件长度与对象总长度一致时
  才被解释为“已完成”，否则是协议错误；
* 只有全部区间完整、临时文件实际长度一致、整体 SHA256（若给出期望值）
  也匹配，才用 ``os.replace`` 原子替换目标；失败期间旧目标保持可用；
* 同一目标用非阻塞 ``flock`` 独占，后启动的进程立即报占用；进程异常退出
  时内核自动释放锁，新进程可以直接重新接管。

磁盘布局（与最终文件可区分）
------------------------------
    <target>                     最终文件（只在完整且摘要满足后替换）
    <target>.fetchpart           临时分块文件（未完成字节）
    <target>.fetchstate.json     进度记录（身份 + 已确认块区间/摘要）
    <target>.fetchlock           进程占用锁
    <target>.provenance.json     成功后的来源记录

本模块只搬字节，不改变任何洪水统计口径；下载后的本地文件与原有
``make-inventory`` / 分析入口完全兼容。
"""
from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
import re
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

from .hashing import sha256_file

PROGRESS_SCHEMA = "flood-exposure.fetch-progress/1"
SOURCE_SCHEMA = "flood-exposure.fetch-source/1"

PART_SUFFIX = ".fetchpart"
STATE_SUFFIX = ".fetchstate.json"
LOCK_SUFFIX = ".fetchlock"
SOURCE_SUFFIX = ".provenance.json"

USER_AGENT = "flood-exposure-fetch/1.0"
MAX_REDIRECTS = 10
READ_BLOCK = 64 * 1024
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

_CONTENT_RANGE_RE = re.compile(r"^\s*bytes\s+(\d+)-(\d+)/(\d+)\s*$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_STRONG_ETAG_RE = re.compile(r'^"[^"]*"$')
_SSL_CONTEXT = ssl.create_default_context()


# --------------------------------------------------------------------------- #
# 错误分类：可重试的只有断连与明确可重试的服务错误
# --------------------------------------------------------------------------- #
class FetchError(RuntimeError):
    """下载失败基类；消息必须讲清保留了什么、下次如何继续。"""

    def __init__(self, message: str, *, retained: list[Path] | None = None):
        super().__init__(message)
        self.retained = retained or []


class FetchLockedError(FetchError):
    """同一目标已被另一个进程占用。"""


class IdentityError(FetchError):
    """远端对象身份与本地进度不一致，版本已经无法沿用。"""


class ProtocolError(FetchError):
    """服务器响应违反协议（Content-Range/长度/416 等），绝不伪装成功。"""


class TransferError(FetchError):
    """传输中断、重试耗尽或正文不完整。"""


class RetentionError(FetchError):
    """本地保留的数据本身不可信（不是远端协议问题）。"""


class FetchVerificationError(FetchError):
    """整体摘要不符；目标未被替换。"""


class _RetryableTransfer(Exception):
    """内部信号：可重试的断连/服务错误。"""

    def __init__(self, message: str, *, whole: bool = False):
        super().__init__(message)
        self.whole = whole


# --------------------------------------------------------------------------- #
# 身份与断点数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RemoteIdentity:
    """一个远端对象的可核对身份。"""

    request_url: str
    final_url: str
    etag: str
    total_bytes: int

    def to_dict(self) -> dict:
        return {
            "request_url": self.request_url,
            "final_url": self.final_url,
            "etag": self.etag,
            "total_bytes": self.total_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RemoteIdentity":
        return cls(
            request_url=data["request_url"],
            final_url=data["final_url"],
            etag=data["etag"],
            total_bytes=int(data["total_bytes"]),
        )


@dataclass(frozen=True)
class ChunkRecord:
    """一个已确认块 [start, end]（含端点）及其内容 SHA256。"""

    start: int
    end: int
    sha256: str

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, data: dict) -> "ChunkRecord":
        return cls(int(data["start"]), int(data["end"]), str(data["sha256"]))


def _ensure_part_file(paths: "FetchPaths", total: int) -> None:
    """把临时文件调整为对象总长度（稀疏预分配）。

    磁盘上“文件长度 = 总长度”只是布局，**绝不**作为完成证据；完成判定
    仍必须依赖已校验块覆盖区间 + 逐块内容摘要 + 整体摘要。
    """
    if paths.part.exists():
        with open(paths.part, "r+b") as fh:
            if os.fstat(fh.fileno()).st_size != total:
                fh.truncate(total)
                fh.flush()
                os.fsync(fh.fileno())
    else:
        with open(paths.part, "w+b") as fh:
            fh.truncate(total)
            fh.flush()
            os.fsync(fh.fileno())


def _reset_part_file(paths: "FetchPaths", total: int) -> None:
    """丢弃全部已留字节并重建为空白稀疏文件（用于 --fresh / 整文件重传）。"""
    with open(paths.part, "w+b") as fh:
        fh.truncate(total)
        fh.flush()
        os.fsync(fh.fileno())


def chunk_sha256(path: Path, start: int, end: int,
                 read_block: int = READ_BLOCK) -> str:
    """只读取 [start,end] 计算摘要；内存占用与读块大小相关，不读整文件。"""
    h = hashlib.sha256()
    remaining = end - start + 1
    with open(path, "rb") as fh:
        fh.seek(start)
        while remaining > 0:
            n = min(read_block, remaining)
            data = fh.read(n)
            if not data:
                raise RetentionError(
                    f"临时文件在区间 {start}-{end} 内提前结束（还缺 {remaining} 字节）"
                )
            h.update(data)
            remaining -= len(data)
    return h.hexdigest()


def blocks_cover_range(records: list[ChunkRecord], start: int, end: int) -> bool:
    """[start,end]（含端点）是否被一组不重叠块完整覆盖。"""
    if end < start:
        return not records
    pos = start
    for rec in sorted(records, key=lambda r: (r.start, r.end)):
        if rec.end < pos:
            continue
        if rec.start > pos:
            return False
        pos = max(pos, rec.end + 1)
        if pos > end:
            return True
    return pos > end


def _sorted_unique(records: list[ChunkRecord]) -> list[ChunkRecord]:
    out: list[ChunkRecord] = []
    for rec in sorted(records, key=lambda r: (r.start, r.end)):
        if rec.length <= 0:
            continue
        if out and rec.start <= out[-1].end:
            continue
        out.append(rec)
    return out


def _missing_gaps(records: list[ChunkRecord], total: int,
                  block: int) -> list[tuple[int, int]]:
    """把未覆盖区间切成不超过 block 字节的闭区间请求。"""
    gaps: list[tuple[int, int]] = []
    pos = 0
    for rec in _sorted_unique(records):
        if rec.start > pos:
            gaps.append((pos, min(rec.start - 1, total - 1)))
        pos = max(pos, rec.end + 1)
    if pos < total:
        gaps.append((pos, total - 1))
    requests: list[tuple[int, int]] = []
    for start, end in gaps:
        cur = start
        while cur <= end:
            stop = min(cur + block - 1, end)
            requests.append((cur, stop))
            cur = stop + 1
    return requests


# --------------------------------------------------------------------------- #
# 本地路径与占用锁
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FetchPaths:
    target: Path

    @property
    def part(self) -> Path:
        return self.target.parent / (self.target.name + PART_SUFFIX)

    @property
    def state(self) -> Path:
        return self.target.parent / (self.target.name + STATE_SUFFIX)

    @property
    def lock(self) -> Path:
        return self.target.parent / (self.target.name + LOCK_SUFFIX)

    @property
    def source(self) -> Path:
        return self.target.parent / (self.target.name + SOURCE_SUFFIX)


class TargetLock:
    """同一目标的非阻塞独占锁。

    flock 锁由内核随文件描述符维护：进程崩溃或被 kill -9 时自动释放，因此
    异常退出后新进程无需手工删锁即可重新取得占用。
    """

    def __init__(self, paths: FetchPaths, wait: bool = False):
        self.paths = paths
        self.wait = wait
        self._fh = None

    def acquire(self) -> "TargetLock":
        self.paths.target.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.paths.lock, "a+b")
        try:
            if not self.wait:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except BlockingIOError:
            holder = ""
            try:
                fh.seek(0)
                holder = fh.read(200).decode("utf-8", "replace").strip()
            except OSError:
                pass
            fh.close()
            detail = f"（占用进程: {holder}）" if holder else ""
            raise FetchLockedError(
                f"目标 {self.paths.target} 已被另一个下载进程占用{detail}。"
                f"未改动任何进度文件；请等待该进程结束，或确认其已异常退出后重试。"
            ) from None
        except OSError as exc:
            fh.close()
            raise FetchLockedError(
                f"无法取得目标占用锁 {self.paths.lock}: {exc}"
            ) from exc
        self._fh = fh
        payload = f"pid={os.getpid()} at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
        fh.seek(0)
        fh.truncate()
        fh.write(payload.encode("utf-8"))
        fh.flush()
        return self

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                fh.seek(0)
                fh.truncate()
                fh.flush()
            except OSError:
                pass
            fh.close()

    def __enter__(self) -> "TargetLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


# --------------------------------------------------------------------------- #
# HTTP 基础
# --------------------------------------------------------------------------- #
def describe_url(url: str) -> str:
    """日志/来源记录用 URL：去掉 userinfo（凭据），其余保持原样。"""
    parts = urlsplit(url)
    if not (parts.username or parts.password):
        return url
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https: {describe_url(url)}")
    host = parts.hostname
    if not host:
        raise ValueError(f"URL 缺少主机名: {describe_url(url)}")
    return scheme, host, parts.port or (443 if scheme == "https" else 80)


def _request_target(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _connect(url: str, timeout: float) -> http.client.HTTPConnection:
    scheme, host, port = _origin(url)
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout,
                                           context=_SSL_CONTEXT)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _parse_content_range(value: str | None) -> tuple[int, int, int]:
    if not value:
        raise ProtocolError("206 响应缺少 Content-Range 头")
    m = _CONTENT_RANGE_RE.match(value)
    if not m:
        raise ProtocolError(f"无法解析的 Content-Range: {value!r}")
    start, end, total = (int(g) for g in m.groups())
    if start > end:
        raise ProtocolError(f"Content-Range 起止颠倒: {value!r}")
    if end >= total:
        raise ProtocolError(f"Content-Range 结束超过总长度: {value!r}")
    return start, end, total


def _parse_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        n = int(value)
    except ValueError as exc:
        raise ProtocolError(f"无效的 Content-Length: {value!r}") from exc
    if n < 0:
        raise ProtocolError(f"负的 Content-Length: {value!r}")
    return n


def _strong_etag(resp: http.client.HTTPResponse) -> str | None:
    etag = resp.getheader("ETag")
    if etag is None:
        return None
    etag = etag.strip()
    return etag if _STRONG_ETAG_RE.match(etag) else None


@dataclass
class _Response:
    """一条已经拿到响应的连接句柄。"""

    effective_url: str
    resp: http.client.HTTPResponse
    conn: http.client.HTTPConnection

    def close(self) -> None:
        try:
            self.resp.close()
        finally:
            try:
                self.conn.close()
            except Exception:
                pass


def _open(method: str, url: str, headers: dict,
          opts: "HttpOptions") -> _Response:
    """发请求并手工跟随重定向，返回最终 URL 与响应；连接不复用。"""
    redirects = 0
    while True:
        conn = _connect(url, opts.timeout)
        try:
            conn.request(method, _request_target(url),
                         headers={"User-Agent": USER_AGENT,
                                  "Connection": "close", **headers})
            resp = conn.getresponse()
        except Exception:
            conn.close()
            raise
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.getheader("Location")
            if not location:
                conn.close()
                raise ProtocolError(f"重定向 {resp.status} 缺少 Location 头")
            if redirects >= opts.max_redirects:
                conn.close()
                raise ProtocolError(f"重定向次数超过上限 {opts.max_redirects}")
            redirects += 1
            try:
                resp.read(READ_BLOCK)
            except Exception:
                pass
            conn.close()
            url = urljoin(url, location)
            opts.log(f"重定向 {resp.status} -> {describe_url(url)}")
            continue
        return _Response(url, resp, conn)


class _ReadInterrupted(Exception):
    """响应正文读取中途失败（断连、超时、IncompleteRead）。"""


def _write_bytes(fh, data: bytes, label: str) -> None:
    try:
        fh.write(data)
    except OSError as exc:
        raise TransferError(
            f"{label}: 本地写入临时文件失败: {exc}"
        ) from exc


def _read_body_into(resp: http.client.HTTPResponse, fh, length: int,
                    read_block: int, label: str, *,
                    check_no_extra: bool = False) -> int:
    """流式读取至多 length 字节；内存占用与 read_block 同阶。

    正文中途断连抛 :class:`_ReadInterrupted`（不留下已确认块）；超长抛
    :class:`ProtocolError`；提前干净地读到 EOF 返回实际字节数。
    """
    got = 0
    while got < length:
        n = min(read_block, length - got)
        try:
            data = resp.read(n)
        except http.client.IncompleteRead as exc:
            if exc.partial:
                _write_bytes(fh, exc.partial, label)
            raise _ReadInterrupted(
                f"读到 {got + len(exc.partial or b'')}/{length} 字节后连接中断"
            ) from exc
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            raise _ReadInterrupted(f"读到 {got}/{length} 字节后连接中断: {exc}") from exc
        if not data:
            # 连接干净地提前 EOF：短体不能当成功（重试有上限，最终明确失败）
            raise _ReadInterrupted(
                f"正文提前结束（只读到 {got}/{length} 字节）"
            )
        if got + len(data) > length:
            raise ProtocolError(
                f"{label}: 响应体超过声明的 {length} 字节")
        _write_bytes(fh, data, label)
        got += len(data)
    if not check_no_extra:
        return got
    # 声明的区间字节已读满：再确认连接上确实没有多余字节，否则是
    # “超长体”，绝不能默默截断后当作成功。仅在服务器给出了
    # Content-Length 时检查，避免向 keep-alive 连接多要一个字节而阻塞。
    try:
        extra = resp.read(1)
    except http.client.IncompleteRead as exc:
        if exc.partial:
            raise ProtocolError(
                f"{label}: 响应体超过声明的 {length} 字节")
        extra = b""
    except (OSError, http.client.HTTPException, TimeoutError):
        extra = b""
    if extra:
        raise ProtocolError(
            f"{label}: 响应体超过声明的 {length} 字节")
    return got


# --------------------------------------------------------------------------- #
# 探测远端身份
# --------------------------------------------------------------------------- #
def probe_remote(url: str, opts: "HttpOptions") -> RemoteIdentity:
    """探测总长度与强 ETag。缺失 ETag 或弱 ETag 一律拒绝作为续传依据。"""
    current = url
    while True:
        resp = _open("HEAD", current, {}, opts)
        try:
            status = resp.resp.status
            if status == 405 or (status == 200 and
                                 _parse_content_length(resp.resp.getheader("Content-Length")) is None):
                return _probe_via_range(url, opts)
            if status == 404:
                raise FetchError(f"远端对象不存在 (404): {describe_url(resp.effective_url)}")
            if status != 200:
                raise TransferError(
                    f"HEAD 探测失败: HTTP {status} {describe_url(resp.effective_url)}"
                )
            length = _parse_content_length(resp.resp.getheader("Content-Length"))
            if length is None:
                raise ProtocolError(
                    f"HEAD 响应缺少 Content-Length: {describe_url(resp.effective_url)}"
                )
            etag = _strong_etag(resp.resp)
            if etag is None:
                raise IdentityError(
                    "远端没有可用的强 ETag（缺失或为弱 ETag），无法把断点续传绑定到"
                    f"唯一对象版本: {describe_url(resp.effective_url)}。"
                    " 已拒绝在不安全的身份上继续。"
                )
            return RemoteIdentity(url, resp.effective_url, etag, length)
        finally:
            resp.close()


def _probe_via_range(url: str, opts: "HttpOptions") -> RemoteIdentity:
    """HEAD 不可用时的探测：Range 0-0 的 206，或忽略 Range 的 200 头。"""
    resp = _open("GET", url, {"Range": "bytes=0-0"}, opts)
    try:
        etag = _strong_etag(resp.resp)
        if etag is None:
            raise IdentityError(
                "远端没有可用的强 ETag，拒绝不安全的断点续传: "
                f"{describe_url(resp.effective_url)}"
            )
        if resp.resp.status == 206:
            start, end, total = _parse_content_range(
                resp.resp.getheader("Content-Range"))
            if (start, end) != (0, 0):
                raise ProtocolError(
                    f"探测 Content-Range 不是 0-0: "
                    f"{resp.resp.getheader('Content-Range')!r}"
                )
            declared = _parse_content_length(resp.resp.getheader("Content-Length"))
            if declared is not None and declared != 1:
                raise ProtocolError(
                    f"探测 Content-Length {declared} 与 0-0 区间不符")
            try:
                resp.resp.read(READ_BLOCK)
            except Exception:
                pass
            return RemoteIdentity(url, resp.effective_url, etag, total)
        if resp.resp.status == 200:
            length = _parse_content_length(resp.resp.getheader("Content-Length"))
            if length is None:
                raise ProtocolError("探测响应缺少 Content-Length")
            return RemoteIdentity(url, resp.effective_url, etag, length)
        raise TransferError(
            f"GET 探测失败: HTTP {resp.resp.status} {describe_url(resp.effective_url)}"
        )
    finally:
        resp.close()


def _identity_diffs(saved: RemoteIdentity, current: RemoteIdentity,
                    *,
                    compare_final_url: bool = True) -> list[str]:
    """逐项比较远端身份；返回人类可读差异。

    ``compare_final_url`` 由调用方按上下文选择：恢复前的探测与上次记录的
    最终 URL 比较；对 200 回退响应则同时核对实际响应是否来自同一落点。
    """
    diffs: list[str] = []
    if describe_url(current.request_url) != describe_url(saved.request_url):
        diffs.append(f"请求 URL: 记录={describe_url(saved.request_url)} "
                     f"当前={describe_url(current.request_url)}")
    if compare_final_url and describe_url(current.final_url) != describe_url(saved.final_url):
        diffs.append(f"最终 URL: 记录={describe_url(saved.final_url)} "
                     f"当前={describe_url(current.final_url)}")
    if current.etag != saved.etag:
        diffs.append(f"强 ETag: 记录={saved.etag} 当前={current.etag}")
    if current.total_bytes != saved.total_bytes:
        diffs.append(f"总长度: 记录={saved.total_bytes} 当前={current.total_bytes}")
    return diffs


# --------------------------------------------------------------------------- #
# 进度记录
# --------------------------------------------------------------------------- #
def load_progress(paths: FetchPaths) -> dict | None:
    if not paths.state.exists():
        return None
    try:
        with open(paths.state, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise RetentionError(
            f"进度记录 {paths.state} 无法解析: {exc}。本次未改动目标；"
            "可用 --fresh 放弃临时进度重新下载。"
        ) from exc
    if data.get("schema") != PROGRESS_SCHEMA:
        raise RetentionError(
            f"进度记录 {paths.state} 版本不受支持: {data.get('schema')!r}。"
            "请使用 --fresh 重新下载。"
        )
    ident = data.get("identity")
    if not isinstance(ident, dict) or set(ident) != {
            "request_url", "final_url", "etag", "total_bytes"}:
        raise RetentionError(
            f"进度记录 {paths.state} 缺少完整的对象身份；本次未改动目标。"
            "请使用 --fresh 重新下载。"
        )
    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        raise RetentionError(
            f"进度记录 {paths.state} 的块列表损坏；请使用 --fresh 重新下载。"
        )
    for i, c in enumerate(chunks):
        if not isinstance(c, dict) or set(c) != {"start", "end", "sha256"}:
            raise RetentionError(
                f"进度记录 {paths.state} 第 {i} 个块记录损坏；"
                "请使用 --fresh 重新下载。"
            )
    return data


def save_progress(paths: FetchPaths, progress: dict) -> None:
    tmp = paths.state.with_name(paths.state.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(progress, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, paths.state)


def _new_progress(identity: RemoteIdentity) -> dict:
    return {
        "schema": PROGRESS_SCHEMA,
        "identity": identity.to_dict(),
        "chunks": [],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _clear_partials(paths: FetchPaths, log: Callable[[str], None]) -> None:
    for path, label in ((paths.part, "临时分块文件"), (paths.state, "进度记录")):
        if path.exists():
            log(f"丢弃{label}: {path}")
            path.unlink()


def _verified_chunks(paths: FetchPaths, progress: dict,
                     log: Callable[[str], None]) -> tuple[list[ChunkRecord],
                                                           list[str]]:
    """按临时文件的实际内容核对记录；内容/长度不符的块不再算数。"""
    identity = RemoteIdentity.from_dict(progress["identity"])
    try:
        records = [ChunkRecord.from_dict(d) for d in progress.get("chunks", [])]
    except (KeyError, TypeError, ValueError) as exc:
        raise RetentionError(
            f"进度记录 {paths.state} 无法解析块列表: {exc}；请使用 --fresh 重新下载。"
        ) from exc
    if not records:
        return [], []
    if not paths.part.exists():
        log("临时分块文件不存在，已记录的块全部丢弃")
        return [], [f"临时文件缺失，丢弃 {len(records)} 个已记块"]
    size = paths.part.stat().st_size
    valid: list[ChunkRecord] = []
    dropped: list[str] = []
    for rec in records:
        reason = None
        if rec.length <= 0 or rec.start < 0 or rec.end >= identity.total_bytes:
            reason = "区间超出对象总长度"
        elif rec.end >= size:
            reason = f"临时文件长度 {size} 不覆盖区间 {rec.start}-{rec.end}"
        else:
            try:
                actual = chunk_sha256(paths.part, rec.start, rec.end)
            except RetentionError as exc:
                reason = str(exc)
            else:
                if actual != rec.sha256:
                    reason = (f"内容摘要不符 (记录={rec.sha256[:16]}… "
                              f"实际={actual[:16]}…)")
        if reason is None:
            valid.append(rec)
        else:
            dropped.append(f"块 {rec.start}-{rec.end}: {reason}")
            log(f"丢弃不可信块 {rec.start}-{rec.end}: {reason}")
    return valid, dropped


def _locally_complete(paths: FetchPaths, progress: dict, identity: RemoteIdentity,
                      log: Callable[[str], None]) -> bool:
    """416 的唯一可接受解释：所有区间已校验且临时文件长度一致。"""
    try:
        valid, dropped = _verified_chunks(paths, progress, log)
    except FetchError:
        return False
    if dropped:
        return False
    if not paths.part.exists() or paths.part.stat().st_size != identity.total_bytes:
        return False
    return blocks_cover_range(_sorted_unique(valid), 0, identity.total_bytes - 1)


# --------------------------------------------------------------------------- #
# 传输参数与结果
# --------------------------------------------------------------------------- #
def _is_retryable_http_error(text: str) -> bool:
    """只有 408/425/429/5xx 这类明确可重试的服务错误才允许重试。"""
    m = re.search(r"HTTP (\d{3})", text)
    return bool(m) and int(m.group(1)) in RETRYABLE_STATUS


@dataclass
class HttpOptions:
    timeout: float = 30.0
    max_retries: int = 4
    retry_backoff: float = 0.5
    max_redirects: int = MAX_REDIRECTS
    read_block: int = READ_BLOCK
    # 默认不等待：同一目标被占用时立即报错（演示/运维要求“快速报占用”）。
    # 仅当调用方明确愿意排队时才置 True。
    lock_wait: bool = False
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = lambda msg: None


@dataclass
class FetchResult:
    path: Path
    size_bytes: int
    sha256: str
    identity: RemoteIdentity
    source_record: Path
    resumed_chunks: int = 0
    refetched_chunks: int = 0
    whole_stream_restarts: int = 0
    retries: int = 0
    wrote_bytes: int = 0


class _RetryBudget:
    def __init__(self, opts: HttpOptions, paths: FetchPaths):
        self.opts = opts
        self.paths = paths
        self.used = 0

    def consume(self, exc: BaseException, *, what: str) -> None:
        if self.used >= self.opts.max_retries:
            raise TransferError(
                f"{what}: 重试已用尽 ({self.used}/{self.opts.max_retries}): {exc}。"
                f" 保留临时文件 {self.paths.part} 与进度 {self.paths.state}；"
                "稍后重跑同一命令会从已校验块继续，或加 --fresh 重新开始。",
                retained=[self.paths.part, self.paths.state],
            ) from exc
        self.used += 1
        delay = self.opts.retry_backoff * (2 ** (self.used - 1))
        self.opts.log(f"可重试错误（第 {self.used}/{self.opts.max_retries} 次重试，"
                      f"{delay:.2f}s 后）: {exc}")
        self.opts.sleep(delay)


# --------------------------------------------------------------------------- #
# 单段传输
# --------------------------------------------------------------------------- #
def _verify_response_identity(resp: _Response, identity: RemoteIdentity,
                             label: str, paths: FetchPaths | None = None) -> str:
    """核对最终 URL 与强 ETag；返回 ETag。"""
    keep = ""
    if paths is not None:
        keep = (f" 已保留临时文件 {paths.part} 与进度 {paths.state}，"
                f"旧目标 {paths.target} 未改动。")
    if describe_url(resp.effective_url) != describe_url(identity.final_url):
        raise IdentityError(
            f"{label}: 响应实际最终 URL {describe_url(resp.effective_url)} 与已确认的 "
            f"{describe_url(identity.final_url)} 不同；恢复下载不允许无提示地"
            "换成不同资源。" + keep + " 如要获取当前版本请加 --fresh。"
        )
    etag = _strong_etag(resp.resp)
    if etag is None:
        raise IdentityError(
            f"{label}: 响应缺少强 ETag（缺失或弱 ETag），无法确认仍是同一对象版本；"
            "对象版本已经无法沿用。" + keep + " 如要获取当前版本请加 --fresh。"
        )
    if etag != identity.etag:
        raise IdentityError(
            f"{label}: 响应强 ETag {etag} 与已确认的 {identity.etag} 不同；"
            "对象版本已经无法沿用，拒绝把新版本的余下字节接到旧片段上。"
            + keep + " 不允许无提示地换成不同资源；如要获取当前版本请加 --fresh。"
        )
    return etag


def _fetch_range_into_part(paths: FetchPaths, resp: _Response,
                           identity: RemoteIdentity, start: int, end: int,
                           opts: HttpOptions, label: str) -> ChunkRecord:
    """校验 206 头与正文，落盘到 [start,end]，返回按真实内容取摘要的块。"""
    _verify_response_identity(resp, identity, label, paths)
    rstart, rend, total = _parse_content_range(resp.resp.getheader("Content-Range"))
    if (rstart, rend) != (start, end):
        raise ProtocolError(
            f"{label}: Content-Range {rstart}-{rend} 与请求 {start}-{end} 不符。"
            f" 已保留 {paths.part} 与 {paths.state}。"
        )
    if total != identity.total_bytes:
        raise ProtocolError(
            f"{label}: Content-Range 总长度 {total} 与已确认的 "
            f"{identity.total_bytes} 不符。已保留 {paths.part} 与 {paths.state}。"
        )
    expected = end - start + 1
    declared = _parse_content_length(resp.resp.getheader("Content-Length"))
    if declared is not None and declared != expected:
        raise ProtocolError(
            f"{label}: Content-Length {declared} 与区间长度 {expected} 不符。"
            f" 已保留 {paths.part} 与 {paths.state}。"
        )
    with open(paths.part, "r+b") as fh:
        fh.seek(start)
        got = _read_body_into(resp.resp, fh, expected, opts.read_block, label,
                              check_no_extra=declared is not None)
        fh.flush()
        os.fsync(fh.fileno())
    if got != expected:
        raise _RetryableTransfer(
            f"{label}: 正文在区间内提前结束（读到 {got}/{expected} 字节）"
        )
    return ChunkRecord(start, end, chunk_sha256(paths.part, start, end,
                                                opts.read_block))


def _stream_whole_into_part(paths: FetchPaths, resp: _Response,
                            identity: RemoteIdentity, opts: HttpOptions) -> ChunkRecord:
    """处理 200：核对身份/长度后从 0 流式整文件重传。"""
    _verify_response_identity(resp, identity, "整文件重传", paths)
    declared = _parse_content_length(resp.resp.getheader("Content-Length"))
    if declared is not None and declared != identity.total_bytes:
        raise ProtocolError(
            f"整文件重传: Content-Length {declared} 与已确认总长度 "
            f"{identity.total_bytes} 不符；拒绝把整对象写到旧进度上。"
        )
    with open(paths.part, "w+b") as fh:
        # 从 0 覆盖写；先截到 0 再预分配，保证绝不会把旧版本的尾部字节留下
        fh.truncate(0)
        fh.truncate(identity.total_bytes)
        got = _read_body_into(resp.resp, fh, identity.total_bytes,
                              opts.read_block, "整文件重传",
                              check_no_extra=declared is not None)
        fh.flush()
        os.fsync(fh.fileno())
    if got != identity.total_bytes:
        raise _RetryableTransfer(
            f"整文件重传: 正文不完整（读到 {got}/{identity.total_bytes} 字节），"
            "该轮作废", whole=True
        )
    return ChunkRecord(0, identity.total_bytes - 1,
                       chunk_sha256(paths.part, 0, identity.total_bytes - 1,
                                    opts.read_block))


@dataclass
class _Outcome:
    kind: str           # "chunk" | "whole" | "complete"
    record: ChunkRecord | None = None


def _attempt_gap(paths: FetchPaths, identity: RemoteIdentity, progress: dict,
                 start: int, end: int, opts: HttpOptions, label: str) -> _Outcome:
    """一次分段尝试；可重试错误向上抛 ``_RetryableTransfer``。"""
    headers = {
        "Range": f"bytes={start}-{end}",
        # 每条分段请求都带条件：身份变化时不得静默拼接
        "If-Match": identity.etag,
        "If-Range": identity.etag,
    }
    try:
        resp = _open("GET", identity.request_url, headers, opts)
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise _RetryableTransfer(f"{label}: 连接失败: {exc}") from exc
    try:
        try:
            return _dispatch_gap_response(paths, resp, identity, progress,
                                          start, end, opts, label)
        except _ReadInterrupted as exc:
            raise _RetryableTransfer(f"{label}: {exc}") from exc
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            # 头已到、正文读一半断连等
            raise _RetryableTransfer(f"{label}: 传输中断: {exc}") from exc
    finally:
        resp.close()


def _dispatch_gap_response(paths: FetchPaths, resp: _Response,
                           identity: RemoteIdentity, progress: dict,
                           start: int, end: int, opts: HttpOptions,
                           label: str) -> _Outcome:
    status = resp.resp.status
    if status == 206:
        return _Outcome("chunk", _fetch_range_into_part(
            paths, resp, identity, start, end, opts, label))

    if status == 200:
        # 服务器忽略 Range。先核对整对象身份与实际长度，再决定是否重传：
        # 绝不把整对象写进某个偏移。
        _verify_response_identity(resp, identity, "整对象响应", paths)
        declared = _parse_content_length(resp.resp.getheader("Content-Length"))
        if declared is not None and declared != identity.total_bytes:
            raise ProtocolError(
                f"服务器忽略 Range 返回 200，但整对象长度 {declared} 与已确认的 "
                f"{identity.total_bytes} 不符；拒绝把整对象写到旧进度的位置。"
                f" 已保留 {paths.part} 与 {paths.state}。"
            )
        progress["chunks"] = []
        save_progress(paths, progress)
        opts.log("服务器忽略 Range（HTTP 200）：本轮分段进度作废，"
                 "转为从 0 流式整文件传输并再次核对身份与实际长度")
        # 身份/长度已在上面核对；直接从这条连接的字节 0 起流式写入临时文件，
        # 绝不写到请求区间对应的偏移上。
        return _Outcome("whole", _stream_whole_into_part(
            paths, resp, identity, opts))

    if status == 412:
        current = _probe_new_identity(paths, identity, opts)
        if _identity_diffs(identity, current, compare_final_url=False):
            _raise_identity_changed(paths, identity, current,
                                    why="服务器以 412 拒绝条件请求")
        raise TransferError(
            f"{label}: 服务器以 412 拒绝条件请求，但重新探测到的对象身份"
            f"（URL/强 ETag/总长度）与本地记录一致；这是无法重试的服务端"
            f"错误。已保留 {paths.part} 与 {paths.state}，旧目标未改动。"
            " 可稍后重跑同一命令继续。"
        )

    if status == 416:
        # 416 的唯一可接受解释：本地所有区间已逐块校验且长度一致。
        # 满足时进入最终核对（覆盖 / 长度 / 逐块 / 整体摘要），
        # 绝不把“进度记录说完成”本身当作完成。
        if _locally_complete(paths, progress, identity, opts.log):
            opts.log("416：本地全部区间已校验且长度一致，进入最终核对")
            return _Outcome("complete")
        raise ProtocolError(
            f"{label}: 服务器返回 416，但本地所有区间并未全部通过摘要校验，"
            f"或临时文件长度与对象总长度不一致；按协议错误处理。"
            f" 已保留 {paths.part} 与 {paths.state}。"
        )

    body = ""
    try:
        body = resp.resp.read(512).decode("utf-8", "replace").strip()
    except Exception:
        pass
    if status in RETRYABLE_STATUS:
        # 明确可重试的服务错误 -> 有限次数重试；其余 4xx/5xx 直接失败
        raise _RetryableTransfer(
            f"{label}: 服务端可重试错误 HTTP {status} {body[:160]}".rstrip())
    raise TransferError(
        f"{label}: HTTP {status} {describe_url(resp.effective_url)} {body[:160]}。"
        f" 已保留 {paths.part} 与 {paths.state}。"
    )


def _raise_identity_changed(paths: FetchPaths, saved: RemoteIdentity,
                            current: RemoteIdentity, *, why: str = "") -> None:
    diffs = _identity_diffs(saved, current)
    head = "远端对象版本已无法沿用"
    if why:
        head += f"（{why}）"
    raise IdentityError(
        head + "：\n  " + "\n  ".join(diffs)
        + f"\n本地进度 {paths.state} 与临时文件 {paths.part} 均未改动，"
        f"目标 {paths.target} 保持原样，不允许无提示地换成不同资源。\n"
        "如要获取当前版本，请加 --fresh 显式放弃旧进度。"
    )


def _probe_new_identity(paths: FetchPaths, identity: RemoteIdentity,
                        opts: HttpOptions) -> RemoteIdentity:
    """412 后重新探测当前身份；探测失败则明确说明进度不再可信。"""
    try:
        return probe_remote(identity.request_url, opts)
    except FetchError:
        raise IdentityError(
            "服务器以 412 拒绝条件请求，且无法重新探测当前远端身份；"
            "远端版本可能已变化，本地进度无法继续沿用。"
            f" 保留 {paths.part} 与 {paths.state}，旧目标未改动。"
            " 如需获取当前版本请加 --fresh。"
        ) from None


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def fetch(
    url: str,
    target: str | Path,
    chunk_bytes: int,
    *,
    expected_sha256: str | None = None,
    fresh: bool = False,
    opts: HttpOptions | None = None,
) -> FetchResult:
    """把 ``url`` 拉到 ``target``，支持可恢复的字节传输。

    成功时才替换目标（``os.replace``），失败期间旧目标保持可用；临时文件、
    进度记录、占用锁与来源记录均以独立后缀与最终文件区分。
    """
    target = Path(target)
    paths = FetchPaths(target)
    opts = opts or HttpOptions()
    log = opts.log
    if expected_sha256 is not None:
        expected_sha256 = expected_sha256.strip().lower()
        if not _SHA256_RE.match(expected_sha256):
            raise ValueError(f"expected_sha256 不是合法 SHA256: {expected_sha256!r}")
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes 必须为正数")
    if opts.read_block <= 0:
        raise ValueError("read_block 必须为正数")
    with TargetLock(paths, wait=opts.lock_wait):
        if fresh:
            _clear_partials(paths, log)
        # fresh 时不存在可核对进度：_fetch_locked 会重建空白临时文件，
        # 按当次探测到的远端版本从头开始。
        return _fetch_locked(url, paths, chunk_bytes, expected_sha256, fresh, opts)


def _probe_with_retry(url: str, opts: HttpOptions,
                     paths: FetchPaths) -> RemoteIdentity:
    """探测失败时：只对断连/可重试服务错误做有限重试，其余直接报错。

    探测发生在任何本地写入之前，因此失败的提示只说明“旧目标未改动”，
    不假装存在可续传的临时进度。
    """
    attempts = 0
    while True:
        try:
            return probe_remote(url, opts)
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            retryable, detail = True, exc
        except TransferError as exc:
            detail = exc
            retryable = _is_retryable_http_error(str(exc))
        except FetchError:
            raise
        if not retryable or attempts >= opts.max_retries:
            keep = (f"已保留临时文件 {paths.part} 与进度 {paths.state}；"
                    if paths.state.exists()
                    else "也没有可续传的临时进度；")
            raise TransferError(
                f"无法探测远端对象（已重试 {attempts}/{opts.max_retries} 次）: "
                f"{detail}。目标 {paths.target} 未被改动，{keep}"
                "请检查网络/URL 后重跑同一命令。",
                retained=[paths.target],
            ) from detail
        attempts += 1
        delay = opts.retry_backoff * (2 ** (attempts - 1))
        opts.log(f"探测时可重试错误（第 {attempts}/{opts.max_retries} 次，"
                 f"{delay:.2f}s 后）: {detail}")
        opts.sleep(delay)


def _fetch_locked(url: str, paths: FetchPaths, chunk_bytes: int,
                  expected_sha: str | None, fresh: bool,
                  opts: HttpOptions) -> FetchResult:
    log = opts.log
    paths.target.parent.mkdir(parents=True, exist_ok=True)

    identity = _probe_with_retry(url, opts, paths)
    log(f"探测对象: 总长度={identity.total_bytes} 强ETag={identity.etag} "
        f"最终URL={describe_url(identity.final_url)}")

    progress = None if fresh else load_progress(paths)
    resumed_chunks = 0
    refetched_chunks = 0
    if progress is None:
        # 没有可核对的进度记录：固定重建为空白稀疏临时文件，
        # 绝不把来历不明的旧字节当续传基础（fresh 也走这条）。
        if paths.part.exists() and not fresh:
            log(f"丢弃无进度记录的临时文件: {paths.part}")
        _reset_part_file(paths, identity.total_bytes)
        progress = _new_progress(identity)
        save_progress(paths, progress)
    else:
        saved = RemoteIdentity.from_dict(progress["identity"])
        diffs = _identity_diffs(saved, identity)
        if diffs:
            _raise_identity_changed(
                paths, saved, identity, why="恢复下载时检测到对象身份变化")
        # 先把临时文件恢复为对象总长度的稀疏文件（上次中断可能留下了
        # 更短的结尾），否则尾部缺失会被误认为“长度一致”。
        _ensure_part_file(paths, identity.total_bytes)
        valid, dropped = _verified_chunks(paths, progress, log)
        refetched_chunks = len(dropped)
        if dropped:
            progress["chunks"] = [c.to_dict() for c in _sorted_unique(valid)]
            save_progress(paths, progress)
        resumed_chunks = len(valid)
        log(f"恢复下载: 身份一致，校验通过 {resumed_chunks} 个块，"
            f"丢弃 {refetched_chunks} 个不可信块")

    _ensure_part_file(paths, identity.total_bytes)

    budget = _RetryBudget(opts, paths)
    whole_restarts = 0
    wrote_bytes = 0

    while True:
        records = _sorted_unique(
            [ChunkRecord.from_dict(d) for d in progress.get("chunks", [])])
        if identity.total_bytes == 0 or blocks_cover_range(
                records, 0, identity.total_bytes - 1):
            break
        gaps = _missing_gaps(records, identity.total_bytes, chunk_bytes)
        if not gaps:
            break
        # 取第一个缺口；遇到需要整文件重传时在该缺口内切换到整对象流式
        start, end = gaps[0]
        label = f"分段 {start}-{end}"
        cur_start, cur_end, cur_label = start, end, label
        while True:
            try:
                outcome = _attempt_gap(paths, identity, progress,
                                       cur_start, cur_end, opts, cur_label)
                break
            except _RetryableTransfer as exc:
                budget.consume(exc, what=cur_label)
                if exc.whole or cur_label == "整文件重传":
                    # 整文件重传必须每次都从 0 开始，不能续到半截对象上
                    cur_start = 0
                    cur_end = min(chunk_bytes - 1, identity.total_bytes - 1)
                    cur_label = "整文件重传"

        if outcome.kind == "complete":
            break
        if outcome.record is None:
            raise AssertionError("内部错误：成功结果缺少块记录")
        if outcome.kind == "whole":
            whole_restarts += 1
            progress["chunks"] = [outcome.record.to_dict()]
        else:
            progress["chunks"] = [c.to_dict() for c in
                                  _sorted_unique(records + [outcome.record])]
        save_progress(paths, progress)
        wrote_bytes += outcome.record.length

    # ---- 最终核对：区间覆盖、实际长度、逐块摘要、整体摘要 ------------------
    if identity.total_bytes > 0 and not blocks_cover_range(
            [ChunkRecord.from_dict(d) for d in progress.get("chunks", [])],
            0, identity.total_bytes - 1):
        raise TransferError(
            f"本地进度未覆盖全部区间 [0,{identity.total_bytes - 1}]；"
            f" 不能仅凭文件长度认定完成。保留 {paths.part} 与 {paths.state}。"
        )
    size = paths.part.stat().st_size
    if size != identity.total_bytes:
        raise TransferError(
            f"临时文件实际长度 {size} 与对象总长度 {identity.total_bytes} 不一致；"
            f" 不能仅凭进度记录认定完成。保留 {paths.part} 与 {paths.state}。"
        )
    valid, dropped = _verified_chunks(paths, progress, log)
    if dropped:
        progress["chunks"] = [c.to_dict() for c in _sorted_unique(valid)]
        save_progress(paths, progress)
        raise RetentionError(
            "临时文件中的部分块内容与记录不符，已丢弃这些块并保留进度；"
            " 重跑同一命令会重新获取缺失块。"
            f" 保留 {paths.part} 与 {paths.state}。"
        )

    actual_sha = sha256_file(paths.part)
    if expected_sha is not None and actual_sha != expected_sha:
        raise FetchVerificationError(
            f"整体 SHA256 不符: 期望 {expected_sha} 实际 {actual_sha}。\n"
            f"目标 {paths.target} 未被替换（旧文件保持可用）。\n"
            f"临时文件 {paths.part} 与进度 {paths.state} 已保留；"
            "更正 --expected-sha256 后可直接复用已校验块，"
            "若要按当前远端版本重新获取请加 --fresh。",
            retained=[paths.part, paths.state],
        )

    # 顺序：先写来源记录（临时名字），再原子替换目标，最后落地来源记录并
    # 清除进度记录。任何一步失败，旧目标都不会被半成品覆盖。
    _stage_source_record(paths, identity, size, actual_sha)
    os.replace(paths.part, paths.target)
    _commit_source_record(paths)
    if paths.state.exists():
        paths.state.unlink()
    log(f"下载完成并原子替换: {paths.target} ({size} 字节, sha256={actual_sha})")

    return FetchResult(
        path=paths.target,
        size_bytes=size,
        sha256=actual_sha,
        identity=identity,
        source_record=paths.source,
        resumed_chunks=resumed_chunks,
        refetched_chunks=refetched_chunks,
        whole_stream_restarts=whole_restarts,
        retries=budget.used,
        wrote_bytes=wrote_bytes,
    )


def _stage_source_record(paths: FetchPaths, identity: RemoteIdentity,
                         size: int, sha: str) -> None:
    record = {
        "schema": SOURCE_SCHEMA,
        "request_url": describe_url(identity.request_url),
        "final_url": describe_url(identity.final_url),
        "strong_etag": identity.etag,
        "total_bytes": size,
        "sha256": sha,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "仅记录字节来源；分析输入口径仍以 make-inventory 的 sha256 为准。",
    }
    tmp = paths.source.with_name(paths.source.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())


def _commit_source_record(paths: FetchPaths) -> None:
    tmp = paths.source.with_name(paths.source.name + ".tmp")
    os.replace(tmp, paths.source)
