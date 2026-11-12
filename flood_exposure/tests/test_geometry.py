"""几何单元测试：反经线拆分、无效多边形修复、重叠检测、权重边界。"""
from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon, box

from flood_exposure.config import QualityFlag
from flood_exposure.geometry import (
    detect_overlaps,
    intersect_basin_cells,
    make_transformer,
    repair_geometry,
    split_antimeridian,
)
from rasterio.transform import from_origin


def test_split_antimeridian_produces_two_parts_and_preserves_area():
    # 跨 ±180° 的多边形：经度宽 20°、纬度 60°~70°
    crossing = Polygon([(170, 60), (-170, 60), (-170, 70), (170, 70)])
    parts = split_antimeridian(crossing)
    assert parts.geom_type == "MultiPolygon"
    assert len(parts.geoms) == 2

    to_work = make_transformer("EPSG:4326", "EPSG:6933")

    def _area(geom):
        return shapely.transform(
            geom, lambda xy: np.column_stack(
                to_work.transform(xy[:, 0], xy[:, 1]))).area

    got_area = _area(parts)

    # 等面积圆柱下，同纬度带、同经度宽度 (20°) 的面积与经度位置无关，
    # 取一个远离环绕线的 20° 带作参照
    reference = Polygon([(150, 60), (170, 60), (170, 70), (150, 70)])
    expected_area = _area(reference)
    assert abs(got_area - expected_area) / expected_area < 1e-9


def test_non_crossing_geometry_untouched():
    normal = box(100, 30, 101, 31)
    assert split_antimeridian(normal).equals(normal)


def test_repair_self_intersecting_polygon_succeeds():
    # 顶点穿入内部的自相交五边形
    bad = Polygon([(0, 10), (10, 0), (0, -2), (-10, 0), (0, -10)])
    assert not bad.is_valid
    fixed, flag = repair_geometry(bad)
    assert fixed is not None and fixed.is_valid and fixed.area > 0
    assert flag == QualityFlag.INVALID_GEOMETRY_REPAIRED


def test_repair_degenerate_linestring_fails():
    line = LineString([(0, 0), (5, 1), (10, 0)])
    fixed, flag = repair_geometry(line)
    assert fixed is None
    assert flag == QualityFlag.GEOMETRY_REPAIR_FAILED


def test_detect_overlaps_records_pair_and_areas():
    a = box(0, 0, 10, 10)
    b = box(9, 0, 19, 10)          # 与 A 重叠 1×10
    c = box(100, 100, 110, 110)    # 独立
    report = detect_overlaps(["A", "B", "C"], [a, b, c])
    assert report["C"] == []
    assert len(report["A"]) == 1 and report["A"][0]["other_basin_id"] == "B"
    assert abs(report["A"][0]["overlap_area_m2"] - 10.0) < 1e-9


def test_intersect_weights_are_fractions_and_window_independent():
    # 工作 CRS 直接用米制平面，人口格 100m
    transform = from_origin(0, 1000, 100, 100)
    identity = make_transformer("EPSG:3857", "EPSG:3857")  # 仅本测试用米制平面
    basin = box(150, 550, 650, 950)  # 跨多个格的部分覆盖矩形

    full = intersect_basin_cells(basin, transform, identity, 0, 10, 0, 10)
    # 换一组窗口边界（同一几何、同一格网）
    split_a = intersect_basin_cells(basin, transform, identity, 0, 5, 0, 10)
    split_b = intersect_basin_cells(basin, transform, identity, 5, 10, 0, 10)

    for key, w in full.weights.items():
        frac = w / full.cell_areas[key]
        assert 0.0 < frac <= 1.0 + 1e-12
        merged = (split_a.weights.get(key, 0.0) + split_b.weights.get(key, 0.0))
        assert abs(merged - w) < 1e-9

    # 完全包含的格权重恰为 1
    center_key = (2, 2)  # 格 (200,700)-(300,800) 完全在流域内
    assert abs(full.fraction(*center_key) - 1.0) < 1e-12
    # 边界穿越格严格小于 1
    edge_key = (0, 1)    # 与左边界 x=150 相交
    assert 0.0 < full.fraction(*edge_key) < 1.0
