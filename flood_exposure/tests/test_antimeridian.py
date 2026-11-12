"""反经线 (antimeridian) 场景的端到端测试。

构造一个跨越 ±180° 的流域边界与两套栅格，验证：
* split_antimeridian 拆分后面积与等面积参照一致；
* 流水线不报错且产出的权重份额在 [0,1]；
* 拆分出的两部分都能与人口格相交。
"""
from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Polygon, mapping
import geopandas as gpd

from flood_exposure.basins import load_basins
from flood_exposure.config import Config
from flood_exposure.geometry import make_transformer, split_antimeridian
from flood_exposure.hashing import sha256_file
from flood_exposure.manifest import (
    BoundaryEntry, DepthEntry, InputInventory, PopulationEntry,
)
from flood_exposure.population import build_weight_table, load_population
from flood_exposure.runner import run_pipeline
import shapely


def _area_work(geom, to_work):
    return shapely.transform(
        geom, lambda xy: np.column_stack(to_work.transform(xy[:, 0], xy[:, 1]))
    ).area


def test_antimeridian_basin_end_to_end(tmp_path):
    work_crs = "EPSG:6933"
    # 跨 ±180° 的流域：经度 170°E ~ 170°W，纬度 60°~66°
    crossing = Polygon([(170, 60), (-170, 60), (-170, 66), (170, 66)])
    split = split_antimeridian(crossing)
    assert split.geom_type == "MultiPolygon" and len(split.geoms) == 2

    # 面积守恒：拆分后两片合计 = 同纬度带 20° 经度宽的理论等面积值
    # (原始跨线多边形直接投影会被拉成 340° 宽带，面积无业务意义)
    to_work = make_transformer("EPSG:4326", work_crs)
    reference_band = Polygon([(150, 60), (170, 60), (170, 66), (150, 66)])
    expected = _area_work(reference_band, to_work)
    got = _area_work(split, to_work)
    assert abs(got - expected) / expected < 1e-9

    # 边界 GeoPackage（地理 CRS）
    bpath = tmp_path / "basins_am.gpkg"
    gpd.GeoDataFrame(
        {"basin_id": ["AM"]}, geometry=[crossing], crs="EPSG:4326"
    ).to_file(bpath, driver="GPKG", engine="pyogrio")

    # 人口网格：等面积 CRS 下 20km 像元，覆盖两片拆分区域的工作坐标范围
    work_env = shapely.transform(
        split, lambda xy: np.column_stack(to_work.transform(xy[:, 0], xy[:, 1]))
    ).bounds
    res = 20000.0
    minx, miny, maxx, maxy = work_env
    cols = int(np.ceil((maxx - minx) / res)) + 2
    rows = int(np.ceil((maxy - miny) / res)) + 2
    pop_data = np.full((rows, cols), 100.0, dtype="float32")
    ppath = tmp_path / "pop.tif"
    with rasterio.open(
        ppath, "w", driver="GTiff", width=cols, height=rows, count=1,
        dtype="float32", crs=work_crs,
        transform=from_origin(minx - res, maxy + res, res, res),
    ) as ds:
        ds.write(pop_data, 1)

    # 水深栅格：与人口网格同网格，一半淹、一半有效零，无 NoData
    dpath = tmp_path / "depth.tif"
    depth_data = np.zeros((rows, cols), dtype="float32")
    depth_data[: rows // 2, :] = 1.5
    with rasterio.open(
        dpath, "w", driver="GTiff", width=cols, height=rows, count=1,
        dtype="float32", crs=work_crs,
        transform=from_origin(minx - res, maxy + res, res, res), nodata=-9999.0,
    ) as ds:
        ds.write(depth_data, 1)

    coll = load_basins(bpath, "v1", sha256_file(bpath), "basin_id", work_crs)
    basin = coll.by_id()["AM"]
    assert basin.usable and basin.area_work_m2 > 0

    pop = load_population(ppath, sha256_file(ppath))
    table = build_weight_table(basin, pop, work_crs)
    assert table is not None and len(table.fractions) > 0
    assert all(0.0 < f <= 1.0 + 1e-9 for f in table.fractions.values())
    # 权重覆盖面积应接近流域几何面积
    covered = sum(f * table.cell_areas_m2[k] for k, f in table.fractions.items())
    assert abs(covered - basin.area_work_m2) / basin.area_work_m2 < 0.02

    # 全流水线可发布、可追溯
    inv = InputInventory(
        depth_rasters=[DepthEntry("T0", str(dpath), sha256_file(dpath),
                                  dpath.stat().st_size)],
        boundary=BoundaryEntry("v1", str(bpath), None, "basin_id",
                               sha256_file(bpath), bpath.stat().st_size),
        population=PopulationEntry(str(ppath), sha256_file(ppath),
                                   ppath.stat().st_size),
    )
    summary = run_pipeline(inv, tmp_path / "out",
                           Config(window_tile_size=16), run_id="am")
    assert summary["gate_passed"] is True
