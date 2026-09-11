from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    # XDG-style on Linux: ~/.local/share/nobleport-mailhub
    return Path.home() / ".local" / "share" / "nobleport-mailhub"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAILHUB_", env_file=".env", extra="ignore")

    data_dir: Path = _default_data_dir()
    db_path: Path | None = None          # defaults to <data_dir>/mailhub.db
    secret_key: str | None = None        # Fernet key; generated on first run when unset

    # API
    host: str = "127.0.0.1"
    port: int = 8025
    api_token: str = ""                  # empty = local, unauthenticated (bind to 127.0.0.1!)

    # Sync
    sync_interval_s: int = 120
    sync_batch: int = 200                # max new messages per folder per pass
    initial_backfill: int = 500          # messages fetched on the first sync of a folder
    folders: str = "INBOX"               # comma-separated IMAP folders to watch
    imap_timeout_s: float = 30.0
    store_bodies: bool = True

    log_level: str = "INFO"

    @property
    def resolved_db_path(self) -> Path:
        return self.db_path or (self.data_dir / "mailhub.db")

    @property
    def folder_list(self) -> list[str]:
        return [f.strip() for f in self.folders.split(",") if f.strip()]


settings = Settings()
