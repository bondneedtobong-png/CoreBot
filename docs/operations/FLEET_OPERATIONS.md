# Fleet operations (task 07, ADR 0001: Ansible с control node)

Единственная инструкция по управлению парком VPS. RUNBOOK остаётся источником
истины для шагов на одном хосте; здесь — только fleet-слой поверх него.
Второго оркестратора нет: Bash-скрипты — примитивы одного хоста, Ansible —
оркестрация поверх них (вызывает, а не дублирует логику обновлений).

Модель: один пользователь — один VPS — один inventory-хост. Секреты — только
в локальном Vault-файле, никогда в Git. Реальных IP/токенов в этом документе
нет — только синтетические `TEST-...` примеры.

## 1) Control node (Windows → WSL/Ubuntu)

Ansible не ставится на Windows напрямую. Control node — WSL2/Ubuntu (или
отдельный Linux-хост) оператора:

```powershell
wsl --install -d Ubuntu
wsl --exec bash -lc "sudo apt update && sudo apt install -y ansible git python3"
wsl --exec bash -lc "ansible --version"
```

Рабочая копия репозитория доступна из WSL через `/mnt/c/...`, либо склонируйте
репозиторий внутри WSL. Все команды ниже выполняются внутри WSL из каталога
`ops/ansible`.

Проверка без Ansible (этот репозиторий, Windows): YAML-синтаксис — `python -c`
с PyYAML, логика rollout — `pytest tests/test_fleet_automation.py`. Реальных
VPS нет — валидация идёт через mock/local-симуляцию, к VPS не подключаться.

## 2) Vault: создание локального файла секретов

Пароль Vault хранится ТОЛЬКО локально (файл вне репозитория или менеджер
секретов). Сам vault-файл — в `.gitignore`, в Git лежит только schema
(`group_vars/all/vault.yml.example` с плейсхолдерами `VAULT:...`).

```bash
cd ops/ansible
# 1. Файл пароля ВНЕ репозитория, права 600:
mkdir -p ~/.ansible && chmod 700 ~/.ansible
openssl rand -base64 32 > ~/.ansible/vault-password-file
chmod 600 ~/.ansible/vault-password-file
# 2. Создать локальный vault-файл из schema-примера:
cp group_vars/all/vault.yml.example group_vars/all/vault.yml
ansible-vault encrypt group_vars/all/vault.yml
# 3. Заполнить: ansible-vault edit group_vars/all/vault.yml
```

Проверка host keys ВКЛ (`ansible.cfg`: `host_key_checking = True`) — не
отключать. Первый SSH-контакт подтверждает отпечаток вручную.

## 3) Добавление нового человека за один проход

Шаги (пример для инстанса `corebot-client-ivan`):

1. Получить от человека: `BOT_TOKEN`, `OWNER_ID`, SSH-доступ (IP/порт/user).
2. Внести карточку инстанса (RUNBOOK §3): tenant, ip, user, owner, версия, статус.
3. Добавить хост в локальный `inventory/hosts.yml` (скопировать из
   `hosts.yml.example`, секретов сюда НЕ писать — только несекретное):

```yaml
corebot-client-ivan:
  ansible_host: 203.0.113.10        # реальный IP — только в локальном файле
  ansible_user: deploy
  ansible_port: 22
  instance_name: client-ivan
  corebot_target_tag: v0.1.0
  corebot_target_sha: ""
  parser_embedded: "1"
  cp_enabled: true
  owner_id: "100000007"
  corebot_app_dir: /opt/corebot/app
  corebot_database_url: sqlite+aiosqlite:////opt/corebot/app/data/corebot-ivan.db
  corebot_bot_database_url: sqlite:////opt/corebot/app/data/corebot-ivan.db
  corebot_cp_database_url: sqlite:////opt/corebot/app/data/control-plane-ivan.db
```

4. Добавить секреты инстанса в локальный Vault (`ansible-vault edit`):

```yaml
vault_instances:
  corebot-client-ivan:
    bot_token: "<BOT_TOKEN человека>"
    cp_jwt_secret: "<openssl rand -hex 32>"
    cp_bootstrap_admin_password: "<сложный пароль 12+>"
    cp_agent_token: ""
    openrouter_api_key: ""
```

5. Dry-run, затем install, затем verify (один проход):

```bash
ansible-playbook playbooks/install.yml --limit corebot-client-ivan --check --ask-vault-pass
ansible-playbook playbooks/install.yml --limit corebot-client-ivan --ask-vault-pass
ansible-playbook playbooks/verify.yml --limit corebot-client-ivan
```

6. Проверить `/start` с аккаунта владельца, записать версию/дату в карточку.

## 4) Canary rollout обновлений

Порядок: canary 1 хост → батчи 50%. Любой провал ОСТАНАВЛИВАЕТ очередь
(`serial: [1, 50%]`, `max_fail_percentage: 0`, `any_errors_fatal: true`):

```bash
# 0. Указать pinned SHA в inventory/group_vars (никогда "latest master").
# 1. Canary на один VPS:
ansible-playbook playbooks/update.yml --limit corebot-test-alpha --ask-vault-pass
# 2. Только после зелёного canary — весь парк:
ansible-playbook playbooks/update.yml --ask-vault-pass
# 3. Аудит без изменений:
ansible-playbook playbooks/verify.yml --ask-vault-pass
```

Движок обновления на хосте — `scripts/update_corebot.sh --sha <sha>`
(preflight → backup → stage → deps → stop/swap cp→bot → readiness +
version-паритет → success | авторолбэк, exit 0/2/3/4). Ansible его вызывает,
а не копирует. Итоговый отчёт каждого прогона (без секретов): версия
до/после, backup-путь, service status, readiness, ошибки —
`fleet_report_dir` (`/tmp/corebot-fleet-reports/<host>-<play>.txt`).

## 5) Backup / rollback команды

```bash
ansible-playbook playbooks/backup.yml --ask-vault-pass                 # весь парк
ansible-playbook playbooks/backup.yml --limit corebot-test-alpha       # один хост
ansible-playbook playbooks/rollback.yml --limit corebot-test-alpha \
  -e rollback_prev_sha=<prev-sha12> \
  -e rollback_backup_archive=/opt/corebot/backups/corebot-<stamp>.tar.gz
```

Откат: предыдущий код + restore `.env`/`data/` из предобновленческого бэкапа
(порт `step_rollback` 1:1 — для случая exit 4, когда повторный вызов скрипта
небезопасен) → рестарт cp→bot → health-gate → запись `.deployed_sha`.

## 6) Что запрещено

- Коммитить `inventory/hosts.yml`, `group_vars/all/vault.yml`, пароль Vault,
  `.env`, `data/`, отчёты с секретами (всё это в `.gitignore`).
- Класть секреты в inventory/group_vars в открытом виде (только Vault).
- Отключать `host_key_checking`, открывать `8081` наружу, биндить CP на
  `0.0.0.0`, запускать два parser-loop (`PARSER_EMBEDDED` ровно `0`/`1`).
- Обновлять парк мимо canary или на "latest master" без pinned SHA/тега.
