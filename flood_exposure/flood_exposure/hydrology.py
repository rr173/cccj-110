"""从 DEM 推导排水拓扑与测站汇水分区 (本地纯算法实现，不依赖外部水文软件)。

本模块把“已经画好的流域边界”换成“从高程栅格推导出来的汇水边界”。输入是
一张单波段 GeoTIFF 高程图 (投影 CRS，米制，北向上) 和一张测站表
(``outlet_id, row, col``，行列从 0 开始，指向原图像元中心，不做自动吸附)，
输出是

* 填洼高程 (filled DEM) 与每格被抬高的量；
* D8 单流向图 (每格至多一个流向，外圈可流出图的独立编码)；
* 沿流向图全部上游贡献面积 (含像元自身面积)；
* 每个有效格“下游首先经过”的测站 ID，也就是测站独占分区；
* 测站摘要 (原始/填洼/抬高、全上游贡献面积、独占分区、下一个下游测站)
  与全局量 (流出图总面积、未归属面积、填洼体积)。

两个面积口径 (务必分清)
-----------------------
对测站 t：

* **全上游贡献面积 (total_upstream_area_m2)**：沿流向图所有能到达 t 的有效格
  的面积之和（含 t 自身）。河道上串联的上下游测站之间，上游站的面积真实
  流过下游站，因此**下游站的贡献面积包含上游站的全部贡献面积**。
* **独占分区面积 (exclusive_area_m2)**：凡是“下游首先经过”上游站 t_up 的
  格子（哪怕它随后又流到 t_down），都归 t_up 独占，不再计入 t_down 的独占
  分区。它衡量的是“相对相邻站新增的汇水范围”，**不是贡献面积**。

两者对有效格互斥且完备：每个有效格要么落在某个测站的独占分区，要么一路
流入图外且未经过任何测站，计入“未归属面积”。

算法要点
--------
1. **有效格** = 高程有限 (非 NaN/Inf) 且未被 NoData 掩膜。无效格是不可穿越
   的屏障，参与排水路径时只能绕行，绝不当作泄水口。
2. **优先漫水填洼 (priority-flood)**：每个有效格最终必须能沿一条**不升高**
   的路径排到图像最外圈的某个有效格 (真正的泄水口)。只从外圈有效格出发，
   内部洼地被填平；已经能排水的坡面保持原值 (填洼只抬高不降低)。某个有效
   连通块完全不接触外圈时，抛出 :class:`NoOutletError` 并报告该块，**不得
   穿过 NoData 或凭空选出口**。
3. **平地展开 (flat routing)**：填平后的平台可能有成千上万个同高程格，不能
   按邻格编号随便选一个，也不能靠给高程叠加微小斜坡来掩盖。这里先从所有
   “已确定出口的格”（有更低邻格或本身在最外圈）做同高程反向 BFS 求每个
   平台格到出口的跳数，再让每格指向跳数少 1 的同高程邻格（并列时取
   ``D8_ORDER`` 中最靠前者），保证沿平坦区真实走出口且无环。
4. **D8 最陡下降**：存在更低有效邻格时选坡度 ``(z_c - z_n) / dist(c,n)``
   最大者；横纵分辨率不同时对角与正交距离不同，方向会因此改变。等坡度取
   ``D8_ORDER`` 中第一个，与遍历顺序、CSV 行序无关。
5. **外圈编码**：有效外圈格在没有更低图内邻格时可写 ``DIR_OUTSIDE = 8``。
   无效格写 ``DIR_NODATA = 255``。两者不同值，也不与 0..7 混淆。

复杂度：时间 O(N log N) (优先漫水的堆)，空间 O(N)。全图拓扑只构造一次，
测站摘要全部从同一份拓扑派生，不为每个测站重跑整图。
"""
from __future__ import annotations

import heapq
import json
import math
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import rasterio
from pyproj import CRS
from rasterio.transform import Affine

# --------------------------------------------------------------------------- #
# 固定编码与优先级 (随结果写出，读者可据此走完整条路径)
# --------------------------------------------------------------------------- #
#: 八邻域方向编码：0..7 表示“流到图内该方向上的邻格”。
#: 下标 = 行列增量在 ``D8_ORDER`` 中的位置。
D8_ORDER: tuple[tuple[int, int], ...] = (
    (-1, -1),  # 0 西北 NW
    (-1, 0),   # 1 正北 N
    (-1, 1),   # 2 东北 NE
    (0, 1),    # 3 正东 E
    (1, 1),    # 4 东南 SE
    (1, 0),    # 5 正南 S
    (1, -1),   # 6 西南 SW
    (0, -1),   # 7 正西 W
)
_DIR_NAMES = ("NW", "N", "NE", "E", "SE", "S", "SW", "W")

#: 有效格流出图像边界（只有最外圈有效格可写）。
DIR_OUTSIDE = 8
#: 无效格（高程非有限或被 NoData 掩膜）。不参与排水，也不是泄水口。
DIR_NODATA = 255
#: 内部哨兵：尚未确定方向；正常结果中不会出现。
DIR_UNSET = -1

#: 分区栅格编码：无效格。
PARTITION_NODATA = -1
#: 分区栅格编码：有效但一路出图未经过任何测站（未归属）。
PARTITION_UNASSIGNED = 0
#: 分区栅格中测站索引从 1 开始，与 summary.json 的 stations[].station_index 对应。

#: 栅格输出时的 NoData；不用 0，避免与真实高程/零面积混淆。
NODATA_FILL = -9999.0

TIE_BREAK_RULE = (
    "等坡度时按固定顺序 NW,N,NE,E,SE,S,SW,W (方向编码 0..7) 取第一个满足条件"
    "的邻格；该顺序只与行列相对位置有关，与遍历顺序和 CSV 行序无关。"
)
FLAT_ROUTING_RULE = (
    "同高程平台内不按邻格编号随意选方向：先由所有已确定出口的格 (有更低邻格"
    "或本身在最外圈) 出发做同高程反向 BFS，得到每格到出口的跳数；每格指向"
    "跳数少 1 的同高程邻格，并列时取 D8_ORDER 中靠前者。平台路径因此必然"
    "走向真实出口且无环，未用任何微斜坡扰动高程。"
)

DIRECTION_CODES = {
    str(i): {"delta": [int(dr), int(dc)], "name": nm,
             "description": f"流向 {nm} 邻格 (row{dr:+d}, col{dc:+d})"}
    for i, ((dr, dc), nm) in enumerate(zip(D8_ORDER, _DIR_NAMES))
}
DIRECTION_CODES[str(DIR_OUTSIDE)] = {
    "delta": None, "name": "OUTSIDE",
    "description": "流出图像边界，路径终止 (仅最外圈有效格可写，与 NoData 区分)",
}
DIRECTION_CODES[str(DIR_NODATA)] = {
    "delta": None, "name": "NODATA",
    "description": "无效格 (高程非有限或被 NoData 掩膜)，不参与排水，"
                   "不是泄水口；与流出图编码 8 不同",
}

ALGORITHM_DECISIONS = {
    "valid_cell": "高程有限 (非 NaN/Inf) 且未被 NoData 掩膜/掩码",
    "fill": "priority-flood 只从最外圈有效格出发；只抬高不降低，"
            "已能排水的格保持原值；无效格是不可穿越屏障，不是泄水口",
    "outlet": "真正的泄水口只在图像最外圈的效格；无外圈出口的有效连通块直接报错终止",
    "d8_slope": "存在更低有效邻格时按 (z_中 - z_邻)/dist 取最陡；"
                "dist 用横纵分辨率，对角为 hypot(dx, dy)",
    "tie_break": TIE_BREAK_RULE,
    "flat_routing": FLAT_ROUTING_RULE,
    "outside_code": "有效外圈格无更低图内邻格时写 8 (OUTSIDE)；无效格写 255 (NODATA)",
    "station_location": "CSV row/col 从 0 开始，直接指向像元中心，不做自动吸附",
    "station_order": "测站次序与 CSV 行序不影响结果；所有量按格坐标定义",
    "area_scope": "贡献面积含像元自身面积；像元面积 = |dx| * |dy| (投影米制)",
    "fill_volume": "Σ(每格抬高量) * 像元面积",
    "partition_semantics": "独占分区：(下游首先经过该测站的格)；串联时上游站的"
                           "分区不并入下游站的独占分区，但下游站的贡献面积仍含全部"
                           "真实上游面积",
}


# --------------------------------------------------------------------------- #
# 错误类型：所有输入/拓扑问题都给出具体原因
# --------------------------------------------------------------------------- #
class DemError(ValueError):
    """DEM / 测站输入的显式错误基类。"""


class NoOutletError(DemError):
    """存在完全不接触图像外圈的有效连通块，无法确定出口。"""

    def __init__(self, components: list[dict], cell_area_m2: float = 0.0):
        self.components = components
        lines = ["存在没有任何外圈出口的有效连通块，无法确定排水出口 "
                 "(不得穿过 NoData 或凭空选出口)，已终止。"]
        for c in components:
            lines.append(
                f"  连通块 #{c['component']}: {c['cell_count']} 个有效格, "
                f"行 {c['row_min']}..{c['row_max']}, 列 {c['col_min']}..{c['col_max']}, "
                f"面积 {c['cell_count'] * cell_area_m2:.1f} m²"
            )
        super().__init__("\n".join(lines))


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Station:
    outlet_id: str
    row: int
    col: int


@dataclass
class DemGrid:
    """已读取的单波段高程网格，以及可参与排水的有效掩膜。"""

    elevation: np.ndarray            # float64，原始高程 (无效格保留原值)
    valid: np.ndarray                # bool，可参与排水
    nodata: float | None
    crs: str
    transform: Affine
    height: int
    width: int
    dtype: str = "float64"
    source_sha256: str = ""
    source_path: str = ""
    invalid_reason: np.ndarray | None = None   # int16: 0 有效, 1 非有限, 2 NoData

    @property
    def x_res(self) -> float:
        return float(self.transform.a)

    @property
    def y_res(self) -> float:
        return float(-self.transform.e)

    @property
    def cell_area_m2(self) -> float:
        return self.x_res * self.y_res

    @property
    def total_valid_area_m2(self) -> float:
        return float(self.valid.sum()) * self.cell_area_m2


@dataclass
class DrainageResult:
    filled: np.ndarray               # float64 填洼高程 (无效格为 NaN)
    raised: np.ndarray               # float64 抬高量 = filled - original，>= 0
    direction: np.ndarray            # int16 方向编码
    pit_filled_cells: int
    components: int
    component_stats: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 输入读取与校验
# --------------------------------------------------------------------------- #
_METRE_UNITS = {"metre", "meter", "m", "meters", "metres"}


def _unit_is_metre(crs: CRS) -> bool:
    for ai in crs.axis_info:
        if ai.unit_name not in _METRE_UNITS:
            return False
        if abs(ai.unit_conversion_factor - 1.0) > 1e-12:
            return False
    return bool(crs.axis_info)


def validate_projected_metre_crs(crs, path: str | Path) -> CRS:
    """只接受米制投影 CRS；明确拒绝地理坐标与其它单位。"""
    if crs is None:
        raise DemError(f"DEM 缺少 CRS: {path}")
    c = CRS.from_user_input(crs)
    if c.is_geographic:
        raise DemError(
            f"DEM 使用地理坐标 CRS ({c.to_string()})；本命令只支持单位为米的"
            "投影 CRS (行列测站与像元面积都以平面米为单位)"
        )
    if not c.is_projected:
        raise DemError(f"DEM CRS 不是投影 CRS: {c.to_string()}")
    if not _unit_is_metre(c):
        units = ", ".join(sorted({ai.unit_name for ai in c.axis_info})) or "未知"
        raise DemError(
            f"DEM CRS 的坐标单位是 {units}，不是米；本命令只支持米制投影 CRS"
        )
    return c


def validate_north_up_transform(transform: Affine, path: str | Path) -> None:
    """必须北向上、无旋转；像元长宽可不同。"""
    a, b, d, e = transform.a, transform.b, transform.d, transform.e
    if b != 0.0 or d != 0.0:
        raise DemError(
            f"DEM 仿射变换含旋转/错切 (b={b}, d={d})，本命令只接受北向上的"
            "规则网格；请先把栅格重投影/重采样为北向上"
        )
    if a <= 0:
        raise DemError(f"DEM 像元 x 方向尺寸非正 (a={a})，只接受北向上栅格")
    if e >= 0:
        raise DemError(f"DEM 像元 y 方向尺寸应为负值 (北向上)，当前 e={e}")


def load_dem(path: str | Path) -> DemGrid:
    """读取单波段 DEM，结合有限性与 NoData 确定 ``valid``。

    ``valid = 数值有限 且 未被 NoData 掩膜/掩码``。无效格保留原始数值供
    追溯，但永不参与排水。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"DEM 不存在: {p}")
    with rasterio.open(p) as ds:
        if ds.driver not in ("GTiff", "COG"):
            raise DemError(f"DEM 必须是 GeoTIFF，当前驱动 {ds.driver}: {p}")
        if ds.count != 1:
            raise DemError(f"DEM 必须只有一个波段，当前 {ds.count} 个: {p}")
        crs = validate_projected_metre_crs(ds.crs, p)
        validate_north_up_transform(ds.transform, p)
        if ds.height < 1 or ds.width < 1:
            raise DemError(f"DEM 为空 (0 行或 0 列): {p}")
        raw = ds.read(1, masked=True)
        nodata = ds.nodata
        height, width = ds.height, ds.width
        transform = ds.transform
        dtype = ds.dtypes[0]

    data = np.asarray(raw.data, dtype="float64")
    nodata_mask = np.ma.getmaskarray(raw).copy()
    finite = np.isfinite(data)
    invalid_reason = np.where(~finite, 1, 0).astype("int16")   # 1 = 非有限
    if nodata is not None:
        nv = np.float64(nodata)
        if math.isfinite(nv):
            nodata_mask |= (data == nv)
        invalid_reason[nodata_mask] = 2                         # 2 = NoData
    valid = finite & ~nodata_mask
    if not valid.any():
        raise DemError(f"DEM 没有任何有效高程格 (全部为 NoData/非有限): {p}")

    from .hashing import sha256_file
    return DemGrid(
        elevation=data,
        valid=valid,
        nodata=float(nodata) if nodata is not None else None,
        crs=crs.to_string(),
        transform=transform,
        height=height,
        width=width,
        dtype=dtype,
        source_sha256=sha256_file(p),
        source_path=str(p),
        invalid_reason=invalid_reason,
    )


def _parse_int(text: str, field_name: str, lineno: int) -> int:
    if text == "":
        raise DemError(f"测站 CSV 第 {lineno} 行 {field_name} 为空")
    try:
        return int(text, 10)
    except ValueError:
        pass
    try:
        val = float(text)
    except ValueError:
        raise DemError(
            f"测站 CSV 第 {lineno} 行 {field_name}={text!r} 不是整数"
        ) from None
    if not math.isfinite(val) or val != int(val):
        raise DemError(
            f"测站 CSV 第 {lineno} 行 {field_name}={text!r} 不是整数；"
            "行列必须是从 0 开始的整数格坐标 (不做自动吸附)"
        )
    return int(val)


def load_stations(path: str | Path, grid: DemGrid) -> list[Station]:
    """读取并校验测站 CSV；任一规则违反即抛 :class:`DemError` 并说明原因。"""
    import csv

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"测站 CSV 不存在: {p}")
    with open(p, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise DemError(f"测站 CSV 没有表头: {p}")
        field_map = {f.strip().lower(): f for f in reader.fieldnames}
        missing = [k for k in ("outlet_id", "row", "col") if k not in field_map]
        if missing:
            raise DemError(
                f"测站 CSV 缺少必需列 {missing}；现有列: "
                f"{[f.strip() for f in reader.fieldnames]}"
            )
        stations: list[Station] = []
        cell_owner: dict[tuple[int, int], str] = {}
        id_cell: dict[str, tuple[int, int]] = {}
        for lineno, raw in enumerate(reader, start=2):
            if raw is None:
                continue
            if not any((v or "").strip() for v in raw.values() if isinstance(v, str)):
                continue
            oid = (raw.get(field_map["outlet_id"]) or "").strip()
            if not oid:
                raise DemError(f"测站 CSV 第 {lineno} 行 outlet_id 为空")
            row = _parse_int((raw.get(field_map["row"]) or "").strip(), "row", lineno)
            col = _parse_int((raw.get(field_map["col"]) or "").strip(), "col", lineno)
            if not (0 <= row < grid.height):
                raise DemError(
                    f"测站 {oid} (CSV 第 {lineno} 行) 的 row={row} 在图外；"
                    f"有效行范围 0..{grid.height - 1}"
                )
            if not (0 <= col < grid.width):
                raise DemError(
                    f"测站 {oid} (CSV 第 {lineno} 行) 的 col={col} 在图外；"
                    f"有效列范围 0..{grid.width - 1}"
                )
            if not bool(grid.valid[row, col]):
                why = ("高程为 NaN/Inf (非有限)"
                       if grid.invalid_reason is not None
                       and int(grid.invalid_reason[row, col]) == 1
                       else "被 NoData 掩膜")
                raise DemError(
                    f"测站 {oid} (CSV 第 {lineno} 行) 落在无效高程格 "
                    f"(row={row}, col={col})：{why}；测站必须位于可参与排水的有效格"
                )
            key = (row, col)
            if key in cell_owner:
                other = cell_owner[key]
                if other != oid:
                    raise DemError(
                        f"两个不同测站占同一格 (row={row}, col={col}): "
                        f"'{other}' 与 '{oid}'"
                    )
                raise DemError(f"测站 {oid} 重复出现于同一格 (row={row}, col={col})")
            if oid in id_cell:
                r0, c0 = id_cell[oid]
                raise DemError(
                    f"同一 outlet_id '{oid}' 指向不同格: "
                    f"(row={r0}, col={c0}) 与 (row={row}, col={col})"
                )
            cell_owner[key] = oid
            id_cell[oid] = key
            stations.append(Station(oid, row, col))
    if not stations:
        raise DemError(f"测站 CSV 为空: {p}")
    return stations


# --------------------------------------------------------------------------- #
# 有效连通块 (8 邻域)，用于“无外圈出口”判定
# --------------------------------------------------------------------------- #
def _label_components(valid: np.ndarray) -> np.ndarray:
    """8 邻域连通块标记，返回 int32 labels (-1 表示无效)。

    运行长度编码 (RLE) + 并查集：每一行把连续有效格压成一个“段”，相邻行的
    重叠段 (8 邻域下对角接触也算) 合并。段数远小于格数，纯 numpy 也足够快。
    """
    h, w = valid.shape
    n = h * w
    labels = np.full(n, -1, dtype="int32")
    if h == 0 or w == 0:
        return labels.reshape(h, w)

    # 逐行求连续段 [start, end)（end 不含）
    runs_per_row: list[tuple[np.ndarray, np.ndarray]] = []
    for r in range(h):
        row = valid[r]
        if not row.any():
            runs_per_row.append((np.empty(0, dtype="int64"),
                                 np.empty(0, dtype="int64")))
            continue
        d = np.diff(row.astype("int8"))
        starts = np.nonzero(np.concatenate(([bool(row[0])], d == 1)))[0]
        ends = np.nonzero(np.concatenate((d == -1, [bool(row[-1])])))[0] + 1
        runs_per_row.append((starts.astype("int64"), ends.astype("int64")))

    # 给每个段编号；每行的段 id 从该行第一个段的全局编号开始连续
    run_id_of_row: list[np.ndarray] = []
    offset = 0
    for r, (starts, ends) in enumerate(runs_per_row):
        ids = np.arange(len(starts), dtype="int64") + offset
        offset += len(starts)
        run_id_of_row.append(ids)
    nruns = offset
    if nruns == 0:
        return labels.reshape(h, w)

    parent = np.arange(nruns, dtype="int64")

    def find(a: np.ndarray) -> np.ndarray:
        a = np.asarray(a, dtype="int64").copy()
        while True:
            p = parent[a]
            if np.array_equal(p, a):
                return a
            parent[a] = parent[p]
            a = parent[p]

    # 相邻两行：对每个上段，找下一行中与其 (pad 1) 相交的段，收集无向边。
    # 8 邻域：上段 [a,b) 与下段 [c,d) 相交 <=> c < b+1 且 d > a-1。
    pair_a: list[np.ndarray] = []
    pair_b: list[np.ndarray] = []
    for r in range(h - 1):
        up_s, up_e = runs_per_row[r]
        dn_s, dn_e = runs_per_row[r + 1]
        if len(up_s) == 0 or len(dn_s) == 0:
            continue
        up_ids = run_id_of_row[r]
        dn_ids = run_id_of_row[r + 1]
        for k in range(len(up_s)):
            a, b = int(up_s[k]), int(up_e[k])
            lo = int(np.searchsorted(dn_e, a - 1, side="right"))
            hi = int(np.searchsorted(dn_s, b + 1, side="left"))
            if hi > lo:
                pair_a.append(np.full(hi - lo, up_ids[k], dtype="int64"))
                pair_b.append(dn_ids[lo:hi])
    # 反复把较大的根挂到较小的根下，直到不再变化；同一轮内共享根也不会丢合并。
    while True:
        changed = False
        for a, b in zip(pair_a, pair_b):
            ra, rb = find(a), find(b)
            hi = np.maximum(ra, rb)
            lo = np.minimum(ra, rb)
            mask = hi != lo
            if mask.any():
                np.minimum.at(parent, hi[mask], lo[mask])
                changed = True
        if not changed:
            break

    roots = find(np.arange(nruns, dtype="int64"))
    _, compact = np.unique(roots, return_inverse=True)
    compact = compact.astype("int32")

    for r in range(h):
        ids = run_id_of_row[r]
        if len(ids) == 0:
            continue
        st = runs_per_row[r][0]
        en = runs_per_row[r][1]
        for k in range(len(ids)):
            a, b = int(st[k]), int(en[k])
            labels[r * w + a: r * w + b] = compact[int(ids[k])]
    return labels.reshape(h, w)


def valid_components(valid: np.ndarray) -> tuple[np.ndarray, int, list[dict]]:
    """8 邻域标记有效连通块，返回 (labels(-1 无效), 块数, 每块统计)。"""
    h, w = valid.shape
    labels = _label_components(valid)
    ncomp = int(labels.max()) + 1 if labels.size else 0
    stats: list[dict] = []
    if ncomp == 0:
        return labels, 0, stats
    outer = np.zeros((h, w), dtype=bool)
    outer[0, :] = outer[-1, :] = True
    outer[:, 0] = outer[:, -1] = True
    rows, cols = np.indices((h, w))
    valid_flat = labels.ravel()
    keeps = valid_flat >= 0
    keys = valid_flat[keeps]
    rs = rows.ravel()[keeps]
    cs = cols.ravel()[keeps]
    on_outer = outer.ravel()[keeps]
    order = np.argsort(keys, kind="stable")
    keys_sorted = keys[order]
    rs_s, cs_s, out_s = rs[order], cs[order], on_outer[order]
    starts = np.searchsorted(keys_sorted, np.arange(ncomp))
    ends = np.searchsorted(keys_sorted, np.arange(ncomp), side="right")
    for k in range(ncomp):
        a, b = int(starts[k]), int(ends[k])
        stats.append({
            "component": k,
            "cell_count": b - a,
            "row_min": int(rs_s[a:b].min()), "row_max": int(rs_s[a:b].max()),
            "col_min": int(cs_s[a:b].min()), "col_max": int(cs_s[a:b].max()),
            "touches_outer": bool(out_s[a:b].any()),
        })
    return labels, ncomp, stats

# --------------------------------------------------------------------------- #
# 优先漫水填洼 (priority-flood)
# --------------------------------------------------------------------------- #
def priority_flood(grid: DemGrid) -> tuple[np.ndarray, np.ndarray]:
    """Barnes 式优先漫水：只从外圈有效格出发、按高程非降序推进。

    返回 (filled, raised)。性质：
    * 只有内部洼地被抬高，已经能排水的格保持原高程 (先弹出者高程不升)；
    * 每个填充格都能沿一个已处理邻格（不高于自己）走到外圈；
    * 堆内并列用 (高程, 行, 列) 全序，结果与堆内部实现无关。

    实现注意：用一维索引 + 预计算邻格偏移避免逐格 Python 对象开销；
    堆元素是 (高程, 索引) 全序，仍然与 heapq 内部实现无关。
    """
    h, w = grid.height, grid.width
    n = h * w
    z_flat = grid.elevation.ravel().astype("float64", copy=True)
    valid_flat = grid.valid.ravel()
    filled = np.where(valid_flat, z_flat, np.nan)
    raised = np.zeros(n, dtype="float64")
    visited = ~valid_flat

    # 预计算每个格在各方向的邻格一维索引 (-1 表示越界)
    idx = np.arange(n, dtype="int64").reshape(h, w)
    neigh = np.full((8, h, w), -1, dtype="int64")
    for k, (dr, dc) in enumerate(D8_ORDER):
        src_r = slice(max(0, -dr), h - max(0, dr))
        src_c = slice(max(0, -dc), w - max(0, dc))
        dst_r = slice(max(0, dr), h - max(0, -dr))
        dst_c = slice(max(0, dc), w - max(0, -dc))
        neigh[k, src_r, src_c] = idx[dst_r, dst_c]
    neigh_flat = neigh.reshape(8, n)

    outer = np.zeros((h, w), dtype=bool)
    outer[0, :] = outer[-1, :] = True
    outer[:, 0] = outer[:, -1] = True
    seeds = np.nonzero(valid_flat & outer.ravel())[0]
    visited[seeds] = True
    heap: list[tuple[float, int]] = [(float(filled[i]), int(i)) for i in seeds]
    heapq.heapify(heap)

    while heap:
        lev, i = heapq.heappop(heap)
        for k in range(8):
            j = int(neigh_flat[k, i])
            if j < 0 or visited[j]:
                continue
            visited[j] = True
            if filled[j] < lev:
                raised[j] = lev - filled[j]
                filled[j] = lev
            heapq.heappush(heap, (float(filled[j]), j))

    unvisited = int((valid_flat & ~visited).sum())
    if unvisited:
        raise DemError(
            f"有 {unvisited} 个有效格无法从最外圈到达 (有效连通块不接触外圈)，"
            "填洼未完成；不得穿过 NoData 或凭空选出口"
        )
    return filled.reshape(h, w), raised.reshape(h, w)


# --------------------------------------------------------------------------- #
# D8 方向 + 平地展开
# --------------------------------------------------------------------------- #
def _neighbour_slopes(filled: np.ndarray, valid: np.ndarray,
                      xr: float, yr: float) -> np.ndarray:
    """返回 shape (8, h, w) 的坡度数组；无效/不下降/越界处为 -inf。

    对角距离为 ``hypot(xr, yr)``，正交为 ``xr`` 或 ``yr`` —— 长宽不同时
    同一高差在对角/正交上的坡度不同，最陡方向会随之改变。
    """
    h, w = filled.shape
    diag = math.hypot(xr, yr)
    dists = [diag, yr, diag, xr, diag, yr, diag, xr]   # 与 D8_ORDER 对齐
    slopes = np.full((8, h, w), -np.inf, dtype="float64")
    for idx, (dr, dc) in enumerate(D8_ORDER):
        # 源格与目标格的切片：取“存在的邻域对”
        src_r = slice(max(0, -dr), h - max(0, dr))
        src_c = slice(max(0, -dc), w - max(0, dc))
        dst_r = slice(max(0, dr), h - max(0, -dr))
        dst_c = slice(max(0, dc), w - max(0, -dc))
        nz = filled[dst_r, dst_c]
        nv = valid[dst_r, dst_c]
        dz = filled[src_r, src_c] - nz
        with np.errstate(invalid="ignore"):
            s = np.where(nv & np.isfinite(nz) & (dz > 0) & valid[src_r, src_c],
                         dz / dists[idx], -np.inf)
        slopes[idx, src_r, src_c] = s
    return slopes


def build_direction(grid: DemGrid, filled: np.ndarray) -> dict:
    """构造 D8 单流向图，返回方向与诊断量。"""
    h, w = grid.height, grid.width
    valid = grid.valid

    slopes = _neighbour_slopes(filled, valid, grid.x_res, grid.y_res)
    # np.argmax 在并列时返回第一个，即 D8_ORDER 中靠前者 —— 固定规则。
    best_idx = np.argmax(slopes, axis=0)
    has_lower = np.isfinite(slopes.max(axis=0))

    direction = np.full((h, w), DIR_NODATA, dtype="int16")
    direction[valid] = DIR_UNSET
    assign = valid & has_lower
    direction[assign] = best_idx[assign].astype("int16")

    # 外圈有效格：没有更低图内邻格时写出图
    outer = np.zeros((h, w), dtype=bool)
    outer[0, :] = True
    outer[h - 1, :] = True
    outer[:, 0] = True
    outer[:, w - 1] = True
    outside = valid & outer & ~assign
    direction[outside] = DIR_OUTSIDE

    defined = valid & (has_lower | outer)

    # 平地展开：从所有已确定出口的格做同高程反向 BFS，求到出口的跳数。
    # 用一维索引 + 预计算邻格偏移，避免逐格的元组/边界检查开销。
    n = h * w
    filled_flat = filled.ravel()
    valid_flat = valid.ravel()
    idx2 = np.arange(n, dtype="int64").reshape(h, w)
    neigh = np.full((8, h, w), -1, dtype="int64")
    for k, (dr, dc) in enumerate(D8_ORDER):
        src_r = slice(max(0, -dr), h - max(0, dr))
        src_c = slice(max(0, -dc), w - max(0, dc))
        dst_r = slice(max(0, dr), h - max(0, -dr))
        dst_c = slice(max(0, dc), w - max(0, -dc))
        neigh[k, src_r, src_c] = idx2[dst_r, dst_c]
    neigh_flat = neigh.reshape(8, n)

    level_flat = np.full(n, -1, dtype="int32")
    seed_idx = np.flatnonzero(defined.ravel())
    level_flat[seed_idx] = 0
    q: deque[int] = deque(int(i) for i in seed_idx)
    while q:
        i = q.popleft()
        lv = int(level_flat[i])
        zc = filled_flat[i]
        for k in range(8):
            j = int(neigh_flat[k, i])
            if j < 0 or not valid_flat[j] or level_flat[j] != -1:
                continue
            if filled_flat[j] != zc:
                continue
            level_flat[j] = lv + 1
            q.append(j)
    level = level_flat.reshape(h, w)

    unresolved = valid & ~defined
    bad = unresolved & (level < 0)
    if bad.any():
        r, c = (int(v) for v in np.argwhere(bad)[0])
        raise DemError(
            "存在无法确定流向的有效格 (既无更低邻格、也不在可展开的同高程平台上): "
            f"row={r}, col={c}；填洼与方向构造不一致，已终止"
        )
    unresolved_idx = np.flatnonzero(unresolved.ravel())
    if not bad.any() and unresolved_idx.size == 0:
        _assert_acyclic(direction, valid, h, w)
        return {
            "direction": direction, "level": level, "has_lower": has_lower,
            "flat_cells": 0, "max_flat_hops": int(level[valid].max()) if valid.any() else 0,
        }
    # 只对平台格选方向；按解析式找“跳数少 1 的同高程邻格”
    lv = level
    chosen = np.full((h, w), DIR_UNSET, dtype="int16")
    for idx, (dr, dc) in enumerate(D8_ORDER):
        if dr >= 0:
            rs, rd = slice(0, h - dr), slice(dr, h)
        else:
            rs, rd = slice(-dr, h), slice(0, h + dr)
        if dc >= 0:
            cs, cd = slice(0, w - dc), slice(dc, w)
        else:
            cs, cd = slice(-dc, w), slice(0, w + dc)
        same = (filled[rs, cs] == filled[rd, cd]) & (lv[rd, cd] == lv[rs, cs] - 1)
        take = same & (chosen[rs, cs] == DIR_UNSET)
        sub = chosen[rs, cs]
        sub[take] = idx
        chosen[rs, cs] = sub
    still = unresolved & (chosen == DIR_UNSET)
    if still.any():
        r, c = (int(v) for v in np.argwhere(still)[0])
        raise DemError(
            f"平台格 (row={r}, col={c}) 找不到跳数更小的同高程邻格，方向图损坏")
    direction[unresolved] = chosen[unresolved]

    _assert_acyclic(direction, valid, h, w)
    return {
        "direction": direction,
        "level": level,
        "has_lower": has_lower,
        "flat_cells": int(unresolved.sum()),
        "max_flat_hops": int(level[valid].max()) if valid.any() else 0,
    }


def _assert_acyclic(direction: np.ndarray, valid: np.ndarray, h: int, w: int) -> None:
    """沿方向图逐格追踪：任何有效格最终必须到达 OUTSIDE，且路径不成环。

    用指针倍增 (binary lifting) 在 O(N log N) 内验证；若某有效格跳了超过
    图幅格数步仍未出图，即判定有环，并实际追踪一次打印具体环路径。
    """
    n = h * w
    dirs = direction.astype("int64").ravel()
    valid_flat = valid.ravel()
    nxt = np.full(n, -1, dtype="int64")     # -1 表示已到图外
    rows = np.arange(n) // w
    cols = np.arange(n) % w
    for k, (dr, dc) in enumerate(D8_ORDER):
        take = valid_flat & (dirs == k)
        if not take.any():
            continue
        idxs = np.nonzero(take)[0]
        rr = rows[idxs] + dr
        cc = cols[idxs] + dc
        bad = (rr < 0) | (rr >= h) | (cc < 0) | (cc >= w)
        if bad.any():
            i = idxs[np.argmax(bad)]
            raise DemError(
                f"有效格 (row={i // w}, col={i % w}) 的方向 {k} 指向图外邻格；"
                "只有最外圈格可写 OUTSIDE，方向图损坏")
        target = rr * w + cc
        bad_t = ~valid_flat[target]
        if bad_t.any():
            i = idxs[np.argmax(bad_t)]
            j = target[np.argmax(bad_t)]
            raise DemError(
                f"有效格 (row={i // w}, col={i % w}) 的方向 {k} 指向无效格 "
                f"({j // w},{j % w})，方向图损坏")
        nxt[idxs] = target

    outside = valid_flat & (dirs == DIR_OUTSIDE)
    bad_dirs = valid_flat & (dirs < 0)
    if bad_dirs.any():
        i = int(np.nonzero(bad_dirs)[0][0])
        raise DemError(f"有效格 (row={i // w}, col={i % w}) 的方向编码为 "
                       f"{dirs[i]}，不是有效流向")

    reach = np.where(outside, -1, nxt)      # 一步后的落点
    alive = valid_flat & (reach != -1)
    hops = 1
    # 每轮把步数翻倍；图幅内若还存在“未出图”的有效格，说明路径长度超过
    # 有效格总数 -> 必有环。
    while alive.any() and hops <= n:
        idxs = np.nonzero(alive)[0]
        moved = reach[idxs]
        valid_move = moved >= 0
        new = np.full(idxs.shape, -1, dtype="int64")
        new[valid_move] = reach[moved[valid_move]]
        reach[idxs] = new
        alive[idxs] = new != -1
        hops *= 2
    if alive.any():
        i = int(np.nonzero(alive)[0][0])
        r, c = i // w, i % w
        chain = []
        seen = {}
        rr, cc = r, c
        while (rr, cc) not in seen:
            seen[(rr, cc)] = len(chain)
            chain.append((rr, cc))
            d = int(direction[rr, cc])
            if d == DIR_OUTSIDE:
                break
            dr, dc = D8_ORDER[d]
            rr, cc = rr + dr, cc + dc
        start = seen[(rr, cc)]
        raise DemError(
            "方向图出现环，无法沿流向图走到图外: "
            + " -> ".join(f"({a},{b})" for a, b in chain[start:])
            + f" -> ({rr},{cc})")


# --------------------------------------------------------------------------- #
# 流向图工具：追踪与上游枚举 (供核查/测试，独立于累计过程)
# --------------------------------------------------------------------------- #
def trace_down(grid: DemGrid, direction: np.ndarray, row: int, col: int,
               max_steps: int | None = None) -> list[tuple[int, int]]:
    """从某格沿方向图逐格追踪，包含起点，遇 OUTSIDE 停止。"""
    h, w = grid.height, grid.width
    steps: list[tuple[int, int]] = []
    r, c = row, col
    limit = max_steps if max_steps is not None else h * w + 1
    while True:
        if not (0 <= r < h and 0 <= c < w):
            raise DemError(f"追踪走出图幅 ({r},{c})")
        steps.append((r, c))
        d = int(direction[r, c])
        if d == DIR_OUTSIDE:
            return steps
        if d == DIR_NODATA or d < 0:
            raise DemError(f"追踪到无效方向编码 {d} 于 ({r},{c})")
        dr, dc = D8_ORDER[d]
        r, c = r + dr, c + dc
        if len(steps) > limit:
            raise DemError(f"追踪超过 {limit} 步仍未出图，方向图可能有环")


def build_upstream_index(grid: DemGrid, direction: np.ndarray) -> list[list[int]]:
    """返回每个格 (按 row*w+col) 的直接上游格索引列表。"""
    h, w = grid.height, grid.width
    up: list[list[int]] = [[] for _ in range(h * w)]
    for r, c in np.argwhere(grid.valid):
        r, c = int(r), int(c)
        d = int(direction[r, c])
        if d == DIR_OUTSIDE:
            continue
        dr, dc = D8_ORDER[d]
        up[(r + dr) * w + (c + dc)].append(r * w + c)
    return up


def enumerate_upstream(grid: DemGrid, direction: np.ndarray,
                       row: int, col: int) -> set[tuple[int, int]]:
    """枚举所有能沿方向图到达 (row,col) 的有效格 (含自身)。用于独立核对。"""
    w = grid.width
    up = build_upstream_index(grid, direction)
    start = row * w + col
    seen: set[int] = set()
    stack = [start]
    while stack:
        i = stack.pop()
        if i in seen:
            continue
        seen.add(i)
        stack.extend(up[i])
    return {(i // w, i % w) for i in seen}


# --------------------------------------------------------------------------- #
# 全图拓扑一次：贡献面积 / 下游首先经过的测站
# --------------------------------------------------------------------------- #
def flow_topology(grid: DemGrid, stations: list[Station],
                  direction: np.ndarray) -> dict:
    """一次遍历算清整张图的上下游关系（不为每个测站重跑整图）。

    * 拓扑序：Kahn 算法，边 上游 -> 下游；无环由 :func:`build_direction` 保证，
      这里再以覆盖数校验。
    * ``total_area``：自身面积 + 直接上游 ``total_area`` 之和，沿拓扑序一次
      算完；测站格的 ``total_area`` 即全上游贡献面积。
    * ``first_station``：按拓扑序自上游向下游传播；格子自身若是测站，则优先
      归它自己；否则继承第一个非空上游的归属。它就是“下游首先经过的测站”，
      也即独占分区。
    """
    h, w = grid.height, grid.width
    valid = grid.valid
    cell_area = grid.cell_area_m2
    n = h * w
    valid_flat = valid.ravel()

    # 测站归属编码：0 = 无测站；k>=1 = 第 k 个测站 (按格坐标排序，与 CSV 行序无关)
    station_ids = [s.outlet_id for s in sorted(
        stations, key=lambda x: (x.row, x.col, x.outlet_id))]
    by_id = {s.outlet_id: s for s in stations}
    own_code = np.zeros(n, dtype="int32")
    for k, oid in enumerate(station_ids, start=1):
        st = by_id[oid]
        own_code[st.row * w + st.col] = k

    # 下游索引：-1 表示图外或无效格
    down = np.full(n, -1, dtype="int64")
    idx = np.arange(n, dtype="int64").reshape(h, w)
    for k, (dr, dc) in enumerate(D8_ORDER):
        take = valid & (direction == k)
        if not take.any():
            continue
        src_r = slice(max(0, -dr), h - max(0, dr))
        src_c = slice(max(0, -dc), w - max(0, dc))
        dst_r = slice(max(0, dr), h - max(0, -dr))
        dst_c = slice(max(0, dc), w - max(0, -dc))
        sub = take[src_r, src_c]
        down.reshape(h, w)[src_r, src_c] = np.where(
            sub, idx[dst_r, dst_c], down.reshape(h, w)[src_r, src_c])
    down_flat = down

    # 拓扑序：Kahn。入度 = 指向该格的有效格数量 (用 numpy bincount 向量化)
    src_cells = np.nonzero(valid_flat & (down_flat >= 0))[0]
    dst_cells = down_flat[src_cells]
    indeg = np.bincount(dst_cells, minlength=n).astype("int64")
    q: deque[int] = deque(int(i) for i in np.nonzero(valid_flat & (indeg == 0))[0])
    topo: list[int] = []
    indeg_work = indeg.copy()
    while q:
        i = q.popleft()
        topo.append(i)
        j = int(down_flat[i])
        if j != -1:
            indeg_work[j] -= 1
            if indeg_work[j] == 0:
                q.append(j)
    n_valid = int(valid.sum())
    if len(topo) != n_valid:
        raise DemError(
            f"拓扑排序未覆盖全部有效格 ({len(topo)}/{n_valid})，方向图存在环"
        )

    # 贡献面积：自身面积 + 全部直接上游面积。按拓扑序一次算完。
    total = np.zeros(n, dtype="float64")
    total[valid_flat] = cell_area
    order = np.array(topo, dtype="int64")
    # 贡献面积 = 自身面积 + 全部直接上游面积。沿拓扑序逐个累加：
    # 每格的 total 在轮到它时已经收齐所有上游 (拓扑序保证)，且只有一次
    # “转发”到直接下游，因此可原地累进且不受队列偶合顺序影响。
    for i in order:
        j = int(down_flat[i])
        if j != -1:
            total[j] += total[i]

    # “下游首先经过的测站”沿下游方向传播：
    #   first(i) = own[i] (若 i 是测站)，否则 first(直接下游)，否则 0 (未归属)
    # 按拓扑序逆序计算即可一次传完，且与测站顺序无关。
    first_code = own_code.copy()
    rev = order[::-1]
    for i in rev:
        i = int(i)
        if first_code[i] != 0:
            continue
        j = int(down_flat[i])
        if j != -1:
            first_code[i] = first_code[j]

    first_station = np.full(n, None, dtype=object)
    for k, oid in enumerate(station_ids, start=1):
        first_station[first_code == k] = oid
    return {
        "total_area": total.reshape(h, w),
        "first_station": first_station.reshape(h, w),
        "down": down,   # 一维索引数组 (row*w+col)，-1 表示图外
        "topo": topo,
        "station_ids": station_ids,
    }


# --------------------------------------------------------------------------- #
# 汇总：测站摘要 + 全局量
# --------------------------------------------------------------------------- #
def summarise(grid: DemGrid, filled: np.ndarray, raised: np.ndarray,
              stations: list[Station], direction: np.ndarray,
              topology: dict, component_labels: np.ndarray) -> dict:
    """生成测站摘要与全局量；所有面积口径都显式命名。"""
    h, w = grid.height, grid.width
    cell_area = grid.cell_area_m2
    total = topology["total_area"]
    first = topology["first_station"]
    down = topology["down"]

    exclusive_area: dict[str, float] = {s.outlet_id: 0.0 for s in stations}
    valid_flat = grid.valid.ravel()
    # 用命中直方图把归属编码转成面积，避免逐格 Python 循环。
    station_ids = topology.get("station_ids") or [
        s.outlet_id for s in sorted(stations, key=lambda x: (x.row, x.col, x.outlet_id))]
    code_of = {oid: k for k, oid in enumerate(station_ids, start=1)}
    first_flat = first.ravel()
    codes = np.zeros(first_flat.shape[0], dtype="int64")
    for oid, k in code_of.items():
        codes[first_flat == oid] = k
    present = valid_flat
    counts = np.bincount(codes[present], minlength=len(station_ids) + 1)
    unassigned = float(counts[0] * cell_area)
    for oid, k in code_of.items():
        exclusive_area[oid] = float(counts[k] * cell_area)

    # 出图总面积：有效格沿方向图最终都出图，按“终点为 OUTSIDE 的格的上游
    # 面积”求和，各集合两两不交，和应等于有效总面积。
    total_flat = total.ravel()
    dir_flat = direction.ravel()
    outlet_idx = np.flatnonzero(valid_flat & (dir_flat == DIR_OUTSIDE))
    outlet_cells = [
        {"row": int(i) // w, "col": int(i) % w,
         "upstream_area_m2": float(total_flat[i])}
        for i in outlet_idx
    ]
    outside_area = float(total_flat[outlet_idx].sum())

    # 下一个下游测站：从测站格沿流向图继续走，遇到的第一个其它测站
    station_at = {(s.row, s.col): s.outlet_id for s in stations}
    next_down: dict[str, str | None] = {}
    for s in stations:
        nxt = None
        j = int(down[s.row * w + s.col])
        hops = 0
        while j != -1:
            rr, cc = divmod(j, w)
            who = station_at.get((rr, cc))
            if who is not None and who != s.outlet_id:
                nxt = who
                break
            j = int(down[j])
            hops += 1
            if hops > h * w:
                raise DemError(f"测站 {s.outlet_id} 下游追踪未终止，方向图可能有环")
        next_down[s.outlet_id] = nxt

    volume = float(np.sum(np.where(grid.valid, raised, 0.0)) * cell_area)

    station_rows = []
    # 索引按 outlet_id 排序分配：CSV 行序翻转或打乱都不改变分区栅格与摘要。
    for idx, s in enumerate(
            sorted(stations, key=lambda x: (x.row, x.col, x.outlet_id)), start=1):
        station_rows.append({
            "station_index": idx,
            "outlet_id": s.outlet_id,
            "row": s.row,
            "col": s.col,
            "component": int(component_labels[s.row, s.col]),
            "elevation_m": float(grid.elevation[s.row, s.col]),
            "filled_elevation_m": float(filled[s.row, s.col]),
            "raised_m": float(raised[s.row, s.col]),
            "total_upstream_area_m2": float(total[s.row, s.col]),
            "exclusive_area_m2": float(exclusive_area[s.outlet_id]),
            "next_downstream_outlet": next_down[s.outlet_id],
        })

    return {
        "stations": station_rows,
        "unassigned_area_m2": float(unassigned),
        "outside_area_m2": float(outside_area),
        "total_valid_area_m2": float(grid.total_valid_area_m2),
        "fill_volume_m3": volume,
        "cell_area_m2": float(cell_area),
        "x_res_m": float(grid.x_res),
        "y_res_m": float(grid.y_res),
        "outlet_cells": outlet_cells,
        "exclusive_area_by_id": {k: float(v) for k, v in exclusive_area.items()},
        "next_downstream_by_id": next_down,
        "components": [],
        "pit_filled_cells": 0,
        "flat_routing": {},
    }


# --------------------------------------------------------------------------- #
# 完整推导
# --------------------------------------------------------------------------- #
def delineate(grid: DemGrid, stations: list[Station]) -> tuple[DrainageResult, dict]:
    """执行从 DEM 到排水拓扑 / 测站分区的完整推导 (单次全图)。"""
    labels, ncomp, comps = valid_components(grid.valid)
    missing = [c for c in comps if not c["touches_outer"]]
    if missing:
        raise NoOutletError(missing, grid.cell_area_m2)

    filled, raised = priority_flood(grid)
    topo_build = build_direction(grid, filled)
    direction = topo_build["direction"]
    topology = flow_topology(grid, stations, direction)
    summary = summarise(grid, filled, raised, stations, direction,
                        topology, labels)

    # 完整性自查
    if not np.all(np.isfinite(filled[grid.valid])):
        raise DemError("填洼后仍有有效格高程非有限，已终止")
    if np.any(filled[grid.valid] < grid.elevation[grid.valid] - 1e-12):
        raise DemError("填洼结果出现降低，违反只抬高不降低的约束，已终止")
    covered = sum(summary["exclusive_area_by_id"].values()) \
        + summary["unassigned_area_m2"]
    if abs(covered - summary["total_valid_area_m2"]) > 1e-6 * max(
            1.0, summary["total_valid_area_m2"]):
        raise DemError(
            "独占分区与未归属面积未覆盖全部有效格 "
            f"({covered} vs {summary['total_valid_area_m2']})，已终止")

    result = DrainageResult(
        filled=filled,
        raised=raised,
        direction=direction,
        pit_filled_cells=int(np.count_nonzero(raised > 0)),
        components=ncomp,
        component_stats=comps,
    )
    summary["components"] = comps
    summary["pit_filled_cells"] = result.pit_filled_cells
    summary["flat_routing"] = {
        "cells_with_lower_neighbour": int(topo_build["has_lower"].sum()),
        "cells_routed_in_flat": int(topo_build["flat_cells"]),
        "max_flat_hops": int(topo_build["max_flat_hops"]),
        "cells_raised": result.pit_filled_cells,
    }
    # 内部给 build_partition_raster 复用，最终写盘前会移除
    summary["_first_station"] = topology["first_station"]
    summary["_total_area"] = topology["total_area"]
    return result, summary


# --------------------------------------------------------------------------- #
# 输出栅格 (方向 / 贡献面积 / 独占分区 / 填洼)
# --------------------------------------------------------------------------- #
def write_raster(path: str | Path, data: np.ndarray, grid: DemGrid,
                 dtype: str, nodata: float, description: str = "") -> None:
    h, w = data.shape
    with rasterio.open(
        path, "w", driver="GTiff", width=w, height=h, count=1,
        dtype=dtype, crs=grid.crs, transform=grid.transform, nodata=nodata,
    ) as ds:
        ds.write(data.astype(dtype), 1)
        if description:
            ds.set_band_description(1, description)


def build_partition_raster(grid: DemGrid, summary: dict) -> tuple[np.ndarray, dict]:
    """独占分区栅格：0=未归属, k>=1=测站索引, -1=无效格。"""
    mapping = {row["outlet_id"]: row["station_index"]
               for row in summary["stations"]}
    part = np.full((grid.height, grid.width), PARTITION_NODATA, dtype="int32")
    part[grid.valid] = PARTITION_UNASSIGNED
    first = summary["_first_station"]
    valid_flat = grid.valid.ravel()
    part_flat = part.ravel()
    for oid, idx in mapping.items():
        hit = np.asarray(first.ravel() == oid) & valid_flat
        part_flat[hit] = idx
    return part, {
        "encoding": {
            str(PARTITION_NODATA): "无效格 (不参与排水)",
            str(PARTITION_UNASSIGNED): "有效格但一路出图未经过任何测站 (未归属)",
            "1..N": "测站索引，见 stations[].station_index (顺序不影响数值定义)",
        },
        "id_to_index": mapping,
    }


def write_all_outputs(out_dir: str | Path, grid: DemGrid,
                      result: DrainageResult, summary: dict,
                      dem_path: str | Path, stations_path: str | Path) -> dict:
    """把栅格与摘要写到 out_dir (调用方保证目录已准备好/原子发布)。"""
    from .hashing import sha256_file

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    filled_out = np.where(grid.valid, result.filled, NODATA_FILL)
    write_raster(out_dir / "filled_dem.tif", filled_out, grid, "float64",
                 NODATA_FILL, "填洼后高程 (m)；无效格为 NoData")
    write_raster(out_dir / "pit_depth_m.tif", result.raised, grid, "float64",
                 NODATA_FILL, "每格被抬高的量 (m)，只抬高不降低")
    write_raster(out_dir / "flow_direction.tif", result.direction, grid,
                 "int16", DIR_NODATA,
                 "D8 方向编码：0..7 邻域方向, 8 流出图, 255 无效格")
    contrib = np.where(grid.valid, summary["_total_area"], NODATA_FILL)
    write_raster(out_dir / "contributing_area_m2.tif", contrib, grid,
                 "float64", NODATA_FILL, "全上游贡献面积 (m²)，含像元自身")
    part, part_meta = build_partition_raster(grid, summary)
    write_raster(out_dir / "exclusive_partition.tif", part, grid,
                 "int32", PARTITION_NODATA,
                 "测站独占分区：0 未归属, k 测站索引, -1 无效格")

    summary_out = {k: v for k, v in summary.items() if not k.startswith("_")}
    summary_out["inputs"] = {
        "dem": {
            "path": str(dem_path),
            "sha256": sha256_file(dem_path),
            "crs": grid.crs,
            "width": grid.width,
            "height": grid.height,
            "x_res_m": grid.x_res,
            "y_res_m": grid.y_res,
            "affine": [grid.transform.a, grid.transform.b, grid.transform.c,
                       grid.transform.d, grid.transform.e, grid.transform.f],
            "nodata": grid.nodata,
            "dtype": grid.dtype,
        },
        "stations": {
            "path": str(stations_path),
            "sha256": sha256_file(stations_path),
            "count": len(summary["stations"]),
        },
    }
    summary_out["direction_codes"] = DIRECTION_CODES
    summary_out["algorithm_decisions"] = ALGORITHM_DECISIONS
    summary_out["partition_raster_encoding"] = part_meta
    summary_out["disclaimer"] = (
        "本结果是地形推导的汇水边界 (DEM -> 填洼 -> D8 -> 上游面积/测站分区)，"
        "不代表洪水水动力模拟；测站格按 CSV 的 row/col 直接使用，未做自动吸附。"
    )
    with open(out_dir / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary_out, fh, ensure_ascii=False, indent=2, sort_keys=True)

    import csv
    cols = ["station_index", "outlet_id", "row", "col", "component",
            "elevation_m", "filled_elevation_m", "raised_m",
            "total_upstream_area_m2", "exclusive_area_m2",
            "next_downstream_outlet"]
    with open(out_dir / "stations.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for row in sorted(summary["stations"], key=lambda x: x["outlet_id"]):
            writer.writerow({k: row[k] for k in cols})
    return summary_out


def run_delineation(dem_path: str | Path, stations_path: str | Path,
                    out_dir: str | Path) -> dict:
    """端到端便捷封装：读入 -> 推导 -> 写盘，返回 summary dict。"""
    grid = load_dem(dem_path)
    stations = load_stations(stations_path, grid)
    result, summary = delineate(grid, stations)
    return write_all_outputs(out_dir, grid, result, summary,
                             dem_path, stations_path)
