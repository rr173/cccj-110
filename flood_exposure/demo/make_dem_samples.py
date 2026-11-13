"""生成用于 delineate 演示/测试的小型手画高程矩阵。

每个场景都是几行几列、可肉眼核对的小矩阵，覆盖需求列出的全部情形：

* ``unique_spill``：内部洼地 + 唯一缺口；填平后有确定抬高量与体积；
* ``flat_platform``：整片同高程平台，需要走过多格才能出水；
* ``anisotropy_wide`` / ``anisotropy_tall``：同一高程矩阵、只换横纵分辨率，
  最陡方向随之改变 (长宽不同)；
* ``nodata_island``：图内 NoData 岛 (不可穿越，不是泄水口)，测站落岛上应报错；
* ``isolated_block``：被 NoData 环包围、完全不接触外圈的有效块 -> 报错；
* ``branching_and_serial``：串联 A->B->C 加一条支流测站 D (收敛型 Y 河道)。

所有矩阵以米为单位；NoData 用哨兵 -9999 写入 GeoTIFF。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

#: 演示统一用的米制投影 CRS (UTM 49N)。
DEM_CRS = "EPSG:32649"
NODATA = -9999.0


SCENARIOS: dict[str, dict] = {
    # ------------------------------------------------------------------ #
    # 1. 唯一缺口的洼地 (unique spill)
    #    7x8 网格：四周高墙 60，内部洼地低至 2/3/4，只在北侧留一个缺口
    #    (1,5)=5 通到 (0,5)=60? 不行：要“不升高”只能到 <= 高程。
    #    构造：内圈地面 2~6，外圈墙 60；唯一缺口是 (1,6)=5，(0,6)=60 太高，
    #    所以真正缺口要从内圈直通边界。把 (0,6) 改成 5，它是最外圈格，
    #    即可流出图。洼地水位被抬到 5 (缺口高程)。
    #
    #    抬高的格子 (逐格手算见测试与 demo)：内圈低于 5 的 9 个格，
    #    抬高量分别为 3,4,2,0,1,3,0,2,1 (m)。像元 100m×100m=10000 m²。
    #    填洼体积 = (3+4+2+0+1+3+0+2+1)*10000 = 160000 m³。
    # ------------------------------------------------------------------ #
    "unique_spill": {
        "elevation": np.array([
            [60, 60, 60, 60, 60, 60,  5, 60],
            [60, 50, 48, 46, 44, 42,  5, 60],
            [60, 48, 10,  8,  6,  5,  5, 60],
            [60, 46,  8,  0,  4,  6,  5, 60],
            [60, 44,  6,  4,  2,  5,  5, 60],
            [60, 42, 40, 38, 36, 34, 32, 60],
            [60, 60, 60, 60, 60, 60, 60, 60],
        ], dtype="float64"),
        "transform": Affine(100, 0, 0, 0, -100, 700),
        # 测站放在唯一出口格 (0,6)：它的全部上游面积 = 整幅有效面积。
        "stations": [("OUT1", 0, 6)],
    },

    # ------------------------------------------------------------------ #
    # 2. 需要走过多个格子才出水的平台 (flat platform)
    #    平台两条 (1..2, 1..9) 高程 6，外圈高 9；东侧 (1..3,10)=2 是低地，
    #    并且 (3,10) 在最外圈 -> 平台从西端 (1,1) 出发要沿平台走 9 格到
    #    (1,9)，再下到 (1,10)/(2,10)/(3,10) 才出图。平台本身是**不升高**
    #    的，因此填洼不抬高任何格；关键是平台内方向必须逐跳向出口收敛。
    # ------------------------------------------------------------------ #
    "flat_platform": {
        "elevation": np.array([
            [9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9],
            [9, 6, 6, 6, 6, 6, 6, 6, 6, 6, 2],
            [9, 6, 6, 6, 6, 6, 6, 6, 6, 6, 2],
            [9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 2],
        ], dtype="float64"),
        "transform": Affine(100, 0, 0, 0, -100, 400),
        "stations": [("PLAT", 3, 10)],
    },

    # ------------------------------------------------------------------ #
    # 3. 长宽不同导致最陡方向变化 (同一高程矩阵，两种分辨率)
    #    3x3：中心 10，正交邻格 8、对角邻格 9。
    #    dx=50, dy=200 (宽像元在 x)：E 的坡度=2/50=0.04，N=2/200=0.01 -> E
    #    dx=200,dy=50 (高像元在 y)  ：N 的坡度=2/50=0.04，E=2/200=0.01 -> N
    #    -> 同一个矩阵，仅分辨率不同，最陡方向从 E 变为 N。
    # ------------------------------------------------------------------ #
    "anisotropy": {
        "elevation": np.array([
            [9, 8, 9],
            [8, 10, 8],
            [9, 8, 9],
        ], dtype="float64"),
        "stations": [("ANISO", 1, 1)],
    },

    # ------------------------------------------------------------------ #
    # 4. 图内 NoData 岛 (nodata island)
    #    7x7 东南向坡面，中心 3x3 为 NoData 岛。流向只能绕岛；岛不是泄水口。
    #    测站放在 (3,3) (岛上) 时必须报“落在无效高程格”。
    # ------------------------------------------------------------------ #
    "nodata_island": {
        "elevation": np.array([
            [14, 13, 12, 11, 10,  9,  8],
            [13, 12, 11, 10,  9,  8,  7],
            [12, 11, 10,  9,  8,  7,  6],
            [11, 10, NODATA, NODATA, NODATA,  6,  5],
            [10,  9, NODATA, NODATA, NODATA,  5,  4],
            [ 9,  8, NODATA, NODATA, NODATA,  4,  3],
            [ 8,  7,  6,  5,  4,  3,  2],
        ], dtype="float64"),
        "transform": Affine(100, 0, 0, 0, -100, 700),
        "stations": [("ISL", 6, 6)],
        "nodata": NODATA,
    },

    # ------------------------------------------------------------------ #
    # 5. 没有外圈出口的孤立有效块 (isolated block)
    #    6x6 全 NoData 环包住内部 2x2 有效块 -> 不能穿 NoData 出图，必须报错。
    # ------------------------------------------------------------------ #
    "isolated_block": {
        "elevation": np.array([
            [NODATA, NODATA, NODATA, NODATA, NODATA, NODATA],
            [NODATA, NODATA, NODATA, NODATA, NODATA, NODATA],
            [NODATA, NODATA,      4,      3, NODATA, NODATA],
            [NODATA, NODATA,      3,      2, NODATA, NODATA],
            [NODATA, NODATA, NODATA, NODATA, NODATA, NODATA],
            [NODATA, NODATA, NODATA, NODATA, NODATA, NODATA],
        ], dtype="float64"),
        "transform": Affine(100, 0, 0, 0, -100, 600),
        "stations": [("ISO", 3, 3)],
        "nodata": NODATA,
    },

    # ------------------------------------------------------------------ #
    # 6. 上下游串联 + 分支测站 (branching_and_serial)
    #    一条从西北向东南的主河道 (1,1)->(1,5)->(2,6)->(3,7)->(4,7)->(5,7)
    #    ->(6,7)->(7,8)，中段 A、B、C 三站串联；从左侧 (4,x) 有一条支流汇入
    #    主河道中段，支流测站 D 只属于支流，随后经过 A/B 之前先遇 D。
    #    高程矩阵手工构造为“离河道越远越高”的扇面 (见值表)。
    # ------------------------------------------------------------------ #
    "branching_and_serial": {
        "elevation": np.array([
            [141.4, 100, 100, 100, 100, 100, 141.4, 223.6, 282.8],
            [100, 6.6, 6, 5.4, 4.8, 4.2, 100, 141.4, 223.6],
            [141.4, 100, 100, 100, 100, 100, 3.6, 100, 141.4],
            [223.6, 200, 200, 200, 200, 141.4, 100, 3, 100],
            [316.2, 300, 300, 300, 282.8, 200, 100, 2.4, 100],
            [412.3, 400, 400, 400, 300, 200, 100, 1.8, 100],
            [509.9, 500, 500, 400, 300, 200, 100, 1.2, 100],
            [608.3, 600, 500, 400, 300, 200, 100, 0.6, 0],
            [707.1, 608.3, 509.9, 412.3, 316.2, 223.6, 141.4, 100, 100],
        ], dtype="float64"),
        "transform": Affine(100, 0, 0, 0, -100, 900),
        "stations": [("A", 1, 2), ("B", 2, 6), ("C", 5, 7), ("D", 4, 6)],
    },
}


def _write_dem(path: Path, matrix: np.ndarray, transform: Affine,
               nodata: float | None) -> None:
    h, w = matrix.shape
    with rasterio.open(
        path, "w", driver="GTiff", width=w, height=h, count=1,
        dtype="float64", crs=DEM_CRS, transform=transform,
        nodata=nodata if nodata is not None else None,
    ) as ds:
        ds.write(matrix.astype("float64"), 1)
        ds.set_band_description(1, "synthetic DEM")


def write_stations(path: Path, stations: list[tuple[str, int, int]],
                   order: str = "as_given") -> None:
    """写测站 CSV；``order`` 支持 ``as_given`` / ``reversed`` / ``shuffled``。"""
    import csv
    import random
    rows = list(stations)
    if order == "reversed":
        rows = list(reversed(rows))
    elif order == "shuffled":
        rnd = random.Random(20261113)
        rnd.shuffle(rows)
    elif order != "as_given":
        raise ValueError(f"未知的测站行序: {order}")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["outlet_id", "row", "col"])
        for oid, r, c in rows:
            writer.writerow([oid, r, c])


def build_anisotropy_variants(out_dir: Path) -> dict:
    """同一高程矩阵，写两种分辨率：宽像元 (dx 大) 与高像元 (dy 大)。"""
    spec = SCENARIOS["anisotropy"]
    matrix = spec["elevation"]
    h, w = matrix.shape
    variants = {}
    for name, dx, dy in (("anisotropy_wide", 50.0, 200.0),
                         ("anisotropy_tall", 200.0, 50.0)):
        d = out_dir / name
        d.mkdir(parents=True, exist_ok=True)
        dem = d / "dem.tif"
        _write_dem(dem, matrix, Affine(dx, 0, 0, 0, -dy, h * dy), None)
        stations = d / "stations.csv"
        write_stations(stations, spec["stations"])
        variants[name] = {"dem": str(dem), "stations": str(stations),
                          "dx": dx, "dy": dy}
    return variants


def _transform_for(spec: dict, matrix: np.ndarray) -> Affine:
    if "transform" in spec:
        return spec["transform"]
    dx = spec.get("transform_x", 100.0)
    dy = spec.get("transform_y", 100.0)
    h = matrix.shape[0]
    return Affine(dx, 0, 0, 0, -dy, h * dy)


def build_scenario(name: str, out_dir: Path,
                   station_order: str = "as_given") -> dict:
    """把一个场景写成 ``dem.tif`` 与 ``stations.csv``，返回说明字典。"""
    spec = SCENARIOS[name]
    out_dir.mkdir(parents=True, exist_ok=True)
    matrix = spec["elevation"]
    transform = _transform_for(spec, matrix)
    dem = out_dir / "dem.tif"
    _write_dem(dem, matrix, transform, spec.get("nodata"))
    stations = out_dir / "stations.csv"
    write_stations(stations, spec["stations"], order=station_order)
    return {
        "name": name,
        "dem": str(dem),
        "stations": str(stations),
        "matrix": matrix.tolist(),
        "nodata": spec.get("nodata"),
        "dx": transform.a,
        "dy": -transform.e,
        "station_rows": [
            {"outlet_id": oid, "row": r, "col": c}
            for oid, r, c in spec["stations"]
        ],
    }


def generate_all(base: str | Path) -> dict:
    """把所有场景写到 ``base/<scenario>/``，返回索引 JSON。"""
    base = Path(base)
    index: dict = {"scenarios": {}, "anisotropy_variants": {}}
    for name in SCENARIOS:
        if name == "anisotropy":
            continue
        index["scenarios"][name] = build_scenario(name, base / name)
    index["anisotropy_variants"] = build_anisotropy_variants(base / "anisotropy")
    (base / "delineate_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="生成 delineate 合成 DEM 场景")
    parser.add_argument("output_dir")
    args = parser.parse_args()
    print(json.dumps(generate_all(Path(args.output_dir)), ensure_ascii=False,
                     indent=2))
