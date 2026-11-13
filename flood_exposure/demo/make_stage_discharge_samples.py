"""生成 stage-discharge 合成样例，并以独立手算值核对完整处理链。

用法（从仓库根目录，隔离模式运行，不加载当前目录的同名模块）::

    python3 -I demo/make_stage_discharge_samples.py [输出目录]

两个测站、三组曲线版本覆盖需求中的全部场景：

* R1 曲线 V1 有效 [01-01, 02-01)，V2 有效 [03-01, 开放)：2 月是有效期空档；
* V1/V2 的涨水与退水节点不同，同一水位在两条分支上对应不同流量；
* R2 另一个测站，初始分支是退水，与 R1 的行交错出现；
* 死区上下限恰好被踩中（差值 == 上限 / 下限）时保持当前分支；
* 水位先涨后退，无效行（空档、上下两端超范围）不改变上一条成功水位；
* 完全相同的一条观测用 +08:00 再写一遍，合并后保留两个原始行号；
* 打乱行序并改写等价时区后重算，业务列逐字段一致。

断言只读取最终输出文件，期望值是手算字面量（下方 EXPECTED），
不调用被测模块的换算函数。
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
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

REGISTRY = {
    "schema_version": 1,
    "stations": {
        "R1": {
            "deadband_m": 0.10,
            "initial_branch": "rising",
            "curves": [
                {
                    "curve_id": "R1-2025",
                    "revision": 3,
                    "effective_from": "2026-01-01T00:00:00+00:00",
                    "effective_to": "2026-02-01T00:00:00+00:00",
                    "rising": [[0.0, 0.0], [1.0, 10.0], [2.0, 30.0]],
                    "falling": [[0.0, 0.0], [1.0, 4.0], [2.0, 18.0]],
                },
                {
                    "curve_id": "R1-2026",
                    "revision": 7,
                    "effective_from": "2026-03-01T00:00:00+00:00",
                    "effective_to": None,
                    "rising": [[0.0, 2.0], [1.0, 12.0], [2.0, 32.0]],
                    "falling": [[0.0, 1.0], [1.0, 5.0], [2.0, 19.0]],
                },
            ],
        },
        "R2": {
            "deadband_m": 0.05,
            "initial_branch": "falling",
            "curves": [
                {
                    "curve_id": "R2-2025",
                    "revision": 1,
                    "effective_from": "2026-01-01T00:00:00Z",
                    "effective_to": "2026-02-15T00:00:00Z",
                    "rising": [[0.0, 0.0], [0.5, 2.5], [1.0, 6.0]],
                    "falling": [[0.0, 0.0], [0.5, 2.0], [1.0, 5.0]],
                },
                {
                    "curve_id": "R2-2026",
                    "revision": 2,
                    "effective_from": "2026-02-15T00:00:00Z",
                    "effective_to": None,
                    "rising": [[0.2, 1.0], [0.6, 3.4], [1.0, 7.0]],
                    "falling": [[0.2, 0.8], [0.6, 2.8], [1.0, 6.0]],
                },
            ],
        },
    },
}

# 故意交错两个测站、并把一条重复观测写成 +08:00；行序不是时间顺序。
OBSERVATIONS = [
    ("R2", "2026-01-05T01:00:00+00:00", "0.60"),
    ("R1", "2026-01-05T00:00:00+00:00", "0.40"),
    ("R1", "2026-01-05T06:00:00+00:00", "0.50"),
    ("R2", "2026-01-05T07:00:00+00:00", "0.55"),
    ("R1", "2026-01-05T12:00:00+00:00", "0.65"),
    ("R2", "2026-01-05T13:00:00+00:00", "0.51"),
    ("R1", "2026-01-05T18:00:00+00:00", "0.50"),
    ("R2", "2026-01-05T19:00:00+00:00", "0.57"),
    ("R1", "2026-01-06T00:00:00+00:00", "0.40"),
    ("R1", "2026-01-06T08:00:00+08:00", "0.400"),  # 与上一行完全等价
    ("R1", "2026-01-06T06:00:00+00:00", "0.30"),
    ("R1", "2026-01-06T12:00:00+00:00", "0.50"),
    ("R1", "2026-01-06T18:00:00+00:00", "1.00"),
    ("R1", "2026-01-07T00:00:00+00:00", "1.50"),
    ("R2", "2026-02-15T00:00:00+00:00", "0.30"),
    ("R2", "2026-02-20T00:00:00+00:00", "0.10"),
    ("R2", "2026-02-21T00:00:00+00:00", "0.20"),
    ("R1", "2026-02-01T00:00:00+00:00", "1.50"),
    ("R1", "2026-02-15T00:00:00+00:00", "3.00"),
    ("R1", "2026-03-01T00:00:00+00:00", "1.60"),
    ("R1", "2026-03-01T06:00:00+00:00", "2.50"),
    ("R1", "2026-03-01T12:00:00+00:00", "1.55"),
    ("R1", "2026-03-01T18:00:00+00:00", "1.35"),
    ("R1", "2026-03-02T00:00:00+00:00", "1.25"),
    ("R1", "2026-03-02T06:00:00+00:00", "-0.05"),
    ("R1", "2026-03-02T12:00:00+00:00", "1.20"),
]

# (站号, UTC 时刻) -> (状态, 流量, 方法, 分支, 版本, 决策, 下节点, 上节点)
# 全部为手算字面量：
#   线性插值 Q = q_low + (h - h_low)(q_high - q_low)/(h_high - h_low)
EXPECTED = {
    ("R1", "2026-01-05T00:00:00Z"): (
        "converted", "4", "linear_interpolation", "rising", "R1-2025",
        "initial", "0", "1"),
    ("R2", "2026-01-05T01:00:00Z"): (
        "converted", "2.6", "linear_interpolation", "falling", "R2-2025",
        "initial", "0.5", "1"),
    ("R1", "2026-01-05T06:00:00Z"): (  # 差 +0.10 == 上限：保持涨水
        "converted", "5", "linear_interpolation", "rising", "R1-2025",
        "keep_within_deadband", "0", "1"),
    ("R2", "2026-01-05T07:00:00Z"): (  # 差 -0.05 == 下限：保持退水
        "converted", "2.3", "linear_interpolation", "falling", "R2-2025",
        "keep_within_deadband", "0.5", "1"),
    ("R1", "2026-01-05T12:00:00Z"): (  # 差 +0.15 > 上限：涨水
        "converted", "6.5", "linear_interpolation", "rising", "R1-2025",
        "switch_to_rising", "0", "1"),
    ("R2", "2026-01-05T13:00:00Z"): (  # 差 -0.04 在死区内
        "converted", "2.06", "linear_interpolation", "falling", "R2-2025",
        "keep_within_deadband", "0.5", "1"),
    ("R1", "2026-01-05T18:00:00Z"): (  # 差 -0.15 < 下限：退水
        "converted", "2", "linear_interpolation", "falling", "R1-2025",
        "switch_to_falling", "0", "1"),
    ("R2", "2026-01-05T19:00:00Z"): (  # 差 +0.06 > 上限：涨水
        "converted", "2.99", "linear_interpolation", "rising", "R2-2025",
        "switch_to_rising", "0.5", "1"),
    ("R1", "2026-01-06T00:00:00Z"): (  # 差 -0.10 == 下限：保持退水
        "converted", "1.6", "linear_interpolation", "falling", "R1-2025",
        "keep_within_deadband", "0", "1"),
    ("R1", "2026-01-06T06:00:00Z"): (  # 再 -0.10 == 下限：仍退水
        "converted", "1.2", "linear_interpolation", "falling", "R1-2025",
        "keep_within_deadband", "0", "1"),
    ("R1", "2026-01-06T12:00:00Z"): (  # 差 +0.20：涨水
        "converted", "5", "linear_interpolation", "rising", "R1-2025",
        "switch_to_rising", "0", "1"),
    ("R1", "2026-01-06T18:00:00Z"): (  # 节点精确命中 h=1
        "converted", "10", "node_exact", "rising", "R1-2025",
        "switch_to_rising", "1", "1"),
    ("R1", "2026-01-07T00:00:00Z"): (  # (1,10)-(2,30) 中点
        "converted", "20", "linear_interpolation", "rising", "R1-2025",
        "switch_to_rising", "1", "2"),
    ("R1", "2026-02-01T00:00:00Z"): (  # V1 失效时刻本身已无适用版本
        "not_converted", "", "", "", "", "no_curve", "", ""),
    ("R2", "2026-02-15T00:00:00Z"): (  # R2 新版恰在生效时刻接管
        "converted", "1.3", "linear_interpolation", "falling", "R2-2026",
        "switch_to_falling", "0.2", "0.6"),
    ("R1", "2026-02-15T00:00:00Z"): (  # 有效期空档
        "not_converted", "", "", "", "", "no_curve", "", ""),
    ("R2", "2026-02-20T00:00:00Z"): (  # 低于曲线下界：不外推
        "not_converted", "", "", "falling", "R2-2026",
        "switch_to_falling", "", ""),
    ("R2", "2026-02-21T00:00:00Z"): (  # 与上一条成功 0.30 相差 -0.10
        "converted", "0.8", "node_exact", "falling", "R2-2026",
        "switch_to_falling", "0.2", "0.2"),
    ("R1", "2026-03-01T00:00:00Z"): (  # 新旧版首尾空档后，新版生效时刻
        "converted", "24", "linear_interpolation", "rising", "R1-2026",
        "keep_within_deadband", "1", "2"),
    ("R1", "2026-03-01T06:00:00Z"): (  # 高于曲线上界：不外推
        "not_converted", "", "", "rising", "R1-2026",
        "switch_to_rising", "", ""),
    ("R1", "2026-03-01T12:00:00Z"): (  # 与上一条成功 1.60 相差 -0.05
        "converted", "23", "linear_interpolation", "rising", "R1-2026",
        "keep_within_deadband", "1", "2"),
    ("R1", "2026-03-01T18:00:00Z"): (  # 实际 -0.20：退水
        "converted", "9.9", "linear_interpolation", "falling", "R1-2026",
        "switch_to_falling", "1", "2"),
    ("R1", "2026-03-02T00:00:00Z"): (  # -0.10 == 下限：保持退水
        "converted", "8.5", "linear_interpolation", "falling", "R1-2026",
        "keep_within_deadband", "1", "2"),
    ("R1", "2026-03-02T06:00:00Z"): (  # 低于下界：不外推
        "not_converted", "", "", "falling", "R1-2026",
        "switch_to_falling", "", ""),
    ("R1", "2026-03-02T12:00:00Z"): (  # 与上一条成功 1.25 相差 -0.05
        "converted", "7.8", "linear_interpolation", "falling", "R1-2026",
        "keep_within_deadband", "1", "2"),
}


def write_inputs(base: Path, rows=None) -> tuple[Path, Path]:
    base.mkdir(parents=True, exist_ok=True)
    registry_path = base / "registry.json"
    observations_path = base / "observations.csv"
    registry_path.write_text(
        json.dumps(REGISTRY, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(observations_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(OBSERVATIONS if rows is None else rows)
    return observations_path, registry_path


def load_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"独立断言失败: {message}")
    print(f"  ✓ {message}")


def result_map(rows: list[dict]) -> dict[tuple[str, str], dict]:
    out = {}
    for row in rows:
        key = (row["station_id"], row["observed_at_utc"])
        check(key not in out, f"{key} 只出现一次")
        out[key] = row
    return out


def independent_assertions(results_path: Path, summary: dict,
                           observations_path: Path) -> None:
    rows = load_csv(results_path)
    results = result_map(rows)

    print("[1] 每条观测都有结果，且业务结果与手算字面量一致")
    check(set(results) == set(EXPECTED),
          f"{len(EXPECTED)} 个 (站号, UTC 时刻) 全部出现在结果表")
    for key, expected in EXPECTED.items():
        (status, discharge, method, branch, curve_id, decision,
         low, high) = expected
        row = results[key]
        check(row["status"] == status
              and row["discharge_m3_s"] == discharge
              and row["method"] == method
              and row["branch"] == branch
              and row["curve_id"] == curve_id
              and row["branch_decision"] == decision
              and row["lower_stage_m"] == low
              and row["upper_stage_m"] == high,
              f"{key[0]} {key[1]}: status={status} Q={discharge or '—'} "
              f"branch={branch or '—'} curve={curve_id or '—'} "
              f"nodes=[{low or '—'},{high or '—'}]")

    print("[2] 节点精确命中与节点间线性换算并存")
    hit = results[("R1", "2026-01-06T18:00:00Z")]
    check(hit["method"] == "node_exact" and hit["calculation"]
          == "Q = q(h=1) = 10", "h=1.00 命中节点，直接取节点流量 10")
    mid = results[("R1", "2026-01-07T00:00:00Z")]
    check(mid["method"] == "linear_interpolation"
          and mid["calculation"] == "Q = 10 + (1.5 - 1) * (30 - 10) / "
                                    "(2 - 1) = 20",
          "h=1.50 在 (1,10)-(2,30) 之间线性换算得 20")
    check(mid["interpolation_fraction"] == "0.5",
          "插值比例 (1.5-1)/(2-1) = 0.5 单独列出，推导可复核")

    print("[3] 先涨后退；差值恰在死区边界时保持原分支")
    r1 = [results[k] for k in sorted(results) if k[0] == "R1"][:6]
    check([r["branch"] for r in r1] ==
          ["rising", "rising", "rising", "falling", "falling", "falling"],
          "R1 前六条：涨水 → 退水，分支随水位变化")
    check(results[("R1", "2026-01-05T06:00:00Z")]["stage_delta_m"] == "0.1"
          and results[("R1", "2026-01-05T06:00:00Z")]["branch_decision"]
          == "keep_within_deadband",
          "水位差 +0.10 恰等于死区上限：保持涨水，不换分支")
    check(results[("R1", "2026-01-06T00:00:00Z")]["stage_delta_m"] == "-0.1"
          and results[("R1", "2026-01-06T00:00:00Z")]["branch_decision"]
          == "keep_within_deadband",
          "水位差 -0.10 恰等于死区下限：保持退水")

    print("[4] 新曲线到达生效时刻后接替旧版本")
    old = results[("R1", "2026-01-07T00:00:00Z")]
    new = results[("R1", "2026-03-01T00:00:00Z")]
    check(old["curve_id"] == "R1-2025" and new["curve_id"] == "R1-2026",
          "1 月用 R1-2025，3 月 1 日 00:00Z 起改用 R1-2026")
    check(new["curve_effective_from_utc"] == "2026-03-01T00:00:00Z"
          and new["observed_at_utc"] == new["curve_effective_from_utc"],
          "观测时刻恰等于新版生效时刻：新版有效（半开区间左闭）")
    check(results[("R1", "2026-02-01T00:00:00Z")]["reason"]
          == "no_effective_curve",
          "旧版失效时刻本身已不属于旧版（半开区间右开）")

    print("[5] 有效期空档与两端超范围都不外推")
    check(results[("R1", "2026-02-15T00:00:00Z")]["reason"]
          == "no_effective_curve",
          "2 月空档内的观测留在产物里并注明 no_effective_curve")
    over = results[("R1", "2026-03-01T06:00:00Z")]
    under = results[("R1", "2026-03-02T06:00:00Z")]
    check(over["reason"] == "stage_out_of_curve_range"
          and over["discharge_m3_s"] == "" and over["curve_stage_max_m"] == "2",
          "h=2.50 高于 R1-2026 上界 2.00：不换算、不外推")
    check(under["reason"] == "stage_out_of_curve_range"
          and under["discharge_m3_s"] == "" and under["curve_stage_min_m"] == "0",
          "h=-0.05 低于 R1-2026 下界 0.00：不换算、不外推")
    check(results[("R2", "2026-02-20T00:00:00Z")]["reason"]
          == "stage_out_of_curve_range"
          and results[("R2", "2026-02-20T00:00:00Z")]["curve_stage_min_m"]
          == "0.2",
          "R2 h=0.10 低于 R2-2026 下界 0.20：不换算、不外推")

    print("[6] 无效观测之后仍按上一条成功结果选择分支")
    after_bad = results[("R1", "2026-03-01T12:00:00Z")]
    check(after_bad["previous_stage_m"] == "1.6"
          and after_bad["stage_delta_m"] == "-0.05"
          and after_bad["branch_decision"] == "keep_within_deadband"
          and after_bad["branch"] == "rising",
          "h=2.50 超范围行被跳过后，下一条仍与成功水位 1.60 比较："
          "差 -0.05 在死区内，保持涨水（若与 2.50 比会误判退水）")
    check(after_bad["state_applied"] == "true"
          and over["state_applied"] == "false",
          "只有换算成功的行提交回放状态")

    print("[7] 相同重复观测合并，保留全部原始行号")
    merged = results[("R1", "2026-01-06T00:00:00Z")]
    check(merged["record_count"] == "2" and ";" in merged["source_lines"],
          f"等价时刻/水位的两行合并为一条，来源行 {merged['source_lines']}")
    lines = [int(x) for x in merged["source_lines"].split(";")]
    with open(observations_path, encoding="utf-8", newline="") as fh:
        physical = list(csv.reader(fh))
    check(all(physical[line - 1][0] == "R1"
              and physical[line - 1][2] in ("0.40", "0.400")
              for line in lines),
          "来源行号指向实际输入中的两条原始记录")
    check(summary["counts"]["raw_observation_records"] == len(OBSERVATIONS)
          and summary["counts"]["merged_duplicate_records"] == 1
          and summary["counts"]["unique_observations"] == len(OBSERVATIONS) - 1,
          "原始 26 行 = 唯一 25 行 + 合并 1 行")

    print("[8] 两个测站交错处理，分支互不串用")
    r2 = results[("R2", "2026-01-05T01:00:00Z")]
    check(r2["branch"] == "falling" and r2["branch_decision"] == "initial",
          "R2 从自己的初始分支退水开始，R1 的首行从涨水开始")
    check(results[("R2", "2026-01-05T19:00:00Z")]["branch"] == "rising"
          and results[("R1", "2026-01-05T18:00:00Z")]["branch"] == "falling",
          "同一交错时段 R2 转涨水、R1 转退水，互不影响")
    check(summary["registry_summary"]["per_station"]["R2"]["initial_branch"]
          == "falling"
          and summary["registry_summary"]["per_station"]["R1"]["initial_branch"]
          == "rising",
          "摘要分别记录每站初始分支")

    print("[9] 输出统计与摘要指纹勾稽")
    counts = summary["counts"]
    check(counts["result_rows"] == len(EXPECTED)
          and counts["converted_rows"] + counts["not_converted_rows"]
          == counts["result_rows"],
          "结果行数 = 换算 + 未换算")
    check(counts["converted_rows"] == 20
          and counts["not_converted_rows"] == 5
          and counts["not_converted_no_effective_curve"] == 2
          and counts["not_converted_stage_out_of_curve_range"] == 3,
          "手算统计：20 条换算、2 条无曲线、3 条超范围")
    check(summary["inputs"]["observations_csv"]["sha256"]
          == sha256_file(observations_path),
          "摘要记录观测 CSV 的 SHA256")
    check("率定关系" in summary["disclaimer"]
          and "并非" in summary["disclaimer"],
          "摘要声明结果是率定关系推算值、并非直接测得的流量")


BUSINESS_COLUMNS = [c for c in (
    "station_id", "observed_at_utc", "stage_m", "record_count", "status",
    "discharge_m3_s", "method", "reason", "curve_id", "revision",
    "curve_effective_from_utc", "curve_effective_to_utc", "branch",
    "branch_decision", "previous_stage_m", "stage_delta_m",
    "curve_stage_min_m", "curve_stage_max_m", "lower_stage_m",
    "upper_stage_m", "lower_discharge_m3_s", "upper_discharge_m3_s",
    "interpolation_fraction", "calculation", "state_applied")]


def shuffled_rows() -> list[tuple[str, str, str]]:
    """打乱行序，并把部分时刻改写成等价时区表示。"""
    order = [9, 25, 0, 13, 4, 19, 21, 2, 16, 7, 11, 23, 1, 17, 5, 24, 3,
             8, 14, 20, 10, 12, 15, 22, 6, 18]
    rewrite = {
        "2026-01-05T00:00:00+00:00": "2026-01-05T08:00:00+08:00",
        "2026-01-05T12:00:00+00:00": "2026-01-05T04:00:00-08:00",
        "2026-02-15T00:00:00+00:00": "2026-02-15T05:30:00+05:30",
        "2026-03-01T00:00:00+00:00": "2026-02-28T16:00:00-08:00",
        "2026-03-02T12:00:00+00:00": "2026-03-02T21:00:00+09:00",
    }
    rows = []
    for index in order:
        station, time_text, stage = OBSERVATIONS[index]
        rows.append((station, rewrite.get(time_text, time_text), stage))
    return rows


def check_shuffle_stability(base: Path) -> None:
    print("[10] 打乱行序并换写等价时区后业务结果不变")
    rows_a = load_csv(base / "out" / RESULTS_FILENAME)
    shuffle_dir = base / "shuffled"
    obs_path, reg_path = write_inputs(shuffle_dir, shuffled_rows())
    summary_b = process_stage_discharge(
        obs_path, reg_path, shuffle_dir / "out")
    rows_b = load_csv(shuffle_dir / "out" / RESULTS_FILENAME)

    check([{c: r[c] for c in BUSINESS_COLUMNS} for r in rows_a]
          == [{c: r[c] for c in BUSINESS_COLUMNS} for r in rows_b],
          "业务列逐字段一致（版本、分支、流量、插值节点与计算式）")
    check(len(rows_a) == len(rows_b), "结果行数不变")

    with open(obs_path, encoding="utf-8", newline="") as fh:
        physical = list(csv.reader(fh))
    for row in rows_b:
        lines = [int(x) for x in row["source_lines"].split(";")]
        for line in lines:
            check(physical[line - 1][0] == row["station_id"]
                  and physical[line - 1][2].rstrip("0").rstrip(".")
                  == row["stage_m"].rstrip("0").rstrip("."),
                  f"打乱后 {row['station_id']} {row['observed_at_utc']} "
                  f"的行号 {line} 仍指向该站该水位的实际输入行")
    check(summary_b["counts"]["converted_rows"]
          == summary_b["counts"]["result_rows"]
          - summary_b["counts"]["not_converted_rows"],
          "打乱后统计仍自洽")


def demonstrate_rejections(base: Path, observations_path: Path,
                          registry_path: Path) -> None:
    print("[11] 冲突观测与非法登记整批拒绝，不产出半套结果")

    conflict = base / "conflict.csv"
    with open(conflict, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerow(("R1", "2026-01-05T00:00:00+00:00", "0.40"))
        writer.writerow(("R1", "2026-01-05T08:00:00+08:00", "0.41"))
    conflict_out = base / "conflict_out"
    try:
        process_stage_discharge(conflict, registry_path, conflict_out)
    except StageDischargeInputError as exc:
        check("水位冲突" in str(exc), f"同站同刻不同水位被拒绝: {exc}")
    else:
        raise AssertionError("同刻水位冲突未触发失败")
    check(not conflict_out.exists(), "失败后不留下任何输出目录")

    unknown = base / "unknown.csv"
    with open(unknown, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerow(("R9", "2026-01-05T00:00:00+00:00", "0.40"))
    try:
        process_stage_discharge(unknown, registry_path, base / "unknown_out")
    except StageDischargeInputError as exc:
        check("未登记站号" in str(exc), f"未登记站号被拒绝: {exc}")

    overlapping = json.loads(json.dumps(REGISTRY))
    overlapping["stations"]["R1"]["curves"][1]["effective_from"] = \
        "2026-01-15T00:00:00+00:00"
    overlap_path = base / "overlap.json"
    overlap_path.write_text(json.dumps(overlapping), encoding="utf-8")
    try:
        process_stage_discharge(observations_path, overlap_path,
                                base / "overlap_out")
    except StageDischargeRegistryError as exc:
        check("有效期交叠" in str(exc), f"有效期交叠被拒绝: {exc}")

    bad_nodes = json.loads(json.dumps(REGISTRY))
    bad_nodes["stations"]["R1"]["curves"][0]["falling"] = \
        [[0.0, 0.0], [1.5, 4.0], [1.0, 18.0]]
    bad_nodes_path = base / "bad_nodes.json"
    bad_nodes_path.write_text(json.dumps(bad_nodes), encoding="utf-8")
    try:
        process_stage_discharge(observations_path, bad_nodes_path,
                                base / "bad_nodes_out")
    except StageDischargeRegistryError as exc:
        check("严格递增" in str(exc), f"节点水位不递增被拒绝: {exc}")

    bad_flow = json.loads(json.dumps(REGISTRY))
    bad_flow["stations"]["R2"]["curves"][1]["rising"] = \
        [[0.2, 1.0], [0.6, 0.5], [1.0, 7.0]]
    bad_flow_path = base / "bad_flow.json"
    bad_flow_path.write_text(json.dumps(bad_flow), encoding="utf-8")
    try:
        process_stage_discharge(observations_path, bad_flow_path,
                                base / "bad_flow_out")
    except StageDischargeRegistryError as exc:
        check("不下降" in str(exc), f"节点流量下降被拒绝: {exc}")


def demonstrate_cli(base: Path, observations_path: Path,
                    registry_path: Path) -> None:
    print("[12] CLI 帮助与退出码")
    parser = build_parser()
    help_text = parser.format_help()
    check("stage-discharge" in help_text,
          "顶层帮助列出 stage-discharge")
    sub_parser = None
    for action in parser._actions:
        if getattr(action, "choices", None) and "stage-discharge" in action.choices:
            sub_parser = action.choices["stage-discharge"]
    check(sub_parser is not None
          and "率定" in sub_parser.format_help()
          and "并非" not in sub_parser.format_help()
          and "率定关系推算值" in sub_parser.format_help(),
          "子命令帮助说明输入、口径与“推算值非实测”边界")

    rc = main(["stage-discharge", "--observations", str(observations_path),
               "--registry", str(registry_path),
               "--output-dir", str(base / "cli_out")])
    check(rc == 0 and (base / "cli_out" / SUMMARY_FILENAME).exists(),
          "正常输入退出码 0，结果表与摘要齐全")

    rc = main(["stage-discharge", "--observations",
               str(base / "conflict.csv"),
               "--registry", str(registry_path),
               "--output-dir", str(base / "cli_conflict")])
    check(rc == 3, "观测冲突退出码 3，且不声称生成完整结果")

    rc = main(["stage-discharge", "--observations", str(observations_path),
               "--registry", str(base / "overlap.json"),
               "--output-dir", str(base / "cli_overlap")])
    check(rc == 2, "登记错误退出码 2")


def main_demo(argv: list[str]) -> int:
    base = Path(argv[1]) if len(argv) > 1 \
        else Path(tempfile.mkdtemp(prefix="stage-discharge-samples-"))
    print(f"合成数据目录: {base}")
    observations_path, registry_path = write_inputs(base)
    out_dir = base / "out"
    print(f"登记: {registry_path}")
    print(f"观测: {observations_path}（{len(OBSERVATIONS)} 行，故意乱序/交错）")
    summary = process_stage_discharge(
        observations_path, registry_path, out_dir)
    print(f"产出: {out_dir}/{{{RESULTS_FILENAME}, {SUMMARY_FILENAME}}}\n")
    independent_assertions(
        out_dir / RESULTS_FILENAME, summary, observations_path)
    check_shuffle_stability(base)
    demonstrate_rejections(base, observations_path, registry_path)
    demonstrate_cli(base, observations_path, registry_path)
    print("\n全部独立断言通过。换算流量为率定关系推算值，并非直接测得。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_demo(sys.argv))
