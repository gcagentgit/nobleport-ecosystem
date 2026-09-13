from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import time
import os
import yaml
from .models import sender_pattern


DEFAULTS = {
    "timezone": "America/New_York", "poll_seconds": 60, "dry_run": True,
    "aggregation_enabled": True, "spam_filtering": True, "urgency_detection": True,
    "sms_notifications": True, "voice_notifications": False, "daily_cleanup": True,
    "retention_days": 90, "urgency_threshold": 40, "notification_channel": "sms",
    "quiet_start": "20:00", "quiet_end": "07:00", "max_alerts_per_hour": 6,
    "summary_time": "08:00", "cleanup_time": "03:00", "notification_max_age_hours": 2,
    "demo_mode": False,
}


def validate_settings(settings, *, allow_demo=False):
    if not isinstance(settings, dict):
        raise ValueError("Settings must be an object")
    if set(settings) - set(DEFAULTS):
        raise ValueError("Unknown setting")
    if "demo_mode" in settings and not allow_demo:
        raise ValueError("Demo mode can only be set at startup")
    for key, value in settings.items():
        if isinstance(DEFAULTS[key], bool) and type(value) is not bool:
            raise ValueError(f"{key} must be true or false")
    for key, low, high in [("poll_seconds", 15, 3600), ("retention_days", 1, 3650),
                            ("urgency_threshold", 1, 100), ("max_alerts_per_hour", 1, 60),
                            ("notification_max_age_hours", 1, 24)]:
        if key in settings and (type(settings[key]) is not int or not low <= settings[key] <= high):
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    if "notification_channel" in settings and settings["notification_channel"] not in ("sms", "voice", "both"):
        raise ValueError("Invalid notification channel")
    if "timezone" in settings:
        try:
            ZoneInfo(settings["timezone"])
        except (TypeError, ValueError, KeyError):
            raise ValueError("Invalid timezone") from None
    for key in ("quiet_start", "quiet_end", "summary_time", "cleanup_time"):
        if key in settings:
            try:
                value = settings[key]
                if len(value) != 5 or time.fromisoformat(value).tzinfo is not None:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValueError(f"{key} requires HH:MM") from None
    return settings


@dataclass
class Config:
    data_dir: Path
    accounts: list[dict] = field(default_factory=list)
    rules: dict = field(default_factory=dict)
    expected: list[dict] = field(default_factory=list)
    settings: dict = field(default_factory=lambda: dict(DEFAULTS))


def load_config(path="config/settings.yaml"):
    path = Path(path).resolve()
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a mapping")
    settings = {**DEFAULTS, **validate_settings(data.get("settings", {}), allow_demo=True)}
    accounts = data.get("accounts", [])
    if not isinstance(accounts, list):
        raise ValueError("accounts must be a list")
    ids = set()
    for account in accounts:
        if not account.get("id") or account["id"] in ids:
            raise ValueError("Account ids must be unique")
        ids.add(account["id"])
        auth = account.get("auth", {})
        if any(k in account or k in auth for k in ("password", "access_token", "refresh_token", "client_secret")):
            raise ValueError("Credentials belong in environment variables, referenced with *_env keys")
        if account.get("provider") == "outlook" and auth.get("type") != "oauth2":
            raise ValueError("Outlook requires OAuth2")
    rules = data.get("rules", {})
    for group in ("priority_senders", "blocked_senders"):
        rules[group] = [sender_pattern(v) for v in rules.get(group, [])]
    data_dir = Path(os.environ.get("STEPH_DATA_DIR", data.get("data_dir", "../data")))
    if not data_dir.is_absolute():
        data_dir = path.parent / data_dir
    return Config(data_dir.resolve(), accounts, rules, data.get("expectations", []), settings)
