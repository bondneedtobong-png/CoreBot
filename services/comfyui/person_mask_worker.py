"""Isolated Ultralytics inference process used by person_mask.py.

Runs under the existing local ComfyUI venv. The official small segmentation
weight is downloaded to ComfyUI/models/ultralytics/segm on first use.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFilter, ImageOps
from ultralytics import YOLO


def make_mask(source: Path, destination: Path, model_path: Path) -> None:
    with Image.open(source) as photo_file:
        photo = ImageOps.exif_transpose(photo_file).convert("RGB")
    if min(photo.size) < 512 or max(photo.size) > 4096:
        raise ValueError("unsupported template dimensions")

    model_path.parent.mkdir(parents=True, exist_ok=True)
    result = YOLO(str(model_path)).predict(
        source=np.asarray(photo), classes=[0], conf=0.35, imgsz=1024,
        retina_masks=True, verbose=False,
    )[0]
    if result.masks is None or len(result.boxes) != 1:
        raise ValueError("exactly one person is required")
    pixels = (result.masks.data[0].cpu().numpy() > 0.5).astype(np.uint8)
    if pixels.shape != (photo.height, photo.width):
        pixels = cv2.resize(pixels, photo.size, interpolation=cv2.INTER_NEAREST)

    # Ignore tiny detached false positives while preserving the person's body.
    count, components, stats, _ = cv2.connectedComponentsWithStats(pixels, 8)
    if count < 2:
        raise ValueError("person mask is empty")
    largest = max(stats[1:, cv2.CC_STAT_AREA])
    keep = np.zeros_like(pixels)
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] >= max(100, largest * 0.01):
            keep[components == label] = 255
    coverage = np.count_nonzero(keep) / keep.size
    if not 0.02 <= coverage <= 0.95:
        raise ValueError("person mask coverage is implausible")

    # Include a narrow halo around hair and clothing; the inpaint path softens it.
    mask = Image.fromarray(keep, "L").filter(ImageFilter.MaxFilter(9))
    mask.save(destination, format="PNG")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(2)
    make_mask(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
