"""Symmetric encryption for credentials.encrypted_key.

Fernet (AES-128-CBC + HMAC-SHA256 via the cryptography package). The key
lives outside Postgres: CREDENTIALS_KEY env, or data/credentials.key
(chmod 600, gitignored). Never log the key or plaintext payloads.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("gguf-router.crypto")

PREFIX = "enc:v1:"
KEY_FILE = Path(__file__).resolve().parent.parent / "data" / "credentials.key"

_fernet: Fernet | None = None


def _load_or_create_key() -> bytes:
    env = os.environ.get("CREDENTIALS_KEY", "").strip().strip('"').strip("'")
    if env:
        return env.encode("ascii")
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.is_file():
        return KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    KEY_FILE.chmod(0o600)
    return key


def reset_for_tests() -> None:
    global _fernet
    _fernet = None


def _fernet_box() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def is_encrypted(blob: str | None) -> bool:
    return isinstance(blob, str) and blob.startswith(PREFIX)


def encrypt_payload(payload: dict) -> str:
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    token = _fernet_box().encrypt(plaintext).decode("ascii")
    return PREFIX + token


def decrypt_blob(blob: str) -> dict:
    if not blob:
        raise ValueError("empty credential blob")
    if is_encrypted(blob):
        token = blob[len(PREFIX) :].encode("ascii")
        try:
            raw = _fernet_box().decrypt(token)
        except InvalidToken as exc:
            raise ValueError("credential decrypt failed") from exc
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("credential payload is not an object")
        return data
    # Legacy plaintext JSON (or a raw password string).
    try:
        data = json.loads(blob)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"password": blob}


def redact(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    out = {}
    for k, v in payload.items():
        if str(k).lower() in {"password", "username", "encrypted_key", "secret", "cookie", "token"}:
            out[k] = "[redacted]"
        else:
            out[k] = v
    return out
