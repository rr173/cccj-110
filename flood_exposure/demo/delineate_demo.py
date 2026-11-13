"""delineate 端到端演示：用手画的小高程矩阵解释每一步结果。

运行：

    .venv/bin/python -I demo/delineate_demo.py [输出目录]

脚本会

1. 生成 6 组合成 DEM 场景 (见 ``make_dem_samples.py``)；
2. 实际调用 ``flood-exposure delineate`` 处理每组；
3. 打印方向图的“箭头地图”、填洼结果、测站摘要与手算对照；
4. 独立逐格追踪核对：有效格都到外界、方向不成环、贡献面积 = 枚举上游
   格面积、独占分区不交叠且与未归属部分覆盖所有有效格；
5. 翻转测站 CSV 行序，比较两次结果是否一致；
6. 对错误输入 (孤立块、测站落在 NoData、两 ID 同格、同 ID 两格、旋转
   栅格、地理坐标) 实际调用 CLI，确认失败并打印原因。

注：输出是**地形推导的汇水边界**，不是洪水水动力模拟。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import make_dem_samples as mk  # noqa: E402

from flood_exposure.hydrology import (  # noqa: E402
    DIR_NODATA,
    DIR_OUTSIDE,
    enumerate_upstream,
    load_dem,
    load_stations,
    delineate,
    trace_down,
)

ARROWS = {0: "↖", 1: "↑", 2: "↗", 3: "→", 4: "↘", 5: "↓", 6: "↙", 7: "←",
          DIR_OUTSIDE: "⇱", DIR_NODATA: "·"}
DIR_NAMES = ("NW", "N", "NE", "E", "SE", "S", "SW", "W")


def cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-m", "flood_exposure.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )


def show_matrix(title: str, matrix: np.ndarray, fmt: str = "{:g}") -> None:
    print(f"  {title}:")
    for row in matrix:
        print("    " + " ".join(fmt.format(v) for v in row))


def arrow_map(direction: np.ndarray) -> None:
    for row in direction:
        print("    " + " ".join(ARROWS[int(v)] for v in row))


def station_marker_map(grid, stations) -> None:
    """在矩阵上标出测站位置，便于对照。"""
    marks = [["." for _ in range(grid.width)] for _ in range(grid.height)]
    for s in stations:
        marks[s.row][s.col] = "*"
    for row in marks:
        print("    " + " ".join(row))


def hand_checks(grid, result, summary, label: str) -> list[str]:
    """独立核对：返回失败项列表 (空列表=全部通过)。"""
    problems: list[str] = []
    direction = result.direction

    # (1) 有效格都能到外界、方向不成环
    for r, c in np.argwhere(grid.valid):
        r, c = int(r), int(c)
        try:
            path = trace_down(grid, direction, r, c)
        except Exception as exc:          # noqa: BLE001 - 演示打印原因
            problems.append(f"{label}: 从 ({r},{c}) 追踪失败: {exc}")
            continue
        if int(direction[path[-1]]) != DIR_OUTSIDE:
            problems.append(f"{label}: ({r},{c}) 未到 OUTSIDE")
        if len(path) > grid.height * grid.width:
            problems.append(f"{label}: ({r},{c}) 路径过长/成环")

    # (2) 贡献面积 = 枚举上游格面积 (独立逐格反向枚举)
    total = summary["_total_area"]
    for r, c in np.argwhere(grid.valid):
        r, c = int(r), int(c)
        up = enumerate_upstream(grid, direction, r, c)
        want = len(up) * grid.cell_area_m2
        if abs(total[r, c] - want) > 1e-9:
            problems.append(
                f"{label}: ({r},{c}) 贡献面积 {total[r,c]} != 枚举 {want}")

    # (3) 独占分区不交叠 + 与未归属覆盖全部有效格
    first = summary["_first_station"]
    seen: dict[tuple[int, int], str | None] = {}
    for r, c in np.argwhere(grid.valid):
        r, c = int(r), int(c)
        sid = first[r, c]
        seen[(r, c)] = sid
    if len(seen) != int(grid.valid.sum()):
        problems.append(f"{label}: 分区格数 != 有效格数")
    by_station = {}
    for key, sid in seen.items():
        if sid is not None:
            by_station.setdefault(sid, 0)
            by_station[sid] += 1
    for row in summary["stations"]:
        want = by_station.get(row["outlet_id"], 0) * grid.cell_area_m2
        if abs(row["exclusive_area_m2"] - want) > 1e-9:
            problems.append(
                f"{label}: {row['outlet_id']} 独占面积 {row['exclusive_area_m2']}"
                f" != 逐格计数 {want}")
    unassigned_cells = sum(1 for v in seen.values() if v is None)
    want_unassigned = unassigned_cells * grid.cell_area_m2
    if abs(summary["unassigned_area_m2"] - want_unassigned) > 1e-9:
        problems.append(f"{label}: 未归属面积不等于逐格计数")

    # (4) 填洼只抬高不降低，且已能排水的格未被改动
    if np.any(result.filled[grid.valid] < grid.elevation[grid.valid] - 1e-12):
        problems.append(f"{label}: 填洼出现降低")
    return problems


def describe_scenario(name: str, sc: dict, out_dir: Path) -> bool:
    print(f"\n{'='*72}\n场景: {name}\n{'='*72}")
    print(f"  DEM: {sc['dem']}")
    print(f"  像元: dx={sc['dx']:g} m, dy={sc['dy']:g} m, "
          f"面积 {sc['dx']*sc['dy']:.0f} m²")
    print("  原始高程矩阵 (NoData=-9999):")
    show_matrix("elevation", np.array(sc["matrix"]))
    grid = load_dem(Path(sc["dem"]))
    stations = load_stations(Path(sc["stations"]), grid)
    print("  测站位置 (*):")
    station_marker_map(grid, stations)

    result, summary = delineate(grid, stations)
    print("\n  D8 方向 (←↖↑↗→↘↓↙；⇱=流出图 8；·=无效 255):")
    arrow_map(result.direction)
    print("\n  填洼高程 (m):")
    show_matrix("filled", result.filled)
    print("\n  每格抬高量 (m):")
    show_matrix("raised", result.raised)
    print(f"  填平格数 {result.pit_filled_cells}, "
          f"填洼体积 {summary['fill_volume_m3']:.0f} m³, "
          f"平台内逐跳 {summary['flat_routing']['max_flat_hops']} 跳")

    print("\n  测站摘要:")
    header = (f"  {'id':<6}{'row,col':>9}{'原始':>10}{'填洼':>10}{'抬高':>8}"
              f"{'贡献面积':>14}{'独占面积':>14}  下一站")
    print(header)
    for r in summary["stations"]:
        print(f"  {r['outlet_id']:<6}{r['row']:>4},{r['col']:<4}"
              f"{r['elevation_m']:>10.1f}{r['filled_elevation_m']:>10.1f}"
              f"{r['raised_m']:>8.1f}"
              f"{r['total_upstream_area_m2']:>14.0f}"
              f"{r['exclusive_area_m2']:>14.0f}  "
              f"{r['next_downstream_outlet'] or '—'}")
    print(f"  有效总面积 {summary['total_valid_area_m2']:.0f} m², "
          f"流出图总面积 {summary['outside_area_m2']:.0f} m², "
          f"未归属 {summary['unassigned_area_m2']:.0f} m²")

    # 每个测站沿方向图的实际路径
    print("  沿方向图逐格追踪 (测站 -> 图外):")
    for s in stations:
        path = trace_down(grid, result.direction, s.row, s.col)
        chain = " -> ".join(f"({r},{c})" for r, c in path)
        print(f"    {s.outlet_id}: {chain}")

    problems = hand_checks(grid, result, summary, name)
    if problems:
        for p in problems:
            print(f"  [核对失败] {p}")
        return False
    print("  [核对通过] 有效格全到外界；方向无环；贡献面积=枚举上游；"
          "独占分区不交叠且覆盖有效格；填洼不降低。")
    return True


def check_unique_spill_by_hand(out_dir: Path) -> bool:
    print(f"\n{'='*72}\n唯一缺口洼地：手算抬高量与填洼体积\n{'='*72}")
    grid = load_dem(out_dir / "unique_spill" / "dem.tif")
    st = load_stations(out_dir / "unique_spill" / "stations.csv", grid)
    result, summary = delineate(grid, st)
    deltas = {(r, c): float(result.raised[r, c])
              for r, c in np.argwhere(result.raised > 0)}
    print("  被抬高的格 (row, col) -> 抬高量 (m):")
    for (r, c), dz in sorted(deltas.items()):
        print(f"    ({r},{c}): {grid.elevation[r,c]:g} -> "
              f"{result.filled[r,c]:g}  (+{dz:g})")
    total = sum(deltas.values())
    area = grid.cell_area_m2
    print(f"  抬高量合计 {total:g} m × 像元面积 {area:g} m² = "
          f"{total * area:g} m³")
    print(f"  输出中的填洼体积: {summary['fill_volume_m3']:g} m³")
    ok = abs(total * area - summary["fill_volume_m3"]) < 1e-9
    print("  [核对通过]" if ok else "  [核对失败]")
    return ok


def check_row_order_invariance(out_dir: Path) -> bool:
    print(f"\n{'='*72}\n翻转测站表行序：结果应完全一致\n{'='*72}")
    ok = True
    for name in ("branching_and_serial", "unique_spill", "flat_platform"):
        spec = mk.SCENARIOS[name]
        d = out_dir / name
        reversed_csv = d / "stations_reversed.csv"
        mk.write_stations(reversed_csv, spec["stations"], order="reversed")

        def run(csv_path: Path):
            g = load_dem(d / "dem.tif")
            s = load_stations(csv_path, g)
            return delineate(g, s)

        r1, m1 = run(d / "stations.csv")
        r2, m2 = run(reversed_csv)
        same = (
            np.array_equal(r1.direction, r2.direction)
            and np.array_equal(r1.filled, r2.filled)
            and m1["fill_volume_m3"] == m2["fill_volume_m3"]
            and m1["unassigned_area_m2"] == m2["unassigned_area_m2"]
            and {x["outlet_id"]: x["total_upstream_area_m2"]
                 for x in m1["stations"]}
            == {x["outlet_id"]: x["total_upstream_area_m2"]
                for x in m2["stations"]}
            and {x["outlet_id"]: x["exclusive_area_m2"]
                 for x in m1["stations"]}
            == {x["outlet_id"]: x["exclusive_area_m2"]
                for x in m2["stations"]}
            and {x["outlet_id"]: x["station_index"] for x in m1["stations"]}
            == {x["outlet_id"]: x["station_index"] for x in m2["stations"]}
        )
        print(f"  {name}: {'一致' if same else '不一致'}")
        ok = ok and same
    print("  [核对通过]" if ok else "  [核对失败]")
    return ok


def check_anisotropy(out_dir: Path) -> bool:
    print(f"\n{'='*72}\n长宽不同导致最陡方向变化\n{'='*72}")
    variants = mk.build_anisotropy_variants(out_dir / "anisotropy")
    got = {}
    for name, spec in variants.items():
        grid = load_dem(Path(spec["dem"]))
        st = load_stations(Path(spec["stations"]), grid)
        result, _ = delineate(grid, st)
        center = int(result.direction[1, 1])
        got[name] = center
        print(f"  {name}: dx={spec['dx']:g}, dy={spec['dy']:g} -> "
              f"中心格流向 {center} ({DIR_NAMES[center]})")
    ok = got["anisotropy_wide"] == 3 and got["anisotropy_tall"] == 1
    print("  手算：宽像元时 2/50 > 2/200 (东)；高像元时 2/50 (北)。")
    print("  [核对通过]" if ok else "  [核对失败]")
    return ok


def expect_failure(title: str, args: list[str], expected_code: int,
                   needle: str) -> bool:
    print(f"\n  错误输入: {title}")
    r = cli(args)
    line = (r.stderr or r.stdout).strip().splitlines()
    detail = line[-1] if line else "(无输出)"
    ok = r.returncode == expected_code and needle in (r.stderr + r.stdout)
    print(f"    退出码 {r.returncode} (期望 {expected_code})，原因: {detail}")
    return ok


def check_error_inputs(out_dir: Path) -> bool:
    print(f"\n{'='*72}\n错误输入必须失败并解释原因\n{'='*72}")
    base = out_dir
    bad_dir = base / "_bad"
    bad_dir.mkdir(exist_ok=True)
    ok = True

    def d(name: str) -> Path:
        return base / name

    # 孤立块
    ok &= expect_failure(
        "没有外圈出口的孤立块", [
            "delineate", "--dem", str(d("isolated_block") / "dem.tif"),
            "--stations", str(d("isolated_block") / "stations.csv"),
            "--output-dir", str(bad_dir / "o1")], 3, "没有任何外圈出口")

    # 测站落在 NoData 岛
    on_island = bad_dir / "on_island.csv"
    mk.write_stations(on_island, [("ON", 3, 3)])
    ok &= expect_failure(
        "测站落在 NoData 岛", [
            "delineate", "--dem", str(d("nodata_island") / "dem.tif"),
            "--stations", str(on_island),
            "--output-dir", str(bad_dir / "o2")], 2, "无效高程格")

    # 两个不同 ID 同格
    two_ids = bad_dir / "two_ids.csv"
    mk.write_stations(two_ids, [("A", 1, 1), ("B", 1, 1)])
    ok &= expect_failure(
        "两个不同 ID 占同一格", [
            "delineate", "--dem", str(d("unique_spill") / "dem.tif"),
            "--stations", str(two_ids),
            "--output-dir", str(bad_dir / "o3")], 2, "两个不同测站占同一格")

    # 同一 ID 指向不同格
    same_id = bad_dir / "same_id.csv"
    mk.write_stations(same_id, [("A", 1, 1), ("A", 2, 2)])
    ok &= expect_failure(
        "同一 ID 指向不同格", [
            "delineate", "--dem", str(d("unique_spill") / "dem.tif"),
            "--stations", str(same_id),
            "--output-dir", str(bad_dir / "o4")], 2, "同一 outlet_id")

    # 行列在图外
    outside = bad_dir / "outside.csv"
    mk.write_stations(outside, [("OUT", 99, 0)])
    ok &= expect_failure(
        "测站落在图外", [
            "delineate", "--dem", str(d("unique_spill") / "dem.tif"),
            "--stations", str(outside),
            "--output-dir", str(bad_dir / "o5")], 2, "在图外")

    # 旋转仿射
    import rasterio
    from rasterio.transform import Affine
    rotated = bad_dir / "rotated.tif"
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    h, w = matrix.shape
    with rasterio.open(
            rotated, "w", driver="GTiff", width=w, height=h, count=1,
            dtype="float64", crs=mk.DEM_CRS,
            transform=Affine(100, 30, 0, 30, -100, h * 100)) as ds:
        ds.write(matrix, 1)
    ok &= expect_failure(
        "旋转/错切仿射", [
            "delineate", "--dem", str(rotated),
            "--stations", str(d("unique_spill") / "stations.csv"),
            "--output-dir", str(bad_dir / "o6")], 2, "旋转")

    # 地理坐标
    geo = bad_dir / "geo.tif"
    with rasterio.open(
            geo, "w", driver="GTiff", width=w, height=h, count=1,
            dtype="float64", crs="EPSG:4326",
            transform=Affine(0.001, 0, 0, 0, -0.001, h * 0.001)) as ds:
        ds.write(matrix, 1)
    ok &= expect_failure(
        "地理坐标 CRS", [
            "delineate", "--dem", str(geo),
            "--stations", str(d("unique_spill") / "stations.csv"),
            "--output-dir", str(bad_dir / "o7")], 2, "地理坐标")

    # 英尺投影
    ft = bad_dir / "feet.tif"
    with rasterio.open(
            ft, "w", driver="GTiff", width=w, height=h, count=1,
            dtype="float64",
            crs="+proj=utm +zone=49 +datum=WGS84 +units=ft +type=crs",
            transform=Affine(300, 0, 0, 0, -300, h * 300)) as ds:
        ds.write(matrix, 1)
    ok &= expect_failure(
        "非米制投影 (英尺)", [
            "delineate", "--dem", str(ft),
            "--stations", str(d("unique_spill") / "stations.csv"),
            "--output-dir", str(bad_dir / "o8")], 2, "不是米")

    print("  [核对通过]" if ok else "  [核对失败]")
    return ok


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        tempfile.mkdtemp(prefix="delineate_demo_"))
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"工作目录: {out_dir}")

    # 1. 生成合成 DEM 场景
    print(f"\n{'='*72}\n生成合成 DEM 场景\n{'='*72}")
    mk.generate_all(out_dir)
    print(f"  场景: {', '.join(sorted(p.name for p in out_dir.iterdir() if p.is_dir()))}")

    all_ok = True

    # 2. 实际调用 CLI 处理每个场景
    for name in ("unique_spill", "flat_platform", "nodata_island",
                 "branching_and_serial"):
        sc = mk.build_scenario(name, out_dir / name)
        cli_out = out_dir / "_cli" / name
        r = cli(["delineate", "--dem", sc["dem"], "--stations", sc["stations"],
                 "--output-dir", str(cli_out)])
        print(f"\n[CLI] {name}: 退出码 {r.returncode}")
        if r.returncode != 0:
            print(r.stderr)
            all_ok = False
            continue
        payload = json.loads(r.stdout)
        print(f"  产出: {', '.join(payload['rasters'])}")
        all_ok &= describe_scenario(name, sc, out_dir)

    # 3. 手算与交叉核对
    all_ok &= check_unique_spill_by_hand(out_dir)
    all_ok &= check_anisotropy(out_dir)
    all_ok &= check_row_order_invariance(out_dir)
    all_ok &= check_error_inputs(out_dir)

    print(f"\n{'='*72}\n总结\n{'='*72}")
    print("  产出是地形推导的汇水边界 (DEM -> 填洼 -> D8 -> 上游面积/测站分区)，"
          "不是洪水水动力模拟。")
    print("  结果依赖：填洼只抬高、D8 最陡下降、等坡度固定顺序、平台反向 BFS、"
          "外圈 OUTSIDE=8 / 无效=255。")
    if all_ok:
        print("  全部场景核对通过。")
        return 0
    print("  存在核对失败项，见上方 [核对失败]。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
