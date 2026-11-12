"""独立参考计算：规则点阵 (lattice Monte Carlo) 面积估计。

与主流水线的关系
----------------
主流水线用“投影四边形 × shapely 精确相交面积”做权重；本模块刻意走一条
完全独立的代码路径：在每个人口格内铺 K×K 个规则点阵点，逐点判断落在
水深像元的哪个类别，用点数比例估计面积比例。

两者共享的只有输入文件与坐标变换，几何求交、面积累加、窗口逻辑均不共用，
因此结果一致可以互为印证。K 越大参考值越接近精确值，边界穿越格的误差
量级为 O(1/K)。对照测试要求相对偏差低于与 K 匹配的容差。

NoData 与有效零值同样严格区分，OOB 单独计数，口径与主流水线一致。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from shapely.geometry import Point, Polygon
from shapely.prepared import prep

from .geometry import make_transformer
from .raster import raster_info


def lattice_reference(
    depth_path: str | Path,
    pop_transform,
    pop_crs: str,
    work_crs: str,
    target_cells: set[tuple[int, int]],
    threshold: float,
    k: int = 40,
) -> dict[tuple[int, int], dict]:
    """对目标人口格返回点阵估计的 wet/nodata/oob 面积比例与面积 (m²)。"""
    with rasterio.open(depth_path) as dds:
        dinfo = raster_info(dds)
        d_to_work = make_transformer(dinfo.crs, work_crs)
        p_to_work = make_transformer(pop_crs, work_crs)
        work_to_depth = make_transformer(work_crs, dinfo.crs)

        depth_box_work = prep(dinfo.footprint_work(d_to_work))
        nodata = dinfo.nodata
        inv = ~dinfo.transform

        offsets = (np.arange(k) + 0.5) / k  # 每个小格取中心
        out: dict[tuple[int, int], dict] = {}

        for (row, col) in target_cells:
            x0 = pop_transform.c + col * pop_transform.a
            x1 = x0 + pop_transform.a
            y1 = pop_transform.f + row * pop_transform.e      # 北边缘
            y0 = y1 + pop_transform.e                          # 南边缘
            gx, gy = np.meshgrid(x0 + offsets * (x1 - x0),
                                 y0 + offsets * (y1 - y0))
            wx, wy = p_to_work.transform(gx.ravel(), gy.ravel())

            quad = Polygon([
                p_to_work.transform(x0, y1),
                p_to_work.transform(x1, y1),
                p_to_work.transform(x1, y0),
                p_to_work.transform(x0, y0),
            ])
            cell_area = quad.area

            dx, dy = work_to_depth.transform(wx, wy)
            src_cols, src_rows = inv @ (np.asarray(dx), np.asarray(dy))
            src_cols = np.floor(src_cols).astype(int)
            src_rows = np.floor(src_rows).astype(int)

            n_wet = n_dry = n_nodata = n_oob = 0
            total = k * k
            for idx in range(total):
                sr, sc = int(src_rows[idx]), int(src_cols[idx])
                inside = (
                    0 <= sr < dinfo.height and 0 <= sc < dinfo.width
                    and depth_box_work.contains(Point(wx[idx], wy[idx]))
                )
                if not inside:
                    n_oob += 1
                    continue
                value = dds.read(
                    1, window=rasterio.windows.Window(sc, sr, 1, 1)
                )[0, 0]
                if np.issubdtype(dds.dtypes[0], np.floating) and np.isnan(value):
                    n_nodata += 1
                elif nodata is not None and value == type(value)(nodata):
                    n_nodata += 1
                elif float(value) > threshold:
                    n_wet += 1
                else:
                    n_dry += 1

            out[(row, col)] = {
                "cell_area_m2": cell_area,
                "wet_fraction": n_wet / total,
                "dry_fraction": n_dry / total,
                "nodata_fraction": n_nodata / total,
                "oob_fraction": n_oob / total,
                "missing_fraction": (n_nodata + n_oob) / total,
                "k": k,
            }
    return out
