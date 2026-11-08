from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import os
import sqlite3
import uuid
from .config import DEFAULTS, validate_settings
from .models import utcnow, sender_pattern

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS emails(
 id TEXT PRIMARY KEY, account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity TEXT NOT NULL,
 uid TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL, received_at TEXT NOT NULL,
 text TEXT NOT NULL, message_id TEXT NOT NULL, attachments TEXT NOT NULL,
 status TEXT NOT NULL, urgency_score INTEGER NOT NULL, reasons TEXT NOT NULL,
 is_spam INTEGER NOT NULL, is_urgent INTEGER NOT NULL, processed_at TEXT NOT NULL,
 UNIQUE(account,folder,uidvalidity,uid));
CREATE INDEX IF NOT EXISTS emails_received ON emails(received_at DESC);
CREATE TABLE IF NOT EXISTS expectations(
 id TEXT PRIMARY KEY,sender TEXT NOT NULL,subject TEXT NOT NULL,notify TEXT NOT NULL,
 created_at TEXT NOT NULL,expires_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
 matched_email_id TEXT,matched_at TEXT);
CREATE TABLE IF NOT EXISTS notifications(
 id TEXT PRIMARY KEY,dedupe_key TEXT UNIQUE NOT NULL,email_id TEXT,channel TEXT NOT NULL,
 body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',provider_id TEXT NOT NULL DEFAULT '',
 detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS notifications_status ON notifications(status);
CREATE TABLE IF NOT EXISTS cursors(
 account TEXT NOT NULL,folder TEXT NOT NULL,uidvalidity TEXT NOT NULL,last_uid INTEGER NOT NULL,
 PRIMARY KEY(account,folder));
CREATE TABLE IF NOT EXISTS accounts(account TEXT PRIMARY KEY,status TEXT NOT NULL,detail TEXT NOT NULL,checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scheduled_runs(task TEXT NOT NULL,day TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(task,day));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT NOT NULL,entity_id TEXT NOT NULL,created_at TEXT NOT NULL);
"""


class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self.path = self.data_dir / "emails.db"
        with self.connect() as conn:
            conn.executescript(SCHEMA)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def seed_settings(self, settings):
        with self.connect() as conn:
            for key, value in {**DEFAULTS, **settings}.items():
                conn.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (key, json.dumps(value)))
            # Runtime safety mode cannot be silently retained from an older database.
            for key in ("dry_run", "demo_mode"):
                conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(settings.get(key, DEFAULTS[key]))))

    def get_settings(self):
        with self.connect() as conn:
            return {**DEFAULTS, **{r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT * FROM settings")}}

    def update_settings(self, settings):
        validate_settings(settings)
        if "dry_run" in settings:
            raise ValueError("Delivery mode must be configured at startup")
        with self.connect() as conn:
            for key, value in settings.items():
                conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))
            self.audit(conn, "settings_updated", ",".join(sorted(settings)))

    @staticmethod
    def audit(conn, action, entity_id):
        conn.execute("INSERT INTO audit(action,entity_id,created_at) VALUES(?,?,?)", (action, entity_id, utcnow().isoformat()))

    @staticmethod
    def decode_email(row):
        data = dict(row)
        for key in ("reasons", "attachments"):
            data[key] = json.loads(data[key])
        for key in ("is_spam", "is_urgent"):
            data[key] = bool(data[key])
        return data

    def list_emails(self, limit=100):
        with self.connect() as conn:
            return [self.decode_email(r) for r in conn.execute("SELECT * FROM emails ORDER BY received_at DESC LIMIT ?", (max(1, min(int(limit), 500)),))]

    def stats(self):
        local = utcnow().astimezone(ZoneInfo(self.get_settings()["timezone"]))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(utcnow().tzinfo).isoformat()
        with self.connect() as conn:
            today = conn.execute("SELECT count(*),coalesce(sum(is_spam),0),coalesce(sum(is_urgent),0) FROM emails WHERE processed_at>=?", (start,)).fetchone()
            pending = conn.execute("SELECT count(*) FROM expectations WHERE status='pending' AND expires_at>?", (utcnow().isoformat(),)).fetchone()[0]
            review = conn.execute("SELECT count(*) FROM emails WHERE status IN ('quarantine','review')").fetchone()[0]
        return {"total_processed": today[0], "spam_blocked": today[1], "spam_flagged": today[1], "urgent_flagged": today[2], "expected_pending": pending, "cleanup_review": review}

    def add_expectation(self, sender, subject, hours=72, notify="sms"):
        sender = sender_pattern(sender)
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 200:
            raise ValueError("Subject keyword is required (maximum 200 characters)")
        if type(hours) is not int or not 1 <= hours <= 2160:
            raise ValueError("Expectation window must be 1–2160 hours")
        if notify not in ("sms", "voice", "both"):
            raise ValueError("Invalid notification channel")
        now, eid = utcnow(), str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute("INSERT INTO expectations(id,sender,subject,notify,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                         (eid, sender, subject.strip().lower(), notify, now.isoformat(), (now + timedelta(hours=hours)).isoformat()))
            self.audit(conn, "expectation_created", eid)
        return eid

    def list_expectations(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM expectations ORDER BY created_at DESC LIMIT 500")]

    def cancel_expectation(self, eid):
        with self.connect() as conn:
            changed = conn.execute("UPDATE expectations SET status='cancelled' WHERE id=? AND status='pending'", (eid,)).rowcount
            self.audit(conn, "expectation_cancelled", eid)
        return bool(changed)

    def list_notifications(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 200")]

    def restore_email(self, eid):
        with self.connect() as conn:
            changed = conn.execute("UPDATE emails SET status=CASE WHEN is_urgent=1 THEN 'urgent' ELSE 'normal' END WHERE id=? AND status IN ('quarantine','review')", (eid,)).rowcount
            self.audit(conn, "email_restored_locally", eid)
        return bool(changed)

    def cleanup_review(self, retention_days):
        if type(retention_days) is not int or not 1 <= retention_days <= 3650:
            raise ValueError("Invalid retention period")
        cutoff = (utcnow() - timedelta(days=retention_days)).isoformat()
        with self.connect() as conn:
            count = conn.execute("UPDATE emails SET status='review' WHERE status='normal' AND received_at<?", (cutoff,)).rowcount
            self.audit(conn, "cleanup_review_only", str(count))
        return {"reviewed": count, "deleted": 0, "mailbox_changes": 0}

    def record_account(self, account, status, detail=""):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO accounts VALUES(?,?,?,?)", (account, status, detail[:300], utcnow().isoformat()))

    def list_accounts(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY account")]

    def get_cursor(self, account, folder):
        with self.connect() as conn:
            r = conn.execute("SELECT uidvalidity,last_uid FROM cursors WHERE account=? AND folder=?", (account, folder)).fetchone()
            return dict(r) if r else None

    def set_cursor(self, account, folder, uidvalidity, last_uid):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO cursors VALUES(?,?,?,?)", (account, folder, str(uidvalidity), int(last_uid)))
