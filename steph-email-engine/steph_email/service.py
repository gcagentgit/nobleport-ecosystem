"""Orchestration: the ``EmailEngine``.

Accounts, sync, urgency scoring, raw preservation, expected-reply
reconciliation, immediate alerts, the morning brief with SMS / voice
delivery, cleanup and the activation status. All network objects are created
through injectable factories so the whole engine is unit-testable offline.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .brief import Brief, BriefBuilder
from .cleanup import Cleaner, preserve_raw
from .config import Settings
from .crypto import SecretBox
from .db import Database, utcnow
from .imap_client import ImapClient, MailboxClient
from .notify import DestinationError, Notifier, NotifyError, VoiceError
from .oauth import fetch_access_token
from .parser import parse_message
from .providers import Provider, detect_provider, get_provider
from .replies import ReplyTracker
from .rules import Tagger
from .smtp_client import SmtpSender, build_message
from .urgency import RANK, UrgencyScorer, at_least

log = logging.getLogger("steph_email.service")

ImapFactory = Callable[[sqlite3.Row, str | None], MailboxClient]


@dataclass
class SyncReport:
    account: str
    folder: str
    fetched: int = 0
    reset: bool = False
    error: str | None = None
    urgent_new: list[int] = field(default_factory=list)   # message ids at/above the alert threshold

    def as_dict(self) -> dict:
        return {"account": self.account, "folder": self.folder, "fetched": self.fetched, "reset": self.reset,
                "error": self.error, "urgent_new": list(self.urgent_new)}


class EmailEngine:
    def __init__(
        self,
        settings: Settings,
        db: Database | None = None,
        secrets: SecretBox | None = None,
        imap_factory: ImapFactory | None = None,
        smtp_factory: Callable[[sqlite3.Row], SmtpSender] | None = None,
        tagger: Tagger | None = None,
        notifier: Notifier | None = None,
    ):
        self.settings = settings
        self.db = db or Database(settings.resolved_db_path)
        self.secrets = secrets or SecretBox.load(Path(settings.data_dir), settings.secret_key)
        self._imap_factory = imap_factory or self._default_imap
        self._smtp_factory = smtp_factory or self._default_smtp
        self.tagger = tagger or Tagger.load(Path(settings.data_dir))
        self.notifier = notifier or Notifier(self.db, settings)
        self.replies = ReplyTracker(self.db, self.owner_addresses(), settings.reply_due_days)
        self.scorer = UrgencyScorer(settings.vip_sender_list, settings.vip_domain_list, self.owner_addresses())
        self.briefs = BriefBuilder(self.db, self.replies, owner_name=settings.owner_name, tz=settings.tz,
                                   dashboard_url=settings.public_base_url)
        self.cleaner = Cleaner(self.db, settings, imap_factory=self._imap_for_account_id)
        self.last_cleanup: dict | None = None

    # ------------------------------------------------------------ helpers
    def owner_addresses(self) -> set[str]:
        addrs = {a["address"].lower() for a in self.db.list_accounts()}
        addrs.update(self.settings.owner_address_list)
        return addrs

    def _refresh_owner(self) -> None:
        owners = self.owner_addresses()
        self.replies.owner_addresses = set(owners)
        self.scorer.owner_addresses = set(owners)

    def _default_imap(self, acct: sqlite3.Row, access_token: str | None) -> MailboxClient:
        client = ImapClient(acct["imap_host"], acct["imap_port"], timeout=self.settings.imap_timeout_s)
        if acct["auth_method"] == "oauth2":
            client.login_oauth2(acct["username"], access_token or "")
        else:
            client.login(acct["username"], self.secrets.decrypt(acct["secret_enc"]))
        return client

    def _default_smtp(self, acct: sqlite3.Row) -> SmtpSender:
        return SmtpSender(acct["smtp_host"], acct["smtp_port"], bool(acct["smtp_ssl"]), timeout=self.settings.imap_timeout_s)

    def _imap_for_account_id(self, account_id: int) -> MailboxClient:
        acct = self.db.get_account(account_id)
        if not acct:
            raise ValueError(f"unknown account {account_id}")
        return self._imap_factory(acct, self._access_token(acct))

    def _access_token(self, acct: sqlite3.Row) -> str | None:
        if acct["auth_method"] != "oauth2":
            return None
        prov = get_provider(acct["provider"])
        return fetch_access_token(
            acct["oauth_token_url"] or prov.oauth_token_url, acct["oauth_client_id"],
            self.secrets.decrypt(acct["oauth_client_secret_enc"]) if acct["oauth_client_secret_enc"] else "",
            self.secrets.decrypt(acct["secret_enc"]), prov.oauth_scope, cache_key=f"acct:{acct['id']}",
        )

    # ------------------------------------------------------------ accounts
    def add_account(self, address: str, secret: str, *, provider: str | None = None, display_name: str = "",
                    username: str | None = None, auth_method: str = "password", imap_host: str | None = None,
                    imap_port: int | None = None, smtp_host: str | None = None, smtp_port: int | None = None,
                    smtp_ssl: bool | None = None, oauth_client_id: str = "", oauth_client_secret: str = "",
                    oauth_token_url: str = "", sent_folder: str | None = None, test: bool = True) -> dict:
        address = address.strip().lower()
        if "@" not in address:
            raise ValueError("address must be an email address")
        prov: Provider = get_provider(provider) if provider else detect_provider(address)
        if auth_method not in prov.auth_methods and prov.key != "generic":
            raise ValueError(f"{prov.name} supports auth methods: {', '.join(prov.auth_methods)}")
        if auth_method == "oauth2" and not oauth_client_id:
            raise ValueError("oauth2 requires oauth_client_id (and the secret is the refresh token)")
        row = dict(
            address=address, display_name=display_name, provider=prov.key,
            imap_host=imap_host or prov.imap_host, imap_port=imap_port or prov.imap_port,
            smtp_host=smtp_host or prov.smtp_host, smtp_port=smtp_port or prov.smtp_port,
            smtp_ssl=int(prov.smtp_ssl if smtp_ssl is None else smtp_ssl), auth_method=auth_method,
            username=username or address, secret_enc=self.secrets.encrypt(secret), oauth_client_id=oauth_client_id,
            oauth_client_secret_enc=self.secrets.encrypt(oauth_client_secret) if oauth_client_secret else "",
            oauth_token_url=oauth_token_url or prov.oauth_token_url, sent_folder=sent_folder or prov.sent_folder,
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
        self._refresh_owner()
        return self.describe_account(acct)

    def describe_account(self, acct: sqlite3.Row) -> dict:
        d = {k: acct[k] for k in acct.keys() if not k.endswith("_enc")}
        d["smtp_ssl"] = bool(d["smtp_ssl"])
        d["enabled"] = bool(d["enabled"])
        d["has_oauth_client_secret"] = bool(acct["oauth_client_secret_enc"])
        return d

    def test_account(self, acct: sqlite3.Row) -> dict:
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
        ok = bool(acct and self.db.remove_account(acct["id"]))
        self._refresh_owner()
        return ok

    # ------------------------------------------------------------ sync
    def sync_all(self, folders: list[str] | None = None, *, alert: bool = True) -> list[SyncReport]:
        reports: list[SyncReport] = []
        for acct in self.db.list_accounts(enabled_only=True):
            reports.extend(self.sync_account(acct, folders, alert=False))
        self.after_sync(reports, alert=alert)
        return reports

    def sync_account(self, acct: sqlite3.Row | int | str, folders: list[str] | None = None, *, alert: bool = True) -> list[SyncReport]:
        if not isinstance(acct, sqlite3.Row):
            found = self.db.get_account(acct)
            if not found:
                raise ValueError(f"unknown account {acct!r}")
            acct = found
        folders = folders or self.settings.folder_list
        reports: list[SyncReport] = []
        try:
            client = self._imap_factory(acct, self._access_token(acct))
        except Exception as exc:
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
        if alert:
            self.after_sync(reports, alert=True)
        return reports

    def _sync_folder(self, client: MailboxClient, acct: sqlite3.Row, folder: str) -> SyncReport:
        report = SyncReport(acct["address"], folder)
        try:
            uidvalidity, _ = client.select(folder)
            known_validity, last_uid = self.db.get_folder_state(acct["id"], folder)
            if known_validity and uidvalidity and known_validity != uidvalidity:
                log.info("UIDVALIDITY changed for %s/%s — resetting", acct["address"], folder)
                self.db.reset_folder(acct["id"], folder)
                last_uid = 0
                report.reset = True
            limit = self.settings.initial_backfill if last_uid == 0 else self.settings.sync_batch
            uids = client.uids_after(last_uid, limit)
            for fetched in client.fetch(uids) if uids else []:
                parsed = parse_message(fetched.raw)
                if not self.settings.store_bodies:
                    parsed["body_html"] = ""
                with self.db.tx():
                    mid, created = self.db.upsert_message(acct["id"], folder, fetched.uid, parsed, fetched.seen, fetched.flagged)
                    tags = self.tagger.tags_for(parsed["subject"], parsed["from_addr"], parsed["body_text"])
                    if tags:
                        self.db.add_tags(mid, tags)
                    result = self.scorer.score(subject=parsed["subject"], from_addr=parsed["from_addr"],
                                               body_text=parsed["body_text"], tags=self.db.tags_for(mid),
                                               to_addrs=parsed["to_addrs"], cc_addrs=parsed["cc_addrs"], flagged=fetched.flagged)
                    self.db.set_urgency(mid, result.level, result.score, result.reasons, result.asks_reply)
                    if self.settings.store_raw:
                        path = preserve_raw(self.settings.raw_dir, acct["address"], folder, fetched.uid, fetched.raw)
                        self.db.set_raw_path(mid, str(path))
                if created and not fetched.seen and at_least(result.level, self.settings.alert_min_urgency):
                    report.urgent_new.append(mid)
                report.fetched += 1
                last_uid = max(last_uid, fetched.uid)
            self.db.set_folder_state(acct["id"], folder, uidvalidity, last_uid)
        except Exception as exc:
            log.exception("sync failed for %s/%s", acct["address"], folder)
            report.error = str(exc)
        return report

    def after_sync(self, reports: list[SyncReport], *, alert: bool) -> dict:
        """Reconcile expected replies and fire immediate alerts for new urgent mail."""
        recon = self.replies.reconcile()
        alerts: list[dict] = []
        if alert and self.settings.alerts_enabled:
            for r in reports:
                for mid in r.urgent_new:
                    rec = self.alert_for_message(mid)
                    if rec:
                        alerts.append(rec)
        return {"reconciled": recon, "alerts": alerts}

    def alert_for_message(self, message_id: int) -> dict | None:
        msg = self.db.get_message(message_id)
        if not msg or self.db.notification_exists("alert", message_id):
            return None
        if not self.settings.owner_phone:
            log.info("urgent message %s but no owner phone configured; alert skipped", message_id)
            return None
        why = msg["urgency_reasons"][0] if msg["urgency_reasons"] else msg["urgency"]
        body = (f"Steph: {msg['urgency'].upper()} email from {msg['from_name'] or msg['from_addr']} — "
                f"{msg['subject'][:80] or '(no subject)'} ({why}).")
        try:
            return self.notifier.send_sms(body, purpose="alert", related_message_id=message_id)
        except (NotifyError, ValueError) as exc:
            log.warning("alert for message %s failed: %s", message_id, exc)
            return None

    # ------------------------------------------------------------ flags / archive
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
    def send(self, account: int | str, to: list[str], subject: str, text: str, *, html: str | None = None,
             cc: list[str] | None = None, bcc: list[str] | None = None, reply_to_message_id: int | None = None,
             attachments: list[Path] | None = None, save_to_sent: bool = True, expect_reply: bool = True,
             due_days: int | None = None) -> dict:
        acct = self.db.get_account(account)
        if not acct:
            raise ValueError(f"unknown account {account!r}")
        if not acct["smtp_host"]:
            raise ValueError(f"account {acct['address']} has no SMTP host configured")
        in_reply_to = references = thread_key = ""
        if reply_to_message_id is not None:
            original = self.db.get_message(reply_to_message_id)
            if not original:
                raise ValueError(f"no message {reply_to_message_id} to reply to")
            in_reply_to = original["message_id"]
            thread_key = original["thread_key"]
            references = thread_key if thread_key != in_reply_to and not thread_key.startswith("subj:") else ""
            if not subject:
                subject = original["subject"] if original["subject"].lower().startswith("re:") else f"Re: {original['subject']}"
        msg = build_message(from_addr=acct["address"], from_name=acct["display_name"], to=to, subject=subject, text=text,
                            html=html, cc=cc, bcc=bcc, in_reply_to=in_reply_to, references=references, attachments=attachments)
        token = self._access_token(acct)
        sender = self._smtp_factory(acct)
        if token:
            msgid = sender.send(msg, user=acct["username"], access_token=token)
        else:
            msgid = sender.send(msg, user=acct["username"], password=self.secrets.decrypt(acct["secret_enc"]))
        self.db.log_sent(acct["id"], msgid, to, subject, in_reply_to)
        if save_to_sent and acct["sent_folder"] and acct["provider"] != "gmail":
            try:
                client = self._imap_factory(acct, token)
                try:
                    client.append(acct["sent_folder"], msg.as_bytes())
                finally:
                    client.close()
            except Exception as exc:
                log.warning("could not copy to sent folder for %s: %s", acct["address"], exc)
        result = {"message_id": msgid, "from": acct["address"], "to": to, "subject": subject, "expected_reply": None}
        if expect_reply:
            result["expected_reply"] = self.replies.track(
                account_id=acct["id"], account_address=acct["address"], message_id=msgid,
                thread_key=thread_key or msgid, to_addrs=to, subject=subject, due_days=due_days)
        return result

    # ------------------------------------------------------------ brief
    def build_brief(self, for_date: date | None = None, now: datetime | None = None) -> Brief:
        archived = self.last_cleanup["archived"] if self.last_cleanup else None
        return self.briefs.build(for_date=for_date, now=now, lookback_hours=self.settings.brief_lookback_hours,
                                 archived_last_run=archived)

    def run_brief(self, for_date: date | None = None, *, deliver: bool = False, voice: bool | None = None,
                  now: datetime | None = None, cleanup: bool | None = None) -> dict:
        """Build today's brief, store it, optionally synthesise audio and deliver by SMS / call."""
        cleanup = self.settings.cleanup_auto if cleanup is None else cleanup
        if cleanup:
            self.last_cleanup = self.cleaner.run(now=now)
        brief = self.build_brief(for_date, now)
        audio_path, voice_source, voice_error = "", "none", None
        want_voice = (self.settings.brief_call if voice is None else voice) and deliver
        if want_voice or (voice and not deliver):
            if self.notifier.voice is not None:
                try:
                    out = self.settings.audio_dir / f"brief-{brief.brief_date}.mp3"
                    info = self.notifier.synthesize(brief.script, out)
                    audio_path, voice_source = info["path"], "elevenlabs"
                except (VoiceError, ValueError) as exc:
                    voice_error = str(exc)
                    voice_source = "twilio-say"
            else:
                voice_source = "twilio-say"
        self.db.save_brief(brief.brief_date, markdown=brief.markdown, script=brief.script, sms=brief.sms,
                           stats=brief.stats, audio_path=audio_path, voice_source=voice_source)
        delivery: list[dict] = []
        if deliver:
            if self.settings.brief_sms:
                delivery.append(self._deliver(lambda: self.notifier.send_sms(brief.sms, purpose="brief"), "sms"))
            if want_voice:
                twiml_url = f"{self.settings.public_base_url.rstrip('/')}/webhooks/twilio/brief/{brief.brief_date}"
                delivery.append(self._deliver(lambda: self.notifier.place_call(twiml_url, purpose="brief", body=brief.sms), "call"))
            for rec in delivery:
                self.db.append_brief_delivery(brief.brief_date, rec)
        out = brief.as_dict()
        out.update({"audio_path": audio_path, "voice_source": voice_source, "voice_error": voice_error,
                    "delivery": delivery, "cleanup": self.last_cleanup if cleanup else None})
        return out

    @staticmethod
    def _deliver(fn: Callable[[], dict], kind: str) -> dict:
        try:
            rec = fn()
            return {"kind": kind, "ok": True, "status": rec.get("status"), "truth_label": rec.get("truth_label"),
                    "provider_sid": rec.get("provider_sid", ""), "to": rec.get("to_number")}
        except (NotifyError, DestinationError, ValueError) as exc:
            return {"kind": kind, "ok": False, "error": str(exc)}

    def brief_due(self, now: datetime | None = None) -> bool:
        """True when it is past the configured brief time today and no brief exists for today."""
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(self.settings.tz)
        if (local.hour, local.minute) < (self.settings.brief_hour, self.settings.brief_minute):
            return False
        return self.db.get_brief(local.date().isoformat()) is None

    # ------------------------------------------------------------ cleanup
    def cleanup(self, *, dry_run: bool = True, now: datetime | None = None, copy_to_server: bool | None = None) -> dict:
        plan = self.cleaner.plan(now=now)
        if dry_run:
            return {"dry_run": True, **plan.as_dict()}
        self.last_cleanup = self.cleaner.run(plan, now=now, copy_to_server=copy_to_server)
        return {"dry_run": False, **self.last_cleanup}

    # ------------------------------------------------------------ status
    def status(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        accounts = self.db.account_stats()
        enabled = [a for a in accounts if a["enabled"]]
        synced = [a for a in enabled if a["last_sync_at"] and not a["last_error"]]
        voice = self.notifier.voice_status()
        latest = self.db.latest_brief()
        local = now.astimezone(self.settings.tz)
        pending: list[str] = []
        if not enabled:
            pending.append("Authorize at least one mailbox (steph-email accounts add you@domain.com).")
        elif not synced:
            pending.append("A connected mailbox has not completed a clean sync yet (check accounts list for errors).")
        if not self.settings.owner_phone:
            pending.append("Set STEPH_EMAIL_OWNER_PHONE so alerts and the brief have somewhere to go.")
        if not self.notifier.is_live:
            pending.append("Add Twilio credentials (STEPH_EMAIL_TWILIO_*) to send real SMS / calls; currently STAGED.")
        if not voice.configured:
            pending.append("Optional: set STEPH_EMAIL_ELEVENLABS_API_KEY and _VOICE_ID for Stephanie's ElevenLabs voice on calls.")
        elif not voice.verified:
            pending.append("Verify voice playback: steph-email voice verify, then --confirm-playback after listening on the phone.")
        return {
            "version": __version__,
            "now_local": local.isoformat(timespec="minutes"),
            "owner": {"name": self.settings.owner_name, "phone_set": bool(self.settings.owner_phone)},
            "mailboxes": {"connected": len(accounts), "enabled": len(enabled), "healthy": len(synced),
                          "authorized": bool(synced), "errors": [{"address": a["address"], "error": a["last_error"]}
                                                                  for a in accounts if a["last_error"]]},
            "notifications": {"transport": self.notifier.transport.name, "truth_label": self.notifier.truth_label,
                              "live": self.notifier.is_live, "alerts_enabled": self.settings.alerts_enabled,
                              "alert_min_urgency": self.settings.alert_min_urgency},
            "voice": {"configured": voice.configured, "verified": voice.verified, "source": voice.source, "detail": voice.detail},
            "brief": {"scheduled_local": f"{self.settings.brief_hour:02d}:{self.settings.brief_minute:02d} {self.settings.timezone}",
                      "latest_date": latest["brief_date"] if latest else None,
                      "due_now": self.brief_due(now), "sms": self.settings.brief_sms, "call": self.settings.brief_call},
            "cleanup": {"after_days": self.settings.cleanup_after_days, "auto": self.settings.cleanup_auto,
                        "copy_to_server": self.settings.cleanup_copy_to_server, "runs": self.db.cleanup_runs(3),
                        "deletes": "never"},
            "activation_pending": pending,
            "live": not pending or pending == [p for p in pending if p.startswith("Optional")],
        }
