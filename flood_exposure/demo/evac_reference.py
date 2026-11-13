"""evacuation-access 的独立参考检查（**不调用生产搜索器**）。

供 demo/make_evac_samples.py 与 tests/test_evacuation.py 共用。本模块不
import flood_exposure 的任何生产代码，自己解析原始输入文件：

* ``load_raw`` —— 直接解析路网 JSON 与积水 CSV，把积水展开成
  “道路 -> {分钟: 深度或 None}” 的逐分钟表（发现冲突即断言失败，顺带
  复核了生产装载器的冲突检测）；
* ``verify_itinerary`` —— 把返回的行程沿路**逐分钟**重放：每段车程的
  每一分钟都要已知且合格，等待的每一分钟都要允许且安全，抵达时刻路口
  不得受淹，抵达分钟必须落在避险点开放窗口内且不超过截止分钟；
* ``enumerate_reference`` —— 在 (路口, 分钟, 已等待分钟) 状态图上穷举
  全部可行行程（与生产搜索器完全不同的算法），给出最早抵达分钟与该
  抵达分钟下的最少等待，用于独立核对生产搜索的结果。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path


def load_raw(network_path: str | Path, conditions_path: str | Path) -> dict:
    """独立解析原始路网与积水文件，展开为逐分钟查找表。"""
    with open(network_path, encoding="utf-8") as fh:
        net = json.load(fh)

    nodes: dict[str, dict] = {}
    for item in net["intersections"]:
        flooded: set[int] = set()
        for begin, end in item.get("flooded_intervals", []):
            flooded.update(range(begin, end))
        nodes[item["node_id"]] = {
            "can_wait": bool(item["can_wait"]), "flooded": flooded}

    roads: dict[str, dict] = {}
    out_roads: dict[str, list[str]] = {}
    for item in net["roads"]:
        roads[item["road_id"]] = item
        out_roads.setdefault(item["from_node"], []).append(item["road_id"])

    shelters: dict[str, list] = {}
    for item in net.get("shelters", []):
        shelters.setdefault(item["node_id"], []).append(
            (item["shelter_id"], [tuple(w) for w in item["open_windows"]]))

    # 逐分钟深度表：road_id -> {minute: float 深度 或 None(未知)}
    depth: dict[str, dict[int, float | None]] = {}
    with open(conditions_path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            road_id = row["road_id"]
            begin, end = int(row["begin"]), int(row["end"])
            text = (row["depth_m"] or "").strip()
            value = None if text == "" else float(text)
            table = depth.setdefault(road_id, {})
            for minute in range(begin, end):
                if minute in table and table[minute] != value:
                    raise AssertionError(
                        f"参考检查发现冲突片段: {road_id} 分钟 {minute} "
                        f"({table[minute]} vs {value})——生产装载器应当已拒绝")
                table[minute] = value

    return {
        "nodes": nodes,
        "roads": roads,
        "out_roads": out_roads,
        "shelters": shelters,
        "depth": depth,
        "window": (net["analysis_window"]["start_minute"],
                   net["analysis_window"]["end_minute"]),
    }


def flooded_at(raw: dict, node: str, minute: int) -> bool:
    return minute in raw["nodes"][node]["flooded"]


def depth_at(raw: dict, road_id: str, minute: int):
    """返回该分钟的已知深度；无记录或空值都是 None（未知）。"""
    return raw["depth"].get(road_id, {}).get(minute)


def road_passable(raw: dict, road_id: str, enter: int, exit_: int) -> bool:
    """逐分钟检查整个 [enter, exit_)：已知且不超过上限才可通过。"""
    limit = float(raw["roads"][road_id]["max_depth_m"])
    for minute in range(enter, exit_):
        depth = depth_at(raw, road_id, minute)
        if depth is None or depth > limit:
            return False
    return True


def verify_itinerary(raw: dict, request: dict, result: dict) -> list[str]:
    """把一条 reachable 行程沿路逐分钟重放，返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    node = request["origin"]
    minute = request["depart_minute"]
    if flooded_at(raw, node, minute):
        return ["起点在出发时已受淹，不应给出行程"]

    total_wait = 0
    road_ids: list[str] = []
    for step in result["itinerary"]:
        if step["type"] == "wait":
            at = step["node_id"]
            if at != node:
                problems.append(f"等待路口 {at} 与当前位置 {node} 不符")
            if step["start_minute"] != minute:
                problems.append(
                    f"等待开始分钟 {step['start_minute']} 与当前分钟 {minute} 不符")
            if not raw["nodes"][at]["can_wait"]:
                problems.append(f"路口 {at} 不允许等待")
            for m in range(step["start_minute"], step["end_minute"]):
                if flooded_at(raw, at, m):
                    problems.append(f"等待分钟 {m} 路口 {at} 受淹")
            if flooded_at(raw, at, step["end_minute"]):
                problems.append(f"等待结束时刻路口 {at} 受淹")
            if step["minutes"] != step["end_minute"] - step["start_minute"]:
                problems.append("等待时长与起止分钟不符")
            total_wait += step["minutes"]
            minute = step["end_minute"]
        elif step["type"] == "drive":
            road_id = step["road_id"]
            road = raw["roads"].get(road_id)
            if road is None:
                problems.append(f"行程使用了不存在的道路 {road_id}")
                continue
            if road["from_node"] != node or step["from_node"] != node:
                problems.append(
                    f"道路 {road_id} 起点与当前位置 {node} 不符（方向错误?）")
            if step["to_node"] != road["to_node"]:
                problems.append(f"道路 {road_id} 终点记录不符")
            if step["enter_minute"] != minute:
                problems.append(
                    f"进入道路分钟 {step['enter_minute']} 与当前分钟 {minute} 不符")
            if step["exit_minute"] != step["enter_minute"] + road["travel_minutes"]:
                problems.append(f"道路 {road_id} 行驶分钟与记录不符")
            if not road_passable(raw, road_id, step["enter_minute"],
                                 step["exit_minute"]):
                problems.append(
                    f"道路 {road_id} 在 [{step['enter_minute']}, "
                    f"{step['exit_minute']}) 内存在未知或超深分钟")
            if flooded_at(raw, road["to_node"], step["exit_minute"]):
                problems.append(
                    f"抵达时刻 {step['exit_minute']} 目的路口 "
                    f"{road['to_node']} 受淹")
            node = road["to_node"]
            minute = step["exit_minute"]
            road_ids.append(road_id)
        else:
            problems.append(f"未知行程步骤类型: {step['type']}")

    if minute != result["arrival_minute"]:
        problems.append(
            f"重放终点分钟 {minute} 与报告抵达 {result['arrival_minute']} 不符")
    if node != result["shelter_node"]:
        problems.append(
            f"重放终点路口 {node} 与报告避险点路口 {result['shelter_node']} 不符")
    if total_wait != result["total_wait_minutes"]:
        problems.append(
            f"重放等待 {total_wait} 与报告 {result['total_wait_minutes']} 不符")
    if road_ids != result["road_ids"]:
        problems.append(f"重放道路序列 {road_ids} 与报告 {result['road_ids']} 不符")
    if minute > request["deadline_minute"]:
        problems.append("抵达晚于截止分钟")
    shelter_ok = any(
        shelter_id == result["shelter_id"]
        and any(begin <= minute < end for begin, end in windows)
        for shelter_id, windows in raw["shelters"].get(node, []))
    if not shelter_ok:
        problems.append("抵达分钟不在所报避险点的开放窗口内")
    return problems


def enumerate_reference(raw: dict, origin: str, depart: int,
                        deadline: int):
    """穷举全部可行行程，返回 (最早抵达分钟, 该抵达下最少等待) 或 None。

    与生产搜索器完全独立的算法：在 (路口, 分钟, 已等待) 状态图上 DFS，
    用“同 (路口, 分钟) 已等待更少即支配”剪枝。调用前保证起点在出发时
    未受淹（受淹的查询属于 invalid_request，不在此核对）。
    """
    best: dict[tuple[str, int], int] = {}
    goals: list[tuple[int, int]] = []
    stack = [(origin, depart, 0)]
    while stack:
        node, minute, waits = stack.pop()
        if minute > deadline:
            continue
        key = (node, minute)
        if best.get(key) is not None and best[key] <= waits:
            continue
        best[key] = waits
        for _shelter_id, windows in raw["shelters"].get(node, []):
            if any(begin <= minute < end for begin, end in windows):
                goals.append((minute, waits))
                break
        if minute + 1 <= deadline and raw["nodes"][node]["can_wait"] \
                and not flooded_at(raw, node, minute + 1):
            stack.append((node, minute + 1, waits + 1))
        for road_id in raw["out_roads"].get(node, []):
            road = raw["roads"][road_id]
            arrive = minute + road["travel_minutes"]
            if arrive > deadline:
                continue
            if flooded_at(raw, road["to_node"], arrive):
                continue
            if not road_passable(raw, road_id, minute, arrive):
                continue
            stack.append((road["to_node"], arrive, waits))
    if not goals:
        return None
    return min(goals)
