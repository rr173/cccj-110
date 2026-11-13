#!/usr/bin/env bash
# 端到端演示：洪水暴露度数据集的全部关键场景。
# 用法: bash demo/run_demo.sh [PYTHON_BIN]
# 推荐 PYTHON_BIN 指向项目虚拟环境解释器 (.venv/bin/python -I)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${1:-$ROOT/.venv/bin/python -I}"
BIN="$ROOT/.venv/bin/flood-exposure"
WORK="$(mktemp -d -t flood_demo_XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
echo "== 演示工作目录: $WORK (退出自动清理)"

step() { printf '\n========== %s ==========\n' "$1"; }

# 1. 生成合成样例 -------------------------------------------------------------
step "1. 生成合成样例 (不同 CRS / 像元 / 重叠边界 / 无效多边形 / NoData)"
$PY "$ROOT/demo/make_sample_data.py" "$WORK/data" >/dev/null
ls "$WORK/data/rasters" "$WORK/data/boundaries"

# 2. 输入清单（三个固定摘要 + sha256）----------------------------------------
step "2. 生成输入清单 (make-inventory)"
$BIN make-inventory \
  --depth "T00=$WORK/data/rasters/depth_T00_utm49n_50m.tif" \
  --depth "T06=$WORK/data/rasters/depth_T06_utm49n_50m.tif" \
  --boundary "$WORK/data/boundaries/basins_v1.gpkg" \
  --boundary-version v1 --id-field basin_id \
  --population "$WORK/data/rasters/population_utm48n_100m.tif" \
  --output "$WORK/inv_v1.json"
cat "$WORK/inv_v1.json"

# 3. 全量处理 + 窗口切分一致性 (两个 tile 尺寸) -------------------------------
step "3a. 全量处理 (tile=32)"
$BIN run --inventory "$WORK/inv_v1.json" --output-root "$WORK/out32" \
  --run-id r1 --max-missing 0.25 --tile 32
step "3b. 全量处理 (tile=128) —— 窗口边界不改变权重"
$BIN run --inventory "$WORK/inv_v1.json" --output-root "$WORK/out128" \
  --run-id r1 --max-missing 0.25 --tile 128
$PY - "$WORK" <<'PYEOF'
import csv, sys
from pathlib import Path
work = Path(sys.argv[1])
def rows(sub):
    with open(work/sub/"releases/r1/exposure.csv", encoding="utf-8") as fh:
        return {(r["basin_id"], r["forecast_time"]): r for r in csv.DictReader(fh)}
a, b = rows(Path("out32")), rows(Path("out128"))
maxdiff = max(abs(float(a[k]["exposed_population"]) - float(b[k]["exposed_population"]))
              for k in a)
print(f"两种窗口切分的受淹人口最大差值: {maxdiff:.3e} (声明精度 1e-6) ->",
      "一致" if maxdiff < 1e-6 else "不一致")
PYEOF

# 4. 流域明细查询 + 计算清单追溯 ----------------------------------------------
step "4. 流域 B 明细查询 (query)"
$BIN query --output-root "$WORK/out32" --basin-id B
step "4b. 计算清单与输入版本追溯 (manifest)"
$BIN manifest --output-root "$WORK/out32" --basin-id B | head -40

# 5. NoData 闸门拒绝发布 ------------------------------------------------------
step "5. 严格 NoData 闸门 (上限 1%) -> 拒绝发布，不做零填补"
set +e
$BIN run --inventory "$WORK/inv_v1.json" --output-root "$WORK/strict" \
  --run-id blocked --max-missing 0.01 --tile 64
rc=$?
set -e
echo "退出码=$rc (2=闸门拒绝)；releases 内容:"
ls "$WORK/strict/releases" 2>/dev/null || echo "  (无任何发布目录)"

# 6. 边界增量：v2 只重建几何变化流域 ------------------------------------------
step "6. 边界增量处理 (v2: B 局部修订 / D 修复失败 / E 新增)"
$BIN make-inventory \
  --depth "T00=$WORK/data/rasters/depth_T00_utm49n_50m.tif" \
  --depth "T06=$WORK/data/rasters/depth_T06_utm49n_50m.tif" \
  --boundary "$WORK/data/boundaries/basins_v2.gpkg" \
  --boundary-version v2 --id-field basin_id \
  --population "$WORK/data/rasters/population_utm48n_100m.tif" \
  --output "$WORK/inv_v2.json" >/dev/null
mkdir -p "$WORK/cache"
$BIN run --inventory "$WORK/inv_v1.json" --output-root "$WORK/inc" \
  --run-id v1 --max-missing 0.25 --tile 64 --cache-dir "$WORK/cache" >/dev/null
$BIN run --inventory "$WORK/inv_v2.json" --output-root "$WORK/inc" \
  --run-id v2 --max-missing 0.25 --max-invalid 0.25 --tile 64 \
  --cache-dir "$WORK/cache" \
  | $PY -c "import json,sys; d=json.load(sys.stdin); print('缓存命中:', d['cache_hits']); print('重建流域:', d['cache_misses']); print('修复失败:', d['failed_basins'])"

# 7. 发布前进程中止 -----------------------------------------------------------
step "7. 发布前进程中止 (SIGTERM)：读者只能看到上一完整版本"
cat > "$WORK/abort_run.py" <<'PYEOF'
import os, signal, sys, time
sys.path.insert(0, os.environ["DEMO_ROOT"])
from pathlib import Path
from flood_exposure.manifest import InputInventory
from flood_exposure.config import Config
from flood_exposure import runner as R
import flood_exposure.population as P

inv = InputInventory.from_file(os.environ["DEMO_INV"])
out = Path(os.environ["DEMO_OUT"])
cfg = Config(max_missing_fraction=0.25, max_invalid_basin_fraction=0.25,
             window_tile_size=64)
orig = P.build_weight_table
state = {"n": 0}
def slow(basin, pop, work_crs):
    state["n"] += 1
    time.sleep(0.4)
    if state["n"] == 3:
        os.kill(os.getpid(), signal.SIGTERM)  # 模拟发布前被外部杀掉
    return orig(basin, pop, work_crs)
P.build_weight_table = slow
R.build_weight_table = slow
R.run_pipeline(inv, out, cfg, run_id="aborted", cache_dir=Path(os.environ["DEMO_CACHE"]))
PYEOF
# 先建立一个完整版本作为“上一完整版本”
$BIN run --inventory "$WORK/inv_v1.json" --output-root "$WORK/kill" \
  --run-id good --max-missing 0.25 --tile 64 --cache-dir "$WORK/cache2" >/dev/null
before="$(readlink "$WORK/kill/releases/current")"
DEMO_ROOT="$ROOT" DEMO_INV="$WORK/inv_v2.json" DEMO_OUT="$WORK/kill" \
DEMO_CACHE="$WORK/cache2" $PY "$WORK/abort_run.py" 2>/dev/null || true
after="$(readlink "$WORK/kill/releases/current")"
echo "中止前 current -> $before ; 中止后 current -> $after"
[ "$before" = "$after" ] && [ "$after" = "good" ] && echo "OK: current 保持上一完整版本"
ls "$WORK/kill/releases"
if [ -n "$(ls -A "$WORK/kill/staging" 2>/dev/null)" ]; then
  echo "异常: 暂存区有残留"; ls "$WORK/kill/staging"
else
  echo "OK: 暂存区已清理 (staging 为空或不存在)"
fi

# 8. 独立参考计算对照 ---------------------------------------------------------
step "8. 独立点阵参考计算 vs 主流水线精确相交"
$PY - "$ROOT" "$WORK" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/demo")
from pathlib import Path
from rasterio.transform import from_origin
from flood_exposure.raster import resample_depth_windowed
from flood_exposure.reference import lattice_reference
work = Path(sys.argv[2])
depth = work / "data/rasters/depth_T00_utm49n_50m.tif"
pt = from_origin(500000.0, 3390000.0, 100.0, 100.0)
cells = {(r, c) for r in range(2, 6) for c in range(4, 8)}
exact = resample_depth_windowed(depth, pt, "EPSG:32648", "EPSG:6933", cells, 0.0, 64)
ref = lattice_reference(depth, pt, "EPSG:32648", "EPSG:6933", cells, 0.0, k=48)
worst = max(abs(exact[k]["wet_fraction"] - ref[k]["wet_fraction"]) for k in cells)
print(f"wet_fraction 最大偏差 {worst:.4f} (点阵 K=48, O(1/K) 容差 0.06) ->",
      "对照通过" if worst < 0.06 else "对照失败")
PYEOF

step "9. 远程水深可恢复下载（可控 HTTP 服务，真实请求与断言）"
$PY "$ROOT/demo/fetch_demo.py" "$WORK/fetch_demo"

step "10. 下载 -> 输入清单 -> 分析入口（端到端）"
$PY "$ROOT/demo/fetch_pipeline_demo.py" "$WORK/fetch_pipeline"

step "11. 水位-流量率定换算: 版本交接 / 死区 / 不外推 / 交错测站"
$PY "$ROOT/demo/make_stage_discharge_samples.py" "$WORK/stage_discharge"

step "全部演示完成"
