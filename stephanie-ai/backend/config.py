"""Stephanie.ai Voice — runtime configuration.

Every value is overridable through environment variables (or a ``.env`` file in
this directory). Nothing here fabricates a LIVE integration: when a provider key
is absent the matching feature runs in STAGED (simulated) mode and says so in
its responses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_BACKEND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_DIR.parent.parent

# Reuse the ecosystem-wide launch gates (danger words, approved wording) so the
# voice layer cannot speak a claim that the rest of NoblePort is not allowed to
# publish.
_DEFAULT_LAUNCH_GATES = _REPO_ROOT / "core" / "config" / "launch-gates.json"
_DEFAULT_BRAND_INTRO = _REPO_ROOT / "ai-voices" / "stephanie_ai_boston_intro.wav"


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so the backend has no python-dotenv dependency."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv(_BACKEND_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class StephanieConfig:
    # --- ElevenLabs-inspired settings -------------------------------------
    ELEVENLABS_API_KEY: Optional[str] = field(
        default_factory=lambda: os.getenv("ELEVENLABS_API_KEY") or None
    )
    ELEVENLABS_BASE_URL: str = field(
        default_factory=lambda: os.getenv(
            "ELEVENLABS_BASE_URL", "https://api.elevenlabs.io/v1"
        )
    )
    ELEVENLABS_MODEL_ID: str = field(
        default_factory=lambda: os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
    )
    MAX_TEXT_LENGTH: int = field(
        default_factory=lambda: int(os.getenv("STEPHANIE_MAX_TEXT_LENGTH", "5000"))
    )
    DEFAULT_VOICE_ID: str = "stephanie_primary"

    # --- Twilio-inspired settings -----------------------------------------
    TWILIO_ACCOUNT_SID: Optional[str] = field(
        default_factory=lambda: os.getenv("TWILIO_ACCOUNT_SID") or None
    )
    TWILIO_AUTH_TOKEN: Optional[str] = field(
        default_factory=lambda: os.getenv("TWILIO_AUTH_TOKEN") or None
    )
    TWILIO_PHONE_NUMBER: Optional[str] = field(
        default_factory=lambda: os.getenv("TWILIO_PHONE_NUMBER") or None
    )
    TWILIO_BASE_URL: str = field(
        default_factory=lambda: os.getenv(
            "TWILIO_BASE_URL", "https://api.twilio.com/2010-04-01"
        )
    )
    # Public URL of this backend, used to build Twilio webhook / TwiML URLs.
    PUBLIC_BASE_URL: str = field(
        default_factory=lambda: os.getenv("STEPHANIE_PUBLIC_BASE_URL", "http://localhost:8000")
    )

    # --- Human gate ---------------------------------------------------------
    # Outbound calls and SMS to real phone numbers are a public broadcast. They
    # only leave the building when this token is presented (X-Human-Approval).
    # With no token configured the telephony layer stays in simulated mode.
    HUMAN_APPROVAL_TOKEN: Optional[str] = field(
        default_factory=lambda: os.getenv("STEPHANIE_HUMAN_APPROVAL_TOKEN") or None
    )
    # Admin token for privileged endpoints (voice library delete, kill switch).
    ADMIN_TOKEN: Optional[str] = field(
        default_factory=lambda: os.getenv("STEPHANIE_ADMIN_TOKEN") or None
    )

    # --- Server settings ----------------------------------------------------
    HOST: str = field(default_factory=lambda: os.getenv("STEPHANIE_HOST", "0.0.0.0"))
    PORT: int = field(default_factory=lambda: int(os.getenv("STEPHANIE_PORT", "8000")))
    DEBUG: bool = field(default_factory=lambda: _env_bool("STEPHANIE_DEBUG", False))
    CORS_ORIGINS: str = field(default_factory=lambda: os.getenv("STEPHANIE_CORS_ORIGINS", "*"))

    # --- Audio settings -----------------------------------------------------
    SAMPLE_RATE: int = field(
        default_factory=lambda: int(os.getenv("STEPHANIE_SAMPLE_RATE", "44100"))
    )
    AUDIO_FORMAT: str = field(default_factory=lambda: os.getenv("STEPHANIE_AUDIO_FORMAT", "wav"))
    MAX_AUDIO_DURATION: int = field(
        default_factory=lambda: int(os.getenv("STEPHANIE_MAX_AUDIO_DURATION", "600"))
    )  # seconds

    # --- Files --------------------------------------------------------------
    LAUNCH_GATES_PATH: str = field(
        default_factory=lambda: os.getenv("STEPHANIE_LAUNCH_GATES", str(_DEFAULT_LAUNCH_GATES))
    )
    BRAND_INTRO_PATH: str = field(
        default_factory=lambda: os.getenv("STEPHANIE_BRAND_INTRO", str(_DEFAULT_BRAND_INTRO))
    )
    DATA_DIR: str = field(
        default_factory=lambda: os.getenv("STEPHANIE_DATA_DIR", str(_BACKEND_DIR / "data"))
    )

    # ------------------------------------------------------------------------
    @property
    def elevenlabs_enabled(self) -> bool:
        return bool(self.ELEVENLABS_API_KEY)

    @property
    def twilio_enabled(self) -> bool:
        return bool(self.TWILIO_ACCOUNT_SID and self.TWILIO_AUTH_TOKEN and self.TWILIO_PHONE_NUMBER)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    def truth_labels(self) -> dict[str, str]:
        """Truth labels for /health — LIVE only when a real provider is wired."""
        return {
            "local_synthesis": "STAGED",  # formant synthesizer, not a neural TTS
            "elevenlabs": "LIVE" if self.elevenlabs_enabled else "STAGED",
            "twilio": "LIVE" if self.twilio_enabled else "STAGED",
            "human_gate": "ENFORCED" if self.HUMAN_APPROVAL_TOKEN else "SIMULATED_ONLY",
        }
