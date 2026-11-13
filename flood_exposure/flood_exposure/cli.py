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
``delineate        从 DEM + 测站表推导 D8 流向、贡献面积与测站汇水分区
rainfall         本地整理雨量站累计翻斗读数：逐段雨量 + 降雨事件 + 质量摘要
evacuation-access  撤离可达性：按“地点×分钟”计算赶在道路被淹前到达开放
                 避险点的可执行行程（纯本地、标准库）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 顶层只保留轻量、纯标准库依赖；各子命令的重依赖（numpy/rasterio/geopandas）
# 全部延迟到对应 cmd_* 内部导入，保证 `rainfall` 等本地命令在最小环境也可用。
from .config import Config
from .hashing import sha256_file
from .manifest import (
    BoundaryEntry,
    DepthEntry,
    InputInventory,
    PopulationEntry,
)


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
    from .publishing import PublishGateError
    from .runner import run_pipeline

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
    from .query import query_basin, query_forecast

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
    from .query import load_run_manifest, provenance

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
    from .query import available_releases

    for name in available_releases(args.output_root):
        print(name)
    return 0


def cmd_rainfall(args) -> int:
    # 纯标准库本地处理，延迟导入以保持子命令隔离（不触碰栅格/DEM 链路）
    from .rain import RainConfigError, RainInputError, RainOutputError
    from .rain import process_rainfall

    try:
        summary = process_rainfall(
            args.readings, args.config, args.output_dir)
    except RainConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except RainInputError as exc:
        print(f"[输入冲突] {exc}", file=sys.stderr)
        return 3
    except RainOutputError as exc:
        print(f"[写出失败] {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "output_dir": args.output_dir,
        "segments_csv": "rainfall_segments.csv",
        "events_csv": "rainfall_events.csv",
        "summary_json": "rainfall_summary.json",
        "counts": summary["counts"],
        "quality_flag_counts": summary["quality_flag_counts"],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_evacuation_access(args) -> int:
    # 纯标准库本地计算，延迟导入以保持子命令隔离
    from .evacuation import (
        EvacuationConditionsError,
        EvacuationNetworkError,
        EvacuationOutputError,
        EvacuationQueryError,
        run_evacuation,
        write_report,
    )

    try:
        report = run_evacuation(args.network, args.conditions, args.queries)
    except EvacuationNetworkError as exc:
        print(f"[路网错误] {exc}", file=sys.stderr)
        return 2
    except EvacuationConditionsError as exc:
        print(f"[积水路况错误] {exc}", file=sys.stderr)
        return 3
    except EvacuationQueryError as exc:
        print(f"[查询文件错误] {exc}", file=sys.stderr)
        return 4
    if args.output:
        try:
            write_report(report, args.output)
        except EvacuationOutputError as exc:
            print(f"[写出失败] {exc}", file=sys.stderr)
            return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_delineate(args) -> int:
    # 延迟导入：delineate 用到 numpy/rasterio，只有真跑时才加载
    from .hydrology import (
        DemError,
        NoOutletError,
        load_dem,
        load_stations,
        delineate,
        write_all_outputs,
    )

    try:
        grid = load_dem(args.dem)
        stations = load_stations(args.stations, grid)
    except (DemError, FileNotFoundError) as exc:
        print(f"[输入错误] {exc}", file=sys.stderr)
        return 2
    try:
        result, summary = delineate(grid, stations)
    except NoOutletError as exc:
        print(f"[无法确定排水拓扑] {exc}", file=sys.stderr)
        return 3
    except DemError as exc:
        print(f"[推导失败] {exc}", file=sys.stderr)
        return 1

    try:
        written = write_all_outputs(args.output_dir, grid, result, summary,
                                    args.dem, args.stations)
    except OSError as exc:
        print(f"[写出失败] {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "output_dir": str(args.output_dir),
        "dem_sha256": written["inputs"]["dem"]["sha256"],
        "stations": {r["outlet_id"]: {
            "row": r["row"], "col": r["col"],
            "elevation_m": r["elevation_m"],
            "filled_elevation_m": r["filled_elevation_m"],
            "total_upstream_area_m2": r["total_upstream_area_m2"],
            "exclusive_area_m2": r["exclusive_area_m2"],
            "next_downstream_outlet": r["next_downstream_outlet"],
        } for r in written["stations"]},
        "outside_area_m2": written["outside_area_m2"],
        "unassigned_area_m2": written["unassigned_area_m2"],
        "fill_volume_m3": written["fill_volume_m3"],
        "pit_filled_cells": written["pit_filled_cells"],
        "rasters": [
            "filled_dem.tif", "pit_depth_m.tif", "flow_direction.tif",
            "contributing_area_m2.tif", "exclusive_partition.tif",
        ],
        "summary": "summary.json", "stations_csv": "stations.csv",
    }, ensure_ascii=False, indent=2))
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

    # ---- delineate -----------------------------------------------------
    pd = sub.add_parser(
        "delineate",
        help="从 DEM + 测站表推导排水拓扑与测站汇水分区 (地形推导, 非水动力模拟)",
        description="输入单波段米制投影 GeoTIFF 高程与测站 CSV (outlet_id,row,col)，"
                    "输出填洼高程、D8 流向、贡献面积、独占分区与测站摘要。",
    )
    pd.add_argument("--dem", required=True, help="单波段 GeoTIFF 高程 (米制投影, 北向上)")
    pd.add_argument("--stations", required=True,
                    help="测站 CSV，列 outlet_id,row,col (行列从 0 开始)")
    pd.add_argument("--output-dir", required=True, dest="output_dir")
    pd.set_defaults(func=cmd_delineate)

    # ---- releases ------------------------------------------------------
    pl = sub.add_parser("releases", help="列出完整发布版本")
    pl.add_argument("--output-root", required=True)
    pl.set_defaults(func=cmd_releases)

    # ---- rainfall ------------------------------------------------------
    prn = sub.add_parser(
        "rainfall",
        help="本地整理雨量站累计翻斗读数（逐段雨量、降雨事件、质量摘要）",
        description="读取读数 CSV 与设备配置 JSON，输出逐段降雨量 CSV、"
                    "降雨事件 CSV 和带输入 SHA256 的 JSON 处理摘要。"
                    "纯本地处理，不接入网络，也不影响洪水栅格/DEM 计算。"
                    "累计计数只确定观测区间上的雨量，不能据此声称雨在区间内"
                    "的精确落点时刻。",
    )
    prn.add_argument("--readings", required=True,
                     help="读数 CSV，列 device_id,observed_at,cumulative_count；"
                          "observed_at 必须带时区偏移（如 2026-06-01T08:00:00+08:00）")
    prn.add_argument("--config", required=True,
                     help="设备配置 JSON：每台设备给 tip_mm、max_interval_seconds、"
                          "dry_separation_seconds，可选 counter_modulus 与 maintenance")
    prn.add_argument("--output-dir", required=True, dest="output_dir",
                     help="输出目录；三个结果文件先写临时文件并回读校验，"
                          "再原子替换已有目标，失败时不留下半成品")
    prn.set_defaults(func=cmd_rainfall)

    # ---- evacuation-access ----------------------------------------------
    pev = sub.add_parser(
        "evacuation-access",
        help="撤离可达性：赶在道路被淹前到达开放避险点的可执行行程",
        description="读取路网 JSON（路口/有向道路/避险点开放窗口）、积水 "
                    "CSV（road_id,begin,end,depth_m；空值=未知）与查询 JSON "
                    "（request_id/起点/出发分钟/截止分钟），按“地点×分钟”状态"
                    "空间计算可实际执行的撤离行程：车程整段区间都必须有已知且"
                    "合格的路况，未知不当干路。逐请求返回 "
                    "reachable/unreachable/invalid_request；路网或路况本身"
                    "非法则整体拒绝。结果只在已知路况与所给行驶时间假设下成立。",
    )
    pev.add_argument("--network", required=True,
                     help="路网 JSON：analysis_window、intersections（含 "
                          "can_wait 与受淹区间）、roads（正整数行驶分钟与可通行"
                          "最大积水深度）、shelters（开放窗口）")
    pev.add_argument("--conditions", required=True,
                     help="积水 CSV：road_id,begin,end,depth_m；左闭右开，"
                          "空值=未知；重叠冲突即整体拒绝")
    pev.add_argument("--queries", required=True,
                     help="查询 JSON：{\"requests\": [{request_id, origin, "
                          "depart_minute, deadline_minute}, ...]}")
    pev.add_argument("--output", default=None,
                     help="可选：报告 JSON 写出路径（临时文件 + 原子替换）；"
                          "无论是否指定，报告都会打印到标准输出")
    pev.set_defaults(func=cmd_evacuation_access)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
