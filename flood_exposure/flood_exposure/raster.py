"""栅格读取与面积权重重采样。

NoData 的业务含义
-----------------
水深栅格里两类“零看起来一样”的值必须严格区分：

* **有效零值 (valid zero)**：该像元被模型/观测覆盖，确认水深为 0，
  业务含义是“确认未淹”。它参与面积权重，淹没判定时不超过阈值。
* **NoData**：该像元没有可用的水深结果 (模型域外、被掩膜、缺测等)，
  业务含义是“不知道”。它既不能当干地、也不能当淹没地，对应面积计入
  “缺测面积”，超过配置上限时拒绝发布，绝不静默按零填补。

人口网格若自身带 NoData，同样不做零填补，而打 POP_RASTER_NODATA 标记。

面积权重定义
------------
源水深像元 s 与目标人口格 t 都投影到等面积工作 CRS：

    A_wet(t)   = Σ_s area(s ∩ t)  对 depth(s) > 阈值 的有效像元
    A_dry(t)   = Σ_s area(s ∩ t)  对 有效且 depth(s) <= 阈值 的像元
    A_nodata(t)= Σ_s area(s ∩ t)  对 NoData 像元
    A_oob(t)   = area(t) - 以上三者  (人口格落在水深栅格范围之外)

把这些面积归到流域 B 时，必须用三向交集，而不是“格内比例 × 流域份额”：

    A_class(B, t) = Σ_s area(s ∩ t ∩ B)     class ∈ {wet, dry, nodata}
    A_oob(B, t)   = area(t ∩ B) - 以上三者

受淹面积只来自真正与流域相交的水深像元，NoData 面积同理；缺测比例的分母
是流域被人口网格覆盖的面积 area(t ∩ B)。若调用方不传 basin_geom_work，
则退化为整格口径 (B 视为覆盖整个目标格)。

权重完全由等面积投影下的几何相交决定，与读取窗口的切分无关；窗口只决定
每次从磁盘读多少像元。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from shapely.geometry import box as shapely_box
from shapely.prepared import prep

from .geometry import cell_footprint, make_transformer, reproject_geometry


@dataclass
class RasterInfo:
    path: Path
    crs: str
    width: int
    height: int
    transform: rasterio.Affine
    nodata: float | None
    dtype: str

    @property
    def bounds_native(self) -> tuple[float, float, float, float]:
        return rasterio.transform.array_bounds(self.height, self.width, self.transform)

    def footprint_work(self, to_work) -> "shapely.Geometry":
        minx, miny, maxx, maxy = self.bounds_native
        native_box = shapely_box(minx, miny, maxx, maxy)
        return reproject_geometry(native_box, to_work)


def open_raster(path: str | Path) -> rasterio.DatasetReader:
    return rasterio.open(path)


def raster_info(ds: rasterio.DatasetReader, path: str | Path | None = None) -> RasterInfo:
    if ds.crs is None:
        raise ValueError(f"栅格缺少 CRS 定义: {path}")
    return RasterInfo(
        path=Path(path) if path else Path(ds.name),
        crs=ds.crs.to_string(),
        width=ds.width,
        height=ds.height,
        transform=ds.transform,
        nodata=float(ds.nodata) if ds.nodata is not None else None,
        dtype=str(ds.dtypes[0]),
    )


def iter_windows(width: int, height: int, tile: int):
    """按 tile×tile 枚举 (row0,row1,col0,col1) 窗口，覆盖整个栅格。"""
    for r0 in range(0, height, tile):
        r1 = min(r0 + tile, height)
        for c0 in range(0, width, tile):
            c1 = min(c0 + tile, width)
            yield r0, r1, c0, c1


@dataclass
class CellAccum:
    """单个人口格上的面积累加器，单位 m² (工作 CRS)。"""

    wet: float = 0.0
    dry: float = 0.0
    nodata: float = 0.0
    slice_area: float = 0.0        # area(人口格 ∩ 流域)，本格归到该流域的覆盖面积

    @property
    def observed(self) -> float:
        return self.wet + self.dry


class AreaWeightedResampler:
    """把一个水深栅格按等面积相交权重聚合到一组人口格上。

    参数
    ----
    depth_ds:
        已打开的水深栅格 (任意 CRS / 像元大小 / 仿射变换)。
    pop_transform, pop_crs:
        人口网格的仿射变换与 CRS；目标格以 (row,col) 标识。
    work_crs:
        等面积工作 CRS。
    target_cells:
        本次需要输出的人口格 (row, col) 集合，通常是某流域窗口内的格。
    threshold:
        淹没水深阈值。
    tile:
        源栅格窗口读取块大小；改变它不改变任何累加结果。
    basin_geom_work:
        流域在 work_crs 下的几何。给定时所有类别面积与覆盖面积都限制在
        area(源像元 ∩ 人口格 ∩ 流域) / area(人口格 ∩ 流域)；不给则按整格
        口径。空间归属由几何相交决定，不能用两个比例相乘来近似。
    """

    def __init__(
        self,
        depth_ds: rasterio.DatasetReader,
        pop_transform,
        pop_crs,
        work_crs: str,
        target_cells: set[tuple[int, int]],
        threshold: float,
        tile: int = 256,
        basin_geom_work: object | None = None,
    ):
        self.ds = depth_ds
        self.info = raster_info(depth_ds)
        self.pop_transform = pop_transform
        self.threshold = float(threshold)
        self.tile = tile
        self.target_cells = target_cells
        self.basin_geom_work = basin_geom_work

        self.depth_to_work = make_transformer(self.info.crs, work_crs)
        self.pop_to_work = make_transformer(pop_crs, work_crs)

        # 目标格的工作 CRS 四边形、面积，以及归到流域的交集几何/面积
        self.cell_quads: dict[tuple[int, int], object] = {}
        self.cell_areas: dict[tuple[int, int], float] = {}
        self.basin_slices: dict[tuple[int, int], object] = {}
        self.slice_areas: dict[tuple[int, int], float] = {}
        basin_prep = prep(basin_geom_work) if basin_geom_work is not None else None
        for (row, col) in target_cells:
            quad = cell_footprint(pop_transform, self.pop_to_work, row, col)
            self.cell_quads[(row, col)] = quad
            self.cell_areas[(row, col)] = quad.area
            if basin_prep is None:
                self.basin_slices[(row, col)] = quad
                self.slice_areas[(row, col)] = quad.area
            elif basin_prep.contains(quad):
                # 格完全在流域内：无需再做一次 intersection
                self.basin_slices[(row, col)] = quad
                self.slice_areas[(row, col)] = quad.area
            else:
                inter = quad.intersection(basin_geom_work)
                self.basin_slices[(row, col)] = inter
                self.slice_areas[(row, col)] = inter.area

        # 水深栅格范围 (工作 CRS)，用于区分 NoData 与“栅格外 (OOB)”
        self.depth_footprint = prep(self.info.footprint_work(self.depth_to_work))
        self.acc: dict[tuple[int, int], CellAccum] = {
            k: CellAccum(slice_area=self.slice_areas[k]) for k in target_cells
        }

    # ------------------------------------------------------------------ #
    def run(self) -> dict[tuple[int, int], CellAccum]:
        for r0, r1, c0, c1 in iter_windows(self.info.width, self.info.height, self.tile):
            self._process_window(r0, r1, c0, c1)
        return self.acc

    def _process_window(self, r0: int, r1: int, c0: int, c1: int) -> None:
        window = Window(c0, r0, c1 - c0, r1 - r0)
        block = self.ds.read(1, window=window, boundless=False)
        nodata = self.info.nodata

        # 先把源窗口的整体包络投到工作 CRS，快速剔除与所有目标格无关的窗口
        win_transform = self.ds.window_transform(window)
        minx = win_transform.c
        maxx = win_transform.c + win_transform.a * block.shape[1]
        maxy = win_transform.f
        miny = win_transform.f + win_transform.e * block.shape[0]
        win_box_native = shapely_box(minx, miny, maxx, maxy)
        win_box_work = reproject_geometry(win_box_native, self.depth_to_work)

        local_cells = [
            k for k in self.target_cells
            if self.slice_areas[k] > 0
            and self.basin_slices[k].intersects(win_box_work)
        ]
        if not local_cells:
            return

        for br in range(block.shape[0]):
            sr = r0 + br
            for bc in range(block.shape[1]):
                sc = c0 + bc
                value = block[br, bc]
                # 掩码数组优先看 mask；标量比较保持在栅格原生 dtype，
                # 避免 float32 的 -9999 提升为 float64 后精度不匹配
                if np.ma.is_masked(block) and block.mask[br, bc]:
                    is_nodata = True
                elif nodata is None:
                    is_nodata = False
                elif np.issubdtype(block.dtype, np.floating) and np.isnan(value):
                    is_nodata = True
                else:
                    is_nodata = value == block.dtype.type(nodata)

                # 源像元工作 CRS 四边形
                src_quad = cell_footprint(
                    self.ds.transform, self.depth_to_work, sr, sc
                )
                if src_quad.is_empty:
                    continue
                src_prep = prep(src_quad)
                for key in local_cells:
                    # 三向交集 area(源像元 ∩ 人口格 ∩ 流域)：类别面积直接
                    # 取自与流域真正相交的那一部分，而非两个比例的乘积
                    basin_slice = self.basin_slices[key]
                    if not src_prep.intersects(basin_slice):
                        continue
                    inter_area = src_quad.intersection(basin_slice).area
                    if inter_area <= 0:
                        continue
                    acc = self.acc[key]
                    if is_nodata:
                        acc.nodata += inter_area
                    elif float(value) > self.threshold:
                        acc.wet += inter_area
                    else:
                        # 有效零值与阈值以下的有效值都落在这里：确认未淹
                        acc.dry += inter_area

    # ------------------------------------------------------------------ #
    def finalize(self) -> dict[tuple[int, int], dict]:
        """把累加器转成该流域口径下的类别面积与比例。

        所有面积都是 area(源像元 ∩ 人口格 ∩ 流域)，OOB 是 area(人口格 ∩ 流域)
        减去三类已知面积；分母同理用流域覆盖面积，而不是整格面积。
        """
        out: dict[tuple[int, int], dict] = {}
        for key, acc in self.acc.items():
            cell_area = self.cell_areas[key]
            slice_area = acc.slice_area
            covered = acc.wet + acc.dry + acc.nodata
            # 数值保护：相交面积之和理论上 <= 流域覆盖面积，投影抖动时截断
            covered = min(covered, slice_area)
            oob = max(slice_area - covered, 0.0)
            wet_frac = acc.wet / slice_area if slice_area > 0 else 0.0
            nodata_frac = acc.nodata / slice_area if slice_area > 0 else 0.0
            oob_frac = oob / slice_area if slice_area > 0 else 0.0
            out[key] = {
                "cell_area_m2": cell_area,
                "basin_slice_area_m2": slice_area,
                "wet_area_m2": acc.wet,
                "dry_area_m2": acc.dry,
                "nodata_area_m2": acc.nodata,
                "oob_area_m2": oob,
                "wet_fraction": wet_frac,
                "nodata_fraction": nodata_frac,
                "oob_fraction": oob_frac,
                "missing_fraction": nodata_frac + oob_frac,
            }
        return out


def resample_depth_windowed(
    depth_path: str | Path,
    pop_transform,
    pop_crs,
    work_crs: str,
    target_cells: set[tuple[int, int]],
    threshold: float,
    tile: int = 256,
    basin_geom_work: object | None = None,
) -> dict[tuple[int, int], dict]:
    """便捷封装：打开栅格、窗口重采样、返回 finalize 后的逐格结果。

    给定 basin_geom_work 时，返回的 wet/nodata/oob 面积与比例都是该流域
    与实际相交的水深像元的交集口径；不给则按整格口径。
    """
    with rasterio.open(depth_path) as ds:
        r = AreaWeightedResampler(
            ds, pop_transform, pop_crs, work_crs, target_cells, threshold, tile,
            basin_geom_work=basin_geom_work,
        )
        r.run()
        return r.finalize()
