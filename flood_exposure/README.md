# 洪水暴露度分析工具 (flood-exposure)

为流域应急分析生成**可追溯**的洪水暴露度数据集：以等面积投影下的几何
相交面积为权重，把不同 CRS / 像元大小 / 仿射变换的水深栅格重采样到人口
网格，输出每个流域 × 预报时次的受淹人口估计、缺测面积比例与质量标记，
全部结果可回溯到参与计算的输入版本。无地图前端，纯命令行 + 本地合成样例。

## 能力对照

| 需求 | 实现位置 |
| --- | --- |
| 三个固定输入清单摘要 + sha256 校验 | `flood_exposure/manifest.py`, CLI `make-inventory` |
| 不同 CRS / 像元 / 仿射变换的栅格 | 全部经工作 CRS (默认 EPSG:6933 等面积) 统一，`raster.py` |
| 无效多边形修复 / 修复失败显式跳过 | `geometry.py::repair_geometry`, `basins.py` |
| 跨经度环绕线多边形 | `geometry.py::split_antimeridian` |
| 相邻流域重叠，人口格按相交面积分摊、不重复全额计数 | `geometry.py::detect_overlaps`, `population.py` |
| 有效零值 vs NoData 严格区分 | `raster.py`（见下“业务口径”） |
| 窗口读取，窗口边界不改变权重 | `raster.py::iter_windows` + 测试 `test_window_tile_does_not_change_weights` |
| 分块顺序无关，声明精度内一致 | 累加器可交换，测试 `test_chunk_order_does_not_change_result` |
| 边界增量：只重算几何变化流域 | 内容寻址权重缓存，`population.py` + `runner.py` |
| 暂存再发布，中断只见上一完整版本 | `publishing.py::StagedRun` |
| NoData 超闸门拒绝发布，不静默零填补 | `publishing.py::check_gate` / `PublishGateError` |
| 流域明细查询 + 计算清单 | `query.py`, CLI `query` / `manifest` |
| 独立参考计算对照 | `reference.py`（规则点阵，独立代码路径） |
| 远程大文件可恢复下载（真实 HTTP + 断点续传） | `fetch.py`, CLI `fetch`, 演示 `demo/fetch_demo.py` |
| 无边界资料时从 DEM 推导汇水边界 | `hydrology.py`, CLI `delineate`, 演示 `demo/delineate_demo.py` |
| 填洼只抬高 / 平地显式展开 / D8 最陡 + 固定等坡规则 | `hydrology.py::priority_flood` / `build_direction` |
| 测站贡献面积与独占分区两个口径 | `hydrology.py::flow_topology` / `summarise` |
| 雨量累计读数 → 逐段雨量/降雨事件（纯本地、标准库） | `rain.py`, CLI `rainfall`, 演示 `demo/make_rain_samples.py` |
| 回绕/重置/超长空档/检修窗口显式质量标记，无效段截断事件 | `rain.py::_classify_interval` / `build_events` |
| 雨量结果原子替换，输入冲突不留半成品 | `rain.py::process_rainfall` |
| 撤离可达性：赶在道路被淹前到达开放避险点的可执行行程 | `evacuation.py`, CLI `evacuation-access`, 演示 `demo/make_evac_samples.py` |
| 状态=(路口,分钟)的精确搜索：最早抵达→最少等待→道路编号字典序 | `evacuation.py::search` |
| 未知路况绝不当干路；车程整段区间检查；重叠冲突整体拒绝 | `evacuation.py::load_conditions` / `road_status` |

## 安装

```bash
python3 -m venv --without-pip .venv
python3 get-pip.py --prefix=.venv          # 无 ensurepip 的环境
.venv/bin/python -m pip install -e .
```

依赖：numpy、rasterio（wheel 自带 GDAL）、shapely、pyproj、geopandas、pyogrio。

## 30 秒上手

```bash
# 1. 合成样例（不同 CRS、重叠边界、无效多边形、NoData 等全场景）
.venv/bin/python demo/make_sample_data.py sample_data

# 2. 输入清单（三个摘要，含 sha256）
.venv/bin/flood-exposure make-inventory \
  --depth T00=sample_data/rasters/depth_T00_utm49n_50m.tif \
  --depth T06=sample_data/rasters/depth_T06_utm49n_50m.tif \
  --boundary sample_data/boundaries/basins_v1.gpkg \
  --boundary-version v1 --id-field basin_id \
  --population sample_data/rasters/population_utm48n_100m.tif \
  --output inv_v1.json

# 3. 全量处理（暂存 → 闸门 → 原子发布）
.venv/bin/flood-exposure run --inventory inv_v1.json \
  --output-root out --run-id rel_v1 --max-missing 0.25 --tile 64

# 4. 边界增量（同缓存目录；只重建几何变化的流域）
.venv/bin/flood-exposure run-incremental --inventory inv_v2.json \
  --output-root out --cache-dir .cache --max-invalid 0.25

# 5. 查询与追溯
.venv/bin/flood-exposure query --output-root out --basin-id B
.venv/bin/flood-exposure manifest --output-root out --basin-id B
.venv/bin/flood-exposure releases --output-root out

# 6. 远程水深：可恢复地拉到本地，再交给 make-inventory / run
.venv/bin/flood-exposure fetch \
  https://supplier.example/depth/T00.tif downloads/depth_T00.tif \
  --chunk-bytes 4194304 \
  --expected-sha256 <期望的整体 SHA256>
.venv/bin/flood-exposure make-inventory --depth T00=downloads/depth_T00.tif ...
```

`fetch` 的字节语义：

* 首次 `HEAD` 探测**总长度与强 ETag**；弱 ETag / 无 ETag 一律拒绝续传。
* 进度记录 `<target>.fetchstate.json` 按块保存区间与内容 SHA256；
  恢复时读取 `<target>.fetchpart` 里的真实字节逐块核对，不符的块重取。
* 每条分段请求带 `If-Match` / `If-Range`；206 的 `Content-Range` 起止、
  总长度、`Content-Length` 与真实读到的字节数全部核对；中途断连不记块。
* 服务器忽略 Range 返回 200 时，不写进任何偏移：作废本轮分段进度，
  从 0 流式重传并再次核对强 ETag / 长度 / 最终 URL。
* 416 只有在本地所有区间已校验且长度一致时才进入最终核对，否则是协议错误。
* 全部区间完整 + 临时文件实际长度一致 + 逐块摘要 + 整体 SHA256（若给定）
  通过后才 `os.replace` 到目标；失败期间旧目标保持可用。
* `--fresh` 显式放弃临时进度，按当前远端版本重新获取。
* 同一目标用非阻塞 `flock` 独占：后启动的进程立即以退出码 3 报占用；
  进程异常退出后内核释放锁，新进程可直接接管。
* 成功后打印本地路径、字节数、SHA256，并写 `<target>.provenance.json`
  （请求 URL、最终 URL、强 ETag、总长度、实际摘要）。

一键端到端演示（含发布前 SIGTERM 中止、闸门拒绝、参考对照、下载故障场景）：

```bash
bash demo/run_demo.sh
```

单独的下载演示（真实本地 HTTP 服务 + 逐条断言）：

```bash
.venv/bin/python -I demo/fetch_demo.py            # 断连/篡改/换版/200/伪造/416/争用
.venv/bin/python -I demo/fetch_pipeline_demo.py   # 下载 -> make-inventory -> run
```

测试：

```bash
.venv/bin/python -I -m pytest tests/      # 61 个测试
```

## 输出结构

```
out/
  staging/<run_id>/          # 暂存区，对外不可见；中断即整体删除
  releases/
    current -> rel_v1        # 符号链接，rename(2) 原子切换
    rel_v1/
      exposure.csv           # 逐 (流域, 时次) 明细
      run_manifest.json      # 计算清单：输入版本/sha256/参数/闸门/缓存命中
      gate_report.json       # 闸门结论
      weight_cache/<hash>.json
      SUCCESS                # 完整发布标记

downloads/
  depth_T00.tif            # fetch 成功后才原子替换到此路径
  depth_T00.tif.provenance.json   # 来源记录（请求/最终 URL、强 ETag、长度、摘要）
  # 失败/进行中：.fetchpart（临时字节）、.fetchstate.json（进度）、.fetchlock
```

## 业务口径（详见 docs/METHODOLOGY.md）

- **有效零值**：模型覆盖且确认水深为 0（未淹），参与面积权重。
- **NoData**：没有可用水深（模型域外/掩膜/缺测），含义是“不知道”，
  计入缺测面积，**绝不按零填补**；超 `max_missing_fraction` 即拒绝发布。
- **面积权重**：`w(B,P) = area(B∩P) / area(P)`，面积均在等面积 EPSG:6933
  下计算。相邻流域重叠时各取自己的相交份额，同一人口格不会被两个流域
  重复全额计数。
- **受淹人口**：各类面积取三向交集 `area(水深像元 ∩ 人口格 ∩ 流域)`，
  `exposed_pop = Σ pop(P)·A_wet(B,P)/area(P)`；缺测比例的分母是流域被
  人口网格覆盖的面积 `area(B∩P)`。受淹区与流域是否在同一侧由几何相交
  决定，不是两个格级比例的乘积；缺测不补零，结果仍是保守下界。

## 无边界资料：`delineate` 从 DEM 推导汇水边界

若新山区没有现成流域边界，但有**单波段米制投影 GeoTIFF 高程**和
**测站 CSV**（`outlet_id,row,col`，行列从 0 开始、指向像元中心、不自动
吸附），`delineate` 在本地完成整条处理链：

```bash
.venv/bin/python -I demo/make_dem_samples.py /tmp/dem_samples   # 生成手画矩阵

.venv/bin/flood-exposure delineate \
  --dem /tmp/dem_samples/branching_and_serial/dem.tif \
  --stations /tmp/dem_samples/branching_and_serial/stations.csv \
  --output-dir /tmp/delineate_out
```

输出 `filled_dem.tif`、`pit_depth_m.tif`、`flow_direction.tif`、
`contributing_area_m2.tif`、`exclusive_partition.tif`、`summary.json`、
`stations.csv`。

口径与规则（完整推导见 `docs/METHODOLOGY.md` 第 9–13 节）：

- **有效格** = 高程有限且未被 NoData 掩膜；无效格是不可穿越屏障，不是泄水口；
- **填洼** 优先漫水只从最外圈有效格出发，只抬高不降低；某个有效连通块
  完全不接触外圈时直接报错（退出码 3），不穿 NoData、不凭空选出口；
- **D8** 有更低邻格时按 `(z_c-z_n)/dist` 取最陡（`dist` 用横纵分辨率，
  长宽不同会改变最陡方向）；等坡度按固定顺序 `NW,N,NE,E,SE,S,SW,W` 取第一个；
  填平平台用“同高程反向 BFS 跳数”显式展开，不叠加微斜坡、不按邻格编号乱选；
- **编码** `0..7` 邻域、`8` 流出图（OUTSIDE）、`255` 无效（NoData），
  `summary.json` 附逐码说明，可沿它走完整条路径；
- **贡献面积** 含像元自身，沿拓扑序一次累加；**独占分区**是“下游首先经过
  该测站”的格，串联时上游站的面积不计入下游站独占分区，但下游站贡献面积
  仍包含全部真实上游面积。`Σ独占 + 未归属 = 有效总面积`；
- 测站索引按格坐标排序，**CSV 行序翻转/打乱不改变任何结果**；
- 产出是**地形推导的汇水边界**，不代表洪水水动力模拟。

演示脚本会打印方向箭头地图、填洼量手算、长宽不同的方向变化，并独立逐格
核对（有效格全到外界、无环、贡献面积=枚举上游、分区不交叠且完备），
再对错误输入逐一确认失败原因：

```bash
.venv/bin/python -I demo/delineate_demo.py
```

## 本地雨量过程整理：`rainfall`

把雨量站定时上报的**累计翻斗计数器读数**整理为可追查质量问题的逐段
降雨量与降雨事件。纯本地、纯标准库实现，不接入网络，也不影响洪水栅格 /
DEM 计算链。

```bash
# 生成刻意乱序的合成读数/配置并运行独立手算断言（无需第三方依赖）
python3 -I demo/make_rain_samples.py sample_rain

# 正式整理：读数 CSV + 设备配置 JSON -> 三个结果文件
flood-exposure rainfall \
  --readings sample_rain/readings.csv \
  --config sample_rain/devices.json \
  --output-dir sample_rain/rainfall_output
```

读数 CSV 固定三列 `device_id,observed_at,cumulative_count`，时间必须带
时区偏移（如 `2026-06-01T08:00:00+08:00`），内部统一换算 UTC 再排序。
设备配置示例：

```json
{
  "devices": {
    "RG-WRAP": {
      "tip_mm": 0.2,
      "max_interval_seconds": 7200,
      "dry_separation_seconds": 1800,
      "counter_modulus": 100,
      "maintenance": [
        {"start": "2026-06-01T01:00:00+00:00",
         "end": "2026-06-01T02:00:00+00:00"}
      ]
    }
  }
}
```

口径要点（完整说明见 `docs/METHODOLOGY.md` 第 14–15 节）：

- **累计计数只确定区间雨量**：相邻有效读数之差 × 每斗毫米数，归属于整个
  `[前一观测时刻, 后一观测时刻)` 区间，不能据此声称雨在区间内的精确落点；
- 同刻同值记录合并并保留全部原始行号；同刻不同值、缺时区、未知设备、
  负读数、非法配置一律明确拒绝，不猜测不静默忽略；
- 读数下降只有在配置模数且严格跨过 `modulus-1 → 0` 边界时才按回绕计算，
  否则视为设备重置，段无效并从新读数重建基准；
- 超过最大间隔、与检修半开区间相交的段禁止跨段相减；有效零雨段
  （`ZERO_RAIN`）与无效段严格区分；
- 无效段与超长空档强制截断事件；干旱时长**严格大于**配置值才拆场；
  事件标注是否被数据边界截断；
- 三个输出先写临时文件、回读校验再原子替换，失败时回滚，旧结果保持可用；
  摘要记录两份输入的 SHA256、完整配置、读数/合并/拒绝数量与各质量标记
  数量。

## 应急撤离可达性：`evacuation-access`

回答应急值班的问题：**从某路口出发，车能否赶在道路被淹以前到达仍开放
的避险点？** 只看出发那一刻的路况不够——车开到一半道路可能已经封闭；
提前到达的路口也不一定允许安全等待。命令读取已经整理好的路网与积水
片段，按“地点 × 分钟”的状态空间计算可实际执行的撤离行程。纯本地、
纯标准库实现，不影响洪水栅格 / DEM / 雨量计算链。

```bash
# 生成覆盖全部规定场景的小路网样例并运行独立断言（无需第三方依赖）
python3 -I demo/make_evac_samples.py sample_evac

# 正式计算：路网 JSON + 积水 CSV + 查询 JSON -> 逐请求结论的报告
flood-exposure evacuation-access \
  --network sample_evac/network.json \
  --conditions sample_evac/conditions.csv \
  --queries sample_evac/queries.json \
  --output sample_evac/report.json
```

输入（全部是相对同一参考时刻的整数分钟，完整口径见
`docs/METHODOLOGY.md` 第 16–20 节）：

- **路网 JSON**：`analysis_window`（有限分析范围）、`intersections`
  （`node_id`、`can_wait`、`flooded_intervals`）、`roads`（`road_id`、
  `from_node`/`to_node`、正整数 `travel_minutes`、可通行最大积水深度
  `max_depth_m`）、`shelters`（挂在路口上的若干开放窗口）；
- **积水 CSV**：`road_id,begin,end,depth_m`，左闭右开；`depth_m` 为
  非负数或空值（未知）。空档与空值都是未知，**绝不当成干路**；重叠
  片段只有深度含义相同才可合并，冲突即整体拒绝；
- **查询 JSON**：`{"requests": [{request_id, origin, depart_minute,
  deadline_minute}, ...]}`，逐请求独立返回。

口径要点：

- 一段车程 `[enter, exit)` 必须在**整个**区间上有已知且合格的路况；
  深度等于上限仍可通过，超过不可通过；车程恰在封闭窗口开始的那一分钟
  结束时道路检查不含抵达端点，但目的路口仍检查抵达当时是否受淹；
- 等待一分钟要求路口允许等待且这一整分钟安全；不能在道路中间停车；
  只沿道路给定方向行驶；避险点窗口左闭右开，抵达关闭时刻不算赶上；
  起点在出发时已受淹直接判 `invalid_request`；
- 优化顺序：最早抵达任一开放避险点 → 累计等待分钟最少 → 道路编号
  序列字典序；搜索状态是 (路口, 分钟)，禁止等待的路口可以绕行后更晚
  抵达再接上开放道路；道路耗时为正且截止分钟有限，搜索必然终止；
- 结果分 `reachable`（选中避险点、抵达分钟、逐条道路进入/离开分钟、
  等待位置与时长）、`unreachable`（搜索范围 + 超深/未知/禁止等待等
  阻塞原因分类计数）、`invalid_request`（具体原因）；同批一个请求
  无效不丢失其他请求；路网/路况自身非法则整体拒绝（退出码 2/3/4）；
- 报告保留三份输入的 SHA256 与分析参数，便于回头核对行程依据。
  **结论只在已知路况与所给行驶时间假设下成立。**

## 命令一览

| 子命令 | 作用 |
| --- | --- |
| `fetch` | 远程大文件可恢复下载（探测身份、分块校验、断点续传、摘要核对） |
| `make-inventory` | 扫描输入，生成带 sha256/字节数的清单 JSON |
| `run` | 全量处理并原子发布 |
| `run-incremental` | 边界增量（缓存驱动，与 `run` 同运行器） |
| `query` | 按流域或预报时次查询已发布明细 |
| `manifest` | 打印计算清单与输入版本追溯 |
| `releases` | 列出完整发布版本 |
| `delineate` | 从 DEM + 测站表推导 D8 流向、贡献面积与测站汇水分区 |
| `rainfall` | 本地整理累计翻斗读数：逐段雨量 CSV、降雨事件 CSV、带 SHA256 的摘要 JSON |
| `evacuation-access` | 撤离可达性：按（路口,分钟）搜索赶在道路被淹前到达开放避险点的可执行行程 |
