"""Read-only, bounded IMAP ingestion with durable commit-after-processing cursors."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser
import logging
import sqlite3
import ssl
import threading
import time

from imapclient import IMAPClient

from .models import Email
from .oauth import AuthenticationError, TokenProvider, secret_env

LOG = logging.getLogger(__name__)
UTC = timezone.utc


def bounded_int(account, name, default, low, high):
    return max(low, min(high, int(account.get(name, default))))


def safe_text(value, limit=1000):
    return " ".join(str(value or "").replace("\x00", "").split())[:limit]


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.suppressed += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.suppressed:
            self.suppressed -= 1

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def parse_message(account, folder, validity, uid, raw, received_at,
                  *, omitted_size=None) -> Email:
    """Parse bounded bytes only; attachment content is never saved or executed."""
    if not isinstance(received_at, datetime) or received_at.tzinfo is None:
        raise ValueError("IMAP INTERNALDATE must include timezone")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    text_parts, html_parts, attachments = [], [], []
    stack, count = [(message, 0)], 0
    while stack and count < 100:
        part, depth = stack.pop()
        count += 1
        if depth > 20:
            attachments.append({"omitted": True, "reason": "MIME depth limit"})
            continue
        filename = part.get_filename()
        content_type = part.get_content_type()
        if part.get_content_disposition() == "attachment" or filename:
            attachments.append({"filename": safe_text(filename, 255),
                                "content_type": content_type, "size": None})
            continue
        if part.is_multipart():
            stack.extend((child, depth + 1) for child in reversed(part.get_payload()))
            continue
        if content_type in ("text/plain", "text/html"):
            content = part.get_payload(decode=True) or b""
            try:
                decoded = content[:65536].decode(part.get_content_charset() or "utf-8", "replace")
            except LookupError:
                decoded = content[:65536].decode("utf-8", "replace")
            (text_parts if content_type == "text/plain" else html_parts).append(decoded)
    if stack:
        attachments.append({"omitted": True, "reason": "MIME part limit"})
    body = "\n".join(text_parts)[:20000]
    if not body and html_parts:
        parser = PlainHTML()
        parser.feed("\n".join(html_parts)[:65536])
        body = " ".join(parser.parts)[:20000]
    if omitted_size is not None:
        body = "[Body omitted: message exceeds ingestion size limit. Review original mailbox.]"
        attachments.append({"omitted": True, "reason": "Message size limit",
                            "size": omitted_size})
    return Email(account=account, folder=folder, uidvalidity=str(validity), uid=str(uid),
                 sender=safe_text(parseaddr(str(message.get("From", "")))[1], 320),
                 subject=safe_text(message.get("Subject", "")),
                 received_at=received_at.astimezone(UTC), text=body,
                 message_id=safe_text(message.get("Message-ID", "")), attachments=attachments)


class EmailAggregator:
    def __init__(self, config, store, *, client_factory=None, token_provider=None):
        self.config, self.store = config, store
        self.client_factory = client_factory or IMAPClient
        self.tokens = token_provider or TokenProvider(config.data_dir)
        self._cycle_lock = threading.Lock()
        self._listener_lock = threading.Lock()
        self._listeners = []
        config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_path = config.data_dir / "ingestion.db"
        with sqlite3.connect(self.state_path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS initial_sync (
                account TEXT, folder TEXT, validity TEXT, snapshot INTEGER,
                cutoff TEXT, PRIMARY KEY(account,folder,validity))""")
            db.execute("""CREATE TABLE IF NOT EXISTS mailbox_bindings (
                account TEXT PRIMARY KEY, host TEXT NOT NULL, port INTEGER NOT NULL,
                username TEXT NOT NULL)""")

    def _bind_source(self, account):
        """A logical account ID may never silently become a different mailbox."""
        host = str(account["host"]).lower().rstrip(".")
        port = int(account.get("port", 993))
        username = str(account["username"])
        with sqlite3.connect(self.state_path) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT host,port,username FROM mailbox_bindings WHERE account=?",
                                  (account["id"],)).fetchone()
            if previous is not None and previous != (host, port, username):
                raise AuthenticationError("Account id is bound to a different mailbox; use a new account id")
            db.execute("INSERT OR IGNORE INTO mailbox_bindings VALUES(?,?,?,?)",
                       (account["id"], host, port, username))

    @contextmanager
    def _connect(self, account):
        auth = account.get("auth", {})
        if account.get("provider") == "outlook" and auth.get("type") != "oauth2":
            raise AuthenticationError("Outlook account requires OAuth2")
        # Resolve secrets before opening a network socket.
        if auth.get("type") == "oauth2":
            credential = self.tokens.access_token(account)
        elif auth.get("type") == "app_password":
            credential = secret_env(auth.get("password_env"))
        else:
            raise AuthenticationError("Configure OAuth2 or an app-password environment reference")
        client = self.client_factory(account["host"], port=int(account.get("port", 993)),
                                     use_uid=True, ssl=True, ssl_context=ssl.create_default_context(),
                                     timeout=bounded_int(account, "timeout_seconds", 30, 5, 60))
        client.normalise_times = False
        try:
            if auth["type"] == "oauth2":
                client.oauth2_login(account["username"], credential)
            else:
                client.login(account["username"], credential)
            yield client
        finally:
            try:
                client.logout()
            except Exception:
                try:
                    client.shutdown()
                except Exception:
                    pass

    def _initial_state(self, account, folder, validity, snapshot, cursor):
        with sqlite3.connect(self.state_path) as db:
            row = db.execute("SELECT snapshot,cutoff FROM initial_sync WHERE account=? AND folder=? AND validity=?",
                             (account["id"], folder, validity)).fetchone()
            if row:
                return row[0], datetime.fromisoformat(row[1])
            if cursor and str(cursor["uidvalidity"]) == validity:
                return 0, datetime.min.replace(tzinfo=UTC)
            cutoff = datetime.now(UTC) - timedelta(days=bounded_int(account, "lookback_days", 30, 1, 3650))
            db.execute("INSERT INTO initial_sync VALUES(?,?,?,?,?)",
                       (account["id"], folder, validity, snapshot, cutoff.isoformat()))
            return snapshot, cutoff

    def _account_cycle(self, account, on_email):
        folder, identity = account.get("folder", "INBOX"), account["id"]
        processed = omitted = 0
        self._bind_source(account)
        with self._connect(account) as client:
            selected = client.select_folder(folder, readonly=True)
            validity = str(int(selected[b"UIDVALIDITY"]))
            snapshot = int(selected[b"UIDNEXT"]) - 1
            cursor = self.store.get_cursor(identity, folder)
            last = int(cursor["last_uid"]) if cursor and str(cursor["uidvalidity"]) == validity else 0
            initial_high, cutoff = self._initial_state(account, folder, validity, snapshot, cursor)
            batch_size = bounded_int(account, "batch_size", 100, 1, 500)
            window_size = bounded_int(account, "uid_scan_window", 10000, 100, 100000)
            max_windows = bounded_int(account, "scan_windows_per_cycle", 20, 1, 100)
            cap = bounded_int(account, "max_message_bytes", 2097152, 65536, 10485760)
            for _ in range(max_windows):
                if last >= snapshot or processed >= batch_size:
                    break
                high = min(snapshot, last + window_size)
                # Split at the initial snapshot so newly appended mail is never date-filtered.
                if last < initial_high:
                    high = min(high, initial_high)
                criteria = ["UID", f"{last + 1}:{high}"]
                if high <= initial_high:
                    criteria += ["SINCE", (cutoff - timedelta(days=1)).date()]
                uids = sorted({int(uid) for uid in client.search(criteria) if last < int(uid) <= high})
                truncated = len(uids) > batch_size - processed
                for uid in uids[:batch_size - processed]:
                    metadata = client.fetch([uid], ["RFC822.SIZE", "INTERNALDATE"]).get(uid)
                    if not metadata:  # Concurrently expunged messages cannot be retrieved.
                        self.store.set_cursor(identity, folder, validity, uid)
                        last = uid
                        continue
                    received = metadata.get(b"INTERNALDATE")
                    if not isinstance(received, datetime) or received.tzinfo is None:
                        raise ValueError("Missing trusted arrival timestamp")
                    if uid <= initial_high and received.astimezone(UTC) < cutoff:
                        self.store.set_cursor(identity, folder, validity, uid)
                        last = uid
                        continue
                    size = int(metadata[b"RFC822.SIZE"])
                    if size < 0:
                        raise ValueError("Invalid message size")
                    oversize = size > cap
                    selector = "BODY.PEEK[HEADER]<0.65536>" if oversize else f"BODY.PEEK[]<0.{cap}>"
                    fetched = client.fetch([uid], [selector]).get(uid)
                    if not fetched:
                        self.store.set_cursor(identity, folder, validity, uid)
                        last = uid
                        continue
                    prefix = b"BODY[HEADER]" if oversize else b"BODY[]"
                    raw = next((value for key, value in fetched.items()
                                if isinstance(key, bytes) and key.startswith(prefix)), None)
                    if not isinstance(raw, bytes) or len(raw) > cap:
                        raise ValueError("Invalid bounded message response")
                    email = parse_message(identity, folder, validity, uid, raw, received,
                                          omitted_size=size if oversize else None)
                    on_email(email)  # Callback must commit; failure leaves cursor untouched.
                    self.store.set_cursor(identity, folder, validity, uid)
                    last, processed = uid, processed + 1
                    omitted += int(oversize)
                if truncated:
                    break
                self.store.set_cursor(identity, folder, validity, high)
                last = high
            if snapshot == 0:
                self.store.set_cursor(identity, folder, validity, 0)
        detail = f"Read-only sync; {processed} processed; {omitted} header-only; cursor {last}/{snapshot}"
        self.store.record_account(identity, "connected", detail)
        return {"processed": processed, "header_only": omitted, "backlog": last < snapshot}

    def run_cycle(self, on_email):
        results = {}
        with self._cycle_lock:
            settings = {**self.config.settings, **self.store.get_settings()}
            if not settings.get("aggregation_enabled", True):
                return {"status": "disabled"}
            for account in self.config.accounts:
                identity = account["id"]
                if not account.get("enabled", False):
                    self.store.record_account(identity, "disabled", "Account is not enabled")
                    continue
                try:
                    results[identity] = self._account_cycle(account, on_email)
                except Exception as exc:
                    # Never expose server responses; they may contain tokens or mail text.
                    detail = str(exc) if isinstance(exc, AuthenticationError) else "Mailbox sync failed; check configuration, authorization, or processing"
                    self.store.record_account(identity, "error", detail)
                    LOG.warning("Mailbox sync failed for configured account %s (%s)", identity, type(exc).__name__)
                    results[identity] = {"status": "error", "detail": detail}
        return results

    def _idle_loop(self, account, on_wakeup, stop_event):
        backoff = 1
        while not stop_event.is_set():
            settings = {**self.config.settings, **self.store.get_settings()}
            if not settings.get("aggregation_enabled", True):
                stop_event.wait(5)
                continue
            try:
                self._bind_source(account)
                with self._connect(account) as client:
                    client.select_folder(account.get("folder", "INBOX"), readonly=True)
                    if not client.has_capability("IDLE"):
                        return  # Scheduled UID polling remains available.
                    connected_at = time.monotonic()
                    on_wakeup()  # Catch mail received while disconnected.
                    session_end = time.monotonic() + 1200
                    while not stop_event.is_set() and time.monotonic() < session_end:
                        settings = {**self.config.settings, **self.store.get_settings()}
                        if not settings.get("aggregation_enabled", True):
                            break
                        client.idle()
                        events = []
                        try:
                            while not stop_event.is_set() and time.monotonic() < session_end:
                                events = client.idle_check(timeout=5)
                                settings = {**self.config.settings, **self.store.get_settings()}
                                if events or not settings.get("aggregation_enabled", True):
                                    break
                        finally:
                            _, tail = client.idle_done()
                            events.extend(tail)
                        if any(len(event) > 1 and event[1] in (b"EXISTS", b"RECENT") for event in events):
                            on_wakeup()
                        if time.monotonic() - connected_at > 60:
                            backoff = 1
            except Exception:
                LOG.warning("IMAP listener disconnected for configured account %s", account["id"])
                if stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2, 60)

    def start_listeners(self, on_wakeup, stop_event):
        """Wake the engine's event; never process mail on the IDLE connection."""
        with self._listener_lock:
            if any(thread.is_alive() for thread in self._listeners):
                return self._listeners
            self._listeners = []
            for account in self.config.accounts:
                if account.get("enabled", False) and account.get("idle_enabled", True):
                    thread = threading.Thread(target=self._idle_loop,
                        args=(account, on_wakeup, stop_event), name=f"imap-idle-{account['id']}", daemon=True)
                    thread.start()
                    self._listeners.append(thread)
            return self._listeners
