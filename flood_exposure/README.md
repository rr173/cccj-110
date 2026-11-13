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
