"""Inbox cleanup that preserves every original.

What "archive" means here:
1. the original RFC 822 bytes are written to ``<data_dir>/raw/<account>/<folder>/<uid>.eml``
   (if they were not already stored at sync time);
2. the message is marked ``archived`` locally so it leaves the working views;
3. optionally the server gets a **COPY** into ``Steph/Archive``.

The engine has no code path that deletes, expunges or moves a message. A
message is never archived unless its original is on disk first.

Candidates: older than ``cleanup_after_days``, low urgency (or normal + already
read), not flagged, not asking for a reply, not part of a thread the owner is
waiting on, and not carrying a protected tag (legal, contract, closing,
permit, invoice, safety, insurance).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .db import Database
from .imap_client import MailboxClient

log = logging.getLogger("steph_email.cleanup")

PROTECTED_TAGS = {"legal", "contract", "closing", "permit", "invoice", "safety", "insurance"}
_SAFE = re.compile(r"[^A-Za-z0-9._@-]+")


@dataclass
class CleanupPlan:
    generated_at: str
    cutoff: str
    candidates: list[dict] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"generated_at": self.generated_at, "cutoff": self.cutoff, "count": len(self.candidates),
                "candidates": [{k: c[k] for k in ("id", "account", "from_addr", "subject", "date", "urgency", "tags", "raw_path")}
                               for c in self.candidates],
                "skipped": self.skipped}


def raw_path_for(raw_dir: Path, account: str, folder: str, uid: int) -> Path:
    return raw_dir / _SAFE.sub("_", account) / _SAFE.sub("_", folder) / f"{uid}.eml"


def preserve_raw(raw_dir: Path, account: str, folder: str, uid: int, raw: bytes) -> Path:
    path = raw_path_for(raw_dir, account, folder, uid)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(raw)
    return path


class Cleaner:
    def __init__(self, db: Database, settings: Settings, imap_factory: Callable[[int], MailboxClient] | None = None):
        self.db = db
        self.settings = settings
        self._imap_factory = imap_factory   # account_id -> logged-in client

    # ------------------------------------------------------------ plan
    def plan(self, now: datetime | None = None, limit: int = 500) -> CleanupPlan:
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=self.settings.cleanup_after_days)).isoformat(timespec="seconds")
        plan = CleanupPlan(generated_at=now.isoformat(timespec="seconds"), cutoff=cutoff)
        open_threads = {r["thread_key"] for r in self.db.list_expected_replies(statuses=("open", "overdue"))}
        skipped: dict[str, int] = {}

        def skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        for msg in self.db.list_messages(limit=limit * 4):
            if not msg["date"] or msg["date"] > cutoff:
                continue
            if msg["flagged"]:
                skip("flagged"); continue
            if msg["urgency"] in ("critical", "high"):
                skip("urgent"); continue
            if msg["urgency"] == "normal" and not msg["seen"]:
                skip("unread normal"); continue
            if msg["needs_reply"]:
                skip("asks for a reply"); continue
            if set(msg["tags"]) & PROTECTED_TAGS:
                skip("protected tag"); continue
            if msg["thread_key"] in open_threads:
                skip("thread awaiting a reply"); continue
            plan.candidates.append(msg)
            if len(plan.candidates) >= limit:
                break
        plan.skipped = skipped
        return plan

    # ------------------------------------------------------------ run
    def run(self, plan: CleanupPlan | None = None, *, now: datetime | None = None,
            copy_to_server: bool | None = None) -> dict:
        plan = plan or self.plan(now=now)
        copy_to_server = self.settings.cleanup_copy_to_server if copy_to_server is None else copy_to_server
        run_id = uuid.uuid4().hex[:12]
        archived = preserved = copied = 0
        errors: list[str] = []
        clients: dict[int, MailboxClient] = {}

        def client_for(account_id: int) -> MailboxClient | None:
            if account_id in clients:
                return clients[account_id]
            if not self._imap_factory:
                return None
            try:
                clients[account_id] = self._imap_factory(account_id)
            except Exception as exc:  # login problems must not stop the local pass
                errors.append(f"account {account_id}: {exc}")
                return None
            return clients[account_id]

        try:
            for msg in plan.candidates:
                raw_path = Path(msg["raw_path"]) if msg["raw_path"] else None
                if not raw_path or not raw_path.exists():
                    client = client_for(msg["account_id"])
                    if client is None:
                        errors.append(f"message {msg['id']}: original not on disk and mailbox unreachable; left untouched")
                        self.db.log_cleanup(run_id, msg["id"], "skipped-no-original")
                        continue
                    try:
                        client.select(msg["folder"])
                        fetched = client.fetch([msg["uid"]])
                        if not fetched:
                            raise RuntimeError("server no longer has the message")
                        raw_path = preserve_raw(self.settings.raw_dir, msg["account"], msg["folder"], msg["uid"], fetched[0].raw)
                        self.db.set_raw_path(msg["id"], str(raw_path))
                        preserved += 1
                    except Exception as exc:
                        errors.append(f"message {msg['id']}: could not preserve original ({exc}); left untouched")
                        self.db.log_cleanup(run_id, msg["id"], "skipped-no-original")
                        continue
                server_copy = ""
                if copy_to_server:
                    client = client_for(msg["account_id"])
                    if client is not None:
                        try:
                            client.ensure_folder(self.settings.cleanup_archive_folder)
                            client.select(msg["folder"])
                            client.copy_to(msg["uid"], self.settings.cleanup_archive_folder)
                            server_copy = self.settings.cleanup_archive_folder
                            copied += 1
                        except Exception as exc:
                            errors.append(f"message {msg['id']}: server copy failed ({exc}); archived locally only")
                self.db.set_archived(msg["id"], True)
                self.db.log_cleanup(run_id, msg["id"], "archived", raw_path=str(raw_path), server_copy=server_copy)
                archived += 1
        finally:
            for c in clients.values():
                try:
                    c.close()
                except Exception:
                    pass
        return {"run_id": run_id, "archived": archived, "preserved_now": preserved, "server_copies": copied,
                "skipped": plan.skipped, "errors": errors}

    def restore(self, message_id: int) -> dict:
        """Undo: bring an archived message back into the working views."""
        msg = self.db.get_message(message_id)
        if not msg:
            raise ValueError(f"no message {message_id}")
        self.db.set_archived(message_id, False)
        self.db.log_cleanup("restore", message_id, "restored", raw_path=msg["raw_path"])
        return self.db.get_message(message_id) or {}
