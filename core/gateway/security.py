"""Security controls for the gateway — authentication, CORS, sanitization.

Every control in this module is fail-closed and configuration-driven. None of
them has an insecure default, and none can be switched off by an environment
variable. If the configuration is incomplete or unsafe, `validate()` raises
and the application refuses to boot — in every environment, not just
production.

  1. Signing key    JWT_SECRET (HS*) or JWT_PUBLIC_KEY / OIDC_JWK (RS*, PS*,
                    ES*, EdDSA) is REQUIRED. There is no built-in key, and
                    well-known placeholder values are rejected.
  2. Audience       Every token must carry `aud` == JWT_AUDIENCE. Audience
                    verification is always on.
  3. Issuer         Every token must carry `iss` == JWT_ISSUER. Issuer
                    verification is always on.
  4. CORS           FRONTEND_ORIGIN is an explicit allowlist. "*" (or any
                    wildcard) is rejected. Empty means no cross-origin access
                    in development and a boot failure in production, where
                    every origin must also be https.
  5. Endpoint auth  Every route except the liveness/readiness probes requires
                    a bearer token with the right scope; the upload endpoint
                    included. Invocation is further bound to the agent named
                    in the token — a token issued to GCagent.ai cannot invoke
                    as Stephanie.ai.

Plus the request sanitizer: Unicode-normalizes and strips control, format,
zero-width and bidi-override characters from every string in the envelope so
the governance claim screen cannot be evaded with invisible characters, and
caps payload depth and size.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = logging.getLogger("nobleport.gateway.security")

# --- Scopes ----------------------------------------------------------------
SCOPE_INVOKE = "gateway:invoke"
SCOPE_READ = "gateway:read"
SCOPE_UPLOAD = "gateway:upload"
SCOPE_ADMIN = "gateway:admin"
ALL_SCOPES = frozenset({SCOPE_INVOKE, SCOPE_READ, SCOPE_UPLOAD, SCOPE_ADMIN})

# --- Key policy ------------------------------------------------------------
MIN_SECRET_CHARS = 32
# Placeholder values that must never be accepted as a signing key, whatever
# their length. The first one is the exact value the defensive review found.
FORBIDDEN_SECRETS = frozenset({
    "insecure-dev-key", "change-me", "changeme", "secret", "password", "dev",
    "development", "test", "jwt_secret", "your-secret-here", "replace-me",
})
_HMAC_ALGS = frozenset({"HS256", "HS384", "HS512"})
_RSA_ALGS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512"})
_EC_ALGS = frozenset({"ES256", "ES256K", "ES384", "ES512"})
_OKP_ALGS = frozenset({"EdDSA"})
_ASYMMETRIC_ALGS = _RSA_ALGS | _EC_ALGS | _OKP_ALGS
REQUIRED_CLAIMS = ("sub", "aud", "iss", "exp", "iat")


class SecurityConfigError(RuntimeError):
    """Raised at boot when the security configuration is missing or unsafe."""


class AuthError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SanitizeError(ValueError):
    pass


# ===========================================================================
# 1-3. Signing key, audience, issuer
# ===========================================================================
def _detect_asymmetric_algs(key: Any) -> frozenset[str]:
    """Which algorithm family a loaded public key belongs to."""
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, ed448, rsa

    if isinstance(key, rsa.RSAPublicKey):
        return _RSA_ALGS
    if isinstance(key, ec.EllipticCurvePublicKey):
        return _EC_ALGS
    if isinstance(key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
        return _OKP_ALGS
    raise SecurityConfigError("JWT_PUBLIC_KEY: unsupported public key type")


def _load_public_key(raw: str) -> tuple[Any, frozenset[str], str]:
    """Accept a PEM public key or a JSON JWK. Returns (key, family, default_alg)."""
    from cryptography.hazmat.primitives import serialization

    text = raw.strip()
    if text.startswith("{"):
        try:
            jwk = json.loads(text)
            key = jwt.PyJWK(jwk).key
        except Exception as exc:  # malformed JWK
            raise SecurityConfigError(f"JWT_PUBLIC_KEY: invalid JWK ({type(exc).__name__})") from exc
        if not hasattr(key, "public_bytes"):
            raise SecurityConfigError("JWT_PUBLIC_KEY: JWK must be a PUBLIC key (symmetric JWKs go in JWT_SECRET)")
        family = _detect_asymmetric_algs(key)
        alg = jwk.get("alg")
        if alg and alg not in family:
            raise SecurityConfigError(f"JWT_PUBLIC_KEY: JWK alg {alg!r} does not match key type")
        return key, family, alg or _default_alg(family)
    if "-----BEGIN" in text:
        try:
            key = serialization.load_pem_public_key(text.encode())
        except Exception as exc:
            raise SecurityConfigError(f"JWT_PUBLIC_KEY: invalid PEM public key ({type(exc).__name__})") from exc
        family = _detect_asymmetric_algs(key)
        return key, family, _default_alg(family)
    raise SecurityConfigError("JWT_PUBLIC_KEY must be a PEM public key or a JSON JWK")


def _default_alg(family: frozenset[str]) -> str:
    if family is _RSA_ALGS:
        return "RS256"
    if family is _EC_ALGS:
        return "ES256"
    return "EdDSA"


@dataclass(frozen=True)
class KeyMaterial:
    key: Any
    algorithms: tuple[str, ...]
    kind: str  # "hmac" | "asymmetric"


def load_key_material(cfg) -> KeyMaterial:
    """Resolve the verification key from settings. Raises SecurityConfigError."""
    secret = (cfg.jwt_secret or "").strip()
    public = (cfg.jwt_public_key or "").strip()
    if secret and public:
        raise SecurityConfigError("Set exactly one of JWT_SECRET or JWT_PUBLIC_KEY, not both")
    if not secret and not public:
        raise SecurityConfigError(
            "No JWT signing key configured: set JWT_SECRET (HS256, >= 32 chars) "
            "or JWT_PUBLIC_KEY / OIDC_JWK (PEM or JWK). There is no default key."
        )

    requested = tuple(a.strip() for a in (cfg.jwt_algorithms or "").split(",") if a.strip())
    if any(a.lower() == "none" for a in requested):
        raise SecurityConfigError("JWT_ALGORITHMS: 'none' is never permitted")

    if secret:
        if secret.lower() in FORBIDDEN_SECRETS:
            raise SecurityConfigError("JWT_SECRET is a known placeholder value; generate a real secret")
        if len(secret) < MIN_SECRET_CHARS:
            raise SecurityConfigError(f"JWT_SECRET must be at least {MIN_SECRET_CHARS} characters")
        if len(set(secret)) < 8:
            raise SecurityConfigError("JWT_SECRET has too little variety to be a real secret")
        algs = requested or ("HS256",)
        bad = [a for a in algs if a not in _HMAC_ALGS]
        if bad:
            raise SecurityConfigError(f"JWT_ALGORITHMS {bad} are not HMAC algorithms; JWT_SECRET only supports HS256/384/512")
        return KeyMaterial(key=secret, algorithms=algs, kind="hmac")

    key, family, default_alg = _load_public_key(public)
    algs = requested or (default_alg,)
    bad = [a for a in algs if a not in family]
    if bad:
        raise SecurityConfigError(f"JWT_ALGORITHMS {bad} do not match the configured public key type")
    return KeyMaterial(key=key, algorithms=algs, kind="asymmetric")


@dataclass(frozen=True)
class Principal:
    subject: str
    agent: Optional[str]
    scopes: frozenset[str]
    claims: dict[str, Any] = field(default_factory=dict, compare=False)

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def _parse_scopes(claims: dict[str, Any]) -> frozenset[str]:
    raw = claims.get("scope")
    if isinstance(raw, str):
        return frozenset(s for s in raw.split() if s)
    raw = claims.get("scopes", raw)
    if isinstance(raw, (list, tuple)):
        return frozenset(str(s) for s in raw if s)
    return frozenset()


class TokenVerifier:
    """Verifies bearer tokens. Signature, audience, issuer, expiry and the
    required-claim set are ALWAYS checked; there is no option to relax any of
    them."""

    def __init__(self, material: KeyMaterial, *, audience: str, issuer: str,
                 leeway_s: int = 30, max_ttl_s: int = 24 * 3600):
        if not audience:
            raise SecurityConfigError("JWT_AUDIENCE is required")
        if not issuer:
            raise SecurityConfigError("JWT_ISSUER is required")
        self.material = material
        self.audience = audience
        self.issuer = issuer
        self.leeway_s = leeway_s
        self.max_ttl_s = max_ttl_s

    @classmethod
    def from_settings(cls, cfg) -> "TokenVerifier":
        return cls(load_key_material(cfg), audience=cfg.jwt_audience, issuer=cfg.jwt_issuer,
                   leeway_s=cfg.jwt_leeway_s, max_ttl_s=cfg.jwt_max_ttl_s)

    def verify(self, token: str) -> Principal:
        if not token or token.count(".") != 2:
            raise AuthError("malformed_token")
        try:
            claims = jwt.decode(
                token,
                self.material.key,
                algorithms=list(self.material.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_s,
                options={
                    "require": list(REQUIRED_CLAIMS),
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("token_expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise AuthError("token_not_yet_valid") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError("invalid_audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("invalid_issuer") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AuthError(f"missing_claim:{exc.claim}") from exc
        except jwt.InvalidAlgorithmError as exc:
            raise AuthError("invalid_algorithm") from exc
        except jwt.InvalidSignatureError as exc:
            raise AuthError("invalid_signature") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError("invalid_token") from exc

        # Defense in depth beyond the library checks.
        try:
            ttl = int(claims["exp"]) - int(claims["iat"])
        except (TypeError, ValueError) as exc:
            raise AuthError("invalid_token") from exc
        if ttl > self.max_ttl_s:
            raise AuthError("token_ttl_too_long")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthError("missing_claim:sub")
        agent = claims.get("agent")
        if agent is not None and (not isinstance(agent, str) or not agent.strip()):
            raise AuthError("invalid_agent_claim")
        scopes = _parse_scopes(claims)
        unknown = scopes - ALL_SCOPES
        if unknown:
            raise AuthError("unknown_scope")
        return Principal(subject=subject, agent=agent, scopes=scopes, claims=claims)


def mint_token(*, key: Any, algorithm: str, audience: str, issuer: str, subject: str,
               agent: Optional[str] = None, scopes: Iterable[str] = (), ttl_s: int = 3600,
               issued_at: Optional[int] = None, extra: Optional[dict[str, Any]] = None) -> str:
    """Issue a token. Used by tests, the benchmark, and the `mint` CLI. The
    gateway itself never mints — it only verifies."""
    now = int(time.time()) if issued_at is None else issued_at
    claims: dict[str, Any] = {
        "sub": subject, "aud": audience, "iss": issuer,
        "iat": now, "nbf": now, "exp": now + ttl_s,
        "jti": uuid.uuid4().hex,
        "scope": " ".join(sorted(set(scopes))),
    }
    if agent:
        claims["agent"] = agent
    if extra:
        claims.update(extra)
    return jwt.encode(claims, key, algorithm=algorithm)


# ===========================================================================
# 4. CORS — explicit allowlist, fail-closed
# ===========================================================================
def parse_origins(raw: str, *, production: bool) -> list[str]:
    origins = [o.strip() for o in (raw or "").split(",") if o.strip()]
    if not origins:
        if production:
            raise SecurityConfigError("FRONTEND_ORIGIN is required in production (explicit https origins)")
        return []
    for origin in origins:
        if "*" in origin:
            raise SecurityConfigError("FRONTEND_ORIGIN: wildcard '*' is not permitted; list explicit origins")
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise SecurityConfigError(f"FRONTEND_ORIGIN: {origin!r} is not a valid origin (scheme://host[:port])")
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise SecurityConfigError(f"FRONTEND_ORIGIN: {origin!r} must be a bare origin without path or credentials")
        if origin.endswith("/"):
            raise SecurityConfigError(f"FRONTEND_ORIGIN: {origin!r} must not end with '/'")
        if production and parts.scheme != "https":
            raise SecurityConfigError(f"FRONTEND_ORIGIN: {origin!r} must be https in production")
    seen: set[str] = set()
    result = []
    for o in origins:
        if o not in seen:
            seen.add(o)
            result.append(o)
    return result


def install_cors(app, cfg) -> list[str]:
    origins = parse_origins(cfg.frontend_origin, production=cfg.is_production)
    if not origins:
        log.warning("FRONTEND_ORIGIN not set: no CORS headers will be emitted; "
                    "cross-origin browser access is denied")
        return []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Admin-Token"],
        max_age=600,
    )
    log.info("CORS allowlist: %s", origins)
    return origins


# ===========================================================================
# Boot-time validation
# ===========================================================================
def validate(cfg) -> dict[str, Any]:
    """Validate the whole security configuration. Raises SecurityConfigError
    with an actionable message; returns a summary safe to log (no secrets)."""
    material = load_key_material(cfg)
    verifier = TokenVerifier.from_settings(cfg)  # checks audience + issuer
    origins = parse_origins(cfg.frontend_origin, production=cfg.is_production)
    if cfg.is_production and not cfg.admin_token:
        raise SecurityConfigError("ADMIN_TOKEN is required in production")
    if cfg.admin_token and cfg.admin_token.lower() in FORBIDDEN_SECRETS:
        raise SecurityConfigError("ADMIN_TOKEN is a known placeholder value")
    if cfg.jwt_leeway_s < 0 or cfg.jwt_leeway_s > 300:
        raise SecurityConfigError("JWT_LEEWAY_S must be between 0 and 300 seconds")
    if cfg.jwt_max_ttl_s <= 0:
        raise SecurityConfigError("JWT_MAX_TTL_S must be positive")
    return {
        "environment": cfg.environment,
        "key_kind": material.kind,
        "algorithms": list(material.algorithms),
        "audience": verifier.audience,
        "issuer": verifier.issuer,
        "cors_origins": origins,
    }


# ===========================================================================
# 5. Endpoint authentication / authorization dependencies
# ===========================================================================
_bearer = HTTPBearer(auto_error=False)


def _unauthorized(code: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail=code,
        headers={"WWW-Authenticate": f'Bearer error="invalid_token", error_description="{code}"'},
    )


async def authenticate(request: Request,
                       creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="bearer_token_required",
                            headers={"WWW-Authenticate": "Bearer"})
    verifier: TokenVerifier = request.app.state.verifier
    try:
        return verifier.verify(creds.credentials)
    except AuthError as exc:
        log.info("auth denied: %s", exc.code)
        raise _unauthorized(exc.code)


def require_scope(scope: str):
    if scope not in ALL_SCOPES:
        raise ValueError(f"unknown scope {scope}")

    async def _dep(principal: Principal = Depends(authenticate)) -> Principal:
        if not principal.has(scope):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail=f"insufficient_scope:{scope}",
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'},
            )
        return principal

    return _dep


def authorize_invoke(principal: Principal, requesting_agent: str) -> None:
    """A token may only invoke the gateway as the agent it was issued to."""
    if not principal.agent:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="agent_claim_required")
    if principal.agent != requesting_agent:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail=f"requesting_agent_mismatch:{principal.agent}")


# ===========================================================================
# Request sanitizer
# ===========================================================================
MAX_PAYLOAD_DEPTH = 8
MAX_PAYLOAD_ITEMS = 500
MAX_STRING_CHARS = 16_000
_KEEP_CONTROL = frozenset("\n\t")


def sanitize_text(value: str) -> str:
    """NFKC-normalize and drop control / format / surrogate / private-use /
    unassigned characters. Zero-width and bidi-override characters are in the
    format (Cf) category, so `guaranteed​returns` becomes
    `guaranteedreturns` and fullwidth or ligature spellings collapse to ASCII —
    the governance claim screen sees what a human would read."""
    if not isinstance(value, str):
        raise SanitizeError("expected string")
    if len(value) > MAX_STRING_CHARS:
        raise SanitizeError("string_too_long")
    normalized = unicodedata.normalize("NFKC", value)
    out = []
    for ch in normalized:
        if ch in _KEEP_CONTROL:
            out.append(ch)
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        out.append(ch)
    return "".join(out)


def sanitize_value(value: Any, *, _depth: int = 0, _budget: Optional[list[int]] = None) -> Any:
    """Recursively sanitize every string inside a JSON-like value; cap depth
    and total item count so a hostile payload cannot exhaust the gate."""
    budget = _budget if _budget is not None else [MAX_PAYLOAD_ITEMS]
    if _depth > MAX_PAYLOAD_DEPTH:
        raise SanitizeError("payload_too_deep")
    budget[0] -= 1
    if budget[0] < 0:
        raise SanitizeError("payload_too_large")
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {sanitize_text(str(k)): sanitize_value(v, _depth=_depth + 1, _budget=budget)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(v, _depth=_depth + 1, _budget=budget) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_text(str(value))


_ENVELOPE_TEXT_FIELDS = ("requesting_agent", "target_agent", "module", "action", "truth_label",
                         "project_id", "customer_id", "approval_level", "message",
                         "requestor_region")


def sanitize_envelope(env):
    """Return a sanitized copy of an McpEnvelope. The human signature is left
    untouched — it is an opaque credential, not text."""
    update: dict[str, Any] = {}
    for name in _ENVELOPE_TEXT_FIELDS:
        current = getattr(env, name)
        if isinstance(current, str):
            update[name] = sanitize_text(current)
    update["source_refs"] = [sanitize_text(s) for s in env.source_refs]
    update["payload"] = sanitize_value(dict(env.payload))
    return env.model_copy(update=update)


# ===========================================================================
# CLI — mint a token for local testing (never used by the server itself)
# ===========================================================================
def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.gateway.security")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="check the security configuration and exit")
    v.set_defaults(fn=_cli_validate)
    m = sub.add_parser("mint", help="mint a bearer token with the configured HS secret")
    m.add_argument("--sub", required=True)
    m.add_argument("--agent", default=None)
    m.add_argument("--scope", action="append", default=[], help="repeatable")
    m.add_argument("--ttl", type=int, default=3600)
    m.set_defaults(fn=_cli_mint)
    args = parser.parse_args(argv)
    return args.fn(args)


def _cli_validate(_args) -> int:
    from .config import settings

    try:
        summary = validate(settings)
    except SecurityConfigError as exc:
        print(f"INVALID: {exc}")
        return 1
    print(json.dumps(summary, indent=2))
    return 0


def _cli_mint(args) -> int:
    from .config import settings

    material = load_key_material(settings)
    if material.kind != "hmac":
        print("mint: only HS* secrets can mint here; asymmetric keys are verified only")
        return 1
    print(mint_token(key=material.key, algorithm=material.algorithms[0],
                     audience=settings.jwt_audience, issuer=settings.jwt_issuer,
                     subject=args.sub, agent=args.agent, scopes=args.scope, ttl_s=args.ttl))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
