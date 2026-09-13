"""Shared fixtures: in-memory IMAP/SMTP stand-ins and a simulated notifier, so no test touches the network."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest

from steph_email.config import Settings
from steph_email.crypto import SecretBox
from steph_email.db import Database
from steph_email.imap_client import FetchedMessage
from steph_email.notify import Notifier, SimulatedTransport
from steph_email.service import EmailEngine

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)   # a Monday


def ago(**kw) -> str:
    return format_datetime(NOW - timedelta(**kw))


def make_raw(subject="Hello", sender="Bob Builder <bob@example.com>", to="me@gmail.com", body="Body text",
             message_id=None, in_reply_to=None, references=None, html=None, attachment=None, date=None, cc=None) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Date"] = date or ago(hours=2)
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    if attachment:
        name, data = attachment
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


class FakeImap:
    """Implements steph_email.imap_client.MailboxClient in memory. No delete exists here either."""

    def __init__(self, folders=None, uidvalidity: int = 1):
        self.folders = folders or {"INBOX": {}}
        self.uidvalidity = uidvalidity
        self.selected = None
        self.stored_flags: list[tuple[int, str, bool]] = []
        self.appended: list[tuple[str, bytes]] = []
        self.copied: list[tuple[int, str]] = []
        self.created: list[str] = []
        self.closed = False

    def select(self, folder):
        if folder not in self.folders:
            raise RuntimeError(f"no such folder {folder}")
        self.selected = folder
        uids = self.folders[folder]
        return self.uidvalidity, (max(uids) + 1 if uids else 1)

    def uids_after(self, last_uid, limit):
        uids = sorted(u for u in self.folders[self.selected] if u > last_uid)
        return uids[-limit:] if limit and len(uids) > limit else uids

    def fetch(self, uids):
        out = []
        for u in uids:
            if u in self.folders[self.selected]:
                raw, seen, flagged = self.folders[self.selected][u]
                out.append(FetchedMessage(uid=u, raw=raw, seen=seen, flagged=flagged))
        return out

    def set_seen(self, uid, seen):
        self.stored_flags.append((uid, "seen", seen))

    def set_flagged(self, uid, flagged):
        self.stored_flags.append((uid, "flagged", flagged))

    def list_folders(self):
        return list(self.folders) + self.created

    def append(self, folder, raw):
        self.appended.append((folder, raw))

    def copy_to(self, uid, folder):
        self.copied.append((uid, folder))

    def ensure_folder(self, folder):
        if folder not in self.list_folders():
            self.created.append(folder)

    def close(self):
        self.closed = True


class FakeSmtp:
    def __init__(self):
        self.sent: list[tuple[EmailMessage, dict]] = []

    def send(self, msg, *, user, password=None, access_token=None):
        self.sent.append((msg, {"user": user, "password": password, "access_token": access_token}))
        return msg["Message-ID"]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, db_path=tmp_path / "test.db", sync_interval_s=0, initial_backfill=10, sync_batch=2,
                    owner_phone="+19785551234", vip_senders="inspector@town.gov", public_base_url="http://test.local",
                    cleanup_after_days=30, timezone="America/New_York", brief_hour=7, brief_minute=0)


@pytest.fixture
def db(settings: Settings) -> Database:
    return Database(settings.resolved_db_path)


def sample_messages() -> dict[int, tuple[bytes, bool, bool]]:
    return {
        1: (make_raw("Permit approved for 12 Oak St", sender="Town Inspector <inspector@town.gov>",
                     body="The building permit has been approved. Inspection Thursday.", message_id="<a1@x>", date=ago(hours=20)), True, False),
        2: (make_raw("Invoice #4471 past due", sender="billing@supplyco.com",
                     body="Payment due immediately. Balance $12,500 must be received by Friday.", message_id="<a2@x>", date=ago(hours=5)), False, False),
        3: (make_raw("Re: Permit approved for 12 Oak St", sender="Town Inspector <inspector@town.gov>", body="Great news",
                     message_id="<a3@x>", in_reply_to="<a1@x>", references="<a1@x>", date=ago(hours=19)), False, False),
        4: (make_raw("Weekly deals - 20% off lumber", sender="BigBox <marketing@bigbox.com>",
                     body="Shop now! Unsubscribe here. View in browser.", message_id="<a4@x>", date=ago(days=45)), True, False),
        5: (make_raw("STOP WORK ORDER - 5 Elm St", sender="clerk@town.gov",
                     body="An OSHA inspector is on site and an injury was reported. Respond by tomorrow.", message_id="<a5@x>", date=ago(hours=1)), False, False),
        6: (make_raw("Can you approve the change order?", sender="Sam Sub <sub@vendor.com>",
                     body="Please confirm by tomorrow. The change order totals $45,000.", message_id="<a6@x>", date=ago(hours=3)), False, False),
        7: (make_raw("Old lunch thread", sender="friend@example.com", body="pizza next week", message_id="<a7@x>", date=ago(days=60)), True, False),
    }


@pytest.fixture
def fake_imap() -> FakeImap:
    return FakeImap({"INBOX": sample_messages()})


@pytest.fixture
def fake_smtp() -> FakeSmtp:
    return FakeSmtp()


@pytest.fixture
def transport() -> SimulatedTransport:
    return SimulatedTransport()


@pytest.fixture
def engine(settings: Settings, fake_imap: FakeImap, fake_smtp: FakeSmtp, transport: SimulatedTransport) -> EmailEngine:
    calls: list[sqlite3.Row] = []

    def imap_factory(acct, token):
        calls.append(acct)
        if acct["address"].startswith("broken"):
            raise RuntimeError("AUTHENTICATIONFAILED invalid credentials")
        fake_imap.closed = False
        return fake_imap

    db = Database(settings.resolved_db_path)
    notifier = Notifier(db, settings, transport=transport, voice=None)
    eng = EmailEngine(settings, db=db, secrets=SecretBox.load(settings.data_dir), imap_factory=imap_factory,
                      smtp_factory=lambda acct: fake_smtp, notifier=notifier)
    eng._imap_calls = calls  # type: ignore[attr-defined]
    return eng


def connect(engine: EmailEngine, address="me@gmail.com", **kw) -> dict:
    return engine.add_account(address, "app-pass", **kw)
