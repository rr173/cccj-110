"""stage-discharge 换算命令的独立断言测试（标准库 unittest）。

期望值都是手算字面量，不复用被测模块的插值/回放函数；每个结果行都核对
原始 CSV 物理行号。覆盖需求列出的全部场景：

* 节点精确命中与两节点间线性换算
* 先涨后退，以及水位差恰在死区上下边界时保持原分支
* 新版在生效时刻接管旧版（半开有效期、首尾相接）
* 有效期空档与曲线上下两端超范围均不外推
* 一条无效观测之后仍按上一条成功结果选分支
* 相同重复观测合并且保留全部行号；同刻冲突整批拒绝
* 交错处理两个测站，分支互不串用
* 打乱输入顺序、改写等价时区后业务结果不变
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from flood_exposure.cli import build_parser, main  # noqa: E402
from flood_exposure.hashing import sha256_file  # noqa: E402
from flood_exposure.stage_discharge import (  # noqa: E402
    RESULTS_FILENAME,
    SUMMARY_FILENAME,
    StageDischargeInputError,
    StageDischargeRegistryError,
    process_stage_discharge,
)

HEADER = ["station_id", "observed_at", "stage_m"]

LEGACY = {
    "curve_id": "C-LEGACY", "revision": 1,
    "effective_from": "2026-01-01T00:00:00+00:00",
    "effective_to": "2026-02-01T00:00:00+00:00",
    "rising": [{"stage_m": 0.0, "discharge_m3_s": 0.0},
               {"stage_m": 1.0, "discharge_m3_s": 10.0},
               {"stage_m": 2.0, "discharge_m3_s": 30.0}],
    "falling": [[0.0, 0.0], [1.0, 4.0], [2.0, 18.0]],
}
CURRENT = {
    "curve_id": "C-CURRENT", "revision": 2,
    "effective_from": "2026-03-01T00:00:00+00:00",
    "effective_to": None,
    "rising": [[0.0, 2.0], [1.0, 12.0], [3.0, 40.0]],
    "falling": [[0.0, 1.0], [1.0, 5.0], [3.0, 27.0]],
}


def registry_single(branch="rising", deadband=0.1):
    return {"schema_version": 1, "stations": {"S1": {
        "deadband_m": deadband,
        "initial_branch": branch,
        "curves": [json.loads(json.dumps(LEGACY))],
    }}}


def registry_two_stations():
    return {"schema_version": 1, "stations": {
        "S1": {"deadband_m": 0.1, "initial_branch": "rising",
               "curves": [json.loads(json.dumps(LEGACY)),
                          json.loads(json.dumps(CURRENT))]},
        "S2": {"deadband_m": 0.05, "initial_branch": "falling",
               "curves": [{
                   "curve_id": "S2-1", "revision": 4,
                   "effective_from": "2026-01-01T00:00:00Z",
                   "effective_to": None,
                   "rising": [[0.0, 0.0], [0.5, 2.5], [1.0, 6.0]],
                   "falling": [[0.0, 0.0], [0.5, 2.0], [1.0, 5.0]],
               }]},
    }}


class StageDischargeCase(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="stage-discharge-test-"))
        self.obs_path = self.tmp / "observations.csv"
        self.reg_path = self.tmp / "registry.json"
        self.out_dir = self.tmp / "out"

    def write_rows(self, rows) -> None:
        with open(self.obs_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(HEADER)
            writer.writerows(rows)

    def write_registry(self, registry) -> None:
        self.reg_path.write_text(
            json.dumps(registry, ensure_ascii=False), encoding="utf-8")

    def run_pipeline(self, out_dir=None):
        return process_stage_discharge(
            self.obs_path, self.reg_path, out_dir or self.out_dir)

    def read_results(self, out_dir=None) -> list[dict]:
        with open((out_dir or self.out_dir) / RESULTS_FILENAME,
                  encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def read_summary(self, out_dir=None) -> dict:
        return json.loads(
            ((out_dir or self.out_dir) / SUMMARY_FILENAME).read_text(
                encoding="utf-8"))

    def result(self, station, time_text, out_dir=None) -> dict:
        rows = [r for r in self.read_results(out_dir)
                if r["station_id"] == station
                and r["observed_at_utc"] == time_text]
        self.assertEqual(len(rows), 1, f"{station} {time_text} 应唯一")
        return rows[0]


class TestInterpolation(StageDischargeCase):

    def test_node_exact_hit_uses_node_value(self):
        self.write_registry(registry_single())
        self.write_rows([("S1", "2026-01-05T00:00:00+00:00", "1.0")])
        self.run_pipeline()
        row = self.result("S1", "2026-01-05T00:00:00Z")
        self.assertEqual(row["status"], "converted")
        self.assertEqual(row["discharge_m3_s"], "10")
        self.assertEqual(row["method"], "node_exact")
        self.assertEqual(row["calculation"], "Q = q(h=1) = 10")
        self.assertEqual(row["lower_stage_m"], "1")
        self.assertEqual(row["upper_stage_m"], "1")
        self.assertEqual(row["interpolation_fraction"], "0")

    def test_linear_interpolation_matches_hand_calculation(self):
        self.write_registry(registry_single())
        self.write_rows([
            ("S1", "2026-01-05T00:00:00+00:00", "0.5"),   # 0..10, f=0.5
            ("S1", "2026-01-05T06:00:00+00:00", "1.5"),   # 10..30, f=0.5
            ("S1", "2026-01-05T12:00:00+00:00", "0.25"),  # 0..10, f=0.25
        ])
        self.run_pipeline()
        expected = {
            "0.5": ("5", "0", "1", "0", "10", "0.5"),
            "1.5": ("20", "1", "2", "10", "30", "0.5"),
            # 0.50 -> 0.25 差 -0.25 越下限 -> 退水分支 0..4
            "0.25": ("1", "0", "1", "0", "4", "0.25"),
        }
        for stage, values in expected.items():
            row = [r for r in self.read_results() if r["stage_m"] == stage][0]
            self.assertEqual(row["status"], "converted")
            self.assertEqual(row["method"], "linear_interpolation")
            self.assertEqual(
                (row["discharge_m3_s"], row["lower_stage_m"],
                 row["upper_stage_m"], row["lower_discharge_m3_s"],
                 row["upper_discharge_m3_s"],
                 row["interpolation_fraction"]), values)

    def test_branch_curves_give_different_discharge_for_same_stage(self):
        self.write_registry(registry_single(branch="falling"))
        self.write_rows([("S1", "2026-01-05T00:00:00+00:00", "1.0")])
        self.run_pipeline()
        row = self.result("S1", "2026-01-05T00:00:00Z")
        self.assertEqual(row["branch"], "falling")
        self.assertEqual(row["discharge_m3_s"], "4")

    def test_decimal_input_has_no_binary_float_tail(self):
        # 0.1 + 0.2 在二进制浮点里不等于 0.3；十进制输入必须精确命中节点
        self.write_registry({"schema_version": 1, "stations": {"S1": {
            "deadband_m": 0.1, "initial_branch": "rising",
            "curves": [{"curve_id": "C0", "revision": 0,
                        "effective_from": "2026-01-01T00:00:00Z",
                        "rising": [[0.1, 1.0], [0.3, 3.0]],
                        "falling": [[0.1, 1.0], [0.3, 3.0]]}],
        }}})
        self.write_rows([("S1", "2026-01-05T00:00:00+00:00", "0.3")])
        self.run_pipeline()
        row = self.result("S1", "2026-01-05T00:00:00Z")
        self.assertEqual(row["method"], "node_exact")
        self.assertEqual(row["discharge_m3_s"], "3")


class TestReplayAndDeadband(StageDischargeCase):

    def test_rise_then_fall_and_deadband_edges_keep_branch(self):
        self.write_registry(registry_single(branch="rising", deadband=0.1))
        self.write_rows([
            ("S1", "2026-01-05T00:00:00+00:00", "0.40"),
            ("S1", "2026-01-05T06:00:00+00:00", "0.50"),  # +0.10 == 上限
            ("S1", "2026-01-05T12:00:00+00:00", "0.65"),  # +0.15 -> 涨水
            ("S1", "2026-01-05T18:00:00+00:00", "0.50"),  # -0.15 -> 退水
            ("S1", "2026-01-06T00:00:00+00:00", "0.40"),  # -0.10 == 下限
        ])
        self.run_pipeline()
        rows = self.read_results()
        self.assertEqual([r["branch"] for r in rows],
                         ["rising"] * 3 + ["falling"] * 2)
        self.assertEqual([r["branch_decision"] for r in rows], [
            "initial", "keep_within_deadband", "switch_to_rising",
            "switch_to_falling", "keep_within_deadband"])
        # 退水分支 h=0.4 -> 0..4 之间 f=0.4 -> Q=1.6
        self.assertEqual(rows[-1]["discharge_m3_s"], "1.6")
        self.assertEqual(rows[-1]["previous_stage_m"], "0.5")

    def test_asymmetric_deadband_object(self):
        self.write_registry({"schema_version": 1, "stations": {"S1": {
            "deadband": {"lower_m": -0.3, "upper_m": 0.05},
            "initial_branch": "涨水",
            "curves": [json.loads(json.dumps(LEGACY))],
        }}})
        self.write_rows([
            ("S1", "2026-01-05T00:00:00+00:00", "0.5"),
            ("S1", "2026-01-05T06:00:00+00:00", "0.20"),  # -0.30 == 下限
            ("S1", "2026-01-05T12:00:00+00:00", "0.60"),  # +0.40 -> 涨水
            ("S1", "2026-01-05T18:00:00+00:00", "0.55"),  # -0.05 == 上限
        ])
        self.run_pipeline()
        rows = self.read_results()
        self.assertEqual([r["branch"] for r in rows], ["rising"] * 4)
        self.assertEqual(rows[1]["branch_decision"], "keep_within_deadband")
        self.assertEqual(rows[3]["branch_decision"], "keep_within_deadband")
        summary = self.read_summary()
        self.assertEqual(
            summary["registry_summary"]["per_station"]["S1"]
            ["deadband_lower_m"], "-0.3")

    def test_out_of_range_row_does_not_change_previous_success(self):
        self.write_registry(registry_single(branch="rising", deadband=0.1))
        self.write_rows([
            ("S1", "2026-01-05T00:00:00+00:00", "1.00"),
            ("S1", "2026-01-05T06:00:00+00:00", "2.50"),  # 超上界，不换算
            ("S1", "2026-01-05T12:00:00+00:00", "1.25"),  # 应比 1.00
            ("S1", "2026-01-05T18:00:00+00:00", "-0.10"),  # 超下界，不换算
            ("S1", "2026-01-06T00:00:00+00:00", "1.20"),  # 仍比 1.25
        ])
        self.run_pipeline()
        rows = self.read_results()
        self.assertEqual(rows[1]["reason"], "stage_out_of_curve_range")
        self.assertEqual(rows[1]["state_applied"], "false")
        # 若误把 2.50 当成上一次成功水位，1.25 的差会是 -1.25 -> 退水；
        # 正确行为是与 1.00 比较：+0.25 -> 涨水
        self.assertEqual(rows[2]["previous_stage_m"], "1")
        self.assertEqual(rows[2]["stage_delta_m"], "0.25")
        self.assertEqual(rows[2]["curve_id"], "C-LEGACY")
        self.assertEqual(rows[2]["branch"], "rising")
        self.assertEqual(rows[2]["branch_decision"], "switch_to_rising")
        self.assertEqual(rows[3]["reason"], "stage_out_of_curve_range")
        self.assertEqual(rows[4]["previous_stage_m"], "1.25")
        self.assertEqual(rows[4]["stage_delta_m"], "-0.05")

    def test_no_curve_row_does_not_change_state(self):
        self.write_registry({"schema_version": 1, "stations": {"S1": {
            "deadband_m": 0.1, "initial_branch": "rising",
            "curves": [{"curve_id": "C1", "revision": 1,
                        "effective_from": "2026-01-01T00:00:00Z",
                        "effective_to": "2026-01-02T00:00:00Z",
                        "rising": [[0.0, 0.0], [1.0, 10.0]],
                        "falling": [[0.0, 0.0], [1.0, 4.0]]},
                       {"curve_id": "C2", "revision": 2,
                        "effective_from": "2026-01-03T00:00:00Z",
                        "rising": [[0.0, 0.0], [1.0, 12.0]],
                        "falling": [[0.0, 0.0], [1.0, 5.0]]}],
        }}})
        self.write_rows([
            ("S1", "2026-01-01T12:00:00+00:00", "0.5"),
            ("S1", "2026-01-02T12:00:00+00:00", "0.9"),   # 空档
            ("S1", "2026-01-03T00:00:00+00:00", "0.8"),   # 新版接管
        ])
        self.run_pipeline()
        rows = self.read_results()
        self.assertEqual(rows[1]["status"], "not_converted")
        self.assertEqual(rows[1]["reason"], "no_effective_curve")
        self.assertEqual(rows[1]["branch"], "")
        self.assertEqual(rows[1]["branch_decision"], "no_curve")
        self.assertEqual(rows[1]["curve_id"], "")
        self.assertEqual(rows[2]["curve_id"], "C2")
        self.assertEqual(rows[2]["previous_stage_m"], "0.5")
        self.assertEqual(rows[2]["discharge_m3_s"], "9.6")


class TestVersionSelection(StageDischargeCase):

    def test_effective_boundaries_are_half_open_and_handover(self):
        self.write_registry(registry_two_stations())
        self.write_rows([
            ("S1", "2026-01-31T23:59:59+00:00", "1.0"),   # 旧版末尾
            ("S1", "2026-02-01T00:00:00+00:00", "1.0"),   # 旧版失效时刻
            ("S1", "2026-03-01T00:00:00+00:00", "1.0"),   # 新版生效时刻
        ])
        self.run_pipeline()
        rows = [r for r in self.read_results() if r["station_id"] == "S1"]
        self.assertEqual(rows[0]["curve_id"], "C-LEGACY")
        self.assertEqual(rows[1]["status"], "not_converted")
        self.assertEqual(rows[1]["reason"], "no_effective_curve")
        self.assertEqual(rows[2]["curve_id"], "C-CURRENT")
        self.assertEqual(rows[2]["curve_effective_from_utc"],
                         "2026-03-01T00:00:00Z")
        self.assertEqual(rows[2]["curve_effective_to_utc"], "")
        self.assertEqual(rows[2]["discharge_m3_s"], "12")

    def test_adjacent_versions_sharing_instant_are_not_overlap(self):
        self.write_registry({"schema_version": 1, "stations": {"S1": {
            "deadband_m": 0.1, "initial_branch": "rising",
            "curves": [
                {"curve_id": "V1", "revision": 1,
                 "effective_from": "2026-01-01T00:00:00Z",
                 "effective_to": "2026-01-10T00:00:00Z",
                 "rising": [[0, 0], [1, 10]], "falling": [[0, 0], [1, 4]]},
                {"curve_id": "V2", "revision": 2,
                 "effective_from": "2026-01-10T00:00:00Z",
                 "effective_to": "2026-01-20T00:00:00Z",
                 "rising": [[0, 1], [1, 11]], "falling": [[0, 1], [1, 5]]},
            ],
        }}})
        self.write_rows([("S1", "2026-01-10T00:00:00Z", "1.0")])
        self.run_pipeline()
        row = self.result("S1", "2026-01-10T00:00:00Z")
        self.assertEqual(row["curve_id"], "V2")
        self.assertEqual(row["revision"], "2")

    def test_gap_between_versions_is_reported_not_extrapolated(self):
        self.write_registry(registry_two_stations())
        self.write_rows([("S1", "2026-02-15T00:00:00Z", "1.2")])
        self.run_pipeline()
        row = self.result("S1", "2026-02-15T00:00:00Z")
        self.assertEqual(row["status"], "not_converted")
        self.assertEqual(row["reason"], "no_effective_curve")
        self.assertEqual(row["discharge_m3_s"], "")
        summary = self.read_summary()
        self.assertEqual(
            summary["counts"]["not_converted_no_effective_curve"], 1)


class TestMergingAndConflict(StageDischargeCase):

    def test_identical_duplicates_merge_and_keep_all_source_lines(self):
        self.write_registry(registry_single())
        self.write_rows([
            ("S1", "2026-01-05T00:00:00+00:00", "0.50"),
            ("S1", "2026-01-05T08:00:00+08:00", "0.500"),  # 等价重复
            ("S1", "2026-01-05T12:00:00+00:00", "0.40"),
        ])
        summary = self.run_pipeline()
        rows = self.read_results()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["record_count"], "2")
        self.assertEqual(rows[0]["source_lines"], "2;3")
        self.assertEqual(summary["counts"]["raw_observation_records"], 3)
        self.assertEqual(summary["counts"]["merged_duplicate_records"], 1)
        self.assertEqual(summary["counts"]["unique_observations"], 2)
        # 合并的重复记录不额外推进回放：第二条与 0.50 比较
        self.assertEqual(rows[1]["previous_stage_m"], "0.5")

    def test_same_instant_different_stage_rejects_whole_batch(self):
        self.write_registry(registry_single())
        self.write_rows([
            ("S1", "2026-01-05T00:00:00+00:00", "0.50"),
            ("S1", "2026-01-05T08:00:00+08:00", "0.51"),
        ])
        with self.assertRaises(StageDischargeInputError) as ctx:
            self.run_pipeline()
        self.assertIn("水位冲突", str(ctx.exception))
        self.assertFalse(self.out_dir.exists())

    def test_unknown_station_rejects_whole_batch(self):
        self.write_registry(registry_single())
        self.write_rows([
            ("S1", "2026-01-05T00:00:00+00:00", "0.50"),
            ("S9", "2026-01-05T00:00:00+00:00", "0.50"),
        ])
        with self.assertRaises(StageDischargeInputError) as ctx:
            self.run_pipeline()
        self.assertIn("未登记站号", str(ctx.exception))
        self.assertFalse(self.out_dir.exists())

    def test_naive_time_rejects_whole_batch(self):
        self.write_registry(registry_single())
        self.write_rows([("S1", "2026-01-05T00:00:00", "0.50")])
        with self.assertRaises(StageDischargeInputError) as ctx:
            self.run_pipeline()
        self.assertIn("时区", str(ctx.exception))

    def test_bad_stage_or_header_rejects(self):
        self.write_registry(registry_single())
        for rows in ([("S1", "2026-01-05T00:00:00+00:00", "abc")],
                     [("S1", "2026-01-05T00:00:00+00:00", "NaN")]):
            self.write_rows(rows)
            with self.assertRaises(StageDischargeInputError):
                self.run_pipeline()

    def test_failure_keeps_previous_output_available(self):
        self.write_registry(registry_single())
        self.write_rows([("S1", "2026-01-05T00:00:00+00:00", "0.50")])
        self.run_pipeline()
        before = (self.out_dir / RESULTS_FILENAME).read_bytes()
        self.write_rows([
            ("S1", "2026-01-05T00:00:00+00:00", "0.50"),
            ("S1", "2026-01-05T00:00:00+00:00", "0.51"),
        ])
        with self.assertRaises(StageDischargeInputError):
            self.run_pipeline()
        self.assertEqual((self.out_dir / RESULTS_FILENAME).read_bytes(), before)


class TestDecoupledStations(StageDischargeCase):

    def test_interleaved_stations_do_not_share_branch_state(self):
        self.write_registry(registry_two_stations())
        self.write_rows([
            ("S2", "2026-01-05T01:00:00+00:00", "0.60"),  # 初始退水
            ("S1", "2026-01-05T02:00:00+00:00", "0.20"),  # 初始涨水
            ("S2", "2026-01-05T03:00:00+00:00", "0.55"),  # -0.05 死区
            ("S1", "2026-01-05T04:00:00+00:00", "0.35"),  # +0.15 涨水
            ("S2", "2026-01-05T05:00:00+00:00", "0.57"),  # +0.02 死区
            ("S1", "2026-01-05T06:00:00+00:00", "0.20"),  # -0.15 退水
        ])
        self.run_pipeline()
        rows = self.read_results()
        self.assertEqual([(r["station_id"], r["branch"]) for r in rows], [
            ("S1", "rising"), ("S1", "rising"), ("S1", "falling"),
            ("S2", "falling"), ("S2", "falling"), ("S2", "falling")])
        s1_falling = [r for r in rows if r["station_id"] == "S1"][-1]
        s2_falling = [r for r in rows if r["station_id"] == "S2"][0]
        self.assertEqual(s1_falling["discharge_m3_s"], "0.8")
        # 退水分支 h=0.60 在 (0.5,2.0)-(1.0,5.0) 之间 f=0.2 -> Q=2.6
        self.assertEqual(s2_falling["discharge_m3_s"], "2.6")
        self.assertEqual(s2_falling["previous_stage_m"], "")

    def test_reversing_interleaving_does_not_change_business_rows(self):
        registry = registry_two_stations()
        rows = [
            ("S2", "2026-01-05T01:00:00+00:00", "0.60"),
            ("S1", "2026-01-05T02:00:00+00:00", "0.20"),
            ("S2", "2026-01-05T03:00:00+00:00", "0.55"),
            ("S1", "2026-01-05T04:00:00+00:00", "0.35"),
            ("S1", "2026-01-05T06:00:00+00:00", "0.20"),
        ]
        self.write_registry(registry)
        self.write_rows(rows)
        self.run_pipeline()
        first = self._rows_without_source(self.read_results())

        out2 = self.tmp / "out2"
        self.write_rows(list(reversed(rows)))
        self.run_pipeline(out2)
        second = self._rows_without_source(self.read_results(out2))
        self.assertEqual(first, second)

    @staticmethod
    def _rows_without_source(rows):
        return [{k: v for k, v in r.items() if k != "source_lines"}
                for r in rows]


class TestRegistryValidation(StageDischargeCase):

    def test_node_rules_and_effective_overlap_are_rejected(self):
        cases = []

        non_monotonic = registry_single()
        non_monotonic["stations"]["S1"]["curves"][0]["rising"] = \
            [[0, 0], [1, 10], [1.5, 12], [1.0, 14]]
        cases.append(("节点水位", non_monotonic))

        decreasing_flow = registry_single()
        decreasing_flow["stations"]["S1"]["curves"][0]["falling"] = \
            [[0, 0], [1, 5], [2, 4]]
        cases.append(("不下降", decreasing_flow))

        negative_flow = registry_single()
        negative_flow["stations"]["S1"]["curves"][0]["rising"] = \
            [[0, 0], [1, -1]]
        cases.append(("非负", negative_flow))

        overlap = registry_single()
        overlap["stations"]["S1"]["curves"].append({
            "curve_id": "C2", "revision": 2,
            "effective_from": "2026-01-15T00:00:00Z",
            "effective_to": "2026-03-01T00:00:00Z",
            "rising": [[0, 0], [1, 10]], "falling": [[0, 0], [1, 4]]})
        cases.append(("交叠", overlap))

        open_ended_previous = registry_single()
        open_ended_previous["stations"]["S1"]["curves"][0].pop("effective_to")
        open_ended_previous["stations"]["S1"]["curves"].append({
            "curve_id": "C2", "revision": 2,
            "effective_from": "2026-03-01T00:00:00Z",
            "rising": [[0, 0], [1, 10]], "falling": [[0, 0], [1, 4]]})
        cases.append(("交叠", open_ended_previous))

        for keyword, registry in cases:
            self.write_registry(registry)
            self.write_rows([("S1", "2026-01-05T00:00:00+00:00", "0.5")])
            with self.assertRaises(StageDischargeRegistryError) as ctx:
                self.run_pipeline()
            self.assertIn(keyword, str(ctx.exception))

    def test_structure_errors_are_rejected_with_specific_messages(self):
        cases = []

        empty = {"schema_version": 1, "stations": {}}
        cases.append(("至少登记", empty))

        unknown_station_key = registry_single()
        unknown_station_key["stations"]["S1"]["colour"] = "blue"
        cases.append(("未知字段", unknown_station_key))

        bad_branch = registry_single(branch="sideways")
        cases.append(("rising/falling", bad_branch))

        no_curves = registry_single()
        no_curves["stations"]["S1"]["curves"] = []
        cases.append(("非空数组", no_curves))

        duplicate_curve_id = registry_single()
        duplicate_curve_id["stations"]["S1"]["curves"].append(
            json.loads(json.dumps(LEGACY)))
        cases.append(("重复", duplicate_curve_id))

        naive_time = registry_single()
        naive_time["stations"]["S1"]["curves"][0]["effective_from"] = \
            "2026-01-01T00:00:00"
        cases.append(("时区", naive_time))

        reversed_interval = registry_single()
        reversed_interval["stations"]["S1"]["curves"][0]["effective_to"] = \
            "2025-12-01T00:00:00Z"
        cases.append(("半开区间", reversed_interval))

        bad_deadband = registry_single()
        bad_deadband["stations"]["S1"]["deadband_m"] = -0.1
        cases.append(("非负数", bad_deadband))

        for keyword, registry in cases:
            self.write_registry(registry)
            self.write_rows([("S1", "2026-01-05T00:00:00+00:00", "0.5")])
            with self.assertRaises(StageDischargeRegistryError) as ctx:
                self.run_pipeline()
            self.assertIn(keyword, str(ctx.exception))

    def test_malformed_json_and_duplicate_keys_rejected(self):
        self.write_rows([("S1", "2026-01-05T00:00:00+00:00", "0.5")])
        self.reg_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StageDischargeRegistryError):
            self.run_pipeline()

        self.reg_path.write_text(
            '{"schema_version": 1, "stations": {"S1": '
            '{"deadband_m": 0.1, "initial_branch": "rising", '
            '"curves": [], "curves": []}}}', encoding="utf-8")
        with self.assertRaises(StageDischargeRegistryError) as ctx:
            self.run_pipeline()
        self.assertIn("重复键", str(ctx.exception))

    def test_revision_and_curve_id_are_preserved_in_output(self):
        self.write_registry(registry_two_stations())
        self.write_rows([("S1", "2026-03-02T00:00:00Z", "0.5")])
        summary = self.run_pipeline()
        row = self.result("S1", "2026-03-02T00:00:00Z")
        self.assertEqual(row["curve_id"], "C-CURRENT")
        self.assertEqual(row["revision"], "2")
        self.assertEqual(
            summary["registry_summary"]["curve_versions"], 3)


class TestSummaryAndShuffle(StageDischargeCase):

    def test_summary_hashes_and_counts_reconcile(self):
        self.write_registry(registry_two_stations())
        rows = [
            ("S1", "2026-01-05T00:00:00+00:00", "0.50"),
            ("S1", "2026-01-05T08:00:00+08:00", "0.500"),
            ("S1", "2026-02-15T00:00:00+00:00", "1.00"),   # 空档
            ("S1", "2026-03-01T06:00:00+00:00", "5.00"),   # 超范围
            ("S2", "2026-01-05T01:00:00+00:00", "0.60"),
        ]
        self.write_rows(rows)
        summary = self.run_pipeline()
        counts = summary["counts"]
        self.assertEqual(summary["inputs"]["observations_csv"]["sha256"],
                         sha256_file(self.obs_path))
        self.assertEqual(summary["inputs"]["registry_json"]["sha256"],
                         sha256_file(self.reg_path))
        self.assertEqual(counts["raw_observation_records"], 5)
        self.assertEqual(counts["merged_duplicate_records"], 1)
        self.assertEqual(counts["unique_observations"], 4)
        self.assertEqual(counts["result_rows"], 4)
        self.assertEqual(counts["converted_rows"] +
                         counts["not_converted_rows"], 4)
        self.assertEqual(counts["not_converted_no_effective_curve"], 1)
        self.assertEqual(counts["not_converted_stage_out_of_curve_range"], 1)
        self.assertEqual(sum(summary["reason_counts"].values()),
                         counts["not_converted_rows"])
        self.assertIn("并非", summary["disclaimer"])

    def test_shuffle_and_timezone_rewrite_keep_business_columns(self):
        self.write_registry(registry_two_stations())
        rows = [
            ("S2", "2026-01-05T01:00:00+00:00", "0.60"),
            ("S1", "2026-01-05T00:00:00+00:00", "0.40"),
            ("S1", "2026-01-05T12:00:00+00:00", "0.50"),
            ("S2", "2026-01-05T07:00:00+00:00", "0.55"),
            ("S1", "2026-03-01T00:00:00+00:00", "1.60"),
        ]
        self.write_rows(rows)
        self.run_pipeline()
        base = self._business_rows(self.read_results())

        rewritten = [
            ("S2", "2026-01-05T06:30:00+05:30", "0.60"),
            ("S1", "2026-03-01T08:00:00+08:00", "1.60"),
            ("S1", "2026-01-05T04:00:00-08:00", "0.50"),
            ("S2", "2026-01-05T12:30:00+05:30", "0.55"),
            ("S1", "2026-01-05T00:00:00+00:00", "0.40"),
        ]
        out2 = self.tmp / "out2"
        self.write_rows(rewritten)
        self.run_pipeline(out2)
        self.assertEqual(self._business_rows(self.read_results(out2)), base)
        # 行号必须指向实际输入：第 3 行是 +08:00 写的 S1 1.60
        row = self.result("S1", "2026-03-01T00:00:00Z", out2)
        with open(self.obs_path, encoding="utf-8", newline="") as fh:
            physical = list(csv.reader(fh))
        line = int(row["source_lines"])
        self.assertEqual(physical[line - 1][0], "S1")
        self.assertEqual(physical[line - 1][2], "1.60")

    @staticmethod
    def _business_rows(rows):
        return [{k: v for k, v in r.items()
                 if k not in ("source_lines",)} for r in rows]


class TestCli(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="stage-discharge-cli-"))
        self.obs = self.tmp / "observations.csv"
        self.reg = self.tmp / "registry.json"
        self.out = self.tmp / "out"
        self.reg.write_text(json.dumps(registry_two_stations()),
                            encoding="utf-8")
        with open(self.obs, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(HEADER)
            writer.writerows([
                ("S1", "2026-01-05T00:00:00+00:00", "0.50"),
                ("S2", "2026-01-05T01:00:00+00:00", "0.60"),
            ])

    def test_parser_registers_help(self):
        parser = build_parser()
        args = parser.parse_args([
            "stage-discharge", "--observations", str(self.obs),
            "--registry", str(self.reg), "--output-dir", str(self.out)])
        self.assertEqual(args.command, "stage-discharge")
        self.assertIn("率定曲线", parser.format_help())
        sub_parser = None
        for action in parser._actions:
            choices = getattr(action, "choices", None) or {}
            if "stage-discharge" in choices:
                sub_parser = choices["stage-discharge"]
        self.assertIsNotNone(sub_parser)
        self.assertIn("不是直接测得的流量",
                      sub_parser.format_help())

    def test_exit_codes(self):
        rc = main(["stage-discharge", "--observations", str(self.obs),
                   "--registry", str(self.reg),
                   "--output-dir", str(self.out)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.out / SUMMARY_FILENAME).exists())

        bad_obs = self.tmp / "bad.csv"
        with open(bad_obs, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(HEADER)
            writer.writerow(("S1", "2026-01-05T00:00:00", "0.5"))
        rc = main(["stage-discharge", "--observations", str(bad_obs),
                   "--registry", str(self.reg),
                   "--output-dir", str(self.tmp / "bad-out")])
        self.assertEqual(rc, 3)
        self.assertFalse((self.tmp / "bad-out" / RESULTS_FILENAME).exists())

        bad_reg = self.tmp / "bad-registry.json"
        bad_reg.write_text("{oops", encoding="utf-8")
        rc = main(["stage-discharge", "--observations", str(self.obs),
                   "--registry", str(bad_reg),
                   "--output-dir", str(self.tmp / "bad-out2")])
        self.assertEqual(rc, 2)
        self.assertFalse((self.tmp / "bad-out2" / RESULTS_FILENAME).exists())



class TestMoreEdges(StageDischargeCase):

    def test_zero_width_deadband_switches_on_any_nonzero_delta(self):
        self.write_registry(registry_single(branch="rising", deadband=0))
        self.write_rows([
            ("S1", "2026-01-05T00:00:00Z", "0.50"),
            ("S1", "2026-01-05T06:00:00Z", "0.50"),  # 差 == 0：保持涨水
            ("S1", "2026-01-05T12:00:00Z", "0.49"),  # 差 < 0：退水
        ])
        self.run_pipeline()
        rows = self.read_results()
        self.assertEqual([r["branch"] for r in rows],
                         ["rising", "rising", "falling"])
        self.assertEqual([r["branch_decision"] for r in rows],
                         ["initial", "keep_within_deadband",
                          "switch_to_falling"])

    def test_small_interpolation_fraction_is_not_rounded_to_zero(self):
        self.write_registry({"schema_version": 1, "stations": {"S1": {
            "deadband_m": 0.1, "initial_branch": "rising",
            "curves": [{"curve_id": "C", "revision": 1,
                        "effective_from": "2026-01-01T00:00:00Z",
                        "rising": [[0.0, 0.0], [0.25, 10.0], [50.0, 20.0]],
                        "falling": [[0.0, 0.0], [0.25, 10.0], [50.0, 20.0]]}],
        }}})
        self.write_rows([("S1", "2026-01-05T00:00:00Z", "0.2501")])
        self.run_pipeline()
        row = self.result("S1", "2026-01-05T00:00:00Z")
        self.assertEqual(row["method"], "linear_interpolation")
        self.assertNotEqual(row["interpolation_fraction"], "0")
        self.assertNotEqual(row["discharge_m3_s"], "")
        self.assertGreater(float(row["discharge_m3_s"]), 10)
        # 手算：0.0001/49.75 ≈ 2.010e-6，Q ≈ 10.0000201
        self.assertAlmostEqual(
            float(row["discharge_m3_s"]), 10.0000201, places=6)

    def test_station_list_form_is_accepted(self):
        registry = registry_two_stations()
        as_list = {"schema_version": 1, "stations": [
            {"station_id": sid, **cfg}
            for sid, cfg in registry["stations"].items()]}
        self.write_registry(as_list)
        self.write_rows([("S1", "2026-01-05T00:00:00Z", "0.5")])
        self.run_pipeline()
        row = self.result("S1", "2026-01-05T00:00:00Z")
        self.assertEqual(row["discharge_m3_s"], "5")

    def test_deadband_object_must_cover_zero(self):
        self.write_registry({"schema_version": 1, "stations": {"S1": {
            "deadband": {"lower_m": 0.1, "upper_m": 0.2},
            "initial_branch": "rising",
            "curves": [json.loads(json.dumps(LEGACY))],
        }}})
        self.write_rows([("S1", "2026-01-05T00:00:00Z", "0.5")])
        with self.assertRaises(StageDischargeRegistryError) as ctx:
            self.run_pipeline()
        self.assertIn("覆盖 0", str(ctx.exception))

    def test_deadband_object_lower_above_upper_rejected(self):
        self.write_registry({"schema_version": 1, "stations": {"S1": {
            "deadband": {"lower_m": -0.1, "upper_m": -0.2},
            "initial_branch": "rising",
            "curves": [json.loads(json.dumps(LEGACY))],
        }}})
        self.write_rows([("S1", "2026-01-05T00:00:00Z", "0.5")])
        with self.assertRaises(StageDischargeRegistryError) as ctx:
            self.run_pipeline()
        self.assertIn("不得高于", str(ctx.exception))

    def test_no_curve_row_has_empty_branch_and_keeps_previous_success(self):
        self.write_registry(registry_two_stations())
        self.write_rows([
            ("S1", "2026-01-05T00:00:00Z", "0.40"),
            ("S1", "2026-02-15T00:00:00Z", "2.00"),   # 空档
            ("S1", "2026-03-01T00:00:00Z", "0.50"),   # 仍与 0.40 比较
        ])
        self.run_pipeline()
        rows = self.read_results()
        self.assertEqual(rows[1]["branch"], "")
        self.assertEqual(rows[1]["branch_decision"], "no_curve")
        self.assertEqual(rows[1]["state_applied"], "false")
        self.assertEqual(rows[2]["previous_stage_m"], "0.4")
        self.assertEqual(rows[2]["stage_delta_m"], "0.1")
        self.assertEqual(rows[2]["branch"], "rising")
        # 差 +0.10 恰等于上限：保持死区内的涨水
        self.assertEqual(rows[2]["branch_decision"], "keep_within_deadband")

    def test_empty_observations_file_produces_empty_table_and_counts(self):
        self.write_registry(registry_single())
        self.write_rows([])
        summary = self.run_pipeline()
        self.assertEqual(self.read_results(), [])
        self.assertEqual(summary["counts"]["raw_observation_records"], 0)
        self.assertEqual(summary["counts"]["result_rows"], 0)
        self.assertTrue((self.out_dir / SUMMARY_FILENAME).exists())

if __name__ == "__main__":
    unittest.main(verbosity=2)
