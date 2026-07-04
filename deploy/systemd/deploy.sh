#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

git pull --ff-only origin main
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
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m mdv.cli --config config/config.yaml init
if ! .venv/bin/python -m mdv.cli --config config/config.yaml collect; then
  echo "WARNING: one or more public collection endpoints failed; preserved successful snapshots" >&2
fi
bash deploy/systemd/install_systemd.sh --start-service
sudo systemctl --no-pager --full status asset-master-data.service
