"""TData import helpers shared by the web panel API."""
from __future__ import annotations

from pathlib import Path

REQUIRED_FILES = ("settings", "key_datas")


def find_tdata_roots(root: Path) -> list[Path]:
    """Return directories that look like a valid tdata folder."""
    return [
        p
        for p in root.rglob("tdata")
        if p.is_dir() and all((p / name).is_file() for name in REQUIRED_FILES)
    ]
