"""Orchestration: add accounts, test connections, sync folders, send mail.

All IMAP/SMTP objects are created through injectable factories so the whole
service is unit-testable without a network.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Settings
from .crypto import SecretBox
from .db import Database, utcnow
from .imap_client import ImapClient, MailboxClient
from .oauth import fetch_access_token
from .parser import parse_message
from .providers import Provider, detect_provider, get_provider
from .rules import Tagger
from .smtp_client import SmtpSender, build_message

log = logging.getLogger("mailhub.service")

ImapFactory = Callable[[sqlite3.Row, str | None], MailboxClient]  # (account_row, access_token) -> client


@dataclass
class SyncReport:
    account: str
    folder: str
    fetched: int = 0
    reset: bool = False
    error: str | None = None


class MailHub:
    def __init__(
        self,
        settings: Settings,
        db: Database | None = None,
        secrets: SecretBox | None = None,
        imap_factory: ImapFactory | None = None,
        smtp_factory: Callable[[sqlite3.Row], SmtpSender] | None = None,
        tagger: Tagger | None = None,
    ):
        self.settings = settings
        self.db = db or Database(settings.resolved_db_path)
        self.secrets = secrets or SecretBox.load(Path(settings.data_dir), settings.secret_key)
        self._imap_factory = imap_factory or self._default_imap
        self._smtp_factory = smtp_factory or self._default_smtp
        self.tagger = tagger or Tagger.load(Path(settings.data_dir))

    # ------------------------------------------------------------ factories
    def _default_imap(self, acct: sqlite3.Row, access_token: str | None) -> MailboxClient:
        client = ImapClient(acct["imap_host"], acct["imap_port"], timeout=self.settings.imap_timeout_s)
        if acct["auth_method"] == "oauth2":
            client.login_oauth2(acct["username"], access_token or "")
        else:
            client.login(acct["username"], self.secrets.decrypt(acct["secret_enc"]))
        return client

    def _default_smtp(self, acct: sqlite3.Row) -> SmtpSender:
        return SmtpSender(acct["smtp_host"], acct["smtp_port"], bool(acct["smtp_ssl"]), timeout=self.settings.imap_timeout_s)

    def _access_token(self, acct: sqlite3.Row) -> str | None:
        if acct["auth_method"] != "oauth2":
            return None
        prov = get_provider(acct["provider"])
        return fetch_access_token(
            acct["oauth_token_url"] or prov.oauth_token_url,
            acct["oauth_client_id"],
            self.secrets.decrypt(acct["oauth_client_secret_enc"]) if acct["oauth_client_secret_enc"] else "",
            self.secrets.decrypt(acct["secret_enc"]),
            prov.oauth_scope,
            cache_key=f"acct:{acct['id']}",
        )

    # ------------------------------------------------------------ accounts
    def add_account(
        self,
        address: str,
        secret: str,
        *,
        provider: str | None = None,
        display_name: str = "",
        username: str | None = None,
        auth_method: str = "password",
        imap_host: str | None = None,
        imap_port: int | None = None,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_ssl: bool | None = None,
        oauth_client_id: str = "",
        oauth_client_secret: str = "",
        oauth_token_url: str = "",
        sent_folder: str | None = None,
        test: bool = True,
    ) -> dict:
        address = address.strip().lower()
        if "@" not in address:
            raise ValueError("address must be an email address")
        prov: Provider = get_provider(provider) if provider else detect_provider(address)
        if auth_method not in prov.auth_methods and prov.key != "generic":
            raise ValueError(f"{prov.name} supports auth methods: {', '.join(prov.auth_methods)}")
        if auth_method == "oauth2" and not oauth_client_id:
            raise ValueError("oauth2 requires oauth_client_id (and the secret is the refresh token)")
        row = dict(
            address=address,
            display_name=display_name,
            provider=prov.key,
            imap_host=imap_host or prov.imap_host,
            imap_port=imap_port or prov.imap_port,
            smtp_host=smtp_host or prov.smtp_host,
            smtp_port=smtp_port or prov.smtp_port,
            smtp_ssl=int(prov.smtp_ssl if smtp_ssl is None else smtp_ssl),
            auth_method=auth_method,
            username=username or address,
            secret_enc=self.secrets.encrypt(secret),
            oauth_client_id=oauth_client_id,
            oauth_client_secret_enc=self.secrets.encrypt(oauth_client_secret) if oauth_client_secret else "",
            oauth_token_url=oauth_token_url or prov.oauth_token_url,
            sent_folder=sent_folder or prov.sent_folder,
        )
        if not row["imap_host"]:
            raise ValueError(f"no IMAP host known for {address}; pass imap_host/smtp_host (provider=generic)")
        if self.db.get_account(address):
            raise ValueError(f"account {address} already connected")
        account_id = self.db.add_account(**row)
        acct = self.db.get_account(account_id)
        if test:
            try:
                self.test_account(acct)
            except Exception:
                self.db.remove_account(account_id)
                raise
        return self.describe_account(acct)

    def describe_account(self, acct: sqlite3.Row) -> dict:
        d = {k: acct[k] for k in acct.keys() if not k.endswith("_enc")}
        d["smtp_ssl"] = bool(d["smtp_ssl"])
        d["enabled"] = bool(d["enabled"])
        d["has_oauth_client_secret"] = bool(acct["oauth_client_secret_enc"])
        return d

    def test_account(self, acct: sqlite3.Row) -> dict:
        """Log in over IMAP and select INBOX. Raises on failure."""
        client = self._imap_factory(acct, self._access_token(acct))
        try:
            uidvalidity, uidnext = client.select("INBOX")
            folders = client.list_folders()
        finally:
            client.close()
        self.db.update_account(acct["id"], last_error=None)
        return {"ok": True, "uidvalidity": uidvalidity, "uidnext": uidnext, "folders": folders}

    def remove_account(self, ident: int | str) -> bool:
        acct = self.db.get_account(ident)
        return bool(acct and self.db.remove_account(acct["id"]))

    # ------------------------------------------------------------ sync
    def sync_all(self, folders: list[str] | None = None) -> list[SyncReport]:
        reports: list[SyncReport] = []
        for acct in self.db.list_accounts(enabled_only=True):
            reports.extend(self.sync_account(acct, folders))
        return reports

    def sync_account(self, acct: sqlite3.Row | int | str, folders: list[str] | None = None) -> list[SyncReport]:
        if not isinstance(acct, sqlite3.Row):
            found = self.db.get_account(acct)
            if not found:
                raise ValueError(f"unknown account {acct!r}")
            acct = found
        folders = folders or self.settings.folder_list
        reports: list[SyncReport] = []
        try:
            client = self._imap_factory(acct, self._access_token(acct))
        except Exception as exc:  # login failure: report on every folder, keep going with other accounts
            log.warning("login failed for %s: %s", acct["address"], exc)
            self.db.update_account(acct["id"], last_error=f"login: {exc}"[:500])
            return [SyncReport(acct["address"], f, error=str(exc)) for f in folders]
        try:
            for folder in folders:
                reports.append(self._sync_folder(client, acct, folder))
        finally:
            client.close()
        errors = [r.error for r in reports if r.error]
        self.db.update_account(acct["id"], last_sync_at=utcnow(), last_error=("; ".join(errors)[:500] or None))
        return reports

    def _sync_folder(self, client: MailboxClient, acct: sqlite3.Row, folder: str) -> SyncReport:
        report = SyncReport(acct["address"], folder)
        try:
            uidvalidity, _uidnext = client.select(folder)
            known_validity, last_uid = self.db.get_folder_state(acct["id"], folder)
            if known_validity and uidvalidity and known_validity != uidvalidity:
                log.info("UIDVALIDITY changed for %s/%s — resetting", acct["address"], folder)
                self.db.reset_folder(acct["id"], folder)
                last_uid = 0
                report.reset = True
            first_run = last_uid == 0
            limit = self.settings.initial_backfill if first_run else self.settings.sync_batch
            uids = client.uids_after(last_uid, limit)
            if uids:
                for fetched in client.fetch(uids):
                    parsed = parse_message(fetched.raw)
                    if not self.settings.store_bodies:
                        parsed["body_html"] = ""
                    with self.db.tx():
                        mid = self.db.upsert_message(acct["id"], folder, fetched.uid, parsed, fetched.seen, fetched.flagged)
                        tags = self.tagger.tags_for(parsed["subject"], parsed["from_addr"], parsed["body_text"])
                        if tags:
                            self.db.add_tags(mid, tags)
                    report.fetched += 1
                    last_uid = max(last_uid, fetched.uid)
            self.db.set_folder_state(acct["id"], folder, uidvalidity, last_uid)
        except Exception as exc:
            log.exception("sync failed for %s/%s", acct["address"], folder)
            report.error = str(exc)
        return report

    # ------------------------------------------------------------ flags
    def mark(self, message_id: int, *, seen: bool | None = None, flagged: bool | None = None, push: bool = True) -> dict:
        msg = self.db.get_message(message_id)
        if not msg:
            raise ValueError(f"no message {message_id}")
        self.db.set_flags(message_id, seen=seen, flagged=flagged)
        if push:
            acct = self.db.get_account(msg["account_id"])
            client = self._imap_factory(acct, self._access_token(acct))
            try:
                client.select(msg["folder"])
                if seen is not None:
                    client.set_seen(msg["uid"], seen)
                if flagged is not None:
                    client.set_flagged(msg["uid"], flagged)
            finally:
                client.close()
        return self.db.get_message(message_id) or {}

    # ------------------------------------------------------------ send
    def send(
        self,
        account: int | str,
        to: list[str],
        subject: str,
        text: str,
        *,
        html: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to_message_id: int | None = None,
        attachments: list[Path] | None = None,
        save_to_sent: bool = True,
    ) -> dict:
        acct = self.db.get_account(account)
        if not acct:
            raise ValueError(f"unknown account {account!r}")
        if not acct["smtp_host"]:
            raise ValueError(f"account {acct['address']} has no SMTP host configured")
        in_reply_to = references = ""
        if reply_to_message_id is not None:
            original = self.db.get_message(reply_to_message_id)
            if not original:
                raise ValueError(f"no message {reply_to_message_id} to reply to")
            in_reply_to = original["message_id"]
            references = original["thread_key"] if original["thread_key"] != in_reply_to and not original["thread_key"].startswith("subj:") else ""
            if not subject:
                subject = original["subject"] if original["subject"].lower().startswith("re:") else f"Re: {original['subject']}"
        msg = build_message(
            from_addr=acct["address"], from_name=acct["display_name"], to=to, subject=subject, text=text,
            html=html, cc=cc, bcc=bcc, in_reply_to=in_reply_to, references=references, attachments=attachments,
        )
        token = self._access_token(acct)
        sender = self._smtp_factory(acct)
        if token:
            msgid = sender.send(msg, user=acct["username"], access_token=token)
        else:
            msgid = sender.send(msg, user=acct["username"], password=self.secrets.decrypt(acct["secret_enc"]))
        self.db.log_sent(acct["id"], msgid, to, subject, in_reply_to)
        if save_to_sent and acct["sent_folder"] and acct["provider"] != "gmail":
            # Gmail files sent mail automatically; others need an APPEND.
            try:
                client = self._imap_factory(acct, token)
                try:
                    client.append(acct["sent_folder"], msg.as_bytes())
                finally:
                    client.close()
            except Exception as exc:
                log.warning("could not copy to sent folder for %s: %s", acct["address"], exc)
        return {"message_id": msgid, "from": acct["address"], "to": to, "subject": subject}
