"""几何处理：反经线拆分、无效多边形修复、等面积投影、格网相交权重。

面积权重的定义 (业务约定)
-------------------------
所有面积都在等面积投影 (默认 EPSG:6933) 下计算。对人口网格的一个像元
P（其四角从人口网格原生 CRS 投影到工作 CRS 后构成的四边形）与流域多边形 B：

    w(B, P) = area(intersection(B, P)) / area(P)

即“该人口格落在流域内的面积比例”。当相邻流域边界轻微重叠时，同一个人口格
会与多个流域分别求交，各流域只拿到自己的相交份额 w，而不是整格人口，
因此不会被相邻流域重复全额计数。重叠区域的各份额之和可能 >1，重叠对会被
显式记录并打上 OVERLAP_WEIGHTED 标记。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import shapely
from pyproj import CRS, Transformer
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Polygon,
    box,
    shape,
)
from shapely.ops import unary_union

from .config import QualityFlag

_EARTH_LON_SPAN = 180.0


# --------------------------------------------------------------------------- #
# 反经线 (antimeridian) 处理
# --------------------------------------------------------------------------- #
def split_antimeridian(geom: shapely.Geometry) -> shapely.Geometry:
    """拆分跨越 ±180° 经线的几何。

    判定依据：地理坐标下经度跨度 > 180°，说明坐标沿最短路径跨越了环绕线。
    做法是把西经度整体 +360 使几何在 [0,360] 空间连续，在 180° 处切开后
    再把东半球部分平移回 [-180,180]。拆分结果是 MultiPolygon，可被后续
    投影与相交流程正常处理。
    """
    if geom.is_empty:
        return geom
    minx, _, maxx, _ = geom.bounds
    if (maxx - minx) <= _EARTH_LON_SPAN:
        return geom

    geom360 = shapely.transform(
        geom, lambda xy: np.column_stack(
            (np.where(xy[:, 0] < 0, xy[:, 0] + 360.0, xy[:, 0]), xy[:, 1]))
    )
    west_half = box(0.0, -90.0, 180.0, 90.0)
    east_half = box(180.0, -90.0, 360.0, 90.0)

    parts: list[shapely.Geometry] = []
    p_west = geom360.intersection(west_half)
    if not p_west.is_empty:
        parts.append(p_west)
    p_east = geom360.intersection(east_half)
    if not p_east.is_empty:
        p_east = shapely.transform(
            p_east, lambda xy: np.column_stack((xy[:, 0] - 360.0, xy[:, 1])))
        parts.append(p_east)
    if not parts:
        return geom
    return unary_union(parts)


# --------------------------------------------------------------------------- #
# 无效多边形修复
# --------------------------------------------------------------------------- #
def _polygonal_parts(geom: shapely.Geometry) -> list[Polygon]:
    """从任意几何中抽出有面积的 Polygon 部分，剔除线/点/空几何。"""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom] if geom.area > 0 else []
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if isinstance(g, Polygon) and g.area > 0]
    if isinstance(geom, GeometryCollection):
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_polygonal_parts(g))
        return out
    return []


def repair_geometry(geom: shapely.Geometry) -> tuple[shapely.Geometry | None, str | None]:
    """修复无效多边形。

    返回 (修复后的几何或 None, 标记)。修复失败 (无法得到任何有面积的
    多边形，例如自相交且 make_valid 后只剩线串) 时返回 (None,
    GEOMETRY_REPAIR_FAILED)，调用方必须跳过该流域而不是静默继续。
    """
    was_invalid = not geom.is_valid
    candidate = geom
    if was_invalid:
        # shapely.make_valid 对应 GEOS MakeValid，保留输入位置信息
        candidate = shapely.make_valid(geom)
    candidate = split_antimeridian(candidate)
    if not candidate.is_valid:
        candidate = shapely.make_valid(candidate)
        candidate = split_antimeridian(candidate)

    parts = _polygonal_parts(candidate)
    if not parts:
        return None, QualityFlag.GEOMETRY_REPAIR_FAILED
    fixed = unary_union(parts)
    if fixed.is_empty or fixed.area <= 0:
        return None, QualityFlag.GEOMETRY_REPAIR_FAILED
    flag = QualityFlag.INVALID_GEOMETRY_REPAIRED if was_invalid else None
    return fixed, flag


# --------------------------------------------------------------------------- #
# CRS 投影
# --------------------------------------------------------------------------- #
def make_transformer(src_crs: str | CRS, dst_crs: str | CRS) -> Transformer:
    """ always_xy=True 保证输入输出都是 (经度/x, 纬度/y) 顺序。"""
    return Transformer.from_crs(CRS.from_user_input(src_crs),
                                CRS.from_user_input(dst_crs),
                                always_xy=True)


def reproject_geometry(geom: shapely.Geometry, transformer: Transformer) -> shapely.Geometry:
    def _f(xy: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(xy[:, 0], xy[:, 1])
        return np.column_stack((x, y))

    return shapely.transform(geom, _f)


def assert_equal_area(crs: str | CRS) -> None:
    c = CRS.from_user_input(crs)
    if not c.is_geographic and c.coordinate_operation is not None:
        # pyproj 对投影 CRS 用轴信息 + 方法名判断；等面积方法名含 Equal Area
        method = (c.coordinate_operation.method_name or "").lower()
        if "equal area" in method or "eqc" in method:
            return
    # 兜底：检查 CRS 名称
    name = (c.name or "").lower()
    if "equal area" in name or "ease" in name:
        return
    raise ValueError(f"工作 CRS 必须是等面积投影，当前为: {c}")


# --------------------------------------------------------------------------- #
# 人口格四边形 (工作 CRS)
# --------------------------------------------------------------------------- #
def cell_corners(transform, row: int, col: int) -> tuple[float, float, float, float,
                                                         float, float, float, float]:
    """返回栅格像元四角在其原生 CRS 下的坐标 (x0,y0,x1,y1,... 顺时针)。"""
    x_left = transform.c + col * transform.a
    x_right = x_left + transform.a
    y_top = transform.f + row * transform.e
    y_bottom = y_top + transform.e
    return (x_left, y_top, x_right, y_top, x_right, y_bottom,
            x_left, y_bottom)


def cell_footprint(transform, transformer: Transformer, row: int, col: int) -> Polygon:
    """把像元四角投影到工作 CRS，构成 (可能轻微弯曲的) 四边形。"""
    x0, y0, x1, y1, x2, y2, x3, y3 = cell_corners(transform, row, col)
    xs_src = (x0, x1, x2, x3)
    ys_src = (y0, y1, y2, y3)
    xs, ys = transformer.transform(xs_src, ys_src)
    return Polygon(zip(xs, ys))


@dataclass
class CellWeights:
    """一个流域与一个窗口内人口格的相交结果。

    weights: (row, col) -> 相交面积 (m²，工作 CRS)
    cell_areas: (row, col) -> 像元自身面积 (m²)
    """

    weights: dict[tuple[int, int], float] = field(default_factory=dict)
    cell_areas: dict[tuple[int, int], float] = field(default_factory=dict)

    def fraction(self, row: int, col: int) -> float:
        area = self.cell_areas[(row, col)]
        return self.weights[(row, col)] / area if area > 0 else 0.0


def intersect_basin_cells(
    basin_geom_work: shapely.Geometry,
    pop_transform,
    pop_to_work: Transformer,
    row_start: int,
    row_stop: int,
    col_start: int,
    col_stop: int,
    min_area: float = 1.0e-6,
) -> CellWeights:
    """对给定窗口内的每个人口格，计算与流域的等面积相交面积。

    窗口只决定枚举哪些像元；每个像元的四边形与权重由几何唯一确定，
    因此窗口边界切在哪里都不会改变权重值。
    """
    out = CellWeights()
    prep = shapely.prepared.prep(basin_geom_work)
    for row in range(row_start, row_stop):
        for col in range(col_start, col_stop):
            quad = cell_footprint(pop_transform, pop_to_work, row, col)
            cell_area = quad.area
            if cell_area <= 0:
                continue
            # prepared 几何先做快速包含/相交判定，避免无谓的 intersection
            if prep.contains(quad):
                inter_area = cell_area
            elif prep.intersects(quad):
                inter = basin_geom_work.intersection(quad)
                inter_area = inter.area
            else:
                continue
            if inter_area > min_area:
                out.weights[(row, col)] = inter_area
                out.cell_areas[(row, col)] = cell_area
    return out


# --------------------------------------------------------------------------- #
# 流域重叠检测
# --------------------------------------------------------------------------- #
def detect_overlaps(
    basin_ids: list[str], basin_geoms_work: list[shapely.Geometry]
) -> dict[str, list[dict]]:
    """返回 basin_id -> 与之重叠的相邻流域列表 (含重叠面积 m²)。

    只做两两精确相交；轻微重叠也会被记录，供分摊逻辑与质量标记使用。
    """
    overlap_map: dict[str, list[dict]] = {bid: [] for bid in basin_ids}
    n = len(basin_ids)
    for i in range(n):
        gi = basin_geoms_work[i]
        if gi is None:
            continue
        for j in range(i + 1, n):
            gj = basin_geoms_work[j]
            if gj is None or not gi.intersects(gj):
                continue
            inter = gi.intersection(gj)
            area = inter.area
            if area > 0:
                overlap_map[basin_ids[i]].append(
                    {"other_basin_id": basin_ids[j], "overlap_area_m2": area}
                )
                overlap_map[basin_ids[j]].append(
                    {"other_basin_id": basin_ids[i], "overlap_area_m2": area}
                )
    return overlap_map
