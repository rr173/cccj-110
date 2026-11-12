"""运行配置：阈值、NoData 上限、暂存/发布目录等。

配置以 JSON 文件或命令行参数提供；所有影响数值的参数都会写入计算清单，
保证可追溯。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 等面积工作投影：EPSG:6933 (WGS 84 / NSIDC EASE-Grid 2.0 全球等面积圆柱)。
# 选它而不是 LAEA/AEA，是因为业务中可能遇到跨越经度环绕线 (±180°) 的流域，
# 任何以中央经线为界的等面积投影都会把这类多边形切到图幅两端。
# EPSG:6933 的经度域为 ±180°，配合 antimeridian 拆分逻辑可统一处理。
EQUAL_AREA_CRS = "EPSG:6933"

# 声明精度：同一网格输入分块顺序不同时，结果差值低于该阈值即视为一致。
DECLARED_PRECISION = 1.0e-6


@dataclass
class Config:
    """一次运行的全部可调参数。

    Attributes
    ----------
    work_crs:
        面积计算使用的等面积 CRS，必须是等面积投影 (proj 方法 aea/laea/eqc 等)。
    depth_threshold_m:
        淹没判定水深阈值 (米)。像素水深 > 阈值才计为受淹。
        有效零值 (depth=0) 是“确认未淹”，不超过阈值，绝不与 NoData 混淆。
    max_missing_fraction:
        发布闸门：某 (流域, 时次) 的缺测面积比例超过该值则拒绝发布。
    max_invalid_basin_fraction:
        边界修复失败流域占比的上限，超过则整体拒绝发布。
    window_tile_size:
        窗口读取的块大小 (人口网格像素数)。只影响 I/O 与中间聚合，
        不改变任何权重，因此不同 tile 尺寸结果在声明精度内一致。
    declared_precision:
        结果一致性判定的相对/绝对容差。
    """

    work_crs: str = EQUAL_AREA_CRS
    depth_threshold_m: float = 0.0
    max_missing_fraction: float = 0.10
    max_invalid_basin_fraction: float = 0.0
    window_tile_size: int = 256
    declared_precision: float = DECLARED_PRECISION

    def validate(self) -> None:
        if self.depth_threshold_m < 0:
            raise ValueError("depth_threshold_m 不能为负")
        if not (0.0 <= self.max_missing_fraction <= 1.0):
            raise ValueError("max_missing_fraction 必须在 [0,1] 区间")
        if not (0.0 <= self.max_invalid_basin_fraction <= 1.0):
            raise ValueError("max_invalid_basin_fraction 必须在 [0,1] 区间")
        if self.window_tile_size < 16:
            raise ValueError("window_tile_size 过小 (<16)")
        if self.declared_precision <= 0:
            raise ValueError("declared_precision 必须为正数")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        cfg = cls(**data)
        cfg.validate()
        return cfg

    @classmethod
    def default(cls) -> "Config":
        cfg = cls()
        cfg.validate()
        return cfg


# 质量标记 (quality flags) 的取值，全部显式枚举，禁止隐式标记。
class QualityFlag:
    OK = "OK"                       # 全部面积均有有效观测
    NODATA_PRESENT = "NODATA_PRESENT"        # 存在 NoData，但未超闸门
    NODATA_GATE_FAIL = "NODATA_GATE_FAIL"    # NoData 超配置上限，拒绝发布
    OUT_OF_DEPTH_EXTENT = "OUT_OF_DEPTH_EXTENT"  # 部分面积在水深栅格外
    OVERLAP_WEIGHTED = "OVERLAP_WEIGHTED"    # 该流域与相邻流域重叠，人口已按面积分摊
    INVALID_GEOMETRY_REPAIRED = "INVALID_GEOM_REPAIRED"  # 边界无效但修复成功
    GEOMETRY_REPAIR_FAILED = "GEOM_REPAIR_FAILED"        # 修复失败，未参与计算
    POP_RASTER_NODATA = "POP_RASTER_NODATA"  # 人口网格自身存在 NoData
