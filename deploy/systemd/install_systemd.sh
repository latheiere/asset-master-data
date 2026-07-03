#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_PATH="$PROJECT_DIR/config/config.yaml"
SYSTEMD_DIR="/etc/systemd/system"
CURRENT_USER="$(id -un)"
START_SERVICE=false

if [[ "${1:-}" == "--start-service" ]]; then
  START_SERVICE=true
elif [[ -n "${1:-}" ]]; then
  echo "usage: $0 [--start-service]" >&2
  exit 2
fi

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "missing virtual environment; run make install first" >&2
  exit 1
fi
if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "missing configuration: $CONFIG_PATH" >&2
  exit 1
fi
if [[ ! -f "$PROJECT_DIR/config/entitlements.yaml" ]]; then
  echo "missing entitlements: $PROJECT_DIR/config/entitlements.yaml" >&2
  exit 1
fi

COLLECTION_SCHEDULE="$($PROJECT_DIR/.venv/bin/python -m mdv.cli --config "$CONFIG_PATH" config-value collection.schedule)"
if [[ "$COLLECTION_SCHEDULE" == *$'\n'* || "$COLLECTION_SCHEDULE" == *'|'* ]]; then
  echo "collection.schedule contains unsupported characters" >&2
  exit 1
fi
systemd-analyze calendar "$COLLECTION_SCHEDULE" >/dev/null

mkdir -p "$PROJECT_DIR/.data"
chmod 700 "$PROJECT_DIR/.data"
find "$PROJECT_DIR/.data" -maxdepth 1 -type f -name 'mdv.sqlite3*' -exec chmod 600 {} +
chmod 600 "$PROJECT_DIR/config/entitlements.yaml"

install_unit() {
  local template_path="$1"
  local output_name="$2"
  local temporary
  temporary="$(mktemp)"
  sed \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__CONFIG_PATH__|$CONFIG_PATH|g" \
    -e "s|__USER__|$CURRENT_USER|g" \
    -e "s|__ON_CALENDAR__|$COLLECTION_SCHEDULE|g" \
    "$template_path" > "$temporary"
  sudo install -m 0644 "$temporary" "$SYSTEMD_DIR/$output_name"
  rm -f "$temporary"
  echo "Installed $output_name"
}

install_unit "$PROJECT_DIR/deploy/systemd/asset-master-data.service.tpl" "asset-master-data.service"
install_unit "$PROJECT_DIR/deploy/systemd/asset-master-refresh.service.tpl" "asset-master-refresh.service"
install_unit "$PROJECT_DIR/deploy/systemd/asset-master-refresh.timer.tpl" "asset-master-refresh.timer"

sudo systemctl daemon-reload
sudo systemctl enable asset-master-data.service
sudo systemctl enable --now asset-master-refresh.timer
if [[ "$START_SERVICE" == true ]]; then
  sudo systemctl restart asset-master-data.service
  echo "Started asset-master-data.service"
else
  echo "Prepared asset-master-data.service without starting it"
fi
sudo systemctl --no-pager list-timers asset-master-refresh.timer
