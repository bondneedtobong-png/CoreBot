"""
Утилита для проверки прокси.
"""
import asyncio
import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType
from loguru import logger

log = logger


async def check_proxy(
    proxy_type: str,
    host: str,
    port: int,
    username: str = None,
    password: str = None,
    timeout: int = 15,
) -> tuple[bool, str | None]:
    """
    Проверка работоспособности прокси.

    Args:
        proxy_type: Тип прокси (socks5, http)
        host: Хост прокси
        port: Порт прокси
        username: Логин (опционально)
        password: Пароль (опционально)
        timeout: Таймаут в секундах

    Returns:
        (is_working, ip_address) - кортеж из статуса и IP (или None)
    """
    log.debug(f"🔍 Проверка прокси: {host}:{port} ({proxy_type})")

    connector = None
    try:
        # Создаём коннектор напрямую через параметры, а не from_url().
        # ВАЖНО: aiohttp_socks.ProxyType — SOCKS5=2, HTTP=3 (раньше тут был
        # хардкод 5 для SOCKS5 → ValueError, и любой socks5 считался мёртвым).
        if proxy_type.lower() == 'socks5':
            connector = ProxyConnector(
                proxy_type=ProxyType.SOCKS5,
                host=host,
                port=port,
                username=username if username else None,
                password=password if password else None,
                rdns=True,  # DNS resolution через прокси
            )
        elif proxy_type.lower() == 'http':
            connector = ProxyConnector(
                proxy_type=ProxyType.HTTP,
                host=host,
                port=port,
                username=username if username else None,
                password=password if password else None,
                rdns=False,
            )
        else:
            log.error(f"Неподдерживаемый тип прокси: {proxy_type}")
            return False, None

        # Проверка через ipify.org
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                "https://api.ipify.org?format=json",
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    ip = data.get('ip', 'unknown')
                    log.info(f"✅ Прокси работает. Exit IP: {ip}")
                    return True, ip
                else:
                    log.warning(f"⚠️ Прокси вернул статус {response.status}")
                    return False, None

    except aiohttp.ClientConnectorError as e:
        log.error(f"❌ Ошибка подключения к прокси: {e}")
        return False, None

    except asyncio.TimeoutError:
        log.error(f"❌ Таймаут при проверке прокси ({timeout} сек)")
        return False, None

    except Exception as e:
        log.error(f"❌ Ошибка проверки прокси: {type(e).__name__}: {e}")
        return False, None

    finally:
        if connector:
            try:
                await connector.close()
            except Exception as e:
                log.debug(f"Ошибка при закрытии коннектора: {e}")


async def check_proxy_simple(
    host: str,
    port: int,
    username: str = None,
    password: str = None,
    proxy_type: str = "socks5",
) -> bool:
    """
    Упрощённая проверка — только работоспособность.

    Returns:
        bool: True если прокси работает
    """
    is_working, ip = await check_proxy(
        proxy_type=proxy_type,
        host=host,
        port=port,
        username=username,
        password=password,
    )
    return is_working
