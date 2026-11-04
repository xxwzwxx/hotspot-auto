"""给 campus.log / hotspot.log 追加一行日志，格式和原来的 PowerShell 脚本一致。"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_log(path: Path | None, message: str) -> None:
    if not path:
        return
    user = os.environ.get("USERNAME", "") or os.environ.get("USER", "")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp()} [{user}] {message}\n")
    except OSError:
        pass
