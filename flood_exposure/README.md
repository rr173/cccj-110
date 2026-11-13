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
```

一键端到端演示（含发布前 SIGTERM 中止、闸门拒绝、参考对照）：

```bash
bash demo/run_demo.sh
```

测试：

```bash
.venv/bin/python -I -m pytest tests/      # 29 个测试
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

## 命令一览

| 子命令 | 作用 |
| --- | --- |
| `make-inventory` | 扫描输入，生成带 sha256/字节数的清单 JSON |
| `run` | 全量处理并原子发布 |
| `run-incremental` | 边界增量（缓存驱动，与 `run` 同运行器） |
| `query` | 按流域或预报时次查询已发布明细 |
| `manifest` | 打印计算清单与输入版本追溯 |
| `releases` | 列出完整发布版本 |
