from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# core/config/launch-gates.json — reused as the single source of truth for
# prohibited claims, RED-gate scopes, and NY geo-blocks.
_DEFAULT_LAUNCH_GATES = str(
    (Path(__file__).resolve().parent.parent / "config" / "launch-gates.json")
)

PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    database_url: str = "postgresql://noble:noble@postgres:5432/noble"
    redis_url: str = "redis://redis:6379/0"

    launch_gates_path: str = _DEFAULT_LAUNCH_GATES

    # Governance
    max_message_chars: int = 2000
    rate_limit_per_min: int = 20
    cache_ttl_s: int = 60

    # KPI snapshot worker
    kpi_interval_s: int = 600          # 5-15 min cadence
    enable_kpi_worker: bool = True

    # Tool execution
    tool_timeout_s: float = 10.0
    use_http_executor: bool = False    # default to STAGED stub executor

    # Admin (kill switch / privileged endpoints). Required in addition to a
    # bearer token carrying the gateway:admin scope.
    admin_token: str = ""

    # --- Security (see security.py) ---------------------------------------
    # None of these has an insecure default. `security.validate()` refuses to
    # boot the app when the configuration is incomplete or unsafe.
    environment: str = "development"   # "production" enables the strict boot checks

    # Signing key — exactly one of:
    #   JWT_SECRET       shared secret for HS256/HS384/HS512 (>= 32 chars)
    #   JWT_PUBLIC_KEY   PEM public key or JSON JWK for RS*/PS*/ES*/EdDSA
    #                    (OIDC_JWK is accepted as an alias for JWT_PUBLIC_KEY)
    jwt_secret: str = ""
    jwt_public_key: str = Field(
        default="", validation_alias=AliasChoices("JWT_PUBLIC_KEY", "OIDC_JWK", "jwt_public_key")
    )
    jwt_algorithms: str = ""           # comma-separated allowlist; derived from the key type if empty
    jwt_audience: str = ""             # REQUIRED — every token must carry this `aud`
    jwt_issuer: str = ""               # REQUIRED — every token must carry this `iss`
    jwt_leeway_s: int = 30             # clock skew tolerance for exp/nbf/iat
    jwt_max_ttl_s: int = 24 * 3600     # tokens valid longer than this are rejected

    # CORS — explicit comma-separated origin allowlist. "*" is never accepted.
    # Empty: no cross-origin access (development) / refuse to boot (production).
    frontend_origin: str = ""

    # Uploads (POST /agent/upload) — authenticated, size-capped, MIME-allowlisted.
    upload_dir: str = "/var/lib/nobleport/uploads"
    upload_max_bytes: int = 10 * 1024 * 1024
    upload_allowed_mime: str = "image/png,image/jpeg,image/webp,application/pdf,text/plain"

    log_level: str = "INFO"

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in PRODUCTION_ENVIRONMENTS


settings = Settings()
