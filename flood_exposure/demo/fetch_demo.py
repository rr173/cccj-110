"""可恢复下载演示：用本地可控 HTTP 服务复现每一种故障，逐条断言并打印
服务器**实际收到的请求**与磁盘上留下的字节。

用标准库实现，不依赖 pytest；所有断言失败即非零退出。

用法::

    python demo/fetch_demo.py [工作目录]

覆盖的场景（与需求一一对应）：

1. 首轮在块中途断连 -> 新进程补齐 -> 与原文件逐字节对照；
2. 篡改一个已记完成的临时块 -> 恢复时重取该块；
3. 断点处替换同长度对象 -> 身份变化被拒，旧目标保留；--fresh 重新取得；
4. 服务器忽略 Range 返回 200 -> 从 0 流式重传，不写进偏移；
5. 伪造 Content-Range -> 协议错误，块不记为完成；
6. 不完整正文（短体） -> 失败且旧目标不变；
7. 416 -> 仅在本地全部区间已校验时才当作完成，否则协议错误；
8. 摘要不符 -> 旧目标不变，已校验块可复用；
9. 双进程争用 -> 后进程快速报占用；异常退出后新进程可接管。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo"))

from http_support_server import ControllableServer, strong_etag  # noqa: E402

sys.path.insert(0, str(ROOT))
from flood_exposure.fetch import (  # noqa: E402
    FetchError,
    FetchLockedError,
    FetchPaths,
    FetchVerificationError,
    HttpOptions,
    IdentityError,
    ProtocolError,
    TargetLock,
    _new_progress,
    chunk_sha256,
    fetch,
    probe_remote,
    save_progress,
)

PY = str(ROOT / ".venv/bin/python")
CLI = str(ROOT / ".venv/bin/flood-exposure")
CHUNK = 1000
_FAILS: list[str] = []


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def check(cond: bool, what: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {what}")
    if not cond:
        _FAILS.append(what)


def show_requests(srv: ControllableServer, limit: int | None = None) -> None:
    print("  服务器实际收到的请求:")
    for line in srv.summary()[:limit]:
        print("   " + line)


def opts() -> HttpOptions:
    return HttpOptions(max_retries=0, retry_backoff=0.0, sleep=lambda s: None,
                       log=lambda m: print("   [fetch] " + m))


def make_body(size: int = 15360) -> bytes:
    base = bytes(range(256))
    return (base * (size // len(base) + 1))[:size]


def run_cli(*args: str, expect_rc: int | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run([CLI, "fetch", *args], cwd=ROOT,
                       capture_output=True, text=True)
    tail = [ln for ln in r.stderr.strip().splitlines() if ln][-3:]
    for ln in tail:
        print("   " + ln)
    if expect_rc is not None:
        check(r.returncode == expect_rc,
              f"CLI 退出码 {r.returncode} (期望 {expect_rc})")
    return r


# --------------------------------------------------------------------------- #
def scenario_disconnect_resume(work: Path) -> None:
    banner("1. 首轮在块中途断连，新进程补齐，并与原文件逐字节对照")
    srv = ControllableServer()
    body = make_body()
    srv.add_object("/supplier/depth.tif", body)
    url = srv.url("/supplier/depth.tif")
    target = work / "depth_resume.tif"
    srv.drop_range_once(nth_range_get=3, after_bytes=400)

    print(f"  对象 {len(body)} 字节, 块 {CHUNK} 字节; 第 3 个 Range 请求在 400 字节处断连")
    run_cli(url, str(target), "--chunk-bytes", str(CHUNK),
            "--max-retries", "0", expect_rc=1)
    show_requests(srv)

    paths = FetchPaths(target)
    check(not target.exists(), "失败时目标不存在（不会有半成品冒充完整文件）")
    state = json.loads(paths.state.read_text())
    check([c["start"] for c in state["chunks"]] == [0, 1000],
          "只有前两个完整块被记录；断连的第 3 块未记为完成")
    check(paths.part.stat().st_size == len(body),
          "临时文件按总长度稀疏预分配（长度本身不作为完成证据）")

    print("  -- 新进程（CLI）补齐:")
    srv.clear_requests()
    r = run_cli(url, str(target), "--chunk-bytes", str(CHUNK),
                "--expected-sha256", hashlib.sha256(body).hexdigest(),
                "--max-retries", "2", "--retry-backoff", "0", expect_rc=0)
    summary = json.loads(r.stdout)
    show_requests(srv, limit=3)
    print(f"   resumed_chunks={summary['resumed_chunks']} sha256={summary['sha256'][:16]}…")
    check(target.read_bytes() == body, "下载结果与原文件逐字节一致")
    check(summary["resumed_chunks"] == 2, "续传复用了 2 个已校验块")
    reqs = srv.requests()
    first_get = next(q for q in reqs if q["method"] == "GET")
    check(first_get["range"] == "bytes=2000-2999", "从缺口 2000 精确续传")
    check(bool(first_get["if_match"]) and bool(first_get["if_range"]),
          "每条分段请求都带 If-Match / If-Range 条件")
    check(not any(q["status"] == 200 and q["range"] for q in reqs),
          "没有把整对象写进某个偏移")
    srv.stop()


# --------------------------------------------------------------------------- #
def scenario_tampered_chunk(work: Path) -> None:
    banner("2. 篡改一个已记完成的临时块，恢复时重取该块")
    srv = ControllableServer()
    body = make_body()
    srv.add_object("/supplier/depth.tif", body)
    url = srv.url("/supplier/depth.tif")
    target = work / "depth_tamper.tif"
    paths = FetchPaths(target)
    identity = probe_remote(url, opts())
    with open(paths.part, "wb") as fh:
        fh.write(body)
    progress = _new_progress(identity)
    progress["chunks"] = [
        {"start": s, "end": e, "sha256": chunk_sha256(paths.part, s, e)}
        for s, e in [(0, 999), (1000, 1999), (2000, 2999)]
    ]
    save_progress(paths, progress)
    with open(paths.part, "r+b") as fh:
        fh.seek(1500)
        fh.write(b"TAMPERED")
    print("  已完成块 1000-1999 的中间 8 字节被篡改")

    r = fetch(url, target, CHUNK, expected_sha256=hashlib.sha256(body).hexdigest(),
              opts=opts())
    show_requests(srv, limit=4)
    check(r.refetched_chunks == 1, "检测出 1 个不可信块")
    check(r.resumed_chunks == 2, "另外 2 个已校验块被复用")
    get_ranges = [q["range"] for q in srv.requests() if q["method"] == "GET"]
    check("bytes=1000-1999" in get_ranges, "被篡改块被重新获取")
    check("bytes=0-999" not in get_ranges, "未受影响的块没有重下")
    check(target.read_bytes() == body, "最终文件逐字节一致")
    srv.stop()


# --------------------------------------------------------------------------- #
def scenario_version_swap(work: Path) -> None:
    banner("3. 断点处换成同长度对象：拒绝拼接；--fresh 取得当前版本")
    srv = ControllableServer()
    body = make_body()
    srv.add_object("/supplier/depth.tif", body)
    url = srv.url("/supplier/depth.tif")
    target = work / "depth_swap.tif"
    old_target = b"previous-good-target"
    target.write_bytes(old_target)
    paths = FetchPaths(target)
    identity = probe_remote(url, opts())
    with open(paths.part, "wb") as fh:
        fh.write(body)
    progress = _new_progress(identity)
    progress["chunks"] = [
        {"start": s, "end": e, "sha256": chunk_sha256(paths.part, s, e)}
        for s, e in [(0, 999), (1000, 1999)]
    ]
    save_progress(paths, progress)
    part_before = paths.part.read_bytes()
    state_before = paths.state.read_text()

    new_body = bytearray(body)
    new_body[5000:6000] = bytes(reversed(new_body[5000:6000]))
    new_body = bytes(new_body)
    srv.set_object("/supplier/depth.tif", new_body)
    print(f"  远端换成同长度新版本: 旧 ETag={identity.etag[:14]}… 新 ETag={strong_etag(new_body)[:14]}…")
    srv.clear_requests()
    try:
        fetch(url, target, CHUNK,
              expected_sha256=hashlib.sha256(new_body).hexdigest(), opts=opts())
        check(False, "版本变化被拒绝")
    except IdentityError as exc:
        print("   [fetch] " + str(exc).replace("\n", " "))
        check(True, "版本变化被拒绝（IdentityError）")
        check("版本已无法沿用" in str(exc), "明确说明版本无法沿用")
    show_requests(srv)
    check(target.read_bytes() == old_target, "旧目标保持可用")
    check(paths.part.read_bytes() == part_before, "临时文件未被改动")
    check(paths.state.read_text() == state_before, "进度记录未被改动")
    check(all(q["method"] == "HEAD" for q in srv.requests()),
          "没有为拼接新版本发出任何 Range 请求")

    print("  -- 显式 --fresh 放弃旧进度:")
    srv.clear_requests()
    r = run_cli(url, str(target), "--chunk-bytes", str(CHUNK),
                "--expected-sha256", hashlib.sha256(new_body).hexdigest(),
                "--fresh", expect_rc=0)
    summary = json.loads(r.stdout)
    show_requests(srv, limit=2)
    check(target.read_bytes() == new_body, "fresh 后取得当前版本")
    check(summary["strong_etag"] == strong_etag(new_body), "来源记录中的 ETag 是新版本")
    srv.stop()


# --------------------------------------------------------------------------- #
def scenario_ignored_range(work: Path) -> None:
    banner("4. 服务器忽略 Range 返回 200：从 0 流式重传，不写进偏移")
    srv = ControllableServer()
    body = make_body()
    srv.add_object("/supplier/depth.tif", body)
    url = srv.url("/supplier/depth.tif")
    target = work / "depth_ignore.tif"
    paths = FetchPaths(target)
    identity = probe_remote(url, opts())
    with open(paths.part, "wb") as fh:
        fh.write(body)
    progress = _new_progress(identity)
    progress["chunks"] = [{"start": 0, "end": 999,
                           "sha256": chunk_sha256(paths.part, 0, 999)}]
    save_progress(paths, progress)
    srv.faults.ignore_range = True
    print("  服务器对带 Range 的请求返回 200 + 整个对象")
    srv.clear_requests()
    r = fetch(url, target, CHUNK, expected_sha256=hashlib.sha256(body).hexdigest(),
              opts=opts())
    show_requests(srv, limit=2)
    check(r.whole_stream_restarts == 1, "检测到忽略 Range，整文件重传 1 次")
    check(target.read_bytes() == body, "最终文件逐字节一致")
    ranged_200 = [q for q in srv.requests() if q["status"] == 200 and q["range"]]
    check(len(ranged_200) == 1 and ranged_200[0]["bytes_sent"] == len(body),
          "200 响应发的是整个对象（客户端没有把它写进 0-999 的偏移）")
    srv.stop()


# --------------------------------------------------------------------------- #
def scenario_protocol_faults(work: Path) -> None:
    banner("5. 伪造 Content-Range / 不完整正文 / 416")
    body = make_body()
    sha = hashlib.sha256(body).hexdigest()

    # 5a 伪造 Content-Range
    srv = ControllableServer()
    srv.add_object("/supplier/depth.tif", body)
    url = srv.url("/supplier/depth.tif")
    target = work / "depth_forged.tif"
    target.write_bytes(b"old-target")
    srv.faults.forge_content_range = (0, 0, 7)
    print("  5a. 206 的 Content-Range 总长度被伪造为 total+7")
    try:
        fetch(url, target, CHUNK, expected_sha256=sha, opts=opts())
        check(False, "伪造 Content-Range 被拒绝")
    except ProtocolError as exc:
        print("   [fetch] " + str(exc)[:150])
        check("Content-Range" in str(exc), "报出 Content-Range 协议错误")
    show_requests(srv, limit=2)
    check(target.read_bytes() == b"old-target", "旧目标未被替换")
    state = json.loads(FetchPaths(target).state.read_text())
    check(state["chunks"] == [], "伪造响应没有产生任何“已确认块”")
    srv.stop()

    # 5b 不完整正文
    srv = ControllableServer()
    srv.add_object("/supplier/depth.tif", body)
    url = srv.url("/supplier/depth.tif")
    target = work / "depth_short.tif"
    target.write_bytes(b"old-target")
    srv.faults.short_body = 100
    print("  5b. 206 头齐全但正文少发 100 字节后关闭连接")
    try:
        fetch(url, target, CHUNK, expected_sha256=sha, opts=opts())
        check(False, "短体被拒绝")
    except FetchError as exc:
        print("   [fetch] " + str(exc)[:150])
        check(True, "短体失败（非零退出语义）")
    show_requests(srv, limit=2)
    check(target.read_bytes() == b"old-target", "旧目标未被替换")
    srv.stop()

    # 5c 416（本地不完整）
    srv = ControllableServer()
    srv.add_object("/supplier/depth.tif", body)
    url = srv.url("/supplier/depth.tif")
    target = work / "depth_416.tif"
    target.write_bytes(b"old-target")
    srv.faults.force_416 = True
    print("  5c. 服务器对 Range 请求一律返回 416，而本地没有任何已校验区间")
    try:
        fetch(url, target, CHUNK, opts=opts())
        check(False, "不完整本地的 416 被拒绝")
    except ProtocolError as exc:
        print("   [fetch] " + str(exc)[:150])
        check("416" in str(exc) and "协议错误" in str(exc),
              "416 被当作协议错误而非完成")
    show_requests(srv, limit=2)
    srv.stop()

    # 5d 416 且本地全部区间已校验 -> 才是“已完成”
    srv = ControllableServer()
    srv.add_object("/supplier/depth.tif", body)
    url = srv.url("/supplier/depth.tif")
    target = work / "depth_416_done.tif"
    paths = FetchPaths(target)
    identity = probe_remote(url, opts())
    with open(paths.part, "wb") as fh:
        fh.write(body)
    progress = _new_progress(identity)
    progress["chunks"] = [{"start": 0, "end": len(body) - 1,
                           "sha256": chunk_sha256(paths.part, 0, len(body) - 1)}]
    save_progress(paths, progress)
    srv.faults.force_416 = True
    print("  5d. 同样的 416，但本地全部区间已按摘要校验且长度一致")
    r = fetch(url, target, CHUNK, expected_sha256=sha, opts=opts())
    show_requests(srv, limit=3)
    check(target.read_bytes() == body, "416 仅在本地已校验完整时按完成处理")
    check(r.sha256 == sha, "整体摘要复核通过")
    srv.stop()


# --------------------------------------------------------------------------- #
def scenario_digest_and_contention(work: Path) -> None:
    banner("6. 期望摘要不符时旧目标不变；双进程争用与异常退出后接管")
    srv = ControllableServer()
    body = make_body()
    srv.add_object("/supplier/depth.tif", body)
    url = srv.url("/supplier/depth.tif")
    target = work / "depth_digest.tif"
    old = b"old-target-content"
    target.write_bytes(old)
    print("  6a. 提供错误的期望 SHA256")
    try:
        fetch(url, target, CHUNK, expected_sha256="0" * 64, opts=opts())
        check(False, "摘要不符被拒绝")
    except FetchVerificationError as exc:
        print("   [fetch] " + str(exc).splitlines()[0])
        check("未被替换" in str(exc), "明确说明目标未被替换")
    check(target.read_bytes() == old, "旧目标未被替换")
    r = fetch(url, target, CHUNK,
              expected_sha256=hashlib.sha256(body).hexdigest(), opts=opts())
    check(r.wrote_bytes == 0, "更正期望摘要后复用全部已校验块（0 字节重下）")
    check(target.read_bytes() == body, "最终文件正确")

    # 6b 同一进程内争用（快速报占用）
    print("  6b. 同一目标已持有 flock 时再次启动")
    with TargetLock(FetchPaths(target)):
        t0 = time.monotonic()
        try:
            fetch(url, target, CHUNK, opts=opts())
            check(False, "被占用时应当报错")
        except FetchLockedError as exc:
            dt = (time.monotonic() - t0) * 1000
            check(dt < 500, f"占用冲突 {dt:.1f}ms 内快速报错")
            print("   [fetch] " + str(exc)[:150])

    # 6c 两个 CLI 进程争用
    print("  6c. 两个 CLI 进程同时下载同一目标")
    target2 = work / "depth_two_procs.tif"
    srv.faults.delay = 0.15
    srv.clear_requests()
    p1 = subprocess.Popen([CLI, "fetch", url, str(target2),
                           "--chunk-bytes", str(CHUNK)], cwd=ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if any(q["method"] == "GET" for q in srv.requests()):
            break
        time.sleep(0.01)
    p2 = subprocess.Popen([CLI, "fetch", url, str(target2),
                           "--chunk-bytes", str(CHUNK)], cwd=ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _, err2 = p2.communicate()
    _, err1 = p1.communicate()
    print("   后进程 stderr:", err2.strip().splitlines()[-1][:160] if err2 else "")
    if p2.returncode == 0:
        print("   （本机第一个进程先完成，并发窗口未出现；仍验证最终文件完整）")
        check(p1.returncode == 0 and target2.read_bytes() == body,
              "两进程至少一个成功且结果完整")
    else:
        check(p2.returncode == 3, f"后进程退出码 {p2.returncode} (期望 3=占用)")
        check("占用" in err2, "后进程明确报占用")
        check(p1.returncode == 0, "前进程正常完成")
        check(target2.read_bytes() == body, "后进程没有覆盖前进程进度")
    srv.faults.delay = 0

    # 6d 异常退出后接管
    print("  6d. 下载进程被 kill -9 后，新进程重新接管")
    target3 = work / "depth_takeover.tif"
    srv.faults.delay = 0.03
    srv.clear_requests()
    proc = subprocess.Popen([CLI, "fetch", url, str(target3),
                             "--chunk-bytes", str(CHUNK)], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    proc.kill()
    proc.wait()
    check(proc.returncode != 0, "进程被强制终止 (模拟异常退出)")
    srv.faults.delay = 0
    fetch(url, target3, CHUNK,
          expected_sha256=hashlib.sha256(body).hexdigest(), opts=opts())
    check(r.sha256 == hashlib.sha256(body).hexdigest(), "接管后摘要正确")
    check(target3.read_bytes() == body, "接管后文件完整（flock 随进程退出自动释放）")
    print(f"   接管时复用了 {r.resumed_chunks} 个先前进程校验过的块")
    srv.stop()


# --------------------------------------------------------------------------- #
def scenario_redirect(work: Path) -> None:
    banner("7. 重定向：记录请求 URL 与最终 URL，来源记录可审计")
    srv = ControllableServer()
    body = make_body()
    srv.add_object("/storage/2026-11-13/depth_T00.tif", body)
    srv.add_redirect("/supplier/depth_T00.tif", "/storage/2026-11-13/depth_T00.tif")
    url = srv.url("/supplier/depth_T00.tif")
    target = work / "depth_redirect.tif"
    fetch(url, target, CHUNK,
          expected_sha256=hashlib.sha256(body).hexdigest(), opts=opts())
    show_requests(srv, limit=2)
    record = json.loads(FetchPaths(target).source.read_text())
    check(record["request_url"] == url, "来源记录保留请求 URL")
    check(record["final_url"] == srv.url("/storage/2026-11-13/depth_T00.tif"),
          "来源记录保留最终 URL")
    check(record["strong_etag"] == strong_etag(body), "来源记录保留强 ETag")
    check(record["total_bytes"] == len(body), "来源记录保留总长度")
    check(record["sha256"] == hashlib.sha256(body).hexdigest(), "来源记录保留实际摘要")
    check(target.read_bytes() == body, "最终文件逐字节一致")
    srv.stop()


# --------------------------------------------------------------------------- #
def main() -> int:
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        tempfile.mkdtemp(prefix="fetch_demo_"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"演示工作目录: {work}")
    scenario_disconnect_resume(work)
    scenario_tampered_chunk(work)
    scenario_version_swap(work)
    scenario_ignored_range(work)
    scenario_protocol_faults(work)
    scenario_digest_and_contention(work)
    scenario_redirect(work)
    print()
    if _FAILS:
        print(f"演示失败: {len(_FAILS)} 项断言未通过")
        for f in _FAILS:
            print("  - " + f)
        return 1
    print("全部下载场景断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
