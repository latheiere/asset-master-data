from __future__ import annotations

import yaml

from mdv.auth import Entitlements, hash_password, verify_password
from mdv.config import Settings


def test_settings_load_every_runtime_value_from_yaml(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "database": {"path": "/tmp/mdv-test.sqlite3"},
                "server": {
                    "host": "127.0.0.2",
                    "port": 9000,
                    "refresh_on_startup": "never",
                },
                "collection": {
                    "http_timeout_seconds": 12.5,
                    "schedule": "Mon *-*-* 01:02:03 UTC",
                },
                "auth": {
                    "entitlements_path": "/tmp/entitlements.yaml",
                    "session_cookie_name": "test_session",
                    "session_ttl_seconds": 99,
                    "session_cookie_secure": True,
                },
            }
        ),
        encoding="utf-8",
    )

    settings = Settings.from_yaml(config)

    assert str(settings.db_path) == "/tmp/mdv-test.sqlite3"
    assert settings.host == "127.0.0.2"
    assert settings.port == 9000
    assert settings.refresh_on_startup == "never"
    assert settings.http_timeout_seconds == 12.5
    assert settings.collection_schedule == "Mon *-*-* 01:02:03 UTC"
    assert str(settings.entitlements_path) == "/tmp/entitlements.yaml"
    assert settings.session_cookie_name == "test_session"
    assert settings.session_ttl_seconds == 99
    assert settings.session_cookie_secure is True


def test_password_hash_and_signed_session_are_fail_closed(tmp_path):
    password_hash = hash_password("correct-password")
    assert verify_password("correct-password", password_hash)
    assert not verify_password("incorrect-password", password_hash)

    path = tmp_path / "entitlements.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "session_secret": "session-secret-with-more-than-32-characters",
                "users": {"api": {"password_hash": password_hash}},
            }
        ),
        encoding="utf-8",
    )
    entitlements = Entitlements.load(path)

    token = entitlements.issue_session("api", 60)
    assert entitlements.authenticate("api", "correct-password")
    assert not entitlements.authenticate("api", "incorrect-password")
    assert entitlements.session_username(token) == "api"
    assert entitlements.session_username(token + "tampered") is None
