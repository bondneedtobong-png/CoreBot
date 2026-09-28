"""Translate a Russian image prompt using a locally cached RU→EN model."""

from __future__ import annotations

import sys

import torch
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer


MODEL = "Helsinki-NLP/opus-mt-ru-en"


def translate(prompt: str) -> str:
    tokenizer = AutoTokenizer.from_pretrained(MODEL, local_files_only=True)
    model = AutoModelForSeq2SeqLM.from_pretrained(MODEL, local_files_only=True).eval()
    inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=512)
    with torch.inference_mode():
        result = model.generate(**inputs, max_new_tokens=256)
    return tokenizer.decode(result[0], skip_special_tokens=True).strip()


if __name__ == "__main__":
    source = sys.stdin.buffer.read(4097)
    if not source or len(source) > 4096:
        raise SystemExit(2)
    output = translate(source.decode("utf-8"))
    if not output or len(output) > 2000:
        raise SystemExit(3)
    sys.stdout.buffer.write(output.encode("utf-8"))
