from datetime import datetime, timezone
from unittest.mock import Mock
from steph_email.config import Config, DEFAULTS
from steph_email.engine import Engine
from steph_email.models import Email
from steph_email.storage import Store


def test_daily_brief_waits_for_initial_ingestion_and_reports_partial_coverage(tmp_path, monkeypatch):
    now = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("steph_email.engine.utcnow", lambda: now)
    config = Config(tmp_path, accounts=[{"id": "mail", "enabled": True}], settings=dict(DEFAULTS))
    store = Store(tmp_path)
    adapter = Mock()

    def sync(callback):
        callback(Email("mail", "INBOX", "1", "1", "x@example.com", "Hello", now))
        return {"mail": {"processed": 1, "backlog": True}}

    adapter.run_cycle.side_effect = sync
    engine = Engine(config, store, aggregator=adapter, notifier=Mock())
    engine.tick(now)
    assert store.list_notifications() == []
    engine.run_cycle()
    engine.tick(now)
    digest = store.list_notifications()
    assert len(digest) == 1
    assert "1 imported emails" in digest[0]["body"]
    assert "coverage is incomplete" in digest[0]["body"]
