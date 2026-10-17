"""OAuth access tokens; secret values never enter YAML, logs, or API responses."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
import requests


class AuthenticationError(RuntimeError):
    """Intentionally contains only an operator-safe description."""


def secret_env(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", name):
        raise AuthenticationError("Invalid credential environment reference")
    value = os.environ.get(name, "")
    if not value or value.startswith("${"):
        raise AuthenticationError("Required credential environment variable is absent")
    return value


class TokenProvider:
    """Single-process token cache with encrypted, atomic refresh-token rotation."""

    def __init__(self, data_dir: Path, session=None):
        self.path = Path(data_dir) / "oauth-tokens.json"
        self.session = session or requests.Session()
        self._lock = threading.Lock()
        self._cache = {}

    @staticmethod
    def _endpoint(auth: dict) -> str:
        url = auth.get("token_url", "")
        parts = urlsplit(url)
        google = parts.hostname == "oauth2.googleapis.com" and parts.path == "/token"
        microsoft = parts.hostname == "login.microsoftonline.com" and bool(
            re.fullmatch(r"/[A-Za-z0-9.-]+/oauth2/v2\.0/token", parts.path)
        )
        if (parts.scheme != "https" or parts.port not in (None, 443) or
                parts.username or parts.password or parts.query or parts.fragment or
                not (google or microsoft)):
            raise AuthenticationError("OAuth token endpoint is not an approved provider endpoint")
        return url

    def access_token(self, account: dict) -> str:
        auth = account.get("auth", {})
        if auth.get("access_token_env"):
            return secret_env(auth["access_token_env"])
        with self._lock:
            identity = str(account["id"])
            cached = self._cache.get(identity)
            if cached and cached[1] > time.time() + 60:
                return cached[0]
            endpoint = self._endpoint(auth)
            client_id = secret_env(auth.get("client_id_env"))
            try:
                cipher = Fernet(secret_env("STEPH_TOKEN_ENCRYPTION_KEY").encode())
                saved = json.loads(self.path.read_text()) if self.path.exists() else {}
                binding = "|".join((identity, account.get("username", ""), client_id, endpoint))
                refresh = (cipher.decrypt(saved[binding].encode()).decode()
                           if binding in saved else secret_env(auth.get("refresh_token_env")))
            except (ValueError, InvalidToken, OSError, TypeError):
                raise AuthenticationError("Encrypted OAuth token storage is unavailable") from None
            payload = {"grant_type": "refresh_token", "client_id": client_id,
                       "refresh_token": refresh}
            if auth.get("client_secret_env"):
                payload["client_secret"] = secret_env(auth["client_secret_env"])
            if auth.get("scope"):
                payload["scope"] = auth["scope"]
            try:
                response = self.session.post(endpoint, data=payload, timeout=(10, 20),
                                             allow_redirects=False)
                if response.status_code != 200:
                    raise AuthenticationError("OAuth refresh rejected; reauthorize the account")
                result = response.json()
                token = result["access_token"]
                if not isinstance(token, str) or not token:
                    raise ValueError("missing token")
                expires = max(60, min(int(result.get("expires_in", 3600)), 86400))
                rotated = result.get("refresh_token", refresh)
                if not isinstance(rotated, str) or not rotated:
                    raise ValueError("missing refresh token")
            except (requests.RequestException, ValueError, KeyError, TypeError):
                raise AuthenticationError("OAuth refresh failed; check provider authorization") from None
            saved[binding] = cipher.encrypt(rotated.encode()).decode()
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as handle:
                    json.dump(saved, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
            except OSError:
                raise AuthenticationError("Could not preserve refreshed OAuth credentials") from None
            self._cache[identity] = (token, time.time() + expires)
            return token
