"""运行编排：全量处理与边界增量处理。

增量语义
--------
权重表缓存键包含 (流域几何指纹, 人口网格 sha256, 人口网格变换/CRS,
工作 CRS, 代码版本)。新边界版本运行时：

* 几何指纹未变的流域 —— 直接命中缓存，跳过全部几何相交；
* 几何有变化或新增的流域 —— 重建权重表；
* 新版本中消失的流域 —— 不再产出。

水深栅格不参与权重表键，因此换预报时次天然复用全部权重表。
"""
from __future__ import annotations

import csv
import platform
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__ as CODE_VERSION
from .basins import BasinCollection, load_basins
from .config import Config
from .exposure import BasinExposure, compute_basin_exposure
from .hashing import sha256_file
from .manifest import InputInventory
from .population import (
    BasinWeightTable,
    build_weight_table,
    load_population,
    load_weight_table_cache,
    overlap_report,
    save_weight_table_cache,
)
from .publishing import (
    PublishGateError,
    StagedRun,
    cleanup_staging,
    current_release,
)

EXPOSURE_CSV_FIELDS = [
    "basin_id", "forecast_time", "boundary_version",
    "depth_path", "depth_sha256", "population_path", "population_sha256",
    "work_crs", "threshold_m", "basin_area_m2",
    "weighted_population", "exposed_population",
    "wet_area_m2", "dry_area_m2", "nodata_area_m2", "oob_area_m2",
    "missing_area_m2", "missing_fraction", "pop_nodata_cells",
    "cell_count", "flags", "gate_passed",
]


def _write_exposure_csv(path: Path, rows: list[BasinExposure]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPOSURE_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            d = row.to_row()
            writer.writerow({k: d.get(k, "") for k in EXPOSURE_CSV_FIELDS})


def run_pipeline(
    inventory: InputInventory,
    output_root: Path,
    config: Config,
    run_id: str | None = None,
    cache_dir: Path | None = None,
) -> dict:
    """执行一次完整运行并原子发布。

    Returns
    -------
    dict  运行摘要 (发布目录、闸门报告、缓存命中情况等)。
    Raises
    ------
    PublishGateError  NoData/无效流域超闸门时抛出，发布被拒绝。
    """
    config.validate()
    output_root = Path(output_root)
    run_id = run_id or time.strftime("run_%Y%m%dT%H%M%SZ", time.gmtime())

    # 0. 输入校验 + 启动清理
    errors = inventory.verify_files()
    if errors:
        raise FileNotFoundError("输入清单校验失败:\n" + "\n".join(errors))
    cleaned = cleanup_staging(output_root)

    # 1. 边界（带修复、投影、几何指纹）
    boundary = inventory.boundary
    collection: BasinCollection = load_basins(
        path=boundary.path,
        version=boundary.version,
        source_sha256=boundary.sha256,
        id_field=boundary.id_field,
        work_crs=config.work_crs,
        layer=boundary.layer,
    )
    pop = load_population(inventory.population.path, inventory.population.sha256)
    overlaps = overlap_report(collection)

    # 2. 权重表：命中缓存则跳过几何相交
    external_cache = cache_dir
    tables: dict[str, BasinWeightTable] = {}
    cache_hits: list[str] = []
    cache_misses: list[str] = []

    with StagedRun(output_root, run_id) as stage:
        staged_cache = stage.staging / "weight_cache"
        for basin in collection.usable_basins():
            stage.raise_if_aborted()
            # 先用一个临时表对象算出缓存键
            probe = build_weight_table(basin, pop, config.work_crs)
            if probe is None:
                continue
            key = probe.cache_key(pop, config.work_crs, CODE_VERSION)
            cached = None
            for d in [staged_cache, external_cache] if external_cache else [staged_cache]:
                cached = load_weight_table_cache(Path(d), key)
                if cached is not None:
                    break
            if cached is not None:
                tables[basin.basin_id] = cached
                cache_hits.append(basin.basin_id)
            else:
                tables[basin.basin_id] = probe
                cache_misses.append(basin.basin_id)
                save_weight_table_cache(
                    staged_cache, probe, pop, config.work_crs, CODE_VERSION
                )

        # 外部缓存：把新生成的表也回写一份，供跨运行复用（增量入口用）
        if external_cache:
            Path(external_cache).mkdir(parents=True, exist_ok=True)
            for bid in cache_misses:
                save_weight_table_cache(
                    Path(external_cache), tables[bid], pop,
                    config.work_crs, CODE_VERSION,
                )

        # 3. 逐时次 × 逐流域暴露度
        results: list[BasinExposure] = []
        for depth in inventory.depth_rasters:
            stage.raise_if_aborted()
            for basin in collection.usable_basins():
                stage.raise_if_aborted()
                wt = tables.get(basin.basin_id)
                if wt is None:
                    continue
                overlap_ids = [o["other_basin_id"] for o in overlaps.get(basin.basin_id, [])]
                result = compute_basin_exposure(
                    basin=basin,
                    table=wt,
                    pop=pop,
                    depth_path=depth.path,
                    depth_sha256=depth.sha256,
                    forecast_time=depth.forecast_time,
                    boundary_version=boundary.version,
                    work_crs=config.work_crs,
                    threshold=config.depth_threshold_m,
                    tile=config.window_tile_size,
                    overlap_ids=overlap_ids,
                )
                results.append(result)

        # 4. 发布闸门
        row_dicts = [r.to_row() for r in results]
        gate = stage.check_gate(
            row_dicts, collection.failed_ids(), len(collection.basins), config
        )
        # 闸门失败的行打上标记 (仍随清单留在暂存区供审计，但不发布)
        fail_keys = {
            (f["basin_id"], f["forecast_time"])
            for f in gate.failures if f.get("basin_id")
        }
        for r in results:
            if (r.basin_id, r.forecast_time) in fail_keys:
                from .config import QualityFlag
                if QualityFlag.NODATA_GATE_FAIL not in r.flags:
                    r.flags.append(QualityFlag.NODATA_GATE_FAIL)
                r.gate_passed = False

        # 5. 计算清单（完整追溯信息）
        manifest = _build_run_manifest(
            inventory=inventory,
            config=config,
            run_id=run_id,
            collection=collection,
            overlaps=overlaps,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            gate=gate,
            cleaned_staging=cleaned,
        )

        # 6. 写暂存区；闸门不过则拒绝发布（暂存区随上下文管理器销毁）
        _write_exposure_csv(stage.staging / "exposure.csv", results)
        stage.write_json("run_manifest.json", manifest)
        stage.write_json(
            "gate_report.json",
            {"passed": gate.passed, "failures": gate.failures,
             "invalid_basin_fraction": gate.invalid_basin_fraction},
        )

        if not gate.passed:
            raise PublishGateError(
                "发布闸门拒绝，暂存区已丢弃，current 保持上一完整版本。\n"
                + str(gate.failures)
            )

        released = stage.publish(gate)

    return {
        "run_id": run_id,
        "release_dir": str(released),
        "gate_passed": gate.passed,
        "n_basins": len(collection.basins),
        "n_times": len(inventory.depth_rasters),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "failed_basins": collection.failed_ids(),
        "current_release": str(current_release(output_root)) if current_release(output_root) else None,
    }


def _build_run_manifest(inventory: InputInventory, config: Config, run_id: str,
                        collection: BasinCollection, overlaps: dict,
                        cache_hits: list[str], cache_misses: list[str],
                        gate, cleaned_staging: list[str]) -> dict:
    return {
        "run_id": run_id,
        "code_version": CODE_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "config": config.to_dict(),
        "declared_precision": config.declared_precision,
        "inputs": inventory.to_dict(),
        "input_summary": {
            "n_depth_rasters": len(inventory.depth_rasters),
            "depth_forecast_times": [d.forecast_time for d in inventory.depth_rasters],
            "boundary_version": inventory.boundary.version,
            "boundary_sha256": inventory.boundary.sha256,
            "population_sha256": inventory.population.sha256,
        },
        "basins": [
            {
                "basin_id": b.basin_id,
                "usable": b.usable,
                "area_work_m2": b.area_work_m2,
                "geometry_hash": b.geometry_hash,
                "flags": b.flags,
                "overlaps": overlaps.get(b.basin_id, []),
            }
            for b in collection.basins
        ],
        "incremental": {
            "weight_cache_hits": cache_hits,
            "weight_cache_misses": cache_misses,
            "rebuilt_basin_ids": cache_misses,
        },
        "gate": {
            "passed": gate.passed,
            "failures": gate.failures,
            "invalid_basin_fraction": gate.invalid_basin_fraction,
        },
        "cleaned_staging_runs": cleaned_staging,
    }
