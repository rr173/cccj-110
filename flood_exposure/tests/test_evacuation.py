"""evacuation-access 命令的独立断言测试（标准库 unittest，pytest 也可收集）。

所有期望值都是手算字面量；行程用独立参考实现（demo/evac_reference.py，
不调用生产搜索器）逐分钟重放，并用穷举可行行程核对最早抵达与等待时间。
覆盖：

* 较短路线车程中段被淹、较长路线通过；积水恰好压上限仍可通过
* 禁止等待的路口必须绕行后更晚抵达才接得上开放道路
* 允许等待的路口被受淹窗口阻断等待；中途空档前的安全等待
* 路况空档与空值同为未知；反向道路不可用；避险点恰好关闭；等价路线按
  道路编号字典序定结果；抵达恰在封闭窗口开始分钟（道路半开区间）
* 有效/无效请求混批互不影响；起点出发时已受淹直接判无效
* 路网/路况/查询三份输入的非法情形整体拒绝（退出码 2/3/4）
* 打乱道路与片段顺序结果稳定；报告保留输入摘要与分析参数
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
DEMO_DIR = REPO_ROOT / "demo"
sys.path.insert(0, str(DEMO_DIR))

from flood_exposure.cli import main  # noqa: E402
from flood_exposure.hashing import sha256_file  # noqa: E402

import evac_reference  # noqa: E402
import make_evac_samples as samples  # noqa: E402


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _write_conditions(path: Path, rows: list[tuple]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(samples.CSV_HEADER)
        for row in rows:
            writer.writerow(row)


class EvacCase(unittest.TestCase):
    """每个用例一套独立临时目录与样例文件。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="evac-test-"))
        self.paths = samples.write_samples(self.tmp)
        self.report_path = self.tmp / "report.json"

    def run_cli(self, network=None, conditions=None, queries=None,
                output=None) -> int:
        argv = ["evacuation-access",
                "--network", str(network or self.paths["network"]),
                "--conditions", str(conditions or self.paths["conditions"]),
                "--queries", str(queries or self.paths["queries"])]
        if output is not None:
            argv += ["--output", str(output)]
        return main(argv)

    def run_ok(self) -> dict:
        rc = self.run_cli(output=self.report_path)
        self.assertEqual(rc, 0)
        return json.loads(self.report_path.read_text(encoding="utf-8"))

    def network_variant(self, mutate) -> Path:
        data = json.loads(json.dumps(samples.NETWORK))
        mutate(data)
        path = self.tmp / "bad_network.json"
        _write_json(path, data)
        return path

    def conditions_variant(self, rows: list[tuple]) -> Path:
        path = self.tmp / "bad_conditions.csv"
        _write_conditions(path, rows)
        return path

    def queries_variant(self, data: object, name="queries_bad.json") -> Path:
        path = self.tmp / name
        _write_json(path, data)
        return path


class TestBatchResults(EvacCase):

    def setUp(self) -> None:
        super().setUp()
        self.report = self.run_ok()
        self.results = {r["request_id"]: r for r in self.report["results"]}

    def test_statuses_and_counts(self) -> None:
        self.assertEqual(len(self.report["results"]), len(samples.QUERIES))
        self.assertEqual(
            [r["request_id"] for r in self.report["results"]],
            [q["request_id"] for q in samples.QUERIES])
        self.assertEqual(self.report["summary"],
                         {"requests": 18, "reachable": 9,
                          "unreachable": 5, "invalid_request": 4})
        for request_id, expected in samples.EXPECTED.items():
            self.assertEqual(self.results[request_id]["status"],
                             expected["status"], request_id)

    def test_reachable_details(self) -> None:
        for request_id, expected in samples.EXPECTED.items():
            if expected["status"] != "reachable":
                continue
            result = self.results[request_id]
            self.assertEqual(result["shelter_id"], expected["shelter_id"],
                             request_id)
            self.assertEqual(result["arrival_minute"], expected["arrival"],
                             request_id)
            self.assertEqual(result["total_wait_minutes"], expected["wait"],
                             request_id)
            self.assertEqual(result["road_ids"], expected["road_ids"],
                             request_id)
            waits = [(s["node_id"], s["start_minute"], s["end_minute"])
                     for s in result["itinerary"] if s["type"] == "wait"]
            self.assertEqual(waits, expected["waits"], request_id)

    def test_unreachable_explain_scope_and_reasons(self) -> None:
        for request_id, expected in samples.EXPECTED.items():
            if expected["status"] != "unreachable":
                continue
            result = self.results[request_id]
            search = result["search"]
            self.assertGreaterEqual(search["states_expanded"], 1, request_id)
            self.assertIn("depart_minute", search)
            self.assertIn("deadline_minute", search)
            self.assertEqual(set(search["blockers"].keys()),
                             expected["blockers"], request_id)
            for category, detail in search["blockers"].items():
                self.assertGreater(detail["count"], 0)
                self.assertLessEqual(len(detail["examples"]), 5)

    def test_invalid_requests_carry_reasons_and_do_not_lose_others(self) -> None:
        invalid = [r for r in self.report["results"]
                   if r["status"] == "invalid_request"]
        self.assertEqual(len(invalid), 4)
        for result in invalid:
            self.assertTrue(result["reason"])
        # 混批中的合法请求全部保留且各自有结论
        self.assertEqual(len(self.report["results"]), 18)
        self.assertEqual(self.results["Q1-long-route"]["status"], "reachable")
        self.assertEqual(self.results["Q15-reverse-road"]["status"],
                         "unreachable")

    def test_flooded_origin_is_invalid_not_relocated(self) -> None:
        result = self.results["Q16-flooded-origin"]
        self.assertEqual(result["status"], "invalid_request")
        self.assertIn("受淹", result["reason"])

    def test_itineraries_replay_minute_by_minute(self) -> None:
        raw = evac_reference.load_raw(self.paths["network"],
                                      self.paths["conditions"])
        requests = {q["request_id"]: q for q in samples.QUERIES}
        checked = 0
        for result in self.report["results"]:
            if result["status"] != "reachable":
                continue
            problems = evac_reference.verify_itinerary(
                raw, requests[result["request_id"]], result)
            self.assertEqual(problems, [], result["request_id"])
            checked += 1
        self.assertEqual(checked, 9)

    def test_reference_enumeration_matches(self) -> None:
        raw = evac_reference.load_raw(self.paths["network"],
                                      self.paths["conditions"])
        requests = {q["request_id"]: q for q in samples.QUERIES}
        for result in self.report["results"]:
            if result["status"] == "invalid_request":
                continue
            request = requests[result["request_id"]]
            reference = evac_reference.enumerate_reference(
                raw, request["origin"], request["depart_minute"],
                request["deadline_minute"])
            if result["status"] == "unreachable":
                self.assertIsNone(reference, result["request_id"])
            else:
                self.assertEqual(
                    reference,
                    (result["arrival_minute"], result["total_wait_minutes"]),
                    result["request_id"])

    def test_shuffle_stability(self) -> None:
        variants = samples.shuffled_variants(self.tmp, self.paths)
        shuffled_report = self.tmp / "report_shuffled.json"
        rc = self.run_cli(network=variants["network"],
                          conditions=variants["conditions"],
                          output=shuffled_report)
        self.assertEqual(rc, 0)
        shuffled = json.loads(shuffled_report.read_text(encoding="utf-8"))
        self.assertEqual(shuffled["results"], self.report["results"])

    def test_report_keeps_digests_and_parameters(self) -> None:
        inputs = self.report["inputs"]
        self.assertEqual(inputs["network_json"]["sha256"],
                         sha256_file(self.paths["network"]))
        self.assertEqual(inputs["conditions_csv"]["sha256"],
                         sha256_file(self.paths["conditions"]))
        self.assertEqual(inputs["queries_json"]["sha256"],
                         sha256_file(self.paths["queries"]))
        self.assertEqual(self.report["parameters"],
                         {"analysis_start_minute": 0,
                          "analysis_end_minute": 120})
        summary = self.report["network_summary"]
        self.assertEqual(summary["intersections"], 27)
        self.assertEqual(summary["roads"], 23)
        self.assertEqual(summary["shelters"], 8)
        self.assertGreater(summary["merged_condition_records"], 0)


class TestNetworkRejection(EvacCase):
    """路网自身问题：整体拒绝（退出码 2），不出具有效行程。"""

    def assert_network_rejected(self, mutate) -> None:
        path = self.network_variant(mutate)
        rc = self.run_cli(network=path)
        self.assertEqual(rc, 2)
        self.assertFalse(self.report_path.exists())

    def test_duplicate_node_id(self) -> None:
        def mutate(data):
            data["intersections"].append(dict(data["intersections"][0]))
        self.assert_network_rejected(mutate)

    def test_duplicate_road_id(self) -> None:
        def mutate(data):
            data["roads"].append(dict(data["roads"][0]))
        self.assert_network_rejected(mutate)

    def test_duplicate_shelter_id(self) -> None:
        def mutate(data):
            data["shelters"].append(dict(data["shelters"][0]))
        self.assert_network_rejected(mutate)

    def test_road_references_missing_node(self) -> None:
        def mutate(data):
            data["roads"][0]["to_node"] = "GHOST"
        self.assert_network_rejected(mutate)

    def test_shelter_references_missing_node(self) -> None:
        def mutate(data):
            data["shelters"][0]["node_id"] = "GHOST"
        self.assert_network_rejected(mutate)

    def test_non_positive_travel_minutes(self) -> None:
        for bad in (0, -3, 2.5, "5", True):
            def mutate(data, bad=bad):
                data["roads"][0]["travel_minutes"] = bad
            self.assert_network_rejected(mutate)

    def test_negative_max_depth(self) -> None:
        def mutate(data):
            data["roads"][0]["max_depth_m"] = -0.1
        self.assert_network_rejected(mutate)

    def test_non_finite_max_depth_constant(self) -> None:
        # JSON 的 NaN/Infinity 常量直接拒绝
        data = json.loads(json.dumps(samples.NETWORK))
        data["roads"][0]["max_depth_m"] = 0.5
        text = json.dumps(data).replace(
            '"max_depth_m": 0.5', '"max_depth_m": NaN', 1)
        path = self.tmp / "nan_network.json"
        path.write_text(text, encoding="utf-8")
        self.assertEqual(self.run_cli(network=path), 2)

    def test_flooded_interval_out_of_range(self) -> None:
        def mutate(data):
            for node in data["intersections"]:
                if node["node_id"] == "O":
                    node["flooded_intervals"] = [[10, 121]]
        self.assert_network_rejected(mutate)

    def test_inverted_interval(self) -> None:
        def mutate(data):
            for node in data["intersections"]:
                if node["node_id"] == "O":
                    node["flooded_intervals"] = [[15, 10]]
        self.assert_network_rejected(mutate)

    def test_shelter_window_out_of_range(self) -> None:
        def mutate(data):
            data["shelters"][0]["open_windows"] = [[0, 999]]
        self.assert_network_rejected(mutate)

    def test_analysis_window_inverted(self) -> None:
        def mutate(data):
            data["analysis_window"] = {"start_minute": 120,
                                       "end_minute": 0}
        self.assert_network_rejected(mutate)

    def test_unknown_top_level_key(self) -> None:
        def mutate(data):
            data["surprise"] = True
        self.assert_network_rejected(mutate)


class TestConditionsRejection(EvacCase):
    """积水路况问题：整体拒绝（退出码 3）。"""

    def assert_conditions_rejected(self, rows: list[tuple]) -> None:
        path = self.conditions_variant(rows)
        self.assertEqual(self.run_cli(conditions=path), 3)
        self.assertFalse(self.report_path.exists())

    def test_unknown_road(self) -> None:
        self.assert_conditions_rejected([("R_GHOST", 0, 10, "0.1")])

    def test_empty_interval(self) -> None:
        self.assert_conditions_rejected([("R_AB", 10, 10, "0.1"),
                                         ("R_AB", 12, 10, "0.1")])

    def test_segment_out_of_range(self) -> None:
        self.assert_conditions_rejected([("R_AB", 0, 121, "0.1")])
        self.assert_conditions_rejected([("R_AB", -1, 10, "0.1")])

    def test_negative_depth(self) -> None:
        self.assert_conditions_rejected([("R_AB", 0, 10, "-0.2")])

    def test_non_finite_depth(self) -> None:
        self.assert_conditions_rejected([("R_AB", 0, 10, "NaN")])
        self.assert_conditions_rejected([("R_AB", 0, 10, "Infinity")])

    def test_unparsable_depth(self) -> None:
        self.assert_conditions_rejected([("R_AB", 0, 10, "wet")])

    def test_conflicting_overlap_known_depths(self) -> None:
        self.assert_conditions_rejected([
            ("R_AB", 0, 30, "0.1"),
            ("R_AB", 20, 40, "0.2"),   # [20,30) 深度不同：冲突
        ])

    def test_conflicting_overlap_known_vs_unknown(self) -> None:
        self.assert_conditions_rejected([
            ("R_AB", 0, 30, "0.1"),
            ("R_AB", 20, 40, ""),      # 已知与未知相撞：冲突
        ])

    def test_mergeable_overlaps_load_fine(self) -> None:
        # 同深度重叠、未知与未知重叠、完全重复、首尾相接：都可以合并
        path = self.conditions_variant([
            ("R_AB", 0, 30, "0.1"),
            ("R_AB", 20, 40, "0.1"),
            ("R_AB", 40, 60, "0.1"),
            ("R_AB", 40, 60, "0.1"),
            ("R_AC", 0, 25, ""),
            ("R_AC", 20, 60, ""),
        ])
        self.assertEqual(self.run_cli(conditions=path), 0)

    def test_bad_minute_text(self) -> None:
        self.assert_conditions_rejected([("R_AB", "x", 10, "0.1")])
        self.assert_conditions_rejected([("R_AB", 0, "1.5", "0.1")])

    def test_missing_column(self) -> None:
        path = self.tmp / "missing_col.csv"
        path.write_text("road_id,begin,end\nR_AB,0,10\n", encoding="utf-8")
        self.assertEqual(self.run_cli(conditions=path), 3)


class TestQueriesHandling(EvacCase):

    def test_queries_file_not_json(self) -> None:
        path = self.tmp / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_cli(queries=path), 4)

    def test_queries_top_level_not_requests(self) -> None:
        path = self.queries_variant({"requests": [], "extra": 1})
        self.assertEqual(self.run_cli(queries=path), 4)
        path = self.queries_variant([1, 2, 3], name="queries_list.json")
        self.assertEqual(self.run_cli(queries=path), 4)

    def test_duplicate_request_id_marks_only_the_duplicate(self) -> None:
        path = self.queries_variant({"requests": [
            {"request_id": "DUP", "origin": "A",
             "depart_minute": 0, "deadline_minute": 60},
            {"request_id": "DUP", "origin": "B",
             "depart_minute": 0, "deadline_minute": 60},
            {"request_id": "KEEP", "origin": "G1",
             "depart_minute": 0, "deadline_minute": 60},
        ]})
        rc = self.run_cli(queries=path, output=self.report_path)
        self.assertEqual(rc, 0)
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        statuses = [r["status"] for r in report["results"]]
        self.assertEqual(statuses,
                         ["reachable", "invalid_request", "reachable"])
        self.assertIn("重复", report["results"][1]["reason"])

    def test_malformed_request_entries_become_invalid(self) -> None:
        path = self.queries_variant({"requests": [
            42,
            {"request_id": "X1", "origin": "A"},
            {"request_id": "X2", "origin": "A", "depart_minute": 0,
             "deadline_minute": 60, "extra": 1},
            {"request_id": "X3", "origin": "G1", "depart_minute": 0,
             "deadline_minute": 60},
        ]})
        rc = self.run_cli(queries=path, output=self.report_path)
        self.assertEqual(rc, 0)
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        statuses = [r["status"] for r in report["results"]]
        self.assertEqual(statuses, ["invalid_request", "invalid_request",
                                    "invalid_request", "reachable"])
        self.assertEqual(report["results"][3]["arrival_minute"], 7)


class TestCliBehavior(EvacCase):

    def test_output_file_written_atomically_with_same_content(self) -> None:
        rc = self.run_cli(output=self.report_path)
        self.assertEqual(rc, 0)
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["tool"], "evacuation-access")
        self.assertFalse((self.tmp / "report.json.tmp").exists())

    def test_missing_input_file_exit_codes(self) -> None:
        ghost = self.tmp / "ghost.json"
        self.assertEqual(self.run_cli(network=ghost), 2)
        self.assertEqual(self.run_cli(conditions=ghost), 3)
        self.assertEqual(self.run_cli(queries=ghost), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
