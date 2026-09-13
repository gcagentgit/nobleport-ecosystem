from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
import re


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def sender_pattern(value):
    value = value.strip().lower()
    pattern = r"(?:[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}"
    if not re.fullmatch(pattern, value) or len(value) > 254:
        raise ValueError("Use a complete sender email address or domain")
    return value


def sender_matches(pattern, sender):
    address = parseaddr(sender)[1].strip().lower()
    if address.count("@") != 1:
        return False
    return address == pattern if "@" in pattern else address.split("@", 1)[1] == pattern


@dataclass
class Email:
    account: str
    folder: str
    uidvalidity: str
    uid: str
    sender: str
    subject: str
    received_at: datetime
    text: str = ""
    message_id: str = ""
    attachments: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.received_at = aware(self.received_at)
        self.uid = str(self.uid)
        self.uidvalidity = str(self.uidvalidity)
