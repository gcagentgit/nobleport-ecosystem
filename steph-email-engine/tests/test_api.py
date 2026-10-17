import pytest
from fastapi.testclient import TestClient

from steph_email import api as api_mod
from steph_email.api import build_app


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setattr(api_mod.settings, "api_token", "")
    monkeypatch.setattr(api_mod.settings, "public_base_url", "http://test.local")
    with TestClient(build_app(engine, background=False)) as c:
        yield c


def test_health_ui_and_status(client):
    assert client.get("/health").json()["status"] == "ok"
    assert "Steph" in client.get("/").text and "Morning brief" in client.get("/").text
    st = client.get("/api/status").json()
    assert st["live"] is False and st["activation_pending"]
    assert client.get("/api/providers/detect", params={"address": "x@icloud.com"}).json()["provider"] == "icloud"


def test_accounts_sync_urgent_and_messages(client, fake_smtp):
    r = client.post("/api/accounts", json={"address": "me@gmail.com", "secret": "pw", "display_name": "Michael"})
    assert r.status_code == 201 and "secret_enc" not in r.json()
    assert client.post("/api/accounts", json={"address": "broken@gmail.com", "secret": "pw"}).status_code == 502
    assert client.post("/api/accounts", json={"address": "ops@corp.com", "secret": "pw"}).status_code == 400

    sync = client.post("/api/sync").json()
    assert sync[0]["fetched"] == 7 and len(sync[0]["urgent_new"]) == 1
    urgent = client.get("/api/urgent").json()
    assert urgent["min_urgency"] == "high" and urgent["messages"][0]["urgency"] == "critical"
    assert client.get("/api/messages", params={"min_urgency": "bogus"}).status_code == 422
    assert len(client.get("/api/messages", params={"needs_reply": "true"}).json()) >= 1
    assert len(client.get("/api/messages", params={"q": "permit"}).json()) == 2

    mid = client.get("/api/messages", params={"tag": "invoice"}).json()[0]["id"]
    assert client.get(f"/api/messages/{mid}").json()["urgency_reasons"]
    assert client.post(f"/api/messages/{mid}/flags", json={"seen": True, "push": False}).json()["seen"] is True
    assert "oak-st" in client.post(f"/api/messages/{mid}/tags", json={"tags": ["Oak-St"]}).json()["tags"]
    assert "oak-st" not in client.delete(f"/api/messages/{mid}/tags/oak-st").json()["tags"]

    send = client.post("/api/send", json={"account": "me@gmail.com", "to": ["sub@vendor.com"], "subject": "Hi", "text": "yo", "due_days": 1})
    assert send.status_code == 200 and send.json()["expected_reply"]["status"] == "open"
    ov = client.get("/api/overview").json()
    assert ov["urgency"]["critical"] >= 1 and ov["replies"]["open"] == 1 and ov["replies_summary"]["waiting_on_them"] == 1
    assert client.get("/api/messages/9999").status_code == 404
    assert client.delete("/api/accounts/me@gmail.com").json()["removed"] == "me@gmail.com"


def test_replies_endpoints(client):
    client.post("/api/accounts", json={"address": "me@gmail.com", "secret": "pw"})
    client.post("/api/sync")
    rid = client.post("/api/send", json={"account": "me@gmail.com", "to": ["late@x.com"], "subject": "COI", "text": "pls"}).json()["expected_reply"]["id"]
    r = client.get("/api/replies").json()
    assert r["summary"]["waiting_on_them"] == 1 and any("change order" in m["subject"].lower() for m in r["needs_my_reply"])
    assert client.post("/api/replies/reconcile").json() == {"replied": [], "overdue": []}
    assert "Following up" in client.post(f"/api/replies/{rid}/nudge").json()["follow_up"]
    assert client.post(f"/api/replies/{rid}/close").json()["status"] == "closed"
    assert client.post("/api/replies/999/close").status_code == 404
    assert client.get("/api/replies").json()["summary"]["waiting_on_them"] == 0


def test_brief_run_twiml_and_audio(client):
    client.post("/api/accounts", json={"address": "me@gmail.com", "secret": "pw"})
    client.post("/api/sync")
    assert client.get("/api/brief").json() == {}
    preview = client.get("/api/brief/preview").json()
    assert preview["markdown"].startswith("# Email brief")
    run = client.post("/api/brief/run", json={"deliver": True}).json()
    assert run["delivery"][0]["kind"] == "sms" and run["delivery"][0]["ok"]
    date = run["brief_date"]
    assert client.get("/api/brief").json()["brief_date"] == date
    assert client.get(f"/api/brief/{date}").json()["sms"].startswith("Steph brief")
    assert client.get("/api/brief/1999-01-01").status_code == 404
    assert len(client.get("/api/briefs").json()) == 1
    tw = client.post(f"/webhooks/twilio/brief/{date}")
    assert tw.status_code == 200 and tw.headers["content-type"].startswith("application/xml") and "<Say" in tw.text
    assert client.get(f"/audio/brief-{date}.mp3").status_code == 404
    assert client.get("/webhooks/twilio/brief/1999-01-01").status_code == 404


def test_cleanup_notifications_and_voice_endpoints(client, transport):
    client.post("/api/accounts", json={"address": "me@gmail.com", "secret": "pw"})
    client.post("/api/sync")
    plan = client.get("/api/cleanup/plan").json()
    assert plan["dry_run"] and plan["count"] == 2
    run = client.post("/api/cleanup/run", json={}).json()
    assert run["archived"] == 2 and run["errors"] == []
    archived = client.get("/api/messages", params={"archived": "true"}).json()
    assert len(archived) == 2
    assert client.post(f"/api/messages/{archived[0]['id']}/restore").json()["archived"] is False
    sms = client.post("/api/notify/test-sms", json={"body": "ping"})
    assert sms.status_code == 200 and sms.json()["truth_label"] == "STAGED" and transport.outbox[-1]["body"] == "ping"
    n = client.get("/api/notifications").json()
    assert n["transport"] == "simulated" and n["items"][0]["purpose"] == "test"
    ev = client.post("/api/voice/verify", json={}).json()
    assert ev["verified"] is False and client.get("/api/voice/evidence").json()["verified"] is False


def test_api_token_enforced(engine, monkeypatch):
    monkeypatch.setattr(api_mod.settings, "api_token", "s3cret")
    with TestClient(build_app(engine, background=False)) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers={"X-API-Token": "s3cret"}).status_code == 200
        assert c.get("/health").status_code == 200
        assert c.post("/webhooks/twilio/brief/2026-01-01").status_code == 404   # webhooks are unauthenticated by design


def test_sms_destination_error_is_400(client, settings):
    settings.owner_phone = ""
    assert client.post("/api/notify/test-sms", json={}).status_code == 400
