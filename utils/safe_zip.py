"""Bounded ZIP extraction for user supplied archives."""
from __future__ import annotations

import stat
import zipfile
from pathlib import Path


def extract_zip_safely(
    archive: Path,
    destination: Path,
    *,
    max_files: int = 5000,
    max_uncompressed_bytes: int = 1_000_000_000,
) -> None:
    """Reject path traversal, links and oversized archives before extracting."""
    root = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > max_files:
            raise ValueError("Слишком много файлов в архиве")
        if sum(item.file_size for item in entries) > max_uncompressed_bytes:
            raise ValueError("Распакованный архив слишком большой")
        for item in entries:
            name = item.filename.replace("\\", "/")
            parts = name.split("/")
            if not name or name.startswith("/") or any(part in ("", "..") for part in parts if part != parts[-1]):
                raise ValueError("Недопустимый путь в ZIP-архиве")
            if ".." in parts or ":" in parts[0]:
                raise ValueError("Недопустимый путь в ZIP-архиве")
            mode = (item.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError("Ссылки внутри ZIP-архива не поддерживаются")
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise ValueError("Недопустимый путь в ZIP-архиве")
        root.mkdir(parents=True, exist_ok=True)
        extracted_bytes = 0
        for item in entries:
            target = root / item.filename.replace("\\", "/")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as reader, target.open("wb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        extracted_bytes += len(chunk)
                        if extracted_bytes > max_uncompressed_bytes:
                            raise ValueError("Распакованный архив слишком большой")
                        writer.write(chunk)
