"""Safe ZIP extraction + общий helper поиска TData-корней (задача 12).

``safe_extract_zip`` — явная защита от path traversal (абсолютные пути,
``..``-сегменты в POSIX- и Windows-нотации, drive-буквы, NUL-байты) и
лимит распакованного объёма. Нарушение → :class:`ZipSafetyError` с
машиночитаемым ``code`` (run/item маппится в ``archive_invalid``).
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from pathlib import Path, PureWindowsPath


class ZipSafetyError(Exception):
    """Архив отклонён guard'ом. ``code`` — безопасный error code наружу."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def _is_safe_member(name: str) -> bool:
    """True, если имя записи ZIP безопасно распаковывать в целевой каталог."""
    if not name or "\x00" in name:
        return False
    # Нормализуем обе нотации: zip хранит '/', на Windows опасен и '\\'.
    unified = name.replace("\\", "/")
    if posixpath.isabs(unified):
        return False
    # Drive-буква (C:/..., C:...) — абсолютный путь на Windows.
    if len(unified) >= 2 and unified[1] == ":":
        return False
    parts = [p for p in unified.split("/") if p not in ("", ".")]
    if not parts:
        return False
    if any(p == ".." for p in parts):
        return False
    # Перестраховка: после normpath не должно остаться выхода наверх.
    if posixpath.normpath("/" + "/".join(parts)) == "/":
        return False
    win_parts = [
        p for p in PureWindowsPath(name).parts if p not in ("", ".", "\\", "/")
    ]
    if any(p == ".." for p in win_parts):
        return False
    return True


def safe_extract_zip(data: bytes, dest: Path, *, max_bytes: int) -> Path:
    """Распаковать ZIP в ``dest`` с guard'ами. Возвращает ``dest``.

    Raises:
        ZipSafetyError: ``not_a_zip`` / ``archive_empty`` / ``archive_too_large``
            / ``path_traversal`` / ``no_files``.
    """
    if not data:
        raise ZipSafetyError("archive_empty", "archive is empty")
    if len(data) > max_bytes:
        raise ZipSafetyError("archive_too_large", f"archive exceeds {max_bytes} bytes")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ZipSafetyError("not_a_zip", "not a valid ZIP archive") from exc
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos:
            raise ZipSafetyError("no_files", "archive contains no files")
        total = sum(max(0, int(i.file_size)) for i in infos)
        if total > max_bytes:
            raise ZipSafetyError(
                "archive_too_large", f"uncompressed size {total} exceeds {max_bytes}"
            )
        for info in infos:
            if not _is_safe_member(info.filename):
                raise ZipSafetyError(
                    "path_traversal", f"unsafe entry: {info.filename[:120]!r}"
                )
        dest.mkdir(parents=True, exist_ok=True)
        for info in infos:
            target = dest.joinpath(
                *[
                    p
                    for p in info.filename.replace("\\", "/").split("/")
                    if p not in ("", ".")
                ]
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1024 * 64)
                    if not chunk:
                        break
                    out.write(chunk)
    return dest


def is_tdata_root(path: Path) -> bool:
    """Эвристика корня TData: settings+key_datas или hex-подпапка + один из них."""
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    has_settings = (path / "settings").is_file()
    has_key_datas = (path / "key_datas").is_file()
    if has_settings and has_key_datas:
        return True
    has_hex = False
    for item in entries:
        if item.is_dir() and len(item.name) >= 16:
            try:
                int(item.name, 16)
            except ValueError:
                continue
            has_hex = True
            break
    return bool(has_hex and (has_settings or has_key_datas))


def find_check_roots(extract_dir: Path) -> list[Path]:
    """Общий helper: все валидные корни, стабильный порядок, без вложенных дублей.

    Возвращает top-most корни, отсортированные по относительному POSIX-пути
    (сам ``extract_dir`` — последний кандидат, чтобы вложенные имели приоритет
    стабильности детерминированного ``item_id``).
    """
    candidates: list[Path] = []
    try:
        nested = sorted(
            (p for p in extract_dir.rglob("*") if p.is_dir()),
            key=lambda p: p.relative_to(extract_dir).as_posix(),
        )
    except OSError:
        nested = []
    for item in nested:
        if is_tdata_root(item):
            candidates.append(item)
    if not candidates and is_tdata_root(extract_dir):
        candidates.append(extract_dir)
    # Убираем корни, вложенные в другой найденный корень (top-most only).
    tops: list[Path] = []
    for root in candidates:
        if not any(root != t and t in root.parents for t in candidates):
            tops.append(root)
    return sorted(tops, key=lambda p: p.relative_to(extract_dir).as_posix())


__all__ = [
    "ZipSafetyError",
    "safe_extract_zip",
    "is_tdata_root",
    "find_check_roots",
]
