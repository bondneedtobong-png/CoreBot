"""
Конвертация Tdata в .session файлы с использованием tgconvertor.
Современная библиотека для конвертации сессий Telegram (Python 3.11+).

Структура Tdata 2026:
tdata/
├── <hex_hash>/       # Папка с длинным именем (16+ символов)
│   ├── *.0           # Файлы сессии
│   └── *.1
├── settings          # Настройки клиента
├── key_datas         # Ключи
└── prefix            # Префикс
"""
import time
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Dict, Any, List
from telethon import TelegramClient

from telethon.errors import (
    FloodWaitError,
    AuthKeyUnregisteredError,
    SessionPasswordNeededError
)

from utils.logger import log


def _fallback_converter_root() -> Path:
    from bot.config import DATA_DIR

    return DATA_DIR / "tdata_converter_py311"


def _resolve_py311_executable() -> Optional[str]:
    """
    Ищет Python 3.11 для fallback-конвертера.
    Приоритет:
    1) env TDATA_CONVERTER_PYTHON
    2) py -3.11
    3) типичный путь пользователя
    """
    # Если текущий процесс уже на Python 3.11 (например venv311),
    # используем его — это самый стабильный путь на Windows.
    if sys.version_info.major == 3 and sys.version_info.minor == 11:
        return sys.executable

    env_py = os.getenv("TDATA_CONVERTER_PYTHON", "").strip()
    if env_py:
        if env_py.startswith("py "):
            return env_py
        if Path(env_py).exists():
            return env_py

    try:
        probe = subprocess.run(
            ["py", "-3.11", "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if probe.returncode == 0:
            exe = probe.stdout.strip()
            if exe and Path(exe).exists():
                return exe
            # Иногда launcher возвращает путь, который не виден из текущего процесса;
            # в таком случае используем сам launcher как «интерпретатор».
            return "py -3.11"
    except Exception:
        pass

    candidate = Path.home() / "Desktop" / "Python 3.11" / "python.exe"
    if candidate.exists():
        return str(candidate)

    uv_py312 = (
        Path.home()
        / "AppData"
        / "Roaming"
        / "uv"
        / "python"
        / "cpython-3.12.12-windows-x86_64-none"
        / "python.exe"
    )
    if uv_py312.exists():
        return str(uv_py312)
    return None


def _ensure_fallback_converter_env(py311_exe: str) -> tuple[bool, str]:
    """
    Создаёт (один раз) изолированный venv на Python 3.11
    и ставит tgconvertor[tdata] + opentele внутри него.
    """
    root = _fallback_converter_root()
    venv_dir = root / ".venv"
    py = venv_dir / "Scripts" / "python.exe"
    marker = root / ".ready"
    root.mkdir(parents=True, exist_ok=True)
    py311_cmd = ["py", "-3.11"] if py311_exe.startswith("py ") else [py311_exe]

    if py.exists() and marker.exists():
        return True, str(py)

    try:
        if not py.exists():
            subprocess.run([*py311_cmd, "-m", "venv", str(venv_dir)], check=True, timeout=120)

        subprocess.run(
            [
                str(py),
                "-m",
                "pip",
                "install",
                "-q",
                "tgconvertor[tdata]>=0.1.4",
                "PyQt5>=5.15.2",
                "PyQt5-sip>=12.13.0",
            ],
            check=True,
            timeout=300,
        )
        marker.write_text("ok", encoding="utf-8")
        return True, str(py)
    except Exception as e:
        return False, f"Не удалось подготовить fallback env: {e}"


def _run_fallback_convert_subprocess(
    converter_python: str,
    tdata_path: Path,
    session_path: Path,
    password: Optional[str],
) -> tuple[bool, str]:
    """
    Конвертация через отдельный Python 3.11 процесс.
    """
    script = (
        "import json,sys; "
        "from TGConvertor import SessionManager; "
        "tdata=sys.argv[1]; out=sys.argv[2]; pwd=sys.argv[3]; "
        "mgr = SessionManager.from_tdata_folder(tdata, password=pwd) if pwd else SessionManager.from_tdata_folder(tdata); "
        "import asyncio; asyncio.run(mgr.to_telethon_file(out)); "
        "print(json.dumps({'ok': True}))"
    )
    pwd = password or ""
    try:
        proc = subprocess.run(
            [converter_python, "-c", script, str(tdata_path), str(session_path), pwd],
            capture_output=True,
            text=True,
            timeout=240,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:800]
            return False, f"fallback subprocess error: {err}"
        return True, "ok"
    except Exception as e:
        return False, f"fallback subprocess exception: {e}"


async def _fallback_convert_tdata_to_session_file(
    tdata_path: Path,
    session_path: Path,
    password: Optional[str],
) -> tuple[bool, str]:
    py311 = _resolve_py311_executable()
    if not py311:
        return False, (
            "Python 3.11 не найден для fallback-конвертера. "
            "Укажите путь в TDATA_CONVERTER_PYTHON."
        )

    ok, env_or_err = await asyncio.to_thread(_ensure_fallback_converter_env, py311)
    if not ok:
        return False, str(env_or_err)

    ok2, msg = await asyncio.to_thread(
        _run_fallback_convert_subprocess,
        str(env_or_err),
        tdata_path,
        session_path,
        password,
    )
    return ok2, msg


def _explain_tdata_unavailable() -> str:
    """
    Почему SessionManager.from_tdata_folder может быть недоступен.
    TGConvertor ставит TDataSession=None, если импорт sessions.tdata упал (часто opentele/PyQt5).
    """
    try:
        from TGConvertor import manager as tg_mgr  # noqa: WPS433

        if tg_mgr.TDataSession is not None:
            return ""
    except BaseException as e:
        return f"Не удалось импортировать TGConvertor: {e}"

    ope = ""
    try:
        from opentele.td import TDesktop  # noqa: F401, WPS433

        ope = (
            "Пакет opentele импортируется, но TGConvertor не подключил TDataSession "
            "(переустановите: pip install --force-reinstall tgconvertor[tdata]). "
        )
    except BaseException as e:
        ope = (
            f"opentele не импортируется ({e!r}). "
            "Часто: PyQt5.sip отсутствует или несовместимость opentele с Telethon. "
        )

    return (
        ope
        + "Проверка: python -c \"from opentele.td import TDesktop\". "
        + "Ремонт зависимостей: pip install --force-reinstall \"PyQt5>=5.15\" \"PyQt5-sip>=12.15\" telethon opentele"
    )


# Глобальные переменные для API (загружаются один раз при импорте)
_api_initialized = False
_api_id = 0
_api_hash = ""


def _get_api_credentials():
    """
    Получение API ID и Hash из переменных окружения.
    Кэшируется после первого вызова.
    """
    global _api_initialized, _api_id, _api_hash
    
    if not _api_initialized:
        import os
        from dotenv import load_dotenv
        
        load_dotenv()
        
        _api_id = int(os.getenv("API_ID", "0"))
        _api_hash = os.getenv("API_HASH", "")
        
        if not _api_id or not _api_hash:
            raise ValueError("В .env файле отсутствуют API_ID или API_HASH")
        
        _api_initialized = True
        log.info(f"✅ API credentials загружены: ID={_api_id}")
    
    return _api_id, _api_hash


def find_tdata_roots(extract_dir: Path) -> List[Path]:
    """
    Поиск корневых папок Tdata после распаковки ZIP.
    
    Критерии поиска (в порядке приоритета):
    1. Папка с именем содержащим "tdata"
    2. Папка, содержащая:
       - Хотя бы одну подпапку с hex-именем (16+ символов)
       - Файл settings
       - Файл key_datas
    
    Args:
        extract_dir: Директория после распаковки ZIP
    
    Returns:
        List[Path]: Список найденных корневых папок Tdata
    """
    tdata_roots = []
    
    log.info(f"🔍 Поиск Tdata в: {extract_dir}")
    
    # Приоритет 1: Ищем папки с "tdata" в имени
    for item in extract_dir.rglob("*"):
        if not item.is_dir():
            continue
        
        item_name = item.name.lower()
        
        # Проверяем имя папки
        if "tdata" in item_name:
            if _is_valid_tdata_root(item):
                log.info(f"📁 Найдена Tdata папка (по имени): {item}")
                tdata_roots.append(item)
    
    # Приоритет 2: Ищем папки с правильной структурой
    if not tdata_roots:
        for item in extract_dir.rglob("*"):
            if not item.is_dir():
                continue
            
            # Пропускаем уже найденные
            if item in tdata_roots:
                continue
            
            if _is_valid_tdata_root(item):
                log.info(f"📁 Найдена Tdata папка (по структуре): {item}")
                tdata_roots.append(item)
    
    # Приоритет 3: Проверяем сам extract_dir
    if not tdata_roots and _is_valid_tdata_root(extract_dir):
        log.info(f"📁 extract_dir является Tdata: {extract_dir}")
        tdata_roots.append(extract_dir)
    
    log.info(f"✅ Найдено Tdata корней: {len(tdata_roots)}")
    
    return tdata_roots


def _is_valid_tdata_root(path: Path) -> bool:
    """
    Проверка, является ли папка корневой папкой Tdata.
    
    Критерии:
    1. Есть подпапка с hex-именем (16+ символов)
    2. Есть файл settings
    3. Есть файл key_datas
    
    Args:
        path: Путь к папке
    
    Returns:
        bool: True если это корневая папка Tdata
    """
    # Проверка на наличие hex-папки (16+ символов)
    has_hex_folder = False
    for item in path.iterdir():
        if item.is_dir():
            # Проверяем длину и hex-формат
            if len(item.name) >= 16:
                try:
                    int(item.name, 16)  # Проверяем, что это hex
                    has_hex_folder = True
                    break
                except ValueError:
                    continue
    
    # Проверка на наличие обязательных файлов
    has_settings = (path / "settings").exists()
    has_key_datas = (path / "key_datas").exists()
    
    # Должна быть hex-папка И хотя бы один из файлов
    if has_hex_folder and (has_settings or has_key_datas):
        return True
    
    # Или хотя бы оба файла
    if has_settings and has_key_datas:
        return True
    
    return False


async def convert_tdata_to_session(
    tdata_path: Path,
    sessions_dir: Path,
    password: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Конвертация Tdata в .session файл через tgconvertor.
    
    Args:
        tdata_path: Путь к корневой папке с Tdata
        sessions_dir: Директория для сохранения .session
        password: 2FA пароль (если есть)
    
    Returns:
        Dict с результатом:
        - success: bool
        - session_name: str (имя файла)
        - phone: str (номер телефона)
        - username: str (@username аккаунта)
        - user_id: int (ID аккаунта)
        - first_name: str (имя)
        - last_name: str (фамилия, опционально)
        - error: str (если ошибка)
    """
    try:
        tdata_path = Path(tdata_path)
        sessions_dir = Path(sessions_dir)
        
        # Проверка существования
        if not tdata_path.exists():
            return {"success": False, "error": f"Tdata папка не найдена: {tdata_path}"}
        
        # Валидация структуры Tdata
        if not _is_valid_tdata_root(tdata_path):
            log.warning(f"⚠️ Подозрительная структура Tdata: {tdata_path}")
            # Но продолжаем попытку конвертации
        
        log.info(f"📂 Начало конвертации Tdata: {tdata_path}")

        try:
            hint = _explain_tdata_unavailable()
        except BaseException as e:
            hint = f"Проверка TData завершилась с ошибкой импорта: {e!r}"
        # Генерируем уникальное имя сессии заранее: понадобится и primary, и fallback
        timestamp = int(time.time())
        session_name = f"account_{timestamp}"
        session_path = sessions_dir / f"{session_name}.session"

        if hint:
            log.warning(f"⚠️ Primary TData путь недоступен: {hint}")
            log.info("🛟 Пробую fallback-конвертер (изолированный Python 3.11)...")
            fb_ok, fb_msg = await _fallback_convert_tdata_to_session_file(
                tdata_path=tdata_path,
                session_path=session_path,
                password=password,
            )
            if not fb_ok:
                log.error(f"❌ Fallback не сработал: {fb_msg}")
                return {
                    "success": False,
                    "error": f"{hint}\n\nFallback: {fb_msg}",
                }
            log.info(f"✅ Fallback конвертация завершена: {session_path}")
            manager = None

        # Создаём сессию из Tdata через tgconvertor
        if hint == "":
            try:
                log.info("🔄 Загрузка Tdata через tgconvertor...")
                from TGConvertor import SessionManager  # ленивый импорт: не валим старт всего бота
                # from_tdata_folder — синхронный метод; запускаем в thread + таймаут
                if password:
                    manager = await asyncio.wait_for(
                        asyncio.to_thread(
                            SessionManager.from_tdata_folder,
                            str(tdata_path),
                            password=password,
                        ),
                        timeout=120,
                    )
                else:
                    manager = await asyncio.wait_for(
                        asyncio.to_thread(
                            SessionManager.from_tdata_folder,
                            str(tdata_path),
                        ),
                        timeout=120,
                    )

                log.info("✅ Tdata загружена")

            except BaseException as e:
                log.warning(f"⚠️ Primary конвертация упала: {e}")
                log.info("🛟 Пробую fallback-конвертер (изолированный Python 3.11)...")
                fb_ok, fb_msg = await _fallback_convert_tdata_to_session_file(
                    tdata_path=tdata_path,
                    session_path=session_path,
                    password=password,
                )
                if not fb_ok:
                    log.error(f"❌ Fallback не сработал: {fb_msg}")
                    return {
                        "success": False,
                        "error": f"Ошибка загрузки Tdata: {e}\n\nFallback: {fb_msg}",
                    }
                log.info(f"✅ Fallback конвертация завершена: {session_path}")
                manager = None
        
        if manager is not None:
            # Проверяем, есть ли аккаунт (async метод!)
            try:
                user = await manager.get_user()
                if not user:
                    log.error("❌ В Tdata не найдено аккаунтов")
                    return {"success": False, "error": "В Tdata нет аккаунтов"}

                log.info(f"📊 Найден аккаунт: {user}")
            except Exception as e:
                log.error(f"❌ Ошибка получения информации об аккаунте: {e}")
                return {"success": False, "error": f"Ошибка получения аккаунта: {e}"}

            log.info(f"💾 Конвертация в сессию: {session_path}")

            # Сохраняем как Telethon .session файл (async метод!)
            try:
                await manager.to_telethon_file(str(session_path))
                log.info(f"✅ Сессия сохранена: {session_path}")
            except Exception as e:
                log.error(f"❌ Ошибка сохранения сессии: {e}")
                return {"success": False, "error": f"Ошибка сохранения сессии: {e}"}
        
        # Получаем информацию об аккаунте через Telethon
        try:
            # Создаём клиента Telethon вручную (make_telethon() не существует!)
            # Используем реальные API credentials из .env
            api_id, api_hash = _get_api_credentials()
            
            client = TelegramClient(str(session_path), api_id=api_id, api_hash=api_hash)
            await client.connect()
            log.info("🔌 Подключение к Telegram...")
            
            # Используем is_user_authorized() вместо isAuthorized()
            if await client.is_user_authorized():
                me = await client.get_me()
                
                # Извлекаем информацию
                phone = me.phone if hasattr(me, 'phone') and me.phone else "unknown"
                username = me.username if hasattr(me, 'username') else None
                user_id = me.id if hasattr(me, 'id') else 0
                first_name = me.first_name if hasattr(me, 'first_name') else ""
                last_name = me.last_name if hasattr(me, 'last_name') else ""
                
                display_name = f"{first_name} {last_name}".strip() if first_name else "Unknown"
                
                log.info("✅ Аккаунт успешно конвертирован:")
                log.info(f"   📱 Телефон: {phone}")
                log.info(f"   👤 Username: @{username or 'N/A'}")
                log.info(f"   🆔 ID: {user_id}")
                log.info(f"   📛 Имя: {display_name}")
                
                await client.disconnect()
                log.info("🔌 Отключено")
                
                return {
                    "success": True,
                    "session_name": session_name,
                    "phone": phone,
                    "username": username,
                    "user_id": user_id,
                    "first_name": first_name,
                    "last_name": last_name,
                }
            else:
                log.warning("⚠️ Сессия неавторизована")
                await client.disconnect()
                return {
                    "success": False,
                    "error": "Сессия неавторизована",
                }
                
        except SessionPasswordNeededError:
            log.error("❌ Требуется 2FA пароль. Передайте параметр password.")
            return {
                "success": False,
                "error": "Требуется 2FA пароль. Передайте параметр password.",
            }
            
        except FloodWaitError as e:
            log.error(f"⏳ FloodWait при конвертации: {e.seconds} сек")
            return {
                "success": False,
                "error": f"FloodWait: {e.seconds} сек",
            }
            
        except AuthKeyUnregisteredError:
            log.error("❌ Сессия недействительна (AuthKeyUnregistered)")
            return {
                "success": False,
                "error": "Сессия недействительна",
            }
            
        except Exception as connect_error:
            log.error(f"❌ Ошибка подключения: {connect_error}")
            import traceback
            log.error(traceback.format_exc())
            return {
                "success": False,
                "error": str(connect_error),
            }
        
    except Exception as e:
        log.error(f"❌ Критическая ошибка конвертации: {e}")
        import traceback
        log.error(traceback.format_exc())
        return {
            "success": False,
            "error": str(e),
        }


async def check_session_validity(session_path: Path) -> bool:
    """
    Проверка валидности сессии.

    Args:
        session_path: Путь к .session файлу

    Returns:
        bool: True если сессия валидна и авторизована
    """
    try:
        session_path = Path(session_path)

        if not session_path.exists():
            log.warning(f"❌ Сессия не найдена: {session_path}")
            return False

        # Используем реальные API credentials из .env
        api_id, api_hash = _get_api_credentials()

        client = TelegramClient(str(session_path), api_id=api_id, api_hash=api_hash)
        await client.connect()

        # Используем is_user_authorized() вместо isAuthorized()
        is_valid = await client.is_user_authorized()

        await client.disconnect()

        if is_valid:
            log.info(f"✅ Сессия валидна: {session_path.name}")
        else:
            log.warning(f"⚠️ Сессия неавторизована: {session_path.name}")
        
        return is_valid
        
    except Exception as e:
        log.error(f"❌ Ошибка проверки сессии {session_path}: {e}")
        return False


async def get_account_info(session_path: Path) -> Optional[Dict[str, Any]]:
    """
    Получение полной информации об аккаунте из сессии.

    Args:
        session_path: Путь к .session файлу

    Returns:
        Dict с информацией об аккаунте или None
    """
    try:
        session_path = Path(session_path)

        if not session_path.exists():
            log.warning(f"❌ Сессия не найдена: {session_path}")
            return None

        # Используем реальные API credentials из .env
        api_id, api_hash = _get_api_credentials()

        client = TelegramClient(str(session_path), api_id=api_id, api_hash=api_hash)
        await client.connect()

        # Используем is_user_authorized() вместо isAuthorized()
        if not await client.is_user_authorized():
            log.warning(f"⚠️ Сессия неавторизована: {session_path}")
            await client.disconnect()
            return None

        me = await client.get_me()

        info = {
            "user_id": me.id,
            "phone": me.phone,
            "username": me.username,
            "first_name": me.first_name,
            "last_name": me.last_name,
            "is_bot": me.bot if hasattr(me, 'bot') else False,
            "is_premium": me.premium if hasattr(me, 'premium') else False,
        }

        await client.disconnect()

        log.info(f"✅ Получена информация об аккаунте: @{info['username'] or 'N/A'}")

        return info

    except Exception as e:
        log.error(f"❌ Ошибка получения информации об аккаунте: {e}")
        return None
