#!/usr/bin/env bash
set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_DIR="${MDV_PROJECT_DIR:-$BUNDLE_DIR}"
CONFIG_PATH="${MDV_CONFIG_PATH:-$PROJECT_DIR/config/config.yaml}"
SYSTEMD_DIR="/etc/systemd/system"
CURRENT_USER="$(id -un)"
GIT_SHA="${MDV_GIT_SHA_OVERRIDE:-$(git -C "$PROJECT_DIR" rev-parse --verify HEAD)}"
PYTHON_PATH="${MDV_PYTHON:-$PROJECT_DIR/.venv/bin/python}"
START_SERVICE=false
START_COLLECTION_TIMER="${MDV_START_COLLECTION_TIMER:-1}"

if [[ "${1:-}" == "--start-service" ]]; then
  START_SERVICE=true
elif [[ -n "${1:-}" ]]; then
  echo "usage: $0 [--start-service]" >&2
  exit 2
fi
if [[ "$START_COLLECTION_TIMER" != "0" && "$START_COLLECTION_TIMER" != "1" ]]; then
  echo "MDV_START_COLLECTION_TIMER must be 0 or 1" >&2
  exit 2
fi

if [[ ! -x "$PYTHON_PATH" ]]; then
  echo "missing runtime interpreter: $PYTHON_PATH" >&2
  exit 1
fi
if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "missing configuration: $CONFIG_PATH" >&2
  exit 1
fi
ENTITLEMENTS_PATH="$("$PYTHON_PATH" -m mdv.cli --config "$CONFIG_PATH" config-value auth.entitlements_path)"
DB_PATH="$("$PYTHON_PATH" -m mdv.cli --config "$CONFIG_PATH" config-value database.path)"
if [[ "$ENTITLEMENTS_PATH" != /* ]]; then
  ENTITLEMENTS_PATH="$PROJECT_DIR/$ENTITLEMENTS_PATH"
fi
if [[ "$DB_PATH" != /* ]]; then
  DB_PATH="$PROJECT_DIR/$DB_PATH"
fi
"$PYTHON_PATH" - "$PROJECT_DIR" "$CONFIG_PATH" "$PYTHON_PATH" \
  "$DB_PATH" "$ENTITLEMENTS_PATH" <<'PY'
import re
import sys

for value in sys.argv[1:]:
    if not re.fullmatch(r"/(?:[A-Za-z0-9._@:+&|=-]+/)*[A-Za-z0-9._@:+&|=-]*", value):
        raise SystemExit(f"runtime path is not absolute or contains unsupported characters: {value!r}")
    if re.search(r"__[A-Z][A-Z0-9_]*__", value):
        raise SystemExit(f"runtime path contains a placeholder token: {value!r}")
PY
DATA_DIR="$(dirname "$DB_PATH")"
if [[ ! -f "$ENTITLEMENTS_PATH" ]]; then
  echo "missing entitlements: $ENTITLEMENTS_PATH" >&2
  exit 1
fi
if [[ ! "$GIT_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "unable to determine deployed Git revision" >&2
  exit 1
fi

COLLECTION_SCHEDULE="$("$PYTHON_PATH" -m mdv.cli --config "$CONFIG_PATH" config-value collection.schedule)"
if [[ "$COLLECTION_SCHEDULE" == *$'\n'* || "$COLLECTION_SCHEDULE" == *$'\r'* ]]; then
  echo "collection.schedule contains a newline" >&2
  exit 1
fi
systemd-analyze calendar "$COLLECTION_SCHEDULE" >/dev/null

mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"
find "$DATA_DIR" -maxdepth 1 -type f -name "$(basename "$DB_PATH")*" -exec chmod 600 {} +
chmod 600 "$ENTITLEMENTS_PATH"

RENDER_DIR="$(mktemp -d)"
cleanup_render_dir() {
  rm -rf -- "$RENDER_DIR"
}
trap cleanup_render_dir EXIT

render_unit() {
  local template_path="$1"
  local output_name="$2"
  "$PYTHON_PATH" - "$template_path" "$RENDER_DIR/$output_name" \
    "$PROJECT_DIR" "$CONFIG_PATH" "$PYTHON_PATH" "$DATA_DIR" \
    "$CURRENT_USER" "$GIT_SHA" "$COLLECTION_SCHEDULE" <<'PY'
import pathlib
import re
import sys

template, output, *values = sys.argv[1:]
placeholders = (
    "__PROJECT_DIR__",
    "__CONFIG_PATH__",
    "__PYTHON__",
    "__DATA_DIR__",
    "__USER__",
    "__GIT_SHA__",
    "__ON_CALENDAR__",
)
for value in values:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SystemExit("unit replacement contains a control character")
    if re.search(r"__[A-Z][A-Z0-9_]*__", value):
        raise SystemExit("unit replacement contains a placeholder token")
rendered = pathlib.Path(template).read_text(encoding="utf-8")
for placeholder, value in zip(placeholders, values, strict=True):
    rendered = rendered.replace(placeholder, value)
unresolved = sorted(set(re.findall(r"__[A-Z][A-Z0-9_]*__", rendered)))
if unresolved:
    raise SystemExit(f"unresolved systemd placeholder(s): {', '.join(unresolved)}")
pathlib.Path(output).write_text(rendered, encoding="utf-8")
PY
}

render_unit "$BUNDLE_DIR/deploy/systemd/asset-master-data.slice.tpl" "asset-master-data.slice"
render_unit "$BUNDLE_DIR/deploy/systemd/asset-master-data.service.tpl" "asset-master-data.service"
render_unit "$BUNDLE_DIR/deploy/systemd/asset-master-refresh.service.tpl" "asset-master-refresh.service"
render_unit "$BUNDLE_DIR/deploy/systemd/asset-master-refresh.timer.tpl" "asset-master-refresh.timer"
systemd-analyze verify \
  "$RENDER_DIR/asset-master-data.slice" \
  "$RENDER_DIR/asset-master-data.service" \
  "$RENDER_DIR/asset-master-refresh.service" \
  "$RENDER_DIR/asset-master-refresh.timer" >/dev/null
for unit in \
  asset-master-data.slice \
  asset-master-data.service \
  asset-master-refresh.service \
  asset-master-refresh.timer; do
  sudo install -m 0644 "$RENDER_DIR/$unit" "$SYSTEMD_DIR/$unit"
  echo "Installed $unit"
done
cleanup_render_dir
trap - EXIT

sudo systemctl daemon-reload
sudo systemctl enable asset-master-data.service
sudo systemctl enable asset-master-refresh.timer
if [[ "$START_COLLECTION_TIMER" == "1" ]]; then
  sudo systemctl start asset-master-refresh.timer
else
  sudo systemctl stop asset-master-refresh.timer
fi
if [[ "$START_SERVICE" == true ]]; then
  sudo systemctl restart asset-master-data.service
  echo "Started asset-master-data.service"
else
  echo "Prepared asset-master-data.service without starting it"
fi
sudo systemctl --no-pager list-timers asset-master-refresh.timer
