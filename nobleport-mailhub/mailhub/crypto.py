"""Secrets at rest.

Passwords and OAuth refresh tokens are encrypted with Fernet (AES-128-CBC +
HMAC).  The key comes from ``MAILHUB_SECRET_KEY`` or is generated once into
``<data_dir>/secret.key`` with mode 0600 so a stolen SQLite file alone reveals
nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: bytes):
        self._f = Fernet(key)

    @classmethod
    def load(cls, data_dir: Path, env_key: str | None = None) -> "SecretBox":
        if env_key:
            return cls(env_key.encode())
        key_path = data_dir / "secret.key"
        if key_path.exists():
            return cls(key_path.read_bytes().strip())
        data_dir.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return cls(key)

    def encrypt(self, plain: str) -> str:
        return self._f.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("cannot decrypt secret: wrong MAILHUB_SECRET_KEY or corrupted store") from exc
