"""Isolated YOLO detection and cached SAM contour refinement for person_mask.py.

Runs under the existing local ComfyUI venv. The segmentation weights and the
SAM model must be present locally; a missing model leaves the manual-mask path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image, ImageFilter, ImageOps
from transformers import SamModel, SamProcessor
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
    yolo_mask = (result.masks.data[0].cpu().numpy() > 0.5)
    if yolo_mask.shape != (photo.height, photo.width):
        yolo_mask = cv2.resize(
            yolo_mask.astype(np.uint8), photo.size, interpolation=cv2.INTER_NEAREST,
        ).astype(bool)
    person_box = result.boxes.xyxy[0].cpu().tolist()

    # SAM separates foreground props from the person inside YOLO's box.
    processor = SamProcessor.from_pretrained("facebook/sam-vit-base", local_files_only=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    segmenter = SamModel.from_pretrained(
        "facebook/sam-vit-base", local_files_only=True,
    ).to(device).eval()
    inputs = processor(images=photo, input_boxes=[[person_box]], return_tensors="pt")
    with torch.inference_mode():
        prediction = segmenter(**{key: value.to(device) for key, value in inputs.items()})
    candidates = processor.image_processor.post_process_masks(
        prediction.pred_masks.cpu(), inputs["original_sizes"].cpu(),
        inputs["reshaped_input_sizes"].cpu(),
    )[0][0].numpy().astype(bool)
    scores = prediction.iou_scores.cpu().flatten().tolist()
    ranked = sorted(zip(scores, candidates), key=lambda item: item[0], reverse=True)
    pixels = None
    for _score, candidate in ranked:
        intersection = np.count_nonzero(candidate & yolo_mask)
        union = np.count_nonzero(candidate | yolo_mask)
        if union and intersection / union >= 0.45:
            pixels = candidate.astype(np.uint8)
            break
    if pixels is None:
        raise ValueError("segmentation disagrees with person detection")

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
    mask = Image.fromarray(keep, "L").filter(ImageFilter.MaxFilter(5))
    mask.save(destination, format="PNG")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(2)
    make_mask(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
