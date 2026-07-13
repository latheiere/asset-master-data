#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

if [[ "${MDV_DEPLOY_REEXEC:-0}" != "1" ]]; then
  git pull --ff-only origin main
  exec env MDV_DEPLOY_REEXEC=1 bash "$PROJECT_DIR/deploy/systemd/deploy.sh"
fi
git fetch --tags origin
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "deployment requires a clean Git worktree" >&2
  exit 1
fi
VERSION="$(python3 -c 'import pathlib,tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
if [[ ! "$VERSION" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "project.version must be a final Semantic Version: $VERSION" >&2
  exit 1
fi
RELEASE_TAG="v$VERSION"
if [[ "$(git cat-file -t "$RELEASE_TAG" 2>/dev/null || true)" != "tag" ]]; then
  echo "deployment requires annotated release tag $RELEASE_TAG" >&2
  exit 1
fi
if [[ "$(git rev-list -n 1 "$RELEASE_TAG")" != "$(git rev-parse HEAD)" ]]; then
  echo "release tag $RELEASE_TAG does not identify current main HEAD" >&2
  exit 1
fi
echo "Deploying $RELEASE_TAG ($(git rev-parse HEAD))"
BACKUP_DIR="$PROJECT_DIR/.local/backups"
BACKUP_FILE="$BACKUP_DIR/predeploy-$VERSION-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
mkdir -p "$BACKUP_DIR"
python3 scripts/runtime_backup.py create \
  --output "$BACKUP_FILE" \
  --sqlite .data/mdv.sqlite3 \
  --path config/config.yaml \
  --path config/entitlements.yaml
python3 scripts/runtime_backup.py verify "$BACKUP_FILE"
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps .
.venv/bin/python -m mdv.cli --config config/config.yaml init
bash deploy/systemd/install_systemd.sh --start-service
sudo systemctl --no-pager --full status asset-master-data.service
