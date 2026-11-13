"""水情通报接收（纯本地、标准库实现）。

把值班室收到的**固定格式**中文水情通报整理为观测表与待核清单。本模块只做
有限句法的规则抽取，**不理解任意自然语言**：不在支持句法内的疑似实测内容
一律进入待核清单，绝不静默丢弃；比较量、参考水位与预报内容只作非实测记录。
本模块只接收通报中明确给出的水位/流量实测值：不推算流量、不预测、不改动
任何既有计算链（不 import numpy / rasterio / pandas）。

有限句法
--------
* 空行划分段落；段内可有独立标题行 ``日期：…`` ``时刻：…`` ``测站：…``
  （冒号中英文均可，整行只能是标题）。标题只向后生效，空行清掉全部标题，
  绝不从后一条回填；陈述里的显式信息只管本条，不改标题。
* 陈述以句号 ``。``、分号 ``；;`` 或换行分开；逗号 ``，,`` 连接同一陈述
  中的字段。
* 日期：``2026-11-12`` / ``2026年11月12日`` / ``11月12日`` / ``今日`` /
  ``昨日``；``今8时`` 按今日 8 时解释（昨同理）。月日未写年时取发布当年，
  晚于发布日才退到上一年；非法日期（如 2月30日）不跨年猜补，直接待核。
* 时刻：``8时`` / ``8时30分`` / ``08:30``；``24时`` 仅整点合法并归入次日
  零点，``24时30分`` 非法。采样时刻一律按发布时间携带的时区解释并写成
  UTC；落在发布时刻之后的“实测”进入待核，不改当预报；没有时刻的陈述绝不
  用发布时间冒充采样时刻。
* 数值：``水位 数字 (米|厘米|毫米)``、``流量 数字 立方米每秒``，关键词与
  数字之间允许空白或冒号；数字可带正负号，不接受范围或约数。换算用
  Decimal 精确进行。``缺报``/``未测``/``缺测``/破折号记为缺测观测，绝不
  当零；水位负值与零值合法，流量负值待核。

站名匹配与边界
--------------
* 对照表列出正式站号（字符串，前导零有意义）、名称与别名；同一名称/别名
  可对应两个站号，遇到时该陈述进入待核，不取第一项。
* 匹配从左到右扫描，同一起点**最长名称优先**（短别名不会抢走长站名的一
  部分）；名称构成字符 = 汉字 / ASCII 字母 / 数字。
* 左邻字符不能是构成字符；**单字别名**的右邻字符也不能是构成字符（所以
  “江水上涨”里的“江”不算测站，而“甲站今8时…”里的“甲站”算）。多字
  名称的右侧不做边界要求，因为最长优先已经挡住更长的已登记名称。

非实测与待核
------------
* 含 ``预计``/``预报``/``将达到`` 的分句、含 ``涨``/``落`` 的比较分句、
  ``警戒水位``/``保证水位``/``历史最高水位``/``历史最低水位`` 等参考量，
  只作非实测内容记录，按分句分别判断；同一陈述前面的实测值不会被后面的
  预计值顶掉，也不会被它污染（预报/比较分句不提供站名、日期与时刻）。
* 疑似实测但不在支持句法内的内容（约数、范围、缺单位、单位与指标不匹配、
  缺关键词的裸数值等）进入待核，不静默消失。
* 同站号、同采样时刻、同指标的同值发布合成一条并保存全部出处；不同值
  （含一处缺报一处实数）把该指标整组来源移入冲突待核，不任意留一条；
  另一个指标与无关站点继续接收。

输出
----
``bulletin_observations.csv``（观测明细，含缺测）、``bulletin_review.csv``
（待核明细）、``bulletin_summary.json``（接收摘要）。每条观测记录站号、
UTC 采样时刻、指标、统一单位值或缺测原因，并为站名、日期、时刻、数值分别
记录原文位置：0 起 Unicode 字符半开区间 + 1 起行列；继承标题时位置指向
标题行本身；用这些位置切回原文必须得到记录的文字。全部输出先写临时文件、
回读校验再原子替换。摘要区分已收、缺测、冲突、其他待核与明确非实测数量；
存在待核项时命令以非零状态结束（部分接收），已确认结果仍可查看。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .hashing import sha256_file

# --------------------------------------------------------------------------- #
# 常量：文件名、指标、单位、理由码
# --------------------------------------------------------------------------- #
OBSERVATIONS_FILENAME = "bulletin_observations.csv"
REVIEW_FILENAME = "bulletin_review.csv"
SUMMARY_FILENAME = "bulletin_summary.json"

SCHEMA_VERSION = 1

METRIC_STAGE = "水位"
METRIC_DISCHARGE = "流量"
METRICS = (METRIC_STAGE, METRIC_DISCHARGE)

STATUS_MEASURED = "measured"
STATUS_MISSING = "missing"

UNIT_FOR_METRIC = {METRIC_STAGE: "米", METRIC_DISCHARGE: "立方米每秒"}

# 待核理由码
R_NO_STATION = "no_station"                # 没有唯一测站（未写且无标题）
R_AMBIGUOUS_STATION = "ambiguous_station"  # 多个站号（含歧义别名、多站名）
R_UNKNOWN_STATION = "unknown_station"      # 测站标题不在对照表内
R_NO_DATE = "no_date"                      # 缺采样日期
R_NO_TIME = "no_time"                      # 缺采样时刻
R_INVALID_DATE = "invalid_date"            # 非法日期（不跨年猜补）
R_INVALID_TIME = "invalid_time"            # 非法时刻（如 24时30分）
R_AMBIGUOUS_DATE = "ambiguous_date"        # 同一陈述多个不同日期
R_AMBIGUOUS_TIME = "ambiguous_time"        # 同一陈述多个不同时刻
R_FUTURE = "future_observation"            # 实测时刻晚于发布时刻
R_NEGATIVE_DISCHARGE = "negative_discharge"  # 流量负值
R_UNSUPPORTED = "unsupported_syntax"       # 疑似实测但不在支持句法内
R_MALFORMED_HEADER = "malformed_header"    # 标题行无法解析
R_CONFLICT = "conflict"                    # 同站同刻同指标不同内容

CATEGORY_CONFLICT = "conflict"
CATEGORY_OTHER = "other"

# 非实测内容类别
NM_FORECAST = "forecast"          # 含 预计/预报/将达到 的分句
NM_COMPARISON = "comparison"      # 含 涨/落 的比较分句
NM_REFERENCE = "reference_level"  # 警戒/保证/历史水位等参考量

FORECAST_WORDS = ("预计", "预报", "将达到")
COMPARISON_WORDS = ("涨", "落")
MISSING_WORDS = ("缺报", "未测", "缺测")

OBS_HEADER = [
    "observation_id", "station_id", "station_name", "sampled_at_utc",
    "metric", "status", "value", "unit", "missing_reason", "source_no",
    "station_ref", "station_text", "date_ref", "date_text",
    "time_ref", "time_text", "value_ref", "value_text",
]
REVIEW_HEADER = [
    "review_id", "category", "reasons", "station_id", "sampled_at_utc",
    "metric", "detail", "span", "text",
]


class BulletinConfigError(ValueError):
    """站名对照或发布时间非法/无法读取。"""


class BulletinInputError(ValueError):
    """通报文本无法读取。"""


class BulletinOutputError(RuntimeError):
    """写出或回读校验失败。"""


# --------------------------------------------------------------------------- #
# 正则：标点分隔、标题、日期、时刻、数值
# --------------------------------------------------------------------------- #
#: 名称构成字符：汉字（含〇与扩展、兼容字）/ ASCII 字母 / 数字
_NAME_CONSTITUENT = re.compile(
    r"[0-9A-Za-z〇㐀-䶿一-鿿豈-﫿]")

_HEADER_RE = re.compile(r"^\s*(日期|时刻|测站)\s*[:：]\s*(.*?)\s*$")

_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日(?!\d)"
    r"|(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)"
    r"|(?<!\d)(\d{1,2})月(\d{1,2})日(?!\d)"
    r"|今日|昨日"
)

_TIME_RE = re.compile(
    r"(?<!\d)(\d{1,2})时(?:(\d{1,2})分)?"
    r"|(?<!\d)(\d{1,2}):(\d{2})(?!\d)"
)

_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
_UNITS = r"(?:立方米每秒|厘米|毫米|米)"

_MEASURE_RE = re.compile(
    r"(水位|流量)[ \t]*[:：]?[ \t]*(" + _NUMBER + r")[ \t]*(" + _UNITS + r")")
_MISSING_RE = re.compile(
    r"(水位|流量)[ \t]*[:：]?[ \t]*"
    r"(缺报|未测|缺测|(?:—|–|―|−)+|-+(?!\d))")
_KEYWORD_RE = re.compile(r"水位|流量")
_BARE_VALUE_RE = re.compile(
    r"(?<!\d)(" + _NUMBER + r")[ \t]*(" + _UNITS + r")")
_REF_LEVEL_RE = re.compile(
    r"(?:警戒水位|保证水位|历史最高水位|历史最低水位)"
    r"(?:[ \t]*[:：]?[ \t]*(?:" + _NUMBER + r")?[ \t]*(?:" + _UNITS + r")?)?")

_STATEMENT_SEPS = frozenset("。；;")
_CLAUSE_SEPS = frozenset("，,")

_UNIT_FACTOR = {
    "米": Decimal("1"),
    "厘米": Decimal("0.01"),
    "毫米": Decimal("0.001"),
    "立方米每秒": Decimal("1"),
}
_STAGE_UNITS = frozenset({"米", "厘米", "毫米"})
_DISCHARGE_UNITS = frozenset({"立方米每秒"})

#: 站名/别名中禁止出现的字符与保留字（出现会让固定句法无法可靠切分）
_TOKEN_SEPARATORS = frozenset("，。；;,:：")
_TOKEN_RESERVED_WORDS = ("水位", "流量", "涨", "落")


# --------------------------------------------------------------------------- #
# 位置：0 起字符半开区间 + 1 起行列
# --------------------------------------------------------------------------- #
class _LineIndex:
    """把字符偏移换算为 1 起 (行, 列)。"""

    def __init__(self, text: str) -> None:
        self._starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self._starts.append(i + 1)

    def line_col(self, offset: int) -> tuple[int, int]:
        line = bisect_right(self._starts, offset)
        return line, offset - self._starts[line - 1] + 1


def _fmt_span(index: _LineIndex, start: int, end: int) -> str:
    """位置引用格式：``c{起}-{止}@L{起行}C{起列}-L{止行}C{止列}``（止为开区间）。"""
    ls, cs = index.line_col(start)
    le, ce = index.line_col(end)
    return f"c{start}-{end}@L{ls}C{cs}-L{le}C{ce}"


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_str(value: Decimal) -> str:
    """统一单位的十进制呈现（去掉多余尾零，不用科学计数法）。"""
    return format(value.normalize(), "f")


# --------------------------------------------------------------------------- #
# 站名对照
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StationEntry:
    station_id: str
    name: str
    aliases: tuple[str, ...]


class StationRegistry:
    """正式站号（前导零有意义）+ 名称/别名 -> 站号集合的索引。"""

    def __init__(self, entries: dict[str, StationEntry]) -> None:
        self.entries = entries
        name_to_ids: dict[str, set[str]] = {}
        for entry in entries.values():
            for token in (entry.name, *entry.aliases):
                name_to_ids.setdefault(token, set()).add(entry.station_id)
        self.name_to_ids = {k: tuple(sorted(v)) for k, v in name_to_ids.items()}
        #: 同一起点最长优先；长度相同按字典序保证确定性
        self.names_by_length = sorted(
            self.name_to_ids, key=lambda n: (-len(n), n))

    # ------------------------------------------------------------------ #
    @classmethod
    def from_file(cls, path: str | Path) -> "StationRegistry":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise BulletinConfigError(
                f"站名对照不是合法 JSON: {exc}") from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise BulletinConfigError(
                f"无法读取站名对照 {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise BulletinConfigError("站名对照顶层必须是对象")
        unknown = sorted(set(data) - {"schema_version", "stations"})
        if unknown:
            raise BulletinConfigError(f"站名对照含未知顶层字段: {unknown}")
        raw_stations = data.get("stations")
        if not isinstance(raw_stations, list) or not raw_stations:
            raise BulletinConfigError("stations 必须是非空数组")
        entries: dict[str, StationEntry] = {}
        for idx, raw in enumerate(raw_stations):
            if not isinstance(raw, dict):
                raise BulletinConfigError(f"第 {idx} 个测站条目必须是对象")
            unknown = sorted(set(raw) - {"station_id", "name", "aliases"})
            if unknown:
                raise BulletinConfigError(
                    f"第 {idx} 个测站条目含未知字段: {unknown}")
            station_id = raw.get("station_id")
            if not isinstance(station_id, str) or not station_id:
                raise BulletinConfigError(
                    f"第 {idx} 个测站条目的 station_id 必须是非空字符串"
                    "（前导零有意义，按原样保留）")
            if station_id in entries:
                raise BulletinConfigError(f"站号重复: {station_id!r}")
            aliases = raw.get("aliases", [])
            if not isinstance(aliases, list):
                raise BulletinConfigError(
                    f"测站 {station_id} 的 aliases 必须是数组")
            tokens = [raw.get("name"), *aliases]
            seen: list[str] = []
            for token in tokens:
                _validate_token(token, station_id)
                if token not in seen:
                    seen.append(token)
            entries[station_id] = StationEntry(
                station_id, seen[0], tuple(seen[1:]))
        return cls(entries)


def _validate_token(token: object, station_id: str) -> None:
    if not isinstance(token, str) or not token:
        raise BulletinConfigError(
            f"测站 {station_id} 的名称/别名必须是非空字符串")
    if any(ch.isspace() for ch in token):
        raise BulletinConfigError(
            f"测站 {station_id} 的名称/别名不能含空白字符: {token!r}")
    bad = sorted(set(token) & _TOKEN_SEPARATORS)
    if bad:
        raise BulletinConfigError(
            f"测站 {station_id} 的名称/别名不能含分隔标点 {bad}: {token!r}")
    for word in _TOKEN_RESERVED_WORDS:
        if word in token:
            raise BulletinConfigError(
                f"测站 {station_id} 的名称/别名含句法保留字 {word!r}: "
                f"{token!r}（该词用于识别实测/比较内容）")


# --------------------------------------------------------------------------- #
# 日期与时刻
# --------------------------------------------------------------------------- #
def _valid_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _resolve_month_day(month: int, day: int, publish_date: date) -> date | None:
    """月日未写年：取发布当年，晚于发布日才退上一年；非法日期不跨年猜补。"""
    candidate = _valid_date(publish_date.year, month, day)
    if candidate is None:
        return None
    if candidate > publish_date:
        candidate = _valid_date(publish_date.year - 1, month, day)
    return candidate


def _classify_date_match(match: re.Match, publish_date: date) -> date | None:
    token = match.group(0)
    if token == "今日":
        return publish_date
    if token == "昨日":
        return publish_date - timedelta(days=1)
    if match.group(1) is not None:      # 年月日
        return _valid_date(int(match.group(1)), int(match.group(2)),
                           int(match.group(3)))
    if match.group(4) is not None:      # 年-月-日
        return _valid_date(int(match.group(4)), int(match.group(5)),
                           int(match.group(6)))
    return _resolve_month_day(int(match.group(7)), int(match.group(8)),
                              publish_date)


def _parse_time_parts(hour_text: str, minute_text: str | None):
    """返回 (时, 分) 或 None（非法）。24 时仅整点合法，归入次日零点。"""
    hour = int(hour_text)
    minute = int(minute_text) if minute_text else 0
    if hour == 24:
        return (24, 0) if minute == 0 else None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return (hour, minute)
    return None


def _parse_header_date(value: str, publish_date: date) -> date | None:
    match = _DATE_RE.fullmatch(value)
    if match is None:
        return None
    return _classify_date_match(match, publish_date)


def _parse_header_time(value: str):
    match = _TIME_RE.fullmatch(value)
    if match is None:
        return None
    if match.group(1) is not None:
        return _parse_time_parts(match.group(1), match.group(2))
    return _parse_time_parts(match.group(3), match.group(4))


def _parse_published_at(raw: str):
    """发布时间必须带时区偏移；采样时刻按该（固定偏移）时区解释。"""
    if not isinstance(raw, str):
        raise BulletinConfigError(f"发布时间必须是字符串: {raw!r}")
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise BulletinConfigError(
            f"发布时间不是合法 ISO 8601: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise BulletinConfigError("发布时间必须携带时区偏移，拒绝猜测")
    return parsed.astimezone(timezone.utc), parsed.tzinfo, parsed.date()


# --------------------------------------------------------------------------- #
# 内部数据结构
# --------------------------------------------------------------------------- #
@dataclass
class _StationMention:
    span: tuple[int, int]
    token: str
    station_ids: tuple[str, ...]


@dataclass
class _DateMention:
    span: tuple[int, int]
    value: date


@dataclass
class _TimeMention:
    span: tuple[int, int]
    hour: int
    minute: int


@dataclass
class _Candidate:
    """一条疑似实测（数值或缺报），尚未通过站/时校验。"""

    metric: str
    value: Decimal | None        # None 表示缺报
    missing_token: str | None
    span: tuple[int, int]        # 数值/缺报提及的原文区间


@dataclass
class _StatementCtx:
    span: tuple[int, int]
    station_mentions: list[_StationMention] = field(default_factory=list)
    date_mentions: list[_DateMention] = field(default_factory=list)
    invalid_date_spans: list[tuple[int, int]] = field(default_factory=list)
    time_mentions: list[_TimeMention] = field(default_factory=list)
    invalid_time_spans: list[tuple[int, int]] = field(default_factory=list)
    candidates: list[_Candidate] = field(default_factory=list)


@dataclass
class _ReviewItem:
    category: str                # conflict | other
    reasons: list[str]
    detail: str
    span: tuple[int, int]
    station_id: str = ""
    sampled_at_utc: str = ""
    metric: str = ""


@dataclass
class _NonMeasurement:
    kind: str                    # forecast | comparison | reference_level
    span: tuple[int, int]


@dataclass
class _Accepted:
    """通过站/时校验、等待合并的一条实测或缺报。"""

    station_id: str
    instant: datetime            # UTC
    metric: str
    value: Decimal | None
    missing_token: str | None
    station_span: tuple[int, int]
    date_span: tuple[int, int]
    time_span: tuple[int, int]
    value_span: tuple[int, int]


@dataclass
class _Observation:
    station_id: str
    instant: datetime
    metric: str
    status: str                  # measured | missing
    value: Decimal | None
    missing_tokens: list[str]
    sources: list[_Accepted]


# --------------------------------------------------------------------------- #
# 文本切分：行 -> 段落 -> 陈述 -> 分句
# --------------------------------------------------------------------------- #
def _split_lines(text: str) -> list[tuple[int, str]]:
    """(行起始偏移, 不含换行符的行文本)；换行符本身计入原文偏移。"""
    lines: list[tuple[int, str]] = []
    i = 0
    n = len(text)
    while i < n:
        j = text.find("\n", i)
        if j == -1:
            lines.append((i, text[i:]))
            break
        lines.append((i, text[i:j]))
        i = j + 1
    return lines


def _paragraphs(lines: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """空行（全空白行）划分段落。"""
    paras: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for start, line_text in lines:
        if line_text.strip() == "":
            if current:
                paras.append(current)
                current = []
        else:
            current.append((start, line_text))
    if current:
        paras.append(current)
    return paras


def _split_pieces(global_start: int, text: str,
                  seps: frozenset) -> list[tuple[int, int, str]]:
    """按分隔符切分并修剪空白，返回 (全局起点, 全局终点, 片段文本)。"""
    pieces: list[tuple[int, int]] = []
    seg_start = 0
    for idx, ch in enumerate(text):
        if ch in seps:
            pieces.append((seg_start, idx))
            seg_start = idx + 1
    pieces.append((seg_start, len(text)))
    out: list[tuple[int, int, str]] = []
    for a, b in pieces:
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if a < b:
            out.append((global_start + a, global_start + b, text[a:b]))
    return out


# --------------------------------------------------------------------------- #
# 分句抽取：参考量 -> 站名 -> 日期 -> 时刻 -> 实测/缺报 -> 裸数值
# --------------------------------------------------------------------------- #
def _extract_clause(clause_text: str, clause_start: int,
                    ctx: _StatementCtx, registry: StationRegistry,
                    publish_date: date,
                    review_items: list[_ReviewItem],
                    non_measurements: list[_NonMeasurement]) -> None:
    masked = list(clause_text)

    def mask(a: int, b: int) -> None:
        for i in range(a, b):
            masked[i] = "\x00"

    def current() -> str:
        return "".join(masked)

    def gspan(a: int, b: int) -> tuple[int, int]:
        return (clause_start + a, clause_start + b)

    # 1) 警戒/保证/历史水位等参考量：非实测，先遮蔽，避免“水位13米”被当实测
    for m in _REF_LEVEL_RE.finditer(clause_text):
        non_measurements.append(
            _NonMeasurement(NM_REFERENCE, gspan(m.start(), m.end())))
        mask(m.start(), m.end())

    # 2) 站名：从左到右、同起点最长优先；左邻不能是构成字符，单字别名右邻也不能
    n = len(clause_text)
    i = 0
    while i < n:
        token = None
        if masked[i] != "\x00":
            for name in registry.names_by_length:
                if clause_text.startswith(name, i) and \
                        "\x00" not in masked[i:i + len(name)]:
                    token = name
                    break
        if token is None:
            i += 1
            continue
        left_ok = i == 0 or not _NAME_CONSTITUENT.match(masked[i - 1])
        right_ok = True
        if len(token) == 1:
            right_ok = i + 1 >= n or not _NAME_CONSTITUENT.match(masked[i + 1])
        if left_ok and right_ok:
            ctx.station_mentions.append(_StationMention(
                gspan(i, i + len(token)), token, registry.name_to_ids[token]))
            mask(i, i + len(token))
            i += len(token)
        else:
            i += 1

    # 3) 日期
    for m in _DATE_RE.finditer(current()):
        value = _classify_date_match(m, publish_date)
        span = gspan(m.start(), m.end())
        if value is None:
            ctx.invalid_date_spans.append(span)
        else:
            ctx.date_mentions.append(_DateMention(span, value))
        mask(m.start(), m.end())

    # 4) 时刻；紧邻的 今/昨 前缀同时给出日期
    text_now = current()
    for m in _TIME_RE.finditer(text_now):
        if m.group(1) is not None:
            parts = _parse_time_parts(m.group(1), m.group(2))
        else:
            parts = _parse_time_parts(m.group(3), m.group(4))
        span = gspan(m.start(), m.end())
        if m.start() > 0 and text_now[m.start() - 1] in ("今", "昨"):
            prefix = text_now[m.start() - 1]
            day = publish_date if prefix == "今" else \
                publish_date - timedelta(days=1)
            ctx.date_mentions.append(
                _DateMention(gspan(m.start() - 1, m.start()), day))
            mask(m.start() - 1, m.start())
        if parts is None:
            ctx.invalid_time_spans.append(span)
        else:
            ctx.time_mentions.append(_TimeMention(span, parts[0], parts[1]))
        mask(m.start(), m.end())

    # 5) 实测 / 缺报 / 疑似实测
    pos = 0
    while True:
        km = _KEYWORD_RE.search(current(), pos)
        if km is None:
            break
        k = km.start()
        metric = km.group(0)
        cur = current()
        mm = _MEASURE_RE.match(cur, k)
        if mm is not None:
            unit = mm.group(3)
            unit_ok = (metric == METRIC_STAGE and unit in _STAGE_UNITS) or \
                (metric == METRIC_DISCHARGE and unit in _DISCHARGE_UNITS)
            if unit_ok:
                value = Decimal(mm.group(2)) * _UNIT_FACTOR[unit]
                ctx.candidates.append(_Candidate(
                    metric, value, None, gspan(mm.start(), mm.end())))
            else:
                review_items.append(_ReviewItem(
                    CATEGORY_OTHER, [R_UNSUPPORTED],
                    f"疑似实测但单位与指标不匹配: {cur[mm.start():mm.end()]}",
                    gspan(mm.start(), mm.end()), metric=metric))
            mask(mm.start(), mm.end())
            pos = mm.end()
            continue
        ms = _MISSING_RE.match(cur, k)
        if ms is not None:
            ctx.candidates.append(_Candidate(
                metric, None, ms.group(2), gspan(ms.start(), ms.end())))
            mask(ms.start(), ms.end())
            pos = ms.end()
            continue
        # 疑似实测但不在支持句法内：遮蔽到下一个关键词或分句末，不静默丢弃
        nxt = _KEYWORD_RE.search(cur, k + len(metric))
        end = nxt.start() if nxt is not None else len(clause_text)
        while end > k and clause_text[end - 1].isspace():
            end -= 1
        if end <= k:
            end = k + len(metric)
        review_items.append(_ReviewItem(
            CATEGORY_OTHER, [R_UNSUPPORTED],
            f"疑似实测但不在支持句法内: {clause_text[k:end]}",
            gspan(k, end), metric=metric))
        mask(k, end)
        pos = end

    # 6) 缺关键词的裸数值+单位：疑似实测，进入待核
    for m in _BARE_VALUE_RE.finditer(current()):
        review_items.append(_ReviewItem(
            CATEGORY_OTHER, [R_UNSUPPORTED],
            f"疑似实测数值但缺少指标关键词: {m.group(0)}",
            gspan(m.start(), m.end())))
        mask(m.start(), m.end())


# --------------------------------------------------------------------------- #
# 陈述处理：分句分类 -> 抽取 -> 站/时校验 -> 接收或待核
# --------------------------------------------------------------------------- #
def _process_statement(span: tuple[int, int], stmt_text: str,
                       headers: dict, registry: StationRegistry,
                       publish_date: date, publish_tz,
                       publish_utc: datetime,
                       review_items: list[_ReviewItem],
                       non_measurements: list[_NonMeasurement],
                       accepted: list[_Accepted]) -> None:
    ctx = _StatementCtx(span=span)
    for c_start, c_end, c_text in _split_pieces(span[0], stmt_text,
                                                _CLAUSE_SEPS):
        if any(w in c_text for w in FORECAST_WORDS):
            non_measurements.append(_NonMeasurement(NM_FORECAST,
                                                    (c_start, c_end)))
            continue
        if any(w in c_text for w in COMPARISON_WORDS):
            non_measurements.append(_NonMeasurement(NM_COMPARISON,
                                                    (c_start, c_end)))
            continue
        _extract_clause(c_text, c_start, ctx, registry, publish_date,
                        review_items, non_measurements)
    if not ctx.candidates:
        return

    problems: list[str] = []
    # 测站：陈述显式优先，否则继承标题；必须唯一
    station_id = None
    station_span = None
    if ctx.station_mentions:
        ids: set[str] = set()
        for mention in ctx.station_mentions:
            ids.update(mention.station_ids)
        if len(ids) == 1:
            station_id = next(iter(ids))
            station_span = ctx.station_mentions[0].span
        else:
            problems.append(R_AMBIGUOUS_STATION)
    elif headers["station"] is not None:
        station_id, station_span = headers["station"]
    else:
        problems.append(R_NO_STATION)

    # 日期：非法 > 歧义 > 显式 > 标题 > 缺失
    date_value = None
    date_span = None
    if ctx.invalid_date_spans:
        problems.append(R_INVALID_DATE)
    else:
        distinct_dates = {m.value for m in ctx.date_mentions}
        if len(distinct_dates) > 1:
            problems.append(R_AMBIGUOUS_DATE)
        elif len(distinct_dates) == 1:
            date_value = ctx.date_mentions[0].value
            date_span = ctx.date_mentions[0].span
        elif headers["date"] is not None:
            date_value, date_span = headers["date"]
        else:
            problems.append(R_NO_DATE)

    # 时刻：非法 > 歧义 > 显式 > 标题 > 缺失
    time_value = None
    time_span = None
    if ctx.invalid_time_spans:
        problems.append(R_INVALID_TIME)
    else:
        distinct_times = {(m.hour, m.minute) for m in ctx.time_mentions}
        if len(distinct_times) > 1:
            problems.append(R_AMBIGUOUS_TIME)
        elif len(distinct_times) == 1:
            mention = ctx.time_mentions[0]
            time_value = (mention.hour, mention.minute)
            time_span = mention.span
        elif headers["time"] is not None:
            time_value, time_span = headers["time"]
        else:
            problems.append(R_NO_TIME)

    if problems:
        review_items.append(_ReviewItem(
            CATEGORY_OTHER, problems, _problem_detail(ctx, problems), span))
        return

    hour, minute = time_value
    sample_date = date_value
    if hour == 24:  # 24 时整点归入次日零点
        sample_date = sample_date + timedelta(days=1)
        hour = 0
    local = datetime(sample_date.year, sample_date.month, sample_date.day,
                     hour, minute, tzinfo=publish_tz)
    instant_utc = local.astimezone(timezone.utc)
    if instant_utc > publish_utc:
        review_items.append(_ReviewItem(
            CATEGORY_OTHER, [R_FUTURE],
            f"实测时刻 {_utc_iso(instant_utc)} 晚于发布时刻 "
            f"{_utc_iso(publish_utc)}，不改当预报",
            span, station_id=station_id,
            sampled_at_utc=_utc_iso(instant_utc)))
        return

    for cand in ctx.candidates:
        if cand.metric == METRIC_DISCHARGE and cand.value is not None \
                and cand.value < 0:
            review_items.append(_ReviewItem(
                CATEGORY_OTHER, [R_NEGATIVE_DISCHARGE],
                f"流量负值: {_decimal_str(cand.value)} 立方米每秒",
                cand.span, station_id=station_id,
                sampled_at_utc=_utc_iso(instant_utc), metric=cand.metric))
            continue
        accepted.append(_Accepted(
            station_id=station_id, instant=instant_utc, metric=cand.metric,
            value=cand.value, missing_token=cand.missing_token,
            station_span=station_span, date_span=date_span,
            time_span=time_span, value_span=cand.span))


def _problem_detail(ctx: _StatementCtx, problems: list[str]) -> str:
    desc = []
    for cand in ctx.candidates:
        if cand.value is not None:
            desc.append(f"{cand.metric} {_decimal_str(cand.value)}")
        else:
            desc.append(f"{cand.metric} {cand.missing_token}")
    return ("疑似实测陈述缺少必要条件 (" + ";".join(problems) + "): "
            + ", ".join(desc))


# --------------------------------------------------------------------------- #
# 标题行
# --------------------------------------------------------------------------- #
def _process_header(match: re.Match, line_start: int, line_text: str,
                    headers: dict, registry: StationRegistry,
                    publish_date: date,
                    review_items: list[_ReviewItem]) -> None:
    kind = match.group(1)
    value = match.group(2)
    vspan = (line_start + match.start(2), line_start + match.end(2))
    line_span = (line_start, line_start + len(line_text))
    key = {"日期": "date", "时刻": "time", "测站": "station"}[kind]
    if not value:
        review_items.append(_ReviewItem(
            CATEGORY_OTHER, [R_MALFORMED_HEADER],
            f"标题缺少内容: {line_text.strip()}", line_span))
        headers[key] = None
        return
    if kind == "日期":
        parsed = _parse_header_date(value, publish_date)
        if parsed is None:
            review_items.append(_ReviewItem(
                CATEGORY_OTHER, [R_MALFORMED_HEADER],
                f"无法理解的日期标题（非法日期不跨年猜补）: {value}", vspan))
            headers["date"] = None
        else:
            headers["date"] = (parsed, vspan)
    elif kind == "时刻":
        parsed = _parse_header_time(value)
        if parsed is None:
            review_items.append(_ReviewItem(
                CATEGORY_OTHER, [R_MALFORMED_HEADER],
                f"无法理解的时刻标题（24时仅整点合法）: {value}", vspan))
            headers["time"] = None
        else:
            headers["time"] = (parsed, vspan)
    else:  # 测站
        ids = registry.name_to_ids.get(value)
        if ids is None:
            review_items.append(_ReviewItem(
                CATEGORY_OTHER, [R_UNKNOWN_STATION],
                f"测站标题不在对照表内: {value}", vspan))
            headers["station"] = None
        elif len(ids) > 1:
            review_items.append(_ReviewItem(
                CATEGORY_OTHER, [R_AMBIGUOUS_STATION],
                f"测站标题对应多个站号 {list(ids)}，不取第一项: {value}",
                vspan))
            headers["station"] = None
        else:
            headers["station"] = (ids[0], vspan)


# --------------------------------------------------------------------------- #
# 合并与冲突
# --------------------------------------------------------------------------- #
def _build_observations(accepted: list[_Accepted]):
    groups: dict[tuple[str, datetime, str], list[_Accepted]] = {}
    for acc in accepted:
        key = (acc.station_id, acc.instant, acc.metric)
        groups.setdefault(key, []).append(acc)
    observations: list[_Observation] = []
    conflicts: list[_ReviewItem] = []
    for key in sorted(groups, key=lambda k: (k[0], k[1], k[2])):
        station_id, instant, metric = key
        members = sorted(groups[key], key=lambda m: m.value_span)
        measured = [m for m in members if m.value is not None]
        if measured and len(measured) < len(members):
            # 一处缺报与另一处实数：冲突
            conflicts.append(_conflict_item(station_id, instant, metric,
                                            members))
        elif measured:
            if len({m.value for m in measured}) > 1:
                # 同站同刻同指标不同值：整组移入冲突待核，不任意留一条
                conflicts.append(_conflict_item(station_id, instant, metric,
                                                members))
            else:
                observations.append(_Observation(
                    station_id, instant, metric, STATUS_MEASURED,
                    measured[0].value, [], members))
        else:
            tokens: list[str] = []
            for m in members:
                if m.missing_token not in tokens:
                    tokens.append(m.missing_token)
            observations.append(_Observation(
                station_id, instant, metric, STATUS_MISSING, None, tokens,
                members))
    return observations, conflicts


def _conflict_item(station_id: str, instant: datetime, metric: str,
                   members: list[_Accepted]) -> _ReviewItem:
    parts = []
    for m in members:
        if m.value is not None:
            label = f"{_decimal_str(m.value)} {UNIT_FOR_METRIC[metric]}"
        else:
            label = m.missing_token
        parts.append(f"{label} (c{m.value_span[0]}-{m.value_span[1]})")
    return _ReviewItem(
        CATEGORY_CONFLICT, [R_CONFLICT],
        "同站同刻同指标的不同发布，整组移入待核: " + " vs ".join(parts),
        members[0].value_span, station_id=station_id,
        sampled_at_utc=_utc_iso(instant), metric=metric)


# --------------------------------------------------------------------------- #
# 输出渲染
# --------------------------------------------------------------------------- #
def _render_observations(obs_ids, registry: StationRegistry, text: str,
                         index: _LineIndex) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OBS_HEADER, lineterminator="\n",
                            extrasaction="raise")
    writer.writeheader()
    for obs_id, obs in obs_ids:
        if obs.status == STATUS_MEASURED:
            value_s = _decimal_str(obs.value)
            unit = UNIT_FOR_METRIC[obs.metric]
            reason = ""
        else:
            value_s = ""
            unit = ""
            reason = ";".join(obs.missing_tokens)
        for src_no, src in enumerate(obs.sources, 1):
            writer.writerow({
                "observation_id": obs_id,
                "station_id": obs.station_id,
                "station_name": registry.entries[obs.station_id].name,
                "sampled_at_utc": _utc_iso(obs.instant),
                "metric": obs.metric,
                "status": obs.status,
                "value": value_s,
                "unit": unit,
                "missing_reason": reason,
                "source_no": str(src_no),
                "station_ref": _fmt_span(index, *src.station_span),
                "station_text": text[src.station_span[0]:src.station_span[1]],
                "date_ref": _fmt_span(index, *src.date_span),
                "date_text": text[src.date_span[0]:src.date_span[1]],
                "time_ref": _fmt_span(index, *src.time_span),
                "time_text": text[src.time_span[0]:src.time_span[1]],
                "value_ref": _fmt_span(index, *src.value_span),
                "value_text": text[src.value_span[0]:src.value_span[1]],
            })
    return buf.getvalue()


def _render_review(rev_ids, text: str, index: _LineIndex) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=REVIEW_HEADER, lineterminator="\n",
                            extrasaction="raise")
    writer.writeheader()
    for rev_id, item in rev_ids:
        writer.writerow({
            "review_id": rev_id,
            "category": item.category,
            "reasons": ";".join(item.reasons),
            "station_id": item.station_id,
            "sampled_at_utc": item.sampled_at_utc,
            "metric": item.metric,
            "detail": item.detail,
            "span": _fmt_span(index, *item.span),
            "text": text[item.span[0]:item.span[1]],
        })
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 原子写出（临时文件 -> 回读校验 -> os.replace；失败回滚）
# --------------------------------------------------------------------------- #
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
            raise BulletinOutputError(f"临时文件回读内容与预期不一致: {tmp}")
        os.replace(tmp, target)
    except BulletinOutputError:
        _quiet_unlink(tmp)
        raise
    except OSError as exc:
        _quiet_unlink(tmp)
        raise BulletinOutputError(f"写出 {target} 失败: {exc}") from exc


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 端到端
# --------------------------------------------------------------------------- #
def process_bulletin(text_path: str | Path, stations_path: str | Path,
                     published_at: str, output_dir: str | Path) -> dict:
    """读取通报与对照 -> 抽取 -> 合并 -> 原子写出三份产物 -> 返回摘要。

    站名对照/发布时间问题抛 :class:`BulletinConfigError`；通报文本无法读取抛
    :class:`BulletinInputError`；写出失败抛 :class:`BulletinOutputError`。
    存在待核项不抛错——已确认结果照常写出，由调用方按摘要决定退出码。
    """
    text_path = Path(text_path)
    stations_path = Path(stations_path)
    output_dir = Path(output_dir)

    registry = StationRegistry.from_file(stations_path)
    publish_utc, publish_tz, publish_date = _parse_published_at(published_at)

    try:
        with open(text_path, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except UnicodeDecodeError as exc:
        raise BulletinInputError(
            f"通报文本不是合法 UTF-8 {text_path}: {exc}") from exc
    except OSError as exc:
        raise BulletinInputError(
            f"无法读取通报文本 {text_path}: {exc}") from exc

    index = _LineIndex(text)
    headers = {"date": None, "time": None, "station": None}
    review_items: list[_ReviewItem] = []
    non_measurements: list[_NonMeasurement] = []
    accepted: list[_Accepted] = []
    n_statements = 0

    paragraphs = _paragraphs(_split_lines(text))
    for para in paragraphs:
        # 空行清掉全部标题；相邻段的信息不漏过来
        headers = {"date": None, "time": None, "station": None}
        for line_start, line_text in para:
            header_match = _HEADER_RE.match(line_text)
            if header_match is not None:
                _process_header(header_match, line_start, line_text, headers,
                                registry, publish_date, review_items)
                continue
            for s_start, s_end, s_text in _split_pieces(
                    line_start, line_text, _STATEMENT_SEPS):
                n_statements += 1
                _process_statement(
                    (s_start, s_end), s_text, headers, registry,
                    publish_date, publish_tz, publish_utc,
                    review_items, non_measurements, accepted)

    observations, conflicts = _build_observations(accepted)
    review_items.extend(conflicts)
    review_items.sort(key=lambda it: (it.span[0], it.span[1], it.reasons[0]))

    obs_ids = [(f"obs-{i:04d}", obs)
               for i, obs in enumerate(observations, 1)]
    rev_ids = [(f"rev-{i:04d}", item)
               for i, item in enumerate(review_items, 1)]

    observations_csv = _render_observations(obs_ids, registry, text, index)
    review_csv = _render_review(rev_ids, text, index)

    n_measured = sum(1 for _, o in obs_ids if o.status == STATUS_MEASURED)
    n_missing = sum(1 for _, o in obs_ids if o.status == STATUS_MISSING)
    n_conflict = sum(1 for _, it in rev_ids
                     if it.category == CATEGORY_CONFLICT)
    n_other = sum(1 for _, it in rev_ids if it.category == CATEGORY_OTHER)
    reason_counts: dict[str, int] = {}
    for _, item in rev_ids:
        for reason in item.reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    summary = {
        "schema_version": SCHEMA_VERSION,
        "inputs": {
            "bulletin_text": {
                "path": str(text_path),
                "sha256": sha256_file(text_path),
            },
            "stations": {
                "path": str(stations_path),
                "sha256": sha256_file(stations_path),
            },
            "published_at": published_at,
            "published_at_utc": _utc_iso(publish_utc),
            "sample_timezone": "采样时刻按发布时间携带的固定偏移时区解释",
        },
        "counts": {
            "paragraphs": len(paragraphs),
            "statements": n_statements,
            "accepted": n_measured,
            "missing": n_missing,
            "conflict": n_conflict,
            "other_review": n_other,
            "review_total": n_conflict + n_other,
            "non_measurement": len(non_measurements),
        },
        "review_reason_counts": reason_counts,
        "non_measurement_items": [
            {"kind": item.kind,
             "span": _fmt_span(index, *item.span),
             "text": text[item.span[0]:item.span[1]]}
            for item in sorted(non_measurements, key=lambda it: it.span)
        ],
        "products": {
            "observations_csv": OBSERVATIONS_FILENAME,
            "review_csv": REVIEW_FILENAME,
            "summary_json": SUMMARY_FILENAME,
        },
        "disclaimer": "本结果由固定句法规则抽取，只接收通报明确给出的实测水位/"
                      "流量；不推算流量、不预测；待核项需人工确认。",
    }
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2,
                              sort_keys=True) + "\n"

    targets = [
        (output_dir / OBSERVATIONS_FILENAME, observations_csv),
        (output_dir / REVIEW_FILENAME, review_csv),
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

    return summary
