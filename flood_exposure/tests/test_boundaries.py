"""边界加载、重叠分摊与“不重复全额计数”测试。"""
from __future__ import annotations

from flood_exposure.basins import load_basins
from flood_exposure.config import QualityFlag
from flood_exposure.hashing import sha256_file
from flood_exposure.population import (
    build_all_weight_tables,
    load_population,
    overlap_report,
)


def test_v1_basins_load_with_repair_flag(sample_dir, layout):
    bpath = layout["files"]["boundary_v1"]
    coll = load_basins(
        bpath, "v1", sha256_file(bpath), "basin_id", "EPSG:6933")
    ids = {b.basin_id for b in coll.basins}
    assert ids == {"A", "B", "C", "D"}
    assert coll.failed_ids() == []
    d = coll.by_id()["D"]
    assert QualityFlag.INVALID_GEOMETRY_REPAIRED in d.flags


def test_v2_degenerate_basin_fails_repair(sample_dir, layout):
    bpath = layout["files"]["boundary_v2"]
    coll = load_basins(
        bpath, "v2", sha256_file(bpath), "basin_id", "EPSG:6933")
    assert "D" in coll.failed_ids()
    d = coll.by_id()["D"]
    assert QualityFlag.GEOMETRY_REPAIR_FAILED in d.flags
    assert d.geom_work is None
    # 新增流域 E 正常
    assert "E" in {b.basin_id for b in coll.usable_basins()}


def test_overlapping_basins_share_cells_by_area_not_double_counted(
        sample_dir, layout):
    bpath = layout["files"]["boundary_v1"]
    coll = load_basins(
        bpath, "v1", sha256_file(bpath), "basin_id", "EPSG:6933")
    pop = load_population(
        layout["files"]["population"],
        sha256_file(layout["files"]["population"]))
    tables = build_all_weight_tables(coll, pop, "EPSG:6933")
    overlaps = overlap_report(coll)

    # A、B 互相报告重叠 (B 在样例中同时与 C 邻接，故只断言 A-B 关系存在)
    assert any(o["other_basin_id"] == "B" for o in overlaps["A"])
    assert any(o["other_basin_id"] == "A" for o in overlaps["B"])

    a_cells = set(tables["A"].fractions)
    b_cells = set(tables["B"].fractions)
    shared = a_cells & b_cells
    assert shared, "A/B 应共享至少一个跨界人口格"
    for cell in shared:
        wa = tables["A"].fractions[cell]
        wb = tables["B"].fractions[cell]
        # 每个流域只拿到自己的相交面积份额；两方不可能同时全额占用同一格
        assert not (wa >= 1.0 and wb >= 1.0)
        # 两个份额都为正，且与重叠几何一致
        assert wa > 0.0 and wb > 0.0
        # 至少一方只拿到部分份额 -> 不存在重复全额计数
        assert min(wa, wb) < 1.0

    # 独立流域 C 与 A 不共享格 (B/C 在样例中邻接，属另一条重叠记录)
    c_cells = set(tables["C"].fractions)
    assert not (c_cells & a_cells)


def test_weight_fractions_sum_within_cell_for_disjoint_split(
        sample_dir, layout):
    """无重叠的相邻流域：共享格的份额之和应 <= 1（不超出格面积）。"""
    bpath = layout["files"]["boundary_v1"]
    coll = load_basins(
        bpath, "v1", sha256_file(bpath), "basin_id", "EPSG:6933")
    pop = load_population(
        layout["files"]["population"],
        sha256_file(layout["files"]["population"]))
    tables = build_all_weight_tables(coll, pop, "EPSG:6933")
    overlaps = overlap_report(coll)

    # 汇总每个人口格被所有流域占用的份额
    totals: dict[tuple[int, int], float] = {}
    for wt in tables.values():
        for cell, f in wt.fractions.items():
            totals[cell] = totals.get(cell, 0.0) + f
    # 任何被多个流域覆盖的格：无重叠时份额和 <=1；重叠格允许 >1 并已显式记录
    all_overlap_cells: set[tuple[int, int]] = set()
    for i, (idi, wti) in enumerate(tables.items()):
        for idj, wtj in list(tables.items())[i + 1:]:
            if any(o["other_basin_id"] == idj for o in overlaps[idi]):
                all_overlap_cells |= set(wti.fractions) & set(wtj.fractions)
    for cell, total in totals.items():
        if cell not in all_overlap_cells:
            assert total <= 1.0 + 1e-9
        else:
            # 重叠格不会出现两个流域同时全额计数
            full = [bid for bid, wt in tables.items()
                    if cell in wt.fractions and wt.fractions[cell] >= 1.0]
            assert len(full) <= 1
