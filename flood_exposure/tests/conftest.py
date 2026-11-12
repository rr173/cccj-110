"""pytest 公共夹具：合成数据 + 临时输出目录。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# demo/ 不是包，直接把目录加进路径复用生成器
DEMO_DIR = Path(__file__).resolve().parents[1] / "demo"
sys.path.insert(0, str(DEMO_DIR))

import make_sample_data  # noqa: E402

from flood_exposure.config import Config  # noqa: E402
from flood_exposure.hashing import sha256_file  # noqa: E402
from flood_exposure.manifest import (  # noqa: E402
    BoundaryEntry, DepthEntry, InputInventory, PopulationEntry,
)


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("sample")
    make_sample_data.generate(d)
    return d


@pytest.fixture(scope="session")
def layout(sample_dir) -> dict:
    import json
    with open(sample_dir / "sample_layout.json", encoding="utf-8") as fh:
        return json.load(fh)


def _inventory(sample_dir: Path, boundary_file: str, version: str) -> InputInventory:
    files = {}
    for p in (sample_dir / "rasters").iterdir():
        files[p.name] = p
    bpath = sample_dir / "boundaries" / boundary_file
    ppath = sample_dir / "rasters" / "population_utm48n_100m.tif"
    return InputInventory(
        depth_rasters=[
            DepthEntry("T00", str(files["depth_T00_utm49n_50m.tif"]),
                       sha256_file(files["depth_T00_utm49n_50m.tif"]),
                       files["depth_T00_utm49n_50m.tif"].stat().st_size),
            DepthEntry("T06", str(files["depth_T06_utm49n_50m.tif"]),
                       sha256_file(files["depth_T06_utm49n_50m.tif"]),
                       files["depth_T06_utm49n_50m.tif"].stat().st_size),
        ],
        boundary=BoundaryEntry(
            version, str(bpath), None, "basin_id",
            sha256_file(bpath), bpath.stat().st_size),
        population=PopulationEntry(
            str(ppath), sha256_file(ppath), ppath.stat().st_size),
    )


@pytest.fixture
def inventory_v1(sample_dir) -> InputInventory:
    return _inventory(sample_dir, "basins_v1.gpkg", "v1")


@pytest.fixture
def inventory_v2(sample_dir) -> InputInventory:
    return _inventory(sample_dir, "basins_v2.gpkg", "v2")


@pytest.fixture
def config() -> Config:
    # 放宽无效流域闸门：v2 样例故意包含 1 个修复失败流域 (1/5=20%)
    return Config(max_missing_fraction=0.25,
                  max_invalid_basin_fraction=0.25,
                  window_tile_size=64)
