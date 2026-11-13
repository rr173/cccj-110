"""测站水位-流量率定关系换算（纯本地、标准库实现）。

测站组会为同一条河段陆续批准多个**水位-流量率定曲线版本**：旧曲线用于
历史观测，新曲线只从批准时刻起生效。本模块把观测表里的水位按**观测发生
时刻当时有效**的曲线版本换算为流量，并在输出里逐行保留推导依据。

核心口径
--------
* 时间：观测时刻必须带时区偏移，统一换算 UTC 后按 (站号, UTC 时刻) 排序；
  曲线版本的有效期是半开区间 ``[effective_from, effective_to)``，同一测站
  的任何两个版本不得有非零长度的交叠，首尾相接（旧版失效时刻 = 新版生效
  时刻）是合法交接。观测时刻恰好等于新版生效时刻时使用新版。
* 回放：每个测站**独立**回放，从配置的 ``initial_branch`` 开始。只有观测
  水位与**上一条成功换算**水位之差严格高于死区上限（转涨水）或严格低于
  死区下限（转退水）才改变分支；差值落在死区闭区间内（包括恰好碰到边界）
  继续使用当前分支。某个站的回放状态绝不读取或改变另一个站的状态。
* 无效观测：当时没有适用版本、或水位超出所选分支曲线覆盖范围时，该行仍
  写出并注明原因；**不外推**，也**不更新**该站的上一条成功水位与分支，
  后续有效行仍按上一条成功结果选分支。
* 插值：在包围该水位的两个节点间线性换算；水位恰好等于节点时直接使用
  节点流量。节点水位必须严格递增，流量必须有限、非负且随水位不下降。
* 合并：同站同一 UTC 时刻、水位完全相同的重复观测合并为一条计算，保留
  全部原始物理行号；同站同刻水位不同是输入歧义，整批拒绝。
* 整批拒绝：登记格式错误、节点顺序/流量规则不合法、有效期交叠、观测
  引用未登记站号、缺时区、坏数值、同刻冲突等都不产出结果，以非零码结束。

换算结果只是**率定关系推算值**，不是流速仪/水工建筑物等直接测得的流量。
"""
from __future__ import annotations

import csv
import io
import json
import os
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from pathlib import Path

from .hashing import sha256_file

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
CSV_STATION_COL = "station_id"
CSV_TIME_COL = "observed_at"
CSV_STAGE_COL = "stage_m"
REQUIRED_CSV_COLUMNS = (CSV_STATION_COL, CSV_TIME_COL, CSV_STAGE_COL)

BRANCH_RISING = "rising"
BRANCH_FALLING = "falling"
BRANCHES = (BRANCH_RISING, BRANCH_FALLING)
_BRANCH_ALIASES = {
    "rising": BRANCH_RISING, "rise": BRANCH_RISING,
    "falling": BRANCH_FALLING, "fall": BRANCH_FALLING,
    "涨水": BRANCH_RISING, "退水": BRANCH_FALLING,
}

STATUS_CONVERTED = "converted"
STATUS_NOT_CONVERTED = "not_converted"

REASON_NO_CURVE = "no_effective_curve"
REASON_OUT_OF_RANGE = "stage_out_of_curve_range"
REASONS = (REASON_NO_CURVE, REASON_OUT_OF_RANGE)

METHOD_NODE = "node_exact"
METHOD_INTERPOLATION = "linear_interpolation"

DECISION_INITIAL = "initial"
DECISION_WITHIN_DEADBAND = "keep_within_deadband"
DECISION_TO_RISING = "switch_to_rising"
DECISION_TO_FALLING = "switch_to_falling"
DECISION_NO_CURVE = "no_curve"

RESULTS_FILENAME = "stage_discharge_results.csv"
SUMMARY_FILENAME = "stage_discharge_summary.json"

SCHEMA_VERSION = 1

#: 输出层的小数位。只影响呈现，不参与分支、范围或插值判定。
OUTPUT_DECIMALS = 6

#: 插值内部精度：十进制运算保留足够有效位，最后才按输出位数呈现。
_INTERPOLATION_PRECISION = 50

RESULT_HEADER = [
    "station_id", "observed_at_utc", "stage_m", "source_lines",
    "record_count", "status", "discharge_m3_s", "method", "reason",
    "curve_id", "revision", "curve_effective_from_utc",
    "curve_effective_to_utc", "branch", "branch_decision",
    "previous_stage_m", "stage_delta_m", "curve_stage_min_m",
    "curve_stage_max_m", "lower_stage_m", "upper_stage_m",
    "lower_discharge_m3_s", "upper_discharge_m3_s",
    "interpolation_fraction", "calculation", "state_applied",
]


class StageDischargeRegistryError(ValueError):
    """率定登记文件非法（结构、节点、有效期、死区等）。"""


class StageDischargeInputError(ValueError):
    """观测 CSV 无法处理（缺时区、未知站号、同刻冲突等），整批拒绝。"""


class StageDischargeOutputError(RuntimeError):
    """写出或回读校验失败。"""


# --------------------------------------------------------------------------- #
# 时间与 Decimal 工具
# --------------------------------------------------------------------------- #
def _parse_time(value: object, *, context: str) -> datetime:
    """解析带时区的 ISO 8601 时间并换算 UTC；缺时区拒绝猜测。"""
    if not isinstance(value, str):
        raise StageDischargeInputError(f"{context} 时间必须是字符串: {value!r}")
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise StageDischargeInputError(f"{context} 时间无法解析: {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise StageDischargeInputError(
            f"{context} 缺少时区偏移，拒绝猜测: {value!r}")
    return dt.astimezone(timezone.utc)


def _parse_registry_time(value: object, *, context: str,
                         allow_none: bool = False) -> datetime | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise StageDischargeRegistryError(
            f"{context} 必须是带时区的时间字符串: {value!r}")
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise StageDischargeRegistryError(
            f"{context} 时间无法解析: {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise StageDischargeRegistryError(
            f"{context} 缺少时区偏移，拒绝猜测: {value!r}")
    return dt.astimezone(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_decimal(value: object, *, context: str) -> Decimal:
    """把 JSON/CSV 数值转成有限 Decimal；布尔、NaN/Inf 一律非法。"""
    if isinstance(value, bool):
        raise StageDischargeInputError(f"{context} 必须是数字: {value!r}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        # 正常 JSON 数字已由 parse_float=Decimal 处理；这里只作防御。
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise StageDischargeInputError(
                f"{context} 不是合法数字: {value!r}") from exc
    else:
        raise StageDischargeInputError(f"{context} 必须是数字: {value!r}")
    if not result.is_finite():
        raise StageDischargeInputError(f"{context} 必须是有限数: {value!r}")
    return result


def _as_registry_decimal(value: object, *, context: str) -> Decimal:
    try:
        return _as_decimal(value, context=context)
    except StageDischargeInputError as exc:
        raise StageDischargeRegistryError(str(exc)) from exc


def _format_decimal(value: Decimal | None,
                    places: int = OUTPUT_DECIMALS) -> str:
    """稳定、去尾零的小数呈现；不参与任何业务判定。

    常规量按 ``places`` 位小数四舍五入。若一个非零量在该精度下会变成 0
    （例如极小的插值比例），改用 ``places`` 位有效数字，避免把有意义的小量
    抹成 0。负数零统一呈现为 0。
    """
    if value is None:
        return ""
    if value.is_zero():
        return "0"
    with localcontext() as ctx:
        ctx.prec = max(_INTERPOLATION_PRECISION, places + 10)
        quantum = Decimal(1).scaleb(-places)
        rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
        if rounded.is_zero():
            # adjusted() 是最高有效位的指数；保留 places 位有效数字。
            quantum = Decimal(1).scaleb(value.adjusted() - places + 1)
            rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
        if text in ("", "-"):
            return "0"
    return text


def _ceil_interval_end(dt: datetime | None,
                       fallback: datetime = datetime.max.replace(
                           tzinfo=timezone.utc)) -> datetime:
    return fallback if dt is None else dt


# --------------------------------------------------------------------------- #
# 登记数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CurveNodes:
    """单条分支的节点序列：水位严格递增，流量有限、非负、不下降。"""

    stages: tuple[Decimal, ...]
    discharges: tuple[Decimal, ...]

    @property
    def stage_min(self) -> Decimal:
        return self.stages[0]

    @property
    def stage_max(self) -> Decimal:
        return self.stages[-1]


@dataclass(frozen=True)
class CurveVersion:
    """一个曲线版本：编号、修订号、半开有效期与两套分支节点。"""

    curve_id: str
    revision: int
    effective_from: datetime
    effective_to: datetime | None
    rising: CurveNodes
    falling: CurveNodes

    def covers(self, observed_at: datetime) -> bool:
        return (self.effective_from <= observed_at
                and (self.effective_to is None
                     or observed_at < self.effective_to))

    def nodes_for(self, branch: str) -> CurveNodes:
        return self.rising if branch == BRANCH_RISING else self.falling


@dataclass(frozen=True)
class Deadband:
    """闭区间死区 [lower, upper]；差值严格越界才换分支。"""

    lower: Decimal
    upper: Decimal


@dataclass(frozen=True)
class StationRegistry:
    """单个测站的登记：死区、初始分支与按生效时刻排序的曲线版本。"""

    station_id: str
    deadband: Deadband
    initial_branch: str
    curves: tuple[CurveVersion, ...]

    def version_at(self, observed_at: datetime) -> CurveVersion | None:
        starts = tuple(c.effective_from for c in self.curves)
        index = bisect_right(starts, observed_at) - 1
        if index < 0:
            return None
        candidate = self.curves[index]
        return candidate if candidate.covers(observed_at) else None


# --------------------------------------------------------------------------- #
# 登记文件解析
# --------------------------------------------------------------------------- #
_TOP_LEVEL_KEYS = {"schema_version", "stations"}
_STATION_KEYS = {"deadband_m", "deadband", "initial_branch", "curves"}
_CURVE_KEYS = {"curve_id", "revision", "effective_from", "effective_to",
               "rising", "falling"}
_DEADBAND_OBJECT_KEYS = {
    "lower_m": "lower", "upper_m": "upper", "lower": "lower", "upper": "upper",
}


def _json_object_no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise StageDischargeRegistryError(
                f"登记 JSON 含重复键 {key!r}，拒绝猜测应取哪一个")
        obj[key] = value
    return obj


def _reject_json_constant(text: str) -> object:
    raise StageDischargeRegistryError(
        f"登记 JSON 含非法数字常量 {text!r}（NaN/Infinity 不接受）")


def _load_registry_json(path: Path) -> object:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(
                fh, parse_float=Decimal, parse_int=int,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_json_object_no_duplicates)
    except StageDischargeRegistryError:
        raise
    except json.JSONDecodeError as exc:
        raise StageDischargeRegistryError(
            f"登记文件不是合法 JSON: {exc}") from exc
    except OSError as exc:
        raise StageDischargeRegistryError(
            f"无法读取登记文件 {path}: {exc}") from exc


def _reject_unknown_keys(raw: dict, allowed: set[str], context: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise StageDischargeRegistryError(f"{context} 含未知字段: {unknown}")


def _parse_branch(value: object, *, context: str) -> str:
    if not isinstance(value, str):
        raise StageDischargeRegistryError(f"{context} 必须是非空字符串")
    canonical = _BRANCH_ALIASES.get(value.strip().lower())
    if canonical is None:
        raise StageDischargeRegistryError(
            f"{context} 必须是 rising/falling（涨水/退水）: {value!r}")
    return canonical


def _parse_deadband(raw: dict, context: str) -> Deadband:
    if "deadband_m" in raw and "deadband" in raw:
        raise StageDischargeRegistryError(
            f"{context} 不能同时给出 deadband_m 与 deadband")
    if "deadband_m" in raw:
        value = raw["deadband_m"]
        if isinstance(value, dict):
            return _parse_deadband_object(value, f"{context} 的 deadband_m")
        limit = _as_registry_decimal(value, context=f"{context} 的 deadband_m")
        if limit < 0:
            raise StageDischargeRegistryError(
                f"{context} 的 deadband_m 必须是非负数")
        return Deadband(lower=-limit, upper=limit)
    if "deadband" in raw:
        return _parse_deadband_object(raw["deadband"], f"{context} 的 deadband")
    raise StageDischargeRegistryError(
        f"{context} 缺少必填字段 deadband_m（或 deadband 对象）")


def _parse_deadband_object(value: object, context: str) -> Deadband:
    if not isinstance(value, dict):
        raise StageDischargeRegistryError(
            f"{context} 必须是数字或含 lower_m/upper_m 的对象")
    _reject_unknown_keys(value, set(_DEADBAND_OBJECT_KEYS), context)
    if "lower_m" not in value or "upper_m" not in value:
        raise StageDischargeRegistryError(
            f"{context} 必须同时给出 lower_m 与 upper_m")
    lower = _as_registry_decimal(value["lower_m"], context=f"{context} 的 lower_m")
    upper = _as_registry_decimal(value["upper_m"], context=f"{context} 的 upper_m")
    if lower > upper:
        raise StageDischargeRegistryError(
            f"{context} 的下限 {lower} 不得高于上限 {upper}")
    if lower > 0 or upper < 0:
        raise StageDischargeRegistryError(
            f"{context} 必须覆盖 0（lower_m <= 0 <= upper_m），"
            "否则“保持当前分支”的区间不是死区")
    return Deadband(lower=lower, upper=upper)


def _parse_node(raw: object, *, context: str) -> tuple[Decimal, Decimal]:
    if isinstance(raw, dict):
        allowed = {"stage_m", "discharge_m3_s", "stage", "discharge"}
        _reject_unknown_keys(raw, allowed, context)
        if "stage_m" in raw:
            stage_raw = raw["stage_m"]
        elif "stage" in raw:
            stage_raw = raw["stage"]
        else:
            raise StageDischargeRegistryError(f"{context} 缺少 stage_m")
        if "discharge_m3_s" in raw:
            discharge_raw = raw["discharge_m3_s"]
        elif "discharge" in raw:
            discharge_raw = raw["discharge"]
        else:
            raise StageDischargeRegistryError(
                f"{context} 缺少 discharge_m3_s")
    elif isinstance(raw, (list, tuple)):
        if len(raw) != 2:
            raise StageDischargeRegistryError(
                f"{context} 数组节点必须恰为 [水位, 流量] 两个元素")
        stage_raw, discharge_raw = raw
    else:
        raise StageDischargeRegistryError(
            f"{context} 必须是含 stage_m/discharge_m3_s 的对象，"
            "或 [水位, 流量] 数组")
    stage = _as_registry_decimal(stage_raw, context=f"{context} 的水位")
    discharge = _as_registry_decimal(
        discharge_raw, context=f"{context} 的流量")
    if discharge < 0:
        raise StageDischargeRegistryError(
            f"{context} 的流量必须非负: {discharge}")
    return stage, discharge


def _parse_nodes(raw: object, *, context: str) -> CurveNodes:
    if not isinstance(raw, list) or not raw:
        raise StageDischargeRegistryError(
            f"{context} 必须是非空节点数组")
    stages: list[Decimal] = []
    discharges: list[Decimal] = []
    for index, item in enumerate(raw):
        stage, discharge = _parse_node(item, context=f"{context} 第 {index} 个节点")
        if stages and stage <= stages[-1]:
            raise StageDischargeRegistryError(
                f"{context} 第 {index} 个节点水位 {stage} 未严格递增"
                f"（前一个为 {stages[-1]}）；同水位节点会产生歧义")
        if discharges and discharge < discharges[-1]:
            raise StageDischargeRegistryError(
                f"{context} 第 {index} 个节点流量 {discharge} 低于前一个 "
                f"{discharges[-1]}；节点流量必须随水位不下降")
        stages.append(stage)
        discharges.append(discharge)
    return CurveNodes(stages=tuple(stages), discharges=tuple(discharges))


def _parse_curve(raw: object, *, context: str, station_id: str) -> CurveVersion:
    if not isinstance(raw, dict):
        raise StageDischargeRegistryError(f"{context} 必须是对象")
    _reject_unknown_keys(raw, _CURVE_KEYS, context)
    for key in ("curve_id", "revision", "effective_from", "rising", "falling"):
        if key not in raw:
            raise StageDischargeRegistryError(f"{context} 缺少必填字段 {key}")
    curve_id = raw["curve_id"]
    if not isinstance(curve_id, str) or not curve_id.strip():
        raise StageDischargeRegistryError(f"{context} 的 curve_id 必须是非空字符串")
    curve_id = curve_id.strip()
    revision = raw["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise StageDischargeRegistryError(
            f"{context} 的 revision 必须是 >=0 的整数")
    effective_from = _parse_registry_time(
        raw["effective_from"], context=f"{context} 的 effective_from")
    effective_to = _parse_registry_time(
        raw.get("effective_to"), context=f"{context} 的 effective_to",
        allow_none=True)
    if effective_to is not None and effective_to <= effective_from:
        raise StageDischargeRegistryError(
            f"{context} 的 effective_to 必须晚于 effective_from"
            f"（半开区间 [{_utc_iso(effective_from)}, "
            f"{_utc_iso(effective_to)}) 否则长度为 0）")
    rising = _parse_nodes(raw["rising"], context=f"{context} ({station_id}) rising")
    falling = _parse_nodes(raw["falling"],
                           context=f"{context} ({station_id}) falling")
    return CurveVersion(
        curve_id=curve_id, revision=revision,
        effective_from=effective_from, effective_to=effective_to,
        rising=rising, falling=falling)


def _parse_station(station_id: str, raw: object) -> StationRegistry:
    context = f"测站 {station_id}"
    if not isinstance(raw, dict):
        raise StageDischargeRegistryError(f"{context} 的配置必须是对象")
    _reject_unknown_keys(raw, _STATION_KEYS, context)
    deadband = _parse_deadband(raw, context)
    if "initial_branch" not in raw:
        raise StageDischargeRegistryError(f"{context} 缺少 initial_branch")
    initial_branch = _parse_branch(
        raw["initial_branch"], context=f"{context} 的 initial_branch")
    raw_curves = raw.get("curves")
    if not isinstance(raw_curves, list) or not raw_curves:
        raise StageDischargeRegistryError(
            f"{context} 的 curves 必须是非空数组（至少一个曲线版本）")

    curves: list[CurveVersion] = []
    seen_ids: set[str] = set()
    for index, raw_curve in enumerate(raw_curves):
        curve = _parse_curve(raw_curve, context=f"{context} 第 {index} 个曲线版本",
                             station_id=station_id)
        if curve.curve_id in seen_ids:
            raise StageDischargeRegistryError(
                f"{context} 的 curve_id {curve.curve_id!r} 重复；"
                "同一测站每个版本必须有唯一编号")
        seen_ids.add(curve.curve_id)
        curves.append(curve)

    curves.sort(key=lambda c: (c.effective_from,
                               _ceil_interval_end(c.effective_to),
                               c.curve_id))
    for previous, current in zip(curves, curves[1:]):
        if previous.effective_to is None or previous.effective_to > current.effective_from:
            end_text = ("未设失效时刻（开放式）" if previous.effective_to is None
                        else _utc_iso(previous.effective_to))
            raise StageDischargeRegistryError(
                f"{context} 的曲线版本 {previous.curve_id!r} 与 "
                f"{current.curve_id!r} 有效期交叠：前者的结束 {end_text} 晚于"
                f" 后者的生效 {_utc_iso(current.effective_from)}")
    return StationRegistry(
        station_id=station_id, deadband=deadband,
        initial_branch=initial_branch, curves=tuple(curves))


def load_registry(path: str | Path) -> dict[str, StationRegistry]:
    """读取并校验率定登记 JSON；任何结构问题整批拒绝。"""
    data = _load_registry_json(Path(path))
    if not isinstance(data, dict):
        raise StageDischargeRegistryError(
            "登记文件顶层必须是对象，且包含 stations 字段")
    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, "登记文件顶层")
    if "schema_version" in data:
        version = data["schema_version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise StageDischargeRegistryError(
                "schema_version 必须是 >=1 的整数")
    raw_stations = data.get("stations")
    if isinstance(raw_stations, dict):
        items = list(raw_stations.items())
    elif isinstance(raw_stations, list):
        items = []
        for index, item in enumerate(raw_stations):
            if not isinstance(item, dict):
                raise StageDischargeRegistryError(
                    f"stations 第 {index} 项必须是对象")
            if "station_id" not in item:
                raise StageDischargeRegistryError(
                    f"stations 第 {index} 项缺少 station_id")
            station_id = item["station_id"]
            if not isinstance(station_id, str) or not station_id.strip():
                raise StageDischargeRegistryError(
                    f"stations 第 {index} 项的 station_id 必须是非空字符串")
            payload = dict(item)
            payload.pop("station_id")
            items.append((station_id.strip(), payload))
    else:
        raise StageDischargeRegistryError(
            "stations 必须是对象（站号 -> 配置）或配置数组")

    registry: dict[str, StationRegistry] = {}
    for station_id, raw in items:
        if not isinstance(station_id, str) or not station_id.strip():
            raise StageDischargeRegistryError("站号必须是非空字符串")
        station_id = station_id.strip()
        if station_id in registry:
            raise StageDischargeRegistryError(f"测站 {station_id!r} 重复登记")
        registry[station_id] = _parse_station(station_id, raw)
    if not registry:
        raise StageDischargeRegistryError("stations 必须至少登记一个测站")
    return registry


# --------------------------------------------------------------------------- #
# 观测读取与合并
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Observation:
    """一条合并后的观测（同站同刻同水位的重复行已折成一条）。"""

    station_id: str
    observed_at: datetime
    stage: Decimal
    source_lines: tuple[int, ...]

    @property
    def record_count(self) -> int:
        return len(self.source_lines)


def _parse_stage(text: str, *, context: str) -> Decimal:
    if text == "":
        raise StageDischargeInputError(f"{context} 水位为空")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise StageDischargeInputError(
            f"{context} 水位不是合法数字: {text!r}") from exc
    if not value.is_finite():
        raise StageDischargeInputError(
            f"{context} 水位必须是有限数: {text!r}")
    return value


def _read_observations(path: Path, registry: dict[str, StationRegistry]
                       ) -> tuple[list[Observation], int, int]:
    """读取观测 CSV，返回 (合并后的观测, 原始记录数, 合并掉的记录数)。"""
    try:
        fh = open(path, "r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise StageDischargeInputError(f"无法读取观测文件 {path}: {exc}") from exc

    raw_count = 0
    merged_count = 0
    keyed: dict[tuple[str, datetime], Observation] = {}
    errors: list[str] = []

    with fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise StageDischargeInputError("观测 CSV 为空，缺少表头")
        header = [name.strip() for name in reader.fieldnames]
        missing = [c for c in REQUIRED_CSV_COLUMNS if c not in header]
        if missing:
            raise StageDischargeInputError(f"观测 CSV 缺少必填列: {missing}")
        extra = sorted(set(header) - set(REQUIRED_CSV_COLUMNS))
        if extra:
            raise StageDischargeInputError(f"观测 CSV 含未知列: {extra}")

        for row in reader:
            line_no = reader.line_num
            if None in row or any(value is None for value in row.values()):
                errors.append(f"第 {line_no} 行列数与表头不一致")
                continue
            if all((value or "").strip() == "" for value in row.values()):
                continue  # 完全空白行：没有观测内容
            raw_count += 1
            station_id = (row[CSV_STATION_COL] or "").strip()
            time_text = (row[CSV_TIME_COL] or "").strip()
            stage_text = (row[CSV_STAGE_COL] or "").strip()
            if not station_id:
                errors.append(f"第 {line_no} 行: 站号为空")
                continue
            if station_id not in registry:
                errors.append(
                    f"第 {line_no} 行: 观测引用未登记站号 {station_id!r}")
                continue
            try:
                observed = _parse_time(
                    time_text, context=f"第 {line_no} 行测站 {station_id}")
            except StageDischargeInputError as exc:
                errors.append(str(exc))
                continue
            try:
                stage = _parse_stage(
                    stage_text, context=f"第 {line_no} 行测站 {station_id}")
            except StageDischargeInputError as exc:
                errors.append(str(exc))
                continue

            key = (station_id, observed)
            existing = keyed.get(key)
            if existing is None:
                keyed[key] = Observation(
                    station_id=station_id, observed_at=observed, stage=stage,
                    source_lines=(line_no,))
            elif existing.stage == stage:
                merged_count += 1
                keyed[key] = Observation(
                    station_id=station_id, observed_at=observed, stage=stage,
                    source_lines=tuple(sorted(
                        existing.source_lines + (line_no,))))
            else:
                errors.append(
                    f"第 {line_no} 行与第 "
                    f"{','.join(map(str, existing.source_lines))} 行: "
                    f"测站 {station_id} 在同一 UTC 时刻 "
                    f"{_utc_iso(observed)} 水位冲突 "
                    f"({_format_decimal(existing.stage)} vs "
                    f"{_format_decimal(stage)})，输入本身存在歧义")

    if errors:
        raise StageDischargeInputError("；".join(errors))
    observations = sorted(
        keyed.values(),
        key=lambda o: (o.station_id, o.observed_at, o.source_lines))
    return observations, raw_count, merged_count


# --------------------------------------------------------------------------- #
# 曲线换算与逐站回放
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Evaluation:
    """一次曲线求值：命中节点或两个包围节点之间的线性插值。"""

    discharge: Decimal
    method: str
    lower_index: int
    upper_index: int
    fraction: Decimal


def evaluate_nodes(nodes: CurveNodes, stage: Decimal) -> Evaluation | None:
    """求流量；超出覆盖范围返回 None（绝不外推）。"""
    stages = nodes.stages
    index = bisect_left(stages, stage)
    if index < len(stages) and stages[index] == stage:
        return Evaluation(
            discharge=nodes.discharges[index], method=METHOD_NODE,
            lower_index=index, upper_index=index, fraction=Decimal(0))
    if index == 0 or index == len(stages):
        return None
    lower = index - 1
    upper = index
    with localcontext() as ctx:
        ctx.prec = _INTERPOLATION_PRECISION
        stage_span = stages[upper] - stages[lower]
        fraction = (stage - stages[lower]) / stage_span
        discharge = (nodes.discharges[lower]
                     + (stage - stages[lower])
                     * (nodes.discharges[upper]
                        - nodes.discharges[lower]) / stage_span)
    return Evaluation(
        discharge=discharge, method=METHOD_INTERPOLATION,
        lower_index=lower, upper_index=upper, fraction=fraction)


def _calculation_text(stage: Decimal, nodes: CurveNodes,
                      evaluation: Evaluation) -> str:
    if evaluation.method == METHOD_NODE:
        return (f"Q = q(h={_format_decimal(stage)}) = "
                f"{_format_decimal(evaluation.discharge)}")
    low, high = evaluation.lower_index, evaluation.upper_index
    return (
        f"Q = {_format_decimal(nodes.discharges[low])} + "
        f"({_format_decimal(stage)} - {_format_decimal(nodes.stages[low])})"
        f" * ({_format_decimal(nodes.discharges[high])} - "
        f"{_format_decimal(nodes.discharges[low])}) / "
        f"({_format_decimal(nodes.stages[high])} - "
        f"{_format_decimal(nodes.stages[low])}) = "
        f"{_format_decimal(evaluation.discharge)}")


@dataclass
class _ReplayState:
    branch: str
    last_success_stage: Decimal | None = None


def _decide_branch(state: _ReplayState, stage: Decimal,
                   deadband: Deadband) -> tuple[str, str, Decimal | None, str]:
    """返回 (候选分支, 决策, 与上一条成功水位的差值, 上一条成功水位)。"""
    if state.last_success_stage is None:
        return state.branch, DECISION_INITIAL, None, ""
    delta = stage - state.last_success_stage
    if delta > deadband.upper:
        return BRANCH_RISING, DECISION_TO_RISING, delta, state.last_success_stage
    if delta < deadband.lower:
        return BRANCH_FALLING, DECISION_TO_FALLING, delta, state.last_success_stage
    return state.branch, DECISION_WITHIN_DEADBAND, delta, state.last_success_stage


def _result_row(observation: Observation, registry: StationRegistry,
                state: _ReplayState) -> dict:
    stage = observation.stage
    candidate, decision, delta, previous_stage = _decide_branch(
        state, stage, registry.deadband)
    version = registry.version_at(observation.observed_at)

    row = {
        "station_id": observation.station_id,
        "observed_at_utc": _utc_iso(observation.observed_at),
        "stage_m": _format_decimal(stage),
        "source_lines": ";".join(map(str, observation.source_lines)),
        "record_count": str(observation.record_count),
        "status": "",
        "discharge_m3_s": "",
        "method": "",
        "reason": "",
        "curve_id": "",
        "revision": "",
        "curve_effective_from_utc": "",
        "curve_effective_to_utc": "",
        "branch": candidate,
        "branch_decision": decision,
        "previous_stage_m": (_format_decimal(previous_stage)
                             if isinstance(previous_stage, Decimal) else ""),
        "stage_delta_m": _format_decimal(delta),
        "curve_stage_min_m": "",
        "curve_stage_max_m": "",
        "lower_stage_m": "",
        "upper_stage_m": "",
        "lower_discharge_m3_s": "",
        "upper_discharge_m3_s": "",
        "interpolation_fraction": "",
        "calculation": "",
        "state_applied": "false",
    }

    if version is None:
        # 没有适用版本时连分支也无法“采用”；branch_decision 只说明该时刻
        # 根本没有曲线，不提交也不猜测任何回放状态。
        row["branch"] = ""
        row["branch_decision"] = DECISION_NO_CURVE
        row["status"] = STATUS_NOT_CONVERTED
        row["reason"] = REASON_NO_CURVE
        return row

    row["curve_id"] = version.curve_id
    row["revision"] = str(version.revision)
    row["curve_effective_from_utc"] = _utc_iso(version.effective_from)
    row["curve_effective_to_utc"] = (
        _utc_iso(version.effective_to) if version.effective_to else "")
    nodes = version.nodes_for(candidate)
    row["curve_stage_min_m"] = _format_decimal(nodes.stage_min)
    row["curve_stage_max_m"] = _format_decimal(nodes.stage_max)

    evaluation = evaluate_nodes(nodes, stage)
    if evaluation is None:
        row["status"] = STATUS_NOT_CONVERTED
        row["reason"] = REASON_OUT_OF_RANGE
        return row

    row["status"] = STATUS_CONVERTED
    row["discharge_m3_s"] = _format_decimal(evaluation.discharge)
    row["method"] = evaluation.method
    row["lower_stage_m"] = _format_decimal(nodes.stages[evaluation.lower_index])
    row["upper_stage_m"] = _format_decimal(nodes.stages[evaluation.upper_index])
    row["lower_discharge_m3_s"] = _format_decimal(
        nodes.discharges[evaluation.lower_index])
    row["upper_discharge_m3_s"] = _format_decimal(
        nodes.discharges[evaluation.upper_index])
    row["interpolation_fraction"] = _format_decimal(evaluation.fraction)
    row["calculation"] = _calculation_text(stage, nodes, evaluation)
    row["state_applied"] = "true"

    # 只有成功换算才提交回放状态；失败行不改变上一条成功水位与分支。
    state.branch = candidate
    state.last_success_stage = stage
    return row


def compute_results(observations: list[Observation],
                    registry: dict[str, StationRegistry]) -> list[dict]:
    """按 (站号, UTC 时刻) 逐站独立回放；站与站之间不共享状态。"""
    states: dict[str, _ReplayState] = {
        station_id: _ReplayState(branch=station.initial_branch)
        for station_id, station in registry.items()
    }
    rows: list[dict] = []
    for observation in observations:
        station = registry[observation.station_id]
        rows.append(_result_row(observation, station,
                                states[observation.station_id]))
    return rows


# --------------------------------------------------------------------------- #
# 摘要与输出
# --------------------------------------------------------------------------- #
def build_summary(observations_path: Path, registry_path: Path,
                  registry: dict[str, StationRegistry],
                  observations: list[Observation],
                  raw_count: int, merged_count: int,
                  rows: list[dict]) -> dict:
    converted = sum(1 for r in rows if r["status"] == STATUS_CONVERTED)
    reason_counts = {reason: 0 for reason in REASONS}
    for row in rows:
        reason = row["reason"]
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    per_station: dict[str, dict] = {}
    for station_id in sorted(registry):
        station = registry[station_id]
        station_rows = [r for r in rows if r["station_id"] == station_id]
        station_converted = sum(
            1 for r in station_rows if r["status"] == STATUS_CONVERTED)
        station_reasons = {reason: 0 for reason in REASONS}
        for row in station_rows:
            if row["reason"]:
                station_reasons[row["reason"]] += 1
        # 回放状态就是最近一条成功行的结果；没有任何成功行时保持初始分支。
        last_success = None
        final_branch = station.initial_branch
        for row in station_rows:
            if row["status"] == STATUS_CONVERTED:
                final_branch = row["branch"]
                last_success = row["stage_m"]
        per_station[station_id] = {
            "initial_branch": station.initial_branch,
            "deadband_lower_m": _format_decimal(station.deadband.lower),
            "deadband_upper_m": _format_decimal(station.deadband.upper),
            "curve_versions": len(station.curves),
            "observations": len(station_rows),
            "converted": station_converted,
            "not_converted": len(station_rows) - station_converted,
            "no_effective_curve": station_reasons[REASON_NO_CURVE],
            "stage_out_of_curve_range": station_reasons[REASON_OUT_OF_RANGE],
            "final_branch": final_branch,
            "final_successful_stage_m": last_success or "",
        }

    stations_observed = len({o.station_id for o in observations})
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "stage-discharge",
        "inputs": {
            "observations_csv": {
                "path": str(observations_path),
                "sha256": sha256_file(observations_path),
                "size_bytes": observations_path.stat().st_size,
            },
            "registry_json": {
                "path": str(registry_path),
                "sha256": sha256_file(registry_path),
                "size_bytes": registry_path.stat().st_size,
            },
        },
        "registry_summary": {
            "stations": len(registry),
            "stations_observed": stations_observed,
            "curve_versions": sum(len(s.curves) for s in registry.values()),
            "per_station": per_station,
        },
        "counts": {
            "raw_observation_records": raw_count,
            "merged_duplicate_records": merged_count,
            "unique_observations": len(observations),
            "result_rows": len(rows),
            "converted_rows": converted,
            "not_converted_rows": len(rows) - converted,
            "not_converted_no_effective_curve": reason_counts[REASON_NO_CURVE],
            "not_converted_stage_out_of_curve_range":
                reason_counts[REASON_OUT_OF_RANGE],
        },
        "reason_counts": reason_counts,
        "conventions": {
            "effective_interval":
                "半开区间 [effective_from, effective_to)；首尾相接不算交叠，"
                "生效时刻属于新版本；未设 effective_to 表示开放式",
            "deadband":
                "仅当 水位差 > 死区上限 转涨水、水位差 < 死区下限 转退水；"
                "差值落在闭区间内（含恰好碰到边界）保持当前分支",
            "branch_replay":
                "每站独立回放，从 initial_branch 起；与上一条成功换算水位比较，"
                "不读取其他测站状态",
            "failed_rows":
                "无适用版本或超出曲线覆盖范围的行不外推，也不更新上一条"
                "成功水位与分支",
            "interpolation":
                "水位恰为节点时直接取节点流量；否则在包围水位的两个节点间"
                "线性换算。内部用十进制运算，输出按 6 位小数呈现",
        },
        "disclaimer": (
            "换算流量是由率定关系（水位-流量曲线）推算的估计值，"
            "并非现场直接测得的流量；超出曲线覆盖范围时不做外推，"
            "该行只给出未换算原因。"),
    }


def _render_results_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=RESULT_HEADER,
                            lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    payload = content.encode("utf-8")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        with open(tmp, "rb") as fh:
            actual = fh.read()
        if actual != payload:
            raise StageDischargeOutputError(
                f"临时文件回读内容与预期不一致: {tmp}")
        os.replace(tmp, target)
    except StageDischargeOutputError:
        _quiet_unlink(tmp)
        raise
    except OSError as exc:
        _quiet_unlink(tmp)
        raise StageDischargeOutputError(f"写出 {target} 失败: {exc}") from exc


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _write_outputs(output_dir: Path, results_csv: str, summary_text: str) -> None:
    """两份结果先全部渲染成功再写；任一失败回滚并恢复旧文件。"""
    targets = [
        (output_dir / RESULTS_FILENAME, results_csv),
        (output_dir / SUMMARY_FILENAME, summary_text),
    ]
    backups: list[tuple[Path, Path | None]] = []
    try:
        for target, content in targets:
            backup = None
            if target.exists():
                backup = target.with_name(target.name + ".bak")
                os.replace(target, backup)
            backups.append((target, backup))
            _atomic_write(target, content)
    except BaseException:
        for target, backup in reversed(backups):
            _quiet_unlink(target)
            if backup is not None and backup.exists():
                os.replace(backup, target)
        raise
    else:
        for _, backup in backups:
            if backup is not None:
                _quiet_unlink(backup)


def process_stage_discharge(observations_path: str | Path,
                            registry_path: str | Path,
                            output_dir: str | Path) -> dict:
    """端到端：校验两份输入 → 逐站回放换算 → 结果表 + 摘要原子替换。

    任何输入错误都在写出之前抛出，旧结果保持可用，不产生半套新文件。
    返回写出的摘要 dict。
    """
    observations_path = Path(observations_path)
    registry_path = Path(registry_path)
    output_dir = Path(output_dir)

    registry = load_registry(registry_path)
    observations, raw_count, merged_count = _read_observations(
        observations_path, registry)
    rows = compute_results(observations, registry)
    summary = build_summary(
        observations_path, registry_path, registry, observations,
        raw_count, merged_count, rows)

    results_csv = _render_results_csv(rows)
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2,
                              sort_keys=True) + "\n"
    _write_outputs(output_dir, results_csv, summary_text)
    return summary
