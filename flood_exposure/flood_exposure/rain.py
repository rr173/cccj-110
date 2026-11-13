"""雨量站累计翻斗读数整理（纯本地、标准库实现）。

把雨量站定时上报的**累计翻斗计数器读数**整理为可追查质量问题的逐段
降雨量与降雨事件。本模块不接入任何网络服务，也不依赖或改动洪水栅格 /
DEM 计算链（不 import numpy / rasterio / pandas）。

核心口径
--------
* 累计读数只确定**观测区间**上的雨量：相邻有效读数之差 × 每斗毫米数，
  归属于整个 [前一观测时刻, 后一观测时刻) 区间，**不能**据此声称雨实际
  落在区间内的哪个时刻。
* 时间统一换算为 UTC 后再排序；同设备同一时刻的完全相同记录合并，
  读数不同属于冲突，整体失败、不产出结果。
* 计数器下降：只有配置了模数且前读数恰在高边界 (modulus-1)、后读数恰在
  低边界 (0) 时才按回绕（wrap）计算；其他下降一律视为设备重置，
  该段无效并以后读数重新建立基准。
* 超过最大读数间隔、跨过检修时段的区间不能跨段相减，写出具体质量标记，
  从后一条有效读数重新开始。
* 有效零雨段（读数未增加）与无效段严格区分。
* 事件按配置的干旱分隔时长归并；无效段与超长空档截断事件，不把两侧的
  雨拼成一场。

全部输出先写临时文件并回读校验，再 ``os.replace`` 原子替换目标；
任何配置错误或输入冲突都在替换之前抛出，旧结果保持可用。
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .hashing import sha256_file

# --------------------------------------------------------------------------- #
# 常量：列名、质量标记、输出文件名
# --------------------------------------------------------------------------- #
CSV_DEVICE_COL = "device_id"
CSV_TIME_COL = "observed_at"
CSV_COUNT_COL = "cumulative_count"
REQUIRED_CSV_COLUMNS = (CSV_DEVICE_COL, CSV_TIME_COL, CSV_COUNT_COL)

#: 逐段 CSV 的质量标记（互斥，每段取按严重度排序的第一个）。
Q_OK = "OK"                       # 有效湿段：读数增加，区间雨量 > 0
Q_ZERO = "ZERO_RAIN"              # 有效零雨段：读数未增加，区间雨量确认为 0
Q_WRAP = "COUNTER_WRAP"           # 按配置模数合法回绕，区间雨量按模数补回
Q_RESET = "COUNTER_RESET"         # 下降但不满足回绕条件：设备重置，段无效
Q_LONG_GAP = "GAP_TOO_LONG"       # 区间时长超过 max_interval，禁止跨段相减
Q_MAINT = "MAINTENANCE_WINDOW"    # 区间与检修时段相交，禁止跨段相减
Q_TIME_REVERSED = "TIME_REVERSED"  # 防御性标记：UTC 排序后仍出现时刻倒退

#: 摘要里按主标记统计时使用的固定顺序（也是严重度优先级）。
QUALITY_FLAGS = (Q_OK, Q_ZERO, Q_WRAP, Q_LONG_GAP, Q_MAINT, Q_RESET,
                 Q_TIME_REVERSED)
VALID_FLAGS = frozenset({Q_OK, Q_ZERO, Q_WRAP})

SEGMENTS_FILENAME = "rainfall_segments.csv"
EVENTS_FILENAME = "rainfall_events.csv"
SUMMARY_FILENAME = "rainfall_summary.json"

#: 雨量输出的固定小数位：翻斗雨量是 斗数 × tip_mm 的乘法，统一按 6 位小数
#: 呈现，避免二进制浮点尾差（如 20.000000000000004）；6 位远超翻斗分辨率
#: 所需精度，内部比较仍按精确的整斗增量进行，不依据该舍入做任何判定。
RAIN_MM_DECIMALS = 6

SEGMENT_HEADER = [
    "device_id", "interval_start_utc", "interval_end_utc",
    "interval_seconds", "start_count", "end_count", "count_delta",
    "tip_mm", "rainfall_mm", "quality_flag", "valid",
    "start_source_lines", "end_source_lines",
]
EVENT_HEADER = [
    "device_id", "event_start_utc", "event_end_utc", "duration_seconds",
    "total_rainfall_mm", "max_segment_rainfall_mm", "segment_count",
    "truncated_by_data_boundary", "source_lines",
]


class RainConfigError(ValueError):
    """设备配置非法。"""


class RainInputError(ValueError):
    """输入读数无法处理（缺时区、未知设备、冲突、负读数等）。"""


class RainOutputError(RuntimeError):
    """写出或回读校验失败。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MaintenanceWindow:
    """检修时段 [start, end)，半开区间，边界相碰不算跨过。"""

    start: datetime
    end: datetime

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return start < self.end and self.start < end

    def to_dict(self) -> dict:
        return {"start": _utc_iso(self.start), "end": _utc_iso(self.end)}


@dataclass(frozen=True)
class DeviceConfig:
    """单台雨量站的配置。"""

    device_id: str
    tip_mm: float
    max_interval: timedelta
    dry_separation: timedelta
    counter_modulus: int | None = None
    maintenance: tuple[MaintenanceWindow, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "tip_mm": self.tip_mm,
            "max_interval_seconds": self.max_interval.total_seconds(),
            "dry_separation_seconds": self.dry_separation.total_seconds(),
            "counter_modulus": self.counter_modulus,
            "maintenance": [w.to_dict() for w in self.maintenance],
        }


@dataclass(frozen=True)
class RainConfig:
    """全部雨量站的配置。"""

    devices: dict[str, DeviceConfig]

    def to_dict(self) -> dict:
        return {"devices": {
            dev: self.devices[dev].to_dict() for dev in sorted(self.devices)
        }}

    # ------------------------------------------------------------------ #
    @classmethod
    def from_file(cls, path: str | Path) -> "RainConfig":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise RainConfigError(f"设备配置不是合法 JSON: {exc}") from exc
        except OSError as exc:
            raise RainConfigError(f"无法读取设备配置 {path}: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: object) -> "RainConfig":
        if not isinstance(data, dict):
            raise RainConfigError("配置顶层必须是对象，且包含 devices 字段")
        if set(data.keys()) != {"devices"}:
            unknown = sorted(set(data.keys()) - {"devices"})
            raise RainConfigError(f"配置含未知顶层字段: {unknown}")
        raw_devices = data["devices"]
        if not isinstance(raw_devices, dict) or not raw_devices:
            raise RainConfigError("devices 必须是非空对象，键为设备编号")
        devices: dict[str, DeviceConfig] = {}
        for device_id, raw in sorted(raw_devices.items()):
            if not isinstance(device_id, str) or not device_id.strip():
                raise RainConfigError("设备编号必须是非空字符串")
            devices[device_id] = _parse_device(device_id, raw)
        return cls(devices=devices)


_ALLOWED_DEVICE_KEYS = {
    "tip_mm", "max_interval_seconds", "dry_separation_seconds",
    "counter_modulus", "maintenance",
}
_ALLOWED_WINDOW_KEYS = {"start", "end"}


def _parse_device(device_id: str, raw: object) -> DeviceConfig:
    if not isinstance(raw, dict):
        raise RainConfigError(f"设备 {device_id} 的配置必须是对象")
    unknown = sorted(set(raw.keys()) - _ALLOWED_DEVICE_KEYS)
    if unknown:
        raise RainConfigError(
            f"设备 {device_id} 含未知配置字段: {unknown}")

    tip_mm = _require_finite_positive(raw, "tip_mm", device_id)
    max_interval_s = _require_finite_positive(
        raw, "max_interval_seconds", device_id)
    dry_sep_s = _require_finite_positive(
        raw, "dry_separation_seconds", device_id)

    modulus: int | None = None
    if "counter_modulus" in raw:
        value = raw["counter_modulus"]
        if value is not None:
            if not isinstance(value, int) or isinstance(value, bool) or value < 2:
                raise RainConfigError(
                    f"设备 {device_id} 的 counter_modulus 必须是 >=2 的整数"
                    "（或省略/null 表示不允许回绕）")
            modulus = value

    windows: list[MaintenanceWindow] = []
    if "maintenance" in raw:
        raw_windows = raw["maintenance"]
        if not isinstance(raw_windows, list):
            raise RainConfigError(
                f"设备 {device_id} 的 maintenance 必须是时段数组")
        for index, item in enumerate(raw_windows):
            if not isinstance(item, dict) or \
                    set(item.keys()) != _ALLOWED_WINDOW_KEYS:
                raise RainConfigError(
                    f"设备 {device_id} 第 {index} 个检修时段必须且只能含 "
                    "start/end 两个字段")
            start = _parse_config_time(item["start"], device_id, index, "start")
            end = _parse_config_time(item["end"], device_id, index, "end")
            if start >= end:
                raise RainConfigError(
                    f"设备 {device_id} 第 {index} 个检修时段 start 必须早于 end")
            windows.append(MaintenanceWindow(start=start, end=end))
    windows.sort(key=lambda w: (w.start, w.end))
    return DeviceConfig(
        device_id=device_id,
        tip_mm=tip_mm,
        max_interval=timedelta(seconds=max_interval_s),
        dry_separation=timedelta(seconds=dry_sep_s),
        counter_modulus=modulus,
        maintenance=tuple(windows),
    )


def _require_finite_positive(raw: dict, key: str, device_id: str) -> float:
    if key not in raw:
        raise RainConfigError(f"设备 {device_id} 缺少必填字段 {key}")
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RainConfigError(f"设备 {device_id} 的 {key} 必须是数字")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise RainConfigError(f"设备 {device_id} 的 {key} 必须是有限正数")
    return value


def _parse_config_time(value: object, device_id: str,
                       index: int, side: str) -> datetime:
    if not isinstance(value, str):
        raise RainConfigError(
            f"设备 {device_id} 第 {index} 个检修时段 {side} 必须是时间字符串")
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise RainConfigError(
            f"设备 {device_id} 第 {index} 个检修时段 {side} "
            f"时间无法解析: {value!r}") from exc
    if dt.tzinfo is None:
        raise RainConfigError(
            f"设备 {device_id} 第 {index} 个检修时段 {side} "
            f"缺少时区偏移，拒绝猜测: {value!r}")
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# 时间与读数解析
# --------------------------------------------------------------------------- #
def _parse_iso_time(text: str, *, context: str,
                    require_offset: bool) -> datetime:
    """解析 ISO 8601 时间；require_offset=True 时拒绝无时区时间。"""
    try:
        dt = datetime.fromisoformat(text.strip())
    except (ValueError, TypeError) as exc:
        raise RainInputError(f"{context} 时间无法解析: {text!r}") from exc
    if require_offset and dt.tzinfo is None:
        raise RainInputError(f"{context} 缺少时区偏移，拒绝猜测: {text!r}")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt


def _utc_iso(dt: datetime) -> str:
    """统一的 UTC 输出格式：显式 Z 结尾、秒精度，无偏移则按 UTC 标注。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _format_number(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    value = float(value)
    rounded = round(value, RAIN_MM_DECIMALS)
    # round() 后可能得到 -0.0；雨量语义上不应为负
    if rounded == 0:
        rounded = 0.0
    return repr(rounded)


# --------------------------------------------------------------------------- #
# 内部数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Reading:
    """一条去重后的有效读数。"""

    device_id: str
    observed_at: datetime          # 已换算 UTC
    count: int
    source_lines: tuple[int, ...]  # 对应原始 CSV 物理行（1 起，含表头）
    duplicate_count: int           # 合并的完全相同记录数（>=1）


@dataclass
class Segment:
    """一个观测区间的整理结果。"""

    device_id: str
    start: datetime
    end: datetime
    start_count: int
    end_count: int
    count_delta: int | None
    rainfall_mm: float | None
    quality_flag: str
    valid: bool
    start_lines: tuple[int, ...]
    end_lines: tuple[int, ...]

    @property
    def interval_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class RainEvent:
    """一场降雨事件（由若干有效湿段按干旱分隔时长归并）。"""

    device_id: str
    start: datetime
    end: datetime
    total_rainfall_mm: float
    max_segment_rainfall_mm: float
    segment_count: int
    truncated_by_data_boundary: bool
    source_lines: tuple[int, ...]


# --------------------------------------------------------------------------- #
# 读数 CSV 读取与去重
# --------------------------------------------------------------------------- #
def _read_readings(path: Path, config: RainConfig):
    """读取读数 CSV，返回 (readings, n_raw, n_merged, rejects)。

    致命错误（缺时区、未知设备、负读数、同刻冲突、表头非法）抛
    :class:`RainInputError`；字段级坏行（无法解析的时间/计数等）计入
    rejects，不静默忽略。
    """
    try:
        fh = open(path, "r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise RainInputError(f"无法读取读数文件 {path}: {exc}") from exc

    n_raw = 0
    n_merged = 0
    rejects: list[dict] = []
    # (device, UTC 时刻) -> Reading
    keyed: dict[tuple[str, datetime], Reading] = {}
    errors: list[str] = []

    with fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise RainInputError("读数 CSV 为空，缺少表头")
        header = [name.strip() for name in reader.fieldnames]
        missing = [c for c in REQUIRED_CSV_COLUMNS if c not in header]
        if missing:
            raise RainInputError(f"读数 CSV 缺少必填列: {missing}")
        extra = sorted(set(header) - set(REQUIRED_CSV_COLUMNS))
        if extra:
            raise RainInputError(f"读数 CSV 含未知列: {extra}")

        for row in reader:
            line_no = reader.line_num
            # DictReader 对多余列把内容放进 None；整行空白时各值为 None/''
            if None in row or any(v is None for v in row.values()):
                rejects.append({"line": line_no, "device_id": "",
                                "reason": "列数与表头不一致"})
                continue
            if all((v or "").strip() == "" for v in row.values()):
                # 完全空白行：没有任何记录内容，跳过但不计入读数/拒绝
                continue
            n_raw += 1
            device_id = (row[CSV_DEVICE_COL] or "").strip()
            time_text = (row[CSV_TIME_COL] or "").strip()
            count_text = (row[CSV_COUNT_COL] or "").strip()
            if not device_id:
                rejects.append({"line": line_no, "device_id": "",
                                "reason": "设备编号为空"})
                continue
            if device_id not in config.devices:
                errors.append(
                    f"第 {line_no} 行: 未知设备 {device_id!r}，拒绝猜测")
                continue
            try:
                observed = _parse_iso_time(
                    time_text,
                    context=f"第 {line_no} 行设备 {device_id}",
                    require_offset=True)
            except RainInputError as exc:
                errors.append(str(exc))
                continue
            try:
                count = _parse_count(count_text)
            except RainInputError as exc:
                rejects.append({"line": line_no, "device_id": device_id,
                                "reason": str(exc)})
                continue
            if count < 0:
                errors.append(
                    f"第 {line_no} 行设备 {device_id}: 负读数 {count}，拒绝")
                continue

            key = (device_id, observed)
            existing = keyed.get(key)
            if existing is None:
                keyed[key] = Reading(
                    device_id=device_id, observed_at=observed,
                    count=count, source_lines=(line_no,),
                    duplicate_count=1)
            elif existing.count == count:
                n_merged += 1
                keyed[key] = Reading(
                    device_id=device_id, observed_at=observed,
                    count=count,
                    source_lines=tuple(sorted(
                        existing.source_lines + (line_no,))),
                    duplicate_count=existing.duplicate_count + 1)
            else:
                errors.append(
                    f"第 {line_no} 行与第 "
                    f"{','.join(map(str, existing.source_lines))} 行: "
                    f"设备 {device_id} 在同一时刻 {_utc_iso(observed)} "
                    f"读数冲突 ({existing.count} vs {count})")

    if errors:
        raise RainInputError("；".join(errors))
    readings = sorted(keyed.values(),
                      key=lambda r: (r.device_id, r.observed_at,
                                     r.source_lines))
    return readings, n_raw, n_merged, rejects


def _parse_count(text: str) -> int:
    if text == "":
        raise RainInputError("累计翻斗次数为空")
    try:
        # int() 接受 "+3"/" 3 "；显式拒绝小数（含 3.0），避免把浮点当计数
        return int(text)
    except ValueError as exc:
        raise RainInputError(
            f"累计翻斗次数不是整数: {text!r}") from exc


# --------------------------------------------------------------------------- #
# 逐段整理
# --------------------------------------------------------------------------- #
def build_segments(readings: list[Reading],
                   config: RainConfig) -> list[Segment]:
    """按设备把相邻读数整理为带质量标记的区间段。"""
    segments: list[Segment] = []
    by_device: dict[str, list[Reading]] = {}
    for reading in readings:
        by_device.setdefault(reading.device_id, []).append(reading)

    for device_id in sorted(by_device):
        device_readings = by_device[device_id]
        cfg = config.devices[device_id]
        previous: Reading | None = None
        for reading in device_readings:
            if previous is None:
                previous = reading
                continue
            segments.append(_classify_interval(cfg, previous, reading))
            previous = reading
    return segments


def _classify_interval(cfg: DeviceConfig,
                       previous: Reading, current: Reading) -> Segment:
    common = dict(
        device_id=cfg.device_id,
        start=previous.observed_at,
        end=current.observed_at,
        start_count=previous.count,
        end_count=current.count,
        start_lines=previous.source_lines,
        end_lines=current.source_lines,
    )
    delta_seconds = (current.observed_at - previous.observed_at).total_seconds()

    # 防御性检查：输入已按 UTC 排序，正常数据不会触发；若触发则不允许相减。
    if delta_seconds < 0:
        return _invalid(common, Q_TIME_REVERSED)
    if delta_seconds > cfg.max_interval.total_seconds():
        return _invalid(common, Q_LONG_GAP)
    if any(w.overlaps(previous.observed_at, current.observed_at)
           for w in cfg.maintenance):
        return _invalid(common, Q_MAINT)

    raw_delta = current.count - previous.count
    if raw_delta >= 0:
        if raw_delta == 0:
            return Segment(count_delta=0, rainfall_mm=0.0,
                           quality_flag=Q_ZERO, valid=True, **common)
        return Segment(count_delta=raw_delta,
                       rainfall_mm=raw_delta * cfg.tip_mm,
                       quality_flag=Q_OK, valid=True, **common)

    # 读数下降：只有模数存在且严格跨过配置的高/低边界才允许按回绕处理。
    modulus = cfg.counter_modulus
    if modulus is not None and \
            previous.count == modulus - 1 and current.count == 0:
        wrapped_delta = modulus + raw_delta  # = modulus - (prev - curr)
        return Segment(count_delta=wrapped_delta,
                       rainfall_mm=wrapped_delta * cfg.tip_mm,
                       quality_flag=Q_WRAP, valid=True, **common)
    return _invalid(common, Q_RESET)


def _invalid(common: dict, flag: str) -> Segment:
    return Segment(count_delta=None, rainfall_mm=None,
                   quality_flag=flag, valid=False, **common)


# --------------------------------------------------------------------------- #
# 事件归并
# --------------------------------------------------------------------------- #
def build_events(segments: list[Segment],
                 config: RainConfig) -> list[RainEvent]:
    """按设备与干旱分隔时长把有效湿段归为降雨事件。

    无效段与超长空档本身已经截断（湿段不会跨过它们出现）；相邻有效湿段
    之间的干段（零雨段）时长累计超过 ``dry_separation`` 时拆分事件，
    等于配置时长时仍属同一场（严格大于才拆）。
    """
    events: list[RainEvent] = []
    by_device: dict[str, list[Segment]] = {}
    for segment in segments:
        by_device.setdefault(segment.device_id, []).append(segment)

    for device_id in sorted(by_device):
        cfg = config.devices[device_id]
        device_segments = by_device[device_id]
        first_accepted_time = device_segments[0].start
        last_accepted_time = device_segments[-1].end

        current: list[Segment] = []
        last_wet_end: datetime | None = None

        def flush(wet_segments: list[Segment]) -> None:
            if not wet_segments:
                return
            total = sum(s.rainfall_mm for s in wet_segments)
            lines = tuple(sorted({
                line
                for s in wet_segments
                for line in s.start_lines + s.end_lines
            }))
            truncated = (
                wet_segments[0].start == first_accepted_time
                or wet_segments[-1].end == last_accepted_time
            )
            events.append(RainEvent(
                device_id=device_id,
                start=wet_segments[0].start,
                end=wet_segments[-1].end,
                total_rainfall_mm=total,
                max_segment_rainfall_mm=max(
                    s.rainfall_mm for s in wet_segments),
                segment_count=len(wet_segments),
                truncated_by_data_boundary=truncated,
                source_lines=lines,
            ))

        for segment in device_segments:
            if not segment.valid:
                # 无效段截断事件，两侧的雨绝不拼成一场
                flush(current)
                current = []
                last_wet_end = None
                continue
            if segment.quality_flag == Q_ZERO:
                # 零雨段累积干旱时长；它不会成为事件的一部分
                if current and last_wet_end is not None:
                    dry_seconds = (segment.end - last_wet_end).total_seconds()
                    if dry_seconds > cfg.dry_separation.total_seconds():
                        flush(current)
                        current = []
                        last_wet_end = None
                continue
            # 有效湿段
            if current and last_wet_end is not None:
                # 中间只可能隔着零雨段（无效段已在上面截断）
                gap = (segment.start - last_wet_end).total_seconds()
                if gap > cfg.dry_separation.total_seconds():
                    flush(current)
                    current = []
            current.append(segment)
            last_wet_end = segment.end
        flush(current)

    return events


# --------------------------------------------------------------------------- #
# 摘要与原子写出
# --------------------------------------------------------------------------- #
def build_summary(config: RainConfig, readings_path: Path,
                  config_path: Path, readings: list[Reading],
                  n_raw: int, n_merged: int,
                  rejects: list[dict], segments: list[Segment],
                  events: list[RainEvent]) -> dict:
    flag_counts = {flag: 0 for flag in QUALITY_FLAGS}
    valid_count = 0
    invalid_count = 0
    zero_count = 0
    for segment in segments:
        flag_counts[segment.quality_flag] = \
            flag_counts.get(segment.quality_flag, 0) + 1
        if segment.valid:
            valid_count += 1
            if segment.quality_flag == Q_ZERO:
                zero_count += 1
        else:
            invalid_count += 1

    readings_per_device: dict[str, int] = {}
    for reading in readings:
        readings_per_device[reading.device_id] = \
            readings_per_device.get(reading.device_id, 0) + 1

    per_device: dict[str, dict] = {}
    for device_id in sorted(config.devices):
        dev_segments = [s for s in segments if s.device_id == device_id]
        dev_events = [e for e in events if e.device_id == device_id]
        per_device[device_id] = {
            "readings": readings_per_device.get(device_id, 0),
            "segments": len(dev_segments),
            "valid_segments": sum(1 for s in dev_segments if s.valid),
            "invalid_segments": sum(1 for s in dev_segments if not s.valid),
            "zero_rain_segments": sum(
                1 for s in dev_segments
                if s.valid and s.quality_flag == Q_ZERO),
            "events": len(dev_events),
            "total_valid_rainfall_mm": round(sum(
                s.rainfall_mm for s in dev_segments
                if s.valid and s.quality_flag != Q_ZERO),
                RAIN_MM_DECIMALS),
        }

    return {
        "schema_version": 1,
        "inputs": {
            "readings_csv": {
                "path": str(readings_path),
                "sha256": sha256_file(readings_path),
            },
            "device_config_json": {
                "path": str(config_path),
                "sha256": sha256_file(config_path),
            },
        },
        "config": config.to_dict(),
        "counts": {
            "raw_reading_records": n_raw,
            "merged_duplicate_records": n_merged,
            "unique_readings": len(readings),
            "rejected_records": len(rejects),
            "segments_total": len(segments),
            "segments_valid": valid_count,
            "segments_invalid": invalid_count,
            "zero_rain_segments": zero_count,
            "events": len(events),
        },
        "rejected_records": rejects,
        "quality_flag_counts": flag_counts,
        "per_device": per_device,
    }


def _segment_row(segment: Segment, tip_mm: float) -> dict:
    return {
        "device_id": segment.device_id,
        "interval_start_utc": _utc_iso(segment.start),
        "interval_end_utc": _utc_iso(segment.end),
        "interval_seconds": _format_number(segment.interval_seconds),
        "start_count": str(segment.start_count),
        "end_count": str(segment.end_count),
        "count_delta": _format_number(segment.count_delta),
        "tip_mm": _format_number(tip_mm),
        "rainfall_mm": _format_number(segment.rainfall_mm),
        "quality_flag": segment.quality_flag,
        "valid": "true" if segment.valid else "false",
        "start_source_lines": ";".join(map(str, segment.start_lines)),
        "end_source_lines": ";".join(map(str, segment.end_lines)),
    }


def _event_row(event: RainEvent) -> dict:
    return {
        "device_id": event.device_id,
        "event_start_utc": _utc_iso(event.start),
        "event_end_utc": _utc_iso(event.end),
        "duration_seconds": _format_number(
            (event.end - event.start).total_seconds()),
        "total_rainfall_mm": _format_number(event.total_rainfall_mm),
        "max_segment_rainfall_mm": _format_number(
            event.max_segment_rainfall_mm),
        "segment_count": str(event.segment_count),
        "truncated_by_data_boundary":
            "true" if event.truncated_by_data_boundary else "false",
        "source_lines": ";".join(map(str, event.source_lines)),
    }


def _render_segments_csv(segments: list[Segment],
                         config: RainConfig) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SEGMENT_HEADER,
                            lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    for segment in segments:
        writer.writerow(_segment_row(
            segment, config.devices[segment.device_id].tip_mm))
    return buf.getvalue()


def _render_events_csv(events: list[RainEvent]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EVENT_HEADER,
                            lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    for event in events:
        writer.writerow(_event_row(event))
    return buf.getvalue()


def _atomic_write(target: Path, content: str) -> None:
    """临时文件写出 → 刷新 → 回读校验 → os.replace 原子替换。"""
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
            raise RainOutputError(
                f"临时文件回读内容与预期不一致: {tmp}")
        os.replace(tmp, target)
    except RainOutputError:
        _quiet_unlink(tmp)
        raise
    except OSError as exc:
        _quiet_unlink(tmp)
        raise RainOutputError(f"写出 {target} 失败: {exc}") from exc


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def process_rainfall(readings_path: str | Path,
                     config_path: str | Path,
                     output_dir: str | Path) -> dict:
    """端到端整理：校验输入 → 逐段 → 事件 → 摘要 → 原子替换三个目标。

    任何阶段失败都不会替换已有目标文件。返回写出的摘要 dict。
    """
    readings_path = Path(readings_path)
    config_path = Path(config_path)
    output_dir = Path(output_dir)

    config = RainConfig.from_file(config_path)
    readings, n_raw, n_merged, rejects = _read_readings(readings_path, config)
    segments = build_segments(readings, config)
    events = build_events(segments, config)
    summary = build_summary(
        config, readings_path, config_path, readings, n_raw, n_merged,
        rejects, segments, events)

    segments_csv = _render_segments_csv(segments, config)
    events_csv = _render_events_csv(events)
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2,
                              sort_keys=True) + "\n"

    # 三份内容先在内存中全部渲染成功，再依次原子替换。每个目标替换前先把
    # 已存在的旧文件改名为 .bak 备份；任一步失败则删除本轮新文件并把备份
    # 逐一改回，保证不会留下“看似完整但其实是半套”的新结果。
    targets = [
        (output_dir / SEGMENTS_FILENAME, segments_csv),
        (output_dir / EVENTS_FILENAME, events_csv),
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
        # 回滚：删除本轮新文件，恢复备份
        for target, backup in reversed(backups):
            _quiet_unlink(target)
            if backup is not None and backup.exists():
                os.replace(backup, target)
        raise
    else:
        for _, backup in backups:
            if backup is not None:
                _quiet_unlink(backup)

    return summary
