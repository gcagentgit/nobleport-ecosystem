"""Settings. Every key is an environment variable prefixed ``STEPH_EMAIL_``."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "steph-email-engine"


def _csv(value: str) -> list[str]:
    return [v.strip().lower() for v in value.split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STEPH_EMAIL_", env_file=".env", extra="ignore")

    # storage
    data_dir: Path = _default_data_dir()
    db_path: Path | None = None            # defaults to <data_dir>/steph-email.db
    secret_key: str | None = None          # Fernet key; generated on first run when unset
    store_bodies: bool = True
    store_raw: bool = True                 # keep the original RFC 822 bytes under <data_dir>/raw

    # API / dashboard
    host: str = "127.0.0.1"
    port: int = 8030
    api_token: str = ""                    # empty = local only, unauthenticated
    public_base_url: str = "http://127.0.0.1:8030"   # what Twilio fetches TwiML / audio from

    # sync
    sync_interval_s: int = 120
    sync_batch: int = 200
    initial_backfill: int = 500
    folders: str = "INBOX"
    imap_timeout_s: float = 30.0
    log_level: str = "INFO"

    # the owner
    owner_name: str = "Michael"
    owner_phone: str = ""                  # E.164; the ONLY number the engine will text or call
    owner_addresses: str = ""              # extra addresses that count as "me" (comma separated)
    timezone: str = "America/New_York"

    # urgency + replies
    vip_senders: str = ""                  # comma separated addresses
    vip_domains: str = ""                  # comma separated domains
    reply_due_days: int = 3
    alerts_enabled: bool = True
    alert_min_urgency: str = "critical"    # SMS immediately at or above this level

    # morning brief
    brief_hour: int = 7
    brief_minute: int = 0
    brief_lookback_hours: int = 24
    brief_sms: bool = True
    brief_call: bool = False               # place a voice call that reads the brief

    # notifications: Twilio (SMS + voice call)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    twilio_base_url: str = "https://api.twilio.com/2010-04-01"

    # voice option: ElevenLabs
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    elevenlabs_base_url: str = "https://api.elevenlabs.io/v1"

    # cleanup (never deletes; originals are preserved on disk and on the server)
    cleanup_after_days: int = 30
    cleanup_archive_folder: str = "Steph/Archive"
    cleanup_copy_to_server: bool = False   # also COPY (never MOVE) archived mail into the archive folder
    cleanup_auto: bool = False             # run the cleanup pass after the morning brief

    # ---- derived
    @property
    def resolved_db_path(self) -> Path:
        return self.db_path or (self.data_dir / "steph-email.db")

    @property
    def folder_list(self) -> list[str]:
        return [f.strip() for f in self.folders.split(",") if f.strip()]

    @property
    def vip_sender_list(self) -> list[str]:
        return _csv(self.vip_senders)

    @property
    def vip_domain_list(self) -> list[str]:
        return _csv(self.vip_domains)

    @property
    def owner_address_list(self) -> list[str]:
        return _csv(self.owner_addresses)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)

    @property
    def elevenlabs_configured(self) -> bool:
        return bool(self.elevenlabs_api_key and self.elevenlabs_voice_id)


settings = Settings()
