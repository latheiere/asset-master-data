import re
import subprocess
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

from mdv import __version__, build_revision
from mdv.cli import build_parser


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_version_matches_distribution_metadata():
    assert __version__ == version("asset-master-data")


def test_collection_user_agent_uses_release_version():
    for path in (
        ROOT / "src" / "mdv" / "collection.py",
        ROOT / "src" / "mdv" / "bundles.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "AssetMasterData/{__version__}" in source
        assert "AssetMasterData/0.1" not in source


def test_release_disclosure_matches_single_editable_source():
    project_version = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", project_version)
    assert __version__ == project_version
    assert f"current release is `{project_version}`" in (
        ROOT / "README.md"
    ).read_text(encoding="utf-8")
    assert all(
        project_version not in path.read_text(encoding="utf-8")
        for path in (ROOT / "src" / "mdv").rglob("*.py")
    )


def test_systemd_service_injects_deployed_revision():
    template = (
        ROOT / "deploy" / "systemd" / "asset-master-data.service.tpl"
    ).read_text(encoding="utf-8")
    assert "Environment=MDV_GIT_SHA=__GIT_SHA__" in template


def test_release_local_installer_owns_config_and_unit_contract():
    installer = (ROOT / "deploy" / "systemd" / "install_systemd.sh").read_text(
        encoding="utf-8"
    )
    assert 'BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"' in installer
    assert 'PROJECT_DIR="${MDV_PROJECT_DIR:-$BUNDLE_DIR}"' in installer
    assert 'CONFIG_PATH="${MDV_CONFIG_PATH:-$PROJECT_DIR/config/config.yaml}"' in installer
    assert '$BUNDLE_DIR/deploy/systemd/asset-master-data.service.tpl' in installer
    assert '$BUNDLE_DIR/deploy/systemd/asset-master-data.slice.tpl' in installer
    assert "rendered.replace(placeholder, value)" in installer
    assert "unresolved systemd placeholder" in installer
    assert "systemd-analyze verify" in installer
    assert "sed " not in installer


def test_systemd_enforces_aggregate_limited_host_budget():
    api = (ROOT / "deploy/systemd/asset-master-data.service.tpl").read_text()
    collector = (
        ROOT / "deploy/systemd/asset-master-refresh.service.tpl"
    ).read_text()
    aggregate = (
        ROOT / "deploy/systemd/asset-master-data.slice.tpl"
    ).read_text()

    assert "Slice=asset-master-data.slice" in api
    assert "Slice=asset-master-data.slice" in collector
    assert "MemoryMax=288M" in aggregate
    assert "MemoryHigh=224M" in aggregate
    assert "MemoryMax=224M" in api
    assert "MemoryMax=224M" in collector


def test_deploy_requires_clean_annotated_release_tag():
    script = (ROOT / "deploy" / "systemd" / "deploy.sh").read_text(
        encoding="utf-8"
    )
    assert "git status --porcelain --untracked-files=normal" in script
    assert "MDV_DEPLOY_REEXEC=1" in script
    assert "MDV_DEPLOY_LOCKED=1" in script
    assert 'install -d -m 0700 "$BACKUP_DIR" "$LOCK_DIR"' in script
    assert script.index("flock -n 9") < script.index("git pull --ff-only")
    assert script.index("git branch --show-current") < script.index("git pull --ff-only")
    assert script.index(
        "git status --porcelain --untracked-files=normal"
    ) < script.index("git pull --ff-only")
    assert 'git cat-file -t "$RELEASE_TAG"' in script
    assert 'git rev-list -n 1 "$RELEASE_TAG"' in script
    assert "mdv.cli --config config/config.yaml collect" not in script
    assert "chmod -R a-w" in script
    assert "mv -Tf" in script
    assert "wait_for_readiness" in script
    assert "rollback_release" in script
    assert "cleanup_failed_release" in script
    assert 'ACTIVE_BUILD_DIR=""' in script
    assert 'ACTIVE_SOURCE_DIR=""' in script
    assert "trap 'rm -rf" not in script
    assert "SWITCHED=1" in script
    assert "DEPLOY_SUCCEEDED=1" in script
    assert script.rindex("SWITCHED=1") < script.rindex("DEPLOY_SUCCEEDED=1")
    assert script.index("wait_for_readiness") < script.index("DEPLOY_SUCCEEDED=1")
    assert script.count(
        "sudo systemctl is-active --quiet asset-master-data.service"
    ) >= 2
    assert script.count(
        "sudo systemctl is-active --quiet asset-master-refresh.timer"
    ) >= 2
    assert "status != 0 && SWITCHED == 1 && DEPLOY_SUCCEEDED == 0" in script
    assert 'prune_old_releases "$RELEASE_DIR" "$PREVIOUS_RELEASE" ||' in script
    assert "prune_old_backups" in script
    assert "MDV_PRE_PULL_SHA" in script
    assert "ORIG_HEAD" in script
    assert "bootstrap_legacy_release" in script
    assert 'copy_runtime_contract "$PROJECT_DIR" "$PROJECT_DIR" "$build_dir"' in script
    assert 'bash "$release/deploy/systemd/install_systemd.sh"' in script
    assert "MDV_START_COLLECTION_TIMER=0" in script
    assert "quiesce_collection" in script
    assert "resume_collection_schedule" in script
    assert script.index("\nquiesce_collection\n") < script.index(
        'runtime_backup.py" create'
    )
    assert script.index("\nwait_for_readiness\n") < script.index(
        "\nresume_collection_schedule\n"
    )
    assert "collection service became active before deployment commit" in script
    quiesce = script[script.index("quiesce_collection()") :]
    assert quiesce.index("COLLECTION_QUIESCED=1") < quiesce.index(
        "collection service did not quiesce"
    )
    assert 'MDV_CONFIG_PATH="$CURRENT_LINK/config/config.yaml"' in script
    assert '--config "$release/config/config.yaml" doctor --require-ready' in script
    assert script.index('runtime_backup.py" create') < script.index(
        '--config "$NEW_CONFIG" init'
    ) < script.index('switch_current "$RELEASE_DIR"')
    assert script.index("\nprune_old_backups\n") < script.index(
        '--config "$NEW_CONFIG" init'
    )
    assert '--path "$ENTITLEMENTS_PATH"' not in script
    assert '--evidence "$ACTIVE_CONFIG"' in script
    assert '--metadata "runtime_revision=$PREVIOUS_REVISION"' in script
    assert 'runtime_backup.py" verify "$BACKUP_FILE"' not in script
    assert 'if [[ "$DB_PATH" != "$NEW_DB_PATH" ]]' in script
    assert 'template_path="$installer_root/deploy/systemd/$template"' in script
    assert "TIMER_WAS_ENABLED" in script and "TIMER_WAS_ACTIVE" in script
    assert "collection timer did not quiesce" in script
    assert "already active and healthy; no deployment performed" in script
    assert 'wait_for_release_health "$RELEASE_DIR" "$GIT_SHA"' in script
    assert 'wait_for_release_health "$previous" "$previous_revision" || return 1' in script
    assert 'MDV_GIT_SHA="$revision"' in script
    assert "http://127.0.0.1:$port/favicon.ico" in script
    assert 'grep -Fqx "MDV_GIT_SHA=$revision"' in script
    assert 'mktemp -d "$LOCAL_DIR/.current-switch-XXXXXX"' in script
    assert 'if ! ln -s "$target" "$temporary_link"' in script
    assert 'if ! mv -Tf "$temporary_link" "$CURRENT_LINK"' in script
    assert "2 * database_bytes + reserve" in script
    assert "predeploy-[0-9]*.[0-9]*.[0-9]*-*.tar.gz" in script
    assert 'build_dir="$RELEASE_DIR"' in script
    assert 'python3 -m venv "$build_dir/venv"' in script
    assert 'python3 -m venv "$legacy_dir/venv"' in script
    assert '"$build_dir/venv/bin/mdv" --version' in script
    assert '"$legacy_dir/venv/bin/mdv" --version' in script
    assert 'mv "$build_dir" "$RELEASE_DIR"' not in script
    assert 'mv "$source_stage" "$legacy_dir"' not in script


def test_timer_restore_propagates_failures_even_in_conditional_context():
    script = (ROOT / "deploy" / "systemd" / "deploy.sh").read_text(
        encoding="utf-8"
    )
    start = script.index("resume_collection_schedule()")
    end = script.index("\nrollback_release()", start)
    function = script[start:end]
    harness = function + r'''
TIMER_STATE_CAPTURED=1
TIMER_WAS_ENABLED=1
TIMER_WAS_ACTIVE=1
COLLECTION_QUIESCED=1
sudo() {
  if [[ "$*" == "systemctl enable asset-master-refresh.timer" ]]; then
    return 42
  fi
  return 0
}
if resume_collection_schedule; then
  exit 1
fi
[[ "$COLLECTION_QUIESCED" == "1" ]]
'''

    result = subprocess.run(["bash", "-c", harness], check=False)

    assert result.returncode == 0


def test_make_exposes_production_collection_separately_from_deployment():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "collect-prod:" in makefile
    assert ".local/current/venv/bin/python -m mdv.cli --config .local/current/config/config.yaml collect" in makefile
    assert "--path config/entitlements.yaml" not in makefile
    assert "systemctl is-active --quiet asset-master-refresh.timer" in makefile


def test_cli_reports_distribution_version(capsys):
    with pytest.raises(SystemExit, match="0"):
        build_parser().parse_args(["--version"])
    assert capsys.readouterr().out.strip() == f"mdv {__version__}"


def test_build_revision_requires_full_git_sha(monkeypatch):
    monkeypatch.setenv("MDV_GIT_SHA", "ABCDEF0123456789" * 3)
    assert build_revision() == "unknown"
    monkeypatch.setenv("MDV_GIT_SHA", "A" * 40)
    assert build_revision() == "a" * 40
