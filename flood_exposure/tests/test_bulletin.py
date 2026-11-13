"""bulletin 水情通报接收命令的验收演练（标准库 unittest，pytest 也可收集）。

这是一次真实录入演练：每个场景都实际调用命令、读回产物，对照**手写的**
观测表与待核条目，并逐字段用产物里的位置切回源文本验证。所有期望（站号、
数值、UTC 时刻、字符区间、行列）都是手工计算的字面量，测试自带独立的
行列换算与位置构造函数，不复用被测模块的任何解析逻辑。

覆盖需求列出的全部场景：

* 开头的通报：只收实测水位 12.30，比较量/警戒水位/预计都进非实测
* 标题继承与单条覆盖；空行清标题，相邻段不串用；标题只向后生效不回填
* 跨年“12月31日”、今日/昨日、整点24时、24时30分、非法日期逐条独立预期
* 重叠别名、歧义别名、同名汉字边界
* 同站同刻同值合并、不同值冲突、缺报与实数冲突、缺报合并
* 实测后跟预测互不污染；缺报与真零；负水位合法、负流量待核
* 实测晚于发布待核；无时刻不用发布时间冒充
* 疑似实测但句法不支持（约数/范围/缺单位/裸数值）进待核
* 单位精确换算（厘米/毫米）；前导零站号原样保留
* 文本或对照无法读取时明确失败
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from flood_exposure.cli import main  # noqa: E402
from flood_exposure.bulletin import (  # noqa: E402
    OBSERVATIONS_FILENAME,
    REVIEW_FILENAME,
    SUMMARY_FILENAME,
)

PUBLISH = "2026-11-13T10:00:00+08:00"  # 发布时刻 UTC = 2026-11-13T02:00:00Z


# --------------------------------------------------------------------------- #
# 测试自带的独立位置换算（不复用被测模块）
# --------------------------------------------------------------------------- #
def line_col(text: str, offset: int) -> tuple[int, int]:
    """1 起 (行, 列)，独立实现。"""
    line = text.count("\n", 0, offset) + 1
    newline = text.rfind("\n", 0, offset)
    return line, offset - newline


def ref_of(text: str, start: int, end: int) -> str:
    """按文档格式构造位置引用：c起-止@L行C列-L行C列。"""
    ls, cs = line_col(text, start)
    le, ce = line_col(text, end)
    return f"c{start}-{end}@L{ls}C{cs}-L{le}C{ce}"


def span_of(text: str, fragment: str, start: int = 0) -> tuple[int, int]:
    idx = text.index(fragment, start)
    return idx, idx + len(fragment)


REF_PATTERN = re.compile(r"c(\d+)-(\d+)@L(\d+)C(\d+)-L(\d+)C(\d+)")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
class BulletinCase(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bulletin-test-"))
        self.text_path = self.tmp / "bulletin.txt"
        self.stations_path = self.tmp / "stations.json"
        self.out_dir = self.tmp / "out"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -------------------------------------------------------------- #
    def write_stations(self, stations) -> None:
        self.stations_path.write_text(
            json.dumps({"schema_version": 1, "stations": stations},
                       ensure_ascii=False),
            encoding="utf-8")

    def write_text(self, text: str) -> None:
        self.text_path.write_text(text, encoding="utf-8")

    def run_cli(self, published_at: str = PUBLISH) -> int:
        return main(["bulletin", "--text", str(self.text_path),
                     "--published-at", published_at,
                     "--stations", str(self.stations_path),
                     "--output-dir", str(self.out_dir)])

    def read_observations(self) -> list[dict]:
        with open(self.out_dir / OBSERVATIONS_FILENAME,
                  encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def read_review(self) -> list[dict]:
        with open(self.out_dir / REVIEW_FILENAME,
                  encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def read_summary(self) -> dict:
        with open(self.out_dir / SUMMARY_FILENAME, encoding="utf-8") as fh:
            return json.load(fh)

    # -------------------------------------------------------------- #
    def assert_ref(self, text: str, ref: str, fragment: str) -> None:
        """位置引用必须切回原文得到记录的文字，行列与字符区间自洽。"""
        m = REF_PATTERN.fullmatch(ref)
        self.assertIsNotNone(m, f"位置引用格式非法: {ref}")
        s, e, ls, cs, le, ce = (int(g) for g in m.groups())
        self.assertEqual(text[s:e], fragment,
                         f"位置 {ref} 切回原文不是 {fragment!r}")
        self.assertEqual((ls, cs), line_col(text, s))
        self.assertEqual((le, ce), line_col(text, e))

    def assert_obs_row(self, row: dict, *, obs_id, station_id, station_name,
                       utc, metric, status, value, unit, missing_reason,
                       source_no) -> None:
        self.assertEqual(row["observation_id"], obs_id)
        self.assertEqual(row["station_id"], station_id)
        self.assertEqual(row["station_name"], station_name)
        self.assertEqual(row["sampled_at_utc"], utc)
        self.assertEqual(row["metric"], metric)
        self.assertEqual(row["status"], status)
        self.assertEqual(row["value"], value)
        self.assertEqual(row["unit"], unit)
        self.assertEqual(row["missing_reason"], missing_reason)
        self.assertEqual(row["source_no"], str(source_no))


# --------------------------------------------------------------------------- #
# 1. 开头的通报：只收实测水位
# --------------------------------------------------------------------------- #
class OpeningBulletin(BulletinCase):

    def test_only_measured_stage_accepted(self) -> None:
        text = ("甲站今8时水位12.30米，较昨日涨0.20米，警戒水位13米；"
                "预计明晨达到13.2米。")
        self.write_text(text)
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 0, "无待核项时应全部接收")

        obs = self.read_observations()
        self.assertEqual(len(obs), 1)
        row = obs[0]
        # 手写期望：站号前导零原样保留；12.30米 -> 12.3 米；
        # 今日 8 时 (+08:00) -> 2026-11-13T00:00:00Z
        self.assert_obs_row(
            row, obs_id="obs-0001", station_id="00101", station_name="甲站",
            utc="2026-11-13T00:00:00Z", metric="水位", status="measured",
            value="12.3", unit="米", missing_reason="", source_no=1)
        # 逐字段位置：手写区间 + 切回原文
        self.assertEqual(row["station_ref"], ref_of(text, 0, 2))
        self.assert_ref(text, row["station_ref"], "甲站")
        self.assertEqual(row["date_ref"], ref_of(text, 2, 3))
        self.assert_ref(text, row["date_ref"], "今")
        self.assertEqual(row["time_ref"], ref_of(text, 3, 5))
        self.assert_ref(text, row["time_ref"], "8时")
        self.assertEqual(row["value_ref"], ref_of(text, 5, 13))
        self.assert_ref(text, row["value_ref"], "水位12.30米")

        # 其余三个数（涨 0.20、警戒 13、预计 13.2）都是非实测，不产生观测
        self.assertEqual(self.read_review(), [])
        summary = self.read_summary()
        self.assertEqual(summary["counts"]["accepted"], 1)
        self.assertEqual(summary["counts"]["missing"], 0)
        self.assertEqual(summary["counts"]["conflict"], 0)
        self.assertEqual(summary["counts"]["other_review"], 0)
        self.assertEqual(summary["counts"]["non_measurement"], 3)
        kinds = [it["kind"] for it in summary["non_measurement_items"]]
        self.assertEqual(kinds, ["comparison", "reference_level", "forecast"])
        texts = [it["text"] for it in summary["non_measurement_items"]]
        self.assertEqual(texts, ["较昨日涨0.20米", "警戒水位13米",
                                 "预计明晨达到13.2米"])
        for item in summary["non_measurement_items"]:
            self.assert_ref(text, item["span"], item["text"])


# --------------------------------------------------------------------------- #
# 2. 标题继承与单条覆盖
# --------------------------------------------------------------------------- #
class HeaderInheritance(BulletinCase):

    TEXT = ("日期：2026-11-12\n"
            "时刻：08:30\n"
            "测站：甲站\n"
            "水位12.30米。\n"
            "乙站水位13.40米，流量500立方米每秒。\n"
            "流量800立方米每秒。\n")

    def test_inherit_and_override(self) -> None:
        text = self.TEXT
        self.write_text(text)
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
            {"station_id": "00202", "name": "乙站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 0)
        obs = self.read_observations()
        self.assertEqual(len(obs), 4)

        date_ref = ref_of(text, *span_of(text, "2026-11-12"))
        time_ref = ref_of(text, *span_of(text, "08:30"))
        jia_ref = ref_of(text, *span_of(text, "甲站"))
        yi_ref = ref_of(text, *span_of(text, "乙站"))

        # obs-0001：甲站 水位 12.3，全部字段继承标题 -> 位置指向标题行本身
        self.assert_obs_row(
            obs[0], obs_id="obs-0001", station_id="00101",
            station_name="甲站", utc="2026-11-12T00:30:00Z", metric="水位",
            status="measured", value="12.3", unit="米", missing_reason="",
            source_no=1)
        self.assertEqual(obs[0]["station_ref"], jia_ref)
        self.assertEqual(obs[0]["date_ref"], date_ref)
        self.assertEqual(obs[0]["time_ref"], time_ref)
        self.assert_ref(text, obs[0]["station_ref"], "甲站")
        self.assert_ref(text, obs[0]["date_ref"], "2026-11-12")
        self.assert_ref(text, obs[0]["time_ref"], "08:30")
        self.assert_ref(text, obs[0]["value_ref"], "水位12.30米")

        # obs-0002：甲站 流量 800（第 3 条陈述）——证明第 2 条的“乙站”
        # 没有暗改测站标题
        self.assert_obs_row(
            obs[1], obs_id="obs-0002", station_id="00101",
            station_name="甲站", utc="2026-11-12T00:30:00Z", metric="流量",
            status="measured", value="800", unit="立方米每秒",
            missing_reason="", source_no=1)
        self.assertEqual(obs[1]["station_ref"], jia_ref)
        self.assert_ref(text, obs[1]["value_ref"], "流量800立方米每秒")

        # obs-0003 / obs-0004：乙站陈述自带站名覆盖标题，日期时刻仍继承；
        # 一条陈述同时提供水位和流量，各自保留数值与来源
        self.assert_obs_row(
            obs[2], obs_id="obs-0003", station_id="00202",
            station_name="乙站", utc="2026-11-12T00:30:00Z", metric="水位",
            status="measured", value="13.4", unit="米", missing_reason="",
            source_no=1)
        self.assertEqual(obs[2]["station_ref"], yi_ref)
        self.assertEqual(obs[2]["date_ref"], date_ref)
        self.assertEqual(obs[2]["time_ref"], time_ref)
        self.assert_ref(text, obs[2]["value_ref"], "水位13.40米")

        self.assert_obs_row(
            obs[3], obs_id="obs-0004", station_id="00202",
            station_name="乙站", utc="2026-11-12T00:30:00Z", metric="流量",
            status="measured", value="500", unit="立方米每秒",
            missing_reason="", source_no=1)
        self.assertEqual(obs[3]["station_ref"], yi_ref)
        self.assert_ref(text, obs[3]["value_ref"], "流量500立方米每秒")

        summary = self.read_summary()
        self.assertEqual(summary["counts"]["accepted"], 4)
        self.assertEqual(summary["counts"]["review_total"], 0)


# --------------------------------------------------------------------------- #
# 3. 空行清掉全部标题：相邻段信息不漏过来
# --------------------------------------------------------------------------- #
class BlankLineClearsHeaders(BulletinCase):

    def test_second_paragraph_does_not_inherit(self) -> None:
        text = ("测站：甲站\n"
                "日期：2026-11-12\n"
                "时刻：08:30\n"
                "水位12.30米。\n"
                "\n"
                "水位13.00米。\n")
        self.write_text(text)
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 4, "有待核项应返回部分接收")

        obs = self.read_observations()
        self.assertEqual(len(obs), 1)
        self.assert_obs_row(
            obs[0], obs_id="obs-0001", station_id="00101",
            station_name="甲站", utc="2026-11-12T00:30:00Z", metric="水位",
            status="measured", value="12.3", unit="米", missing_reason="",
            source_no=1)

        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["category"], "other")
        self.assertEqual(review[0]["reasons"],
                         "no_station;no_date;no_time")
        self.assert_ref(text, review[0]["span"], "水位13.00米")

        summary = self.read_summary()
        self.assertEqual(summary["counts"]["accepted"], 1)
        self.assertEqual(summary["counts"]["other_review"], 1)

    def test_headers_only_apply_forward(self) -> None:
        # 标题只向后生效：标题前的陈述缺时刻 -> 待核，绝不从后一条回填
        text = ("测站：甲站\n"
                "日期：2026-11-12\n"
                "水位12.30米。\n"
                "时刻：08:30\n"
                "水位12.40米。\n")
        self.write_text(text)
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        obs = self.read_observations()
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["value"], "12.4")
        self.assertEqual(obs[0]["sampled_at_utc"], "2026-11-12T00:30:00Z")
        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reasons"], "no_time")
        self.assert_ref(text, review[0]["span"], "水位12.30米")


# --------------------------------------------------------------------------- #
# 4. 日期与时刻：逐条独立预期
# --------------------------------------------------------------------------- #
class DatesAndTimes(BulletinCase):

    def setUp(self) -> None:
        super().setUp()
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
        ])

    def one_observation(self, text: str, published_at: str) -> dict:
        self.write_text(text)
        rc = self.run_cli(published_at)
        self.assertEqual(rc, 0)
        obs = self.read_observations()
        self.assertEqual(len(obs), 1)
        return obs[0]

    def test_cross_year_month_day(self) -> None:
        # 2026-01-05 发布，“12月31日”晚于发布日 -> 退到 2025 年
        row = self.one_observation(
            "测站：甲站\n时刻：8时\n12月31日水位10.00米。",
            "2026-01-05T09:00:00+08:00")
        self.assertEqual(row["sampled_at_utc"], "2025-12-31T00:00:00Z")
        self.assertEqual(row["value"], "10")

    def test_month_day_same_year(self) -> None:
        row = self.one_observation(
            "测站：甲站\n时刻：8时\n11月10日水位10.00米。", PUBLISH)
        self.assertEqual(row["sampled_at_utc"], "2026-11-10T00:00:00Z")

    def test_today_and_yesterday(self) -> None:
        self.write_text("测站：甲站\n时刻：8时\n"
                        "今日水位10.00米。昨日水位9.50米。")
        rc = self.run_cli(PUBLISH)
        self.assertEqual(rc, 0)
        obs = self.read_observations()
        self.assertEqual(len(obs), 2)
        # 按 (站号, UTC 时刻) 排序：昨日在前
        self.assertEqual(obs[0]["sampled_at_utc"], "2026-11-12T00:00:00Z")
        self.assertEqual(obs[0]["value"], "9.5")
        self.assertEqual(obs[1]["sampled_at_utc"], "2026-11-13T00:00:00Z")
        self.assertEqual(obs[1]["value"], "10")

    def test_24_oclock_rolls_to_next_day(self) -> None:
        # 11月12日 24时 = 11月13日 00:00 (+08:00) = 2026-11-12T16:00:00Z
        row = self.one_observation(
            "测站：甲站\n日期：2026-11-12\n时刻：24时\n水位10.00米。",
            "2026-11-13T06:00:00+08:00")
        self.assertEqual(row["sampled_at_utc"], "2026-11-12T16:00:00Z")

    def test_24_30_is_invalid(self) -> None:
        self.write_text("测站：甲站\n日期：2026-11-12\n"
                        "24时30分水位10.00米。")
        rc = self.run_cli("2026-11-13T06:00:00+08:00")
        self.assertEqual(rc, 4)
        self.assertEqual(self.read_observations(), [])
        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reasons"], "invalid_time")

    def test_invalid_date_no_cross_year_guess(self) -> None:
        # 2月30日 在发布当年非法 -> 待核，不退回上一年猜补
        self.write_text("测站：甲站\n时刻：8时\n2月30日水位10.00米。")
        rc = self.run_cli(PUBLISH)
        self.assertEqual(rc, 4)
        self.assertEqual(self.read_observations(), [])
        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reasons"], "invalid_date")

    def test_colon_and_fen_time_forms(self) -> None:
        # 08:30 与 8时30分 两种写法
        row = self.one_observation(
            "测站：甲站\n日期：2026-11-12\n甲站8时30分水位10.00米。",
            PUBLISH)
        self.assertEqual(row["sampled_at_utc"], "2026-11-12T00:30:00Z")
        row = self.one_observation(
            "测站：甲站\n日期：2026-11-12\n甲站08:30水位10.00米。", PUBLISH)
        self.assertEqual(row["sampled_at_utc"], "2026-11-12T00:30:00Z")

    def test_today_uses_publish_timezone_not_utc(self) -> None:
        # 发布 2026-11-13T01:00+08:00 = UTC 2026-11-12T17:00Z：
        # “昨日”按发布时区的自然日算是 2026-11-12，不是 UTC 的 2026-11-11
        row = self.one_observation(
            "测站：甲站\n甲站昨日20时水位12.30米。",
            "2026-11-13T01:00:00+08:00")
        self.assertEqual(row["sampled_at_utc"], "2026-11-12T12:00:00Z")


# --------------------------------------------------------------------------- #
# 5. 站名匹配：重叠别名、歧义别名、同名汉字边界
# --------------------------------------------------------------------------- #
class StationMatching(BulletinCase):

    def test_overlapping_aliases_and_boundaries(self) -> None:
        text = ("日期：2026-11-12\n"
                "江口站8时水位10.01米。\n"
                "江口9时水位10.02米。\n"
                "江站8时水位10.03米。\n"
                "江，9时水位10.04米。\n"
                "江8时水位10.05米。\n"
                "江水上涨。\n")
        self.write_text(text)
        self.write_stations([
            {"station_id": "01", "name": "江口站", "aliases": ["江口"]},
            {"station_id": "02", "name": "江站", "aliases": ["江"]},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        obs = self.read_observations()
        self.assertEqual(len(obs), 4)
        # 长站名优先：短别名“江口/江”不会抢走“江口站/江站”
        # “江，...”单字别名右侧是标点 -> 算测站；
        # “江8时...”单字别名右侧是数字 -> 不算 -> 无站待核；
        # “江水上涨”里的“江”右侧是汉字 -> 不算测站，全句只是比较量
        expected = [
            ("obs-0001", "01", "江口站", "2026-11-12T00:00:00Z", "10.01"),
            ("obs-0002", "01", "江口站", "2026-11-12T01:00:00Z", "10.02"),
            ("obs-0003", "02", "江站", "2026-11-12T00:00:00Z", "10.03"),
            ("obs-0004", "02", "江站", "2026-11-12T01:00:00Z", "10.04"),
        ]
        for row, (obs_id, sid, name, utc, value) in zip(obs, expected):
            self.assert_obs_row(
                row, obs_id=obs_id, station_id=sid, station_name=name,
                utc=utc, metric="水位", status="measured", value=value,
                unit="米", missing_reason="", source_no=1)
        # 站号前导零原样保留
        self.assertEqual({r["station_id"] for r in obs}, {"01", "02"})

        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reasons"], "no_station")
        self.assert_ref(text, review[0]["span"], "江8时水位10.05米")

        summary = self.read_summary()
        self.assertEqual(summary["counts"]["non_measurement"], 1)
        self.assertEqual(summary["non_measurement_items"][0]["text"],
                         "江水上涨")

    def test_ambiguous_alias_goes_to_review(self) -> None:
        text = ("日期：2026-11-12\n时刻：8时\n江口水位10.00米。")
        self.write_text(text)
        self.write_stations([
            {"station_id": "01", "name": "江口站", "aliases": ["江口"]},
            {"station_id": "02", "name": "江口二站", "aliases": ["江口"]},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        self.assertEqual(self.read_observations(), [])
        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reasons"], "ambiguous_station")
        self.assert_ref(text, review[0]["span"], "江口水位10.00米")


# --------------------------------------------------------------------------- #
# 6. 合并与冲突
# --------------------------------------------------------------------------- #
class MergeAndConflict(BulletinCase):

    TEXT = ("日期：2026-11-12\n"
            "时刻：08:30\n"
            "甲站水位12.30米。甲站水位12.3米。\n"
            "乙站水位13.40米，流量500立方米每秒。乙站水位13.45米。\n"
            "丙站水位缺报。丙站水位12.00米。\n"
            "丁站水位缺报。丁站水位未测。\n")

    def test_merge_and_conflict(self) -> None:
        text = self.TEXT
        self.write_text(text)
        self.write_stations([
            {"station_id": "01", "name": "甲站", "aliases": []},
            {"station_id": "02", "name": "乙站", "aliases": []},
            {"station_id": "03", "name": "丙站", "aliases": []},
            {"station_id": "04", "name": "丁站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        obs = self.read_observations()

        # obs-0001：甲站同值发布（12.30 与 12.3 数值相同）合成一条，两处出处
        self.assert_obs_row(
            obs[0], obs_id="obs-0001", station_id="01", station_name="甲站",
            utc="2026-11-12T00:30:00Z", metric="水位", status="measured",
            value="12.3", unit="米", missing_reason="", source_no=1)
        self.assert_ref(text, obs[0]["value_ref"], "水位12.30米")
        self.assert_obs_row(
            obs[1], obs_id="obs-0001", station_id="01", station_name="甲站",
            utc="2026-11-12T00:30:00Z", metric="水位", status="measured",
            value="12.3", unit="米", missing_reason="", source_no=2)
        self.assert_ref(text, obs[1]["value_ref"], "水位12.3米")

        # obs-0002：乙站流量不受水位冲突影响，继续接收
        self.assert_obs_row(
            obs[2], obs_id="obs-0002", station_id="02", station_name="乙站",
            utc="2026-11-12T00:30:00Z", metric="流量", status="measured",
            value="500", unit="立方米每秒", missing_reason="", source_no=1)

        # obs-0003：丁站两处缺报合成一条缺测观测（缺报/未测都保留）
        self.assert_obs_row(
            obs[3], obs_id="obs-0003", station_id="04", station_name="丁站",
            utc="2026-11-12T00:30:00Z", metric="水位", status="missing",
            value="", unit="", missing_reason="缺报;未测", source_no=1)
        self.assert_ref(text, obs[3]["value_ref"], "水位缺报")
        self.assert_obs_row(
            obs[4], obs_id="obs-0003", station_id="04", station_name="丁站",
            utc="2026-11-12T00:30:00Z", metric="水位", status="missing",
            value="", unit="", missing_reason="缺报;未测", source_no=2)
        self.assert_ref(text, obs[4]["value_ref"], "水位未测")
        self.assertEqual(len(obs), 5)

        # 冲突：乙站水位 13.40 vs 13.45 整组移入待核；
        #       丙站一处缺报一处实数也视为冲突
        review = self.read_review()
        self.assertEqual(len(review), 2)
        self.assertEqual(review[0]["category"], "conflict")
        self.assertEqual(review[0]["reasons"], "conflict")
        self.assertEqual(review[0]["station_id"], "02")
        self.assertEqual(review[0]["metric"], "水位")
        self.assertEqual(review[0]["sampled_at_utc"],
                         "2026-11-12T00:30:00Z")
        self.assertIn("13.4 米", review[0]["detail"])
        self.assertIn("13.45 米", review[0]["detail"])
        self.assert_ref(text, review[0]["span"], "水位13.40米")
        self.assertEqual(review[1]["category"], "conflict")
        self.assertEqual(review[1]["station_id"], "03")
        self.assertIn("缺报", review[1]["detail"])
        self.assertIn("12 米", review[1]["detail"])
        self.assert_ref(text, review[1]["span"], "水位缺报")

        summary = self.read_summary()
        self.assertEqual(summary["counts"]["accepted"], 2)
        self.assertEqual(summary["counts"]["missing"], 1)
        self.assertEqual(summary["counts"]["conflict"], 2)
        self.assertEqual(summary["counts"]["other_review"], 0)


# --------------------------------------------------------------------------- #
# 7. 实测后跟预测：互不污染
# --------------------------------------------------------------------------- #
class ForecastDoesNotPollute(BulletinCase):

    def test_measurement_then_forecast(self) -> None:
        text = ("日期：2026-11-12\n"
                "甲站8时水位12.30米，预计9时将达到13.2米。\n"
                "预计9时将达到13.2米，乙站8时水位12.40米。\n")
        self.write_text(text)
        self.write_stations([
            {"station_id": "01", "name": "甲站", "aliases": []},
            {"station_id": "02", "name": "乙站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 0)
        obs = self.read_observations()
        self.assertEqual(len(obs), 2)
        # 预报分句里的“9时”“13.2米”不污染实测：采样时刻仍是 8 时
        self.assert_obs_row(
            obs[0], obs_id="obs-0001", station_id="01", station_name="甲站",
            utc="2026-11-12T00:00:00Z", metric="水位", status="measured",
            value="12.3", unit="米", missing_reason="", source_no=1)
        self.assert_ref(text, obs[0]["time_ref"], "8时")
        self.assert_obs_row(
            obs[1], obs_id="obs-0002", station_id="02", station_name="乙站",
            utc="2026-11-12T00:00:00Z", metric="水位", status="measured",
            value="12.4", unit="米", missing_reason="", source_no=1)
        summary = self.read_summary()
        self.assertEqual(summary["counts"]["non_measurement"], 2)


# --------------------------------------------------------------------------- #
# 8. 缺报与真零、负水位与负流量
# --------------------------------------------------------------------------- #
class MissingZeroNegative(BulletinCase):

    def test_missing_zero_negative(self) -> None:
        text = ("日期：2026-11-12\n"
                "时刻：08:30\n"
                "甲站水位0米。\n"
                "乙站水位缺报。\n"
                "丙站水位-0.50米。\n"
                "丁站流量-5立方米每秒。\n"
                "戊站流量0立方米每秒。\n"
                "己站流量未测。\n")
        self.write_text(text)
        self.write_stations([
            {"station_id": "01", "name": "甲站", "aliases": []},
            {"station_id": "02", "name": "乙站", "aliases": []},
            {"station_id": "03", "name": "丙站", "aliases": []},
            {"station_id": "04", "name": "丁站", "aliases": []},
            {"station_id": "05", "name": "戊站", "aliases": []},
            {"station_id": "06", "name": "己站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        obs = self.read_observations()
        self.assertEqual(len(obs), 5)
        expected = [
            # 真零是有效实测，不是缺测
            ("obs-0001", "01", "水位", "measured", "0", "米", ""),
            # 明确缺报保留为缺测观测
            ("obs-0002", "02", "水位", "missing", "", "", "缺报"),
            # 水位负值合法
            ("obs-0003", "03", "水位", "measured", "-0.5", "米", ""),
            # 流量零值合法
            ("obs-0004", "05", "流量", "measured", "0", "立方米每秒", ""),
            ("obs-0005", "06", "流量", "missing", "", "", "未测"),
        ]
        for row, (obs_id, sid, metric, status, value, unit,
                  reason) in zip(obs, expected):
            self.assert_obs_row(
                row, obs_id=obs_id, station_id=sid,
                station_name=row["station_name"],
                utc="2026-11-12T00:30:00Z", metric=metric, status=status,
                value=value, unit=unit, missing_reason=reason, source_no=1)

        # 流量负值待核
        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reasons"], "negative_discharge")
        self.assertEqual(review[0]["station_id"], "04")
        self.assertEqual(review[0]["metric"], "流量")
        self.assert_ref(text, review[0]["span"], "流量-5立方米每秒")

        summary = self.read_summary()
        self.assertEqual(summary["counts"]["accepted"], 3)
        self.assertEqual(summary["counts"]["missing"], 2)
        self.assertEqual(summary["counts"]["other_review"], 1)


# --------------------------------------------------------------------------- #
# 9. 实测晚于发布 -> 待核；无时刻不用发布时间冒充
# --------------------------------------------------------------------------- #
class FutureAndMissingTime(BulletinCase):

    def test_future_observation_goes_to_review(self) -> None:
        text = ("测站：甲站\n"
                "日期：今日\n"
                "时刻：20时\n"
                "水位12.30米。\n"
                "时刻：08时\n"
                "水位12.40米。\n")
        self.write_text(text)
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
        ])
        # 发布 2026-11-13T08:00+08:00 = 2026-11-13T00:00:00Z
        rc = self.run_cli("2026-11-13T08:00:00+08:00")
        self.assertEqual(rc, 4)
        obs = self.read_observations()
        self.assertEqual(len(obs), 1)
        # 与发布时刻相等不算未来，正常接收
        self.assertEqual(obs[0]["sampled_at_utc"], "2026-11-13T00:00:00Z")
        self.assertEqual(obs[0]["value"], "12.4")
        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reasons"], "future_observation")
        # 未来实测不能自动改成预报：非实测计数里没有它
        summary = self.read_summary()
        self.assertEqual(summary["counts"]["non_measurement"], 0)

    def test_missing_time_not_filled_by_publish(self) -> None:
        text = ("日期：2026-11-12\n"
                "测站：甲站\n"
                "水位12.30米。\n")
        self.write_text(text)
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        # 数字看起来正常也不能用发布时间冒充采样时刻
        self.assertEqual(self.read_observations(), [])
        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reasons"], "no_time")


# --------------------------------------------------------------------------- #
# 10. 疑似实测但句法不支持 -> 待核，不静默消失
# --------------------------------------------------------------------------- #
class UnsupportedSyntax(BulletinCase):

    def test_unsupported_forms_go_to_review(self) -> None:
        text = ("测站：甲站\n"
                "日期：2026-11-12\n"
                "时刻：08:30\n"
                "水位约12米。\n"
                "水位12-13米。\n"
                "水位12.3。\n"
                "水位12.3英尺。\n"
                "12.3米。\n")
        self.write_text(text)
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        self.assertEqual(self.read_observations(), [])
        review = self.read_review()
        self.assertEqual(len(review), 5)
        for item in review:
            self.assertEqual(item["category"], "other")
            self.assertEqual(item["reasons"], "unsupported_syntax")
        texts = [item["text"] for item in review]
        self.assertEqual(texts, ["水位约12米", "水位12-13米", "水位12.3",
                                 "水位12.3英尺", "12.3米"])
        for item in review:
            self.assert_ref(text, item["span"], item["text"])
        summary = self.read_summary()
        self.assertEqual(summary["counts"]["other_review"], 5)


# --------------------------------------------------------------------------- #
# 11. 单位精确换算
# --------------------------------------------------------------------------- #
class UnitConversion(BulletinCase):

    def test_centimeters_and_millimeters(self) -> None:
        text = ("测站：甲站\n"
                "日期：2026-11-12\n"
                "时刻：08:30\n"
                "水位1230厘米。\n"
                "测站：乙站\n"
                "水位50毫米。\n"
                "测站：丙站\n"
                "流量250立方米每秒。\n")
        self.write_text(text)
        self.write_stations([
            {"station_id": "01", "name": "甲站", "aliases": []},
            {"station_id": "02", "name": "乙站", "aliases": []},
            {"station_id": "03", "name": "丙站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 0)
        obs = self.read_observations()
        self.assertEqual(len(obs), 3)
        # 1230 厘米 = 12.3 米；50 毫米 = 0.05 米；精确换算
        self.assertEqual((obs[0]["value"], obs[0]["unit"]), ("12.3", "米"))
        self.assertEqual((obs[1]["value"], obs[1]["unit"]), ("0.05", "米"))
        self.assertEqual((obs[2]["value"], obs[2]["unit"]),
                         ("250", "立方米每秒"))


# --------------------------------------------------------------------------- #
# 12. 输入无法读取时明确失败
# --------------------------------------------------------------------------- #
class UnreadableInputs(BulletinCase):

    def test_missing_text_file(self) -> None:
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 3)
        self.assertFalse(self.out_dir.exists())

    def test_missing_stations_file(self) -> None:
        self.write_text("甲站今8时水位12.30米。")
        rc = self.run_cli()
        self.assertEqual(rc, 2)

    def test_malformed_stations_json(self) -> None:
        self.write_text("甲站今8时水位12.30米。")
        self.stations_path.write_text("{not json", encoding="utf-8")
        rc = self.run_cli()
        self.assertEqual(rc, 2)

    def test_duplicate_station_id_rejected(self) -> None:
        self.write_text("甲站今8时水位12.30米。")
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
            {"station_id": "00101", "name": "乙站", "aliases": []},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 2)

    def test_reserved_word_in_alias_rejected(self) -> None:
        self.write_text("甲站今8时水位12.30米。")
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": ["水位站"]},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 2)

    def test_publish_time_requires_timezone(self) -> None:
        self.write_text("甲站今8时水位12.30米。")
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
        ])
        rc = self.run_cli("2026-11-13T10:00:00")
        self.assertEqual(rc, 2)
        rc = self.run_cli("不是时间")
        self.assertEqual(rc, 2)


# --------------------------------------------------------------------------- #
# 13. 更多边界：\r\n、歧义时刻、破折号缺报、一述两站、数值句式空白/冒号
# --------------------------------------------------------------------------- #
class ExtraEdges(BulletinCase):

    def setUp(self) -> None:
        super().setUp()
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": []},
            {"station_id": "00202", "name": "乙站", "aliases": []},
        ])

    def test_crlf_line_endings(self) -> None:
        text = ("测站：甲站\r\n日期：2026-11-12\r\n时刻：08:30\r\n"
                "水位12.30米。\r\n\r\n水位13.00米。\r\n")
        self.write_text(text)
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        obs = self.read_observations()
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["value"], "12.3")
        # 空行（只含 \r）同样清掉标题
        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reasons"],
                         "no_station;no_date;no_time")

    def test_ambiguous_time_in_one_statement(self) -> None:
        self.write_text("测站：甲站\n日期：2026-11-12\n"
                        "8时水位12.30米，9时水位12.40米。")
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        self.assertEqual(self.read_observations(), [])
        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reasons"], "ambiguous_time")

    def test_dash_missing_report(self) -> None:
        text = "测站：甲站\n日期：2026-11-12\n时刻：08:30\n水位——。\n"
        self.write_text(text)
        rc = self.run_cli()
        self.assertEqual(rc, 0)
        obs = self.read_observations()
        self.assertEqual(len(obs), 1)
        self.assert_obs_row(
            obs[0], obs_id="obs-0001", station_id="00101",
            station_name="甲站", utc="2026-11-12T00:30:00Z", metric="水位",
            status="missing", value="", unit="", missing_reason="——",
            source_no=1)
        self.assert_ref(text, obs[0]["value_ref"], "水位——")

    def test_24_colon_form_rolls_to_next_day(self) -> None:
        self.write_text("测站：甲站\n日期：2026-11-12\n时刻：24:00\n"
                        "水位10.00米。")
        rc = self.run_cli("2026-11-13T06:00:00+08:00")
        self.assertEqual(rc, 0)
        obs = self.read_observations()
        self.assertEqual(obs[0]["sampled_at_utc"], "2026-11-12T16:00:00Z")

    def test_two_stations_in_one_statement(self) -> None:
        self.write_text("日期：2026-11-12\n时刻：08:30\n"
                        "甲站乙站水位12.30米。")
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        self.assertEqual(self.read_observations(), [])
        review = self.read_review()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reasons"], "ambiguous_station")

    def test_ambiguous_station_header(self) -> None:
        self.stations_path.write_text(
            json.dumps({"schema_version": 1, "stations": [
                {"station_id": "01", "name": "江口站", "aliases": ["江口"]},
                {"station_id": "02", "name": "江口二站",
                 "aliases": ["江口"]},
            ]}, ensure_ascii=False),
            encoding="utf-8")
        self.write_text("测站：江口\n日期：2026-11-12\n时刻：08:30\n"
                        "水位12.30米。")
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        self.assertEqual(self.read_observations(), [])
        review = self.read_review()
        # 标题歧义一条 + 陈述因无标题可继承再无站一条
        self.assertEqual(len(review), 2)
        self.assertEqual(review[0]["reasons"], "ambiguous_station")
        self.assertEqual(review[1]["reasons"], "no_station")

    def test_value_syntax_allows_blank_and_colon(self) -> None:
        text = ("测站：甲站\n日期：2026-11-12\n时刻：08:30\n"
                "水位 ： 12.30 米。\n"
                "流量:500 立方米每秒。\n")
        self.write_text(text)
        rc = self.run_cli()
        self.assertEqual(rc, 0)
        obs = self.read_observations()
        self.assertEqual(len(obs), 2)
        self.assertEqual((obs[0]["value"], obs[0]["unit"]), ("12.3", "米"))
        self.assertEqual((obs[1]["value"], obs[1]["unit"]),
                         ("500", "立方米每秒"))

    def test_summary_reason_counts(self) -> None:
        self.write_text("测站：甲站\n日期：2026-11-12\n水位12.30米。")
        rc = self.run_cli()
        self.assertEqual(rc, 4)
        summary = self.read_summary()
        self.assertEqual(summary["review_reason_counts"], {"no_time": 1})
        self.assertEqual(summary["counts"]["review_total"], 1)


# --------------------------------------------------------------------------- #
# 14. 综合录入演练：一次真实调用，逐字段核对
# --------------------------------------------------------------------------- #
class FullExercise(BulletinCase):

    P1 = ("甲站今8时水位12.30米，较昨日涨0.20米，警戒水位13米；"
          "预计明晨达到13.2米。")
    P2 = ("日期：2026-11-12\n"
          "时刻：08:30\n"
          "测站：乙站\n"
          "水位13.40米，流量500立方米每秒。\n"
          "丙站水位13.45米。\n"
          "9时水位13.50米。")
    P3 = ("测站：丙站\n"
          "日期：2026-11-12\n"
          "时刻：09:00\n"
          "水位14.00米。\n"
          "江口水位15.00米。")
    P4 = "水位16.00米。"
    TEXT = P1 + "\n\n" + P2 + "\n\n" + P3 + "\n\n" + P4 + "\n"

    def test_full_exercise(self) -> None:
        text = self.TEXT
        self.write_text(text)
        self.write_stations([
            {"station_id": "00101", "name": "甲站", "aliases": ["甲"]},
            {"station_id": "00202", "name": "乙站", "aliases": []},
            {"station_id": "00303", "name": "丙站", "aliases": []},
            {"station_id": "00404", "name": "江口站", "aliases": ["江口"]},
            {"station_id": "00505", "name": "江口二站", "aliases": ["江口"]},
        ])
        rc = self.run_cli()
        self.assertEqual(rc, 4, "有歧义站名与无标题陈述 -> 部分接收")

        # ---- 手写观测表（站号 / 指标 / 值 / UTC 采样时刻 / 各字段原文片段）
        p2_date = span_of(text, "2026-11-12")
        p2_time = span_of(text, "08:30")
        p2_station = span_of(text, "乙站")
        p3_start = text.index(self.P3)
        p3_date = span_of(text, "2026-11-12", p3_start)
        p3_time = span_of(text, "09:00")
        p3_station = span_of(text, "丙站", p3_start)

        expected = [
            # obs-0001：开头通报只收实测水位
            dict(obs_id="obs-0001", station_id="00101", station_name="甲站",
                 utc="2026-11-13T00:00:00Z", metric="水位", value="12.3",
                 unit="米", station_frag="甲站", date_frag="今",
                 time_frag="8时", value_frag="水位12.30米"),
            # obs-0002：标题继承（乙站 + 日期 + 时刻都来自标题行）
            dict(obs_id="obs-0002", station_id="00202", station_name="乙站",
                 utc="2026-11-12T00:30:00Z", metric="水位", value="13.4",
                 unit="米", station_frag="乙站", date_frag="2026-11-12",
                 time_frag="08:30", value_frag="水位13.40米"),
            # obs-0003：同一陈述同时提供流量
            dict(obs_id="obs-0003", station_id="00202", station_name="乙站",
                 utc="2026-11-12T00:30:00Z", metric="流量", value="500",
                 unit="立方米每秒", station_frag="乙站",
                 date_frag="2026-11-12", time_frag="08:30",
                 value_frag="流量500立方米每秒"),
            # obs-0004：陈述自带 9 时覆盖标题时刻，站名与日期仍继承
            dict(obs_id="obs-0004", station_id="00202", station_name="乙站",
                 utc="2026-11-12T01:00:00Z", metric="水位", value="13.5",
                 unit="米", station_frag="乙站", date_frag="2026-11-12",
                 time_frag="9时", value_frag="水位13.50米"),
            # obs-0005：丙站陈述自带站名，日期时刻继承上一段标题
            dict(obs_id="obs-0005", station_id="00303", station_name="丙站",
                 utc="2026-11-12T00:30:00Z", metric="水位", value="13.45",
                 unit="米", station_frag="丙站", date_frag="2026-11-12",
                 time_frag="08:30", value_frag="水位13.45米"),
            # obs-0006：新一段的标题重新给站名/日期/时刻
            dict(obs_id="obs-0006", station_id="00303", station_name="丙站",
                 utc="2026-11-12T01:00:00Z", metric="水位", value="14",
                 unit="米", station_frag="丙站", date_frag="2026-11-12",
                 time_frag="09:00", value_frag="水位14.00米"),
        ]
        obs = self.read_observations()
        self.assertEqual(len(obs), len(expected))
        for row, exp in zip(obs, expected):
            self.assert_obs_row(
                row, obs_id=exp["obs_id"], station_id=exp["station_id"],
                station_name=exp["station_name"], utc=exp["utc"],
                metric=exp["metric"], status="measured",
                value=exp["value"], unit=exp["unit"], missing_reason="",
                source_no=1)
            # 逐字段切回源文本验证位置
            for key, frag_key in (("station_ref", "station_frag"),
                                  ("date_ref", "date_frag"),
                                  ("time_ref", "time_frag"),
                                  ("value_ref", "value_frag")):
                self.assert_ref(text, row[key], exp[frag_key])
        # 继承标题的字段必须指向标题行本身
        self.assertEqual(obs[1]["station_ref"], ref_of(text, *p2_station))
        self.assertEqual(obs[1]["date_ref"], ref_of(text, *p2_date))
        self.assertEqual(obs[1]["time_ref"], ref_of(text, *p2_time))
        self.assertEqual(obs[5]["station_ref"], ref_of(text, *p3_station))
        self.assertEqual(obs[5]["date_ref"], ref_of(text, *p3_date))
        self.assertEqual(obs[5]["time_ref"], ref_of(text, *p3_time))
        # 陈述显式时刻指向陈述自己，而不是标题
        self.assertEqual(obs[3]["time_ref"],
                         ref_of(text, *span_of(text, "9时")))
        self.assert_ref(text, obs[3]["time_ref"], "9时")

        # ---- 手写待核清单
        review = self.read_review()
        self.assertEqual(len(review), 2)
        # 歧义别名“江口”对应 00404/00505 两站 -> 待核，不取第一项
        self.assertEqual(review[0]["category"], "other")
        self.assertEqual(review[0]["reasons"], "ambiguous_station")
        self.assert_ref(text, review[0]["span"], "江口水位15.00米")
        # 空行后的陈述没有任何标题可继承
        self.assertEqual(review[1]["category"], "other")
        self.assertEqual(review[1]["reasons"],
                         "no_station;no_date;no_time")
        self.assert_ref(text, review[1]["span"], "水位16.00米")

        # ---- 接收摘要
        summary = self.read_summary()
        self.assertEqual(summary["counts"], {
            "paragraphs": 4,
            "statements": 8,
            "accepted": 6,
            "missing": 0,
            "conflict": 0,
            "other_review": 2,
            "review_total": 2,
            "non_measurement": 3,
        })
        self.assertEqual(summary["inputs"]["published_at"], PUBLISH)
        self.assertEqual(summary["inputs"]["published_at_utc"],
                         "2026-11-13T02:00:00Z")
        # 非实测内容位置同样可切回
        nm_texts = [it["text"] for it in summary["non_measurement_items"]]
        self.assertEqual(nm_texts, ["较昨日涨0.20米", "警戒水位13米",
                                    "预计明晨达到13.2米"])
        for item in summary["non_measurement_items"]:
            self.assert_ref(text, item["span"], item["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
