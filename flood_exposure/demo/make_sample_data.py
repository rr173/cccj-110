"""生成本地合成样例数据。

样例覆盖需求中的全部情形（均为小网格，可肉眼核对）：

* 人口网格  : EPSG:32648 (UTM 48N)，100m 像元，含一个 NoData 格；
* 水深栅格  : EPSG:32649 (UTM 49N，不同 CRS)，50m 像元、不同仿射变换，
              两个预报时次，含有效零值带与显式 NoData 带；
* 边界 v1   : GeoPackage，4 个流域，A/B 轻微重叠，含 1 个无效但可修复
              的自相交多边形；
* 边界 v2   : 流域 B 局部修订（几何指纹变化），原可修复多边形替换为
              修复失败的退化“蝴蝶结”，新增一个小流域 E。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Polygon, mapping
from pyproj import CRS

POP_CRS = "EPSG:32648"
DEPTH_CRS = "EPSG:32649"
GEO_CRS = "EPSG:4326"

# 统一的地理坐标舞台（约 100.20°E, 30.60°N 一带，两个 UTM 带都能表达）
# 人口网格：原点 (500000, 3390000) UTM48N，12 列 × 10 行，100m
POP_ORIGIN_X, POP_ORIGIN_Y, POP_RES, POP_W, POP_H = 500000.0, 3390000.0, 100.0, 12, 10
DEPTH_RES = 50.0


def _transformer(src: str, dst: str):
    from pyproj import Transformer
    return Transformer.from_crs(CRS.from_user_input(src),
                                CRS.from_user_input(dst), always_xy=True)


def write_population(path: Path) -> None:
    rng = np.random.default_rng(42)
    data = rng.integers(80, 120, size=(POP_H, POP_W)).astype("float32")
    # 一个显式 NoData 格 (第 4 行第 3 列)
    data[4, 3] = -9999.0
    transform = from_origin(POP_ORIGIN_X, POP_ORIGIN_Y, POP_RES, POP_RES)
    with rasterio.open(
        path, "w", driver="GTiff", width=POP_W, height=POP_H,
        count=1, dtype="float32", crs=POP_CRS, transform=transform,
        nodata=-9999.0,
    ) as ds:
        ds.write(data, 1)


def _depth_grid_bounds():
    """把人口网格的地理范围转到 UTM49N，外扩一圈作为水深栅格范围。"""
    t48_49 = _transformer(POP_CRS, DEPTH_CRS)
    corners_native = [
        (POP_ORIGIN_X, POP_ORIGIN_Y),
        (POP_ORIGIN_X + POP_W * POP_RES, POP_ORIGIN_Y),
        (POP_ORIGIN_X + POP_W * POP_RES, POP_ORIGIN_Y - POP_H * POP_RES),
        (POP_ORIGIN_X, POP_ORIGIN_Y - POP_H * POP_RES),
    ]
    xs, ys = t48_49.transform([c[0] for c in corners_native],
                              [c[1] for c in corners_native])
    # 跨 UTM 带重投影后网格方向不再轴对齐，四个方向都要留出足够余量，
    # 保证任何人口格的投影四边形都完整落在水深栅格范围内 (除显式 NoData 带)
    pad = 250.0
    return min(xs) - pad, max(ys) + pad, max(xs) + pad, min(ys) - pad


def write_depth(path: Path, time_index: int) -> None:
    minx, maxy, maxx, miny = _depth_grid_bounds()
    width = int(round((maxx - minx) / DEPTH_RES))
    height = int(round((maxy - miny) / DEPTH_RES))
    transform = from_origin(minx, maxy, DEPTH_RES, DEPTH_RES)

    # 构造有结构的水深场：一条横向洪水带 + 有效零值区 + NoData 带。
    # 栅格行号向下递增、纬度递减；行 0 在北侧 (y=maxy)。
    cols, rows = np.meshgrid(np.arange(width), np.arange(height))
    # 洪水带随时间向南扩张 (行号增大方向)
    flood_center = height * (0.45 + 0.12 * time_index)
    half_width = height * (0.10 + 0.03 * time_index)
    depth = np.where(
        np.abs(rows - flood_center) <= half_width,
        # 高斯状水深 0.2m ~ 2.5m (横向带内再叠加一点东西向变化)
        (2.5
         * np.exp(-((rows - flood_center) / (half_width + 1e-9)) ** 2)
         * (0.85 + 0.15 * np.cos(np.pi * cols / width))
         ).astype("float32"),
        0.0,  # 有效零值：模型覆盖且确认未淹
    ).astype("float32")

    # 显式 NoData 带：最右侧 6 列 (~300m)，业务含义“模型未覆盖，不知道”。
    # 跨带投影错位下仍能覆盖人口网格最右两列，C 流域因此必然出现 NODATA 标记。
    depth[:, -6:] = -9999.0
    # 第二个时次再扩大 NoData (左下角一块)，用于演示缺测比例随时间变化
    if time_index == 1:
        depth[-4:, 0:3] = -9999.0

    with rasterio.open(
        path, "w", driver="GTiff", width=width, height=height,
        count=1, dtype="float32", crs=DEPTH_CRS, transform=transform,
        nodata=-9999.0,
    ) as ds:
        ds.write(depth, 1)


# --------------------------------------------------------------------------- #
# 边界：先在 UTM48N 平面里画，再转 GeoPackage（几何以 EPSG:32648 存储）
# --------------------------------------------------------------------------- #
def _rect(x0, y0, x1, y1) -> Polygon:
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _bowtie(cx, cy, size) -> Polygon:
    """蝴蝶结自相交多边形 (保留用于其他测试)。"""
    return Polygon([
        (cx - size, cy + size), (cx + size, cy + size),
        (cx - size, cy - size), (cx + size, cy - size),
    ])


def _degenerate_line(cx, cy, size):
    """退化几何：只有线串、没有任何面状部分。

    make_valid 无法从中恢复出有面积的多边形，修复流程必须返回
    GEOMETRY_REPAIR_FAILED 并跳过该流域，而不是静默当作空流域。
    """
    from shapely.geometry import LineString
    return LineString([(cx - size, cy), (cx, cy + size * 0.3), (cx + size, cy)])


def _self_intersecting(cx, cy, size) -> Polygon:
    """自相交但 make_valid 可恢复出有面积多边形的样本。"""
    # 五边形顶点顺序故意交叉，修复后得到带切口的有效多边形
    return Polygon([
        (cx, cy + size),
        (cx + size, cy),
        (cx, cy - size * 0.2),       # 顶点穿入内部造成自相交
        (cx - size, cy),
        (cx, cy - size),
    ])


def write_boundaries(v1_path: Path, v2_path: Path) -> None:
    import geopandas as gpd

    x0, y0 = POP_ORIGIN_X, POP_ORIGIN_Y
    r = POP_RES
    # 流域 A: 左 4 列宽 × 上 6 行高
    a = _rect(x0, y0, x0 + 4 * r, y0 - 6 * r)
    # 流域 B: 与 A 重叠 0.5 个像元（列 3.5~4），v1
    b_v1 = _rect(x0 + 3.5 * r, y0 - 1 * r, x0 + 8 * r, y0 - 7 * r)
    # 流域 C: 右下角，独立
    c = _rect(x0 + 8 * r, y0 - 6 * r, x0 + 12 * r, y0 - 10 * r)
    # 流域 D: 自相交但可修复
    d = _self_intersecting(x0 + 10.5 * r, y0 - 2 * r, r)

    gdf_v1 = gpd.GeoDataFrame(
        {"basin_id": ["A", "B", "C", "D"],
         "name": ["上游", "中游", "下游", "弯曲流域"]},
        geometry=[a, b_v1, c, d], crs=POP_CRS,
    )
    gdf_v1.to_file(v1_path, layer="basins", driver="GPKG", engine="pyogrio")

    # v2：B 局部修订（东扩 1 像元）；D 换成修复失败的退化线串；新增 E
    b_v2 = _rect(x0 + 3.5 * r, y0 - 1 * r, x0 + 9 * r, y0 - 7 * r)
    d_bad = _degenerate_line(x0 + 10.5 * r, y0 - 2 * r, r * 0.8)
    e = _rect(x0 + 0.2 * r, y0 - 6.2 * r, x0 + 3 * r, y0 - 9.5 * r)
    gdf_v2 = gpd.GeoDataFrame(
        {"basin_id": ["A", "B", "C", "D", "E"],
         "name": ["上游", "中游-修订", "下游", "退化流域", "新增小流域"]},
        geometry=[a, b_v2, c, d_bad, e], crs=POP_CRS,
    )
    gdf_v2.to_file(v2_path, layer="basins", driver="GPKG", engine="pyogrio")


def generate(base: Path) -> dict:
    base.mkdir(parents=True, exist_ok=True)
    rasters = base / "rasters"
    boundaries = base / "boundaries"
    rasters.mkdir(exist_ok=True)
    boundaries.mkdir(exist_ok=True)

    pop_path = rasters / "population_utm48n_100m.tif"
    d1 = rasters / "depth_T00_utm49n_50m.tif"
    d2 = rasters / "depth_T06_utm49n_50m.tif"
    b1 = boundaries / "basins_v1.gpkg"
    b2 = boundaries / "basins_v2.gpkg"

    write_population(pop_path)
    write_depth(d1, 0)
    write_depth(d2, 1)
    write_boundaries(b1, b2)

    layout = {
        "population_crs": POP_CRS,
        "depth_crs": DEPTH_CRS,
        "pop_resolution_m": POP_RES,
        "depth_resolution_m": DEPTH_RES,
        "files": {
            "population": str(pop_path),
            "depth_T00": str(d1),
            "depth_T06": str(d2),
            "boundary_v1": str(b1),
            "boundary_v2": str(b2),
        },
        "id_field": "basin_id",
        "scenarios": [
            "不同 CRS (UTM48N 人口 vs UTM49N 水深)",
            "不同像元大小与仿射变换 (100m vs 50m)",
            "流域 A/B 边界重叠 -> 人口格按相交面积分摊",
            "边界 v1 含无效但可修复多边形 (D)",
            "边界 v2 含修复失败多边形 (D 蝴蝶结)",
            "水深 NoData 带与有效零值严格区分",
            "人口网格自身含 NoData 格",
            "v2 局部修订 B + 新增 E，增量只重建 B/E/D",
        ],
    }
    with open(base / "sample_layout.json", "w", encoding="utf-8") as fh:
        json.dump(layout, fh, ensure_ascii=False, indent=2)
    return layout


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="生成洪水暴露度合成样例数据")
    parser.add_argument("output_dir", nargs="?", default="sample_data")
    args = parser.parse_args()
    info = generate(Path(args.output_dir))
    print(json.dumps(info, ensure_ascii=False, indent=2))
