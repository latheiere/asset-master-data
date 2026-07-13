from __future__ import annotations

import yaml
import pytest

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
                },
                "collection": {
                    "http_timeout_seconds": 12.5,
                    "max_concurrent_fetches": 3,
                    "stale_after_seconds": 120,
                    "readiness_max_age_seconds": 240,
                    "schedule": "Mon *-*-* 01:02:03 UTC",
                },
                "audit": {
                    "unchanged_observation_retention_days": 14,
                    "changed_payload_retention_days": 5,
                    "max_retained_observations_per_table": 1234,
                },
                "auth": {
                    "entitlements_path": "/tmp/entitlements.yaml",
                    "session_cookie_name": "test_session",
                    "session_ttl_seconds": 99,
                    "session_cookie_secure": True,
                    "max_concurrent_hashes": 1,
                    "failed_attempt_limit": 4,
                    "failed_attempt_window_seconds": 30,
                },
                "integration": {"token_info_url": "https://tokens.example.test/"},
            }
        ),
        encoding="utf-8",
    )

    settings = Settings.from_yaml(config)

    assert str(settings.db_path) == "/tmp/mdv-test.sqlite3"
    assert settings.host == "127.0.0.2"
    assert settings.port == 9000
    assert settings.http_timeout_seconds == 12.5
    assert settings.max_concurrent_fetches == 3
    assert settings.collection_stale_after_seconds == 120
    assert settings.collection_readiness_max_age_seconds == 240
    assert settings.unchanged_observation_retention_days == 14
    assert settings.changed_payload_retention_days == 5
    assert settings.max_retained_observations_per_table == 1234
    assert settings.collection_schedule == "Mon *-*-* 01:02:03 UTC"
    assert str(settings.entitlements_path) == "/tmp/entitlements.yaml"
    assert settings.session_cookie_name == "test_session"
    assert settings.session_ttl_seconds == 99
    assert settings.session_cookie_secure is True
    assert settings.auth_max_concurrent_hashes == 1
    assert settings.auth_failed_attempt_limit == 4
    assert settings.auth_failed_attempt_window_seconds == 30
    assert settings.token_info_url == "https://tokens.example.test"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"server": {"port": 65_536}}, "server.port must be at most 65535"),
        (
            {"collection": {"http_timeout_seconds": float("nan")}},
            "collection.http_timeout_seconds must be finite",
        ),
        (
            {"collection": {"http_timeout_seconds": float("inf")}},
            "collection.http_timeout_seconds must be finite",
        ),
        (
            {"collection": {"http_timeout_seconds": 301}},
            "collection.http_timeout_seconds must be at most 300",
        ),
        (
            {"collection": {"max_concurrent_fetches": 33}},
            "collection.max_concurrent_fetches must be at most 32",
        ),
        (
            {"audit": {"max_retained_observations_per_table": 1_000_001}},
            "audit.max_retained_observations_per_table must be at most 1000000",
        ),
        (
            {"auth": {"max_concurrent_hashes": 9}},
            "auth.max_concurrent_hashes must be at most 8",
        ),
        (
            {"auth": {"failed_attempt_limit": 101}},
            "auth.failed_attempt_limit must be at most 100",
        ),
        (
            {"auth": {"failed_attempt_window_seconds": 3601}},
            "auth.failed_attempt_window_seconds must be at most 3600",
        ),
    ],
)
def test_settings_reject_nonfinite_or_host_unsafe_limits(tmp_path, payload, message):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        Settings.from_yaml(config)


def test_password_hash_and_signed_session_are_fail_closed(tmp_path):
    password_hash = hash_password("correct-password")
    assert verify_password("correct-password", password_hash)
    assert not verify_password("incorrect-password", password_hash)

    path = tmp_path / "entitlements.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "session_secret": "session-secret-with-more-than-32-characters",
                "users": {
                    "api": {"password_hash": password_hash, "role": "reader"}
                },
            }
        ),
        encoding="utf-8",
    )
    entitlements = Entitlements.load(path)

    token = entitlements.issue_session("api", 60)
    assert entitlements.authenticate("api", "correct-password")
    assert not entitlements.authenticate("api", "incorrect-password")
    assert entitlements.role("api") == "reader"
    assert entitlements.session_username(token) == "api"
    assert entitlements.session_username(token + "tampered") is None


def test_password_verification_rejects_unbounded_scrypt_parameters():
    password_hash = hash_password("password")
    assert not verify_password("password", password_hash.replace("$16384$", "$32768$"))
