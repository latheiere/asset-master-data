#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

git pull --ff-only origin main
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m mdv.cli --config config/config.yaml init
if ! .venv/bin/python -m mdv.cli --config config/config.yaml collect; then
  echo "WARNING: one or more public collection endpoints failed; preserved successful snapshots" >&2
fi
bash deploy/systemd/install_systemd.sh --start-service
sudo systemctl --no-pager --full status asset-master-data.service
