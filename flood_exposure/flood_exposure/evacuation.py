"""应急撤离可达性分析（纯本地、标准库实现）。

面向应急值班的提问：“从某路口出发，车能否赶在道路被淹以前到达仍开放的
避险点？” 本模块读取三份本地输入——路网 JSON、路面积水 CSV、查询
JSON——按“地点 × 分钟”的状态空间计算**可实际执行**的撤离行程，而不是
只看出发那一刻的静态路况。本模块不接入网络，也不依赖或改动洪水栅格 /
DEM / 雨量计算链（不 import numpy / rasterio / pandas）。

时间模型
--------
* 全部时间都是相对同一参考时刻的整数分钟；分析范围是路网声明的有限区间
  ``[analysis_start, analysis_end]``：时刻可以落在端点上，时间区间不得
  越出该范围。
* 积水记录覆盖左闭右开区间 ``[begin, end)``；没有记录的空档与空值深度
  一律视为**未知**，未知绝不当成干路。重叠记录只有深度含义相同（都已知
  且相等，或都未知）才可合并；已知与未知相撞、或已知深度不同，属于冲突，
  整体拒绝装载。
* 道路深度等于通行上限仍可通过，超过上限不可通过。一段车程
  ``[enter, exit)`` 必须在**整个**区间上有已知且合格的路况——不只看
  端点，也不跨过中途的未知缺口。车程恰在某封闭窗口开始的那一分钟结束
  时，道路检查不含抵达端点（半开区间），但目的路口仍检查抵达当时是否
  受淹。
* 抵达正在受淹的路口不合法；在路口等待一分钟，要求该路口允许等待且这
  一整分钟安全；不能在道路中间停车。车辆只沿道路给定方向行驶。
* 避险点开放窗口同样左闭右开：抵达关闭时刻不算赶上。起点在出发时已受淹
  的查询直接判 ``invalid_request``，不把车辆凭空放到其他位置。

搜索与优化顺序
--------------
状态 = (路口, 分钟)。所有道路耗时为正且截止分钟有限，按分钟递增一次动态
规划即可终止并给出精确最优：先最早抵达任一开放避险点，再累计等待分钟最
少，仍相同按道路编号序列的字典序。因此“只为每个路口保留最早到达”是
**不够**的：禁止等待的路口可能需要绕行后更晚抵达，才能接上后续开放的
道路。同一状态若 (等待, 道路序列) 都相同（只可能等待位置不同），由时间
递增的处理顺序确定，与输入文件中道路/片段的排列次序无关。

结果只在已知路况与所给行驶时间假设下成立；未知路况会阻断行程而不是被
乐观忽略。
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .hashing import sha256_file

SCHEMA_VERSION = 1

CSV_ROAD_COL = "road_id"
CSV_BEGIN_COL = "begin"
CSV_END_COL = "end"
CSV_DEPTH_COL = "depth_m"
REQUIRED_CSV_COLUMNS = (CSV_ROAD_COL, CSV_BEGIN_COL, CSV_END_COL, CSV_DEPTH_COL)

#: 不可达诊断的阻塞原因类别（固定顺序）。
BLOCKER_CATEGORIES = (
    "depth_exceeded",        # 行程区间内含已知超深分钟
    "unknown_conditions",    # 行程区间内含未知路况（空档或空值）
    "waiting_not_allowed",   # 路口禁止等待
    "wait_blocked_by_flood", # 等待的下一分钟路口受淹
    "arrival_node_flooded",  # 驾车抵达时刻目的路口受淹
    "deadline_exceeded",     # 该车程抵达会超过截止分钟
    "shelter_not_open",      # 到达避险点路口但开放窗口未覆盖该分钟
)

#: 每类阻塞原因最多保留的示例条数（计数始终是精确值）。
MAX_BLOCKER_EXAMPLES = 5


class EvacuationNetworkError(ValueError):
    """路网 JSON 非法（结构、编号、引用、耗时、深度、区间越界等）。"""


class EvacuationConditionsError(ValueError):
    """积水 CSV 非法或片段冲突，整体拒绝装载。"""


class EvacuationQueryError(ValueError):
    """查询 JSON 整体结构非法（单个请求的问题不致命，标 invalid_request）。"""


class EvacuationOutputError(RuntimeError):
    """报告写出失败。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Intersection:
    """路口：编号、是否允许等待、受淹分钟区间（已归并，左闭右开）。"""

    node_id: str
    can_wait: bool
    flooded: tuple[tuple[int, int], ...]

    def flooded_at(self, minute: int) -> bool:
        return any(begin <= minute < end for begin, end in self.flooded)


@dataclass(frozen=True)
class Road:
    """有向道路：正整数行驶分钟数与可通行最大积水深度。"""

    road_id: str
    from_node: str
    to_node: str
    travel_minutes: int
    max_depth_m: Decimal


@dataclass(frozen=True)
class Shelter:
    """挂在路口上的避险点：若干左闭右开的开放窗口。"""

    shelter_id: str
    node_id: str
    windows: tuple[tuple[int, int], ...]

    def open_at(self, minute: int) -> bool:
        return any(begin <= minute < end for begin, end in self.windows)


@dataclass(frozen=True)
class Network:
    """校验通过的路网。"""

    analysis_start: int
    analysis_end: int
    intersections: dict[str, Intersection]
    roads: dict[str, Road]
    shelters: dict[str, Shelter]
    sorted_node_ids: tuple[str, ...]
    out_roads: dict[str, tuple[Road, ...]]        # 按 road_id 排序
    shelters_by_node: dict[str, tuple[Shelter, ...]]


@dataclass(frozen=True)
class Segment:
    """一条积水记录覆盖的左闭右开区间；depth 为 None 表示未知。"""

    begin: int
    end: int
    depth: Decimal | None


@dataclass(frozen=True)
class Conditions:
    """校验并归并后的路况：road_id -> 互不重叠、按 begin 排序的片段。"""

    segments: dict[str, tuple[Segment, ...]]
    record_count: int          # 原始记录条数
    merged_record_count: int   # 因完全相同/同含义重叠而合并掉的条数

    def road_status(self, road: Road, enter: int, exit_: int):
        """检查行程区间 [enter, exit_) 的路况。

        返回 ``("ok", None)`` / ``("unknown", None)`` /
        ``("depth_exceeded", 区间内最大已知深度)``。整个区间都必须被已知且
        合格的片段覆盖；空档与空值都是未知。区间内若同时存在超深与未知，
        优先报告确定的超深。
        """
        cursor = enter
        fully_known = True
        max_known: Decimal | None = None
        for seg in self.segments.get(road.road_id, ()):
            if seg.end <= enter:
                continue
            if seg.begin >= exit_:
                break
            if seg.begin > cursor:
                fully_known = False      # 覆盖空档：未知
            cursor = max(cursor, seg.end)
            if seg.depth is None:
                fully_known = False      # 空值深度：未知
            elif max_known is None or seg.depth > max_known:
                max_known = seg.depth
        if cursor < exit_:
            fully_known = False          # 尾部无记录：未知
        if max_known is not None and max_known > road.max_depth_m:
            return "depth_exceeded", max_known
        if not fully_known:
            return "unknown", None
        return "ok", None


# --------------------------------------------------------------------------- #
# 通用解析小工具
# --------------------------------------------------------------------------- #
def _require_int(value: object, context: str, error=EvacuationNetworkError) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error(f"{context} 必须是整数分钟，得到: {value!r}")
    return value


def _require_id(value: object, context: str, error=EvacuationNetworkError) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{context} 必须是非空字符串，得到: {value!r}")
    return value


def _to_depth(value: object, context: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise EvacuationNetworkError(f"{context} 必须是数字，得到: {value!r}")
    depth = value if isinstance(value, Decimal) else Decimal(str(value))
    if not depth.is_finite():
        raise EvacuationNetworkError(f"{context} 必须是有限数值，得到: {value!r}")
    if depth < 0:
        raise EvacuationNetworkError(f"{context} 不能为负: {depth}")
    return depth


def _reject_json_constant(token: str):
    raise EvacuationNetworkError(
        f"路网 JSON 含非有限数值常量 {token!r}，整体拒绝")


def _parse_interval_pair(raw: object, context: str,
                         start: int, end: int,
                         error=EvacuationNetworkError) -> tuple[int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise error(f"{context} 必须是 [begin, end] 二元组，得到: {raw!r}")
    begin = _require_int(raw[0], f"{context} 起点", error)
    finish = _require_int(raw[1], f"{context} 终点", error)
    if begin >= finish:
        raise error(f"{context} 起点必须早于终点: [{begin}, {finish})")
    if begin < start or finish > end:
        raise error(
            f"{context} [{begin}, {finish}) 越出分析范围 [{start}, {end}]，整体拒绝")
    return begin, finish


def _merge_intervals(intervals: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """归并重叠或首尾相接的区间（含义相同的受淹/开放区间）。"""
    merged: list[list[int]] = []
    for begin, end in sorted(intervals):
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    return tuple((b, e) for b, e in merged)


# --------------------------------------------------------------------------- #
# 路网装载（任何问题整体拒绝）
# --------------------------------------------------------------------------- #
def load_network(path: str | Path) -> Network:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh, parse_float=Decimal,
                             parse_constant=_reject_json_constant)
    except EvacuationNetworkError:
        raise
    except json.JSONDecodeError as exc:
        raise EvacuationNetworkError(f"路网文件不是合法 JSON: {exc}") from exc
    except OSError as exc:
        raise EvacuationNetworkError(f"无法读取路网文件 {path}: {exc}") from exc
    return _parse_network(data)


def _parse_network(data: object) -> Network:
    ctx = "路网"
    if not isinstance(data, dict):
        raise EvacuationNetworkError("路网顶层必须是对象")
    allowed = {"analysis_window", "intersections", "roads", "shelters"}
    unknown = sorted(set(data.keys()) - allowed)
    if unknown:
        raise EvacuationNetworkError(f"路网含未知顶层字段: {unknown}")
    for key in ("analysis_window", "intersections", "roads"):
        if key not in data:
            raise EvacuationNetworkError(f"路网缺少必填字段 {key}")

    window = data["analysis_window"]
    if not isinstance(window, dict) or \
            set(window.keys()) != {"start_minute", "end_minute"}:
        raise EvacuationNetworkError(
            "analysis_window 必须且只能含 start_minute/end_minute")
    start = _require_int(window["start_minute"], "分析范围起点")
    end = _require_int(window["end_minute"], "分析范围终点")
    if start >= end:
        raise EvacuationNetworkError(
            f"分析范围起点必须早于终点: [{start}, {end}]")

    raw_nodes = data["intersections"]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise EvacuationNetworkError("intersections 必须是非空数组")
    intersections: dict[str, Intersection] = {}
    for index, raw in enumerate(raw_nodes):
        ictx = f"{ctx}第 {index} 个路口"
        if not isinstance(raw, dict):
            raise EvacuationNetworkError(f"{ictx} 必须是对象")
        unknown_keys = sorted(
            set(raw.keys()) - {"node_id", "can_wait", "flooded_intervals"})
        if unknown_keys:
            raise EvacuationNetworkError(f"{ictx} 含未知字段: {unknown_keys}")
        node_id = _require_id(raw.get("node_id"), f"{ictx} 的 node_id")
        if node_id in intersections:
            raise EvacuationNetworkError(
                f"路口编号重复: {node_id!r}，整体拒绝")
        can_wait = raw.get("can_wait")
        if not isinstance(can_wait, bool):
            raise EvacuationNetworkError(
                f"路口 {node_id} 的 can_wait 必须是布尔值")
        raw_flooded = raw.get("flooded_intervals", [])
        if not isinstance(raw_flooded, list):
            raise EvacuationNetworkError(
                f"路口 {node_id} 的 flooded_intervals 必须是数组")
        flooded = _merge_intervals([
            _parse_interval_pair(item, f"路口 {node_id} 的受淹区间", start, end)
            for item in raw_flooded
        ])
        intersections[node_id] = Intersection(node_id, can_wait, flooded)

    raw_roads = data["roads"]
    if not isinstance(raw_roads, list):
        raise EvacuationNetworkError("roads 必须是数组")
    roads: dict[str, Road] = {}
    for index, raw in enumerate(raw_roads):
        rctx = f"{ctx}第 {index} 条道路"
        if not isinstance(raw, dict):
            raise EvacuationNetworkError(f"{rctx} 必须是对象")
        required = {"road_id", "from_node", "to_node",
                    "travel_minutes", "max_depth_m"}
        unknown_keys = sorted(set(raw.keys()) - required)
        if unknown_keys:
            raise EvacuationNetworkError(f"{rctx} 含未知字段: {unknown_keys}")
        missing = sorted(required - set(raw.keys()))
        if missing:
            raise EvacuationNetworkError(f"{rctx} 缺少字段: {missing}")
        road_id = _require_id(raw["road_id"], f"{rctx} 的 road_id")
        if road_id in roads:
            raise EvacuationNetworkError(
                f"道路编号重复: {road_id!r}，整体拒绝")
        from_node = _require_id(raw["from_node"], f"道路 {road_id} 的 from_node")
        to_node = _require_id(raw["to_node"], f"道路 {road_id} 的 to_node")
        for endpoint, node in (("from_node", from_node), ("to_node", to_node)):
            if node not in intersections:
                raise EvacuationNetworkError(
                    f"道路 {road_id} 的 {endpoint} 引用了不存在的路口 "
                    f"{node!r}，整体拒绝")
        travel = _require_int(raw["travel_minutes"], f"道路 {road_id} 的行驶分钟")
        if travel <= 0:
            raise EvacuationNetworkError(
                f"道路 {road_id} 的行驶分钟必须是正整数: {travel}，整体拒绝")
        max_depth = _to_depth(raw["max_depth_m"], f"道路 {road_id} 的 max_depth_m")
        roads[road_id] = Road(road_id, from_node, to_node, travel, max_depth)

    raw_shelters = data.get("shelters", [])
    if not isinstance(raw_shelters, list):
        raise EvacuationNetworkError("shelters 必须是数组")
    shelters: dict[str, Shelter] = {}
    for index, raw in enumerate(raw_shelters):
        sctx = f"{ctx}第 {index} 个避险点"
        if not isinstance(raw, dict):
            raise EvacuationNetworkError(f"{sctx} 必须是对象")
        required = {"shelter_id", "node_id", "open_windows"}
        unknown_keys = sorted(set(raw.keys()) - required)
        if unknown_keys:
            raise EvacuationNetworkError(f"{sctx} 含未知字段: {unknown_keys}")
        missing = sorted(required - set(raw.keys()))
        if missing:
            raise EvacuationNetworkError(f"{sctx} 缺少字段: {missing}")
        shelter_id = _require_id(raw["shelter_id"], f"{sctx} 的 shelter_id")
        if shelter_id in shelters:
            raise EvacuationNetworkError(
                f"避险点编号重复: {shelter_id!r}，整体拒绝")
        node_id = _require_id(raw["node_id"], f"避险点 {shelter_id} 的 node_id")
        if node_id not in intersections:
            raise EvacuationNetworkError(
                f"避险点 {shelter_id} 挂在不存在的路口 {node_id!r}，整体拒绝")
        raw_windows = raw["open_windows"]
        if not isinstance(raw_windows, list) or not raw_windows:
            raise EvacuationNetworkError(
                f"避险点 {shelter_id} 的 open_windows 必须是非空数组")
        windows = _merge_intervals([
            _parse_interval_pair(item, f"避险点 {shelter_id} 的开放窗口",
                                 start, end)
            for item in raw_windows
        ])
        shelters[shelter_id] = Shelter(shelter_id, node_id, windows)

    out_roads: dict[str, list[Road]] = {node: [] for node in intersections}
    for road in roads.values():
        out_roads[road.from_node].append(road)
    shelters_by_node: dict[str, list[Shelter]] = {}
    for shelter in shelters.values():
        shelters_by_node.setdefault(shelter.node_id, []).append(shelter)

    return Network(
        analysis_start=start,
        analysis_end=end,
        intersections=intersections,
        roads=roads,
        shelters=shelters,
        sorted_node_ids=tuple(sorted(intersections)),
        out_roads={node: tuple(sorted(rs, key=lambda r: r.road_id))
                   for node, rs in out_roads.items()},
        shelters_by_node={node: tuple(sorted(ss, key=lambda s: s.shelter_id))
                          for node, ss in shelters_by_node.items()},
    )


# --------------------------------------------------------------------------- #
# 积水路况装载（冲突或非法整体拒绝）
# --------------------------------------------------------------------------- #
def load_conditions(path: str | Path, network: Network) -> Conditions:
    try:
        fh = open(path, "r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise EvacuationConditionsError(f"无法读取积水文件 {path}: {exc}") from exc

    segments: dict[str, list[Segment]] = {}
    record_count = 0
    with fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise EvacuationConditionsError("积水 CSV 为空，缺少表头")
        header = [name.strip() for name in reader.fieldnames]
        missing = [c for c in REQUIRED_CSV_COLUMNS if c not in header]
        if missing:
            raise EvacuationConditionsError(f"积水 CSV 缺少必填列: {missing}")
        extra = sorted(set(header) - set(REQUIRED_CSV_COLUMNS))
        if extra:
            raise EvacuationConditionsError(f"积水 CSV 含未知列: {extra}")

        for row in reader:
            line_no = reader.line_num
            if None in row or any(v is None for v in row.values()):
                raise EvacuationConditionsError(
                    f"第 {line_no} 行列数与表头不一致，整体拒绝")
            if all((v or "").strip() == "" for v in row.values()):
                continue  # 完全空白行：无记录内容，跳过
            record_count += 1
            rctx = f"第 {line_no} 行"
            road_id = (row[CSV_ROAD_COL] or "").strip()
            if not road_id:
                raise EvacuationConditionsError(f"{rctx} 道路编号为空，整体拒绝")
            if road_id not in network.roads:
                raise EvacuationConditionsError(
                    f"{rctx} 引用了路网中不存在的道路 {road_id!r}，整体拒绝")
            begin = _parse_csv_minute(row[CSV_BEGIN_COL], f"{rctx} 的 begin")
            end = _parse_csv_minute(row[CSV_END_COL], f"{rctx} 的 end")
            if begin >= end:
                raise EvacuationConditionsError(
                    f"{rctx} 区间为空: [{begin}, {end})，整体拒绝")
            if begin < network.analysis_start or end > network.analysis_end:
                raise EvacuationConditionsError(
                    f"{rctx} 片段 [{begin}, {end}) 越出分析范围 "
                    f"[{network.analysis_start}, {network.analysis_end}]，整体拒绝")
            depth = _parse_csv_depth(
                (row[CSV_DEPTH_COL] or "").strip(), f"{rctx} 的 depth_m")
            segments.setdefault(road_id, []).append(Segment(begin, end, depth))

    merged: dict[str, tuple[Segment, ...]] = {}
    merged_count = 0
    for road_id, segs in segments.items():
        merged[road_id], n_merged = _merge_segments(road_id, segs)
        merged_count += n_merged
    return Conditions(segments=merged, record_count=record_count,
                      merged_record_count=merged_count)


def _parse_csv_minute(text: str, context: str) -> int:
    text = (text or "").strip()
    try:
        return int(text)
    except ValueError:
        raise EvacuationConditionsError(
            f"{context} 必须是整数分钟: {text!r}，整体拒绝") from None


def _parse_csv_depth(text: str, context: str) -> Decimal | None:
    if text == "":
        return None  # 空值 = 未知
    try:
        depth = Decimal(text)
    except InvalidOperation:
        raise EvacuationConditionsError(
            f"{context} 不是合法数值: {text!r}，整体拒绝") from None
    if not depth.is_finite():
        raise EvacuationConditionsError(
            f"{context} 必须是非负有限数值或空值: {text!r}，整体拒绝")
    if depth < 0:
        raise EvacuationConditionsError(
            f"{context} 不能为负: {text}，整体拒绝")
    return depth


def _merge_segments(road_id: str,
                    segs: list[Segment]) -> tuple[tuple[Segment, ...], int]:
    """归并同一条道路的片段：重叠部分只有深度含义相同才可合并。"""
    merged: list[Segment] = []
    n_merged = 0
    for seg in sorted(segs, key=lambda s: (s.begin, s.end)):
        if merged and seg.begin < merged[-1].end:
            prev = merged[-1]
            if prev.depth == seg.depth:  # 都已知且相等，或都未知
                merged[-1] = Segment(prev.begin, max(prev.end, seg.end),
                                     prev.depth)
                n_merged += 1
                continue
            raise EvacuationConditionsError(
                f"道路 {road_id} 的片段 [{prev.begin}, {prev.end}) 与 "
                f"[{seg.begin}, {seg.end}) 重叠但深度含义冲突 "
                f"({_depth_text(prev.depth)} vs {_depth_text(seg.depth)})，整体拒绝")
        merged.append(seg)
    return tuple(merged), n_merged


def _depth_text(depth: Decimal | None) -> str:
    return "未知" if depth is None else str(depth)


# --------------------------------------------------------------------------- #
# 查询装载与逐请求处理
# --------------------------------------------------------------------------- #
def load_queries(path: str | Path) -> list:
    """读取查询 JSON；整体结构非法抛 EvacuationQueryError。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise EvacuationQueryError(f"查询文件不是合法 JSON: {exc}") from exc
    except OSError as exc:
        raise EvacuationQueryError(f"无法读取查询文件 {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data.keys()) != {"requests"}:
        raise EvacuationQueryError('查询文件顶层必须是且只含 "requests" 的对象')
    if not isinstance(data["requests"], list):
        raise EvacuationQueryError('"requests" 必须是数组')
    return data["requests"]


_REQUEST_KEYS = {"request_id", "origin", "depart_minute", "deadline_minute"}


def _invalid(index: int, request_id: object, reason: str) -> dict:
    return {"request_index": index, "request_id": request_id,
            "status": "invalid_request", "reason": reason}


def process_request(network: Network, conditions: Conditions,
                    raw: object, index: int, seen_ids: set[str]) -> dict:
    """校验并执行单个请求；任何请求级问题都只影响本请求。"""
    if not isinstance(raw, dict):
        return _invalid(index, None, f"请求必须是对象，得到: {raw!r}")
    unknown_keys = sorted(set(raw.keys()) - _REQUEST_KEYS)
    if unknown_keys:
        return _invalid(index, raw.get("request_id"),
                        f"请求含未知字段: {unknown_keys}")
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        return _invalid(index, request_id, "request_id 必须是非空字符串")
    if request_id in seen_ids:
        return _invalid(index, request_id, f"request_id 重复: {request_id!r}")
    seen_ids.add(request_id)

    origin = raw.get("origin")
    if not isinstance(origin, str) or origin not in network.intersections:
        return _invalid(index, request_id,
                        f"起点路口不存在于路网: {origin!r}")
    depart = raw.get("depart_minute")
    deadline = raw.get("deadline_minute")
    if isinstance(depart, bool) or not isinstance(depart, int):
        return _invalid(index, request_id,
                        f"depart_minute 必须是整数分钟: {depart!r}")
    if isinstance(deadline, bool) or not isinstance(deadline, int):
        return _invalid(index, request_id,
                        f"deadline_minute 必须是整数分钟: {deadline!r}")
    if not (network.analysis_start <= depart <= network.analysis_end):
        return _invalid(index, request_id,
                        f"出发分钟 {depart} 越出分析范围 "
                        f"[{network.analysis_start}, {network.analysis_end}]")
    if not (network.analysis_start <= deadline <= network.analysis_end):
        return _invalid(index, request_id,
                        f"截止分钟 {deadline} 越出分析范围 "
                        f"[{network.analysis_start}, {network.analysis_end}]")
    if depart > deadline:
        return _invalid(index, request_id,
                        f"出发分钟 {depart} 晚于截止分钟 {deadline}")
    node = network.intersections[origin]
    if node.flooded_at(depart):
        return _invalid(index, request_id,
                        f"起点路口 {origin} 在出发分钟 {depart} 已受淹，"
                        "拒绝把车辆凭空放到其他位置")

    plan, diagnostics = search(network, conditions, origin, depart, deadline)
    if plan is None:
        return {"request_index": index, "request_id": request_id,
                "status": "unreachable",
                "search": diagnostics.to_search_dict(origin, depart, deadline)}
    return {"request_index": index, "request_id": request_id,
            "origin": origin, **plan}


def run_batch(network: Network, conditions: Conditions,
              raw_requests: list) -> list[dict]:
    """逐请求独立执行；单个请求无效不影响其他请求。"""
    seen_ids: set[str] = set()
    return [process_request(network, conditions, raw, index, seen_ids)
            for index, raw in enumerate(raw_requests)]


# --------------------------------------------------------------------------- #
# 搜索：状态 = (路口, 分钟)，按分钟递增的动态规划
# --------------------------------------------------------------------------- #
class Diagnostics:
    """搜索过程遇到的阻塞原因（计数精确、示例限量、顺序确定）。"""

    def __init__(self) -> None:
        self.counts = {category: 0 for category in BLOCKER_CATEGORIES}
        self.examples: dict[str, list] = {c: [] for c in BLOCKER_CATEGORIES}
        self.states_expanded = 0
        self.nodes_reached: set[str] = set()

    def record(self, category: str, example: dict) -> None:
        self.counts[category] += 1
        if len(self.examples[category]) < MAX_BLOCKER_EXAMPLES:
            self.examples[category].append(example)

    def to_search_dict(self, origin: str, depart: int, deadline: int) -> dict:
        return {
            "origin": origin,
            "depart_minute": depart,
            "deadline_minute": deadline,
            "states_expanded": self.states_expanded,
            "nodes_reached": sorted(self.nodes_reached),
            "blockers": {
                category: {"count": self.counts[category],
                           "examples": self.examples[category]}
                for category in BLOCKER_CATEGORIES if self.counts[category]
            },
        }


# 一个状态的费用：(累计等待分钟, 道路编号序列, 行程步骤)
# 行程步骤: ("drive", road_id, enter, exit) 或 ("wait", node_id, minute)
_Cost = tuple[int, tuple[str, ...], tuple]


def _relax(best: dict, state: tuple[str, int], candidate: _Cost) -> None:
    current = best.get(state)
    if current is None or candidate[:2] < current[:2]:
        best[state] = candidate


def search(network: Network, conditions: Conditions,
           origin: str, depart: int, deadline: int):
    """精确搜索最优可执行行程。

    返回 ``(plan | None, diagnostics)``。所有道路耗时为正、状态分钟不超
    过截止分钟，按分钟递增单趟处理即终止。每个状态只保留字典序最小的
    (累计等待, 道路序列)：同一 (路口, 分钟) 下等待相同的两条路径行驶
    分钟数必相同，序列不可能互为前缀，因此该支配规则是精确的。
    """
    diag = Diagnostics()
    best: dict[tuple[str, int], _Cost] = {(origin, depart): (0, (), ())}
    goals = []  # (抵达分钟, 等待, 道路序列, shelter_id, 步骤)

    for minute in range(depart, deadline + 1):
        for node_id in network.sorted_node_ids:
            entry = best.get((node_id, minute))
            if entry is None:
                continue
            diag.states_expanded += 1
            diag.nodes_reached.add(node_id)
            wait_so_far, road_seq, steps = entry
            node = network.intersections[node_id]

            shelters_here = network.shelters_by_node.get(node_id, ())
            covering = [s for s in shelters_here if s.open_at(minute)]
            if covering:
                shelter = min(covering, key=lambda s: s.shelter_id)
                goals.append((minute, wait_so_far, road_seq,
                              shelter.shelter_id, steps))
            elif shelters_here:
                diag.record("shelter_not_open", {
                    "node_id": node_id, "minute": minute,
                    "shelter_ids": [s.shelter_id for s in shelters_here]})

            # 等待一分钟：要求允许等待，且这一整分钟与结束时刻都安全
            if minute + 1 <= deadline:
                if not node.can_wait:
                    diag.record("waiting_not_allowed",
                                {"node_id": node_id, "minute": minute})
                elif node.flooded_at(minute + 1):
                    diag.record("wait_blocked_by_flood",
                                {"node_id": node_id, "minute": minute})
                else:
                    _relax(best, (node_id, minute + 1),
                           (wait_so_far + 1, road_seq,
                            steps + (("wait", node_id, minute),)))

            # 沿出向道路行驶：整个 [enter, exit) 必须已知且合格
            for road in network.out_roads[node_id]:
                arrive = minute + road.travel_minutes
                if arrive > deadline:
                    diag.record("deadline_exceeded", {
                        "road_id": road.road_id, "enter_minute": minute,
                        "would_arrive_minute": arrive})
                    continue
                status, detail = conditions.road_status(road, minute, arrive)
                if status == "depth_exceeded":
                    diag.record("depth_exceeded", {
                        "road_id": road.road_id, "enter_minute": minute,
                        "exit_minute": arrive,
                        "depth_m": str(detail),
                        "limit_m": str(road.max_depth_m)})
                    continue
                if status == "unknown":
                    diag.record("unknown_conditions", {
                        "road_id": road.road_id, "enter_minute": minute,
                        "exit_minute": arrive})
                    continue
                if network.intersections[road.to_node].flooded_at(arrive):
                    diag.record("arrival_node_flooded", {
                        "node_id": road.to_node, "minute": arrive,
                        "road_id": road.road_id})
                    continue
                _relax(best, (road.to_node, arrive),
                       (wait_so_far, road_seq + (road.road_id,),
                        steps + (("drive", road.road_id, minute, arrive),)))

    if not goals:
        return None, diag
    arrival, wait, road_seq, shelter_id, steps = min(
        goals, key=lambda g: (g[0], g[1], g[2], g[3]))
    shelter = network.shelters[shelter_id]
    plan = {
        "status": "reachable",
        "shelter_id": shelter_id,
        "shelter_node": shelter.node_id,
        "origin": origin,
        "depart_minute": depart,
        "arrival_minute": arrival,
        "total_wait_minutes": wait,
        "road_ids": list(road_seq),
        "itinerary": _build_itinerary(steps, network),
    }
    return plan, diag


def _build_itinerary(steps: tuple, network: Network) -> list[dict]:
    """把逐分钟步骤整理成行程：连续等待合并为一段。"""
    itinerary: list[dict] = []
    index = 0
    while index < len(steps):
        step = steps[index]
        if step[0] == "drive":
            _, road_id, enter, exit_ = step
            road = network.roads[road_id]
            itinerary.append({
                "type": "drive", "road_id": road_id,
                "from_node": road.from_node, "to_node": road.to_node,
                "enter_minute": enter, "exit_minute": exit_})
            index += 1
        else:
            _, node_id, start = step
            end_index = index
            while end_index < len(steps) and steps[end_index][0] == "wait" \
                    and steps[end_index][1] == node_id:
                end_index += 1
            end = steps[end_index - 1][2] + 1
            itinerary.append({
                "type": "wait", "node_id": node_id,
                "start_minute": start, "end_minute": end,
                "minutes": end - start})
            index = end_index
    return itinerary


# --------------------------------------------------------------------------- #
# 报告与端到端入口
# --------------------------------------------------------------------------- #
def build_report(network_path: str | Path, conditions_path: str | Path,
                 queries_path: str | Path, network: Network,
                 conditions: Conditions, results: list[dict]) -> dict:
    counts = {"reachable": 0, "unreachable": 0, "invalid_request": 0}
    for result in results:
        counts[result["status"]] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "evacuation-access",
        "inputs": {
            "network_json": {"path": str(network_path),
                             "sha256": sha256_file(network_path)},
            "conditions_csv": {"path": str(conditions_path),
                               "sha256": sha256_file(conditions_path)},
            "queries_json": {"path": str(queries_path),
                             "sha256": sha256_file(queries_path)},
        },
        "parameters": {
            "analysis_start_minute": network.analysis_start,
            "analysis_end_minute": network.analysis_end,
        },
        "network_summary": {
            "intersections": len(network.intersections),
            "roads": len(network.roads),
            "shelters": len(network.shelters),
            "condition_records": conditions.record_count,
            "merged_condition_records": conditions.merged_record_count,
            "condition_segments": sum(
                len(segs) for segs in conditions.segments.values()),
        },
        "results": results,
        "summary": {"requests": len(results), **counts},
    }


def run_evacuation(network_path: str | Path, conditions_path: str | Path,
                   queries_path: str | Path) -> dict:
    """端到端：装载三份输入（非法即整体拒绝）并逐请求执行。"""
    network = load_network(network_path)
    conditions = load_conditions(conditions_path, network)
    raw_requests = load_queries(queries_path)
    results = run_batch(network, conditions, raw_requests)
    return build_report(network_path, conditions_path, queries_path,
                        network, conditions, results)


def write_report(report: dict, output_path: str | Path) -> None:
    """报告先写临时文件、回读校验，再原子替换目标。"""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n") \
        .encode("utf-8")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        with open(tmp, "rb") as fh:
            actual = fh.read()
        if actual != payload:
            raise EvacuationOutputError(f"临时文件回读内容与预期不一致: {tmp}")
        os.replace(tmp, target)
    except EvacuationOutputError:
        _quiet_unlink(tmp)
        raise
    except OSError as exc:
        _quiet_unlink(tmp)
        raise EvacuationOutputError(f"写出 {target} 失败: {exc}") from exc


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
