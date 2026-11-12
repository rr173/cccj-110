"""人口网格读取与流域×人口格权重表。

权重表 (weight table) 是增量处理的核心缓存：它只依赖
  * 边界版本中每个流域的工作 CRS 几何，
  * 人口网格的仿射变换/CRS/内容指纹，
  * 工作 CRS 与代码版本。
水深栅格不参与权重表的构建，因此换一批预报时次或边界局部修订时，
未变化流域的权重表直接复用，不重复做几何相交。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio

from .basins import Basin, BasinCollection
from .geometry import (
    cell_footprint,
    detect_overlaps,
    intersect_basin_cells,
    make_transformer,
)
from .hashing import sha256_text, stable_json_hash
from .raster import RasterInfo, raster_info


@dataclass
class PopulationGrid:
    path: Path
    info: RasterInfo
    sha256: str

    @property
    def transform(self):
        return self.info.transform

    @property
    def crs(self) -> str:
        return self.info.crs

    def read_window(self, r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
        with rasterio.open(self.path) as ds:
            return ds.read(1, window=rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0))

    def read_cell(self, row: int, col: int) -> float:
        with rasterio.open(self.path) as ds:
            return float(ds.read(1, window=rasterio.windows.Window(col, row, 1, 1))[0, 0])


def load_population(path: str | Path, sha256: str) -> PopulationGrid:
    with rasterio.open(path) as ds:
        info = raster_info(ds, path)
    return PopulationGrid(path=Path(path), info=info, sha256=sha256)


def _basin_window(geom_work, pop: PopulationGrid, work_crs: str):
    """求流域包络在人口网格行列空间中的窗口 (含边界外扩 1 像元)。"""
    work_to_pop = make_transformer(work_crs, pop.crs)
    import shapely

    def _to_native(xy: np.ndarray) -> np.ndarray:
        x, y = work_to_pop.transform(xy[:, 0], xy[:, 1])
        return np.column_stack((x, y))

    native_env = shapely.transform(geom_work.envelope, _to_native)
    minx, miny, maxx, maxy = native_env.bounds
    # 行列反算：col = (x - c)/a, row = (y - f)/e
    t = pop.transform
    col_start = max(int(np.floor((minx - t.c) / t.a)) - 1, 0)
    col_stop = min(int(np.ceil((maxx - t.c) / t.a)) + 1, pop.info.width)
    # e 为负（北朝上）
    row_start = max(int(np.floor((maxy - t.f) / t.e)) - 1, 0)
    row_stop = min(int(np.ceil((miny - t.f) / t.e)) + 1, pop.info.height)
    return row_start, row_stop, col_start, col_stop


@dataclass
class BasinWeightTable:
    basin_id: str
    # (row, col) -> 相交面积 / 像元面积，即面积份额 w∈[0,1]
    fractions: dict[tuple[int, int], float]
    cell_areas_m2: dict[tuple[int, int], float]
    geometry_hash: str
    window: tuple[int, int, int, int]

    def cache_key(self, pop: PopulationGrid, work_crs: str, code_version: str) -> str:
        payload = {
            "basin_id": self.basin_id,
            "geometry_hash": self.geometry_hash,
            "pop_sha256": pop.sha256,
            "pop_transform": tuple(pop.transform)[:6],
            "pop_crs": pop.crs,
            "work_crs": work_crs,
            "code": code_version,
            "kind": "basin_weight_table_v1",
        }
        return stable_json_hash(payload)


def build_weight_table(
    basin: Basin, pop: PopulationGrid, work_crs: str
) -> BasinWeightTable | None:
    if not basin.usable or basin.geom_work is None:
        return None
    r0, r1, c0, c1 = _basin_window(basin.geom_work, pop, work_crs)
    pop_to_work = make_transformer(pop.crs, work_crs)
    cw = intersect_basin_cells(
        basin.geom_work, pop.transform, pop_to_work, r0, r1, c0, c1
    )
    fractions = {k: cw.fraction(*k) for k in cw.weights}
    return BasinWeightTable(
        basin_id=basin.basin_id,
        fractions=fractions,
        cell_areas_m2=cw.cell_areas,
        geometry_hash=basin.geometry_hash,
        window=(r0, r1, c0, c1),
    )


def build_all_weight_tables(
    collection: BasinCollection, pop: PopulationGrid, work_crs: str
) -> dict[str, BasinWeightTable]:
    tables: dict[str, BasinWeightTable] = {}
    for basin in collection.usable_basins():
        wt = build_weight_table(basin, pop, work_crs)
        if wt is not None:
            tables[basin.basin_id] = wt
    return tables


def overlap_report(collection: BasinCollection) -> dict[str, list[dict]]:
    ids = [b.basin_id for b in collection.usable_basins()]
    geoms = [b.geom_work for b in collection.usable_basins()]
    return detect_overlaps(ids, geoms)


# --------------------------------------------------------------------------- #
# 权重表的持久化缓存（内容寻址）
# --------------------------------------------------------------------------- #
def save_weight_table_cache(cache_dir: Path, table: BasinWeightTable,
                            pop: PopulationGrid, work_crs: str,
                            code_version: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = table.cache_key(pop, work_crs, code_version)
    payload = {
        "cache_key": key,
        "basin_id": table.basin_id,
        "geometry_hash": table.geometry_hash,
        "window": list(table.window),
        "entries": [
            {"row": r, "col": c, "fraction": f, "cell_area_m2": a}
            for (r, c), f in sorted(table.fractions.items())
            for a in [table.cell_areas_m2[(r, c)]]
        ],
    }
    fp = cache_dir / f"{key}.json"
    tmp = fp.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=0, sort_keys=True)
    tmp.replace(fp)
    return fp


def load_weight_table_cache(cache_dir: Path, key: str) -> BasinWeightTable | None:
    fp = cache_dir / f"{key}.json"
    if not fp.exists():
        return None
    with open(fp, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    fractions: dict[tuple[int, int], float] = {}
    areas: dict[tuple[int, int], float] = {}
    for e in payload["entries"]:
        k = (e["row"], e["col"])
        fractions[k] = e["fraction"]
        areas[k] = e["cell_area_m2"]
    return BasinWeightTable(
        basin_id=payload["basin_id"],
        fractions=fractions,
        cell_areas_m2=areas,
        geometry_hash=payload["geometry_hash"],
        window=tuple(payload["window"]),
    )
