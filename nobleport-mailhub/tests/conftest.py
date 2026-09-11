"""Shared fixtures: an in-memory IMAP/SMTP stand-in so no test touches the network."""

from __future__ import annotations

import sqlite3
from email.message import EmailMessage
from pathlib import Path

import pytest

from mailhub.config import Settings
from mailhub.crypto import SecretBox
from mailhub.db import Database
from mailhub.imap_client import FetchedMessage
from mailhub.service import MailHub


def make_raw(subject="Hello", sender="Bob Builder <bob@example.com>", to="me@gmail.com", body="Body text",
             message_id=None, in_reply_to=None, references=None, html=None, attachment=None, date="Mon, 01 Sep 2026 10:00:00 -0400") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = date
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
    """Implements mailhub.imap_client.MailboxClient in memory."""

    def __init__(self, folders: dict[str, dict[int, tuple[bytes, bool, bool]]] | None = None, uidvalidity: int = 1,
                 fail_login: bool = False):
        self.folders = folders or {"INBOX": {}}
        self.uidvalidity = uidvalidity
        self.fail_login = fail_login
        self.selected: str | None = None
        self.stored_flags: list[tuple[int, str, bool]] = []
        self.appended: list[tuple[str, bytes]] = []
        self.closed = False
        if fail_login:
            raise RuntimeError("AUTHENTICATIONFAILED invalid credentials")

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
            raw, seen, flagged = self.folders[self.selected][u]
            out.append(FetchedMessage(uid=u, raw=raw, seen=seen, flagged=flagged))
        return out

    def set_seen(self, uid, seen):
        self.stored_flags.append((uid, "seen", seen))

    def set_flagged(self, uid, flagged):
        self.stored_flags.append((uid, "flagged", flagged))

    def list_folders(self):
        return list(self.folders)

    def append(self, folder, raw):
        self.appended.append((folder, raw))

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
    return Settings(data_dir=tmp_path, db_path=tmp_path / "test.db", sync_interval_s=0, initial_backfill=3, sync_batch=2)


@pytest.fixture
def fake_imap() -> FakeImap:
    return FakeImap({"INBOX": {
        1: (make_raw("Permit approved for 12 Oak St", body="The building permit has been approved.", message_id="<a1@x>"), True, False),
        2: (make_raw("Invoice #4471 past due", sender="billing@supplyco.com", body="Payment due immediately.", message_id="<a2@x>"), False, True),
        3: (make_raw("Re: Permit approved for 12 Oak St", body="Great news", message_id="<a3@x>", in_reply_to="<a1@x>", references="<a1@x>"), False, False),
    }})


@pytest.fixture
def fake_smtp() -> FakeSmtp:
    return FakeSmtp()


@pytest.fixture
def hub(settings: Settings, fake_imap: FakeImap, fake_smtp: FakeSmtp) -> MailHub:
    calls: list[sqlite3.Row] = []

    def imap_factory(acct, token):
        calls.append(acct)
        if acct["address"].startswith("broken"):
            raise RuntimeError("AUTHENTICATIONFAILED invalid credentials")
        fake_imap.closed = False
        return fake_imap

    h = MailHub(settings, db=Database(settings.resolved_db_path), secrets=SecretBox.load(settings.data_dir),
                imap_factory=imap_factory, smtp_factory=lambda acct: fake_smtp)
    h._imap_calls = calls  # type: ignore[attr-defined]
    return h
