from datetime import timedelta
from zoneinfo import ZoneInfo
import json
import logging
import threading
import time
import uuid
from .models import Email, aware, utcnow, sender_matches
from .classification import classify

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, config, store, *, aggregator=None, notifier=None):
        from .aggregator import EmailAggregator
        from .notifier import Notifier
        self.config, self.store = config, store
        store.seed_settings(config.settings)
        self.aggregator = aggregator or EmailAggregator(config, store)
        self.notifier = notifier or Notifier(config, store)
        self.lock = threading.RLock()
        self.worker = None
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.initial_sync_done = not any(a.get("enabled") for a in config.accounts)
        self.last_sync_complete = self.initial_sync_done
        for account in config.accounts:
            store.record_account(account["id"], "staged" if account.get("enabled") else "disabled", "Awaiting authenticated sync" if account.get("enabled") else "Not enabled")
        # A process crash after provider submission cannot prove failure. Do not resend.
        with store.connect() as conn:
            conn.execute("UPDATE notifications SET status='uncertain',detail='Worker restarted during submission; reconcile with provider before retry' WHERE status='sending'")

    def process_email(self, email):
        settings = self.store.get_settings()
        now = utcnow()
        result = classify(email, self.config.rules, settings)
        eid = str(uuid.uuid4())
        received = email.received_at.isoformat()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT * FROM emails WHERE account=? AND folder=? AND uidvalidity=? AND uid=?", (email.account, email.folder, email.uidvalidity, email.uid)).fetchone()
            if prior:
                return {"id": prior["id"], "status": prior["status"], "duplicate": True}
            # A just-expired expectation can still match a delayed fetch received inside its window.
            matches = [dict(r) for r in conn.execute("SELECT * FROM expectations WHERE status IN ('pending','expired') AND created_at<=? AND expires_at>=?", (received, received))
                       if sender_matches(r["sender"], email.sender) and r["subject"] in email.subject.lower()]
            status = "expected" if matches else "quarantine" if result["is_spam"] else "urgent" if result["is_urgent"] else "normal"
            if matches and result["is_spam"]:
                result["reasons"].append("Expected email matched; inspect suspicious content before acting")
            conn.execute("INSERT INTO emails(id,account,folder,uidvalidity,uid,sender,subject,received_at,text,message_id,attachments,status,urgency_score,reasons,is_spam,is_urgent,processed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (eid, email.account, email.folder, email.uidvalidity, email.uid, email.sender[:1000], email.subject[:1000], received,
                          email.text[:20000], email.message_id[:1000], json.dumps(email.attachments), status, result["urgency_score"],
                          json.dumps(result["reasons"]), int(result["is_spam"]), int(result["is_urgent"]), now.isoformat()))
            channels = set()
            for match in matches:
                conn.execute("UPDATE expectations SET status='matched',matched_email_id=?,matched_at=? WHERE id=?", (eid, now.isoformat(), match["id"]))
                for channel in self.channels(match["notify"]):
                    conn.execute("UPDATE notifications SET status='preview',detail='Superseded: expected mail received within its window',updated_at=? WHERE dedupe_key=? AND status='pending'",
                                 (now.isoformat(), f"expired:{match['id']}:{channel}"))
                channels.update(self.channels(match["notify"]))
            if result["is_urgent"] and not result["is_spam"]:
                channels.update(self.channels(settings["notification_channel"]))
            # No message text, links or sender names leave the engine in the default notification.
            body = "Steph for NoblePort: " + ("An expected email has arrived" if matches else "An email needs urgent review") + ". Open your private email dashboard."
            age = (now - email.received_at).total_seconds()
            stale = age > settings["notification_max_age_hours"] * 3600 or age < -300
            for channel in sorted(channels):
                state = "preview" if settings["dry_run"] or settings["demo_mode"] or stale else "pending"
                detail = "Historical import: no live alert" if stale else "Preview mode" if state == "preview" else ""
                self.enqueue(conn, f"email:{eid}:{channel}", channel, body, now, email_id=eid, status=state, detail=detail)
            self.store.audit(conn, "email_processed", eid)
        return {"id": eid, "status": status, "duplicate": False, **result, "matched_expectations": len(matches)}

    @staticmethod
    def channels(value):
        return ("sms", "voice") if value == "both" else (value,)

    @staticmethod
    def enqueue(conn, dedupe_key, channel, body, now, *, email_id=None, status="pending", detail=""):
        conn.execute("INSERT OR IGNORE INTO notifications(id,dedupe_key,email_id,channel,body,status,detail,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), dedupe_key, email_id, channel, body, status, detail, now.isoformat(), now.isoformat()))

    @staticmethod
    def quiet(settings, now):
        current = now.astimezone(ZoneInfo(settings["timezone"])).strftime("%H:%M")
        start, end = settings["quiet_start"], settings["quiet_end"]
        return start <= current < end if start < end else (current >= start or current < end) if start != end else False

    def dispatch(self, now=None):
        now = aware(now) if now else utcnow()
        settings = self.store.get_settings()
        for _ in range(settings["max_alerts_per_hour"]):
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                item = conn.execute("SELECT n.*,e.received_at AS email_received_at FROM notifications n LEFT JOIN emails e ON e.id=n.email_id WHERE n.status='pending' ORDER BY n.created_at LIMIT 1").fetchone()
                if not item:
                    return
                row = dict(item)
                enabled = settings["sms_notifications"] if row["channel"] == "sms" else settings["voice_notifications"]
                stale = now - aware(row["email_received_at"] or row["created_at"]) > timedelta(hours=settings["notification_max_age_hours"])
                if row["dedupe_key"].startswith("expired:"):
                    expectation_id = row["dedupe_key"].split(":")[1]
                    expectation = conn.execute("SELECT status FROM expectations WHERE id=?", (expectation_id,)).fetchone()
                    if not expectation or expectation["status"] != "expired":
                        conn.execute("UPDATE notifications SET status='preview',detail='Superseded expectation alert',updated_at=? WHERE id=?", (now.isoformat(), row["id"]))
                        continue
                if settings["dry_run"] or settings["demo_mode"] or not enabled or stale:
                    detail = "Expired before dispatch" if stale else "Channel disabled" if not enabled else "Preview mode"
                    conn.execute("UPDATE notifications SET status='preview',detail=?,updated_at=? WHERE id=?", (detail, now.isoformat(), row["id"]))
                    continue
                if self.quiet(settings, now):
                    return
                used = conn.execute("SELECT count(*) FROM notifications WHERE status IN ('sending','submitted','uncertain','failed') AND updated_at>=?", ((now - timedelta(hours=1)).isoformat(),)).fetchone()[0]
                if used >= settings["max_alerts_per_hour"]:
                    return
                conn.execute("UPDATE notifications SET status='sending',updated_at=? WHERE id=?", (now.isoformat(), row["id"]))
            try:
                result = self.notifier.send(row["channel"], row["body"], row["id"])
                if result.get("status") not in ("preview", "submitted", "failed", "uncertain"):
                    raise ValueError("Invalid notification adapter status")
            except Exception:
                result = {"status": "uncertain", "detail": "Adapter interrupted; reconcile provider before retry", "provider_id": ""}
            with self.store.connect() as conn:
                conn.execute("UPDATE notifications SET status=?,provider_id=?,detail=?,updated_at=? WHERE id=?",
                             (result["status"], result.get("provider_id", ""), result.get("detail", ""), now.isoformat(), row["id"]))

    def run_cycle(self):
        if self.store.get_settings()["aggregation_enabled"] and not self.config.settings.get("demo_mode"):
            result = self.aggregator.run_cycle(self.process_email)
            self.initial_sync_done = True
            self.last_sync_complete = all(isinstance(v, dict) and v.get("status") != "error" and not v.get("backlog") for v in result.values())
            return result
        return {"status": "disabled"}

    def tick(self, now=None):
        now = aware(now) if now else utcnow()
        settings = self.store.get_settings()
        local = now.astimezone(ZoneInfo(settings["timezone"]))
        day = local.date().isoformat()
        with self.lock:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                expired = [dict(r) for r in conn.execute("SELECT * FROM expectations WHERE status='pending' AND expires_at<?", (now.isoformat(),))]
                for expectation in expired:
                    conn.execute("UPDATE expectations SET status='expired' WHERE id=?", (expectation["id"],))
                    for channel in self.channels(expectation["notify"]):
                        self.enqueue(conn, f"expired:{expectation['id']}:{channel}", channel,
                                     "Steph for NoblePort: An expected email is overdue. Review your follow-up list.", now,
                                     status="preview" if settings["dry_run"] or settings["demo_mode"] else "pending")
                sync_ready = self.initial_sync_done or not settings["aggregation_enabled"] or settings["demo_mode"]
                if sync_ready and local.strftime("%H:%M") >= settings["summary_time"]:
                    inserted = conn.execute("INSERT OR IGNORE INTO scheduled_runs VALUES(?,?,?)", ("summary", day, now.isoformat())).rowcount
                    if inserted:
                        since = (now - timedelta(hours=24)).isoformat()
                        counts = conn.execute("SELECT count(*),coalesce(sum(is_urgent AND NOT is_spam),0) FROM emails WHERE received_at>=?", (since,)).fetchone()
                        waiting = conn.execute("SELECT count(*) FROM expectations WHERE status='pending'").fetchone()[0]
                        body = f"Steph's NoblePort email brief: {counts[0]} imported emails received in 24 hours, {counts[1]} urgent, {waiting} expected emails pending. Review your dashboard."
                        if not self.last_sync_complete or not settings["aggregation_enabled"]:
                            body += " Mailbox coverage is incomplete; check connections."
                        for channel in self.channels(settings["notification_channel"]):
                            self.enqueue(conn, f"summary:{day}:{channel}", channel, body, now,
                                         status="preview" if settings["dry_run"] or settings["demo_mode"] else "pending")
                if settings["daily_cleanup"] and local.strftime("%H:%M") >= settings["cleanup_time"]:
                    inserted = conn.execute("INSERT OR IGNORE INTO scheduled_runs VALUES(?,?,?)", ("cleanup", day, now.isoformat())).rowcount
                    if inserted:
                        cutoff = (now - timedelta(days=settings["retention_days"])).isoformat()
                        conn.execute("UPDATE emails SET status='review' WHERE status='normal' AND received_at<?", (cutoff,))
                        self.store.audit(conn, "scheduled_cleanup_review_only", day)
            self.dispatch(now)
            drain = getattr(self.notifier, "drain_voice_events", None)
            if drain:
                drain()

    def start_background(self):
        if self.worker and self.worker.is_alive():
            return self.stop
        self.stop.clear()
        self.wakeup.set()

        def run():
            last_poll = 0.0
            while not self.stop.is_set():
                try:
                    due = time.monotonic() - last_poll >= self.store.get_settings()["poll_seconds"]
                    if due or self.wakeup.is_set():
                        self.wakeup.clear()
                        self.run_cycle()
                        last_poll = time.monotonic()
                except Exception as exc:
                    log.error("Engine cycle failed (%s); retrying next cycle", type(exc).__name__)
                    last_poll = time.monotonic()
                self.stop.wait(1)

        def run_notifications():
            # Voice callbacks must continue while a slow mailbox is synchronizing.
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    log.error("Notification cycle failed (%s)", type(exc).__name__)
                self.stop.wait(1)

        self.worker = threading.Thread(target=run, name="steph-engine", daemon=True)
        self.worker.start()
        self.notification_worker = threading.Thread(target=run_notifications, name="steph-notifications", daemon=True)
        self.notification_worker.start()
        if not self.config.settings.get("demo_mode"):
            self.aggregator.start_listeners(self.wakeup.set, self.stop)
        return self.stop

    def demo(self):
        if not self.config.settings.get("demo_mode"):
            raise ValueError("Demo mode is not enabled")
        if not self.store.list_expectations():
            self.store.add_expectation("drawings@example.com", "knee wall", 72, "both")
        now = utcnow()
        samples = [
            ("drawings@example.com", "DEMO · 4 Forest — knee wall drawings", "Synthetic message: revised drawings ready for your review."),
            ("field@example.com", "DEMO · 15 Warehouse — inspection due today", "Synthetic message: action required before insulation."),
            ("supplier@example.com", "DEMO · Decking credit update", "Synthetic message: credit request received."),
            ("offers@example.com", "DEMO · You have won — claim your prize", "Synthetic spam review example."),
            ("team@example.com", "DEMO · 37 Milk — mobilization notes", "Synthetic message: crew access and dumpster coordination."),
        ]
        for uid, (sender, subject, text) in enumerate(samples, 1):
            self.process_email(Email("demo", "INBOX", "1", str(uid), sender, subject, now, text))
        self.store.record_account("demo", "demo", "Synthetic examples; no real mailbox connected")
        return {"status": "demo", "examples": len(samples)}
