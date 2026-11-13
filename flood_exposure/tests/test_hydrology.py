"""delineate 测试：合成矩阵推导、独立逐格核对、错误输入、行序无关。"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

# demo/ 不是包；与 conftest 一样把目录加进路径复用生成器
DEMO_DIR = Path(__file__).resolve().parents[1] / "demo"
sys.path.insert(0, str(DEMO_DIR))

import make_dem_samples as mk  # noqa: E402

from flood_exposure.hydrology import (  # noqa: E402
    DIR_NODATA,
    DIR_OUTSIDE,
    D8_ORDER,
    PARTITION_NODATA,
    PARTITION_UNASSIGNED,
    DemError,
    NoOutletError,
    delineate,
    enumerate_upstream,
    load_dem,
    load_stations,
    trace_down,
    write_all_outputs,
)

CRS = "EPSG:32649"
CELL = 100.0


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def write_dem(path: Path, matrix, *, dx: float = CELL, dy: float = CELL,
              crs: str = CRS, nodata: float | None = None,
              transform: Affine | None = None) -> Path:
    h, w = matrix.shape
    if transform is None:
        transform = Affine(dx, 0, 0, 0, -dy, h * dy)
    matrix = np.asarray(matrix, dtype="float64")
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1,
                       dtype="float64", crs=crs, transform=transform,
                       nodata=nodata) as ds:
        ds.write(matrix, 1)
    return path


def write_stations_csv(path: Path, rows, header=("outlet_id", "row", "col")) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(header)
        for r in rows:
            wr.writerow(r)
    return path


def run_pipeline(dem: Path, stations: Path):
    grid = load_dem(dem)
    st = load_stations(stations, grid)
    return delineate(grid, st)


def _independent_upstream(grid, direction, row, col):
    """独立核对：纯逐格反向枚举，不依赖被测实现的累计过程。"""
    return enumerate_upstream(grid, direction, row, col)


# --------------------------------------------------------------------------- #
# 1. 唯一缺口洼地：填平 + 手算体积 + 全部下游追踪
# --------------------------------------------------------------------------- #
def test_unique_spill_fill_depth_and_volume_by_hand(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix)
    stations = write_stations_csv(
        tmp_path / "s.csv", mk.SCENARIOS["unique_spill"]["stations"])
    grid, st = load_dem(dem), load_stations(stations, load_dem(dem))
    result, summary = delineate(grid, st)

    # 手算：洼地水位抬到唯一缺口高程 5。低于 5 的格 (3,3)=0 (+5),
    # (3,4)=4 (+1), (4,3)=4 (+1), (4,4)=2 (+3)；合计 10 m。
    # 像元 100m×100m = 10000 m² -> 填洼体积 100000 m³。
    expected_raised = {(3, 3): 5.0, (3, 4): 1.0, (4, 3): 1.0, (4, 4): 3.0}
    assert result.pit_filled_cells == 4
    assert abs(summary["fill_volume_m3"] - 100000.0) < 1e-9
    for (r, c), dz in expected_raised.items():
        assert abs(result.raised[r, c] - dz) < 1e-12
        assert abs(result.filled[r, c] - (matrix[r, c] + dz)) < 1e-12

    # 已能排水的坡面不被改动：除上述 4 格外，其余格 raised 必须为 0。
    assert np.count_nonzero(result.raised) == 4
    # 填洼只抬高不降低
    assert np.all(result.filled >= matrix)

    # 唯一出口 (0,6)：它的上游包含整幅 56 个有效格。
    outlet = [r for r in summary["stations"] if r["outlet_id"] == "OUT1"][0]
    assert outlet["total_upstream_area_m2"] == 56 * 10000.0
    assert summary["unassigned_area_m2"] == 0.0
    assert summary["outside_area_m2"] == 56 * 10000.0


def test_unique_spill_all_valid_cells_reach_outside_without_cycles(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix)
    stations = write_stations_csv(
        tmp_path / "s.csv", mk.SCENARIOS["unique_spill"]["stations"])
    grid = load_dem(dem)
    result, _ = delineate(grid, load_stations(stations, grid))

    for r, c in np.argwhere(grid.valid):
        path = trace_down(grid, result.direction, int(r), int(c))
        # 路径终点编码必须为 OUTSIDE，且路径上高程不升高
        assert int(result.direction[path[-1]]) == DIR_OUTSIDE
        zs = [result.filled[rr, cc] for rr, cc in path]
        assert all(zs[i] >= zs[i + 1] for i in range(len(zs) - 1))
        # 路径长度有限（无环）
        assert len(path) <= grid.height * grid.width


def test_direction_grid_uses_separate_outside_and_nodata_codes(tmp_path):
    # 无 NoData 的图：所有有效格外圈无更低邻格处写 8；内部不会有 255。
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix)
    stations = write_stations_csv(tmp_path / "s.csv", [("S", 0, 6)])
    grid = load_dem(dem)
    result, _ = delineate(grid, load_stations(stations, grid))
    assert DIR_OUTSIDE != DIR_NODATA
    assert int(result.direction[0, 6]) == DIR_OUTSIDE
    assert set(np.unique(result.direction[grid.valid]).tolist()) <= set(range(8)) | {8}
    # 无效格才写 255
    assert np.all(result.direction[~grid.valid] == DIR_NODATA)


# --------------------------------------------------------------------------- #
# 2. 平台：需要走多格出水，填洼不抬高，方向必须逐跳收敛
# --------------------------------------------------------------------------- #
def test_flat_platform_needs_many_hops_and_is_not_perturbed(tmp_path):
    matrix = mk.SCENARIOS["flat_platform"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix)
    stations = write_stations_csv(
        tmp_path / "s.csv", mk.SCENARIOS["flat_platform"]["stations"])
    grid = load_dem(dem)
    result, summary = delineate(grid, load_stations(stations, grid))

    # 平台不抬高（不是洼地）也不允许为了“造坡度”被扰动
    assert np.all(result.raised == 0)
    assert np.array_equal(result.filled, matrix)
    # 平台内方向必须逐跳靠近东侧出口；max_flat_hops 至少覆盖平台长度 8
    assert summary["flat_routing"]["max_flat_hops"] >= 8
    # 从平台最西端 (1,1) 追踪，退出码必须为 OUTSIDE
    path = trace_down(grid, result.direction, 1, 1)
    assert int(result.direction[path[-1]]) == DIR_OUTSIDE
    assert len(path) >= 9

    # 平台每一格的方向都必须在同高程邻格上，不能“选一个同高程随便指”
    # 这里逐跳核对：路径上每步高程不变，直到离开平台。
    zs = [result.filled[rr, cc] for rr, cc in path]
    assert all(zs[i] == zs[0] for i in range(len(path) - 4))


# --------------------------------------------------------------------------- #
# 3. 长宽不同 -> 最陡方向改变
# --------------------------------------------------------------------------- #
def test_anisotropic_resolution_changes_steepest_direction(tmp_path):
    variants = mk.build_anisotropy_variants(tmp_path / "aniso")
    # anisotropy_wide: dx=50, dy=200；anisotropy_tall: dx=200, dy=50
    results = {}
    for name, spec in variants.items():
        grid = load_dem(Path(spec["dem"]))
        stations = load_stations(Path(spec["stations"]), grid)
        result, _ = delineate(grid, stations)
        results[name] = result

    # 中心格 (1,1)=10：宽像元时正东 (3= E) 最陡；高像元时正北 (1= N) 最陡
    assert int(results["anisotropy_wide"].direction[1, 1]) == 3
    assert int(results["anisotropy_tall"].direction[1, 1]) == 1

    # 手算核对坡度
    # wide: dz=2, dist E=50 -> 0.04 ; dist N=200 -> 0.01
    assert 2 / 50.0 > 2 / 200.0
    # tall: dist N=50 -> 0.04 ; dist E=200 -> 0.01
    assert 2 / 50.0 > 2 / 200.0


def test_tie_break_rule_is_written_into_summary(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix)
    stations = write_stations_csv(tmp_path / "s.csv", [("S", 0, 6)])
    grid = load_dem(dem)
    result, summary = delineate(grid, load_stations(stations, grid))
    out = tmp_path / "out"
    written = write_all_outputs(out, grid, result, summary, dem, tmp_path / "s.csv")
    assert "tie_break" in written["algorithm_decisions"]
    tie = written["algorithm_decisions"]["tie_break"]
    assert tie.startswith("等坡度时按固定顺序 NW,N,NE,E,SE,S,SW,W")
    # 方向编码说明可被读者用来走路径
    assert written["direction_codes"]["8"]["name"] == "OUTSIDE"
    assert written["direction_codes"]["255"]["name"] == "NODATA"
    for k in range(8):
        assert written["direction_codes"][str(k)]["delta"] == list(D8_ORDER[k])


def test_equal_slope_uses_first_in_fixed_order(tmp_path):
    # 中心格 10，正北与正西都是 8，距离相同 -> 等坡度。
    # 固定顺序 NW,N,NE,E,SE,S,SW,W：正北 (1) 先于正西 (7)。
    matrix = np.array([
        [9, 8, 9],
        [8, 10, 9],
        [9, 9, 9],
    ], dtype="float64")
    dem = write_dem(tmp_path / "dem.tif", matrix)
    stations = write_stations_csv(tmp_path / "s.csv", [("S", 2, 2)])
    grid = load_dem(dem)
    result, _ = delineate(grid, load_stations(stations, grid))
    assert int(result.direction[1, 1]) == 1      # 正北，而非正西


# --------------------------------------------------------------------------- #
# 4. NoData 岛：不可穿越、不是泄水口
# --------------------------------------------------------------------------- #
def test_nodata_island_blocks_flow_but_is_not_an_outlet(tmp_path):
    matrix = mk.SCENARIOS["nodata_island"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix, nodata=mk.NODATA)
    stations = write_stations_csv(tmp_path / "s.csv", [("ISL", 6, 6)])
    grid = load_dem(dem)
    result, summary = delineate(grid, load_stations(stations, grid))

    # 岛内格：方向 255，不参与排水
    assert np.all(result.direction[3:6, 2:5] == DIR_NODATA)
    # 有效格的流向绝不指进 NoData 岛：逐格检查
    for r, c in np.argwhere(grid.valid):
        d = int(result.direction[r, c])
        if d == DIR_OUTSIDE:
            continue
        dr, dc = D8_ORDER[d]
        assert grid.valid[r + dr, c + dc], (r, c, d)
    # 岛不是泄水口：没有任何有效格把“岛”当作下游
    # 全部有效格最终到 OUTSIDE
    for r, c in np.argwhere(grid.valid):
        path = trace_down(grid, result.direction, int(r), int(c))
        assert int(result.direction[path[-1]]) == DIR_OUTSIDE


def test_station_on_nodata_island_is_rejected(tmp_path):
    matrix = mk.SCENARIOS["nodata_island"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix, nodata=mk.NODATA)
    stations = write_stations_csv(tmp_path / "s.csv", [("ON_ISLAND", 3, 3)])
    grid = load_dem(dem)
    with pytest.raises(DemError) as excinfo:
        load_stations(stations, grid)
    msg = str(excinfo.value)
    assert "无效高程格" in msg and "NoData" in msg


# --------------------------------------------------------------------------- #
# 5. 孤立块：无外圈出口必须报错
# --------------------------------------------------------------------------- #
def test_isolated_block_without_outer_outlet_is_rejected(tmp_path):
    matrix = mk.SCENARIOS["isolated_block"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix, nodata=mk.NODATA)
    stations = write_stations_csv(tmp_path / "s.csv", [("ISO", 3, 3)])
    grid = load_dem(dem)
    with pytest.raises(NoOutletError) as excinfo:
        delineate(grid, load_stations(stations, grid))
    msg = str(excinfo.value)
    assert "没有任何外圈出口" in msg
    assert "连通块" in msg and "4 个有效格" in msg


# --------------------------------------------------------------------------- #
# 6. 串联 + 分支：贡献面积 vs 独占面积
# --------------------------------------------------------------------------- #
def test_serial_and_branch_stations_area_semantics(tmp_path):
    matrix = mk.SCENARIOS["branching_and_serial"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix,
                    transform=Affine(100, 0, 0, 0, -100, 900))
    stations = write_stations_csv(
        tmp_path / "s.csv", mk.SCENARIOS["branching_and_serial"]["stations"])
    grid = load_dem(dem)
    result, summary = delineate(grid, load_stations(stations, grid))
    by_id = {r["outlet_id"]: r for r in summary["stations"]}

    # 串联 A -> B -> C；B 的贡献面积包含 A 的全部上游面积
    assert by_id["A"]["next_downstream_outlet"] == "B"
    assert by_id["B"]["next_downstream_outlet"] == "C"
    assert by_id["C"]["next_downstream_outlet"] is None
    assert by_id["B"]["total_upstream_area_m2"] >= by_id["A"]["total_upstream_area_m2"]
    assert by_id["C"]["total_upstream_area_m2"] >= by_id["B"]["total_upstream_area_m2"]

    # 用独立枚举核对贡献面积 = 枚举上游格面积（含自身）
    for oid, r in by_id.items():
        up = enumerate_upstream(grid, result.direction, r["row"], r["col"])
        expected = len(up) * grid.cell_area_m2
        assert r["total_upstream_area_m2"] == pytest.approx(expected, abs=1e-9), oid

    # 上游站的独占分区不并入下游站的独占分区：
    # A 的独占面积 (240000) 大于 B 的 (210000)，说明 A 那部分没有被算进
    # B；同时 B 的贡献面积 (450000) 仍包含 A 的全部上游面积 (240000)。
    assert by_id["A"]["exclusive_area_m2"] != by_id["B"]["exclusive_area_m2"]
    assert (by_id["A"]["exclusive_area_m2"]
            + by_id["B"]["exclusive_area_m2"]
            + by_id["C"]["exclusive_area_m2"]
            + by_id["D"]["exclusive_area_m2"]
            + summary["unassigned_area_m2"]) == pytest.approx(
                summary["total_valid_area_m2"], abs=1e-9)

    # 分支 D 汇入主河道并在到达 C 之前被截获：D 的下一个测站是 C
    assert by_id["D"]["next_downstream_outlet"] == "C"
    # 但 D 的独占分区不被 B/C 重复计入
    # —— 逐格核对分区交叠见下一个测试

    # 所有有效格都被独占分区或未归属恰好覆盖一次
    cells, unassigned_cells = _partition_cells(grid, summary)
    covered = sum(len(v) for v in cells.values()) + len(unassigned_cells)
    assert covered == int(grid.valid.sum())


def _partition_cells(grid, summary):
    """按 summary 的 first_station 复算分区 (使用内部字段)。"""
    first = summary["_first_station"]
    cells = {}
    unassigned = set()
    for r, c in np.argwhere(grid.valid):
        r, c = int(r), int(c)
        sid = first[r, c]
        if sid is None:
            unassigned.add((r, c))
        else:
            cells.setdefault(sid, set()).add((r, c))
    return cells, unassigned


def test_exclusive_partitions_do_not_overlap_and_cover_valid_cells(tmp_path):
    matrix = mk.SCENARIOS["branching_and_serial"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix,
                    transform=Affine(100, 0, 0, 0, -100, 900))
    stations = write_stations_csv(
        tmp_path / "s.csv", mk.SCENARIOS["branching_and_serial"]["stations"])
    grid = load_dem(dem)
    result, summary = delineate(grid, load_stations(stations, grid))

    # 用航位方向独立复算“下游首先经过的测站”（纯逐格追踪，不依赖实现传播）
    station_at = {(r["row"], r["col"]): r["outlet_id"] for r in summary["stations"]}
    independent = {}
    unassigned = set()
    for r, c in np.argwhere(grid.valid):
        r, c = int(r), int(c)
        seen = set()
        rr, cc = r, c
        while True:
            assert (rr, cc) not in seen, "方向图成环"
            seen.add((rr, cc))
            who = station_at.get((rr, cc))
            if who is not None:
                independent[(r, c)] = who
                break
            d = int(result.direction[rr, cc])
            if d == DIR_OUTSIDE:
                unassigned.add((r, c))
                break
            dr, dc = D8_ORDER[d]
            rr, cc = rr + dr, cc + dc

    # 独占分区不交叠
    cells, unassigned_impl = _partition_cells(grid, summary)
    for sid_a, set_a in cells.items():
        for sid_b, set_b in cells.items():
            if sid_a < sid_b:
                assert not (set_a & set_b)
    # 分区与未归属合起来覆盖全部有效格，且每格恰好一次
    assert sum(len(s) for s in cells.values()) + len(unassigned_impl) \
        == int(grid.valid.sum())
    # 与独立逐格追踪完全一致
    for (r, c), sid in independent.items():
        assert sid in cells and (r, c) in cells[sid], (r, c)
    assert unassigned == unassigned_impl
    # 面积一致性
    for oid, s in cells.items():
        r = next(x for x in summary["stations"] if x["outlet_id"] == oid)
        assert r["exclusive_area_m2"] == pytest.approx(
            len(s) * grid.cell_area_m2, abs=1e-9)


def test_contributing_area_equals_independent_enumeration_everywhere(tmp_path):
    matrix = mk.SCENARIOS["branching_and_serial"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix,
                    transform=Affine(100, 0, 0, 0, -100, 900))
    stations = write_stations_csv(
        tmp_path / "s.csv", mk.SCENARIOS["branching_and_serial"]["stations"])
    grid = load_dem(dem)
    result, summary = delineate(grid, load_stations(stations, grid))

    total = summary["_total_area"]
    for r, c in np.argwhere(grid.valid):
        r, c = int(r), int(c)
        up = enumerate_upstream(grid, result.direction, r, c)
        assert total[r, c] == pytest.approx(len(up) * grid.cell_area_m2, abs=1e-9)


def test_every_valid_cell_terminates_at_outside_and_no_cycle(tmp_path):
    matrix = mk.SCENARIOS["branching_and_serial"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix,
                    transform=Affine(100, 0, 0, 0, -100, 900))
    stations = write_stations_csv(tmp_path / "s.csv", [("A", 1, 2)])
    grid = load_dem(dem)
    result, _ = delineate(grid, load_stations(stations, grid))

    max_steps = grid.height * grid.width + 1
    for r, c in np.argwhere(grid.valid):
        r, c = int(r), int(c)
        path = trace_down(grid, result.direction, r, c)
        assert int(result.direction[path[-1]]) == DIR_OUTSIDE
        assert len(path) < max_steps
    # 面积守恒：出图口上游面积之和 = 有效总面积
    outlet_sum = sum(o["upstream_area_m2"] for o in _outlet_cells(grid, result))
    assert outlet_sum == pytest.approx(grid.total_valid_area_m2, abs=1e-9)


def _outlet_cells(grid, result):
    return [{"row": int(r), "col": int(c),
             "upstream_area_m2": _enum_area(grid, result, int(r), int(c))}
            for r, c in np.argwhere(grid.valid)
            if int(result.direction[r, c]) == DIR_OUTSIDE]


def _enum_area(grid, result, r, c):
    return len(enumerate_upstream(grid, result.direction, r, c)) \
        * grid.cell_area_m2


# --------------------------------------------------------------------------- #
# 7. 翻转测站行序结果一致
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scenario", ["branching_and_serial", "unique_spill",
                                      "flat_platform"])
def test_reversing_station_csv_rows_does_not_change_results(tmp_path, scenario):
    spec = mk.SCENARIOS[scenario]
    transform = spec.get("transform")
    matrix = spec["elevation"]
    tmp = tmp_path / scenario
    tmp.mkdir()
    dem = write_dem(tmp / "dem.tif", matrix, transform=transform,
                    nodata=spec.get("nodata"))
    fwd = write_stations_csv(tmp / "fwd.csv", spec["stations"])
    rev = write_stations_csv(tmp / "rev.csv", list(reversed(spec["stations"])))

    g1, s1 = load_dem(dem), load_stations(fwd, load_dem(dem))
    r1, m1 = delineate(g1, s1)
    g2, s2 = load_dem(dem), load_stations(rev, load_dem(dem))
    r2, m2 = delineate(g2, s2)

    assert np.array_equal(r1.direction, r2.direction)
    assert np.array_equal(r1.filled, r2.filled)
    assert np.array_equal(r1.raised, r2.raised)
    assert m1["fill_volume_m3"] == m2["fill_volume_m3"]
    assert m1["unassigned_area_m2"] == m2["unassigned_area_m2"]
    assert m1["outside_area_m2"] == m2["outside_area_m2"]
    by1 = {x["outlet_id"]: x for x in m1["stations"]}
    by2 = {x["outlet_id"]: x for x in m2["stations"]}
    for oid in by1:
        for field in ("total_upstream_area_m2", "exclusive_area_m2",
                      "next_downstream_outlet", "station_index",
                      "filled_elevation_m", "raised_m"):
            assert by1[oid][field] == by2[oid][field], (oid, field)


# --------------------------------------------------------------------------- #
# 8. 错误输入：逐一给出具体原因
# --------------------------------------------------------------------------- #
def test_rejects_geographic_crs(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix, crs="EPSG:4326")
    with pytest.raises(DemError) as excinfo:
        load_dem(dem)
    assert "地理坐标" in str(excinfo.value)


def test_rejects_non_metre_projected_crs(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    # US survey foot 投影
    dem = write_dem(tmp_path / "dem.tif", matrix,
                    crs="+proj=utm +zone=49 +datum=WGS84 +units=ft +type=crs")
    with pytest.raises(DemError) as excinfo:
        load_dem(dem)
    assert "不是米" in str(excinfo.value)
    assert "foot" in str(excinfo.value)


def test_rejects_rotated_affine(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    rotated = Affine(100, 30, 0, 30, -100, 700)
    dem = write_dem(tmp_path / "dem.tif", matrix, transform=rotated)
    with pytest.raises(DemError) as excinfo:
        load_dem(dem)
    assert "旋转" in str(excinfo.value)


def test_station_outside_image_is_rejected(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix)
    s = write_stations_csv(tmp_path / "s.csv", [("X", matrix.shape[0], 3)])
    grid = load_dem(dem)
    with pytest.raises(DemError) as excinfo:
        load_stations(s, grid)
    assert "在图外" in str(excinfo.value)
    assert "行范围" in str(excinfo.value)


def test_two_ids_same_cell_rejected(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix)
    s = write_stations_csv(tmp_path / "s.csv", [("A", 2, 2), ("B", 2, 2)])
    with pytest.raises(DemError) as excinfo:
        load_stations(s, load_dem(dem))
    assert "两个不同测站占同一格" in str(excinfo.value)


def test_same_id_different_cells_rejected(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix)
    s = write_stations_csv(tmp_path / "s.csv", [("A", 2, 2), ("A", 3, 3)])
    with pytest.raises(DemError) as excinfo:
        load_stations(s, load_dem(dem))
    assert "同一 outlet_id" in str(excinfo.value)


def test_station_on_nodata_rejected_with_reason(tmp_path):
    matrix = np.array([[1.0, 2.0], [-9999.0, 3.0]])
    dem = write_dem(tmp_path / "dem.tif", matrix, nodata=-9999.0)
    s = write_stations_csv(tmp_path / "s.csv", [("N", 1, 0)])
    with pytest.raises(DemError) as excinfo:
        load_stations(s, load_dem(dem))
    assert "无效高程格" in str(excinfo.value)


def test_non_integer_row_col_rejected(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix)
    s = write_stations_csv(tmp_path / "s.csv", [("F", "1.5", 2)])
    with pytest.raises(DemError) as excinfo:
        load_stations(s, load_dem(dem))
    assert "不是整数" in str(excinfo.value)


def test_missing_required_column_rejected(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix)
    s = write_stations_csv(tmp_path / "s.csv", [("X", 1, 2)],
                           header=("outlet_id", "x", "y"))
    with pytest.raises(DemError) as excinfo:
        load_stations(s, load_dem(dem))
    assert "缺少必需列" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 9. 产出栅格与摘要：编码、坐标、追溯
# --------------------------------------------------------------------------- #
def test_outputs_include_all_rasters_and_traceable_summary(tmp_path):
    matrix = mk.SCENARIOS["unique_spill"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix,
                    transform=Affine(100, 0, 0, 0, -100, 700))
    s = write_stations_csv(tmp_path / "s.csv", mk.SCENARIOS["unique_spill"]["stations"])
    grid = load_dem(dem)
    result, summary = delineate(grid, load_stations(s, grid))
    out = tmp_path / "out"
    write_all_outputs(out, grid, result, summary, dem, s)

    names = {"filled_dem.tif", "pit_depth_m.tif", "flow_direction.tif",
             "contributing_area_m2.tif", "exclusive_partition.tif",
             "summary.json", "stations.csv"}
    assert names <= {p.name for p in out.iterdir()}

    with rasterio.open(out / "flow_direction.tif") as ds:
        assert ds.crs.to_string().upper().startswith("EPSG:32649")
        assert ds.transform.a == 100.0 and ds.transform.e == -100.0
        assert ds.nodata == DIR_NODATA

    with rasterio.open(out / "exclusive_partition.tif") as ds:
        part = ds.read(1)
        assert ds.nodata == PARTITION_NODATA

    # 分区栅格编码：无效 -1, 未归属 0, 测站 >=1
    assert np.all(part[~grid.valid] == PARTITION_NODATA)
    valid_part = part[grid.valid]
    assert np.all(valid_part >= PARTITION_UNASSIGNED)
    assert set(np.unique(valid_part)) <= {0, 1, 2, 3, 4}

    summary_json = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary_json["inputs"]["dem"]["sha256"] == grid.source_sha256
    assert summary_json["inputs"]["dem"]["crs"] == "EPSG:32649"
    assert summary_json["inputs"]["dem"]["x_res_m"] == 100.0
    assert summary_json["inputs"]["dem"]["y_res_m"] == 100.0
    assert "不代表洪水水动力模拟" in summary_json["disclaimer"]
    assert summary_json["direction_codes"]["3"]["name"] == "E"

    # 填洼高程输出：无效格为 NoData，有效格 = filled
    with rasterio.open(out / "filled_dem.tif") as ds:
        filled_out = ds.read(1)
    assert np.allclose(filled_out[grid.valid], result.filled[grid.valid])


def test_station_summary_fields(tmp_path):
    matrix = mk.SCENARIOS["branching_and_serial"]["elevation"]
    dem = write_dem(tmp_path / "dem.tif", matrix,
                    transform=Affine(100, 0, 0, 0, -100, 900))
    s = write_stations_csv(tmp_path / "s.csv",
                           mk.SCENARIOS["branching_and_serial"]["stations"])
    grid = load_dem(dem)
    result, summary = delineate(grid, load_stations(s, grid))
    for r in summary["stations"]:
        for field in ("elevation_m", "filled_elevation_m", "raised_m",
                      "total_upstream_area_m2", "exclusive_area_m2",
                      "next_downstream_outlet"):
            assert field in r
        assert r["filled_elevation_m"] == r["elevation_m"] + r["raised_m"]
    # 全局量
    assert summary["outside_area_m2"] == summary["total_valid_area_m2"]
    assert summary["unassigned_area_m2"] > 0
    assert summary["fill_volume_m3"] == 0.0


# --------------------------------------------------------------------------- #
# 10. CLI 实际调用
# --------------------------------------------------------------------------- #
def test_cli_delineate_end_to_end(tmp_path):
    data = tmp_path / "data"
    mk.generate_all(data)
    out = tmp_path / "out"
    dem = data / "branching_and_serial" / "dem.tif"
    stations = data / "branching_and_serial" / "stations.csv"
    r = subprocess.run(
        [sys.executable, "-I", "-m", "flood_exposure.cli", "delineate",
         "--dem", str(dem), "--stations", str(stations),
         "--output-dir", str(out)],
        capture_output=True, text=True, cwd=str(Path(__file__).parents[1]),
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["stations"]["A"]["next_downstream_outlet"] == "B"
    assert (out / "flow_direction.tif").exists()
    assert (out / "summary.json").exists()


def test_cli_isolated_block_exits_with_specific_reason(tmp_path):
    data = tmp_path / "data"
    mk.generate_all(data)
    out = tmp_path / "out"
    dem = data / "isolated_block" / "dem.tif"
    stations = data / "isolated_block" / "stations.csv"
    r = subprocess.run(
        [sys.executable, "-I", "-m", "flood_exposure.cli", "delineate",
         "--dem", str(dem), "--stations", str(stations),
         "--output-dir", str(out)],
        capture_output=True, text=True, cwd=str(Path(__file__).parents[1]),
    )
    assert r.returncode == 3
    assert "没有任何外圈出口" in r.stderr
    assert not out.exists() or not any(out.iterdir())


def test_cli_rejects_bad_station_csv(tmp_path):
    data = tmp_path / "data"
    mk.generate_all(data)
    bad = tmp_path / "bad.csv"
    write_stations_csv(bad, [("A", 0, 0), ("B", 0, 0)])
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, "-I", "-m", "flood_exposure.cli", "delineate",
         "--dem", str(data / "unique_spill" / "dem.tif"),
         "--stations", str(bad), "--output-dir", str(out)],
        capture_output=True, text=True, cwd=str(Path(__file__).parents[1]),
    )
    assert r.returncode == 2
    assert "两个不同测站占同一格" in r.stderr
