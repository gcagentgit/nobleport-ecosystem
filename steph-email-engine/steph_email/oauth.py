"""OAuth2 refresh-token exchange for Gmail / Microsoft 365 (XOAUTH2).

Adapted from NoblePort MailHub.

The engine never runs the browser consent step itself; you supply a refresh token
obtained once from the provider's OAuth playground / Azure app registration
and the engine keeps exchanging it for short-lived access tokens.
"""

from __future__ import annotations

import base64
import time

import httpx


class TokenCache:
    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        item = self._tokens.get(key)
        if item and item[1] > time.time() + 60:
            return item[0]
        return None

    def put(self, key: str, token: str, expires_in: int) -> None:
        self._tokens[key] = (token, time.time() + int(expires_in))


_cache = TokenCache()


def fetch_access_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    scope: str = "",
    *,
    cache_key: str | None = None,
    timeout: float = 20.0,
) -> str:
    key = cache_key or f"{token_url}|{client_id}|{refresh_token[-12:]}"
    cached = _cache.get(key)
    if cached:
        return cached
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if scope:
        data["scope"] = scope
    resp = httpx.post(token_url, data=data, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"oauth token refresh failed ({resp.status_code}): {resp.text[:300]}")
    body = resp.json()
    token = body["access_token"]
    _cache.put(key, token, body.get("expires_in", 3600))
    return token


def xoauth2_string(user: str, access_token: str) -> str:
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


def xoauth2_b64(user: str, access_token: str) -> str:
    return base64.b64encode(xoauth2_string(user, access_token).encode()).decode()
