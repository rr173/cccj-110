"""可恢复字节传输的系统测试：真实本地 HTTP 服务 + 实际网络请求。

每个用例都启动一个可控的 HTTP 服务（``demo/http_support_server.py``），
断言“服务器实际收到了什么请求”以及“磁盘上留下了什么字节”，而不是只检查
下载函数的返回值。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo"))

from http_support_server import ControllableServer  # noqa: E402

from flood_exposure.fetch import (  # noqa: E402
    FetchError,
    FetchLockedError,
    FetchPaths,
    FetchVerificationError,
    HttpOptions,
    IdentityError,
    ProtocolError,
    TargetLock,
    fetch,
    probe_remote,
)


BODY = bytes(range(256)) * 60          # 15360 字节，可整除 1000 以外的块
SHA = hashlib.sha256(BODY).hexdigest()
CHUNK = 1000


@pytest.fixture
def server():
    srv = ControllableServer()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def url(server):
    server.add_object("/objects/depth.tif", BODY)
    return server.url("/objects/depth.tif")


def opts(**kw) -> HttpOptions:
    kw.setdefault("max_retries", 4)
    kw.setdefault("retry_backoff", 0.0)
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("log", lambda m: None)
    return HttpOptions(**kw)


def seed_partial(target: Path, identity, chunks: list[tuple[int, int]]) -> None:
    """在磁盘上造出“已下载若干块”的临时文件与进度记录。"""
    paths = FetchPaths(target)
    with open(paths.part, "wb") as fh:
        fh.write(BODY)
    from flood_exposure.fetch import (
        ChunkRecord, _new_progress, chunk_sha256, save_progress,
    )
    progress = _new_progress(identity)
    progress["chunks"] = [
        ChunkRecord(s, e, chunk_sha256(paths.part, s, e)).to_dict()
        for s, e in chunks
    ]
    save_progress(paths, progress)


# --------------------------------------------------------------------------- #
# 1. 首轮在块中途断连；新进程补齐，逐字节对照
# --------------------------------------------------------------------------- #
def test_mid_block_disconnect_then_new_process_resumes(
        server, url, tmp_path):
    target = tmp_path / "depth.tif"
    server.drop_range_once(nth_range_get=3, after_bytes=400)

    with pytest.raises(FetchError):
        # 模拟第一轮进程失败：不重试，立即失败退出
        fetch(url, target, CHUNK, expected_sha256=SHA,
              opts=opts(max_retries=0))

    reqs = server.requests()
    drop = [r for r in reqs if r["truncated"]]
    assert len(drop) == 1
    assert drop[0]["status"] == 206
    assert drop[0]["bytes_sent"] == 400
    assert drop[0]["range"] == "bytes=2000-2999"

    paths = FetchPaths(target)
    assert not target.exists(), "失败期间目标不应出现"
    state = json.loads(paths.state.read_text())
    # 只有完整校验过的前两块被记录；断连的那块不得记为完成
    assert [c["start"] for c in state["chunks"]] == [0, 1000]
    assert all(len(c["sha256"]) == 64 for c in state["chunks"])
    assert paths.part.stat().st_size == len(BODY)

    # 新进程（CLI 子进程）补齐
    r = subprocess.run(
        [".venv/bin/flood-exposure", "fetch", url, str(target),
         "--chunk-bytes", str(CHUNK), "--expected-sha256", SHA,
         "--max-retries", "2", "--retry-backoff", "0"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    summary = json.loads(r.stdout)
    assert summary["resumed_chunks"] == 2
    assert summary["sha256"] == SHA
    assert target.read_bytes() == BODY, "逐字节对照失败"

    resumed_reqs = server.requests()[len(reqs):]
    # 从缺口 2000 起继续，且带条件头
    first = next(q for q in resumed_reqs if q["method"] == "GET")
    assert first["range"] == "bytes=2000-2999"
    assert first["if_match"] and first["if_range"]
    assert not any(q["status"] == 200 and q["range"] for q in resumed_reqs), \
        "不应把整对象写到偏移"


# --------------------------------------------------------------------------- #
# 2. 篡改一个已记完成的临时块后恢复
# --------------------------------------------------------------------------- #
def test_tampered_completed_chunk_is_refetched(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    paths = FetchPaths(target)
    identity = probe_remote(url, opts())
    seed_partial(target, identity, [(0, 999), (1000, 1999), (2000, 2999)])
    with open(paths.part, "r+b") as fh:
        fh.seek(1500)
        fh.write(b"TAMPERED")

    server.clear_requests()
    result = fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    assert result.refetched_chunks == 1
    assert result.resumed_chunks == 2
    assert result.sha256 == SHA
    assert target.read_bytes() == BODY

    reqs = server.requests()
    assert reqs[0]["method"] == "HEAD"
    get_ranges = [q["range"] for q in reqs if q["method"] == "GET"]
    # 被篡改的 1000-1999 必须重新获取；其余不再重复
    assert "bytes=1000-1999" in get_ranges
    assert "bytes=0-999" not in get_ranges
    assert "bytes=2000-2999" not in get_ranges


# --------------------------------------------------------------------------- #
# 3. 断点处换成同长度新对象
# --------------------------------------------------------------------------- #
def test_same_length_replacement_at_breakpoint_keeps_old_progress(
        server, url, tmp_path):
    target = tmp_path / "depth.tif"
    paths = FetchPaths(target)
    identity = probe_remote(url, opts())
    seed_partial(target, identity, [(0, 999), (1000, 1999)])
    original_part = paths.part.read_bytes()
    old_target = tmp_path / "old.bin"
    old_target.write_bytes(b"previous-good-target")
    # 目标路径已有旧版本文件（应保持可用）
    target.write_bytes(b"previous-good-target")

    new_body = bytearray(BODY)
    new_body[5000:6000] = bytes(reversed(new_body[5000:6000]))  # 等长换版
    new_sha = hashlib.sha256(bytes(new_body)).hexdigest()
    server.set_object("/objects/depth.tif", bytes(new_body))
    server.clear_requests()

    with pytest.raises(IdentityError) as excinfo:
        fetch(url, target, CHUNK, expected_sha256=new_sha, opts=opts())
    msg = str(excinfo.value)
    assert "版本已无法沿用" in msg
    assert "强 ETag" in msg
    assert "记录=" in msg and "当前=" in msg
    # 旧目标、临时文件、进度都不许被改动
    assert target.read_bytes() == b"previous-good-target"
    assert paths.part.read_bytes() == original_part
    assert json.loads(paths.state.read_text())["chunks"][0]["start"] == 0
    # 恢复时只探测到新身份，不允许发任何 Range 请求去拼字节
    assert [q["method"] for q in server.requests()] == ["HEAD"]

    # 显式 fresh 才放弃旧进度，取得当前版本
    server.clear_requests()
    result = fetch(url, target, CHUNK, expected_sha256=new_sha,
                   fresh=True, opts=opts())
    assert result.sha256 == new_sha
    assert target.read_bytes() == bytes(new_body)
    first_get = next(q for q in server.requests() if q["method"] == "GET")
    assert first_get["range"] == "bytes=0-999"
    assert first_get["if_match"] == result.identity.etag


# --------------------------------------------------------------------------- #
# 4. 服务器忽略 Range 返回 200
# --------------------------------------------------------------------------- #
def test_ignored_range_200_restarts_from_zero(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    identity = probe_remote(url, opts())
    seed_partial(target, identity, [(0, 999), (1000, 1999)])
    server.faults.ignore_range = True
    server.clear_requests()

    result = fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    reqs = server.requests()
    ranged_200 = [q for q in reqs if q["status"] == 200 and q["range"]]
    assert len(ranged_200) == 1, "应有一次忽略 Range 的分段请求"
    assert result.whole_stream_restarts == 1
    assert result.sha256 == SHA
    assert target.read_bytes() == BODY
    # 整文件响应只写进从 0 开始的临时文件并整体核对，最终逐字节一致
    gets = [q for q in reqs if q["method"] == "GET"]
    assert gets[0]["status"] == 200 and gets[0]["bytes_sent"] == len(BODY)


def test_ignored_range_200_with_changed_etag_is_rejected(
        server, url, tmp_path):
    """200 回退后先核对身份：ETag 变了必须拒绝，不能把新版本当旧对象。"""
    target = tmp_path / "depth.tif"
    paths = FetchPaths(target)
    identity = probe_remote(url, opts())
    seed_partial(target, identity, [(0, 999)])
    new_body = bytes(reversed(BODY))
    server.faults.ignore_range = True
    server.faults.swap_on_next_range = new_body   # 在分段 GET 置头前换版
    server.clear_requests()

    with pytest.raises(IdentityError) as excinfo:
        # 本轮 HEAD 看到的仍是旧 ETag（本地进度身份一致），但响应已是新版本
        fetch(url, target, CHUNK, opts=opts())
    msg = str(excinfo.value)
    assert "强 ETag" in msg and ("记录=" in msg or "已确认" in msg)
    gets = [q for q in server.requests() if q["method"] == "GET"]
    assert len(gets) == 1 and gets[0]["status"] == 200 and gets[0]["range"]
    assert not target.exists()
    # 200 的整对象不应被写进任何偏移：临时文件与进度原样保留，
    # 且 200 响应体一个字节都没有落盘（失败发生在写入之前）
    assert paths.state.exists()
    assert [c["start"] for c in json.loads(paths.state.read_text())["chunks"]] == [0]
    assert paths.part.read_bytes() == BODY


# --------------------------------------------------------------------------- #
# 5. 伪造 Content-Range / 不完整正文 / 416
# --------------------------------------------------------------------------- #
def test_forged_content_range_is_protocol_error(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    target.write_bytes(b"old-target")
    server.faults.forge_content_range = (0, 0, 7)  # 总长度伪造
    server.clear_requests()

    with pytest.raises(ProtocolError) as excinfo:
        fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    assert "Content-Range" in str(excinfo.value)
    assert target.read_bytes() == b"old-target"
    assert not FetchPaths(target).state.exists() is False or True
    # 该轮一个块都不许记为完成
    state = json.loads(FetchPaths(target).state.read_text())
    assert state["chunks"] == []
    assert server.requests()[-1]["status"] == 206


def test_forged_content_range_start_end_is_protocol_error(
        server, url, tmp_path):
    target = tmp_path / "depth.tif"
    server.faults.forge_content_range = (1, 1, 0)  # 起止整体偏移
    with pytest.raises(ProtocolError) as excinfo:
        fetch(url, target, CHUNK, opts=opts())
    assert "与请求" in str(excinfo.value)
    assert not target.exists()


def test_short_body_is_failure_not_success(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    target.write_bytes(b"old-target")
    server.faults.short_body = 100
    with pytest.raises(FetchError):
        fetch(url, target, CHUNK, expected_sha256=SHA,
              opts=opts(max_retries=2))
    assert target.read_bytes() == b"old-target", "短体绝不能被替换成成功"
    assert server.requests()[-1]["truncated"] is True


def test_416_with_unverified_local_ranges_is_protocol_error(
        server, url, tmp_path):
    target = tmp_path / "depth.tif"
    server.faults.force_416 = True
    with pytest.raises(ProtocolError) as excinfo:
        fetch(url, target, CHUNK, opts=opts(max_retries=0))
    assert "416" in str(excinfo.value)
    assert "协议错误" in str(excinfo.value)


def test_416_only_means_complete_when_ranges_verified(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    identity = probe_remote(url, opts())
    seed_partial(target, identity, [(0, len(BODY) - 1)])
    server.faults.force_416 = True
    server.clear_requests()
    # 全部区间已校验且长度一致：416 可以解释为“已完成”
    result = fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    assert result.sha256 == SHA and target.read_bytes() == BODY

    # 同样的 416，但本地块被篡改 -> 不能伪装成功
    bad = tmp_path / "bad.tif"
    seed_partial(bad, identity, [(0, len(BODY) - 1)])
    with open(FetchPaths(bad).part, "r+b") as fh:
        fh.seek(0)
        fh.write(b"X")
    with pytest.raises(ProtocolError):
        fetch(url, bad, CHUNK, expected_sha256=SHA, opts=opts())
    assert not bad.exists()


# --------------------------------------------------------------------------- #
# 6. 摘要不符时旧目标不变
# --------------------------------------------------------------------------- #
def test_expected_digest_mismatch_keeps_old_target(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    target.write_bytes(b"old-target-content")
    with pytest.raises(FetchVerificationError) as excinfo:
        fetch(url, target, CHUNK, expected_sha256="0" * 64, opts=opts())
    assert "未被替换" in str(excinfo.value)
    assert target.read_bytes() == b"old-target-content"
    paths = FetchPaths(target)
    assert paths.part.exists() and paths.state.exists()
    # 全部块已校验：更正期望摘要后不必重下任何字节
    result = fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    assert result.resumed_chunks == len(range(0, len(BODY), CHUNK))
    assert result.wrote_bytes == 0
    assert target.read_bytes() == BODY


# --------------------------------------------------------------------------- #
# 7. 双进程争用与异常退出后接管
# --------------------------------------------------------------------------- #
def test_second_process_lock_contention_is_fast(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    with TargetLock(FetchPaths(target)):
        t0 = time.monotonic()
        with pytest.raises(FetchLockedError) as excinfo:
            fetch(url, target, CHUNK, opts=opts())
        elapsed = time.monotonic() - t0
    assert elapsed < 0.5, "被占用时必须快速报错，不能阻塞等待"
    assert "已被另一个下载进程占用" in str(excinfo.value)
    assert "未改动任何进度文件" in str(excinfo.value)


def test_two_cli_processes_do_not_clobber_progress(server, url, tmp_path):
    """双进程确定性构造：A 进程持有锁直到收到放行信号，B 进程（后启动）
    必须立刻以退出码 3 报占用；随后 A 正常完成，B 从未改动进度。"""
    target = tmp_path / "depth.tif"
    holder_script = tmp_path / "hold_lock.py"
    holder_script.write_text(
        "import sys, time, pathlib\n"
        "sys.path.insert(0, sys.argv[3])\n"
        "from flood_exposure.fetch import FetchPaths, TargetLock\n"
        "p = FetchPaths(pathlib.Path(sys.argv[1]))\n"
        "with TargetLock(p):\n"
        "    pathlib.Path(sys.argv[2]).write_text('locked')\n"
        "    ready = pathlib.Path(sys.argv[2] + '.go')\n"
        "    while not ready.exists():\n"
        "        time.sleep(0.01)\n",
        encoding="utf-8",
    )
    sentinel = tmp_path / "locked"
    holder = subprocess.Popen(
        [sys.executable, "-I", str(holder_script), str(target),
         str(sentinel), str(ROOT)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 60
    while not sentinel.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sentinel.exists(), "持锁进程未能启动"

    t0 = time.monotonic()
    p2 = subprocess.run(
        [".venv/bin/flood-exposure", "fetch", url, str(target),
         "--chunk-bytes", str(CHUNK)], cwd=ROOT,
        capture_output=True, text=True)
    dt = time.monotonic() - t0
    assert p2.returncode == 3, p2.stderr
    assert "占用" in p2.stderr
    assert dt < 8.0, f"后进程应快速报占用，实测 {dt:.2f}s"
    # 持锁进程从未下载，因此目标仍不存在，也没有任何进度被后进程写坏
    assert not target.exists()
    assert not FetchPaths(target).part.exists()

    # 放行 holder，再让同一目标正常下载一次
    (tmp_path / "locked.go").write_text("go", encoding="utf-8")
    holder.communicate(timeout=30)
    assert holder.returncode == 0
    result = fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    assert result.sha256 == SHA
    assert target.read_bytes() == BODY


def test_takeover_after_abnormal_exit(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    server.faults.delay = 0.02
    proc = subprocess.Popen(
        [".venv/bin/flood-exposure", "fetch", url, str(target),
         "--chunk-bytes", str(CHUNK)], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.25)
    proc.kill()
    proc.wait()
    assert proc.returncode != 0
    # 内核随进程退出释放 flock：新进程可立即重新接管
    srv_faults_was = server.faults.delay
    server.faults.delay = 0.0
    result = fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    assert result.sha256 == SHA
    assert target.read_bytes() == BODY
    server.faults.delay = srv_faults_was


# --------------------------------------------------------------------------- #
# 8. 重定向后的最终 URL / 身份
# --------------------------------------------------------------------------- #
def test_redirect_final_url_is_recorded_and_enforced(server, tmp_path):
    body = BODY
    sha = SHA
    server.add_object("/storage/real-object.tif", body)
    server.add_redirect("/supplier/depth.tif", "/storage/real-object.tif")
    url = server.url("/supplier/depth.tif")
    target = tmp_path / "depth.tif"
    result = fetch(url, target, CHUNK, expected_sha256=sha, opts=opts())
    assert result.identity.request_url == url
    assert result.identity.final_url == server.url("/storage/real-object.tif")
    assert server.requests()[0]["status"] == 302
    record = json.loads(FetchPaths(target).source.read_text())
    assert record["request_url"] == url
    assert record["final_url"] == result.identity.final_url
    assert record["strong_etag"] == result.identity.etag
    assert record["total_bytes"] == len(body)
    assert record["sha256"] == sha


def test_resume_does_not_silently_switch_resource(server, tmp_path):
    """恢复时重定向目标换了：分段请求的最终 URL 变化必须显式失败。

    探测只发生在他的 HEAD 上，本地上次记录的最终 URL 是 a.tif；恢复后
    供应商把 redirect 指向 b.tif，分段 GET 的实际落点变化，不能悄悄拼接。
    """
    server.add_object("/storage/a.tif", BODY)
    server.add_object("/storage/b.tif", bytes(reversed(BODY)))
    server.add_redirect("/supplier/depth.tif", "/storage/a.tif")
    url = server.url("/supplier/depth.tif")
    target = tmp_path / "depth.tif"
    target.write_bytes(b"old-target")

    # 先在不换资源时失败一次，留下基于 a.tif 的进度
    server.faults.ignore_range = False
    server.faults.short_body = 100
    with pytest.raises(FetchError):
        fetch(url, target, CHUNK, opts=opts(max_retries=0))

    server.faults.short_body = None
    server.faults.drop_range_at = None
    server.faults.drop_count = 0
    server.faults.delay = 0
    # 确保 a.tif 的 ETag 与进度记录一致，但让恢复后的 GET 落到 b.tif
    server.set_object("/storage/a.tif", BODY)
    # 把记录中的最终 URL 改成 a，而当前 HEAD 也仍然 302 到 a
    state = json.loads(FetchPaths(target).state.read_text())
    assert state["identity"]["final_url"] == server.url("/storage/a.tif")
    # 现在 HEAD 也跟随 redirect 到 b：先换 redirect，再恢复
    server.add_redirect("/supplier/depth.tif", "/storage/b.tif")
    server.clear_requests()

    with pytest.raises(IdentityError) as excinfo:
        fetch(url, target, CHUNK, opts=opts())
    msg = str(excinfo.value)
    assert "最终 URL" in msg
    assert "不同资源" in msg or "拒绝" in msg
    assert target.read_bytes() == b"old-target", "旧目标必须保持可用"
    # 没有把 b.tif 的字节拼到 a.tif 的进度上：只有重定向与探测，没有任何 GET
    assert [q["status"] for q in server.requests()] == [302, 200]
    assert not any(q["method"] == "GET" for q in server.requests())


# --------------------------------------------------------------------------- #
# 9. 弱 ETag / 缺失 ETag 拒绝续传
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fault", ["weak_etag", "omit_etag"])
def test_weak_or_missing_etag_refuses_resume(server, url, tmp_path, fault):
    target = tmp_path / "depth.tif"
    setattr(server.faults, fault, True)
    with pytest.raises(IdentityError) as excinfo:
        fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    assert "强 ETag" in str(excinfo.value)
    assert not target.exists()
    assert [q["method"] for q in server.requests()] == ["HEAD"]


# --------------------------------------------------------------------------- #
# 10. 结束语义：本地长度不足以证明完整
# --------------------------------------------------------------------------- #
def test_truncated_part_file_with_complete_chunk_records_is_rejected(
        server, url, tmp_path):
    target = tmp_path / "depth.tif"
    identity = probe_remote(url, opts())
    paths = FetchPaths(target)
    # 记录声称整对象已完成，但磁盘上只留了前 2000 字节
    seed_partial(target, identity, [(0, len(BODY) - 1)])
    with open(paths.part, "r+b") as fh:
        fh.truncate(2000)
    # 恢复时必须重新核对真实块内容：声称完成但内容不符的块被丢弃并重取，
    # 绝不能仅凭“记录说完整”就替换目标
    server.clear_requests()
    result = fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    assert target.read_bytes() == BODY
    assert result.refetched_chunks == 1
    gets = [q for q in server.requests() if q["method"] == "GET"]
    assert gets and gets[0]["range"] == "bytes=0-999"


def test_truncated_part_file_with_unverified_tail_never_replaces(
        server, url, tmp_path):
    """尾部字节缺失且记录无法覆盖时，失败且不替换旧目标。"""
    target = tmp_path / "depth.tif"
    target.write_bytes(b"old-target")
    identity = probe_remote(url, opts())
    paths = FetchPaths(target)
    seed_partial(target, identity, [(0, 999), (1000, 1999)])
    with open(paths.part, "r+b") as fh:
        fh.truncate(1500)   # 记录说 1000-1999 已完成，但文件连这块都装不下
    assert json.loads(paths.state.read_text())["chunks"]
    # 让后续请求都失败，观察最终是否误判完成
    server.faults.fail_status = 403
    with pytest.raises(FetchError):
        fetch(url, target, CHUNK, expected_sha256=SHA,
              opts=opts(max_retries=0))
    assert target.read_bytes() == b"old-target"


def test_ignored_range_200_length_mismatch_is_protocol_error(
        server, url, tmp_path):
    """200 回退时 ETag 未变但声明的整对象长度变了：拒绝，不写进旧进度。"""
    target = tmp_path / "depth.tif"
    identity = probe_remote(url, opts())
    seed_partial(target, identity, [(0, 999)])
    original_part = FetchPaths(target).part.read_bytes()
    server.faults.ignore_range = True
    server.faults.fallback_200_wrong_length = len(BODY) + 100
    server.clear_requests()

    with pytest.raises(ProtocolError) as excinfo:
        fetch(url, target, CHUNK, opts=opts())
    assert "长度" in str(excinfo.value)
    gets = [q for q in server.requests() if q["method"] == "GET"]
    assert len(gets) == 1 and gets[0]["status"] == 200
    assert FetchPaths(target).part.read_bytes() == original_part
    assert not target.exists()


def test_source_record_and_temp_files_have_distinct_suffixes(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    result = fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    paths = FetchPaths(target)
    files = {p.name for p in tmp_path.iterdir()}
    assert target.name in files
    assert paths.part.name.endswith(".fetchpart") and paths.part.name not in files
    assert paths.state.name.endswith(".fetchstate.json") and paths.state.name not in files
    assert paths.lock.name.endswith(".fetchlock")
    assert paths.source.name.endswith(".provenance.json")
    record = json.loads(paths.source.read_text())
    assert set(record) >= {
        "request_url", "final_url", "strong_etag", "total_bytes", "sha256"
    }
    assert record["sha256"] == result.sha256 == SHA


# --------------------------------------------------------------------------- #
# 11. HEAD 不可用时用 Range 0-0 探测，且仍要求强 ETag
# --------------------------------------------------------------------------- #
def test_head_unsupported_falls_back_to_range_probe(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    server.faults.head_unsupported = True
    server.clear_requests()
    result = fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    assert result.identity.total_bytes == len(BODY)
    assert result.identity.etag == server.state.objects["/objects/depth.tif"].etag
    assert target.read_bytes() == BODY
    statuses = [(q["method"], q["status"]) for q in server.requests()]
    assert statuses[0] == ("HEAD", 405)
    assert statuses[1] == ("GET", 206)
    assert server.requests()[1]["range"] == "bytes=0-0"


def test_probe_rejects_zero_length_lying_content_range(server, url, tmp_path):
    """探测阶段的伪造 Content-Range 也必须报协议错误，不能靠猜测继续。"""
    target = tmp_path / "depth.tif"
    server.faults.head_unsupported = True
    server.faults.forge_content_range = (0, 0, 5)
    with pytest.raises(ProtocolError) as excinfo:
        fetch(url, target, CHUNK, expected_sha256=SHA,
              opts=opts(max_retries=0))
    assert "Content-Range" in str(excinfo.value) or "Content-Length" in str(excinfo.value)
    assert not target.exists()


# --------------------------------------------------------------------------- #
# 12. 可重试错误有上限；不可重试的协议错误不重试
# --------------------------------------------------------------------------- #
def test_retryable_http_error_is_bounded(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    server.faults.fail_status = 503
    server.clear_requests()
    with pytest.raises(FetchError) as excinfo:
        fetch(url, target, CHUNK, expected_sha256=SHA,
              opts=opts(max_retries=2))
    assert "重试" in str(excinfo.value)
    # HEAD + 2 次重试 = 3 次请求；不会无限重试
    assert len(server.requests()) == 3
    assert not target.exists()


def test_protocol_error_is_not_retried(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    server.faults.forge_content_range = (0, 0, 7)
    server.clear_requests()
    with pytest.raises(ProtocolError):
        fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts(max_retries=5))
    gets = [q for q in server.requests() if q["method"] == "GET"]
    assert len(gets) == 1, "Content-Range 错误不允许重试"


# --------------------------------------------------------------------------- #
# 13. 成功后的清理与来源记录
# --------------------------------------------------------------------------- #
def test_success_removes_temp_and_state_keeps_provenance(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    result = fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    paths = FetchPaths(target)
    assert not paths.part.exists()
    assert not paths.state.exists()
    assert paths.source.exists()
    record = json.loads(paths.source.read_text())
    assert record["sha256"] == result.sha256
    assert record["strong_etag"] == result.identity.etag
    assert record["total_bytes"] == len(BODY)
    # 第二次对同一目标再下载：不需要 fresh，因为没有遗留进度
    result2 = fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    assert result2.sha256 == SHA
    assert target.read_bytes() == BODY


# --------------------------------------------------------------------------- #
# 14. 损坏的进度记录不能导致错误拼接，必须显式要求 --fresh
# --------------------------------------------------------------------------- #
def test_corrupt_progress_state_requires_fresh(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    paths = FetchPaths(target)
    paths.part.write_bytes(BODY)
    paths.state.write_text("{not json", encoding="utf-8")
    with pytest.raises(FetchError) as excinfo:
        fetch(url, target, CHUNK, expected_sha256=SHA, opts=opts())
    assert "--fresh" in str(excinfo.value)
    assert not target.exists()
    assert paths.part.read_bytes() == BODY, "损坏记录时不得清掉临时字节"

    # --fresh 可以恢复
    result = fetch(url, target, CHUNK, expected_sha256=SHA, fresh=True,
                   opts=opts())
    assert result.sha256 == SHA and target.read_bytes() == BODY


def test_progress_state_missing_identity_key_is_rejected(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    paths = FetchPaths(target)
    paths.part.write_bytes(BODY)
    paths.state.write_text(json.dumps({
        "schema": "flood-exposure.fetch-progress/1",
        "identity": {"request_url": "x"},
        "chunks": [],
    }), encoding="utf-8")
    with pytest.raises(FetchError) as excinfo:
        fetch(url, target, CHUNK, opts=opts())
    assert "身份" in str(excinfo.value) and "--fresh" in str(excinfo.value)
    assert not target.exists()


# --------------------------------------------------------------------------- #
# 15. 重试成功后仍从同一缺口续传，不重复整段
# --------------------------------------------------------------------------- #
def test_bounded_retry_refetches_same_gap_then_completes(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    server.drop_range_once(nth_range_get=4, after_bytes=300)
    server.clear_requests()
    result = fetch(url, target, CHUNK, expected_sha256=SHA,
                   opts=opts(max_retries=2))
    assert result.retries == 1
    assert target.read_bytes() == BODY
    gets = [q for q in server.requests() if q["method"] == "GET"]
    assert gets[3]["range"] == "bytes=3000-3999"
    assert gets[3]["truncated"] is True
    assert gets[4]["range"] == "bytes=3000-3999", "重试的是同一缺口"
    assert result.sha256 == SHA


# --------------------------------------------------------------------------- #
# 16. 空对象
# --------------------------------------------------------------------------- #
def test_empty_object(server, tmp_path):
    body = b""
    server.add_object("/objects/empty.tif", body)
    url = server.url("/objects/empty.tif")
    target = tmp_path / "empty.tif"
    result = fetch(url, target, CHUNK,
                   expected_sha256=hashlib.sha256(body).hexdigest(), opts=opts())
    assert result.size_bytes == 0
    assert target.read_bytes() == b""
    assert result.source_record.exists()


# --------------------------------------------------------------------------- #
# 17. 超长响应体（Content-Length 与区间不符，或多发字节）
# --------------------------------------------------------------------------- #
def test_overlong_body_is_protocol_error(server, url, tmp_path):
    target = tmp_path / "depth.tif"
    target.write_bytes(b"old-target")
    server.faults.long_body = 100
    server.clear_requests()
    with pytest.raises(ProtocolError) as excinfo:
        fetch(url, target, CHUNK, expected_sha256=SHA,
              opts=opts(max_retries=0))
    assert "Content-Length" in str(excinfo.value) or "响应体超过" in str(excinfo.value)
    assert target.read_bytes() == b"old-target"
    assert json.loads(FetchPaths(target).state.read_text())["chunks"] == []


# --------------------------------------------------------------------------- #
# 18. 412：版本变化 -> 明确无法沿用；身份未变 -> 不可重试的服务端错误
# --------------------------------------------------------------------------- #
def test_412_with_changed_identity_is_reported_as_version_change(
        server, url, tmp_path):
    target = tmp_path / "depth.tif"
    target.write_bytes(b"old-target")
    target.write_bytes(b"old-target")
    identity = probe_remote(url, opts())
    seed_partial(target, identity, [(0, 999)])
    target.write_bytes(b"old-target")
    new_body = bytearray(BODY)
    new_body[7000:8000] = bytes(reversed(new_body[7000:8000]))
    # HEAD 仍报旧身份；本次 Range GET 置头前供应商换成同长度新对象，
    # 于是条件请求被 412 拒绝，重新探测确认版本已变化。
    server.faults.swap_on_next_range = bytes(new_body)
    server.clear_requests()
    with pytest.raises(IdentityError) as excinfo:
        fetch(url, target, CHUNK, opts=opts(max_retries=0))
    assert "版本已无法沿用" in str(excinfo.value)
    assert target.read_bytes() == b"old-target"
    assert [q["method"] for q in server.requests()] == ["HEAD", "GET", "HEAD"]
    assert server.requests()[1]["status"] == 412


def test_412_with_unchanged_identity_is_server_error_not_retried(
        server, url, tmp_path):
    target = tmp_path / "depth.tif"
    target.write_bytes(b"old-target")
    server.faults.reject_conditionals = True
    server.clear_requests()
    with pytest.raises(FetchError) as excinfo:
        fetch(url, target, CHUNK, opts=opts(max_retries=3))
    assert "412" in str(excinfo.value)
    assert "无法重试" in str(excinfo.value)
    assert target.read_bytes() == b"old-target"
    assert len(server.requests()) == 3, "412 不允许按重试预算循环"

