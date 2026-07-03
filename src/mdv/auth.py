from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import yaml


SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1


def _derived_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )


@dataclass(frozen=True)
class Entitlements:
    session_secret: bytes
    users: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "Entitlements":
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise RuntimeError(f"entitlements file does not exist: {path}") from exc
        except yaml.YAMLError as exc:
            raise RuntimeError(f"invalid entitlements YAML in {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("entitlements root must be a mapping")
        secret = str(payload.get("session_secret", "")).strip()
        users = payload.get("users")
        if len(secret) < 32:
            raise RuntimeError("entitlements session_secret must contain at least 32 characters")
        if not isinstance(users, dict) or not users:
            raise RuntimeError("entitlements must define at least one user")
        normalized: dict[str, str] = {}
        for username, record in users.items():
            if not isinstance(record, dict):
                raise RuntimeError(f"entitlement for {username!r} must be a mapping")
            password_hash = str(record.get("password_hash", "")).strip()
            if not str(username).strip() or not password_hash:
                raise RuntimeError("every entitlement requires a username and password_hash")
            normalized[str(username)] = password_hash
        return cls(session_secret=secret.encode("utf-8"), users=normalized)

    def authenticate(self, username: str, password: str) -> bool:
        expected = self.users.get(username)
        if expected is None:
            # Perform comparable work for unknown users to reduce timing leakage.
            verify_password(password, DUMMY_PASSWORD_HASH)
            return False
        return verify_password(password, expected)

    def issue_session(self, username: str, ttl_seconds: int) -> str:
        expires = int(time.time()) + ttl_seconds
        payload = f"{username}\n{expires}".encode("utf-8")
        signature = hmac.new(self.session_secret, payload, hashlib.sha256).digest()
        return _b64(payload + signature)

    def session_username(self, token: str) -> str | None:
        try:
            decoded = _unb64(token)
            payload, signature = decoded[:-32], decoded[-32:]
            expected = hmac.new(self.session_secret, payload, hashlib.sha256).digest()
            username, expires_raw = payload.decode("utf-8").split("\n", 1)
            expires = int(expires_raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not hmac.compare_digest(signature, expected) or expires < int(time.time()):
            return None
        return username if username in self.users else None


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = _derived_hash(password, salt)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_unb64(expected)),
        )
        return hmac.compare_digest(derived, _unb64(expected))
    except (ValueError, TypeError):
        return False


def basic_credentials(header: str | None) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(None, 1)[1], validate=True).decode("utf-8")
        if ":" not in decoded:
            return None
        username, password = decoded.split(":", 1)
        return username, password
    except (ValueError, UnicodeDecodeError):
        return None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


DUMMY_PASSWORD_HASH = (
    f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(b'\0' * 16)}$"
    f"{_b64(_derived_hash('invalid-password', b'\0' * 16))}"
)
