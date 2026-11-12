"""栅格重采样测试：有效零/NoData 区分、窗口不变性、分块顺序一致性、参考对照。"""
from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import from_origin

from flood_exposure.geometry import make_transformer
from flood_exposure.raster import (
    AreaWeightedResampler,
    iter_windows,
    resample_depth_windowed,
)
from flood_exposure.reference import lattice_reference

WORK = "EPSG:6933"


def _write_depth(path, data, crs, res, origin, nodata=-9999.0):
    h, w = data.shape
    with rasterio.open(
        path, "w", driver="GTiff", width=w, height=h, count=1,
        dtype="float32", crs=crs,
        transform=from_origin(origin[0], origin[1], res, res), nodata=nodata,
    ) as ds:
        ds.write(data.astype("float32"), 1)


def test_valid_zero_vs_nodata_are_distinguished(tmp_path):
    # 源 2x2、100m 像元，覆盖与人口格相同的 200m×200m 范围；
    # 左列有效零、右列 NoData，各占目标格一半面积
    depth = np.array([[0.0, -9999.0],
                      [0.0, -9999.0]], dtype="float32")
    dpath = tmp_path / "d.tif"
    _write_depth(dpath, depth, WORK, 100.0, (0, 200))
    pop_transform = from_origin(0, 200, 200, 200)

    res = resample_depth_windowed(
        dpath, pop_transform, WORK, WORK, {(0, 0)}, threshold=0.0, tile=16
    )
    cell = res[(0, 0)]
    # 左半确认未淹 (有效零)，右半 NoData；二者面积各半
    assert abs(cell["dry_area_m2"] - 20000.0) < 1e-6
    assert abs(cell["nodata_area_m2"] - 20000.0) < 1e-6
    assert cell["wet_area_m2"] == 0.0
    assert cell["oob_area_m2"] == 0.0
    assert abs(cell["nodata_fraction"] - 0.5) < 1e-9
    assert abs(cell["missing_fraction"] - 0.5) < 1e-9


def test_wet_threshold_and_wet_fraction(tmp_path):
    # 左列水深 2m (淹)、右列有效零 (干)，无 NoData
    depth = np.array([[2.0, 0.0],
                      [2.0, 0.0]], dtype="float32")
    dpath = tmp_path / "d.tif"
    _write_depth(dpath, depth, WORK, 100.0, (0, 200))
    pop_transform = from_origin(0, 200, 200, 200)
    res = resample_depth_windowed(
        dpath, pop_transform, WORK, WORK, {(0, 0)}, threshold=0.5, tile=16
    )
    cell = res[(0, 0)]
    assert abs(cell["wet_fraction"] - 0.5) < 1e-9
    assert cell["nodata_fraction"] == 0.0
    assert cell["missing_fraction"] == 0.0


def test_window_tile_does_not_change_weights(tmp_path):
    rng = np.random.default_rng(7)
    data = rng.choice([0.0, 1.5, -9999.0], size=(40, 40), p=[0.5, 0.4, 0.1])
    dpath = tmp_path / "d.tif"
    _write_depth(dpath, data, WORK, 25.0, (0, 1000))
    pop_transform = from_origin(0, 1000, 100, 100)
    cells = {(r, c) for r in range(0, 10) for c in range(0, 10)}

    small = resample_depth_windowed(
        dpath, pop_transform, WORK, WORK, cells, 0.0, tile=8)
    large = resample_depth_windowed(
        dpath, pop_transform, WORK, WORK, cells, 0.0, tile=256)
    for key in cells:
        for field_ in ("wet_fraction", "nodata_fraction", "missing_fraction",
                       "wet_area_m2", "nodata_area_m2"):
            assert abs(small[key][field_] - large[key][field_]) < 1e-6, key


def test_chunk_order_does_not_change_result(tmp_path):
    rng = np.random.default_rng(11)
    data = rng.choice([0.0, 1.2, -9999.0], size=(24, 24), p=[0.45, 0.45, 0.1])
    dpath = tmp_path / "d.tif"
    _write_depth(dpath, data, WORK, 25.0, (0, 600))
    pop_transform = from_origin(0, 600, 100, 100)
    cells = {(r, c) for r in range(6) for c in range(6)}

    def run(order):
        with rasterio.open(dpath) as ds:
            r = AreaWeightedResampler(
                ds, pop_transform, WORK, WORK, cells, 0.0, tile=8)
            windows = list(iter_windows(r.info.width, r.info.height, r.tile))
            for w in order(windows):
                r._process_window(*w)
            return r.finalize()

    forward = run(list)
    reverse = run(lambda ws: list(reversed(ws)))
    shuffled = run(lambda ws: [ws[i] for i in np.random.permutation(len(ws))])
    for key in cells:
        for field_ in ("wet_area_m2", "dry_area_m2", "nodata_area_m2",
                       "wet_fraction", "missing_fraction"):
            assert abs(forward[key][field_] - reverse[key][field_]) < 1e-9
            assert abs(forward[key][field_] - shuffled[key][field_]) < 1e-9


def test_lattice_reference_agrees_with_exact_pipeline(sample_dir, layout):
    depth = layout["files"]["depth_T00"]
    # 取 B 流域窗口内的若干跨界人口格
    cells = {(r, c) for r in range(2, 6) for c in range(4, 8)}
    pop_transform = from_origin(500000.0, 3390000.0, 100.0, 100.0)

    exact = resample_depth_windowed(
        depth, pop_transform, layout["population_crs"], WORK,
        cells, threshold=0.0, tile=64)
    ref = lattice_reference(
        depth, pop_transform, layout["population_crs"], WORK,
        cells, threshold=0.0, k=48)

    for key in cells:
        for field_ in ("wet_fraction", "nodata_fraction", "missing_fraction"):
            # 点阵法 O(1/K) 误差，K=48 时限 0.06
            assert abs(exact[key][field_] - ref[key][field_]) < 0.06, (key, field_)
