"""Boundary tests: authentication, CSRF, persistent controls, and mail safety."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import re

import pytest

from steph_email.config import Config
from steph_email.storage import Store
from steph_email.web import create_app

TOKEN = "dashboard-test-token-" + "a" * 32
SECRET = "session-test-secret-" + "b" * 32


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("STEPH_DASHBOARD_TOKEN", TOKEN)
    monkeypatch.setenv("STEPH_SESSION_SECRET", SECRET)
    config = Config(data_dir=tmp_path)
    store = Store(tmp_path)
    store.seed_settings(config.settings)
    engine = SimpleNamespace(demo=Mock(), notifier=SimpleNamespace(handle_telnyx_webhook=Mock(return_value=({"error": "Invalid signature"}, 401))))
    app = create_app(engine, store, config)
    app.config["TESTING"] = True
    return app, store, engine, config


def login(client):
    page = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    result = client.post("/login", data={"token": TOKEN, "csrf_token": csrf})
    assert result.status_code == 302
    with client.session_transaction() as session:
        return session["csrf"]


def test_authentication_blocks_mail_and_mutation(setup):
    app, _, _, _ = setup
    client = app.test_client()
    assert client.get("/health").json == {"status": "ok"}
    for route in ("/api/emails", "/api/stats", "/api/settings", "/api/expectations", "/api/notifications", "/api/accounts"):
        assert client.get(route).status_code == 401
    assert client.post("/api/cleanup", json={}).status_code == 401
    assert client.get("/").status_code == 302
    assert client.get("/api/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/stats", headers={"Authorization": "Bearer 💥"}).status_code == 401


def test_session_csrf_and_persistent_settings(setup):
    app, store, _, _ = setup
    client = app.test_client()
    csrf = login(client)
    assert client.post("/api/settings", json={"urgency_threshold": 62}).status_code == 403
    response = client.post("/api/settings", json={"urgency_threshold": 62, "voice_notifications": False}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert Store(store.data_dir).get_settings()["urgency_threshold"] == 62
    assert client.post("/api/settings", json={"dry_run": False}, headers={"X-CSRF-Token": csrf}).status_code == 400
    assert store.get_settings()["dry_run"] is True
    assert client.post("/logout", headers={"X-CSRF-Token": csrf}).status_code == 302
    assert client.get("/api/stats").status_code == 401


@pytest.mark.parametrize("payload", [[], {"voice_notifications": "false"}, {"max_alerts_per_hour": True}, {"notification_channel": []}, {"quiet_start": "99:90"}, {"timezone": "Europe/London"}, {"arbitrary": "value"}])
def test_invalid_controls_rejected_atomically(setup, payload):
    app, store, _, _ = setup
    before = store.get_settings()
    response = app.test_client().post("/api/settings", json=payload, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 400
    assert store.get_settings() == before


def test_login_csrf_and_bad_unicode_key(setup):
    app, _, _, _ = setup
    client = app.test_client()
    assert client.post("/login", data={"token": TOKEN}).status_code == 403
    client.get("/login")
    with client.session_transaction() as session:
        csrf = session["csrf"]
    assert client.post("/login", data={"token": "💥", "csrf_token": csrf}).status_code == 401


def test_expectation_persistence_validation_and_cancel(setup):
    app, store, _, _ = setup
    client = app.test_client()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = client.post("/api/expectations", json={"sender": "foreman@example.com", "subject": "Drawings", "hours": 24, "notify": "both"}, headers=headers)
    assert response.status_code == 201
    assert store.list_expectations()[0]["subject"] == "drawings"
    assert client.post("/api/expectations", json={"sender": "foreman@example.com", "subject": "drawings", "notify": []}, headers=headers).status_code == 400
    assert client.post(f"/api/expectations/{response.json['id']}/cancel", json={}, headers=headers).status_code == 200
    assert store.list_expectations()[0]["status"] == "cancelled"
    assert client.post("/api/expectations/missing/cancel", json={}, headers=headers).status_code == 404
    assert client.post("/api/emails/missing/restore", json={}, headers=headers).status_code == 404


def test_mail_text_is_json_and_safe_dom_rendering(setup, monkeypatch):
    app, store, _, _ = setup
    payload = '<img src=x onerror="window.hacked=true"><script>alert(1)</script>'
    monkeypatch.setattr(store, "list_emails", lambda limit: [{"subject": payload, "sender": payload, "text": payload}])
    client = app.test_client()
    csrf = login(client)
    page = client.get("/")
    assert page.status_code == 200 and payload not in page.text
    assert client.get("/api/emails").json[0]["subject"] == payload
    assert client.get("/api/emails").mimetype == "application/json"
    script = client.get("/static/dashboard.js").text
    assert "innerHTML" not in script and "textContent" in script
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert page.headers["Cache-Control"] == "no-store"
    assert "HttpOnly" in page.headers.get("Set-Cookie", "") or csrf


def test_cleanup_only_reviews_and_demo_is_gated(setup):
    app, _, engine, config = setup
    client = app.test_client()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = client.post("/api/cleanup", json={}, headers=headers)
    assert response.json["deleted"] == 0
    assert response.json["mailbox_changes"] == 0
    assert client.post("/api/demo", json={}, headers=headers).status_code == 403
    engine.demo.assert_not_called()
    config.settings["demo_mode"] = True
    assert client.post("/api/demo", json={}, headers=headers).status_code == 200
    engine.demo.assert_called_once()


def test_telnyx_hook_uses_signature_handler_without_dashboard_auth(setup):
    app, _, engine, _ = setup
    body = b'{"data":{"event_type":"call.answered"}}'
    response = app.test_client().post("/webhooks/telnyx", data=body, headers={"telnyx-signature-ed25519": "invalid"})
    assert response.status_code == 401
    args = engine.notifier.handle_telnyx_webhook.call_args.args
    assert args[0] == body
    assert args[1]["telnyx-signature-ed25519"] == "invalid"


def test_short_credentials_fail_closed(setup, monkeypatch):
    _, store, engine, config = setup
    monkeypatch.setenv("STEPH_DASHBOARD_TOKEN", "short")
    with pytest.raises(ValueError, match="32 characters"):
        create_app(engine, store, config)
