import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

from mdv import __version__, build_revision
from mdv.cli import build_parser


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_version_matches_distribution_metadata():
    assert __version__ == version("asset-master-data")


def test_release_disclosure_matches_single_editable_source():
    project_version = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
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


def test_deploy_requires_clean_annotated_release_tag():
    script = (ROOT / "deploy" / "systemd" / "deploy.sh").read_text(
        encoding="utf-8"
    )
    assert "git status --porcelain --untracked-files=normal" in script
    assert 'git cat-file -t "$RELEASE_TAG"' in script
    assert 'git rev-list -n 1 "$RELEASE_TAG"' in script


def test_cli_reports_distribution_version(capsys):
    with pytest.raises(SystemExit, match="0"):
        build_parser().parse_args(["--version"])
    assert capsys.readouterr().out.strip() == f"mdv {__version__}"


def test_build_revision_requires_full_git_sha(monkeypatch):
    monkeypatch.setenv("MDV_GIT_SHA", "ABCDEF0123456789" * 3)
    assert build_revision() == "unknown"
    monkeypatch.setenv("MDV_GIT_SHA", "A" * 40)
    assert build_revision() == "a" * 40
