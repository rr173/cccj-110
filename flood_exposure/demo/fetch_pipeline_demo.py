"""端到端演示：合成水深 GeoTIFF -> 可控 HTTP 服务 -> fetch 拉取 ->
make-inventory -> run。

重点展示两点：

1. 下载器只搬字节，不改洪水统计口径：拉下来的文件与原文件逐字节一致，
   随机断连后续传完成；
2. 下载后的本地路径直接喂给现有 ``make-inventory`` 和 ``run``，无需改动
   分析入口，也不需要云服务账号。

用法::

    python demo/fetch_pipeline_demo.py [工作目录]
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo"))

from http_support_server import ControllableServer  # noqa: E402

sys.path.insert(0, str(ROOT))
from flood_exposure.fetch import HttpOptions, probe_remote  # noqa: E402

PY = str(ROOT / ".venv/bin/python")
CLI = str(ROOT / ".venv/bin/flood-exposure")
FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {what}")
    if not cond:
        FAILS.append(what)


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
    return r


def main() -> int:
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        tempfile.mkdtemp(prefix="fetch_pipeline_"))
    work.mkdir(parents=True, exist_ok=True)
    data = work / "supplier-data"
    out = work / "analysis"

    print("=" * 78)
    print("A. 生成合成数据（与原 demo 相同的水深/边界/人口）")
    print("=" * 78)
    r = run([PY, "-I", str(ROOT / "demo/make_sample_data.py"), str(data)])
    check(r.returncode == 0, "合成数据生成成功")
    depth_t00 = data / "rasters/depth_T00_utm49n_50m.tif"
    depth_t06 = data / "rasters/depth_T06_utm49n_50m.tif"
    pop = data / "rasters/population_utm48n_100m.tif"
    bnd = data / "boundaries/basins_v1.gpkg"
    for p in (depth_t00, depth_t06, pop, bnd):
        check(p.exists(), f"存在 {p.name} ({p.stat().st_size} 字节)")

    print()
    print("=" * 78)
    print("B. 可控 HTTP 服务提供两个水深 GeoTIFF；首轮在块中途断连")
    print("=" * 78)
    srv = ControllableServer()
    t00_bytes = depth_t00.read_bytes()
    t06_bytes = depth_t06.read_bytes()
    etags = {
        "/supplier/depth_T00.tif": srv.add_object("/supplier/depth_T00.tif", t00_bytes),
        "/supplier/depth_T06.tif": srv.add_object("/supplier/depth_T06.tif", t06_bytes),
    }
    urls = {k: srv.url(k) for k in etags}
    print(f"  深度 T00: {len(t00_bytes)} 字节, 强 ETag={etags['/supplier/depth_T00.tif'][:18]}…")
    print(f"  深度 T06: {len(t06_bytes)} 字节, 强 ETag={etags['/supplier/depth_T06.tif'][:18]}…")

    # 让第 2 条带 Range 的 GET（第 2 块）在正文 1000 字节处断连：
    # 这样第 1 块已完整校验并保留，能真实展示“断点续传”
    srv.drop_range_once(nth_range_get=2, after_bytes=1000)
    local_t00 = work / "downloads/depth_T00_utm49n_50m.tif"
    print("  第一轮：第 2 块在正文 1000 字节处断连，进程以非零退出")
    r = run([CLI, "fetch", urls["/supplier/depth_T00.tif"], str(local_t00),
             "--chunk-bytes", "2048", "--max-retries", "0",
             "--retry-backoff", "0"])
    check(r.returncode == 1, f"首轮退出码 {r.returncode} (期望 1)")
    check(not local_t00.exists(), "失败后目标未出现")
    print("  服务器请求记录:")
    for line in srv.summary()[:4]:
        print("   " + line)
    state = json.loads(
        Path(str(local_t00) + ".fetchstate.json").read_text())
    completed = [(c["start"], c["end"]) for c in state["chunks"]]
    print(f"  断连后保留的已校验块区间: {completed}")
    check(completed == [(0, 2047)], "只有完整校验过的第一块被保留")

    print("  第二轮：新进程续传完成")
    srv.clear_requests()
    expected = hashlib.sha256(t00_bytes).hexdigest()
    r = run([CLI, "fetch", urls["/supplier/depth_T00.tif"], str(local_t00),
             "--chunk-bytes", "2048", "--expected-sha256", expected,
             "--max-retries", "3", "--retry-backoff", "0"])
    check(r.returncode == 0, f"续传退出码 {r.returncode} (期望 0)")
    if r.returncode == 0:
        summary = json.loads(r.stdout)
        print(f"  续传结果: {summary['size_bytes']} 字节, "
              f"resumed_chunks={summary['resumed_chunks']}, "
              f"sha256={summary['sha256'][:20]}…")
        print(f"  来源记录: {summary['source_record']}")
    print("  服务器请求记录（续传阶段）:")
    for line in srv.summary()[:4]:
        print("   " + line)
    check(local_t00.read_bytes() == t00_bytes,
          "T00 下载文件与原文件逐字节一致")

    # T06 正常（分块）下载
    local_t06 = work / "downloads/depth_T06_utm49n_50m.tif"
    r = run([CLI, "fetch", urls["/supplier/depth_T06.tif"], str(local_t06),
             "--chunk-bytes", "2048",
             "--expected-sha256", hashlib.sha256(t06_bytes).hexdigest()])
    check(r.returncode == 0, "T06 分块下载成功")
    check(local_t06.read_bytes() == t06_bytes, "T06 下载文件与原文件逐字节一致")

    # 供应商目录里没有的“本地已有”边界与人口文件照旧直接引用
    print()
    print("=" * 78)
    print("C. 用下载后的本地路径生成输入清单（原有 make-inventory 不变）")
    print("=" * 78)
    inv = work / "inventory_downloaded.json"
    r = run([CLI, "make-inventory",
             "--depth", f"T00={local_t00}",
             "--depth", f"T06={local_t06}",
             "--boundary", str(bnd),
             "--boundary-version", "v1", "--id-field", "basin_id",
             "--population", str(pop),
             "--output", str(inv)])
    check(r.returncode == 0, "make-inventory 成功")
    print(r.stdout.strip())
    inv_data = json.loads(inv.read_text())
    check(inv_data["depth_rasters"][0]["sha256"] == expected,
          "清单里 T00 的 sha256 与下载文件一致")
    check(inv_data["depth_rasters"][0]["path"] == str(local_t00),
          "清单记录的是下载后的本地路径")

    print()
    print("=" * 78)
    print("D. 用同一清单跑分析入口（run），不调整任何洪水统计口径")
    print("=" * 78)
    r = run([CLI, "run", "--inventory", str(inv), "--output-root", str(out),
             "--run-id", "demo_v1", "--max-missing", "0.25",
             "--tile", "64"])
    check(r.returncode == 0, "run 成功发布")
    summary = json.loads(r.stdout)
    print(f"  发布: gate_passed={summary['gate_passed']}, "
          f"release={summary.get('release')}")
    rel = out / "releases" / "demo_v1"
    check((rel / "SUCCESS").exists(), "存在 SUCCESS 完整发布标记")
    check((rel / "exposure.csv").exists(), "存在 exposure.csv")
    with open(rel / "exposure.csv", encoding="utf-8") as fh:
        rows = list(__import__("csv").DictReader(fh))
    print(f"  明细行数: {len(rows)}")
    for row in rows:
        print(f"    {row['basin_id']:<2} {row['forecast_time']:<4} "
              f"exposed_population={float(row['exposed_population']):.2f} "
              f"missing_fraction={float(row['missing_fraction']):.3f}")
    check(len(rows) >= 8, "逐 (流域, 时次) 明细已生成")
    manifest = json.loads((rel / "run_manifest.json").read_text())
    depth_in_manifest = {
        d["forecast_time"]: d["sha256"]
        for d in manifest["inputs"]["depth_rasters"]
    }
    check(depth_in_manifest["T00"] == expected,
          "计算清单可追溯到下载文件的 sha256")
    check(depth_in_manifest["T06"] == hashlib.sha256(t06_bytes).hexdigest(),
          "T06 也可追溯")

    # 下载文件与供应商端的对象在同一个字节序列上：再核对一次
    served = srv.object_body("/supplier/depth_T00.tif")
    check(served == local_t00.read_bytes(), "本地文件与服务端对象逐字节一致")

    print()
    print("=" * 78)
    print("E. 恢复下载绑定原 URL/长度/ETag：供应商换版不会被静默拼接")
    print("=" * 78)
    t00_etag = etags["/supplier/depth_T00.tif"]
    identity = probe_remote(urls["/supplier/depth_T00.tif"],
                            HttpOptions(log=lambda m: None))
    check(identity.etag == t00_etag, "恢复时的强 ETag 与首次探测一致")
    check(identity.total_bytes == len(t00_bytes), "恢复时的总长度与首次一致")
    print(f"  request_url={identity.request_url}")
    print(f"  final_url  ={identity.final_url}")
    source = json.loads(
        Path(str(local_t00) + ".provenance.json").read_text())
    print("  来源记录:")
    print(json.dumps(source, ensure_ascii=False, indent=2))

    srv.stop()
    print()
    print("=" * 78)
    print(f"工作目录: {work}")
    if FAILS:
        print(f"失败 {len(FAILS)} 项:")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("端到端演示通过：下载 → 输入清单 → 分析入口。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
