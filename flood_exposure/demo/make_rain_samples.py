"""生成雨量整理的合成读数/配置，并以独立手算断言核对完整处理链。

用法（从仓库根目录，隔离模式运行，不加载当前目录的同名模块）::

    python3 -I demo/make_rain_samples.py [输出目录]

场景全部由手算字面量给出，断言不调用被测模块的计算函数，只读取最终产出的
三个文件做独立核对，覆盖：

* 不等时间间隔的手算降雨量与有效零雨段
* 跨模数回绕（严格跨高/低边界）与未配置回绕的意外归零、新基准恢复
* 超长缺测与检修窗口（含半开边界相碰）导致的分段及事件截断
* 不同时区偏移换算 UTC、完全相同重复行合并、冲突重复行拒绝
* 干旱分隔时长边界两侧（== 阈值合并，> 阈值拆分）
* 乱序输入结果稳定，且每个输出区间都能回查原始记录行
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from flood_exposure.cli import main  # noqa: E402
from flood_exposure.hashing import sha256_file  # noqa: E402
from flood_exposure.rain import (  # noqa: E402
    EVENTS_FILENAME,
    SEGMENTS_FILENAME,
    SUMMARY_FILENAME,
    RainInputError,
    process_rainfall,
)

HEADER = ["device_id", "observed_at", "cumulative_count"]

# 设备配置（与 tests/test_rainfall.py 的夹具独立维护，避免共享被测口径）
DEVICE_CONFIG = {
    "RG-A": {  # 不等间隔 + 零雨段
        "tip_mm": 0.5, "max_interval_seconds": 3600,
        "dry_separation_seconds": 1800,
    },
    "RG-WRAP": {  # 模数 100
        "tip_mm": 0.2, "max_interval_seconds": 7200,
        "dry_separation_seconds": 1800, "counter_modulus": 100,
    },
    "RG-RESET": {  # 无模数：下降即重置
        "tip_mm": 1.0, "max_interval_seconds": 7200,
        "dry_separation_seconds": 1800,
    },
    "RG-GAP": {  # 最大间隔 1800s
        "tip_mm": 0.5, "max_interval_seconds": 1800,
        "dry_separation_seconds": 600,
    },
    "RG-MAINT": {  # 检修 [01:00,02:00) 与 [05:00,05:30)
        "tip_mm": 0.5, "max_interval_seconds": 7200,
        "dry_separation_seconds": 1800,
        "maintenance": [
            {"start": "2026-06-01T01:00:00+00:00",
             "end": "2026-06-01T02:00:00+00:00"},
            {"start": "2026-06-01T05:00:00+00:00",
             "end": "2026-06-01T05:30:00+00:00"},
        ],
    },
    "RG-TZ": {
        "tip_mm": 0.5, "max_interval_seconds": 7200,
        "dry_separation_seconds": 1800,
    },
    "RG-DRY": {  # 干旱边界 1800s
        "tip_mm": 1.0, "max_interval_seconds": 7200,
        "dry_separation_seconds": 1800,
    },
}

# 故意乱序排列，验证“给定乱序输入时结果应稳定一致”
READINGS = [
    ("RG-DRY", "2026-06-01T01:30:00+00:00", "6"),
    ("RG-A", "2026-06-01T00:25:00+00:00", "4"),    # 零雨
    ("RG-WRAP", "2026-06-01T00:10:00+00:00", "99"),
    ("RG-GAP", "2026-06-01T02:10:00+00:00", "7"),
    ("RG-RESET", "2026-06-01T00:30:00+00:00", "2"),
    ("RG-MAINT", "2026-06-01T02:30:00+00:00", "30"),
    ("RG-TZ", "2026-06-01T01:00:00+00:00", "5"),   # UTC 01:00
    ("RG-DRY", "2026-06-01T00:10:00+00:00", "2"),
    ("RG-WRAP", "2026-06-01T00:20:00+00:00", "0"),  # 回绕 99->0
    ("RG-A", "2026-06-01T00:40:00+00:00", "10"),
    ("RG-GAP", "2026-06-01T00:10:00+00:00", "4"),
    ("RG-RESET", "2026-06-01T00:20:00+00:00", "0"),  # 意外归零
    ("RG-MAINT", "2026-06-01T05:30:00+00:00", "34"),  # 与窗口边界相碰
    ("RG-DRY", "2026-06-01T00:50:00+00:00", "4"),
    ("RG-WRAP", "2026-06-01T00:00:00+00:00", "98"),
    ("RG-GAP", "2026-06-01T02:00:00+00:00", "5"),    # 超长空档后
    ("RG-RESET", "2026-06-01T00:40:00+00:00", "3"),
    ("RG-MAINT", "2026-06-01T05:00:00+00:00", "33"),  # 边界相碰段有效
    ("RG-DRY", "2026-06-01T01:21:00+00:00", "4"),    # 累计干旱 1860s
    ("RG-A", "2026-06-01T00:10:00+00:00", "4"),
    ("RG-WRAP", "2026-06-01T00:30:00+00:00", "2"),
    ("RG-GAP", "2026-06-01T00:00:00+00:00", "0"),
    ("RG-RESET", "2026-06-01T00:10:00+00:00", "15"),
    ("RG-MAINT", "2026-06-01T04:50:00+00:00", "31"),
    ("RG-DRY", "2026-06-01T00:00:00+00:00", "0"),
    ("RG-A", "2026-06-01T00:00:00+00:00", "0"),
    # 完全相同的重复行（不同写法的同一 UTC 时刻），应合并
    ("RG-TZ", "2026-06-01T08:00:00+08:00", "0"),
    ("RG-TZ", "2026-06-01T00:00:00+00:00", "0"),
    ("RG-WRAP", "2026-06-01T00:40:00+00:00", "98"),
    ("RG-RESET", "2026-06-01T00:00:00+00:00", "10"),
    ("RG-MAINT", "2026-06-01T02:15:00+00:00", "25"),  # 仍在检修内
    ("RG-DRY", "2026-06-01T00:40:00+00:00", "2"),    # 零，间隔恰 1800s
    ("RG-WRAP", "2026-06-01T00:50:00+00:00", "2"),   # 98->2 非边界=重置
    ("RG-MAINT", "2026-06-01T01:30:00+00:00", "20"),  # 跨检修
    ("RG-DRY", "2026-06-01T01:20:00+00:00", "4"),    # 零，累计 1800s
    ("RG-MAINT", "2026-06-01T00:45:00+00:00", "10"),
    ("RG-RESET", "2026-06-01T00:50:00+00:00", "3"),  # 零雨尾巴
    ("RG-TZ", "2026-06-01T06:00:00+05:30", "3"),     # UTC 00:30
    ("RG-MAINT", "2026-06-01T00:30:00+00:00", "0"),
]


def write_inputs(base: Path) -> tuple[Path, Path]:
    base.mkdir(parents=True, exist_ok=True)
    cfg_path = base / "devices.json"
    csv_path = base / "readings.csv"
    cfg_path.write_text(
        json.dumps({"devices": DEVICE_CONFIG}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(READINGS)
    return cfg_path, csv_path


def load_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"独立断言失败: {message}")
    print(f"  ✓ {message}")


def independent_assertions(out_dir: Path, cfg_path: Path,
                           csv_path: Path, summary: dict) -> None:
    segments = load_csv(out_dir / SEGMENTS_FILENAME)
    events = load_csv(out_dir / EVENTS_FILENAME)

    def seg(device, start, end):
        rows = [s for s in segments if s["device_id"] == device
                and s["interval_start_utc"] == start
                and s["interval_end_utc"] == end]
        check(len(rows) == 1, f"{device} 区间 {start}/{end} 唯一")
        return rows[0]

    print("[1] 不等间隔手算雨量与有效零雨段")
    s = seg("RG-A", "2026-06-01T00:00:00Z", "2026-06-01T00:10:00Z")
    check(s["valid"] == "true" and s["quality_flag"] == "OK"
          and float(s["rainfall_mm"]) == 4 * 0.5,
          "00:00->00:10 增量 4 斗 × 0.5mm = 2.0mm (600s 区间)")
    s = seg("RG-A", "2026-06-01T00:10:00Z", "2026-06-01T00:25:00Z")
    check(s["valid"] == "true" and s["quality_flag"] == "ZERO_RAIN"
          and float(s["rainfall_mm"]) == 0.0,
          "读数不变是有效零雨段 (900s 区间)，与无效段区分")
    s = seg("RG-A", "2026-06-01T00:25:00Z", "2026-06-01T00:40:00Z")
    check(float(s["rainfall_mm"]) == 6 * 0.5,
          "00:25->00:40 增量 6 斗 × 0.5mm = 3.0mm")

    print("[2] 模数回绕与非法下降")
    s = seg("RG-WRAP", "2026-06-01T00:10:00Z", "2026-06-01T00:20:00Z")
    check(s["quality_flag"] == "COUNTER_WRAP" and s["valid"] == "true"
          and int(s["count_delta"]) == 100 - 99 + 0
          and float(s["rainfall_mm"]) == 0.2,
          "99->0 严格跨模数边界：回绕增量 1 斗 = 0.2mm")
    s = seg("RG-WRAP", "2026-06-01T00:40:00Z", "2026-06-01T00:50:00Z")
    check(s["valid"] == "false" and s["quality_flag"] == "COUNTER_RESET"
          and s["rainfall_mm"] == "",
          "98->2 未跨 99->0 边界：即使有模数也按重置，段无效且无雨量")

    print("[3] 未配置回绕的意外归零与新基准恢复")
    s = seg("RG-RESET", "2026-06-01T00:10:00Z", "2026-06-01T00:20:00Z")
    check(s["valid"] == "false" and s["quality_flag"] == "COUNTER_RESET",
          "无模数设备 15->0：重置，禁止跨段相减")
    s = seg("RG-RESET", "2026-06-01T00:20:00Z", "2026-06-01T00:30:00Z")
    check(s["valid"] == "true" and float(s["rainfall_mm"]) == 2.0,
          "以新读数 0 重建基准：0->2 = 2.0mm")

    print("[4] 超长缺测分段")
    s = seg("RG-GAP", "2026-06-01T00:10:00Z", "2026-06-01T02:00:00Z")
    check(s["valid"] == "false" and s["quality_flag"] == "GAP_TOO_LONG"
          and float(s["interval_seconds"]) == 6600.0,
          "6600s > 最大间隔 1800s：无效，不跨空档相减")
    s = seg("RG-GAP", "2026-06-01T02:00:00Z", "2026-06-01T02:10:00Z")
    check(s["valid"] == "true" and float(s["rainfall_mm"]) == 1.0,
          "空档后从后一条有效读数重新开始：5->7 = 1.0mm")

    print("[5] 检修窗口与半开边界")
    s = seg("RG-MAINT", "2026-06-01T00:45:00Z", "2026-06-01T01:30:00Z")
    check(s["valid"] == "false" and s["quality_flag"] == "MAINTENANCE_WINDOW",
          "区间与检修 [01:00,02:00) 相交：无效")
    s = seg("RG-MAINT", "2026-06-01T04:50:00Z", "2026-06-01T05:00:00Z")
    check(s["valid"] == "true" and float(s["rainfall_mm"]) == 1.0,
          "[04:50,05:00) 与窗口仅边界相碰：半开区间不相交，仍有效")
    s = seg("RG-MAINT", "2026-06-01T05:00:00Z", "2026-06-01T05:30:00Z")
    check(s["valid"] == "false" and s["quality_flag"] == "MAINTENANCE_WINDOW",
          "[05:00,05:30) 与检修窗口完全重合：无效")

    print("[6] 时区换算与重复合并")
    tz = [s for s in segments if s["device_id"] == "RG-TZ"]
    check([(s["interval_start_utc"], s["interval_end_utc"]) for s in tz]
          == [("2026-06-01T00:00:00Z", "2026-06-01T00:30:00Z"),
              ("2026-06-01T00:30:00Z", "2026-06-01T01:00:00Z")],
          "+08:00 / +05:30 / +00:00 统一换算为 UTC 后排序")
    check(tz[0]["start_source_lines"] != tz[0]["end_source_lines"]
          and ";" in tz[0]["start_source_lines"],
          "同刻同值的两条记录已合并，区间起点保留两个原始行号")
    check(summary["counts"]["merged_duplicate_records"] == 1,
          "摘要记录合并数量 = 1")

    print("[7] 干旱分隔边界的事件合并与拆分")
    dry_events = [e for e in events if e["device_id"] == "RG-DRY"]
    check(len(dry_events) == 2, "RG-DRY 归并为 2 场事件")
    e1, e2 = dry_events
    check(e1["event_start_utc"] == "2026-06-01T00:00:00Z"
          and e1["event_end_utc"] == "2026-06-01T00:50:00Z"
          and float(e1["total_rainfall_mm"]) == 4.0
          and int(e1["segment_count"]) == 2,
          "干旱恰为 1800s（== 阈值）不拆分，两场雨合并为同一事件")
    check(e2["event_start_utc"] == "2026-06-01T01:21:00Z"
          and float(e2["total_rainfall_mm"]) == 2.0,
          "累计干旱 1860s（> 阈值）后另起事件")

    print("[8] 无效段截断事件，不拼接两侧的雨")
    reset_events = [e for e in events if e["device_id"] == "RG-RESET"]
    check([float(e["total_rainfall_mm"]) for e in reset_events] == [5.0, 3.0],
          "重置段两侧各成一场 (5.0mm / 重置后 2+1=3.0mm)，绝不拼成一场")
    gap_events = [e for e in events if e["device_id"] == "RG-GAP"]
    check(len(gap_events) == 2
          and all(e["truncated_by_data_boundary"] == "true"
                  for e in gap_events),
          "超长空档两侧事件均标记为数据边界截断")
    edge_event = [e for e in events if e["device_id"] == "RG-A"][0]
    check(edge_event["truncated_by_data_boundary"] == "true",
          "事件触及输入首条读数：标记数据边界截断")

    print("[9] 全程可追溯：每个区间都对应原始记录行")
    n_data_lines = len(READINGS)  # 去重前的数据行数（含 1 条重复）
    referenced: set[int] = set()
    for s in segments:
        for column in ("start_source_lines", "end_source_lines"):
            lines = [int(x) for x in s[column].split(";")]
            check(all(2 <= x <= n_data_lines + 1 for x in lines),
                  f"{s['device_id']} 区间行号落在原始数据行范围内")
            referenced.update(lines)
    # 物理行：表头占第 1 行，数据行是 2..n_data_lines+1；
    # 合并的重复行也通过 start_source_lines 保留，因此 39 行全部应被引用
    check(referenced == set(range(2, n_data_lines + 2)),
          "全部原始数据行（含被合并的重复行）都至少被一个区间引用")

    print("[10] 摘要指纹与计数勾稽")
    check(summary["inputs"]["readings_csv"]["sha256"]
          == sha256_file(csv_path),
          "摘要记录读数 CSV 的 SHA256")
    check(summary["inputs"]["device_config_json"]["sha256"]
          == sha256_file(cfg_path),
          "摘要记录配置 JSON 的 SHA256")
    c = summary["counts"]
    check(c["raw_reading_records"]
          == c["unique_readings"] + c["merged_duplicate_records"]
          + c["rejected_records"],
          "原始/唯一/合并/拒绝记录数勾稽一致")
    check(sum(summary["quality_flag_counts"].values())
          == c["segments_total"],
          "质量标记计数之和等于段总数")


def demonstrate_rejections(base: Path, cfg_path: Path) -> None:
    print("[11] 冲突重复行与缺时区时间必须失败且不产出结果")
    bad_dir = base / "should_not_exist"
    bad_csv = base / "conflict.csv"
    with open(bad_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerow(("RG-A", "2026-06-01T00:00:00+00:00", "1"))
        writer.writerow(("RG-A", "2026-06-01T00:00:00+00:00", "2"))
    try:
        process_rainfall(bad_csv, cfg_path, bad_dir)
    except RainInputError as exc:
        check("冲突" in str(exc), f"同刻不同读数被拒绝: {exc}")
    else:
        raise AssertionError("冲突重复行未触发失败")
    check(not bad_dir.exists(), "失败后不留下任何输出目录")

    naive_csv = base / "naive.csv"
    with open(naive_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerow(("RG-A", "2026-06-01T00:00:00", "1"))
    try:
        process_rainfall(naive_csv, cfg_path, base / "should_not_exist2")
    except RainInputError as exc:
        check("时区" in str(exc), f"缺时区时间被拒绝: {exc}")

    print("[12] CLI 退出码：成功 0 / 输入冲突 3 / 配置错误 2")
    out_dir = base / "cli_out"
    rc = main(["rainfall", "--readings", str(base / "readings.csv"),
               "--config", str(cfg_path), "--output-dir", str(out_dir)])
    check(rc == 0 and (out_dir / SUMMARY_FILENAME).exists(),
          "CLI 正常运行退出码 0 且三个结果文件齐全")
    rc = main(["rainfall", "--readings", str(bad_csv),
               "--config", str(cfg_path),
               "--output-dir", str(base / "cli_bad")])
    check(rc == 3, "输入冲突退出码 3")
    bad_cfg = base / "bad_devices.json"
    bad_cfg.write_text(json.dumps({"devices": {}}), encoding="utf-8")
    rc = main(["rainfall", "--readings", str(base / "readings.csv"),
               "--config", str(bad_cfg),
               "--output-dir", str(base / "cli_bad2")])
    check(rc == 2, "非法配置退出码 2")


def main_demo(argv: list[str]) -> int:
    base = Path(argv[1]) if len(argv) > 1 \
        else Path(tempfile.mkdtemp(prefix="rain-samples-"))
    print(f"合成数据目录: {base}")
    cfg_path, csv_path = write_inputs(base)
    out_dir = base / "rainfall_output"
    print(f"配置: {cfg_path}\n读数: {csv_path}（{len(READINGS)} 行，故意乱序）")
    summary = process_rainfall(csv_path, cfg_path, out_dir)
    print(f"产出: {out_dir}/{{{SEGMENTS_FILENAME}, {EVENTS_FILENAME}, "
          f"{SUMMARY_FILENAME}}}\n")
    independent_assertions(out_dir, cfg_path, csv_path, summary)
    demonstrate_rejections(base, cfg_path)
    print("\n全部独立断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_demo(sys.argv))
