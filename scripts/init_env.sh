#!/usr/bin/env bash
set -euo pipefail

# Быстрая инициализация .env из .env.example:
# - копирует шаблон, если .env отсутствует
# - просит ввести BOT_TOKEN и OWNER_ID
# - проверяет, что API_ID/API_HASH уже заданы (общий Telegram app)
#
# Запуск:
#   bash scripts/init_env.sh
#   bash scripts/init_env.sh /opt/corebot/app/.env

ENV_PATH="${1:-.env}"
EXAMPLE_PATH=".env.example"

if [[ ! -f "$EXAMPLE_PATH" ]]; then
  echo "❌ Не найден $EXAMPLE_PATH (запустите из корня репозитория)." >&2
  exit 1
fi

if [[ ! -f "$ENV_PATH" ]]; then
  cp "$EXAMPLE_PATH" "$ENV_PATH"
  echo "✅ Создан $ENV_PATH из $EXAMPLE_PATH"
else
  echo "ℹ️ Файл $ENV_PATH уже существует, обновим только BOT_TOKEN/OWNER_ID."
fi

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

get_value() {
  local key="$1"
  local file="$2"
  local line
  line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
  line="${line#*=}"
  trim "$line"
}

set_value() {
  local key="$1"
  local val="$2"
  local file="$3"
  if grep -q -E "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$file"
  else
    printf '\n%s=%s\n' "$key" "$val" >> "$file"
  fi
}

api_id="$(get_value API_ID "$ENV_PATH")"
api_hash="$(get_value API_HASH "$ENV_PATH")"
if [[ -z "$api_id" || -z "$api_hash" ]]; then
  echo "⚠️ API_ID/API_HASH пустые в $ENV_PATH."
  echo "   Вставьте общий Telegram app (один на команду), затем повторите запуск."
  exit 1
fi

default_bot_token="$(get_value BOT_TOKEN "$ENV_PATH")"
default_owner_id="$(get_value OWNER_ID "$ENV_PATH")"

read -r -p "Введите BOT_TOKEN: " bot_token
bot_token="$(trim "$bot_token")"
if [[ -z "$bot_token" ]]; then
  bot_token="$default_bot_token"
fi

read -r -p "Введите OWNER_ID: " owner_id
owner_id="$(trim "$owner_id")"
if [[ -z "$owner_id" ]]; then
  owner_id="$default_owner_id"
fi

if [[ -z "$bot_token" || -z "$owner_id" ]]; then
  echo "❌ BOT_TOKEN и OWNER_ID обязательны." >&2
  exit 1
fi

set_value BOT_TOKEN "$bot_token" "$ENV_PATH"
set_value OWNER_ID "$owner_id" "$ENV_PATH"

chmod 600 "$ENV_PATH" || true
echo "✅ $ENV_PATH готов."
echo "   Проверка: API_ID/API_HASH общие, BOT_TOKEN/OWNER_ID установлены."
