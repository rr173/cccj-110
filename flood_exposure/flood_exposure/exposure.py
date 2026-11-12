"""受淹人口、缺测面积比例与质量标记的逐 (流域, 时次) 计算。

受淹人口的保守估计 (业务口径)
------------------------------
对人口格 t，流域 B 的面积份额 w(B,t)=area(B∩t)/area(t)。格内人口按面积
均摊到单位面积，因此该流域在该格“拥有”的人口为 w(B,t)·pop(t)。

格内受淹面积比例 wet_frac(t) 由水深栅格的等面积相交权重得到。于是

    exposed_pop(B, 时次) = Σ_t w(B,t) · pop(t) · wet_frac(t)

NoData / 栅格外 (OOB) 面积**不做零填补**：它们不进入 wet_frac，因此
exposed_pop 是一个下界保守估计，缺测规模通过 missing_fraction 与
质量标记单独披露。人口网格自身的 NoData 格整体剔除并打 POP_RASTER_NODATA。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import rasterio

from .basins import Basin
from .config import QualityFlag
from .population import BasinWeightTable, PopulationGrid
from .raster import resample_depth_windowed


@dataclass
class BasinExposure:
    basin_id: str
    forecast_time: str
    boundary_version: str
    depth_path: str
    depth_sha256: str
    population_path: str
    population_sha256: str
    work_crs: str
    threshold_m: float
    basin_area_m2: float
    weighted_population: float          # Σ w·pop，流域分摊到的总人口
    exposed_population: float           # 受淹人口保守估计 (下界)
    wet_area_m2: float
    dry_area_m2: float
    nodata_area_m2: float
    oob_area_m2: float
    missing_area_m2: float
    missing_fraction: float             # 缺测面积 / 流域权重覆盖面积
    pop_nodata_cells: int
    cell_count: int
    flags: list[str] = field(default_factory=list)
    gate_passed: bool = True

    def to_row(self) -> dict:
        d = self.__dict__.copy()
        d["flags"] = ";".join(self.flags)
        return d


def _read_pop_values(pop: PopulationGrid,
                     cells: list[tuple[int, int]]) -> dict[tuple[int, int], float]:
    """窗口化读取人口值；同一 (row,col) 只读一次。"""
    if not cells:
        return {}
    rows = [r for r, _ in cells]
    cols = [c for _, c in cells]
    r0, r1 = min(rows), max(rows) + 1
    c0, c1 = min(cols), max(cols) + 1
    with rasterio.open(pop.path) as ds:
        block = ds.read(
            1, window=rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0)
        )
    nodata = pop.info.nodata
    out: dict[tuple[int, int], float] = {}
    for (r, c) in cells:
        val = block[r - r0, c - c0]
        if np.ma.is_masked(block) and block.mask[r - r0, c - c0]:
            out[(r, c)] = np.nan
        elif nodata is not None and float(val) == float(nodata):
            # 用栅格原生 dtype 比较，规避 float32 填充值精度问题
            out[(r, c)] = np.nan if val == block.dtype.type(nodata) else float(val)
        elif np.issubdtype(block.dtype, np.floating) and np.isnan(float(val)):
            out[(r, c)] = np.nan
        else:
            out[(r, c)] = float(val)
    return out


def compute_basin_exposure(
    basin: Basin,
    table: BasinWeightTable,
    pop: PopulationGrid,
    depth_path: str,
    depth_sha256: str,
    forecast_time: str,
    boundary_version: str,
    work_crs: str,
    threshold: float,
    tile: int,
    overlap_ids: list[str] | None = None,
) -> BasinExposure:
    cells = sorted(table.fractions.keys())
    depth_res = resample_depth_windowed(
        depth_path=depth_path,
        pop_transform=pop.transform,
        pop_crs=pop.crs,
        work_crs=work_crs,
        target_cells=set(cells),
        threshold=threshold,
        tile=tile,
    )
    pop_values = _read_pop_values(pop, cells)

    weighted_pop = 0.0
    exposed = 0.0
    wet_area = dry_area = nodata_area = oob_area = 0.0
    covered_weight = 0.0          # 流域在人口格上的权重总面积 (m²)
    pop_nodata_cells = 0

    for key in cells:
        w = table.fractions[key]                 # 流域 × 人口格面积份额
        cell_area = table.cell_areas_m2[key]
        d = depth_res[key]
        pop_val = pop_values[key]

        covered_weight += w * cell_area
        wet_area += w * d["wet_area_m2"]
        dry_area += w * d["dry_area_m2"]
        nodata_area += w * d["nodata_area_m2"]
        oob_area += w * d["oob_area_m2"]

        if np.isnan(pop_val):
            pop_nodata_cells += 1
            continue
        weighted_pop += w * pop_val
        # wet_fraction 是格内受淹面积比例；NoData/OOB 不在其中，
        # 故受淹人口为保守下界，缺测另计。
        exposed += w * pop_val * d["wet_fraction"]

    missing_area = nodata_area + oob_area
    denom = covered_weight if covered_weight > 0 else 0.0
    missing_fraction = missing_area / denom if denom > 0 else 0.0

    # ----------------------------- 质量标记 ----------------------------- #
    flags = list(basin.flags)
    if overlap_ids:
        flags.append(QualityFlag.OVERLAP_WEIGHTED)
    if pop_nodata_cells:
        flags.append(QualityFlag.POP_RASTER_NODATA)
    # 相对容差 1e-9：投影重投影的亚平方米抖动不视为真实缺测
    if nodata_area > 0:
        flags.append(QualityFlag.NODATA_PRESENT)
    if denom > 0 and (oob_area / denom) > 1e-9:
        flags.append(QualityFlag.OUT_OF_DEPTH_EXTENT)
    if not flags:
        flags.append(QualityFlag.OK)

    return BasinExposure(
        basin_id=basin.basin_id,
        forecast_time=forecast_time,
        boundary_version=boundary_version,
        depth_path=str(depth_path),
        depth_sha256=depth_sha256,
        population_path=str(pop.path),
        population_sha256=pop.sha256,
        work_crs=work_crs,
        threshold_m=threshold,
        basin_area_m2=basin.area_work_m2,
        weighted_population=weighted_pop,
        exposed_population=exposed,
        wet_area_m2=wet_area,
        dry_area_m2=dry_area,
        nodata_area_m2=nodata_area,
        oob_area_m2=oob_area,
        missing_area_m2=missing_area,
        missing_fraction=missing_fraction,
        pop_nodata_cells=pop_nodata_cells,
        cell_count=len(cells),
        flags=flags,
    )
