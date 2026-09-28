"""Report face boxes for a template and its generated replacement.

This process uses the local ComfyUI environment, which already has Ultralytics.
It reports measurements only; the caller makes the quality decision.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ultralytics import YOLO


def _face_box(result) -> list[float] | None:
    if len(result.boxes) != 1:
        return None
    return [float(value) for value in result.boxes.xyxy[0].cpu().tolist()]


def inspect(source: Path, generated: Path, model_path: Path) -> dict:
    detector = YOLO(str(model_path))
    results = detector.predict(
        source=[str(source), str(generated)], conf=0.45, imgsz=1024,
        device="cpu", verbose=False,
    )
    return {"source": _face_box(results[0]), "generated": _face_box(results[1])}


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(2)
    print(json.dumps(inspect(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))))
