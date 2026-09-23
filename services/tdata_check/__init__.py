"""Backend-проверка TData через обязательный TDATA_CHECK proxy pool (задача 12).

Доменные правила (контракт результата и proxy-purpose зафиксированы здесь):

* Проверка НЕ создаёт ``Account``, НЕ использует ``Account.proxy_id`` и НЕ
  пишет сессии в production ``data/sessions`` — временные артефакты живут
  только в системном tmp и удаляются гарантированно (``try/finally``).
* ЛЮБОЙ ``TelegramClient.connect()/get_me()/is_user_authorized()`` во время
  проверки обязан получить proxy из TDATA_CHECK pool. Пустой pool или
  недоступный proxy → ``proxy_required``/``proxy_failed``; прямой коннект
  запрещён (покрыто тестом через factory-spy).
* Error details проходят через ``control_plane.services.sanitize``: никаких
  токенов, proxy-паролей, session-путей и полных TG-response.

Лимиты (числами, см. также :mod:`services.tdata_check.limits`):

* размер архива: 200 МБ (``TDATA_CHECK_MAX_ARCHIVE_BYTES``);
* max папок из ZIP: 20 (``TDATA_CHECK_MAX_FOLDERS``, остаток отмечается
  ``truncated=True``);
* concurrency: 3 (``TDATA_CHECK_MAX_CONCURRENCY``);
* timeout Telethon connect/auth: 25 с (``TDATA_CHECK_CONNECT_TIMEOUT_SEC``);
* SpamBot-check timeout: 15 с, только через тот же proxy, без сообщений
  пользователям (по умолчанию выключен — включается явным флагом);
* retention tmp: 0 — каталог проверки удаляется сразу после run;
* FloodWait: НЕ ретраится автоматически → статус ``flood_wait`` с
  ``retry_after`` секундами; повтор — только новым запуском после паузы.

Sync/job-выбор: SYNC bounded + pollable run store. Обоснование: при
``MAX_FOLDERS=20`` и ``concurrency=3`` худшее время run — десятки секунд
(Telethon auth ~ секунды на папку), что укладывается в бюджет
операторского HTTP-запроса; фоновый job-воркер (по модели задачи 03)
не окупается на таких объёмах. POST выполняет проверку синхронно,
сохраняет результат в in-memory store и возвращает ``run_id``;
GET ``/business/tdata/check/{run_id}`` — polling. Если лимиты вырастут
(>50 папок / минуты auth), выносить в background job — зафиксировано
как follow-up, контракт результата при этом не меняется.
"""

from services.tdata_check.models import (
    CHECK_STATUSES,
    TDataCheckItem,
    TDataCheckRun,
)

__all__ = ["CHECK_STATUSES", "TDataCheckItem", "TDataCheckRun"]
