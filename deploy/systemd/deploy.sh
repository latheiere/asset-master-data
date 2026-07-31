#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOCAL_DIR="$PROJECT_DIR/.local"
RELEASES_DIR="$LOCAL_DIR/releases"
CURRENT_LINK="$LOCAL_DIR/current"
BACKUP_DIR="$LOCAL_DIR/backups"
LOCK_DIR="$LOCAL_DIR/locks"
cd "$PROJECT_DIR"

install -d -m 0700 "$BACKUP_DIR" "$LOCK_DIR"
if ! command -v flock >/dev/null 2>&1; then
  echo "deployment requires flock" >&2
  exit 1
fi
if [[ "${MDV_DEPLOY_LOCKED:-0}" != "1" ]]; then
  exec 9>"$LOCK_DIR/deploy.lock"
  chmod 0600 "$LOCK_DIR/deploy.lock"
  if ! flock -n 9; then
    echo "another deployment is already running" >&2
    exit 1
  fi
else
  if ! { true >&9; } 2>/dev/null; then
    echo "deployment lock descriptor was not inherited" >&2
    exit 1
  fi
fi

if [[ "${MDV_DEPLOY_REEXEC:-0}" != "1" ]]; then
  if [[ "$(git branch --show-current)" != "main" ]]; then
    echo "deployment requires the main branch" >&2
    exit 1
  fi
  if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
    echo "deployment requires a clean Git worktree" >&2
    exit 1
  fi
  PRE_PULL_SHA="$(git rev-parse --verify HEAD)"
  git pull --ff-only origin main
  exec env MDV_DEPLOY_REEXEC=1 MDV_DEPLOY_LOCKED=1 \
    MDV_PRE_PULL_SHA="$PRE_PULL_SHA" \
    bash "$PROJECT_DIR/deploy/systemd/deploy.sh"
fi
git fetch --tags origin
if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "deployment requires the main branch" >&2
  exit 1
fi
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
  echo "deployment requires main HEAD to match origin/main" >&2
  exit 1
fi
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

GIT_SHA="$(git rev-parse HEAD)"
RELEASE_ID="$RELEASE_TAG-${GIT_SHA:0:12}"
RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
BACKUP_FILE="$BACKUP_DIR/asset-master-data-runtime.tar.gz"
mkdir -p "$RELEASES_DIR"
BUILT_NEW_RELEASE=0
SWITCHED=0
DEPLOY_SUCCEEDED=0
ACTIVE_BUILD_DIR=""
ACTIVE_SOURCE_DIR=""
COLLECTION_QUIESCED=0
CUTOVER_STARTED=0
STATE_MUTATED=0
TIMER_STATE_CAPTURED=0
TIMER_WAS_ENABLED=0
TIMER_WAS_ACTIVE=0

cleanup_failed_release() {
  local status=$?
  trap - EXIT
  if [[ -n "$ACTIVE_BUILD_DIR" && -d "$ACTIVE_BUILD_DIR" ]]; then
    chmod -R u+w "$ACTIVE_BUILD_DIR" 2>/dev/null || true
    rm -rf -- "$ACTIVE_BUILD_DIR"
  fi
  if [[ -n "$ACTIVE_SOURCE_DIR" && -d "$ACTIVE_SOURCE_DIR" ]]; then
    rm -rf -- "$ACTIVE_SOURCE_DIR"
  fi
  if (( status != 0 && CUTOVER_STARTED == 1 && DEPLOY_SUCCEEDED == 0 )); then
    if ! rollback_release "$PREVIOUS_RELEASE"; then
      echo "automatic rollback failed; operator intervention is required" >&2
    fi
  elif (( status != 0 && COLLECTION_QUIESCED == 1 )); then
    if ! resume_collection_schedule; then
      echo "failed to restore the collection timer after deployment failure" >&2
    fi
  fi
  if (( status != 0 && BUILT_NEW_RELEASE == 1 )) && [[ -d "$RELEASE_DIR" ]]; then
    local active_release=""
    if [[ -L "$CURRENT_LINK" ]]; then
      active_release="$(readlink -f "$CURRENT_LINK")"
    fi
    if [[ "$active_release" != "$RELEASE_DIR" ]]; then
      chmod -R u+w "$RELEASE_DIR"
      rm -rf -- "$RELEASE_DIR"
      echo "Removed failed inactive release $RELEASE_ID" >&2
    fi
  fi
  exit "$status"
}
trap cleanup_failed_release EXIT

release_complete() {
  local release="$1"
  local revision="$2"
  local version="$3"
  [[ -x "$release/venv/bin/python" ]] && \
    [[ -f "$release/config/config.yaml" ]] && \
    [[ -x "$release/deploy/systemd/install_systemd.sh" ]] && \
    [[ -f "$release/deploy/systemd/asset-master-data.slice.tpl" ]] && \
    [[ -f "$release/deploy/systemd/asset-master-data.service.tpl" ]] && \
    [[ -f "$release/deploy/systemd/asset-master-refresh.service.tpl" ]] && \
    [[ -f "$release/deploy/systemd/asset-master-refresh.timer.tpl" ]] && \
    [[ "$(<"$release/REVISION")" == "$revision" ]] && \
    [[ "$(<"$release/VERSION")" == "$version" ]]
}

copy_runtime_contract() {
  local source_root="$1"
  local installer_root="$2"
  local destination="$3"
  local template template_path
  install -D -m 0644 "$source_root/config/config.yaml" \
    "$destination/config/config.yaml"
  install -D -m 0755 "$installer_root/deploy/systemd/install_systemd.sh" \
    "$destination/deploy/systemd/install_systemd.sh"
  for template in \
    asset-master-data.slice.tpl \
    asset-master-data.service.tpl \
    asset-master-refresh.service.tpl \
    asset-master-refresh.timer.tpl; do
    template_path="$source_root/deploy/systemd/$template"
    if [[ ! -f "$template_path" && "$template" == "asset-master-data.slice.tpl" ]]; then
      # The first immutable deployment upgrades a legacy revision that did not
      # yet carry the aggregate slice. The current installer requires it, so
      # supply only that new resource contract while preserving all legacy
      # service/timer templates for a faithful rollback runtime.
      template_path="$installer_root/deploy/systemd/$template"
    fi
    if [[ ! -f "$template_path" ]]; then
      echo "missing runtime template: $template_path" >&2
      return 1
    fi
    mkdir -p "$destination/deploy/systemd"
    sed 's|__PROJECT_DIR__/.venv/bin/python|__PYTHON__|g' \
      "$template_path" \
      > "$destination/deploy/systemd/$template"
    chmod 0644 "$destination/deploy/systemd/$template"
  done
}

build_release() {
  local build_dir
  if [[ -d "$RELEASE_DIR" ]]; then
    if ! release_complete "$RELEASE_DIR" "$GIT_SHA" "$VERSION"; then
      echo "existing release directory is incomplete or does not match: $RELEASE_DIR" >&2
      return 1
    fi
    echo "Reusing immutable release $RELEASE_ID"
    return
  fi
  # Console-script shebangs created by venv are absolute. Build directly at
  # the final inactive release path so the sealed environment is never moved.
  # Atomicity is provided by the later current-symlink switch; the EXIT trap
  # removes this directory if any build or validation step fails.
  build_dir="$RELEASE_DIR"
  install -d -m 0700 "$build_dir"
  ACTIVE_BUILD_DIR="$build_dir"
  BUILT_NEW_RELEASE=1
  python3 -m venv "$build_dir/venv"
  "$build_dir/venv/bin/pip" install --require-hashes -r requirements.lock
  "$build_dir/venv/bin/pip" install --no-deps "$PROJECT_DIR"
  if [[ "$("$build_dir/venv/bin/python" -c 'import mdv; print(mdv.__version__)')" != "$VERSION" ]]; then
    echo "built package version does not match $VERSION" >&2
    return 1
  fi
  copy_runtime_contract "$PROJECT_DIR" "$PROJECT_DIR" "$build_dir"
  printf '%s\n' "$VERSION" > "$build_dir/VERSION"
  printf '%s\n' "$GIT_SHA" > "$build_dir/REVISION"
  if [[ "$("$build_dir/venv/bin/mdv" --version)" != "mdv $VERSION" ]]; then
    echo "installed mdv entrypoint is not executable at its final release path" >&2
    return 1
  fi
  chmod -R a-w "$build_dir"
  ACTIVE_BUILD_DIR=""
  echo "Built immutable release $RELEASE_ID"
}

bootstrap_legacy_release() {
  local revision="$1"
  local source_stage source_root legacy_version legacy_id legacy_dir
  if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]] || \
    ! git cat-file -e "$revision^{commit}" 2>/dev/null || \
    [[ "$revision" == "$GIT_SHA" ]]; then
    echo "first immutable deploy requires a distinct, valid pre-pull revision" >&2
    return 1
  fi
  source_stage="$(mktemp -d "$RELEASES_DIR/.legacy-source-XXXXXX")"
  ACTIVE_SOURCE_DIR="$source_stage"
  source_root="$source_stage/source"
  mkdir -p "$source_root"
  git archive "$revision" | tar -x -C "$source_root"
  legacy_version="$(python3 -c 'import pathlib,tomllib,sys; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text())["project"]["version"])' "$source_root/pyproject.toml")"
  legacy_id="v$legacy_version-${revision:0:12}"
  legacy_dir="$RELEASES_DIR/$legacy_id"
  if [[ -d "$legacy_dir" ]]; then
    if ! release_complete "$legacy_dir" "$revision" "$legacy_version"; then
      echo "existing legacy release is incomplete: $legacy_dir" >&2
      return 1
    fi
    BOOTSTRAP_RELEASE="$legacy_dir"
    rm -rf -- "$source_stage"
    ACTIVE_SOURCE_DIR=""
    echo "Reusing legacy rollback release $legacy_id"
    return
  fi
  install -d -m 0700 "$legacy_dir"
  ACTIVE_BUILD_DIR="$legacy_dir"
  python3 -m venv "$legacy_dir/venv"
  "$legacy_dir/venv/bin/pip" install --require-hashes \
    -r "$source_root/requirements.lock"
  "$legacy_dir/venv/bin/pip" install --no-deps "$source_root"
  if [[ "$("$legacy_dir/venv/bin/python" -c 'import mdv; print(mdv.__version__)')" != "$legacy_version" ]]; then
    echo "legacy package version does not match $legacy_version" >&2
    return 1
  fi
  # The current release-local installer understands stable runtime paths. Old
  # templates are preserved except for normalizing their mutable .venv command
  # to the release interpreter placeholder.
  copy_runtime_contract "$source_root" "$PROJECT_DIR" "$legacy_dir"
  rm -rf -- "$source_stage"
  ACTIVE_SOURCE_DIR=""
  printf '%s\n' "$legacy_version" > "$legacy_dir/VERSION"
  printf '%s\n' "$revision" > "$legacy_dir/REVISION"
  if [[ "$("$legacy_dir/venv/bin/mdv" --version)" != "mdv $legacy_version" ]]; then
    echo "legacy mdv entrypoint is not executable at its final release path" >&2
    return 1
  fi
  chmod -R a-w "$legacy_dir"
  ACTIVE_BUILD_DIR=""
  BOOTSTRAP_RELEASE="$legacy_dir"
  echo "Built legacy rollback release $legacy_id"
}

absolute_path() {
  local path="$1"
  if [[ "$path" = /* ]]; then
    printf '%s\n' "$path"
  else
    printf '%s\n' "$PROJECT_DIR/$path"
  fi
}

switch_current() {
  local target="$1"
  local temporary_dir temporary_link
  temporary_dir="$(mktemp -d "$LOCAL_DIR/.current-switch-XXXXXX")" || return 1
  temporary_link="$temporary_dir/current"
  if ! ln -s "$target" "$temporary_link"; then
    rmdir "$temporary_dir" || true
    return 1
  fi
  if ! mv -Tf "$temporary_link" "$CURRENT_LINK"; then
    rm -f -- "$temporary_link"
    rmdir "$temporary_dir" || true
    return 1
  fi
  rmdir "$temporary_dir" || true
  return 0
}

install_release() {
  local release="$1"
  local revision="$2"
  MDV_PYTHON="$CURRENT_LINK/venv/bin/python" \
    MDV_PROJECT_DIR="$PROJECT_DIR" \
  MDV_CONFIG_PATH="$CURRENT_LINK/config/config.yaml" \
  MDV_GIT_SHA_OVERRIDE="$revision" \
  MDV_START_COLLECTION_TIMER=0 \
    bash "$release/deploy/systemd/install_systemd.sh" --start-service || return 1
  sudo systemctl is-active --quiet asset-master-data.service || return 1
  if sudo systemctl is-active --quiet asset-master-refresh.timer; then
    echo "collection timer unexpectedly active during release validation" >&2
    return 1
  fi
}

running_revision_matches() {
  local revision="$1"
  local main_pid
  main_pid="$(sudo systemctl show -p MainPID --value asset-master-data.service)" || \
    return 1
  [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] || return 1
  sudo cat "/proc/$main_pid/environ" | tr '\0' '\n' | \
    grep -Fqx "MDV_GIT_SHA=$revision"
}

wait_for_release_health() {
  local release="$1"
  local revision="$2"
  local version port output attempt doctor_required=0 doctor_ok
  version="$(<"$release/VERSION")" || return 1
  port="$("$release/venv/bin/python" -m mdv.cli \
    --config "$release/config/config.yaml" config-value server.port)" || return 1
  output="$LOCAL_DIR/doctor-$(basename "$release").json"
  if python3 - "$version" <<'PY'
import sys
parts = tuple(int(part) for part in sys.argv[1].split("."))
raise SystemExit(0 if parts >= (0, 12, 0) else 1)
PY
  then
    doctor_required=1
  fi
  for attempt in $(seq 1 30); do
    doctor_ok=1
    if (( doctor_required == 1 )); then
      if ! MDV_GIT_SHA="$revision" "$release/venv/bin/python" -m mdv.cli \
        --config "$release/config/config.yaml" doctor --require-ready \
        > "$output"; then
        doctor_ok=0
      fi
    fi
    if (( doctor_ok == 1 )) && \
      sudo systemctl is-active --quiet asset-master-data.service && \
      running_revision_matches "$revision" && \
      "$release/venv/bin/python" -c \
        'import sys,urllib.request; response=urllib.request.urlopen(sys.argv[1], timeout=2); raise SystemExit(0 if response.status in (200, 204) else 1)' \
        "http://127.0.0.1:$port/favicon.ico"; then
      rm -f -- "$output"
      return 0
    fi
    sleep 2
  done
  echo "Release health check failed for $(basename "$release")" >&2
  test ! -f "$output" || cat "$output" >&2
  return 1
}

quiesce_collection() {
  if (( TIMER_STATE_CAPTURED == 0 )); then
    if [[ "$(sudo systemctl show -p LoadState --value asset-master-refresh.timer)" != "loaded" ]]; then
      echo "collection timer is not loaded" >&2
      return 1
    fi
    if sudo systemctl is-enabled --quiet asset-master-refresh.timer; then
      TIMER_WAS_ENABLED=1
    fi
    if sudo systemctl is-active --quiet asset-master-refresh.timer; then
      TIMER_WAS_ACTIVE=1
    fi
    TIMER_STATE_CAPTURED=1
  fi
  # From this point every failure path must restore scheduling, even if the
  # in-flight service refuses to stop cleanly.
  COLLECTION_QUIESCED=1
  if ! sudo systemctl stop asset-master-refresh.timer; then
    echo "collection timer could not be stopped" >&2
    return 1
  fi
  if sudo systemctl is-active --quiet asset-master-refresh.timer; then
    echo "collection timer did not quiesce" >&2
    return 1
  fi
  if ! sudo systemctl stop asset-master-refresh.service; then
    echo "collection service could not be stopped" >&2
    return 1
  fi
  if sudo systemctl is-active --quiet asset-master-refresh.service; then
    echo "collection service did not quiesce" >&2
    return 1
  fi
}

resume_collection_schedule() {
  if (( TIMER_STATE_CAPTURED == 0 )); then
    echo "collection timer state was not captured" >&2
    return 1
  fi
  if (( TIMER_WAS_ENABLED == 1 )); then
    sudo systemctl enable asset-master-refresh.timer || return 1
  else
    sudo systemctl disable asset-master-refresh.timer || return 1
  fi
  if (( TIMER_WAS_ACTIVE == 1 )); then
    sudo systemctl start asset-master-refresh.timer || return 1
    sudo systemctl is-active --quiet asset-master-refresh.timer || return 1
  else
    sudo systemctl stop asset-master-refresh.timer || return 1
    if sudo systemctl is-active --quiet asset-master-refresh.timer; then
      echo "collection timer unexpectedly active after state restoration" >&2
      return 1
    fi
  fi
  COLLECTION_QUIESCED=0
}

rollback_release() {
  local previous="$1"
  sudo systemctl stop asset-master-refresh.timer || return 1
  sudo systemctl stop asset-master-refresh.service || return 1
  sudo systemctl stop asset-master-data.service || return 1
  COLLECTION_QUIESCED=1
  if [[ -z "$previous" || ! -x "$previous/venv/bin/python" ]]; then
    echo "No previous immutable release is available; stopping the failed service" >&2
    return 1
  fi
  local previous_revision previous_version previous_config previous_database
  local previous_state previous_database_name
  previous_revision="$(<"$previous/REVISION")" || return 1
  previous_version="$(<"$previous/VERSION")" || return 1
  previous_config="$previous/config/config.yaml"
  previous_database="$(absolute_path "$("$previous/venv/bin/python" -m mdv.cli --config "$previous_config" config-value database.path)")" || return 1
  previous_state="$(dirname "$previous_database")"
  previous_database_name="$(basename "$previous_database")"
  echo "Deployment failed; rolling back to $(basename "$previous")" >&2
  if (( STATE_MUTATED == 1 )) && [[ "$previous_database" == "$NEW_DB_PATH" ]]; then
    "$NEW_PYTHON" -m mdv.runtime_backup restore \
      "$BACKUP_FILE" \
      --target "$previous_state" \
      --replace \
      --release "$(basename "$previous")" \
      --revision "$previous_revision" \
      --version "$previous_version" \
      --configuration "$previous_config" || return 1
    if [[ ! -f "$previous_state/$previous_database_name" ]]; then
      echo "StateCrate restore did not recover the selected release database" >&2
      return 1
    fi
  fi
  switch_current "$previous" || return 1
  install_release "$previous" "$previous_revision" || return 1
  wait_for_release_health "$previous" "$previous_revision" || return 1
  resume_collection_schedule || return 1
  SWITCHED=0
  CUTOVER_STARTED=0
  STATE_MUTATED=0
  return 0
}

wait_for_readiness() {
  wait_for_release_health "$CURRENT_LINK" "$GIT_SHA"
}

prune_old_releases() {
  local current="$1"
  local rollback="$2"
  local kept_extra=0
  local release
  local -a releases
  mapfile -t releases < <(
    find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -name 'v*' \
      -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-
  )
  for release in "${releases[@]}"; do
    if [[ "$release" == "$current" || "$release" == "$rollback" ]]; then
      continue
    fi
    if (( kept_extra == 0 )); then
      kept_extra=1
      continue
    fi
    chmod -R u+w "$release" || return 1
    rm -rf -- "$release" || return 1
    echo "Pruned old immutable release $(basename "$release")"
  done
}

echo "Preparing $RELEASE_TAG ($GIT_SHA)"
if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK")"
  if [[ -z "$PREVIOUS_RELEASE" || ! -d "$PREVIOUS_RELEASE" ]]; then
    echo "runtime current symlink is broken: $CURRENT_LINK" >&2
    exit 1
  fi
  PREVIOUS_REVISION="$(<"$PREVIOUS_RELEASE/REVISION")"
  PREVIOUS_VERSION="$(<"$PREVIOUS_RELEASE/VERSION")"
  if ! release_complete "$PREVIOUS_RELEASE" "$PREVIOUS_REVISION" "$PREVIOUS_VERSION"; then
    echo "current immutable release is incomplete: $PREVIOUS_RELEASE" >&2
    exit 1
  fi
elif [[ -e "$CURRENT_LINK" ]]; then
  echo "runtime current path exists but is not a symlink: $CURRENT_LINK" >&2
  exit 1
else
  LEGACY_SHA="${MDV_PRE_PULL_SHA:-}"
  if [[ ! "$LEGACY_SHA" =~ ^[0-9a-f]{40}$ ]] || [[ "$LEGACY_SHA" == "$GIT_SHA" ]]; then
    LEGACY_SHA="$(git rev-parse --verify ORIG_HEAD 2>/dev/null || true)"
  fi
  BOOTSTRAP_RELEASE=""
  bootstrap_legacy_release "$LEGACY_SHA"
  PREVIOUS_RELEASE="$BOOTSTRAP_RELEASE"
fi
PREVIOUS_REVISION="$(<"$PREVIOUS_RELEASE/REVISION")"
PREVIOUS_VERSION="$(<"$PREVIOUS_RELEASE/VERSION")"
if [[ "$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)" == "$RELEASE_DIR" ]]; then
  if ! release_complete "$RELEASE_DIR" "$GIT_SHA" "$VERSION"; then
    echo "active release is incomplete: $RELEASE_DIR" >&2
    exit 1
  fi
  wait_for_release_health "$RELEASE_DIR" "$GIT_SHA"
  DEPLOY_SUCCEEDED=1
  echo "Release $RELEASE_ID is already active and healthy; no deployment performed"
  exit 0
fi
build_release
NEW_PYTHON="$RELEASE_DIR/venv/bin/python"
NEW_CONFIG="$RELEASE_DIR/config/config.yaml"
ACTIVE_PYTHON="$PREVIOUS_RELEASE/venv/bin/python"
ACTIVE_CONFIG="$PREVIOUS_RELEASE/config/config.yaml"
DB_PATH="$(absolute_path "$("$ACTIVE_PYTHON" -m mdv.cli --config "$ACTIVE_CONFIG" config-value database.path)")"
NEW_DB_PATH="$(absolute_path "$("$NEW_PYTHON" -m mdv.cli --config "$NEW_CONFIG" config-value database.path)")"
STATE_DIR="$(dirname "$NEW_DB_PATH")"
DATABASE_NAME="$(basename "$NEW_DB_PATH")"
if [[ "$STATE_DIR" != "$LOCAL_DIR/state" || "$DATABASE_NAME" != "mdv.sqlite3" ]]; then
  echo "release configuration must use the dedicated runtime state path: $LOCAL_DIR/state/mdv.sqlite3" >&2
  exit 1
fi
if [[ "$DB_PATH" != "$NEW_DB_PATH" && "$DB_PATH" != "$PROJECT_DIR/.data/mdv.sqlite3" ]]; then
  echo "unsupported legacy database path for offline migration: $DB_PATH" >&2
  exit 1
fi
if [[ ! -f "$DB_PATH" ]]; then
  echo "active runtime database does not exist: $DB_PATH" >&2
  exit 1
fi

# Prevent a pre-release collector from committing old projection logic after
# the migration or after the new API has declared itself ready.
quiesce_collection

# State migration and schema migration are offline. From this point every
# failure restarts the selected previous immutable release; when both releases
# share the state path, rollback first asks StateCrate to replace the complete
# directory from the stable predeploy archive.
CUTOVER_STARTED=1
sudo systemctl stop asset-master-data.service
if sudo systemctl is-active --quiet asset-master-data.service; then
  echo "asset master API did not quiesce" >&2
  exit 1
fi

# Every deploy replaces one stable signed archive with a consistent snapshot of
# the active database. The signed manifest identifies the immutable release and
# configuration; configuration is recovered from that release, not the archive.
# Entitlements contain a session secret and must be backed up separately with
# encryption rather than placed in this unencrypted runtime archive.
"$NEW_PYTHON" -m mdv.runtime_backup create \
  --output "$BACKUP_FILE" \
  --state-dir "$(dirname "$DB_PATH")" \
  --database-name "$(basename "$DB_PATH")" \
  --release "$(basename "$PREVIOUS_RELEASE")" \
  --revision "$PREVIOUS_REVISION" \
  --version "$PREVIOUS_VERSION" \
  --configuration "$ACTIVE_CONFIG"

if [[ "$DB_PATH" != "$NEW_DB_PATH" ]]; then
  echo "Migrating the legacy database into the dedicated runtime state directory"
  restore_arguments=(
    "$BACKUP_FILE"
    --target "$STATE_DIR"
    --release "$(basename "$PREVIOUS_RELEASE")"
    --revision "$PREVIOUS_REVISION"
    --version "$PREVIOUS_VERSION"
    --configuration "$ACTIVE_CONFIG"
  )
  if [[ -e "$STATE_DIR" ]]; then
    restore_arguments+=(--replace)
  fi
  STATE_MUTATED=1
  "$NEW_PYTHON" -m mdv.runtime_backup restore \
    "${restore_arguments[@]}"
fi

STATE_MUTATED=1
"$NEW_PYTHON" -m mdv.cli --config "$NEW_CONFIG" init

switch_current "$RELEASE_DIR"
SWITCHED=1
install_release "$RELEASE_DIR" "$GIT_SHA"
wait_for_readiness

# This is the deployment commit point. Re-check both long-lived units after
# readiness so any failure up to here is handled by the EXIT rollback trap.
sudo systemctl is-active --quiet asset-master-data.service
if sudo systemctl is-active --quiet asset-master-refresh.service; then
  echo "collection service became active before deployment commit" >&2
  exit 1
fi
resume_collection_schedule
DEPLOY_SUCCEEDED=1

prune_old_releases "$RELEASE_DIR" "$PREVIOUS_RELEASE" || \
  echo "warning: unable to prune old immutable releases" >&2
sudo systemctl --no-pager --full status asset-master-data.service || \
  echo "warning: unable to render final service status" >&2
echo "Deployed $RELEASE_ID"
echo "Verified backup: $BACKUP_FILE"
