"""Outbound alerts with durable at-most-one local attempts and signed voice events.

An API acceptance is ``submitted``, never proof that a person received an alert.
An interrupted request stays ``uncertain`` and is not retried automatically.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import time
import uuid
from collections.abc import Mapping
from urllib.parse import quote, urlparse

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


PHONE = re.compile(r"\+[1-9]\d{7,14}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9:_=+./-]{1,512}\Z")


def result(status, provider_id="", detail=""):
    return {"status": status, "provider_id": provider_id, "detail": detail}


class Notifier:
    def __init__(self, config, store, *, session=None, clock=time.time):
        self.config, self.store, self.clock = config, store, clock
        self.http = session or requests.Session()
        # No HTTP retry adapter: mutating requests can succeed before a timeout.
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS voice_attempts (
                    notification_id TEXT NOT NULL, channel TEXT NOT NULL,
                    status TEXT NOT NULL, provider_id TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                    PRIMARY KEY(notification_id, channel));
                CREATE TABLE IF NOT EXISTS voice_calls (
                    notification_id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL,
                    call_id TEXT UNIQUE, body TEXT NOT NULL, created_at REAL NOT NULL,
                    destination TEXT NOT NULL, source TEXT NOT NULL, connection TEXT NOT NULL,
                    voice TEXT NOT NULL, voice_settings TEXT NOT NULL,
                    answered INTEGER NOT NULL DEFAULT 0, finished INTEGER NOT NULL DEFAULT 0,
                    terminated INTEGER NOT NULL DEFAULT 0,
                    speak_status TEXT NOT NULL DEFAULT 'pending',
                    hangup_status TEXT NOT NULL DEFAULT 'pending', detail TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS voice_events (
                    event_id TEXT PRIMARY KEY, notification_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    received_at REAL NOT NULL);
            """)

    def _settings(self):
        return {**self.config.settings, **self.store.get_settings()}

    def _gate(self, channel):
        settings = self._settings()
        if settings.get("dry_run", True) is not False:
            return "Dry-run preview; no provider request made."
        if settings.get("demo_mode", False) is not False:
            return "Demo mode; no provider request made."
        if settings.get(f"{channel}_notifications", False) is not True:
            return f"{channel.upper()} notifications disabled."
        return None

    @staticmethod
    def _env(name):
        return os.environ.get(name, "").strip()

    def _credentials(self, channel):
        destination = self._env("NOTIFY_PHONE")
        if not PHONE.fullmatch(destination):
            raise ValueError("Set NOTIFY_PHONE to the operator's E.164 number.")
        if channel == "sms":
            sid, token, source = (self._env(n) for n in
                                  ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"))
            if not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid) or not token or not PHONE.fullmatch(source):
                raise ValueError("Twilio credentials or sending number missing/invalid.")
            return {"destination": destination, "source": source, "sid": sid, "key": token}
        key, connection, source, webhook = (self._env(n) for n in
            ("TELNYX_API_KEY", "TELNYX_CONNECTION_ID", "TELNYX_FROM_NUMBER", "TELNYX_WEBHOOK_URL"))
        parsed = urlparse(webhook)
        if (not key or not connection or not PHONE.fullmatch(source) or parsed.scheme != "https"
                or not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment or not parsed.path.endswith("/webhooks/telnyx")):
            raise ValueError("Telnyx credentials, sending number or HTTPS webhook missing/invalid.")
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(self._env("TELNYX_PUBLIC_KEY"), validate=True))
        except (ValueError, binascii.Error):
            raise ValueError("TELNYX_PUBLIC_KEY must contain the account's base64 Ed25519 public key.") from None
        voice = self._env("TELNYX_VOICE") or "female"
        voice_settings = {}
        if voice.startswith("ElevenLabs."):
            secret_ref = self._env("TELNYX_ELEVENLABS_API_KEY_REF")
            if not secret_ref:
                raise ValueError("ElevenLabs voice requires TELNYX_ELEVENLABS_API_KEY_REF.")
            voice_settings = {"type": "elevenlabs", "api_key_ref": secret_ref}
        return dict(destination=destination, source=source, key=key, connection=connection,
                    webhook=webhook, voice=voice, voice_settings=voice_settings)

    def send(self, channel, body, notification_id):
        if channel not in ("sms", "voice"):
            return result("failed", detail="Unknown notification channel.")
        if not isinstance(body, str) or not body.strip() or len(body) > 1000:
            return result("failed", detail="Alert text must contain 1–1000 characters.")
        notification_id = str(notification_id)
        if not notification_id or len(notification_id) > 200:
            return result("failed", detail="Invalid notification identifier.")
        gate = self._gate(channel)
        if gate:
            return result("preview", detail=gate)
        try:
            creds = self._credentials(channel)
        except ValueError as exc:
            return result("failed", detail=str(exc))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT status,provider_id,detail FROM voice_attempts WHERE notification_id=? AND channel=?",
                             (notification_id, channel)).fetchone()
            if old:
                return result(*tuple(old))
            db.execute("INSERT INTO voice_attempts(notification_id,channel,status,detail,created_at) VALUES(?,?,'uncertain',?,?)",
                       (notification_id, channel, "Provider request claimed; outcome not yet confirmed.", self.clock()))
            if channel == "voice":
                token = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
                db.execute("""INSERT INTO voice_calls(notification_id,token,body,created_at,destination,source,connection,voice,voice_settings)
                              VALUES(?,?,?,?,?,?,?,?,?)""", (notification_id, token, body, self.clock(), creds["destination"],
                              creds["source"], creds["connection"], creds["voice"], json.dumps(creds["voice_settings"])))
        # No db transaction spans a provider call. A process crash retains uncertain.
        if channel == "sms":
            outcome = self._request(
                f"https://api.twilio.com/2010-04-01/Accounts/{creds['sid']}/Messages.json",
                auth=(creds["sid"], creds["key"]),
                data={"To": creds["destination"], "From": creds["source"], "Body": body,
                      "ValidityPeriod": "300"})
            if outcome["status"] == "submitted":
                payload = outcome.pop("response")
                sid = payload.get("sid", "")
                if not isinstance(sid, str) or not re.fullmatch(r"(?:SM|MM)[0-9a-fA-F]{32}", sid):
                    outcome = result("uncertain", detail="Twilio accepted request without a valid message identifier.")
                elif payload.get("status") in ("failed", "undelivered", "canceled"):
                    outcome = result("failed", sid, "Twilio reported message failure.")
                else:
                    outcome = result("submitted", sid, "Twilio accepted the request; delivery unverified.")
        else:
            outcome = self._request("https://api.telnyx.com/v2/calls",
                headers={"Authorization": f"Bearer {creds['key']}"},
                json={"connection_id": creds["connection"], "to": creds["destination"], "from": creds["source"],
                      "client_state": token, "command_id": self._command(notification_id, "dial"),
                      "webhook_url": creds["webhook"], "webhook_url_method": "POST",
                      "timeout_secs": 30, "time_limit_secs": 90})
            if outcome["status"] == "submitted":
                payload = outcome.pop("response").get("data", {})
                call_id = payload.get("call_control_id", "") if isinstance(payload, dict) else ""
                if not isinstance(call_id, str) or not SAFE_ID.fullmatch(call_id):
                    outcome = result("uncertain", detail="Telnyx accepted request without a valid call identifier.")
                else:
                    with self.store.connect() as db:
                        existing = db.execute("SELECT call_id FROM voice_calls WHERE notification_id=?", (notification_id,)).fetchone()[0]
                        if existing and existing != call_id:
                            outcome = result("uncertain", detail="Telnyx response and webhook correlation disagree.")
                        else:
                            db.execute("UPDATE voice_calls SET call_id=? WHERE notification_id=?", (call_id, notification_id))
                            outcome = result("submitted", call_id, "Telnyx accepted dial; waiting for signed answer event.")
        with self.store.connect() as db:
            db.execute("UPDATE voice_attempts SET status=?,provider_id=?,detail=? WHERE notification_id=? AND channel=?",
                       (outcome["status"], outcome["provider_id"], outcome["detail"], notification_id, channel))
        return outcome

    @staticmethod
    def _command(notification_id, action):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"steph-email/{notification_id}/{action}"))

    def _request(self, url, **kwargs):
        try:
            response = self.http.post(url, timeout=(5, 15), allow_redirects=False, **kwargs)
            if response.status_code >= 500 or response.status_code in (408,):
                return result("uncertain", detail=f"Provider HTTP {response.status_code}; reconcile before any new attempt.")
            if not 200 <= response.status_code < 300:
                return result("failed", detail=f"Provider rejected request (HTTP {response.status_code}).")
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Unexpected provider response")
            return {**result("submitted"), "response": data}
        except (requests.Timeout, requests.ConnectionError):
            return result("uncertain", detail="Provider connection interrupted; no automatic retry.")
        except (requests.RequestException, ValueError):
            return result("uncertain", detail="Provider response unconfirmed; no automatic retry.")

    def handle_telnyx_webhook(self, raw_body: bytes, headers: Mapping):
        """Verify exact signed bytes, correlate a local call and persist without network I/O."""
        if len(raw_body) > 65536:
            return {"error": "Payload too large"}, 413
        headers = {k.lower(): v for k, v in headers.items()}
        try:
            timestamp = headers["telnyx-timestamp"]
            if abs(self.clock() - int(timestamp)) > 300:
                raise ValueError("Expired signature")
            public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(self._env("TELNYX_PUBLIC_KEY"), validate=True))
            signature = base64.b64decode(headers["telnyx-signature-ed25519"], validate=True)
            public_key.verify(signature, timestamp.encode("ascii") + b"|" + raw_body)
            event = json.loads(raw_body)["data"]
            event_id, kind, payload = event["id"], event["event_type"], event["payload"]
            if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200 or not isinstance(payload, dict):
                raise ValueError("Malformed event")
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError, binascii.Error, InvalidSignature, UnicodeError):
            return {"error": "Invalid signature or event"}, 401
        if kind not in ("call.answered", "call.speak.ended", "call.hangup"):
            return {"status": "ignored"}, 200
        call_id, token = payload.get("call_control_id"), payload.get("client_state")
        if not isinstance(call_id, str) or not SAFE_ID.fullmatch(call_id) or not isinstance(token, str):
            return {"error": "Invalid call correlation"}, 400
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM voice_events WHERE event_id=?", (event_id,)).fetchone():
                return {"status": "duplicate"}, 200
            row = db.execute("SELECT * FROM voice_calls WHERE token=?", (token,)).fetchone()
            if not row:
                return {"status": "unrelated"}, 200
            row = dict(row)
            if (row["call_id"] and row["call_id"] != call_id) or self.clock() - row["created_at"] > 900:
                return {"error": "Call correlation mismatch or expired"}, 400
            # Answer events include phone numbers; speak.ended intentionally does not.
            correlations = [("connection_id", "connection")]
            if kind == "call.answered":
                correlations += [("to", "destination"), ("from", "source")]
            if any(str(payload.get(field, "")) != row[column] for field, column in correlations):
                return {"error": "Call correlation mismatch"}, 400
            db.execute("UPDATE voice_calls SET call_id=? WHERE notification_id=?", (call_id, row["notification_id"]))
            db.execute("INSERT INTO voice_events VALUES(?,?,?,?,?)", (event_id, row["notification_id"], kind,
                       str(event.get("occurred_at", ""))[:64], self.clock()))
            if kind == "call.hangup":
                db.execute("UPDATE voice_calls SET terminated=1 WHERE notification_id=?", (row["notification_id"],))
            elif kind == "call.answered":
                db.execute("UPDATE voice_calls SET answered=1 WHERE notification_id=?", (row["notification_id"],))
            elif kind == "call.speak.ended":
                db.execute("UPDATE voice_calls SET finished=1 WHERE notification_id=? AND speak_status!='pending'", (row["notification_id"],))
        return {"status": "queued"}, 200

    def drain_voice_events(self):
        """Called by the engine worker; persisted claims prevent replay and concurrent sends."""
        outcomes = []
        for _ in range(20):
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("""SELECT * FROM voice_calls WHERE terminated=0 AND call_id IS NOT NULL AND
                    ((answered=1 AND speak_status='pending') OR (finished=1 AND hangup_status='pending'))
                    ORDER BY created_at LIMIT 1""").fetchone()
                if not row:
                    break
                row = dict(row)
                action = "hangup" if row["finished"] else "speak"
                state_col = "hangup_status" if action == "hangup" else "speak_status"
                if self.clock() - row["created_at"] > 900:
                    db.execute(f"UPDATE voice_calls SET {state_col}='expired' WHERE notification_id=?", (row["notification_id"],))
                    continue
                gate = self._gate("voice") if action == "speak" else None
                if gate:
                    db.execute("UPDATE voice_calls SET speak_status='suppressed',detail=? WHERE notification_id=?", (gate, row["notification_id"]))
                    outcomes.append(result("preview", row["call_id"], gate))
                    continue
                # Mark uncertain before side effect. A worker crash never replays this command.
                db.execute(f"UPDATE voice_calls SET {state_col}='uncertain' WHERE notification_id=?", (row["notification_id"],))
            key = self._env("TELNYX_API_KEY")
            if not key:
                outcome = result("failed", detail="Telnyx API key missing.")
            else:
                body = {"command_id": self._command(row["notification_id"], action)}
                if action == "speak":
                    body.update(payload=row["body"], payload_type="text", voice=row["voice"], language="en-US",
                                service_level="basic" if row["voice"] in ("male", "female") else "premium")
                    voice_settings = json.loads(row["voice_settings"])
                    if voice_settings:
                        body["voice_settings"] = voice_settings
                outcome = self._request(f"https://api.telnyx.com/v2/calls/{quote(row['call_id'], safe='')}/actions/{action}",
                                        headers={"Authorization": f"Bearer {key}"}, json=body)
                accepted = outcome.pop("response", {})
                data = accepted.get("data", {})
                if outcome["status"] == "submitted" and (not isinstance(data, dict) or data.get("result") != "ok"):
                    outcome = result("uncertain", detail="Telnyx command result unconfirmed; no automatic retry.")
            with self.store.connect() as db:
                db.execute(f"UPDATE voice_calls SET {state_col}=?,detail=? WHERE notification_id=?",
                           (outcome["status"], outcome["detail"], row["notification_id"]))
            outcomes.append({**outcome, "provider_id": row["call_id"]})
        return outcomes

    def list_voice_status(self):
        """Operator diagnostics omit destination, credentials, tokens and alert bodies."""
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("""SELECT notification_id,answered,finished,terminated,
                speak_status,hangup_status,detail FROM voice_calls ORDER BY created_at DESC LIMIT 100""")]
