"""rainfall 整理命令的独立断言测试（标准库 unittest，pytest 也可收集）。

所有期望值都是手算字面量，不复用被测模块的计算逻辑；每个输出区间都回查
原始 CSV 物理行号。覆盖需求列出的全部场景：

* 不等时间间隔的手算降雨量与有效零雨段
* 跨模数回绕（严格跨高/低边界）与不满足边界条件的下降
* 未配置回绕的意外归零：段无效并从新基准恢复
* 超长缺测与检修窗口导致的分段、事件截断（含半开边界相碰）
* 不同时区偏移换算 UTC、完全相同重复行合并、冲突重复行拒绝
* 干旱分隔时长边界两侧的事件合并（== 阈值）与拆分（> 阈值）
* 乱序输入结果稳定一致；失败不破坏既有输出
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from flood_exposure.cli import build_parser, main  # noqa: E402
from flood_exposure.hashing import sha256_file  # noqa: E402
from flood_exposure.rain import (  # noqa: E402
    EVENTS_FILENAME,
    SEGMENTS_FILENAME,
    SUMMARY_FILENAME,
    RainConfigError,
    RainInputError,
    process_rainfall,
)

HEADER = ["device_id", "observed_at", "cumulative_count"]


# --------------------------------------------------------------------------- #
# 夹具与独立参考逻辑
# --------------------------------------------------------------------------- #
class RainCase(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rain-test-"))
        self.cfg_path = self.tmp / "devices.json"
        self.readings_path = self.tmp / "readings.csv"
        self.out_dir = self.tmp / "out"

    def write_config(self, config: dict) -> None:
        self.cfg_path.write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def write_rows(self, rows: list[tuple[str, str, str]]) -> None:
        with open(self.readings_path, "w", newline="",
                  encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(HEADER)
            writer.writerows(rows)

    def run_pipeline(self):
        return process_rainfall(
            self.readings_path, self.cfg_path, self.out_dir)

    def read_segments(self) -> list[dict]:
        with open(self.out_dir / SEGMENTS_FILENAME, encoding="utf-8",
                  newline="") as fh:
            return list(csv.DictReader(fh))

    def read_events(self) -> list[dict]:
        with open(self.out_dir / EVENTS_FILENAME, encoding="utf-8",
                  newline="") as fh:
            return list(csv.DictReader(fh))

    def read_summary(self) -> dict:
        return json.loads(
            (self.out_dir / SUMMARY_FILENAME).read_text(encoding="utf-8"))

    def find_segment(self, segments, start: str, end: str,
                     device: str | None = None) -> dict:
        matches = [s for s in segments
                   if s["interval_start_utc"] == start
                   and s["interval_end_utc"] == end
                   and (device is None or s["device_id"] == device)]
        self.assertEqual(len(matches), 1, f"区间 {start}~{end} 不唯一")
        return matches[0]

    def assert_valid_rain(self, segment: dict, *, delta: int,
                          rain_mm: float, flag: str = "OK") -> None:
        self.assertEqual(segment["valid"], "true")
        self.assertEqual(segment["quality_flag"], flag)
        self.assertEqual(int(segment["count_delta"]), delta)
        self.assertAlmostEqual(float(segment["rainfall_mm"]), rain_mm)
        self.assertAlmostEqual(
            float(segment["rainfall_mm"]),
            delta * float(segment["tip_mm"]))  # 独立核对口径

    def assert_invalid(self, segment: dict, flag: str) -> None:
        self.assertEqual(segment["valid"], "false")
        self.assertEqual(segment["quality_flag"], flag)
        self.assertEqual(segment["count_delta"], "")
        self.assertEqual(segment["rainfall_mm"], "")


def standard_config() -> dict:
    """多设备综合配置：各设备专门承担一类场景。"""
    return {"devices": {
        # 不等间隔 + 零雨段 + 手算雨量
        "RG-A": {
            "tip_mm": 0.5,
            "max_interval_seconds": 3600,
            "dry_separation_seconds": 1800,
        },
        # 模数 100 的合法回绕与非法下降
        "RG-WRAP": {
            "tip_mm": 0.2,
            "max_interval_seconds": 7200,
            "dry_separation_seconds": 1800,
            "counter_modulus": 100,
        },
        # 未配置模数：任何下降都是重置
        "RG-RESET": {
            "tip_mm": 1.0,
            "max_interval_seconds": 7200,
            "dry_separation_seconds": 1800,
        },
        # 超长缺测
        "RG-GAP": {
            "tip_mm": 0.5,
            "max_interval_seconds": 1800,
            "dry_separation_seconds": 600,
        },
        # 检修窗口 [01:00,02:00) 与边界相碰窗口 [05:00,05:30)
        "RG-MAINT": {
            "tip_mm": 0.5,
            "max_interval_seconds": 7200,
            "dry_separation_seconds": 1800,
            "maintenance": [
                {"start": "2026-06-01T01:00:00+00:00",
                 "end": "2026-06-01T02:00:00+00:00"},
                {"start": "2026-06-01T05:00:00+00:00",
                 "end": "2026-06-01T05:30:00+00:00"},
            ],
        },
        # 不同时区偏移
        "RG-TZ": {
            "tip_mm": 0.5,
            "max_interval_seconds": 7200,
            "dry_separation_seconds": 1800,
        },
        # 干旱分隔边界（==1800 合并，>1800 拆分）
        "RG-DRY": {
            "tip_mm": 1.0,
            "max_interval_seconds": 7200,
            "dry_separation_seconds": 1800,
        },
        # 数据边界截断判定（前导/尾随零雨段）
        "RG-EDGE": {
            "tip_mm": 0.5,
            "max_interval_seconds": 7200,
            "dry_separation_seconds": 1800,
        },
    }}


def standard_rows() -> list[tuple[str, str, str]]:
    return [
        # ---- RG-A: 600s / 900s 不等间隔，含零雨段，手算 ----
        ("RG-A", "2026-06-01T00:00:00+00:00", "0"),    # 行 2
        ("RG-A", "2026-06-01T00:10:00+00:00", "4"),    # 行 3: d=4 -> 2.0mm
        ("RG-A", "2026-06-01T00:25:00+00:00", "4"),    # 行 4: 零雨
        ("RG-A", "2026-06-01T00:40:00+00:00", "10"),   # 行 5: d=6 -> 3.0mm

        # ---- RG-WRAP: 98->99 OK, 99->0 回绕, 0->2 OK, 98->2 非法 ----
        ("RG-WRAP", "2026-06-01T00:00:00+00:00", "98"),  # 行 6
        ("RG-WRAP", "2026-06-01T00:10:00+00:00", "99"),  # 行 7: 0.2mm
        ("RG-WRAP", "2026-06-01T00:20:00+00:00", "0"),   # 行 8: 回绕 d=1
        ("RG-WRAP", "2026-06-01T00:30:00+00:00", "2"),   # 行 9: d=2 -> 0.4
        ("RG-WRAP", "2026-06-01T00:40:00+00:00", "98"),  # 行 10: 大上升 OK
        ("RG-WRAP", "2026-06-01T00:50:00+00:00", "2"),   # 行 11: 下降非边界->重置

        # ---- RG-RESET: 无模数归零，新基准恢复，尾随零雨段 ----
        ("RG-RESET", "2026-06-01T00:00:00+00:00", "10"),  # 行 12
        ("RG-RESET", "2026-06-01T00:10:00+00:00", "15"),  # 行 13: 5mm
        ("RG-RESET", "2026-06-01T00:20:00+00:00", "0"),   # 行 14: 重置
        ("RG-RESET", "2026-06-01T00:30:00+00:00", "2"),   # 行 15: 新基准 2mm
        ("RG-RESET", "2026-06-01T00:40:00+00:00", "3"),   # 行 16: 1mm
        ("RG-RESET", "2026-06-01T00:50:00+00:00", "3"),   # 行 17: 零雨尾巴

        # ---- RG-GAP: 最大间隔 1800s，中间 6600s 空档 ----
        ("RG-GAP", "2026-06-01T00:00:00+00:00", "0"),    # 行 18
        ("RG-GAP", "2026-06-01T00:10:00+00:00", "4"),    # 行 19: 2mm
        ("RG-GAP", "2026-06-01T02:00:00+00:00", "5"),    # 行 20: 超长->无效
        ("RG-GAP", "2026-06-01T02:10:00+00:00", "7"),    # 行 21: 新基准 1mm

        # ---- RG-MAINT ----
        ("RG-MAINT", "2026-06-01T00:30:00+00:00", "0"),  # 行 22
        ("RG-MAINT", "2026-06-01T00:45:00+00:00", "10"), # 行 23: 5mm
        ("RG-MAINT", "2026-06-01T01:30:00+00:00", "20"), # 行 24: 跨检修->无效
        ("RG-MAINT", "2026-06-01T02:15:00+00:00", "25"), # 行 25: 仍相交->无效
        ("RG-MAINT", "2026-06-01T02:30:00+00:00", "30"), # 行 26: 2.5mm
        ("RG-MAINT", "2026-06-01T04:50:00+00:00", "31"), # 行 27: 0.5mm
        # [04:50,05:00) 与窗口 [05:00,05:30) 仅边界相碰，半开区间不相交
        ("RG-MAINT", "2026-06-01T05:00:00+00:00", "33"), # 行 28: 有效 1mm
        ("RG-MAINT", "2026-06-01T05:30:00+00:00", "34"), # 行 29: 边界相碰 0.5mm

        # ---- RG-TZ: +08:00 / +05:30 / Z 三种偏移，UTC 分别 00:00/00:30/01:00
        ("RG-TZ", "2026-06-01T08:00:00+08:00", "0"),     # 行 30 -> 00:00Z
        ("RG-TZ", "2026-06-01T06:00:00+05:30", "3"),     # 行 31 -> 00:30Z
        ("RG-TZ", "2026-06-01T01:00:00+00:00", "5"),     # 行 32 -> 01:00Z

        # ---- RG-DRY: 湿[00:00,00:10] 零1800s 湿[00:40,00:50] 同事件；
        #      之后零雨累计 >1800s 拆分，湿[01:30,...] 为第二场 ----
        ("RG-DRY", "2026-06-01T00:00:00+00:00", "0"),    # 行 33
        ("RG-DRY", "2026-06-01T00:10:00+00:00", "2"),    # 行 34: 2mm
        ("RG-DRY", "2026-06-01T00:40:00+00:00", "2"),    # 行 35: 零，间隔恰 1800s
        ("RG-DRY", "2026-06-01T00:50:00+00:00", "4"),    # 行 36: 2mm，仍同事件
        ("RG-DRY", "2026-06-01T01:20:00+00:00", "4"),    # 行 37: 零 1800s
        ("RG-DRY", "2026-06-01T01:21:00+00:00", "4"),    # 行 38: 零，累计 1860s 拆分
        ("RG-DRY", "2026-06-01T01:30:00+00:00", "6"),    # 行 39: 新事件 2mm

        # ---- RG-EDGE: 前导与尾随零雨段，事件不触数据边界 ----
        ("RG-EDGE", "2026-06-01T00:00:00+00:00", "5"),   # 行 40
        ("RG-EDGE", "2026-06-01T00:10:00+00:00", "5"),   # 行 41: 零
        ("RG-EDGE", "2026-06-01T00:20:00+00:00", "9"),   # 行 42: 2mm
        ("RG-EDGE", "2026-06-01T00:40:00+00:00", "9"),   # 行 43: 零尾巴
    ]


class TestSegmentCalculation(RainCase):
    """逐段口径：手算雨量、零雨、回绕、重置、空档、检修。"""

    def setUp(self) -> None:
        super().setUp()
        self.write_config(standard_config())
        self.write_rows(standard_rows())
        self.summary = self.run_pipeline()
        self.segments = self.read_segments()

    def test_hand_computed_rainfall_and_unequal_intervals(self):
        # RG-A 三个区间：600s 与 900s 不等间隔
        s1 = self.find_segment(
            self.segments, "2026-06-01T00:00:00Z",
            "2026-06-01T00:10:00Z", device="RG-A")
        self.assert_valid_rain(s1, delta=4, rain_mm=2.0)
        self.assertEqual(float(s1["interval_seconds"]), 600.0)
        self.assertEqual(s1["start_source_lines"], "2")
        self.assertEqual(s1["end_source_lines"], "3")

        s2 = self.find_segment(
            self.segments, "2026-06-01T00:10:00Z",
            "2026-06-01T00:25:00Z", device="RG-A")
        # 有效零雨段：valid=true 但雨量精确为 0，与无效段区分
        self.assertEqual(s2["valid"], "true")
        self.assertEqual(s2["quality_flag"], "ZERO_RAIN")
        self.assertEqual(int(s2["count_delta"]), 0)
        self.assertEqual(float(s2["rainfall_mm"]), 0.0)
        self.assertEqual(float(s2["interval_seconds"]), 900.0)

        s3 = self.find_segment(
            self.segments, "2026-06-01T00:25:00Z",
            "2026-06-01T00:40:00Z", device="RG-A")
        self.assert_valid_rain(s3, delta=6, rain_mm=3.0)
        self.assertEqual(float(s3["interval_seconds"]), 900.0)

    def test_modulus_wrap_only_accepts_boundary_crossing(self):
        wrap = self.find_segment(
            self.segments, "2026-06-01T00:10:00Z",
            "2026-06-01T00:20:00Z", device="RG-WRAP")
        # 手算回绕增量 = 模数 - 前读数 + 后读数 = 100 - 99 + 0 = 1
        self.assert_valid_rain(wrap, delta=1, rain_mm=0.2,
                               flag="COUNTER_WRAP")

        before = self.find_segment(
            self.segments, "2026-06-01T00:00:00Z",
            "2026-06-01T00:10:00Z", device="RG-WRAP")
        self.assert_valid_rain(before, delta=1, rain_mm=0.2)
        after = self.find_segment(
            self.segments, "2026-06-01T00:20:00Z",
            "2026-06-01T00:30:00Z", device="RG-WRAP")
        self.assert_valid_rain(after, delta=2, rain_mm=0.4)
        # 98 -> 2：虽然配置了模数，但没有严格跨过 99->0 边界，按重置处理
        bad = self.find_segment(
            self.segments, "2026-06-01T00:40:00Z",
            "2026-06-01T00:50:00Z", device="RG-WRAP")
        self.assert_invalid(bad, "COUNTER_RESET")

    def test_unconfigured_drop_is_reset_and_baseline_recovers(self):
        reset = self.find_segment(
            self.segments, "2026-06-01T00:10:00Z",
            "2026-06-01T00:20:00Z", device="RG-RESET")
        self.assert_invalid(reset, "COUNTER_RESET")

        # 重置后以新读数为基准恢复，雨量只算新基准之后的增量
        recovered = self.find_segment(
            self.segments, "2026-06-01T00:20:00Z",
            "2026-06-01T00:30:00Z", device="RG-RESET")
        self.assert_valid_rain(recovered, delta=2, rain_mm=2.0)
        next_seg = self.find_segment(
            self.segments, "2026-06-01T00:30:00Z",
            "2026-06-01T00:40:00Z", device="RG-RESET")
        self.assert_valid_rain(next_seg, delta=1, rain_mm=1.0)
        zero_tail = self.find_segment(
            self.segments, "2026-06-01T00:40:00Z",
            "2026-06-01T00:50:00Z", device="RG-RESET")
        self.assertEqual(zero_tail["quality_flag"], "ZERO_RAIN")

    def test_long_gap_splits_segment_and_keeps_baseline_after(self):
        gap = [s for s in self.segments if s["device_id"] == "RG-GAP"
               and s["quality_flag"] == "GAP_TOO_LONG"][0]
        self.assert_invalid(gap, "GAP_TOO_LONG")
        self.assertEqual(float(gap["interval_seconds"]), 6600.0)
        after = self.find_segment(
            self.segments, "2026-06-01T02:00:00Z",
            "2026-06-01T02:10:00Z", device="RG-GAP")
        self.assert_valid_rain(after, delta=2, rain_mm=1.0)

    def test_maintenance_window_and_half_open_boundary_touch(self):
        maint = [s for s in self.segments
                 if s["device_id"] == "RG-MAINT"
                 and s["quality_flag"] == "MAINTENANCE_WINDOW"]
        # 00:45->01:30、01:30->02:15 跨检修；05:00->05:30 与窗口完全重合
        self.assertEqual(len(maint), 3)
        for segment in maint:
            self.assert_invalid(segment, "MAINTENANCE_WINDOW")

        # [04:50,05:00) 与窗口 [05:00,05:30) 仅边界相碰：半开区间不相交
        touch = self.find_segment(
            self.segments, "2026-06-01T04:50:00Z",
            "2026-06-01T05:00:00Z", device="RG-MAINT")
        self.assert_valid_rain(touch, delta=2, rain_mm=1.0)
        # [05:00,05:30) 与检修窗口完全重合 -> 无效；05:30 是末读数
        invalid_end = [s for s in self.segments
                       if s["device_id"] == "RG-MAINT"
                       and s["interval_end_utc"]
                       == "2026-06-01T05:30:00Z"][0]
        self.assert_invalid(invalid_end, "MAINTENANCE_WINDOW")

    def test_timezone_offsets_normalized_to_utc(self):
        tz_segments = [s for s in self.segments if s["device_id"] == "RG-TZ"]
        self.assertEqual(len(tz_segments), 2)
        self.assertEqual(tz_segments[0]["interval_start_utc"],
                         "2026-06-01T00:00:00Z")
        self.assertEqual(tz_segments[0]["interval_end_utc"],
                         "2026-06-01T00:30:00Z")
        self.assertEqual(tz_segments[1]["interval_start_utc"],
                         "2026-06-01T00:30:00Z")
        self.assertEqual(tz_segments[1]["interval_end_utc"],
                         "2026-06-01T01:00:00Z")
        self.assert_valid_rain(tz_segments[0], delta=3, rain_mm=1.5)
        self.assert_valid_rain(tz_segments[1], delta=2, rain_mm=1.0)


class TestDuplicatesAndRejection(RainCase):

    def setUp(self) -> None:
        super().setUp()
        self.write_config(standard_config())

    def test_identical_duplicates_merged_with_all_source_lines(self):
        rows = [
            ("RG-A", "2026-06-01T00:00:00+00:00", "4"),       # 行 2
            ("RG-A", "2026-06-01T08:00:00+08:00", "4"),       # 行 3 同刻同值
            ("RG-A", "2026-06-01T00:10:00+00:00", "6"),       # 行 4
        ]
        self.write_rows(rows)
        summary = self.run_pipeline()
        self.assertEqual(summary["counts"]["raw_reading_records"], 3)
        self.assertEqual(summary["counts"]["merged_duplicate_records"], 1)
        self.assertEqual(summary["counts"]["unique_readings"], 2)
        segments = self.read_segments()
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["start_source_lines"], "2;3")
        self.assertEqual(segments[0]["end_source_lines"], "4")
        self.assert_valid_rain(segments[0], delta=2, rain_mm=1.0)

    def test_conflicting_duplicate_fails_and_leaves_no_outputs(self):
        self.write_rows([
            ("RG-A", "2026-06-01T00:00:00+00:00", "4"),
            ("RG-A", "2026-06-01T08:00:00+08:00", "5"),  # 同刻不同值
        ])
        with self.assertRaises(RainInputError) as ctx:
            self.run_pipeline()
        self.assertIn("冲突", str(ctx.exception))
        self.assertFalse(self.out_dir.exists())

    def test_naive_timestamp_rejected(self):
        self.write_rows([("RG-A", "2026-06-01T00:00:00", "4")])
        with self.assertRaises(RainInputError) as ctx:
            self.run_pipeline()
        self.assertIn("时区", str(ctx.exception))

    def test_unknown_device_rejected(self):
        self.write_rows([("RG-X", "2026-06-01T00:00:00+00:00", "4")])
        with self.assertRaises(RainInputError) as ctx:
            self.run_pipeline()
        self.assertIn("未知设备", str(ctx.exception))

    def test_negative_count_rejected(self):
        self.write_rows([("RG-A", "2026-06-01T00:00:00+00:00", "-1")])
        with self.assertRaises(RainInputError) as ctx:
            self.run_pipeline()
        self.assertIn("负读数", str(ctx.exception))

    def test_non_integer_count_is_soft_rejected_not_silently_dropped(self):
        self.write_rows([
            ("RG-A", "2026-06-01T00:00:00+00:00", "3.5"),  # 坏行，计入拒绝
            ("RG-A", "2026-06-01T00:10:00+00:00", "4"),
        ])
        summary = self.run_pipeline()
        self.assertEqual(summary["counts"]["rejected_records"], 1)
        self.assertEqual(summary["rejected_records"][0]["line"], 2)
        segments = self.read_segments()
        self.assertEqual(len(segments), 0)  # 只剩 1 条唯一读数，形不成区间

    def test_bad_csv_headers_rejected(self):
        # 缺列
        with open(self.readings_path, "w", newline="",
                  encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["device_id", "observed_at"])
            writer.writerow(["RG-A", "2026-06-01T00:00:00+00:00"])
        with self.assertRaises(RainInputError):
            self.run_pipeline()

    def test_failure_preserves_existing_outputs(self):
        # 先产出一版完整旧结果
        self.write_rows([
            ("RG-A", "2026-06-01T00:00:00+00:00", "0"),
            ("RG-A", "2026-06-01T00:10:00+00:00", "2"),
        ])
        self.run_pipeline()
        old_files = {p.name: p.read_bytes()
                     for p in self.out_dir.iterdir() if p.is_file()}
        self.assertEqual(set(old_files),
                         {SEGMENTS_FILENAME, EVENTS_FILENAME,
                          SUMMARY_FILENAME})

        # 再用冲突输入重跑：必须失败且旧结果逐字节不变、无临时残留
        self.write_rows([
            ("RG-A", "2026-06-01T00:00:00+00:00", "9"),
            ("RG-A", "2026-06-01T00:00:00+00:00", "8"),
        ])
        with self.assertRaises(RainInputError):
            self.run_pipeline()
        current_files = {p.name: p.read_bytes()
                         for p in self.out_dir.iterdir() if p.is_file()}
        self.assertEqual(current_files, old_files)
        leftovers = [p.name for p in self.out_dir.iterdir()
                     if p.name.endswith((".tmp", ".bak"))]
        self.assertEqual(leftovers, [])


class TestConfigValidation(RainCase):

    def base_device(self) -> dict:
        return {
            "tip_mm": 0.5,
            "max_interval_seconds": 3600,
            "dry_separation_seconds": 1800,
        }

    def assert_config_rejected(self, config: dict, needle: str = ""):
        self.write_config(config)
        self.write_rows([
            ("RG-A", "2026-06-01T00:00:00+00:00", "0"),
            ("RG-A", "2026-06-01T00:10:00+00:00", "1"),
        ])
        with self.assertRaises(RainConfigError) as ctx:
            self.run_pipeline()
        if needle:
            self.assertIn(needle, str(ctx.exception))

    def test_missing_field_negative_zero_and_unknown_key(self):
        cfg = {"devices": {"RG-A": {k: v for k, v in
                                    self.base_device().items()
                                    if k != "tip_mm"}}}
        self.assert_config_rejected(cfg, "tip_mm")

        cfg = {"devices": {"RG-A": {**self.base_device(), "tip_mm": 0}}}
        self.assert_config_rejected(cfg, "有限正数")

        cfg = {"devices": {"RG-A": {**self.base_device(),
                                    "max_interval_seconds": -1}}}
        self.assert_config_rejected(cfg, "有限正数")

        cfg = {"devices": {"RG-A": {**self.base_device(),
                                    "unexpected": 1}}}
        self.assert_config_rejected(cfg, "未知")

    def test_modulus_and_maintenance_rules(self):
        cfg = {"devices": {"RG-A": {**self.base_device(),
                                    "counter_modulus": 1}}}
        self.assert_config_rejected(cfg, "counter_modulus")

        cfg = {"devices": {"RG-A": {**self.base_device(),
                                    "counter_modulus": 100.0}}}
        self.assert_config_rejected(cfg, "整数")

        cfg = {"devices": {"RG-A": {
            **self.base_device(),
            "maintenance": [
                {"start": "2026-06-01T02:00:00+00:00",
                 "end": "2026-06-01T01:00:00+00:00"}],
        }}}
        self.assert_config_rejected(cfg, "start 必须早于 end")

        cfg = {"devices": {"RG-A": {
            **self.base_device(),
            "maintenance": [
                {"start": "2026-06-01T01:00:00",  # 检修时间也必须带偏移
                 "end": "2026-06-01T02:00:00+00:00"}],
        }}}
        self.assert_config_rejected(cfg, "时区")

    def test_empty_devices_rejected(self):
        self.assert_config_rejected({"devices": {}})


class TestEvents(RainCase):

    def setUp(self) -> None:
        super().setUp()
        self.write_config(standard_config())
        self.write_rows(standard_rows())
        self.run_pipeline()
        self.events = self.read_events()
        self.segments = self.read_segments()

    def events_of(self, device: str) -> list[dict]:
        return [e for e in self.events if e["device_id"] == device]

    def test_dry_separation_boundary_merge_and_split(self):
        events = self.events_of("RG-DRY")
        self.assertEqual(len(events), 2)
        # 事件 1：==1800s 干旱不拆，两个湿段合计 4mm，区间跨 [00:00,00:50)
        e1, e2 = events
        self.assertEqual(e1["event_start_utc"], "2026-06-01T00:00:00Z")
        self.assertEqual(e1["event_end_utc"], "2026-06-01T00:50:00Z")
        self.assertAlmostEqual(float(e1["total_rainfall_mm"]), 4.0)
        self.assertAlmostEqual(float(e1["max_segment_rainfall_mm"]), 2.0)
        self.assertEqual(int(e1["segment_count"]), 2)
        # 事件 2：累计干旱 >1800s 后另起一场（湿段 [01:21,01:30)）
        self.assertEqual(e2["event_start_utc"], "2026-06-01T01:21:00Z")
        self.assertEqual(e2["event_end_utc"], "2026-06-01T01:30:00Z")
        self.assertAlmostEqual(float(e2["total_rainfall_mm"]), 2.0)
        self.assertEqual(int(e2["segment_count"]), 1)

    def test_invalid_and_gap_truncate_events_never_joining_both_sides(self):
        # RG-RESET：重置段把两侧湿段切成两场
        reset_events = self.events_of("RG-RESET")
        self.assertEqual(len(reset_events), 2)
        self.assertAlmostEqual(
            float(reset_events[0]["total_rainfall_mm"]), 5.0)
        self.assertAlmostEqual(
            float(reset_events[1]["total_rainfall_mm"]), 3.0)
        # 第二场前后都有有效读数（前有重置段、后有零雨尾巴），不触数据边界
        self.assertEqual(
            reset_events[1]["truncated_by_data_boundary"], "false")

        # RG-GAP：超长空档两侧各成一场，且都贴着数据边界
        gap_events = self.events_of("RG-GAP")
        self.assertEqual(len(gap_events), 2)
        for event in gap_events:
            self.assertEqual(
                event["truncated_by_data_boundary"], "true")

    def test_event_with_leading_and_trailing_zero_segments_not_truncated(self):
        events = self.events_of("RG-EDGE")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event_start_utc"],
                         "2026-06-01T00:10:00Z")
        self.assertEqual(event["event_end_utc"], "2026-06-01T00:20:00Z")
        self.assertEqual(event["truncated_by_data_boundary"], "false")
        self.assertAlmostEqual(float(event["total_rainfall_mm"]), 2.0)
        self.assertAlmostEqual(
            float(event["duration_seconds"]), 600.0)

    def test_maintenance_creates_separate_events(self):
        events = self.events_of("RG-MAINT")
        # 检修窗口 [01:00,02:00) 把雨切成检修前 / 检修后两组；
        # 任何事件的时间范围都不得与检修窗口相交
        self.assertGreaterEqual(len(events), 2)
        for event in events:
            start = datetime.fromisoformat(
                event["event_start_utc"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(
                event["event_end_utc"].replace("Z", "+00:00"))
            for window in standard_config()["devices"]["RG-MAINT"][
                    "maintenance"]:
                w_start = datetime.fromisoformat(window["start"])
                w_end = datetime.fromisoformat(window["end"])
                self.assertFalse(
                    start < w_end and w_start < end,
                    f"事件 {event} 跨过检修窗口 {window}")

    def test_event_source_lines_trace_back_to_raw_rows(self):
        for event in self.events:
            lines = [int(x) for x in event["source_lines"].split(";")]
            self.assertEqual(lines, sorted(lines))
            self.assertTrue(all(line >= 2 for line in lines))


class TestProvenanceAndStability(RainCase):

    def test_summary_hashes_and_count_reconciliation(self):
        self.write_config(standard_config())
        self.write_rows(standard_rows())
        summary = self.run_pipeline()

        self.assertEqual(
            summary["inputs"]["readings_csv"]["sha256"],
            sha256_file(self.readings_path))
        self.assertEqual(
            summary["inputs"]["device_config_json"]["sha256"],
            sha256_file(self.cfg_path))

        counts = summary["counts"]
        # 原始记录 = 去重后读数 + 合并掉的重复 + 软拒绝行
        self.assertEqual(
            counts["raw_reading_records"],
            counts["unique_readings"]
            + counts["merged_duplicate_records"]
            + counts["rejected_records"])
        self.assertEqual(
            counts["segments_total"],
            counts["segments_valid"] + counts["segments_invalid"])

        # 标记计数之和 == 段总数；各标记计数与逐段文件一致
        flag_total = sum(summary["quality_flag_counts"].values())
        self.assertEqual(flag_total, counts["segments_total"])
        segments = self.read_segments()
        for flag, n in summary["quality_flag_counts"].items():
            self.assertEqual(
                sum(1 for s in segments if s["quality_flag"] == flag), n)

    def test_every_segment_traces_to_two_raw_records(self):
        self.write_config(standard_config())
        self.write_rows(standard_rows())
        self.run_pipeline()
        raw_lines = set()
        with open(self.readings_path, encoding="utf-8", newline="") as fh:
            for line_no, _ in enumerate(fh, start=1):
                raw_lines.add(line_no)
        for segment in self.read_segments():
            for key in ("start_source_lines", "end_source_lines"):
                for line in segment[key].split(";"):
                    self.assertIn(int(line), raw_lines)

    def test_shuffled_input_produces_identical_segment_and_event_outputs(self):
        self.write_config(standard_config())
        rows = standard_rows()
        self.write_rows(rows)
        self.run_pipeline()
        first_segments = self._segments_without_line_columns(
            self.out_dir / SEGMENTS_FILENAME)
        first_events = self._events_without_line_columns(
            self.out_dir / EVENTS_FILENAME)
        first_summary = self.read_summary()

        # 确定性乱序：固定步长重排，保证与原序显著不同
        shuffled = rows[::3] + rows[1::3] + rows[2::3]
        self.assertNotEqual(shuffled, rows)
        second_dir = self.tmp / "out2"
        second_csv = self.tmp / "shuffled.csv"
        with open(second_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(HEADER)
            writer.writerows(shuffled)
        second_summary = process_rainfall(
            second_csv, self.cfg_path, second_dir)

        # 业务列（区间、计数、雨量、标记、有效性）逐字节一致；
        # 仅溯源行号随输入物理行变化，单独核对其指向的记录等价
        self.assertEqual(
            self._segments_without_line_columns(
                second_dir / SEGMENTS_FILENAME),
            first_segments)
        self.assertEqual(
            self._events_without_line_columns(
                second_dir / EVENTS_FILENAME),
            first_events)
        # 物理行号随输入重排必然变化；可追溯性不变量是：两次运行引用的
        # 行号并集都恰好覆盖全部原始数据行（表头行 1 除外），且每个被引用
        # 行对应的 (设备, 时刻, 读数) 三元组集合一致。
        def referenced_lines(path):
            used: set[int] = set()
            with open(path, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    for column in ("start_source_lines", "end_source_lines"):
                        used.update(int(x) for x in row[column].split(";"))
            return used

        used_first = referenced_lines(self.out_dir / SEGMENTS_FILENAME)
        used_second = referenced_lines(
            second_dir / SEGMENTS_FILENAME)
        n_data_lines = len(rows) + 1  # 含表头
        self.assertEqual(used_first, set(range(2, n_data_lines + 1)))
        self.assertEqual(used_second, set(range(2, n_data_lines + 1)))
        # 除输入文件指纹/路径外，摘要内容完全一致
        for key in ("config", "counts", "quality_flag_counts", "per_device",
                    "rejected_records"):
            self.assertEqual(second_summary[key], first_summary[key])

    @staticmethod
    def _segments_without_line_columns(path: Path) -> list[dict]:
        with open(path, encoding="utf-8", newline="") as fh:
            return [
                {k: v for k, v in row.items()
                 if k not in ("start_source_lines", "end_source_lines")}
                for row in csv.DictReader(fh)]

    @staticmethod
    def _events_without_line_columns(path: Path) -> list[dict]:
        with open(path, encoding="utf-8", newline="") as fh:
            return [
                {k: v for k, v in row.items() if k != "source_lines"}
                for row in csv.DictReader(fh)]

    def test_utc_sorting_is_stable_and_output_is_deterministic(self):
        self.write_config(standard_config())
        rows = standard_rows()
        self.write_rows(list(reversed(rows)))
        self.run_pipeline()
        reversed_segments = self._segments_without_line_columns(
            self.out_dir / SEGMENTS_FILENAME)

        self.write_rows(rows)
        out2 = self.tmp / "out3"
        process_rainfall(self.readings_path, self.cfg_path, out2)
        self.assertEqual(
            self._segments_without_line_columns(
                out2 / SEGMENTS_FILENAME),
            reversed_segments)


class TestBoundaryAndEdgeCases(RainCase):

    def test_interval_equal_to_max_is_still_valid(self):
        # 严格大于 max_interval 才算超长；相等必须有效
        self.write_config({"devices": {"RG-A": {
            "tip_mm": 0.5, "max_interval_seconds": 600,
            "dry_separation_seconds": 300}}})
        self.write_rows([
            ("RG-A", "2026-06-01T00:00:00+00:00", "0"),
            ("RG-A", "2026-06-01T00:10:00+00:00", "2"),   # 恰 600s
            ("RG-A", "2026-06-01T00:20:01+00:00", "4"),   # 601s 超长
        ])
        self.run_pipeline()
        segments = self.read_segments()
        self.assertEqual(segments[0]["quality_flag"], "OK")
        self.assertEqual(segments[1]["quality_flag"], "GAP_TOO_LONG")

    def test_blank_lines_ignored_soft_rejects_counted_single_reading_device(self):
        self.write_config(standard_config())
        with open(self.readings_path, "w", newline="",
                  encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(HEADER)
            writer.writerow(("RG-A", "2026-06-01T00:00:00+00:00", "0"))
            writer.writerow(())  # 完全空白行：不计读数也不计拒绝
            writer.writerow(("RG-A", "2026-06-01T00:10:00+00:00", "x"))
            writer.writerow(("RG-A", "2026-06-01T00:20:00+00:00", "4"))
            writer.writerow(("RG-WRAP", "2026-06-01T00:00:00+00:00", "9"))
        summary = self.run_pipeline()
        self.assertEqual(summary["counts"]["raw_reading_records"], 4)
        self.assertEqual(summary["counts"]["rejected_records"], 1)
        self.assertEqual(summary["rejected_records"][0]["line"], 4)
        # RG-A 坏行被剔除后，00:00 与 00:20 两条读数仍能形成一个有效段
        segments = self.read_segments()
        rg_a = [s for s in segments if s["device_id"] == "RG-A"]
        self.assertEqual(len(rg_a), 1)
        self.assertEqual(rg_a[0]["quality_flag"], "OK")
        # 单读数设备：没有区间也没有事件，但计入设备统计
        self.assertEqual(summary["per_device"]["RG-WRAP"]["readings"], 1)
        self.assertEqual(summary["per_device"]["RG-WRAP"]["segments"], 0)
        self.assertEqual(
            [e for e in self.read_events() if e["device_id"] == "RG-WRAP"],
            [])
        # RG-A 剔除坏行后仍有一个有效湿段，对应一场被边界截断的事件
        rg_a_events = [e for e in self.read_events()
                       if e["device_id"] == "RG-A"]
        self.assertEqual(len(rg_a_events), 1)
        self.assertEqual(
            rg_a_events[0]["truncated_by_data_boundary"], "true")

    def test_rerun_is_idempotent_and_rolls_back_on_later_failure(self):
        self.write_config(standard_config())
        self.write_rows([
            ("RG-A", "2026-06-01T00:00:00+00:00", "0"),
            ("RG-A", "2026-06-01T00:10:00+00:00", "2"),
        ])
        self.run_pipeline()
        first = {p.name: p.read_bytes()
                 for p in self.out_dir.iterdir() if p.is_file()}
        # 同输入重跑：输出逐字节一致
        self.run_pipeline()
        second = {p.name: p.read_bytes()
                  for p in self.out_dir.iterdir() if p.is_file()}
        self.assertEqual(second, first)


class TestCli(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rain-cli-"))
        self.cfg = self.tmp / "devices.json"
        self.csv_path = self.tmp / "readings.csv"
        self.out = self.tmp / "out"
        self.cfg.write_text(json.dumps(standard_config()), encoding="utf-8")
        with open(self.csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(HEADER)
            writer.writerows(standard_rows())

    def test_parser_registers_rainfall_help(self):
        parser = build_parser()
        args = parser.parse_args([
            "rainfall", "--readings", str(self.csv_path),
            "--config", str(self.cfg), "--output-dir", str(self.out)])
        self.assertEqual(args.command, "rainfall")

    def test_main_success_and_distinct_failure_exit_codes(self):
        rc = main(["rainfall", "--readings", str(self.csv_path),
                   "--config", str(self.cfg), "--output-dir", str(self.out)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.out / SEGMENTS_FILENAME).exists())

        # 输入冲突 -> 退出码 3
        bad_csv = self.tmp / "bad.csv"
        with open(bad_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(HEADER)
            writer.writerow(("RG-A", "2026-06-01T00:00:00+00:00", "1"))
            writer.writerow(("RG-A", "2026-06-01T00:00:00+00:00", "2"))
        rc = main(["rainfall", "--readings", str(bad_csv),
                   "--config", str(self.cfg),
                   "--output-dir", str(self.tmp / "bad-out")])
        self.assertEqual(rc, 3)

        # 配置错误 -> 退出码 2
        bad_cfg = self.tmp / "bad.json"
        bad_cfg.write_text(json.dumps({"devices": {}}), encoding="utf-8")
        rc = main(["rainfall", "--readings", str(self.csv_path),
                   "--config", str(bad_cfg),
                   "--output-dir", str(self.tmp / "bad-out2")])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
