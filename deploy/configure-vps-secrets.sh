#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="/etc/reading-manager"
ENV_FILE="${CONFIG_DIR}/reading-manager.env"

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Run this script as root.\n' >&2
  exit 2
fi

read_hidden() {
  local variable_name="$1"
  local prompt="$2"
  local value
  read -r -s -p "${prompt}" value
  printf '\n'
  printf -v "${variable_name}" '%s' "${value}"
}

read_hidden weread_api_key "WeRead API key: "
read_hidden llm_api_key "Model API key: "
read_hidden lark_base_token "Lark Base token: "
read_hidden lark_base_url "Lark Base URL (optional): "
read_hidden lark_table_books "Lark Books table ID: "
read_hidden lark_table_reading_records "Lark reading records table ID: "
read_hidden lark_table_notes "Lark notes table ID: "
read_hidden lark_table_daily_stats "Lark daily stats table ID: "
read_hidden lark_table_tags "Lark tags table ID: "
read_hidden lark_table_sync_runs "Lark sync runs table ID: "

if [[ -z "${weread_api_key}" ||
      -z "${llm_api_key}" ||
      -z "${lark_base_token}" ||
      -z "${lark_table_books}" ||
      -z "${lark_table_reading_records}" ||
      -z "${lark_table_notes}" ||
      -z "${lark_table_daily_stats}" ||
      -z "${lark_table_tags}" ||
      -z "${lark_table_sync_runs}" ]]; then
  printf 'API keys, the Base token, and all table IDs are required.\n' >&2
  exit 2
fi
if [[ "${weread_api_key}" != wrk-* ]]; then
  printf 'The WeRead API key must start with wrk-.\n' >&2
  exit 2
fi

install -d -m 0700 -o root -g root "${CONFIG_DIR}"
umask 077
temporary_file="$(mktemp "${CONFIG_DIR}/.reading-manager.env.XXXXXX")"
cleanup() {
  rm -f "${temporary_file}"
  unset \
    weread_api_key \
    llm_api_key \
    lark_base_token \
    lark_base_url \
    lark_table_books \
    lark_table_reading_records \
    lark_table_notes \
    lark_table_daily_stats \
    lark_table_tags \
    lark_table_sync_runs
}
trap cleanup EXIT

{
  printf 'WEREAD_API_KEY=%s\n' "${weread_api_key}"
  printf 'LLM_API_KEY=%s\n' "${llm_api_key}"
  printf 'LLM_BASE_URL=https://api.deepseek.com\n'
  printf 'LLM_MODEL=deepseek-v4-flash\n'
  printf 'LLM_TIMEOUT_SECONDS=45\n'
  printf 'LLM_MAX_ATTEMPTS=3\n'
  printf 'LARK_IDENTITY=user\n'
  printf 'LARK_BASE_TOKEN=%s\n' "${lark_base_token}"
  printf 'LARK_BASE_URL=%s\n' "${lark_base_url}"
  printf 'LARK_TABLE_BOOKS=%s\n' "${lark_table_books}"
  printf 'LARK_TABLE_READING_RECORDS=%s\n' "${lark_table_reading_records}"
  printf 'LARK_TABLE_NOTES=%s\n' "${lark_table_notes}"
  printf 'LARK_TABLE_DAILY_STATS=%s\n' "${lark_table_daily_stats}"
  printf 'LARK_TABLE_TAGS=%s\n' "${lark_table_tags}"
  printf 'LARK_TABLE_SYNC_RUNS=%s\n' "${lark_table_sync_runs}"
  printf 'READING_MANAGER_LARK_NOTIFY_USER_ID=\n'
  printf 'READING_MANAGER_LARK_NOTIFY_CHAT_ID=\n'
  printf 'PYTHONDONTWRITEBYTECODE=1\n'
} >"${temporary_file}"

chown root:root "${temporary_file}"
chmod 0600 "${temporary_file}"
mv -f "${temporary_file}" "${ENV_FILE}"
trap - EXIT
unset \
  weread_api_key \
  llm_api_key \
  lark_base_token \
  lark_base_url \
  lark_table_books \
  lark_table_reading_records \
  lark_table_notes \
  lark_table_daily_stats \
  lark_table_tags \
  lark_table_sync_runs

printf 'Secrets written to %s with mode 0600.\n' "${ENV_FILE}"
