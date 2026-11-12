"""只读查询：从已发布版本读取流域明细与计算清单。

查询永远走 releases/current（或显式指定的发布目录），不接触暂存区，
因此运行中断期间的读者看到的始终是上一完整版本。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .publishing import current_release, list_releases


class ReleaseNotFoundError(FileNotFoundError):
    pass


def _resolve_release(output_root: Path, release: str | None = None) -> Path:
    output_root = Path(output_root)
    if release is not None:
        p = output_root / "releases" / release
        if not (p / "SUCCESS").exists():
            raise ReleaseNotFoundError(f"发布版本不存在或不完整: {p}")
        return p
    cur = current_release(output_root)
    if cur is None:
        raise ReleaseNotFoundError(
            f"{output_root} 下没有完整发布版本 (releases/current 缺失)"
        )
    return cur


def load_exposure_rows(output_root: Path, release: str | None = None) -> list[dict]:
    p = _resolve_release(output_root, release) / "exposure.csv"
    with open(p, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_run_manifest(output_root: Path, release: str | None = None) -> dict:
    p = _resolve_release(output_root, release) / "run_manifest.json"
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def query_basin(output_root: Path, basin_id: str,
                release: str | None = None) -> list[dict]:
    """返回某流域在所有预报时次上的明细行。"""
    rows = load_exposure_rows(output_root, release)
    hits = [r for r in rows if r["basin_id"] == basin_id]
    if not hits:
        available = sorted({r["basin_id"] for r in rows})
        raise KeyError(
            f"流域 '{basin_id}' 不在发布版本中，可用流域: {available}"
        )
    return hits


def query_forecast(output_root: Path, forecast_time: str,
                   release: str | None = None) -> list[dict]:
    rows = load_exposure_rows(output_root, release)
    return [r for r in rows if r["forecast_time"] == forecast_time]


def provenance(output_root: Path, basin_id: str | None = None,
               release: str | None = None) -> dict:
    """追溯信息：输入版本/sha256、参数、缓存命中与闸门结论。"""
    manifest = load_run_manifest(output_root, release)
    info = {
        "run_id": manifest["run_id"],
        "code_version": manifest["code_version"],
        "created_at": manifest["created_at"],
        "config": manifest["config"],
        "input_summary": manifest["input_summary"],
        "inputs": manifest["inputs"],
        "gate": manifest["gate"],
        "incremental": manifest["incremental"],
    }
    if basin_id is not None:
        info["basin"] = next(
            (b for b in manifest["basins"] if b["basin_id"] == basin_id), None
        )
    return info


def available_releases(output_root: Path) -> list[str]:
    return [p.name for p in list_releases(Path(output_root))]
