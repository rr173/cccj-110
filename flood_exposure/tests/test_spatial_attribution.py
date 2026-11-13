"""空间归属回归测试。

这些样例专门捕捉“流域占格比例 × 格内类别比例”相乘造成的空间归属丢失：
受淹、干地、缺测与 OOB 面积落在人口格的哪一侧，必须由
area(源像元 ∩ 人口格 ∩ 流域) 决定，不能由两个独立比例相乘近似。

面积期望值全部用测试内独立的小网格几何求交（pyproj + shapely）算出，
不调用生产的重采样 / 聚合函数。
"""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
import shapely
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box

from flood_exposure.basins import load_basins
from flood_exposure.config import Config, QualityFlag
from flood_exposure.exposure import compute_basin_exposure
from flood_exposure.hashing import sha256_file
from flood_exposure.manifest import (
    BoundaryEntry, DepthEntry, InputInventory, PopulationEntry,
)
from flood_exposure.population import build_weight_table, load_population
from flood_exposure.publishing import PublishGateError, current_release
from flood_exposure.query import available_releases, load_exposure_rows, query_basin
from flood_exposure.runner import run_pipeline

WORK = "EPSG:6933"
POP_CRS = "EPSG:32648"
DEPTH_CRS = "EPSG:32649"


# --------------------------------------------------------------------------- #
# 小工具：写栅格 / 边界 / 清单
# --------------------------------------------------------------------------- #
def _write_raster(path: Path, data, crs: str, transform, nodata=None) -> None:
    arr = np.asarray(data, dtype="float32")
    h, w = arr.shape
    with rasterio.open(
        path, "w", driver="GTiff", width=w, height=h, count=1,
        dtype="float32", crs=crs, transform=transform, nodata=nodata,
    ) as ds:
        ds.write(arr, 1)


def _write_basins(path: Path, ids: list[str], geoms, crs: str) -> None:
    gpd.GeoDataFrame(
        {"basin_id": ids}, geometry=geoms, crs=crs,
    ).to_file(path, layer="basins", driver="GPKG", engine="pyogrio")


def _inventory(depth_path, basin_path, pop_path) -> InputInventory:
    d, b, p = Path(depth_path), Path(basin_path), Path(pop_path)
    return InputInventory(
        depth_rasters=[DepthEntry("T0", str(d), sha256_file(d), d.stat().st_size)],
        boundary=BoundaryEntry("v1", str(b), None, "basin_id",
                               sha256_file(b), b.stat().st_size),
        population=PopulationEntry(str(p), sha256_file(p), p.stat().st_size),
    )


def _publish(inv, out_root, *, work_crs=WORK, max_missing=0.01,
             tile=16, run_id="r"):
    cfg = Config(work_crs=work_crs, max_missing_fraction=max_missing,
                 window_tile_size=tile)
    return run_pipeline(inv, out_root, cfg, run_id=run_id)


def _direct(depth_path, basin_path, pop_path, basin_id, *,
            work_crs=WORK, tile=16, threshold=0.0):
    coll = load_basins(basin_path, "v1", sha256_file(basin_path),
                       "basin_id", work_crs)
    basin = coll.by_id()[basin_id]
    pop = load_population(pop_path, sha256_file(pop_path))
    table = build_weight_table(basin, pop, work_crs)
    return compute_basin_exposure(
        basin=basin, table=table, pop=pop, depth_path=str(depth_path),
        depth_sha256=sha256_file(depth_path), forecast_time="T0",
        boundary_version="v1", work_crs=work_crs,
        threshold=threshold, tile=tile,
    ).to_row()


# --------------------------------------------------------------------------- #
# 独立几何求交参考：不调用 flood_exposure 的重采样 / 聚合函数
# --------------------------------------------------------------------------- #
def _quad(transform, row: int, col: int, transformer: Transformer) -> Polygon:
    """栅格像元四角投影到工作 CRS 构成的四边形（独立实现）。"""
    x0 = transform.c + col * transform.a
    x1 = x0 + transform.a
    y_top = transform.f + row * transform.e
    y_bot = y_top + transform.e
    tx, ty = transformer.transform(
        [x0, x1, x1, x0], [y_top, y_top, y_bot, y_bot]
    )
    return Polygon(zip(tx, ty))


def _reproject_polygon(geom, transformer: Transformer):
    return shapely.transform(
        geom,
        lambda xy: np.column_stack(transformer.transform(xy[:, 0], xy[:, 1])),
    )


def _intersection_reference(*, pop_transform, pop_crs, pop_value, pop_cell,
                            depth_data, depth_transform, depth_crs,
                            basin_native, work_crs, threshold=0.0,
                            nodata=-9999.0) -> dict:
    """小网格面积求交得到各类面积、受淹人数与缺测比例。"""
    p2w = Transformer.from_crs(pop_crs, work_crs, always_xy=True)
    d2w = Transformer.from_crs(depth_crs, work_crs, always_xy=True)

    pop_quad = _quad(pop_transform, *pop_cell, p2w)
    basin_work = _reproject_polygon(basin_native, p2w)
    slice_ = pop_quad.intersection(basin_work)

    wet = dry = ndata = 0.0
    for r in range(depth_data.shape[0]):
        for c in range(depth_data.shape[1]):
            src_quad = _quad(depth_transform, r, c, d2w)
            inter = src_quad.intersection(slice_)
            if inter.is_empty or inter.area <= 0:
                continue
            value = float(depth_data[r, c])
            if value == float(np.float32(nodata)):
                ndata += inter.area
            elif value > threshold:
                wet += inter.area
            else:
                dry += inter.area

    oob = max(slice_.area - wet - dry - ndata, 0.0)
    denom = slice_.area
    return {
        "cell_area_m2": pop_quad.area,
        "wet_area_m2": wet,
        "dry_area_m2": dry,
        "nodata_area_m2": ndata,
        "oob_area_m2": oob,
        "missing_area_m2": ndata + oob,
        "missing_fraction": (ndata + oob) / denom if denom > 0 else 0.0,
        "exposed_population": pop_value * wet / pop_quad.area,
        "weighted_population": pop_value * slice_.area / pop_quad.area,
    }


def _assert_close(actual: dict, expected: dict, tol: float = 1.0e-6) -> None:
    for key, exp in expected.items():
        if key not in actual:
            continue  # 参考实现独有的字段 (如 cell_area_m2)
        got = float(actual[key])
        scale = max(abs(exp), 1.0)
        assert abs(got - exp) <= tol * scale, (key, got, exp)


# --------------------------------------------------------------------------- #
# 1. 原始复现：受淹只在左半格，L/R 各 25 是错的
# --------------------------------------------------------------------------- #
def test_flood_on_left_side_belongs_only_to_left_basin(tmp_path):
    pop_path = tmp_path / "pop.tif"
    pop_transform = from_origin(0.0, 10.0, 10.0, 10.0)
    _write_raster(pop_path, [[100.0]], WORK, pop_transform)

    depth_data = np.array([[2.0, 0.0], [2.0, 0.0]])  # 左列 2m，右列有效零
    depth_path = tmp_path / "depth_left.tif"
    depth_transform = from_origin(0.0, 10.0, 5.0, 5.0)
    _write_raster(depth_path, depth_data, WORK, depth_transform)

    left, right = box(0, 0, 5, 10), box(5, 0, 10, 10)
    basin_path = tmp_path / "basins_split.gpkg"
    _write_basins(basin_path, ["L", "R"], [left, right], WORK)

    out = tmp_path / "out"
    _publish(_inventory(depth_path, basin_path, pop_path), out)

    rows = {r["basin_id"]: r for r in load_exposure_rows(out)}
    assert float(rows["L"]["exposed_population"]) == pytest.approx(50.0)
    assert float(rows["R"]["exposed_population"]) == pytest.approx(0.0)
    _assert_close(rows["L"], _intersection_reference(
        pop_transform=pop_transform, pop_crs=WORK, pop_value=100.0,
        pop_cell=(0, 0), depth_data=depth_data,
        depth_transform=depth_transform, depth_crs=WORK,
        basin_native=left, work_crs=WORK))
    _assert_close(rows["R"], _intersection_reference(
        pop_transform=pop_transform, pop_crs=WORK, pop_value=100.0,
        pop_cell=(0, 0), depth_data=depth_data,
        depth_transform=depth_transform, depth_crs=WORK,
        basin_native=right, work_crs=WORK))


# --------------------------------------------------------------------------- #
# 2. NoData 只落在流域内 -> 缺测比例 1.0，闸门拒绝；旧发布仍可读
# --------------------------------------------------------------------------- #
def test_nodata_inside_basin_uses_basin_covered_area_and_keeps_old_release(tmp_path):
    pop_path = tmp_path / "pop.tif"
    pop_transform = from_origin(0.0, 10.0, 10.0, 10.0)
    _write_raster(pop_path, [[100.0]], WORK, pop_transform)

    good_depth = np.array([[2.0, 0.0], [2.0, 0.0]])
    good_path = tmp_path / "depth_good.tif"
    bad_depth = np.array([[-9999.0, 0.0], [-9999.0, 0.0]])
    bad_path = tmp_path / "depth_bad.tif"
    dt = from_origin(0.0, 10.0, 5.0, 5.0)
    _write_raster(good_path, good_depth, WORK, dt, nodata=-9999.0)
    _write_raster(bad_path, bad_depth, WORK, dt, nodata=-9999.0)

    # 唯一流域 = 左半格
    basin_path = tmp_path / "basins_left.gpkg"
    _write_basins(basin_path, ["L"], [box(0, 0, 5, 10)], WORK)

    # 先发布一份完整数据
    out = tmp_path / "out"
    _publish(_inventory(good_path, basin_path, pop_path), out,
             max_missing=0.75, run_id="good")

    # 流域内全部缺测：分母是流域覆盖面积 50 m²，不是整格 100 m²
    direct = _direct(bad_path, basin_path, pop_path, "L")
    assert float(direct["nodata_area_m2"]) == pytest.approx(50.0)
    assert float(direct["oob_area_m2"]) == pytest.approx(0.0)
    assert float(direct["missing_fraction"]) == pytest.approx(1.0)
    assert float(direct["exposed_population"]) == pytest.approx(0.0)
    assert QualityFlag.NODATA_PRESENT in direct["flags"]

    # 上限 0.75 -> 必须拒绝发布，而不是把 0.5 当作缺测比例放行
    with pytest.raises(PublishGateError):
        _publish(_inventory(bad_path, basin_path, pop_path), out,
                 max_missing=0.75, run_id="bad")

    # current 仍指向上一完整版本，旧数据可正常查询
    assert current_release(out) == out / "releases" / "good"
    assert available_releases(out) == ["good"]
    old = query_basin(out, "L")[0]
    assert float(old["exposed_population"]) == pytest.approx(50.0)
    assert old["gate_passed"] == "True"


# --------------------------------------------------------------------------- #
# 3. 把洪水与缺测移到流域另一侧
# --------------------------------------------------------------------------- #
def test_flood_and_nodata_moved_to_other_side(tmp_path):
    pop_path = tmp_path / "pop.tif"
    pop_transform = from_origin(0.0, 10.0, 10.0, 10.0)
    _write_raster(pop_path, [[100.0]], WORK, pop_transform)

    dt = from_origin(0.0, 10.0, 5.0, 5.0)
    moved_flood = np.array([[0.0, 2.0], [0.0, 2.0]])     # 右列淹
    moved_nodata = np.array([[0.0, -9999.0], [0.0, -9999.0]])  # 右列缺测
    flood_path = tmp_path / "depth_right_wet.tif"
    nodata_path = tmp_path / "depth_right_nodata.tif"
    _write_raster(flood_path, moved_flood, WORK, dt, nodata=-9999.0)
    _write_raster(nodata_path, moved_nodata, WORK, dt, nodata=-9999.0)

    left, right = box(0, 0, 5, 10), box(5, 0, 10, 10)
    basin_path = tmp_path / "basins_split.gpkg"
    _write_basins(basin_path, ["L", "R"], [left, right], WORK)

    # 洪水移到右侧：L 完全不受淹，R 得到全部 50 人
    l_flood = _direct(flood_path, basin_path, pop_path, "L")
    r_flood = _direct(flood_path, basin_path, pop_path, "R")
    assert float(l_flood["wet_area_m2"]) == pytest.approx(0.0)
    assert float(l_flood["exposed_population"]) == pytest.approx(0.0)
    assert float(r_flood["wet_area_m2"]) == pytest.approx(50.0)
    assert float(r_flood["exposed_population"]) == pytest.approx(50.0)
    _assert_close(l_flood, _intersection_reference(
        pop_transform=pop_transform, pop_crs=WORK, pop_value=100.0,
        pop_cell=(0, 0), depth_data=moved_flood, depth_transform=dt,
        depth_crs=WORK, basin_native=left, work_crs=WORK))
    _assert_close(r_flood, _intersection_reference(
        pop_transform=pop_transform, pop_crs=WORK, pop_value=100.0,
        pop_cell=(0, 0), depth_data=moved_flood, depth_transform=dt,
        depth_crs=WORK, basin_native=right, work_crs=WORK))

    # 缺测移到右侧：L 没有缺测；R 全缺测 -> 缺测比例 1.0
    l_nd = _direct(nodata_path, basin_path, pop_path, "L")
    r_nd = _direct(nodata_path, basin_path, pop_path, "R")
    assert float(l_nd["nodata_area_m2"]) == pytest.approx(0.0)
    assert float(l_nd["missing_fraction"]) == pytest.approx(0.0)
    assert QualityFlag.NODATA_PRESENT not in l_nd["flags"]
    assert float(r_nd["nodata_area_m2"]) == pytest.approx(50.0)
    assert float(r_nd["missing_fraction"]) == pytest.approx(1.0)
    assert QualityFlag.NODATA_PRESENT in r_nd["flags"]


# --------------------------------------------------------------------------- #
# 4. 三角形边界横穿人口格
# --------------------------------------------------------------------------- #
def test_triangle_boundary_crossing_cell_uses_exact_overlap(tmp_path):
    pop_path = tmp_path / "pop.tif"
    pop_transform = from_origin(0.0, 10.0, 10.0, 10.0)
    _write_raster(pop_path, [[100.0]], WORK, pop_transform)

    # 下排 2m 淹、上排有效零；三角形只覆盖左下 / 对角线以下部分
    depth_data = np.array([[0.0, 0.0], [2.0, 2.0]])
    depth_path = tmp_path / "depth_bottom_wet.tif"
    dt = from_origin(0.0, 10.0, 5.0, 5.0)
    _write_raster(depth_path, depth_data, WORK, dt)

    tri = Polygon([(0.0, 0.0), (10.0, 0.0), (0.0, 10.0)])
    basin_path = tmp_path / "basins_tri.gpkg"
    _write_basins(basin_path, ["TRI"], [tri], WORK)

    out = tmp_path / "out"
    _publish(_inventory(depth_path, basin_path, pop_path), out,
             max_missing=0.01, run_id="tri")
    row = load_exposure_rows(out)[0]

    # 独立求交：湿 37.5 m²、干 12.5 m²、覆盖 50 m²、受淹 37.5 人
    ref = _intersection_reference(
        pop_transform=pop_transform, pop_crs=WORK, pop_value=100.0,
        pop_cell=(0, 0), depth_data=depth_data, depth_transform=dt,
        depth_crs=WORK, basin_native=tri, work_crs=WORK)
    _assert_close(row, ref)
    assert float(row["exposed_population"]) == pytest.approx(37.5)
    assert float(row["wet_area_m2"]) == pytest.approx(37.5)
    assert float(row["dry_area_m2"]) == pytest.approx(12.5)
    assert float(row["missing_fraction"]) == pytest.approx(0.0)

    # 三角边界确实横穿人口格两侧：既非整格，也非零
    direct = _direct(depth_path, basin_path, pop_path, "TRI")
    assert float(direct["wet_area_m2"]) == pytest.approx(37.5)
    assert float(direct["dry_area_m2"]) == pytest.approx(12.5)


# --------------------------------------------------------------------------- #
# 5. 水深范围只覆盖流域一部分（OOB）
# --------------------------------------------------------------------------- #
def test_depth_extent_covering_only_part_of_basin(tmp_path):
    pop_path = tmp_path / "pop.tif"
    pop_transform = from_origin(0.0, 10.0, 10.0, 10.0)
    _write_raster(pop_path, [[100.0]], WORK, pop_transform)

    # 水深只覆盖 x∈[0,5]（左半），左半全部 2m 淹
    depth_data = np.array([[2.0], [2.0]])
    depth_path = tmp_path / "depth_left_column.tif"
    dt = from_origin(0.0, 10.0, 5.0, 5.0)
    _write_raster(depth_path, depth_data, WORK, dt)

    full = box(0, 0, 10, 10)
    right = box(5, 0, 10, 10)
    full_path = tmp_path / "basins_full.gpkg"
    right_path = tmp_path / "basins_right.gpkg"
    _write_basins(full_path, ["FULL"], [full], WORK)
    _write_basins(right_path, ["RIGHT"], [right], WORK)

    # 流域 = 整格：受淹 50、OOB 50、缺测比例 0.5
    direct_full = _direct(depth_path, full_path, pop_path, "FULL")
    assert float(direct_full["wet_area_m2"]) == pytest.approx(50.0)
    assert float(direct_full["oob_area_m2"]) == pytest.approx(50.0)
    assert float(direct_full["missing_fraction"]) == pytest.approx(0.5)
    assert float(direct_full["exposed_population"]) == pytest.approx(50.0)
    assert QualityFlag.OUT_OF_DEPTH_EXTENT in direct_full["flags"]
    _assert_close(direct_full, _intersection_reference(
        pop_transform=pop_transform, pop_crs=WORK, pop_value=100.0,
        pop_cell=(0, 0), depth_data=depth_data, depth_transform=dt,
        depth_crs=WORK, basin_native=full, work_crs=WORK))

    # 流域 = 右半格（水深范围之外）：洪水其实不在流域内 -> 0 人受淹，
    # 覆盖面积全部是 OOB，缺测比例 1.0
    direct_right = _direct(depth_path, right_path, pop_path, "RIGHT")
    assert float(direct_right["wet_area_m2"]) == pytest.approx(0.0)
    assert float(direct_right["oob_area_m2"]) == pytest.approx(50.0)
    assert float(direct_right["missing_fraction"]) == pytest.approx(1.0)
    assert float(direct_right["exposed_population"]) == pytest.approx(0.0)
    _assert_close(direct_right, _intersection_reference(
        pop_transform=pop_transform, pop_crs=WORK, pop_value=100.0,
        pop_cell=(0, 0), depth_data=depth_data, depth_transform=dt,
        depth_crs=WORK, basin_native=right, work_crs=WORK))


# --------------------------------------------------------------------------- #
# 6. 位置不对齐的网格：不同工作坐标系 + 不同读取块大小复算
# --------------------------------------------------------------------------- #
def _misaligned_inputs(tmp_path):
    """人口格与水深像元不对齐，且水深 CRS 与人口 CRS 不同。"""
    pop_transform = from_origin(500000.0, 3390000.0, 10.0, 10.0)
    pop_path = tmp_path / "pop_utm48.tif"
    _write_raster(pop_path, [[100.0]], POP_CRS, pop_transform)

    to_depth = Transformer.from_crs(POP_CRS, DEPTH_CRS, always_xy=True)
    corners_x = [500000.0, 500010.0, 500010.0, 500000.0]
    corners_y = [3390000.0, 3390000.0, 3389990.0, 3389990.0]
    tx, ty = to_depth.transform(corners_x, corners_y)

    pad = 5.0
    res = 1.0
    minx, maxx = min(tx) - pad, max(tx) + pad
    miny, maxy = min(ty) - pad, max(ty) + pad
    width = int(np.ceil((maxx - minx) / res))
    height = int(np.ceil((maxy - miny) / res))
    depth_transform = from_origin(minx, maxy, res, res)

    # 水深在 UTM49N 里按“西侧淹、东侧有效零”分列。这个分界放在 UTM49N
    # 中 1/3 处，在人口 CRS / 工作 CRS 中与人口格中线显著斜交 —— 真正
    # 的位置不对齐，而不是恰好各占一半。
    split_x = minx + (maxx - minx) / 3.0
    xs = minx + (np.arange(width) + 0.5) * res
    ys = maxy - (np.arange(height) + 0.5) * res
    gx, gy = np.meshgrid(xs, ys)
    depth_data = np.where(gx < split_x, 2.0, 0.0).astype("float32")
    depth_path = tmp_path / "depth_utm49_misaligned.tif"
    _write_raster(depth_path, depth_data, DEPTH_CRS, depth_transform)

    # 流域按人口 CRS 的格中线拆分（与水深分界并不重合）
    x0, y_top = 500000.0, 3390000.0
    left = box(x0, y_top - 10.0, x0 + 5.0, y_top)
    right = box(x0 + 5.0, y_top - 10.0, x0 + 10.0, y_top)
    basin_path = tmp_path / "basins_utm48.gpkg"
    _write_basins(basin_path, ["L", "R"], [left, right], POP_CRS)

    return {
        "pop_path": pop_path,
        "pop_transform": pop_transform,
        "depth_path": depth_path,
        "depth_data": depth_data,
        "depth_transform": depth_transform,
        "basin_path": basin_path,
        "left": left,
        "right": right,
    }


def _misaligned_reference(case, basin_native, work_crs):
    return _intersection_reference(
        pop_transform=case["pop_transform"], pop_crs=POP_CRS, pop_value=100.0,
        pop_cell=(0, 0), depth_data=case["depth_data"],
        depth_transform=case["depth_transform"], depth_crs=DEPTH_CRS,
        basin_native=basin_native, work_crs=work_crs)


@pytest.mark.parametrize("work_crs", ["EPSG:6933", "EPSG:6931"])
def test_misaligned_grids_keep_spatial_attribution(tmp_path, work_crs):
    case = _misaligned_inputs(tmp_path)
    inv = _inventory(case["depth_path"], case["basin_path"], case["pop_path"])

    per_tile: dict[int, dict[str, dict]] = {}
    for tile in (16, 32):
        out = tmp_path / f"out_{tile}"
        _publish(inv, out, work_crs=work_crs, max_missing=0.01,
                 tile=tile, run_id="aligned")
        per_tile[tile] = {
            r["basin_id"]: r for r in load_exposure_rows(out)
        }

    # 每个 basin 的结果与独立求交一致（不同工作 CRS 各自算各自的期望）
    for bid, geom in (("L", case["left"]), ("R", case["right"])):
        ref = _misaligned_reference(case, geom, work_crs)
        for tile in (16, 32):
            _assert_close(per_tile[tile][bid], ref, tol=1.0e-6)

    # 不同读取块大小结果一致
    for bid in ("L", "R"):
        for field in ("exposed_population", "wet_area_m2", "dry_area_m2",
                      "missing_fraction", "weighted_population"):
            a = float(per_tile[16][bid][field])
            b = float(per_tile[32][bid][field])
            assert abs(a - b) <= 1.0e-6 * max(abs(a), abs(b), 1.0), (bid, field)

    # 关键回归：空间归属没有被平均掉。流域按人口格中线拆分，水深按
    # UTM49N 的 1/3 分界，两条分界斜交。若退化为“流域份额 × 格内受淹
    # 比例”，L 与 R 都会得到 0.5×0.5×100 = 25 人；真实相交下 L 明显
    # 多于 R，且二者总计仍是格内确认受淹的面积份额。
    l_pop = float(per_tile[16]["L"]["exposed_population"])
    r_pop = float(per_tile[16]["R"]["exposed_population"])
    assert abs(l_pop - r_pop) > 1.0
    assert abs(l_pop - 25.0) > 0.5 or abs(r_pop - 25.0) > 0.5
    # 总的确认受淹面积由水深覆盖决定，与流域怎么切无关
    l_wet = float(per_tile[16]["L"]["wet_area_m2"])
    r_wet = float(per_tile[16]["R"]["wet_area_m2"])
    expected_wet = (float(_misaligned_reference(case, case["left"], work_crs)["wet_area_m2"])
                    + float(_misaligned_reference(case, case["right"], work_crs)["wet_area_m2"]))
    assert abs(l_wet + r_wet - expected_wet) < 1.0
