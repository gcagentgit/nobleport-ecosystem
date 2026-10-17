import pytest
from fastapi.testclient import TestClient

from mailhub import api as api_mod
from mailhub.api import build_app


@pytest.fixture
def client(hub, monkeypatch):
    monkeypatch.setattr(api_mod.settings, "api_token", "")
    app = build_app(hub, background_sync=False)
    with TestClient(app) as c:
        yield c


def test_health_and_ui(client):
    assert client.get("/health").json()["status"] == "ok"
    assert "MailHub" in client.get("/").text
    assert client.get("/api/providers/detect", params={"address": "x@icloud.com"}).json()["provider"] == "icloud"


def test_account_lifecycle_and_inbox(client, fake_smtp):
    r = client.post("/api/accounts", json={"address": "me@gmail.com", "secret": "pw", "display_name": "Mike"})
    assert r.status_code == 201, r.text
    assert r.json()["provider"] == "gmail" and "secret_enc" not in r.json()

    assert client.post("/api/accounts", json={"address": "broken@gmail.com", "secret": "pw"}).status_code == 502
    assert client.post("/api/accounts", json={"address": "ops@corp.com", "secret": "pw"}).status_code == 400

    sync = client.post("/api/sync").json()
    assert sync[0]["fetched"] == 3
    msgs = client.get("/api/messages", params={"tag": "invoice"}).json()
    assert len(msgs) == 1 and msgs[0]["account"] == "me@gmail.com"
    assert len(client.get("/api/messages", params={"q": "permit"}).json()) == 2
    assert len(client.get("/api/messages", params={"unread": "true"}).json()) == 2

    mid = msgs[0]["id"]
    assert client.get(f"/api/messages/{mid}").json()["body_text"].startswith("Payment due")
    assert client.post(f"/api/messages/{mid}/flags", json={"seen": True, "push": False}).json()["seen"] is True
    assert client.post(f"/api/messages/{mid}/tags", json={"tags": ["Oak-St"]}).json()["tags"] == ["invoice", "oak-st", "urgent"]
    assert "oak-st" not in client.delete(f"/api/messages/{mid}/tags/oak-st").json()["tags"]
    assert len(client.get(f"/api/messages/{mid}/thread").json()) == 1

    send = client.post("/api/send", json={"account": "me@gmail.com", "to": ["sub@vendor.com"], "subject": "Hi", "text": "yo"})
    assert send.status_code == 200 and fake_smtp.sent[-1][0]["To"] == "sub@vendor.com"
    assert client.post("/api/send", json={"account": "me@gmail.com", "to": ["not-an-email"], "text": "x"}).status_code == 422

    ov = client.get("/api/overview").json()
    assert ov["messages"] == 3 and ov["accounts"][0]["unread"] == 1 and ov["tags"]["permit"] == 2

    assert client.post("/api/accounts/me@gmail.com/enabled", params={"enabled": "false"}).json()["enabled"] is False
    assert client.delete("/api/accounts/me@gmail.com").json()["removed"] == "me@gmail.com"
    assert client.get("/api/messages/9999").status_code == 404


def test_api_token_enforced(hub, monkeypatch):
    monkeypatch.setattr(api_mod.settings, "api_token", "s3cret")
    with TestClient(build_app(hub, background_sync=False)) as c:
        assert c.get("/api/accounts").status_code == 401
        assert c.get("/api/accounts", headers={"X-API-Token": "s3cret"}).status_code == 200
        assert c.get("/health").status_code == 200
