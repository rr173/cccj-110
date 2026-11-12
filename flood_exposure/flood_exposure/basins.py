"""流域边界 GeoPackage 加载、修复、投影与指纹。

一个边界版本对应一个 :class:`BasinCollection`。每个流域携带：
* 原生 CRS 与工作 (等面积) CRS 下的几何；
* 修复状态标记；
* 几何指纹 —— 增量处理时据此判断新版本里哪些流域真的变了。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import shapely

from .config import QualityFlag
from .geometry import (
    assert_equal_area,
    make_transformer,
    reproject_geometry,
    repair_geometry,
)
from .hashing import sha256_text


@dataclass
class Basin:
    basin_id: str
    geom_native: shapely.Geometry
    geom_work: shapely.Geometry | None
    flags: list[str] = field(default_factory=list)
    area_work_m2: float = 0.0
    geometry_hash: str = ""

    @property
    def usable(self) -> bool:
        return QualityFlag.GEOMETRY_REPAIR_FAILED not in self.flags


@dataclass
class BasinCollection:
    version: str
    source_path: Path
    source_sha256: str
    native_crs: str
    work_crs: str
    id_field: str
    basins: list[Basin]

    def usable_basins(self) -> list[Basin]:
        return [b for b in self.basins if b.usable]

    def by_id(self) -> dict[str, Basin]:
        return {b.basin_id: b for b in self.basins}

    def failed_ids(self) -> list[str]:
        return [b.basin_id for b in self.basins if not b.usable]


def _geometry_fingerprint(geom_work: shapely.Geometry | None, precision: int = 6) -> str:
    """几何指纹：对工作 CRS 坐标取固定小数位后做规范化哈希。

    只用于“同一等面积 CRS 下两版边界是否变化”的判定，因此坐标取整到
    微米级 (m) 足以消除投影库版本的微小抖动。
    """
    if geom_work is None:
        return sha256_text("NULL_GEOMETRY")
    mapped = shapely.set_precision(geom_work, grid_size=10 ** (-precision),
                                   mode="pointwise")
    return sha256_text(shapely.to_wkt(mapped, rounding_precision=precision))


def load_basins(
    path: str | Path,
    version: str,
    source_sha256: str,
    id_field: str,
    work_crs: str,
    layer: str | None = None,
) -> BasinCollection:
    assert_equal_area(work_crs)
    gdf = gpd.read_file(path, layer=layer, engine="pyogrio")
    if gdf.crs is None:
        raise ValueError(f"边界 GeoPackage 缺少 CRS: {path}")
    if id_field not in gdf.columns:
        raise KeyError(f"边界中找不到标识字段 '{id_field}'，现有字段: {list(gdf.columns)}")

    native_crs = gdf.crs.to_string()
    to_work = make_transformer(gdf.crs, work_crs)

    basins: list[Basin] = []
    seen_ids: set[str] = set()
    for _, row in gdf.iterrows():
        bid = str(row[id_field])
        if bid in seen_ids:
            raise ValueError(f"流域标识重复: {bid}")
        seen_ids.add(bid)

        raw = row.geometry
        flags: list[str] = []
        if raw is None or raw.is_empty:
            basins.append(Basin(
                basin_id=bid, geom_native=raw, geom_work=None,
                flags=[QualityFlag.GEOMETRY_REPAIR_FAILED],
                geometry_hash=sha256_text("NULL_GEOMETRY"),
            ))
            continue

        fixed, repair_flag = repair_geometry(raw)
        if repair_flag is not None:
            flags.append(repair_flag)
        if fixed is None:
            basins.append(Basin(
                basin_id=bid, geom_native=raw, geom_work=None,
                flags=flags, geometry_hash=sha256_text("NULL_GEOMETRY"),
            ))
            continue

        geom_work = reproject_geometry(fixed, to_work)
        if not geom_work.is_valid:
            # 投影后可能产生极窄退化，再修一次；仍失败则标记失败
            geom_work, flag2 = repair_geometry(geom_work)
            if flag2 == QualityFlag.GEOMETRY_REPAIR_FAILED or geom_work is None:
                flags.append(QualityFlag.GEOMETRY_REPAIR_FAILED)
                basins.append(Basin(
                    basin_id=bid, geom_native=raw, geom_work=None,
                    flags=flags, geometry_hash=sha256_text("NULL_GEOMETRY"),
                ))
                continue

        basins.append(Basin(
            basin_id=bid,
            geom_native=raw,
            geom_work=geom_work,
            flags=flags,
            area_work_m2=geom_work.area,
            geometry_hash=_geometry_fingerprint(geom_work),
        ))

    return BasinCollection(
        version=version,
        source_path=Path(path),
        source_sha256=source_sha256,
        native_crs=native_crs,
        work_crs=work_crs,
        id_field=id_field,
        basins=basins,
    )
