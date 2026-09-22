# CoreBot production queue — журнал приёмки

Оркестратор: `docs/tasks/MUSE-SPARK-1.3-ORCHESTRATOR.md`. Порядок строго `01 → 11`.
База: `083c525`. В commit включаются только файлы текущей задачи; `docs/tasks/` — входная очередь, не продукт.

## 01 — Эксплуатационные контракты и ADR ✅ принята 2026-09-22

- Commit: (см. git log ниже, `docs: task 01 operational contracts + ADR`).
- База исполнения: `083c525d2e0b380953e51b0b592c02956b463690`.
- Изменённые файлы:
  - `docs/operations/INSTANCE_CONTRACT.md` (нов.)
  - `docs/operations/SLO.md` (нов.)
  - `docs/operations/RELEASE_CONTRACT.md` (нов.)
  - `docs/operations/adr/0001-fleet-management.md` (нов.)
  - `docs/operations/CONFIG_CONTRACT.md` (нов., 49 env из `.env.example`)
  - `docs/ARCHITECTURE.md` (+6 строк ссылок, без переписывания)
- Проверки: `git diff --check` чист; grep `TODO|TBD|FIXME|...` пуст;
  «разумный»/«быстрый» отсутствуют; скан секретов (токены/IP/ключи) пуст;
  зафиксированы Python 3.11+, Ubuntu 22.04/24.04, loopback `127.0.0.1:8081`;
  SLO/RPO/RTO числом (99.5 %/99.0 %, простой обновления ≤5 мин / ≤30 мин/мес,
  RPO ≤24 ч, RTO ≤60 мин / ≤4 ч); ADR 0001 выбирает Ansible с control node
  (WSL/Linux), секреты — Vault, без секретов в Git; Bash — примитивы одного
  хоста, центральный агент отклонён.
- Замечания/риски для следующих задач: SLO-цифры — допущения, валидировать
  метриками первых недель (задачи 08/11); задача 07 обязана покрыть
  Vault-политику и инвентарь «хост=пользователь»; миграции forward-only,
  откат = restore бэкапа; `OPENROUTER_KEY_ENCRYPTION_KEY` обязателен при
  ключе в SQLite — не ослаблять.
