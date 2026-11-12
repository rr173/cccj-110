"""内容哈希工具。

哈希用于两条链路：
* 输入文件指纹 —— 写入计算清单，支持结果回溯；
* 几何/栅格指纹 —— 增量处理的缓存键，新边界版本只重算几何有变化的流域。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_json_hash(obj: object) -> str:
    """对可 JSON 序列化对象做稳定哈希 (排序键、无多余空白)。"""
    blob = json_dumps_stable(obj)
    return sha256_text(blob)


def json_dumps_stable(obj: object) -> str:
    import json

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
