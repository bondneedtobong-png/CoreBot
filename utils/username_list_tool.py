"""
Разбор списков t.me/@username, сортировка «похожих» рядом, эвристики мусорных имён.
"""
from __future__ import annotations

import re
from typing import Iterable

# Telegram: 5–32 символа, буквы цифры _
_UNAME_CORE = r"[a-zA-Z0-9_]{5,32}"

# Строка с t.me / telegram.me
_TME_LINE = re.compile(
    rf"(?:https?://)?(?:t\.me|telegram\.me)/({_UNAME_CORE})",
    re.IGNORECASE,
)
_AT_WORD = re.compile(rf"@({_UNAME_CORE})\b")
# «Голое» имя на строке (если нет других совпадений)
_PLAIN_LINE = re.compile(rf"^\s*({_UNAME_CORE})\s*$", re.MULTILINE)


def extract_usernames(text: str) -> list[str]:
    """Достаёт username из текста (ссылки t.me, @nick, строка = nick). Порядок появления, без дублей."""
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        u = raw.strip().lstrip("@").lower()
        if len(u) < 5 or len(u) > 32:
            return
        if u in seen:
            return
        seen.add(u)
        out.append(u)

    for m in _TME_LINE.finditer(text or ""):
        add(m.group(1))
    for m in _AT_WORD.finditer(text or ""):
        add(m.group(1))
    for m in _PLAIN_LINE.finditer(text or ""):
        add(m.group(1))

    return out


def _stem_for_sort(u: str) -> str:
    """
    Общий «стебель» для группировки: hu112345 и hu122233 → hu.
    Хвост из цифр отрезаем; подчёркивания сохраняем (carter_one1 → carter_one).
    """
    u0 = u.lower().strip()
    u = re.sub(r"[0-9]+$", "", u0)
    return u if u else u0


def sort_grouped(usernames: Iterable[str]) -> list[str]:
    """Сортировка: сначала по stem (похожие рядом), затем лексикографически."""
    uniq = list(dict.fromkeys(u.lower() for u in usernames))
    return sorted(uniq, key=lambda x: (_stem_for_sort(x), x))


def _min_block_size(prefix_len: int) -> int:
    """Чем короче префикс, тем больше нужно совпадений. L=2..3 не используем — слишком много ложных склеек."""
    return {6: 10, 5: 14, 4: 18}.get(prefix_len, 999)


def _iter_runs_same_prefix(sorted_u: list[str], prefix_len: int) -> list[list[str]]:
    """Подряд в отсортированном списке с одинаковыми первыми prefix_len символами."""
    if prefix_len < 4:
        return []
    runs: list[list[str]] = []
    i = 0
    n = len(sorted_u)
    while i < n:
        if len(sorted_u[i]) < prefix_len:
            i += 1
            continue
        pref = sorted_u[i][:prefix_len]
        j = i + 1
        while j < n and len(sorted_u[j]) >= prefix_len and sorted_u[j][:prefix_len] == pref:
            j += 1
        runs.append(sorted_u[i:j])
        i = j
    return runs


def _block_looks_bot_farm(block: list[str], _prefix_len: int) -> bool:
    """
    Пачка похожа на ферму: много ников с длинными хвостами цифр или паттерн «2 буквы + цифры».

    Отсекаем блоки вроде trader*/trust*, где мало цифровых хвостов (человеческие ники).
    """
    n = len(block)
    if n < 8:
        return False

    digit_ge5_end = sum(1 for u in block if re.search(r"\d{5,}$", u))
    digit_ge8_end = sum(1 for u in block if re.search(r"\d{8,}$", u))
    digit_ge10_end = sum(1 for u in block if re.search(r"\d{10,}$", u))
    mostly_no_digits = sum(1 for u in block if not re.search(r"\d{4,}$", u))

    # Явно «человеческий» блок: почти нет длинных цифровых хвостов
    if mostly_no_digits / n > 0.62 and digit_ge8_end / n < 0.04 and digit_ge5_end / n < 0.18:
        return False

    # Длинные «телефонные» хвосты (truongkimnguyen2949302265)
    if digit_ge10_end / n >= 0.08 and n >= 10:
        return True
    if digit_ge8_end / n >= 0.12 and n >= 10:
        return True
    if digit_ge5_end / n >= 0.35 and n >= 12:
        return True

    # Очень большая пачка с умеренной долей цифр (огромные фермы)
    if n >= 150 and digit_ge5_end / n >= 0.12:
        return True
    if n >= 400 and digit_ge5_end / n >= 0.06:
        return True

    return False


def _detect_two_letter_digit_farms(uniq: list[str]) -> set[str]:
    """
    Отдельно от префикса L≥4: только ники вида «две буквы + цифры» (hu1205826),
    сгруппированные по двум буквам — не смешиваем с общим префиксом «tr».
    """
    narrow = [u for u in uniq if re.fullmatch(r"[a-z]{2}\d{5,}", u)]
    narrow.sort()
    out: set[str] = set()
    i = 0
    while i < len(narrow):
        p2 = narrow[i][:2]
        j = i + 1
        while j < len(narrow) and narrow[j][:2] == p2:
            j += 1
        block = narrow[i:j]
        if len(block) >= 8:
            out.update(block)
        i = j
    return out


def detect_batch_suspicious(usernames: Iterable[str]) -> set[str]:
    """
    Подозрительные = пачки в **отсортированном** списке с общим префиксом 4–6 символов
    (truong…, trust_… не смешиваются с коротким tr) + структура с длинными цифровыми хвостами.

    Дополнительно: фермы «ровно 2 буквы + цифры» (hu…) по отдельному проходу.

    Одиночные креативные ники без большой соседней фермы не попадают.
    """
    uniq = sorted({u.lower().strip() for u in usernames if u and len(u.strip()) >= 5})
    if not uniq:
        return set()

    out: set[str] = set()
    for prefix_len in (6, 5, 4):
        need = _min_block_size(prefix_len)
        for block in _iter_runs_same_prefix(uniq, prefix_len):
            if len(block) < need:
                continue
            if _block_looks_bot_farm(block, prefix_len):
                out.update(block)
    out.update(_detect_two_letter_digit_farms(uniq))
    return out


def filter_suspicious(usernames: Iterable[str]) -> set[str]:
    """Совместимое имя: то же, что detect_batch_suspicious."""
    return detect_batch_suspicious(usernames)


def format_at_lines(usernames: Iterable[str]) -> str:
    return "\n".join(f"@{u}" for u in usernames)
