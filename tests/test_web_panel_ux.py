# -*- coding: utf-8 -*-
"""Task 13 — web-panel guard rails.

1. Mojibake guard: характерные последовательности двойного перекодирования
   (UTF-8 -> Windows-1251 -> UTF-8) запрещены в исходниках web-panel/.
   Паттерн реконструируется из первых принципов: второй байт исходного
   UTF-8 (0x80-0xBF) после буквы-индикатора (Р/С/в) + спецслучаи.
2. Файлы панели — UTF-8 без BOM, с meta charset.
3. Статика отдаётся с корректными MIME + charset на всех платформах.
4. Ключевая UX-разметка/проводка присутствует, известные баги отсутствуют.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "web-panel"


def _corrset() -> str:
    """Символы, в которые мог превратиться второй байт UTF-8 (0x80-0xBF)."""
    cands = [chr(c) for c in range(0x80, 0x100)]
    cands += [chr(c) for c in range(0x400, 0x460)]
    cands += list("€№‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ")
    out = set()
    for ch in cands:
        for enc in ("cp1251", "cp1252", "latin-1"):
            try:
                b = ch.encode(enc)
            except UnicodeEncodeError:
                continue
            if len(b) == 1 and 0x80 <= b[0] <= 0xBF:
                out.add(ch)
                break
    return "".join(sorted(out, key=ord))


_CORR = _corrset()
_CLS = "".join(c for c in _CORR).replace("\\", "\\\\").replace("]", "\\]")

MOJIBAKE_RE = re.compile(
    "рџ|�|[ЂЏ]|ёЏ|[\x80-\x9f]"
    "|Р[" + _CLS + "]"
    "|С[" + _CLS + "]"
    "|В[«»]"
    "|в[" + _CLS + "][" + _CLS + "]"
)


def _read(name: str) -> str:
    return (PANEL / name).read_bytes().decode("utf-8")


def test_no_bom_in_panel_sources():
    for name in ("index.html", "main.js", "styles.css"):
        raw = (PANEL / name).read_bytes()
        assert raw[:3] != b"\xef\xbb\xbf", f"{name}: BOM запрещён (UTF-8 без BOM)"


def test_no_mojibake_in_panel_sources():
    hits = []
    for name in ("index.html", "main.js", "styles.css"):
        for i, line in enumerate(_read(name).splitlines(), 1):
            for m in MOJIBAKE_RE.finditer(line):
                hits.append(f"{name}:{i}: {m.group(0)!r}")
    assert not hits, "mojibake в исходниках:\n" + "\n".join(hits[:20])


def test_guard_catches_known_mojibake():
    samples = [
        "РѕС‚РєР»СЋС‡С‘РЅ",  # отключён
        "Р›РѕРіРёРЅ",  # Логин
        "вЂ”",  # em-dash
        "РїР°РЅРµР»Рё…",  # панели…
        "рџ“Љ",  # emoji
        "Р’СЃРµ",  # Все
        "Р СѓС‡РЅС‹Рµ",  # Ручные (Р + nbsp)
    ]
    for s in samples:
        assert MOJIBAKE_RE.search(s), f"guard не ловит: {s!r}"


def test_guard_allows_legit_russian():
    legit = [
        "Рассылки",
        "Навигация",
        "Все",
        "Логин",
        "Дашборд",
        "Режим",
        "Ручные 24ч",
        "Редактирование",
        "Роль read-only",
        "Проверка",
        "Сессия завершена",
        "ПЕРЕД повторным рендером",
        "—",
        "…",
        "«кавычки»",
        "в начало",
        "разделов…",
        "📊",
        "⚙️",
        "🧑‍🤝‍🧑",
    ]
    for s in legit:
        assert not MOJIBAKE_RE.search(s), f"ложное срабатывание: {s!r}"


def test_meta_charset_present():
    html = _read("index.html")
    assert '<meta charset="UTF-8"' in html


def test_panel_assets_versioned_for_cache_bust():
    """Ассеты подключаются с ?v= — браузер не подсунет старый кэшированный бандл."""
    html = _read("index.html")
    m_js = re.search(r'main\.js\?v=([\w.-]+)', html)
    m_css = re.search(r'styles\.css\?v=([\w.-]+)', html)
    assert m_js, "main.js без ?v= — старый бандл может остаться в кэше браузера"
    assert m_css, "styles.css без ?v= — старые стили могут остаться в кэше браузера"
    assert m_js.group(1) != "20260917-tdata-v1", "версия main.js не обновлена после правок"
    assert m_js.group(1) != "20260923-ux-v1", "версия main.js не обновлена после правок прокси"


def test_api_fetch_bypasses_http_cache():
    """api() обязан ходить с cache: no-store — иначе GET-списки после
    POST-мутаций (тест прокси) возвращаются из кэша и таблица врёт."""
    js = _read("main.js")
    assert 'cache: "no-store"' in js, "api() без cache:no-store — stale-таблицы"


def test_proxy_single_test_updates_row_immediately():
    """Кнопка Тест обязана сразу патчить строку (is_working/last_checked),
    а не надеяться только на refetch."""
    js = _read("main.js")
    assert "item.is_working = !!r.ok" in js
    assert "item.last_checked = new Date().toISOString()" in js
    assert "paintProxiesTable();" in js


def test_proxy_pools_and_file_import_wired():
    """Пулы карточками (свободно/занято) + загрузка .txt через bulk-import."""
    html = _read("index.html")
    js = _read("main.js")
    assert "paintPoolCards" in js
    assert "свободно" in js and "занято" in js
    assert 'id="prxImportFile"' in html or 'id="prxImportFile"' in js
    assert "/business/proxies/import" in js
    assert "host:port@user:pass" in js


def test_mailing_full_settings_wired():
    """Все настройки из бота доступны в карточке рассылки."""
    js = _read("main.js")
    for name in (
        'name="use_typing"',
        'name="smart_delay"',
        'name="variant_mode"',
        'name="max_recipients"',
        'name="mailing_cooldown_hours"',
        'name="audience_client_status"',
        'name="audience_include_classes"',
        'name="audience_exclude_classes"',
        "/test-recipients",
        'id="mailTestUsers"',
    ):
        assert name in js, f"в форме рассылки нет {name}"


def test_client_import_wired():
    """Загрузка клиентов из .txt через bulk-import."""
    js = _read("main.js")
    assert 'id="clImportFile"' in js
    assert "/business/clients/import" in js


def test_links_tracking_wired_and_recent_removed():
    """Раздел Ссылки + виджет переходов; «Последние сообщения (БД)» убраны."""
    html = _read("index.html")
    js = _read("main.js")
    assert 'data-route="links"' in html
    assert 'case "links"' in js
    assert "renderLinks" in js
    assert "/business/links" in js
    assert "/r/${" in js or '"/r/"' in js
    assert "loadDashLinks" in js
    assert "dashRecentDb" not in js
    assert "loadDashRecentDb" not in js


def test_panel_served_with_no_store():
    """Статика /panel отдаётся с Cache-Control: no-store (без застревания в кэше)."""
    os.environ.setdefault("PARSER_EMBEDDED", "0")
    os.environ.setdefault("COREBOT_ENV", "local")
    from fastapi.testclient import TestClient

    from control_plane.main import app

    client = TestClient(app, raise_server_exceptions=False)
    for path in ("/panel/", "/panel/main.js", "/panel/styles.css"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-store", (
            f"{path}: {r.headers.get('cache-control')}"
        )


def test_static_mime_and_charset():
    os.environ.setdefault("PARSER_EMBEDDED", "0")
    os.environ.setdefault("COREBOT_ENV", "local")
    from fastapi.testclient import TestClient

    from control_plane.main import app

    client = TestClient(app, raise_server_exceptions=False)
    cases = {
        "/panel/": "text/html",
        "/panel/index.html": "text/html",
        "/panel/main.js": "javascript",
        "/panel/styles.css": "text/css",
    }
    for path, mime_part in cases.items():
        r = client.get(path)
        assert r.status_code == 200, path
        ctype = r.headers.get("content-type", "")
        assert mime_part in ctype, f"{path}: {ctype}"
        assert "charset=utf-8" in ctype.lower(), f"{path}: {ctype}"


def test_tdata_check_wired_to_real_api():
    js = _read("main.js")
    assert 'case "tdata-check": return renderTdataCheck();' in js
    assert "/business/tdata/check" in js
    assert "/business/tdata/check/${encodeURIComponent(runId)}" in js
    assert "/business/proxy-groups" in js
    # TData-проверка не должна ходить в import-контракт
    assert (
        "/business/tdata/import"
        not in js.split("renderTdataCheck")[1].split("TData ZIP Import")[0]
    )


def test_guide_page_wired():
    """Отдельная страница-гайд: роут, навигация, палитра, контент."""
    html = _read("index.html")
    js = _read("main.js")
    assert 'data-route="guide"' in html
    assert '#/guide' in html
    assert 'case "guide"' in js
    assert 'function renderGuide' in js
    assert 'Как пользоваться CoreBot' in js
    assert 'outbound_queue' in js and 'bot_commands' in js


def test_nav_and_shell_wiring():
    html = _read("index.html")
    js = _read("main.js")
    css = _read("styles.css")
    assert 'data-route="tdata-check"' in html
    assert "nav-group-label" in html
    assert 'id="navToggle"' in html
    assert 'id="navBackdrop"' in html
    assert "skip-link" in html
    assert ":root" in css and "--cb-bg" in css
    assert "prefers-reduced-motion" in css
    assert "logs-mono" in css and "msg-cell" in js
    assert "field-error" in css and "setFieldError" in js
    assert "confirmDanger" in js
    assert "aria-current" in js
    # Исправлен селект палитры (был #mailingCreateForm)
    assert "#mailingCreateForm" not in js
    assert "#mailCreateForm input[name=name]" in js
