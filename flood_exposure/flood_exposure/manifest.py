"""输入清单 (manifest) 模型与校验。

每轮运行固定三个输入清单摘要：
  1. depth_rasters  —— 按预报时次组织的水深 GeoTIFF；
  2. boundary       —— 带版本号的流域边界 GeoPackage；
  3. population     —— 人口网格。

清单中的每个文件都带 sha256 与字节大小，运行前逐一核对，任何漂移都会让
内容指纹变化，从而在计算清单 (run manifest) 中可被审计。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .hashing import sha256_file


@dataclass(frozen=True)
class DepthEntry:
    forecast_time: str          # 预报时次标识，如 "2026-11-12T00:00:00+00:00" 或 "T+06h"
    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class BoundaryEntry:
    version: str                # 边界版本号，如 "v1"、"v2"
    path: str
    layer: str | None
    id_field: str               # 流域标识字段名
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class PopulationEntry:
    path: str
    sha256: str
    size_bytes: int


@dataclass
class InputInventory:
    """一轮运行固定的三个输入清单摘要。"""

    depth_rasters: list[DepthEntry]
    boundary: BoundaryEntry
    population: PopulationEntry

    def all_files(self) -> list[Path]:
        files = [Path(e.path) for e in self.depth_rasters]
        files.append(Path(self.boundary.path))
        files.append(Path(self.population.path))
        return files

    def verify_files(self, base_dir: Path | None = None) -> list[str]:
        """核对存在性与 sha256，返回错误信息列表 (空列表=全部通过)。"""
        errors: list[str] = []

        def resolve(p: str) -> Path:
            q = Path(p)
            if not q.is_absolute() and base_dir is not None:
                q = base_dir / q
            return q

        for entry in list(self.depth_rasters) + [self.boundary, self.population]:
            fp = resolve(entry.path)
            if not fp.exists():
                errors.append(f"输入文件不存在: {fp}")
                continue
            actual = sha256_file(fp)
            if actual != entry.sha256:
                errors.append(
                    f"sha256 不匹配: {fp}\n  清单={entry.sha256}\n  实际={actual}"
                )
            if fp.stat().st_size != entry.size_bytes:
                errors.append(
                    f"文件大小不匹配: {fp} 清单={entry.size_bytes} 实际={fp.stat().st_size}"
                )
        times = [e.forecast_time for e in self.depth_rasters]
        if len(set(times)) != len(times):
            errors.append("水深栅格的 forecast_time 存在重复")
        if not self.depth_rasters:
            errors.append("depth_rasters 不能为空")
        return errors

    def to_dict(self) -> dict:
        return {
            "depth_rasters": [e.__dict__ for e in self.depth_rasters],
            "boundary": self.boundary.__dict__,
            "population": self.population.__dict__,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InputInventory":
        return cls(
            depth_rasters=[DepthEntry(**e) for e in data["depth_rasters"]],
            boundary=BoundaryEntry(**data["boundary"]),
            population=PopulationEntry(**data["population"]),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "InputInventory":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        inv = cls.from_dict(data)
        # 结构性校验（文件存在性校验交给运行器，因为路径可能相对输出根目录）
        if not isinstance(inv.depth_rasters, list) or not inv.depth_rasters:
            raise ValueError("清单缺少 depth_rasters 或为空")
        return inv
