"""命令行入口。

子命令
------
fetch            把远程 GeoTIFF 可恢复地拉到本地路径（断点续传 + 摘要核对）
make-inventory   扫描输入文件生成带 sha256 的输入清单 JSON
run              全量处理（三个输入清单摘要 -> 暂存 -> 闸门 -> 原子发布）
run-incremental  边界增量处理（复用权重缓存，只重建几何变化的流域）
query            流域明细 / 时次明细查询
manifest         打印计算清单（追溯到参与计算的全部输入版本）
releases         列出已发布版本
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .hashing import sha256_file
from .manifest import (
    BoundaryEntry,
    DepthEntry,
    InputInventory,
    PopulationEntry,
)
from .publishing import PublishGateError
from .query import (
    available_releases,
    load_exposure_rows,
    load_run_manifest,
    provenance,
    query_basin,
    query_forecast,
)
from .runner import run_pipeline


def _build_inventory(args) -> InputInventory:
    depth_entries = []
    for spec in args.depth:
        t, p = spec.split("=", 1)
        fp = Path(p)
        depth_entries.append(DepthEntry(
            forecast_time=t, path=p,
            sha256=sha256_file(fp), size_bytes=fp.stat().st_size,
        ))
    bfp = Path(args.boundary)
    pfp = Path(args.population)
    return InputInventory(
        depth_rasters=depth_entries,
        boundary=BoundaryEntry(
            version=args.boundary_version, path=args.boundary,
            layer=args.boundary_layer, id_field=args.id_field,
            sha256=sha256_file(bfp), size_bytes=bfp.stat().st_size,
        ),
        population=PopulationEntry(
            path=args.population,
            sha256=sha256_file(pfp), size_bytes=pfp.stat().st_size,
        ),
    )


def cmd_fetch(args) -> int:
    # 延迟导入：`fetch` 只需要标准库，不必为了下载而加载 numpy/rasterio
    # （也让“目标被占用”这类错误不必等待重依赖导入）。
    from .fetch import FetchError, FetchLockedError, HttpOptions, fetch

    def log(msg: str) -> None:
        print(f"[fetch] {msg}", file=sys.stderr)

    opts = HttpOptions(
        timeout=args.timeout,
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
        log=log,
    )
    try:
        result = fetch(
            args.url,
            args.target,
            args.chunk_bytes,
            expected_sha256=args.expected_sha256,
            fresh=args.fresh,
            opts=opts,
        )
    except FetchLockedError as exc:
        log(f"失败: {exc}")
        return 3
    except FetchError as exc:
        log(f"失败: {exc}")
        return 1
    except ValueError as exc:
        log(f"参数错误: {exc}")
        return 2
    print(json.dumps({
        "path": str(result.path),
        "size_bytes": result.size_bytes,
        "sha256": result.sha256,
        "request_url": result.identity.request_url,
        "final_url": result.identity.final_url,
        "strong_etag": result.identity.etag,
        "source_record": str(result.source_record),
        "resumed_chunks": result.resumed_chunks,
        "refetched_chunks": result.refetched_chunks,
        "whole_stream_restarts": result.whole_stream_restarts,
        "retries": result.retries,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_make_inventory(args) -> int:
    inv = _build_inventory(args)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(inv.to_dict(), fh, ensure_ascii=False, indent=2)
    print(f"已写入输入清单: {args.output}")
    print(f"  水深时次 {len(inv.depth_rasters)} 个, "
          f"边界版本 {inv.boundary.version}, 人口网格 {inv.population.sha256[:12]}…")
    return 0


def _load_config(args) -> Config:
    if args.config:
        return Config.from_file(args.config)
    return Config(
        depth_threshold_m=args.threshold,
        max_missing_fraction=args.max_missing,
        max_invalid_basin_fraction=args.max_invalid,
        window_tile_size=args.tile,
    )


def cmd_run(args) -> int:
    inv = InputInventory.from_file(args.inventory)
    cfg = _load_config(args)
    try:
        summary = run_pipeline(
            inventory=inv,
            output_root=Path(args.output_root),
            config=cfg,
            run_id=args.run_id,
            cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        )
    except PublishGateError as exc:
        print(f"[拒绝发布] {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"[运行失败] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_query(args) -> int:
    try:
        if args.forecast_time:
            rows = query_forecast(args.output_root, args.forecast_time, args.release)
        else:
            rows = query_basin(args.output_root, args.basin_id, args.release)
    except (KeyError, FileNotFoundError) as exc:
        print(f"[查询失败] {exc}", file=sys.stderr)
        return 1
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_manifest(args) -> int:
    try:
        if args.basin_id:
            data = provenance(args.output_root, args.basin_id, args.release)
        else:
            data = load_run_manifest(args.output_root, args.release)
    except FileNotFoundError as exc:
        print(f"[查询失败] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_releases(args) -> int:
    for name in available_releases(args.output_root):
        print(name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flood-exposure",
        description="流域洪水暴露度分析：等面积面积权重、可追溯、暂存发布",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # ---- fetch ---------------------------------------------------------
    pf = sub.add_parser(
        "fetch",
        help="把远程大文件可恢复地拉到本地路径（分段校验、断点续传、摘要核对）",
    )
    pf.add_argument("url", help="请求 URL；供应商重定向后的最终 URL 会被记录")
    pf.add_argument("target", help="最终本地文件路径；失败期间旧文件保持可用")
    pf.add_argument("--chunk-bytes", type=int, required=True,
                    dest="chunk_bytes", help="每个分段请求的最大字节数")
    pf.add_argument("--expected-sha256", default=None, dest="expected_sha256",
                    help="可选：整个对象的期望 SHA256，不匹配则不替换目标")
    pf.add_argument("--fresh", action="store_true",
                    help="放弃同目标的临时进度与临时文件，按当前远端版本重下")
    pf.add_argument("--timeout", type=float, default=30.0,
                    help="单次请求的套接字超时秒数 (默认 30)")
    pf.add_argument("--max-retries", type=int, default=4, dest="max_retries",
                    help="断连/可重试服务错误的最大重试次数 (默认 4)")
    pf.add_argument("--retry-backoff", type=float, default=0.5,
                    dest="retry_backoff", help="指数退避基数秒 (默认 0.5)")
    pf.set_defaults(func=cmd_fetch)

    # ---- make-inventory ------------------------------------------------
    pi = sub.add_parser("make-inventory", help="生成带 sha256 的输入清单")
    pi.add_argument("--depth", action="append", required=True,
                    metavar="FORECAST_TIME=PATH",
                    help="可重复，格式 时次=水深GeoTIFF路径")
    pi.add_argument("--boundary", required=True, help="流域边界 GeoPackage")
    pi.add_argument("--boundary-version", required=True)
    pi.add_argument("--boundary-layer", default=None)
    pi.add_argument("--id-field", required=True, help="流域标识字段名")
    pi.add_argument("--population", required=True, help="人口网格 GeoTIFF")
    pi.add_argument("--output", required=True)
    pi.set_defaults(func=cmd_make_inventory)

    # ---- run / run-incremental ----------------------------------------
    def _add_run(sp):
        sp.add_argument("--inventory", required=True)
        sp.add_argument("--output-root", required=True)
        sp.add_argument("--config", default=None, help="运行配置 JSON")
        sp.add_argument("--threshold", type=float, default=0.0)
        sp.add_argument("--max-missing", type=float, default=0.10,
                        dest="max_missing")
        sp.add_argument("--max-invalid", type=float, default=0.0,
                        dest="max_invalid",
                        help="边界修复失败流域占比上限，默认 0 (任一失败即拒绝)")
        sp.add_argument("--tile", type=int, default=256)
        sp.add_argument("--run-id", default=None)
        sp.add_argument("--cache-dir", default=None,
                        help="跨运行权重缓存目录；增量入口指向同一目录")

    pr = sub.add_parser("run", help="全量处理并发布")
    _add_run(pr)
    pr.set_defaults(func=cmd_run)

    pri = sub.add_parser("run-incremental",
                         help="边界增量处理（缓存命中的流域不重建几何）")
    _add_run(pri)
    pri.set_defaults(func=cmd_run)  # 同一运行器，增量由缓存键自动驱动

    # ---- query ---------------------------------------------------------
    pq = sub.add_parser("query", help="查询已发布的流域/时次明细")
    pq.add_argument("--output-root", required=True)
    pq.add_argument("--basin-id", default=None)
    pq.add_argument("--forecast-time", default=None)
    pq.add_argument("--release", default=None)
    pq.set_defaults(func=cmd_query)

    # ---- manifest ------------------------------------------------------
    pm = sub.add_parser("manifest", help="打印计算清单（输入版本追溯）")
    pm.add_argument("--output-root", required=True)
    pm.add_argument("--basin-id", default=None)
    pm.add_argument("--release", default=None)
    pm.set_defaults(func=cmd_manifest)

    # ---- releases ------------------------------------------------------
    pl = sub.add_parser("releases", help="列出完整发布版本")
    pl.add_argument("--output-root", required=True)
    pl.set_defaults(func=cmd_releases)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
