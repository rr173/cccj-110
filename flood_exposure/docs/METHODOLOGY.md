# 方法与业务口径说明

## 1. NoData 的业务含义

水深栅格中两类“看起来都是零/空”的值必须严格区分：

| 类别 | 数值含义 | 业务解释 | 处理方式 |
| --- | --- | --- | --- |
| **有效零值 (valid zero)** | 像元被模型覆盖，水深确认为 0 m | “确认未淹” | 计入干地面积 `dry_area`，参与面积权重 |
| **NoData** | 像元无可用水深（模型域外、被掩膜、传感器缺测等） | **“不知道”**，既不是干地也不是淹地 | 计入 `nodata_area`，披露缺测比例；**禁止按零填补** |
| **栅格外 (OOB)** | 人口格的一部分落在水深栅格覆盖范围之外 | 同样是“不知道” | 计入 `oob_area`，与 NoData 合并为缺测 |

判定优先级：掩码数组的 mask → 浮点 NaN → 与栅格原生 dtype 的 nodata
标量比较。比较保持在栅格原生 dtype（例如 float32 的 `-9999` 不提升为
float64），避免大整数填充值的精度错配。

**发布闸门**：每个 (流域, 时次) 的

```
missing_fraction = (nodata_area + oob_area) / 流域权重覆盖面积
```

超过配置的 `max_missing_fraction` 时，该运行拒绝发布（`PublishGateError`），
暂存区整体销毁，`releases/current` 保持指向上一完整版本。系统不会用 0
替代 NoData 来“凑出”一个结果。

人口网格自身的 NoData 格不赋零人口，而是整体剔除并打 `POP_RASTER_NODATA`。

## 2. 面积权重定义

所有几何运算在**等面积投影**（默认 EPSG:6933，WGS 84 / NSIDC EASE-Grid 2.0
等面积圆柱）下进行。选它而非以中央经线为界的 LAEA/AEA，是为了统一处理
跨越 ±180° 经度环绕线的流域（见第 5 节）。

对人口网格像元 P（四角从其原生 CRS 投影到工作 CRS 后的四边形）与流域 B：

```
w(B, P) = area( intersection(B, P) ) / area(P)        # ∈ [0, 1]
```

水深源像元 s 到人口格 P 的面积贡献同理，按源像元类别累加：

```
A_wet(P)    = Σ_s area(s ∩ P)  ，depth(s) > 阈值 且 s 有效
A_dry(P)    = Σ_s area(s ∩ P)  ，depth(s) ≤ 阈值 且 s 有效（含有效零）
A_nodata(P) = Σ_s area(s ∩ P)  ，s 为 NoData
A_oob(P)    = area(P) − 以上三者
```

### 2.1 受淹人口

人口按面积均摊到单位面积，流域 B 在格 P 上“拥有”的人口为 `w(B,P)·pop(P)`：

```
exposed_pop(B, t) = Σ_P w(B,P) · pop(P) · wet_fraction(P, t)
wet_fraction(P,t) = A_wet(P,t) / area(P)
```

NoData/OOB 不进入 `wet_fraction`，因此该估计是**保守下界**；缺测规模通过
`missing_fraction` 与质量标记单独披露，而不是被悄悄当作干地。

### 2.2 相邻流域重叠

当流域 B1、B2 的边界轻微重叠时，重叠区内的人口格 P 与两个流域分别求交，
各自只拿到份额 `w(B1,P)`、`w(B2,P)`：

- 任何一方都不会仅凭“格与我相交”就拿到整格人口（权重 1）；
- 两个份额可能之和 > 1（重叠是数据质量问题，不应靠裁剪隐藏）；
- 重叠对与重叠面积写入计算清单，相关行打 `OVERLAP_WEIGHTED`。

不变量：**同一条人口记录不可能被两个流域同时全额计数**（测试
`test_overlapping_basins_share_cells_by_area_not_double_counted`）。

## 3. 窗口读取与结果确定性

- 源水深栅格按 `window_tile_size` 分块从磁盘读取（`rasterio.windows.Window`），
  窗口只决定每次 I/O 的像元范围；
- 每个像元的工作 CRS 四边形与相交面积由几何唯一确定，与它在哪个窗口被
  读到无关，因此改变 tile 尺寸权重不变（测试要求差值 < 1e-6）；
- 累加器只做加法，满足交换律/结合律，分块顺序（正序、逆序、乱序）结果
  在声明精度 `DECLARED_PRECISION = 1e-6` 内一致。

## 4. 边界增量处理

权重表只依赖（流域几何指纹, 人口网格 sha256, 人口网格变换/CRS, 工作 CRS,
代码版本），**与水深栅格无关**。缓存键是上述要素的稳定 JSON 哈希：

- 几何指纹未变的流域 → 命中缓存，跳过全部几何相交；
- 几何修订/新增流域 → 指纹变化，重建权重表；
- 修复失败流域 → 不产出、不进缓存，计入 `failed_basins`；
- 换一批预报时次 → 全部权重表天然复用。

演示：v1→v2 中 B 局部修订、D 退化为修复失败、E 新增，运行结果为
缓存命中 `[A, C]`、重建 `[B, E]`、失败 `[D]`。

## 5. 无效几何与经度环绕线

- `shapely.make_valid`（GEOS MakeValid）修复自相交等问题，成功打
  `INVALID_GEOM_REPAIRED`；若修复后没有任何有面积的多边形（如纯线串
  退化几何），返回 `GEOM_REPAIR_FAILED`，该流域被跳过且受
  `max_invalid_basin_fraction` 闸门约束；
- 跨 ±180° 的几何按经度跨度 >180° 识别，平移到 [0,360] 空间在 180° 处
  切开，再把东半部分平移回 [-180,180]，得到 MultiPolygon 进入后续流程。

## 6. 暂存再发布与中断安全

```
staging/<run_id>/  —写中间结果→  releases/<run_id>/  —rename(2)→  current
```

- 所有写操作先落 `*.tmp` 再 `os.replace`；
- 发布是目录整体 `rename` + 符号链接原子切换，读者任何时刻只能看到
  完整版本；
- SIGTERM/SIGINT 置中止标志，运行器在检查点抛 `KeyboardInterrupt`，
  上下文管理器销毁暂存区，`current` 不动；
- 下次启动清理 `staging/` 全部残留。

## 7. 独立参考计算

`reference.py` 用与主流水线**完全独立的代码路径**（规则点阵 / lattice
Monte Carlo）复核：在每个人口格铺 K×K 个点阵点，逐点判定落在水深像元的
哪个类别，以点数比例估计面积比例。两条路径只共享输入文件与坐标变换，
几何求交与面积累加不共用。K=48 时限 0.06（O(1/K) 边界误差），演示中
实测最大偏差约 7e-4。

## 8. 质量标记枚举

| 标记 | 触发条件 |
| --- | --- |
| `OK` | 无任何异常 |
| `NODATA_PRESENT` | 水深 NoData 面积 > 0（未超闸门） |
| `NODATA_GATE_FAIL` | 缺测比例超上限，发布被拒 |
| `OUT_OF_DEPTH_EXTENT` | 显著面积落在水深栅格外 |
| `OVERLAP_WEIGHTED` | 与相邻流域存在重叠，已按面积分摊 |
| `INVALID_GEOM_REPAIRED` | 边界无效但修复成功 |
| `GEOM_REPAIR_FAILED` | 修复失败，流域未参与计算 |
| `POP_RASTER_NODATA` | 人口网格自身含 NoData 格 |
