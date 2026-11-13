"""生成 evacuation-access 的合成样例，并以独立参考检查核对完整处理链。

用法（从仓库根目录，隔离模式运行）::

    python3 -I demo/make_evac_samples.py [输出目录]

一个路网覆盖全部规定场景（分析范围 [0, 120] 分钟）：

* A→B→D 短路线会在车程中被淹（R_BD 第 6 分钟起超深），A→C→D 长路线通过；
  长路线上 R_AC 积水 0.4m 恰好压通行上限 0.4m（压线仍可通过）
* K 禁止等待：R_KM 第 3–6 分钟超深，必须绕 K→L→K 一圈更晚抵达 K 才有出路
* O 允许等待但第 10–15 分钟受淹：R_OP 第 8–12 分钟路况空档，等待被受淹
  窗口阻断 → 不可达；从 N2 出发恰在第 10 分钟抵达 O 不合法，须等到第 15
  分钟才允许抵达
* R_UV 第 5–9 分钟是显式空值（未知），U 允许安全等待 → 等待后通过
* 避险点 S_H 第 20 分钟才开放：在允许等待的 H3 等到开放
* 避险点 S_W 第 12 分钟恰好关闭：第 12 分钟抵达不算赶上 → 不可达
* R_Y21 只有反向（Y2→Y1）：从 Y1 出发无路可走 → 不可达
* G1→G4 两条等价路线同时到达：按道路编号序列字典序选 [R07, R09]
* 同批混入 4 个无效请求（未知起点、出发晚于截止、截止越界、起点已受淹），
  互不影响

断言全部来自手算字面量与独立参考检查（demo/evac_reference.py，不调用
生产搜索器）：逐分钟重放每条返回行程、穷举可行行程核对最早抵达与等待、
打乱道路/片段顺序后结果稳定。
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flood_exposure.cli import main  # noqa: E402
from flood_exposure.hashing import sha256_file  # noqa: E402

import evac_reference  # noqa: E402

ANALYSIS_START, ANALYSIS_END = 0, 120

# --------------------------------------------------------------------------- #
# 样例输入（手算字面量；道路/片段顺序刻意打乱，证明结果与排列次序无关）
# --------------------------------------------------------------------------- #
NETWORK = {
    "analysis_window": {"start_minute": ANALYSIS_START,
                        "end_minute": ANALYSIS_END},
    "intersections": [
        # 组件 A：短路线中途被淹，长路线（压线水深）通过
        {"node_id": "A", "can_wait": True, "flooded_intervals": []},
        {"node_id": "B", "can_wait": True, "flooded_intervals": []},
        {"node_id": "C", "can_wait": True, "flooded_intervals": []},
        {"node_id": "D", "can_wait": True, "flooded_intervals": []},
        # 组件 B：K/L 禁止等待，绕圈换时间
        {"node_id": "J", "can_wait": False, "flooded_intervals": []},
        {"node_id": "K", "can_wait": False, "flooded_intervals": []},
        {"node_id": "L", "can_wait": False, "flooded_intervals": []},
        {"node_id": "M", "can_wait": True, "flooded_intervals": []},
        # 组件 C：O 允许等待但受淹窗口阻断等待
        {"node_id": "N", "can_wait": False, "flooded_intervals": []},
        {"node_id": "N2", "can_wait": True, "flooded_intervals": []},
        {"node_id": "O", "can_wait": True, "flooded_intervals": [[10, 15]]},
        {"node_id": "P", "can_wait": True, "flooded_intervals": []},
        # 组件 C2：U 允许安全等待，等过未知空档
        {"node_id": "T", "can_wait": False, "flooded_intervals": []},
        {"node_id": "U", "can_wait": True, "flooded_intervals": []},
        {"node_id": "V", "can_wait": True, "flooded_intervals": []},
        # 组件 D：避险点第 20 分钟才开放
        {"node_id": "H1", "can_wait": False, "flooded_intervals": []},
        {"node_id": "H2", "can_wait": False, "flooded_intervals": []},
        {"node_id": "H3", "can_wait": True, "flooded_intervals": []},
        # 组件 E：避险点第 12 分钟恰好关闭
        {"node_id": "W1", "can_wait": False, "flooded_intervals": []},
        {"node_id": "W2", "can_wait": False, "flooded_intervals": []},
        {"node_id": "W3", "can_wait": False, "flooded_intervals": []},
        # 组件 F：两条等价路线
        {"node_id": "G1", "can_wait": False, "flooded_intervals": []},
        {"node_id": "G2", "can_wait": False, "flooded_intervals": []},
        {"node_id": "G3", "can_wait": False, "flooded_intervals": []},
        {"node_id": "G4", "can_wait": False, "flooded_intervals": []},
        # 组件 G：只有反向道路
        {"node_id": "Y1", "can_wait": True, "flooded_intervals": []},
        {"node_id": "Y2", "can_wait": True, "flooded_intervals": []},
    ],
    "roads": [
        # 组件 F 的 R08/R10 故意排在 R07/R09 前面
        {"road_id": "R08", "from_node": "G1", "to_node": "G3",
         "travel_minutes": 3, "max_depth_m": 0.5},
        {"road_id": "R10", "from_node": "G3", "to_node": "G4",
         "travel_minutes": 4, "max_depth_m": 0.5},
        {"road_id": "R_BD", "from_node": "B", "to_node": "D",
         "travel_minutes": 5, "max_depth_m": 0.3},
        {"road_id": "R_AB", "from_node": "A", "to_node": "B",
         "travel_minutes": 5, "max_depth_m": 0.3},
        {"road_id": "R_CD", "from_node": "C", "to_node": "D",
         "travel_minutes": 8, "max_depth_m": 0.5},
        {"road_id": "R_AC", "from_node": "A", "to_node": "C",
         "travel_minutes": 8, "max_depth_m": 0.4},
        {"road_id": "R_CA", "from_node": "C", "to_node": "A",
         "travel_minutes": 3, "max_depth_m": 0.9},
        {"road_id": "R_KM", "from_node": "K", "to_node": "M",
         "travel_minutes": 4, "max_depth_m": 0.2},
        {"road_id": "R_JK", "from_node": "J", "to_node": "K",
         "travel_minutes": 3, "max_depth_m": 0.2},
        {"road_id": "R_LK", "from_node": "L", "to_node": "K",
         "travel_minutes": 2, "max_depth_m": 0.2},
        {"road_id": "R_KL", "from_node": "K", "to_node": "L",
         "travel_minutes": 2, "max_depth_m": 0.2},
        {"road_id": "R_OP", "from_node": "O", "to_node": "P",
         "travel_minutes": 6, "max_depth_m": 0.5},
        {"road_id": "R_N2O", "from_node": "N2", "to_node": "O",
         "travel_minutes": 6, "max_depth_m": 0.5},
        {"road_id": "R_NO", "from_node": "N", "to_node": "O",
         "travel_minutes": 4, "max_depth_m": 0.5},
        {"road_id": "R_UV", "from_node": "U", "to_node": "V",
         "travel_minutes": 4, "max_depth_m": 0.5},
        {"road_id": "R_TU", "from_node": "T", "to_node": "U",
         "travel_minutes": 3, "max_depth_m": 0.5},
        {"road_id": "R_H23", "from_node": "H2", "to_node": "H3",
         "travel_minutes": 5, "max_depth_m": 0.5},
        {"road_id": "R_H12", "from_node": "H1", "to_node": "H2",
         "travel_minutes": 4, "max_depth_m": 0.5},
        {"road_id": "R07", "from_node": "G1", "to_node": "G2",
         "travel_minutes": 3, "max_depth_m": 0.5},
        {"road_id": "R09", "from_node": "G2", "to_node": "G4",
         "travel_minutes": 4, "max_depth_m": 0.5},
        {"road_id": "R_W23", "from_node": "W2", "to_node": "W3",
         "travel_minutes": 6, "max_depth_m": 0.5},
        {"road_id": "R_W12", "from_node": "W1", "to_node": "W2",
         "travel_minutes": 6, "max_depth_m": 0.5},
        {"road_id": "R_Y21", "from_node": "Y2", "to_node": "Y1",
         "travel_minutes": 2, "max_depth_m": 0.5},
    ],
    "shelters": [
        {"shelter_id": "S_W", "node_id": "W3", "open_windows": [[0, 12]]},
        {"shelter_id": "S_D", "node_id": "D", "open_windows": [[0, 120]]},
        {"shelter_id": "S_M", "node_id": "M", "open_windows": [[0, 120]]},
        {"shelter_id": "S_P", "node_id": "P", "open_windows": [[0, 120]]},
        {"shelter_id": "S_V", "node_id": "V", "open_windows": [[0, 120]]},
        {"shelter_id": "S_H", "node_id": "H3", "open_windows": [[20, 120]]},
        {"shelter_id": "S_G", "node_id": "G4", "open_windows": [[0, 120]]},
        {"shelter_id": "S_Y", "node_id": "Y2", "open_windows": [[0, 120]]},
    ],
}

# (road_id, begin, end, depth_m)；空字符串 = 未知。刻意乱序并包含：
# 完全重复行、同深度重叠（可合并）、未知与未知重叠（可合并）、真实空档。
CONDITION_ROWS = [
    ("R_CD", 20, 60, "0.0"),
    ("R_AB", 0, 60, "0.1"),
    ("R_BD", 6, 20, "0.5"),      # 短路线车程中段超深
    ("R_AC", 0, 30, "0.4"),      # 恰好压上限 0.4：可通过
    ("R_CD", 0, 40, "0.0"),      # 与第一行 [20,40) 同深度重叠：合并
    ("R_BD", 0, 6, "0.1"),
    ("R_AC", 30, 60, "0.4"),     # 首尾相接：连用
    ("R_BD", 20, 60, "0.1"),
    ("R_AB", 0, 60, "0.1"),      # 完全重复：合并
    ("R_JK", 0, 60, "0.0"),
    ("R_KM", 3, 6, "0.9"),       # 超过上限 0.2
    ("R_KM", 0, 3, "0.0"),
    ("R_KM", 6, 60, "0.0"),
    ("R_KL", 0, 60, "0.0"),
    ("R_LK", 0, 60, "0.0"),
    ("R_NO", 0, 60, "0.0"),
    ("R_N2O", 0, 120, "0.0"),
    ("R_OP", 0, 8, "0.0"),
    ("R_OP", 12, 60, "0.0"),     # [8,12) 真实空档：未知
    ("R_TU", 0, 60, "0.0"),
    ("R_UV", 0, 5, "0.0"),
    ("R_UV", 5, 9, ""),          # 显式空值：未知
    ("R_UV", 9, 60, "0.0"),
    ("R_H12", 0, 60, "0.0"),
    ("R_H23", 0, 60, "0.0"),
    ("R07", 0, 60, "0.0"),
    ("R08", 0, 60, "0.0"),
    ("R09", 0, 60, "0.0"),
    ("R10", 0, 60, "0.0"),
    ("R_W12", 0, 60, "0.0"),
    ("R_W23", 0, 60, "0.0"),
    ("R_Y21", 0, 60, "0.0"),
    ("R_CA", 10, 25, ""),        # 未知
    ("R_CA", 0, 10, "0.0"),
    ("R_CA", 15, 30, ""),        # 与 [10,25) 未知重叠：合并为 [10,30)
    ("R_CA", 30, 60, "0.0"),
]

QUERIES = [
    # 有效与无效请求刻意混排
    {"request_id": "Q1-long-route", "origin": "A",
     "depart_minute": 0, "deadline_minute": 60},
    {"request_id": "Q2-bad-origin", "origin": "ZZZ",
     "depart_minute": 0, "deadline_minute": 60},
    {"request_id": "Q3-edge-of-closure", "origin": "B",
     "depart_minute": 1, "deadline_minute": 60},
    {"request_id": "Q4-depth-wait", "origin": "B",
     "depart_minute": 2, "deadline_minute": 30},
    {"request_id": "Q5-depth-unreachable", "origin": "B",
     "depart_minute": 2, "deadline_minute": 24},
    {"request_id": "Q6-loop-late-arrival", "origin": "J",
     "depart_minute": 0, "deadline_minute": 60},
    {"request_id": "Q7-nowait-unreachable", "origin": "K",
     "depart_minute": 3, "deadline_minute": 8},
    {"request_id": "Q8-wait-blocked-by-flood", "origin": "N",
     "depart_minute": 0, "deadline_minute": 60},
    {"request_id": "Q9-arrive-at-flood-start", "origin": "N2",
     "depart_minute": 4, "deadline_minute": 60},
    {"request_id": "Q10-midroute-wait", "origin": "T",
     "depart_minute": 0, "deadline_minute": 60},
    {"request_id": "Q11-shelter-opens-late", "origin": "H1",
     "depart_minute": 0, "deadline_minute": 60},
    {"request_id": "Q12-bad-time-order", "origin": "A",
     "depart_minute": 50, "deadline_minute": 40},
    {"request_id": "Q13-tie-break", "origin": "G1",
     "depart_minute": 0, "deadline_minute": 60},
    {"request_id": "Q14-shelter-closed", "origin": "W1",
     "depart_minute": 0, "deadline_minute": 60},
    {"request_id": "Q15-reverse-road", "origin": "Y1",
     "depart_minute": 0, "deadline_minute": 60},
    {"request_id": "Q16-flooded-origin", "origin": "O",
     "depart_minute": 12, "deadline_minute": 60},
    {"request_id": "Q17-deadline-out-of-range", "origin": "A",
     "depart_minute": 0, "deadline_minute": 180},
    {"request_id": "Q18-already-at-shelter", "origin": "D",
     "depart_minute": 0, "deadline_minute": 60},
]

# 手算期望（字面量，不调用被测代码）
EXPECTED = {
    "Q1-long-route": {"status": "reachable", "shelter_id": "S_D",
                      "arrival": 16, "wait": 0,
                      "road_ids": ["R_AC", "R_CD"], "waits": []},
    "Q2-bad-origin": {"status": "invalid_request"},
    "Q3-edge-of-closure": {"status": "reachable", "shelter_id": "S_D",
                           "arrival": 6, "wait": 0,
                           "road_ids": ["R_BD"], "waits": []},
    "Q4-depth-wait": {"status": "reachable", "shelter_id": "S_D",
                      "arrival": 25, "wait": 18,
                      "road_ids": ["R_BD"], "waits": [("B", 2, 20)]},
    "Q5-depth-unreachable": {"status": "unreachable",
                             "blockers": {"depth_exceeded",
                                          "deadline_exceeded"}},
    "Q6-loop-late-arrival": {"status": "reachable", "shelter_id": "S_M",
                             "arrival": 11, "wait": 0,
                             "road_ids": ["R_JK", "R_KL", "R_LK", "R_KM"],
                             "waits": []},
    "Q7-nowait-unreachable": {"status": "unreachable",
                              "blockers": {"depth_exceeded",
                                           "waiting_not_allowed",
                                           "deadline_exceeded"}},
    "Q8-wait-blocked-by-flood": {"status": "unreachable",
                                 "blockers": {"unknown_conditions",
                                              "wait_blocked_by_flood",
                                              "waiting_not_allowed"}},
    "Q9-arrive-at-flood-start": {"status": "reachable", "shelter_id": "S_P",
                                 "arrival": 21, "wait": 5,
                                 "road_ids": ["R_N2O", "R_OP"],
                                 "waits": [("N2", 4, 9)]},
    "Q10-midroute-wait": {"status": "reachable", "shelter_id": "S_V",
                          "arrival": 13, "wait": 6,
                          "road_ids": ["R_TU", "R_UV"],
                          "waits": [("U", 3, 9)]},
    "Q11-shelter-opens-late": {"status": "reachable", "shelter_id": "S_H",
                               "arrival": 20, "wait": 11,
                               "road_ids": ["R_H12", "R_H23"],
                               "waits": [("H3", 9, 20)]},
    "Q12-bad-time-order": {"status": "invalid_request"},
    "Q13-tie-break": {"status": "reachable", "shelter_id": "S_G",
                      "arrival": 7, "wait": 0,
                      "road_ids": ["R07", "R09"], "waits": []},
    "Q14-shelter-closed": {"status": "unreachable",
                           "blockers": {"shelter_not_open",
                                        "waiting_not_allowed"}},
    "Q15-reverse-road": {"status": "unreachable", "blockers": set()},
    "Q16-flooded-origin": {"status": "invalid_request"},
    "Q17-deadline-out-of-range": {"status": "invalid_request"},
    "Q18-already-at-shelter": {"status": "reachable", "shelter_id": "S_D",
                               "arrival": 0, "wait": 0,
                               "road_ids": [], "waits": []},
}

CSV_HEADER = ["road_id", "begin", "end", "depth_m"]


# --------------------------------------------------------------------------- #
# 样例文件写出
# --------------------------------------------------------------------------- #
def write_conditions(path: Path, rows: list[tuple]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for row in rows:
            writer.writerow(row)


def write_samples(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    network_path = directory / "network.json"
    conditions_path = directory / "conditions.csv"
    queries_path = directory / "queries.json"
    network_path.write_text(json.dumps(NETWORK, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    write_conditions(conditions_path, CONDITION_ROWS)
    queries_path.write_text(json.dumps({"requests": QUERIES},
                                       ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return {"network": network_path, "conditions": conditions_path,
            "queries": queries_path}


def shuffled_variants(directory: Path, paths: dict) -> dict:
    """道路/路口/避险点/积水片段全部倒序重写，用于次序无关性核对。"""
    shuffled_net = json.loads(json.dumps(NETWORK))
    shuffled_net["intersections"].reverse()
    shuffled_net["roads"].reverse()
    shuffled_net["shelters"].reverse()
    network_path = directory / "network_shuffled.json"
    conditions_path = directory / "conditions_shuffled.csv"
    network_path.write_text(json.dumps(shuffled_net, ensure_ascii=False,
                                       indent=2), encoding="utf-8")
    write_conditions(conditions_path, list(reversed(CONDITION_ROWS)))
    return {"network": network_path, "conditions": conditions_path,
            "queries": paths["queries"]}


# --------------------------------------------------------------------------- #
# 独立断言
# --------------------------------------------------------------------------- #
def _result_by_id(report: dict) -> dict:
    return {r["request_id"]: r for r in report["results"]}


def check_expectations(report: dict) -> None:
    results = _result_by_id(report)
    assert len(report["results"]) == len(QUERIES), "结果数量与请求数量不符"
    assert [r["request_id"] for r in report["results"]] == \
        [q["request_id"] for q in QUERIES], "结果顺序应与请求顺序一致"
    for request_id, expected in EXPECTED.items():
        result = results[request_id]
        assert result["status"] == expected["status"], \
            f"{request_id}: 状态 {result['status']} != 期望 {expected['status']}"
        if expected["status"] == "reachable":
            assert result["shelter_id"] == expected["shelter_id"], request_id
            assert result["arrival_minute"] == expected["arrival"], \
                f"{request_id}: 抵达 {result['arrival_minute']} != {expected['arrival']}"
            assert result["total_wait_minutes"] == expected["wait"], \
                f"{request_id}: 等待 {result['total_wait_minutes']} != {expected['wait']}"
            assert result["road_ids"] == expected["road_ids"], \
                f"{request_id}: 道路序列 {result['road_ids']} != {expected['road_ids']}"
            waits = [(s["node_id"], s["start_minute"], s["end_minute"])
                     for s in result["itinerary"] if s["type"] == "wait"]
            assert waits == expected["waits"], \
                f"{request_id}: 等待 {waits} != {expected['waits']}"
        elif expected["status"] == "unreachable":
            blockers = set(result["search"]["blockers"].keys())
            assert blockers == expected["blockers"], \
                f"{request_id}: 阻塞原因 {blockers} != {expected['blockers']}"
            assert result["search"]["states_expanded"] >= 1
        else:
            assert result["reason"], f"{request_id}: 无效请求必须给出原因"
    summary = report["summary"]
    assert (summary["reachable"], summary["unreachable"],
            summary["invalid_request"]) == (9, 5, 4), summary


def check_replay_and_enumeration(report: dict, paths: dict) -> None:
    """独立参考检查：逐分钟重放 + 穷举核对（不调用生产搜索器）。"""
    raw = evac_reference.load_raw(paths["network"], paths["conditions"])
    requests = {q["request_id"]: q for q in QUERIES}
    for result in report["results"]:
        request = requests[result["request_id"]]
        if result["status"] == "reachable":
            problems = evac_reference.verify_itinerary(raw, request, result)
            assert not problems, \
                f"{result['request_id']} 行程重放失败: {problems}"
        if result["status"] == "invalid_request":
            continue
        reference = evac_reference.enumerate_reference(
            raw, request["origin"], request["depart_minute"],
            request["deadline_minute"])
        if result["status"] == "unreachable":
            assert reference is None, (
                f"{result['request_id']}: 生产搜索判不可达，但穷举找到 "
                f"{reference}")
        else:
            assert reference == (result["arrival_minute"],
                                 result["total_wait_minutes"]), (
                f"{result['request_id']}: 穷举最优 {reference} != 生产结果 "
                f"({result['arrival_minute']}, {result['total_wait_minutes']})")


def check_shuffle_stability(directory: Path, paths: dict,
                            original: dict) -> None:
    variants = shuffled_variants(directory, paths)
    out_path = directory / "report_shuffled.json"
    rc = main(["evacuation-access",
               "--network", str(variants["network"]),
               "--conditions", str(variants["conditions"]),
               "--queries", str(variants["queries"]),
               "--output", str(out_path)])
    assert rc == 0, f"打乱次序后退出码 {rc}"
    shuffled = json.loads(out_path.read_text(encoding="utf-8"))
    assert shuffled["results"] == original["results"], \
        "打乱道路/片段次序后结果发生变化"
    assert shuffled["parameters"] == original["parameters"]
    assert shuffled["network_summary"]["condition_segments"] == \
        original["network_summary"]["condition_segments"]


def check_provenance(report: dict, paths: dict) -> None:
    inputs = report["inputs"]
    assert inputs["network_json"]["sha256"] == sha256_file(paths["network"])
    assert inputs["conditions_csv"]["sha256"] == sha256_file(paths["conditions"])
    assert inputs["queries_json"]["sha256"] == sha256_file(paths["queries"])
    assert report["parameters"] == {
        "analysis_start_minute": ANALYSIS_START,
        "analysis_end_minute": ANALYSIS_END}


def _format_itinerary(result: dict) -> str:
    parts = []
    for step in result["itinerary"]:
        if step["type"] == "drive":
            parts.append(f"乘 {step['road_id']} "
                         f"[{step['enter_minute']}→{step['exit_minute']}]")
        else:
            parts.append(f"在 {step['node_id']} 等待 "
                         f"[{step['start_minute']}→{step['end_minute']}]")
    return "；".join(parts) if parts else "（无需移动）"


def main_demo(directory: Path) -> None:
    paths = write_samples(directory)
    report_path = directory / "report.json"
    rc = main(["evacuation-access",
               "--network", str(paths["network"]),
               "--conditions", str(paths["conditions"]),
               "--queries", str(paths["queries"]),
               "--output", str(report_path)])
    assert rc == 0, f"evacuation-access 退出码 {rc}"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    check_provenance(report, paths)
    check_expectations(report)
    check_replay_and_enumeration(report, paths)
    check_shuffle_stability(directory, paths, report)

    results = _result_by_id(report)
    print(f"样例目录: {directory}")
    print(f"请求 {len(QUERIES)} 个：可达 {report['summary']['reachable']}、"
          f"不可达 {report['summary']['unreachable']}、"
          f"无效 {report['summary']['invalid_request']}\n")
    for request_id in ("Q1-long-route", "Q6-loop-late-arrival",
                       "Q10-midroute-wait", "Q13-tie-break"):
        result = results[request_id]
        print(f"  {request_id}: 第 {result['arrival_minute']} 分钟抵达 "
              f"{result['shelter_id']}（等待 {result['total_wait_minutes']} 分钟）")
        print(f"    {_format_itinerary(result)}")
    print("\n  不可达示例:")
    for request_id in ("Q5-depth-unreachable", "Q8-wait-blocked-by-flood",
                       "Q14-shelter-closed"):
        result = results[request_id]
        blockers = result["search"]["blockers"]
        summary = ", ".join(f"{k}×{v['count']}" for k, v in blockers.items())
        print(f"  {request_id}: {summary or '无任何可行出边'}")
    print("\n全部独立断言通过：手算期望、逐分钟重放、穷举核对、次序无关。")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample_evac")
    main_demo(target)
