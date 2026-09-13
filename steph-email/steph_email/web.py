"""Authenticated, local-first email operating dashboard.

Mail content is untrusted text. No route sends email, deletes remote mail, or
directly dispatches a notification.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta
import hmac
import os
import re
import secrets
import time

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for

BOOL_SETTINGS = {
    "aggregation_enabled", "spam_filtering", "urgency_detection",
    "sms_notifications", "voice_notifications", "daily_cleanup",
}
INT_SETTINGS = {
    "retention_days": (1, 3650), "urgency_threshold": (1, 100),
    "max_alerts_per_hour": (1, 60), "notification_max_age_hours": (1, 24),
    "poll_seconds": (15, 3600),
}
TIME_SETTINGS = {"quiet_start", "quiet_end", "summary_time", "cleanup_time"}
EDITABLE = BOOL_SETTINGS | set(INT_SETTINGS) | TIME_SETTINGS | {"notification_channel"}


def _secret_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def create_app(engine, store, config) -> Flask:
    token = os.environ.get("STEPH_DASHBOARD_TOKEN", "")
    secret = os.environ.get("STEPH_SESSION_SECRET", "")
    if len(token) < 32 or len(secret) < 32:
        raise ValueError("STEPH_DASHBOARD_TOKEN and STEPH_SESSION_SECRET must each contain at least 32 characters")
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret,
        MAX_CONTENT_LENGTH=16 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.environ.get("STEPH_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"},
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    failures = defaultdict(deque)

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def valid_csrf():
        provided = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf_token", "")
        expected = session.get("csrf", "")
        return bool(expected and provided and _secret_equal(expected, provided))

    def json_object():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("A JSON object is required")
        return data

    @app.before_request
    def authenticate():
        if request.endpoint in {"health", "login", "static", "telnyx_webhook"}:
            return None
        authorization = request.headers.get("Authorization", "")
        g.bearer_auth = authorization.startswith("Bearer ") and _secret_equal(authorization[7:], token)
        if not g.bearer_auth and not session.get("authenticated"):
            if request.path.startswith("/api/") or request.method != "GET":
                return jsonify(error="Authentication required"), 401
            return redirect(url_for("login"))
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not g.bearer_auth and not valid_csrf():
            return jsonify(error="Invalid CSRF token; refresh the dashboard and retry"), 403

    @app.after_request
    def secure_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(ValueError)
    def invalid_input(error):
        return jsonify(error=str(error)), 400

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            if not valid_csrf():
                return render_template("login.html", csrf=csrf_token(), error="Refresh this page and try again."), 403
            address = request.remote_addr or "unknown"
            attempts = failures[address]
            now = time.monotonic()
            while attempts and now - attempts[0] > 300:
                attempts.popleft()
            if len(attempts) >= 10:
                return render_template("login.html", csrf=csrf_token(), error="Too many attempts. Try again in five minutes."), 429
            candidate = request.form.get("token", "")
            if _secret_equal(candidate, token):
                failures.pop(address, None)
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                csrf_token()
                return redirect(url_for("dashboard"))
            attempts.append(now)
            error = "That access key was not recognized."
        return render_template("login.html", csrf=csrf_token(), error=error), 401 if error else 200

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", csrf=csrf_token(), demo_mode=bool(config.settings.get("demo_mode", False)))

    @app.get("/api/stats")
    def stats():
        return jsonify(store.stats())

    @app.get("/api/emails")
    def emails():
        return jsonify(store.list_emails(limit=100))

    @app.route("/api/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            data = json_object()
            if set(data) - EDITABLE:
                raise ValueError("Unrecognized or read-only settings")
            for key, value in data.items():
                if key in BOOL_SETTINGS and type(value) is not bool:
                    raise ValueError(f"{key} must be true or false")
                if key in INT_SETTINGS:
                    low, high = INT_SETTINGS[key]
                    if type(value) is not int or not low <= value <= high:
                        raise ValueError(f"{key} must be an integer between {low} and {high}")
                if key in TIME_SETTINGS and (not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)):
                    raise ValueError(f"{key} must use HH:MM, in 24-hour time")
                if key == "notification_channel" and (not isinstance(value, str) or value not in {"sms", "voice", "both"}):
                    raise ValueError("Choose sms, voice, or both")
            store.update_settings(data)
        visible = EDITABLE | {"dry_run", "timezone", "demo_mode"}
        return jsonify({key: value for key, value in store.get_settings().items() if key in visible})

    @app.route("/api/expectations", methods=["GET", "POST"])
    def expectations():
        if request.method == "GET":
            return jsonify(store.list_expectations())
        data = json_object()
        sender, subject = data.get("sender", ""), data.get("subject", "")
        if not isinstance(sender, str) or not isinstance(subject, str):
            raise ValueError("Sender and subject must be text")
        sender, subject = sender.strip().lower(), subject.strip()
        if not sender or len(sender) > 254 or len(subject) > 200 or not subject:
            raise ValueError("Enter a sender email or domain and a subject phrase")
        if any(char.isspace() for char in sender) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~@-]+", sender):
            raise ValueError("Use an exact sender email or domain, without a display name")
        hours, notify = data.get("hours", 72), data.get("notify", "sms")
        if type(hours) is not int or not 1 <= hours <= 2160 or not isinstance(notify, str) or notify not in {"sms", "voice", "both"}:
            raise ValueError("Use 1–2160 hours and choose sms, voice, or both")
        expectation_id = store.add_expectation(sender, subject, hours=hours, notify=notify)
        return jsonify(id=expectation_id, status="created"), 201

    @app.post("/api/expectations/<expectation_id>/cancel")
    def cancel_expectation(expectation_id):
        if not store.cancel_expectation(expectation_id):
            return jsonify(error="Pending watch not found"), 404
        return jsonify(status="cancelled")

    @app.get("/api/notifications")
    def notifications():
        return jsonify(store.list_notifications())

    @app.get("/api/accounts")
    def accounts():
        return jsonify(store.list_accounts())

    @app.post("/api/cleanup")
    def cleanup():
        retention = store.get_settings().get("retention_days", 90)
        return jsonify(store.cleanup_review(retention))

    @app.post("/api/emails/<email_id>/restore")
    def restore_email(email_id):
        if not store.restore_email(email_id):
            return jsonify(error="Message is not in the review queue"), 404
        return jsonify(status="restored")

    @app.post("/api/demo")
    def demo():
        if not config.settings.get("demo_mode", False):
            return jsonify(error="Demo is disabled"), 403
        engine.demo()
        return jsonify(status="loaded", data="synthetic")

    @app.post("/webhooks/telnyx")
    def telnyx_webhook():
        result, status_code = engine.notifier.handle_telnyx_webhook(request.get_data(cache=False), request.headers)
        return jsonify(result), status_code

    return app
