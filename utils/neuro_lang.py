"""Грубая классификация RU/EN по символам (без LLM), один раз на старт нейрочата."""
from __future__ import annotations

import re


def classify_ru_en(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "unk"
    cyr = len(re.findall(r"[а-яА-ЯёЁ]", t))
    lat = len(re.findall(r"[a-zA-Z]", t))
    if cyr == 0 and lat == 0:
        return "unk"
    return "ru" if cyr >= lat else "en"
