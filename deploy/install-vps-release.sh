#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/opt/reading-manager"
RELEASES_DIR="${APP_ROOT}/releases"
TOOLS_DIR="${APP_ROOT}/tools"
STATE_DIR="/var/lib/reading-manager"
CONFIG_DIR="/etc/reading-manager"
LARK_CLI="${TOOLS_DIR}/node_modules/.bin/lark-cli"
LARK_CLI_VERSION="1.0.44"

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Run this installer as root.\n' >&2
  exit 2
fi

if [[ "$#" -ne 2 ]]; then
  printf 'Usage: %s <release-archive.tar.gz> <git-sha>\n' "$0" >&2
  exit 2
fi

archive="$1"
git_sha="$2"
if [[ ! -f "${archive}" ]]; then
  printf 'Release archive not found: %s\n' "${archive}" >&2
  exit 2
fi
if [[ ! "${git_sha}" =~ ^[0-9a-f]{7,40}$ ]]; then
  printf 'Git SHA must contain 7-40 lowercase hexadecimal characters.\n' >&2
  exit 2
fi

if ! getent group reading-manager >/dev/null; then
  groupadd --system reading-manager
fi
if ! id -u reading-manager >/dev/null 2>&1; then
  useradd \
    --system \
    --gid reading-manager \
    --home-dir "${STATE_DIR}" \
    --shell /usr/sbin/nologin \
    reading-manager
fi

install -d -m 0755 -o root -g root "${APP_ROOT}" "${RELEASES_DIR}" "${TOOLS_DIR}"
install -d -m 0700 -o reading-manager -g reading-manager "${STATE_DIR}"
install -d -m 0700 -o root -g root "${CONFIG_DIR}"
chown reading-manager:reading-manager "${STATE_DIR}"
chmod 0700 "${STATE_DIR}"
runuser -u reading-manager -- touch "${STATE_DIR}/.write-check"
rm -f "${STATE_DIR}/.write-check"

venv_probe="$(mktemp -d)"
cleanup_probe() {
  rm -rf "${venv_probe}"
}
trap cleanup_probe EXIT
if ! python3 -m venv "${venv_probe}/venv" >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
  python3 -m venv "${venv_probe}/venv"
fi
cleanup_probe
trap - EXIT

npm install \
  --prefix "${TOOLS_DIR}" \
  --no-audit \
  --no-fund \
  "@larksuite/cli@1.0.44"
lark_version="$("${LARK_CLI}" --version)"
if [[ "${lark_version}" != *"${LARK_CLI_VERSION}"* ]]; then
  printf 'Unexpected lark-cli version: %s\n' "${lark_version}" >&2
  exit 3
fi

release_dir="${RELEASES_DIR}/${git_sha}"
if [[ ! -f "${release_dir}/.installed" ]]; then
  if [[ -e "${release_dir}" ]]; then
    failed_release="${release_dir}.failed.$(date +%Y%m%d%H%M%S)"
    mv "${release_dir}" "${failed_release}"
    printf 'Moved incomplete release to %s\n' "${failed_release}" >&2
  fi
  install -d -m 0755 -o root -g root "${release_dir}"
  tar -xzf "${archive}" -C "${release_dir}"
  python3 -m venv "${release_dir}/.venv"
  PIP_CONFIG_FILE=/dev/null \
  PIP_INDEX_URL=https://pypi.org/simple \
  "${release_dir}/.venv/bin/python" -m pip install \
    --disable-pip-version-check \
    "${release_dir}"
  PYTHONPATH="${release_dir}/src" \
    "${release_dir}/.venv/bin/python" -m unittest discover \
      -s "${release_dir}/tests" \
      -v
  chown -R root:root "${release_dir}"
  chmod -R go-w "${release_dir}"
  chmod 0755 "${release_dir}"
  runuser -u reading-manager -- env HOME="${STATE_DIR}" \
    "${release_dir}/.venv/bin/reading-manager" doctor >/dev/null
  touch "${release_dir}/.installed"
fi

install -m 0644 \
  "${release_dir}/deploy/systemd/reading-manager-daily.service" \
  /etc/systemd/system/reading-manager-daily.service
install -m 0644 \
  "${release_dir}/deploy/systemd/reading-manager-daily.timer" \
  /etc/systemd/system/reading-manager-daily.timer

next_link="${APP_ROOT}/.current-${git_sha}"
ln -sfn "${release_dir}" "${next_link}"
mv -Tf "${next_link}" "${APP_ROOT}/current"
systemctl daemon-reload

printf 'Installed release %s at %s\n' "${git_sha}" "${release_dir}"
printf 'The timer remains disabled until credentials and Lark authorization are verified.\n'
