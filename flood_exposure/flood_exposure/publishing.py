"""暂存再发布 (stage-then-publish) 与发布闸门。

输出目录布局
------------
    output_root/
      staging/<run_id>/        暂存区：对外不可见，可随时整体删除
      releases/
        current -> release_0003   符号链接，原子切换，永远指向上一完整版本
        release_0001/ ...
        release_0003/
          exposure.csv           逐 (流域, 时次) 明细
          run_manifest.json      计算清单：输入版本/指纹/参数/闸门/标记
          weight_cache/          本运行使用/生成的权重表缓存
          SUCCESS                完整发布标记文件

运行中断 (含进程被 kill) 时：
* 暂存区里的半成品留在 staging/，releases/current 不动，读者只能看到
  上一完整版本；下次运行启动时清理未完成的暂存区。
* NoData 超配置上限时抛出 :class:`PublishGateError`，发布被拒绝，
  绝不会静默按零填补，current 同样保持指向上一版本。
"""
from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .hashing import json_dumps_stable

SUCCESS_FILE = "SUCCESS"
CURRENT_LINK = "current"
STAGING_DIR = "staging"
RELEASES_DIR = "releases"


class PublishGateError(RuntimeError):
    """发布闸门未通过 (NoData 超限 / 修复失败流域超限)。"""


@dataclass
class GateReport:
    failures: list[dict]
    invalid_basin_fraction: float
    passed: bool


class StagedRun:
    """一次运行的暂存区句柄，支持中断安全与原子发布。"""

    def __init__(self, output_root: Path, run_id: str):
        self.output_root = Path(output_root)
        self.run_id = run_id
        self.staging = self.output_root / STAGING_DIR / run_id
        self.releases = self.output_root / RELEASES_DIR
        self.release_dir = self.releases / run_id
        self._aborted = False
        self._old_handler = None

    # ------------------------------------------------------------------ #
    def __enter__(self) -> "StagedRun":
        # 清理同名的历史残留暂存区（上次中断遗留）
        if self.staging.exists():
            _rm_tree(self.staging)
        (self.staging / "weight_cache").mkdir(parents=True, exist_ok=True)
        # SIGTERM/SIGINT：置中止标志，由运行器在检查点主动 Abort；
        # 不做信号里的复杂清理，避免在非可重入状态下操作文件系统。
        self._old_handler = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._old_handler[sig] = signal.signal(sig, self._handle_signal)
            except (ValueError, OSError):
                pass
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for sig, handler in self._old_handler.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        if exc_type is not None or self._aborted:
            # 任何异常或中止：销毁暂存区，releases/current 原封不动
            if self.staging.exists():
                _rm_tree(self.staging)

    def _handle_signal(self, signum, frame) -> None:
        self._aborted = True

    @property
    def abort_requested(self) -> bool:
        return self._aborted

    def raise_if_aborted(self) -> None:
        if self._aborted:
            raise KeyboardInterrupt("收到中止信号，丢弃暂存区，保持上一完整发布版本")

    # ------------------------------------------------------------------ #
    def stage_path(self, name: str) -> Path:
        return self.staging / name

    def write_text(self, name: str, text: str) -> Path:
        p = self.staging / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, p)
        return p

    def write_json(self, name: str, obj: dict) -> Path:
        return self.write_text(name, json.dumps(obj, ensure_ascii=False, indent=2,
                                                sort_keys=True))

    # ------------------------------------------------------------------ #
    def check_gate(self, exposure_rows: list[dict], failed_basin_ids: list[str],
                   n_basins: int, cfg: Config) -> GateReport:
        """发布闸门：逐 (流域,时次) 缺测比例 + 边界修复失败比例。"""
        failures: list[dict] = []
        for row in exposure_rows:
            if row["missing_fraction"] > cfg.max_missing_fraction + 1e-12:
                failures.append({
                    "basin_id": row["basin_id"],
                    "forecast_time": row["forecast_time"],
                    "missing_fraction": row["missing_fraction"],
                    "limit": cfg.max_missing_fraction,
                    "reason": "NODATA_GATE_FAIL",
                })
        invalid_fraction = (
            len(failed_basin_ids) / n_basins if n_basins else 1.0
        )
        if invalid_fraction > cfg.max_invalid_basin_fraction + 1e-12:
            failures.append({
                "basin_id": None,
                "forecast_time": None,
                "invalid_basin_fraction": invalid_fraction,
                "limit": cfg.max_invalid_basin_fraction,
                "reason": "INVALID_BASIN_GATE_FAIL",
                "failed_basin_ids": failed_basin_ids,
            })
        report = GateReport(
            failures=failures,
            invalid_basin_fraction=invalid_fraction,
            passed=not failures,
        )
        return report

    def publish(self, gate: GateReport) -> Path:
        """原子发布：暂存目录整体 rename 到 releases，再原子切换 current。"""
        if not gate.passed:
            raise PublishGateError(
                "发布闸门拒绝:\n" + json.dumps(gate.failures, ensure_ascii=False, indent=2)
            )
        self.releases.mkdir(parents=True, exist_ok=True)
        if self.release_dir.exists():  # 同一 run_id 重跑：先隔离旧目录
            _rm_tree(self.release_dir)
        os.replace(self.staging, self.release_dir)

        marker = self.release_dir / SUCCESS_FILE
        marker.write_text(
            json_dumps_stable({"published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                             time.gmtime())}),
            encoding="utf-8",
        )
        link = self.releases / CURRENT_LINK
        tmp_link = self.releases / (CURRENT_LINK + ".tmp")
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        os.symlink(self.release_dir.name, tmp_link)
        os.replace(tmp_link, link)  # rename(2) 原子替换符号链接
        return self.release_dir


def _rm_tree(path: Path) -> None:
    """不依赖 shutil 的递归删除，显式处理符号链接。"""
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            _rm_tree(child)
        else:
            child.unlink()
    path.rmdir()


def current_release(output_root: Path) -> Path | None:
    link = Path(output_root) / RELEASES_DIR / CURRENT_LINK
    if not link.is_symlink():
        return None
    target = link.resolve()
    if not (target / SUCCESS_FILE).exists():
        return None
    return target


def list_releases(output_root: Path) -> list[Path]:
    root = Path(output_root) / RELEASES_DIR
    if not root.exists():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and not p.is_symlink() and (p / SUCCESS_FILE).exists()
    )


def cleanup_staging(output_root: Path) -> list[str]:
    """启动时清理所有未完成暂存区，返回被清理的 run_id。"""
    staging = Path(output_root) / STAGING_DIR
    if not staging.exists():
        return []
    removed = []
    for child in staging.iterdir():
        if child.is_dir():
            _rm_tree(child)
            removed.append(child.name)
    return removed
