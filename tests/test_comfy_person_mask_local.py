"""Local visual regression for the owner's cafe photo (skipped without assets)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from bot.config import DATA_DIR
from services.comfyui.lifecycle import DEFAULT_INSTALL_DIR


_SOURCE = DATA_DIR / "comfy_templates" / "11d21e523a8a46b586f1d7358cee147b.jpg"
_PYTHON = DEFAULT_INSTALL_DIR / ".venv" / "Scripts" / "python.exe"
_MODEL = DEFAULT_INSTALL_DIR / "models" / "ultralytics" / "segm" / "yolo11n-seg.pt"
_SAM = Path.home() / ".cache" / "huggingface" / "hub" / "models--facebook--sam-vit-base"
_WORKER = Path(__file__).resolve().parents[1] / "services" / "comfyui" / "person_mask_worker.py"


@pytest.mark.skipif(
    not all(path.exists() for path in (_SOURCE, _PYTHON, _MODEL, _SAM)),
    reason="requires the owner's local ComfyUI models and cafe test photo",
)
def test_person_mask_preserves_foreground_flower_and_cup(tmp_path):
    output = tmp_path / "mask.png"
    result = subprocess.run(
        [str(_PYTHON), str(_WORKER), str(_SOURCE), str(output), str(_MODEL)],
        cwd=DEFAULT_INSTALL_DIR, capture_output=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-1000:]
    with Image.open(output) as mask:
        assert mask.getpixel((330, 750)) == 0  # flower in front of the person
        assert mask.getpixel((460, 490)) == 0  # white cup stays in front
        assert mask.getpixel((450, 650)) == 255  # clothing is replaced
