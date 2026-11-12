"""发布语义测试：闸门拒绝、暂存再发布、进程中止、增量、查询、清单追溯。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from flood_exposure.config import Config, QualityFlag
from flood_exposure.publishing import (
    PublishGateError,
    StagedRun,
    current_release,
    list_releases,
)
from flood_exposure.query import (
    available_releases,
    load_exposure_rows,
    load_run_manifest,
    provenance,
    query_basin,
)
from flood_exposure.runner import run_pipeline


def test_full_run_publishes_and_manifest_is_traceable(
        inventory_v1, config, tmp_path):
    out = tmp_path / "out"
    summary = run_pipeline(inventory_v1, out, config, run_id="r1")
    assert summary["gate_passed"] is True
    rel = current_release(out)
    assert rel is not None and (rel / "SUCCESS").exists()
    assert (rel / "exposure.csv").exists()
    assert (rel / "run_manifest.json").exists()

    manifest = load_run_manifest(out)
    # 三个输入清单摘要都可回溯
    assert manifest["inputs"]["boundary"]["version"] == "v1"
    assert manifest["inputs"]["boundary"]["sha256"] == inventory_v1.boundary.sha256
    assert manifest["inputs"]["population"]["sha256"] == inventory_v1.population.sha256
    times = [d["forecast_time"] for d in manifest["inputs"]["depth_rasters"]]
    assert times == ["T00", "T06"]
    assert manifest["config"]["max_missing_fraction"] == config.max_missing_fraction


def test_nodata_gate_rejects_publish_without_zero_fill(
        inventory_v1, tmp_path):
    # 闸门收紧到 1%：C/D 流域的 NoData 必然超限
    strict = Config(max_missing_fraction=0.01, window_tile_size=64)
    out = tmp_path / "out"
    with pytest.raises(PublishGateError):
        run_pipeline(inventory_v1, out, strict, run_id="strict")
    # 没有任何完整发布，current 缺失
    assert current_release(out) is None
    assert list_releases(out) == []
    # 暂存区已被销毁
    assert not (out / "staging" / "strict").exists()


def test_staged_publish_keeps_previous_release_on_abort(
        inventory_v1, config, tmp_path, monkeypatch):
    out = tmp_path / "out"
    run_pipeline(inventory_v1, out, config, run_id="good")
    first = current_release(out)

    # 在第二次运行的权重构建阶段注入中止
    import flood_exposure.runner as runner_mod
    original = runner_mod.build_weight_table
    calls = {"n": 0}

    def flaky(basin, pop, work_crs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("模拟发布前进程中止")
        return original(basin, pop, work_crs)

    monkeypatch.setattr(runner_mod, "build_weight_table", flaky)
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(inventory_v1, out, config, run_id="aborted")

    # 读者仍只能看到上一完整版本
    assert current_release(out) == first
    assert available_releases(out) == ["good"]
    # 暂存区被清理
    assert not (out / "staging").exists() or not list((out / "staging").iterdir())


def test_incremental_rebuilds_only_changed_basins(
        inventory_v1, inventory_v2, config, tmp_path):
    out = tmp_path / "out"
    cache = tmp_path / "cache"

    s1 = run_pipeline(inventory_v1, out, config, run_id="v1run",
                      cache_dir=cache)
    assert set(s1["cache_misses"]) == {"A", "B", "C", "D"}

    # 同一 v1 再跑：全部命中缓存
    s1b = run_pipeline(inventory_v1, out, config, run_id="v1rerun",
                       cache_dir=cache)
    assert set(s1b["cache_hits"]) == {"A", "B", "C", "D"}
    assert s1b["cache_misses"] == []

    # 边界升级到 v2：B 几何修订、D 退化失败、E 新增；A/C 命中
    s2 = run_pipeline(inventory_v2, out, config, run_id="v2run",
                      cache_dir=cache)
    hits = set(s2["cache_hits"])
    misses = set(s2["cache_misses"])
    assert {"A", "C"} <= hits
    assert {"B", "E"} <= misses
    # D 修复失败，不出现在任一集合
    assert "D" not in hits and "D" not in misses
    assert "D" in s2["failed_basins"]

    manifest = load_run_manifest(out, release="v2run")
    assert manifest["input_summary"]["boundary_version"] == "v2"
    assert manifest["incremental"]["rebuilt_basin_ids"] == s2["cache_misses"]


def test_query_basin_and_provenance(inventory_v1, config, tmp_path):
    out = tmp_path / "out"
    run_pipeline(inventory_v1, out, config, run_id="r1")

    rows = query_basin(out, "B")
    assert {r["forecast_time"] for r in rows} == {"T00", "T06"}
    for r in rows:
        assert QualityFlag.OVERLAP_WEIGHTED in r["flags"]

    with pytest.raises(KeyError):
        query_basin(out, "NO_SUCH_BASIN")

    prov = provenance(out, basin_id="B")
    assert prov["basin"]["basin_id"] == "B"
    assert prov["input_summary"]["boundary_version"] == "v1"
    # 每行都携带参与计算的输入 sha256
    rows_all = load_exposure_rows(out)
    for r in rows_all:
        assert len(r["depth_sha256"]) == 64
        assert len(r["population_sha256"]) == 64


def test_tile_size_does_not_change_published_results(
        inventory_v1, tmp_path):
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    cfg_a = Config(max_missing_fraction=0.25, window_tile_size=32)
    cfg_b = Config(max_missing_fraction=0.25, window_tile_size=128)
    run_pipeline(inventory_v1, out_a, cfg_a, run_id="x")
    run_pipeline(inventory_v1, out_b, cfg_b, run_id="x")

    ra = {(r["basin_id"], r["forecast_time"]): r
          for r in load_exposure_rows(out_a)}
    rb = {(r["basin_id"], r["forecast_time"]): r
          for r in load_exposure_rows(out_b)}
    assert ra.keys() == rb.keys()
    for key in ra:
        for f in ("exposed_population", "weighted_population",
                  "wet_area_m2", "nodata_area_m2", "missing_fraction"):
            assert abs(float(ra[key][f]) - float(rb[key][f])) < 1e-6, (key, f)
