from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import pytest
from steph_email.config import Config, DEFAULTS, load_config
from steph_email.engine import Engine
from steph_email.models import Email, utcnow
from steph_email.storage import Store


class NoMailbox:
    def run_cycle(self, on_email):
        return {"processed": 0}


class FakeNotifier:
    def __init__(self):
        self.calls = []

    def send(self, channel, body, notification_id):
        self.calls.append((channel, body, notification_id))
        return {"status": "submitted", "provider_id": "test", "detail": "test transport"}


@pytest.fixture
def setup(tmp_path):
    cfg = Config(tmp_path, settings={**DEFAULTS, "quiet_start": "00:00", "quiet_end": "00:00"})
    store, notifier = Store(tmp_path), FakeNotifier()
    engine = Engine(cfg, store, aggregator=NoMailbox(), notifier=notifier)
    return engine, store, notifier, cfg


def message(uid="1", **kw):
    fields = dict(account="gmail", folder="INBOX", uidvalidity="10", uid=uid,
                  sender="field@example.com", subject="Urgent inspection due today", received_at=utcnow(), text="Review needed")
    fields.update(kw)
    return Email(**fields)


def test_concurrent_duplicate_ingestion_is_one_email_and_one_alert(setup):
    engine, store, notifier, _ = setup
    mail = message()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(engine.process_email, [mail] * 8))
    assert sum(not r["duplicate"] for r in results) == 1
    assert len(store.list_emails()) == len(store.list_notifications()) == 1
    assert notifier.calls == []


def test_uid_reuse_new_epoch_and_other_mailbox_are_distinct(setup):
    engine, store, _, _ = setup
    for kw in ({}, {"uidvalidity": "11"}, {"account": "yahoo"}):
        engine.process_email(message(**kw))
    assert len(store.list_emails()) == 3


def test_expected_plus_urgent_coalesces_channels_and_all_matches(setup):
    engine, store, _, _ = setup
    store.add_expectation("field@example.com", "inspection", notify="both")
    store.add_expectation("example.com", "due today", notify="sms")
    result = engine.process_email(message())
    assert result["matched_expectations"] == 2
    assert result["status"] == "expected"
    assert {e["status"] for e in store.list_expectations()} == {"matched"}
    assert sorted(n["channel"] for n in store.list_notifications()) == ["sms", "voice"]


def test_sender_domain_suffix_and_display_name_cannot_match(setup):
    engine, store, _, _ = setup
    store.add_expectation("example.com", "invoice")
    for i, sender in enumerate(["field@example.com.evil.com", '"field@example.com" <attacker@evil.com>']):
        result = engine.process_email(message(str(i), sender=sender, subject="invoice"))
        assert result["matched_expectations"] == 0
    assert store.list_expectations()[0]["status"] == "pending"


def test_historical_email_does_not_fulfill_new_watch(setup):
    engine, store, _, _ = setup
    store.add_expectation("example.com", "inspection")
    result = engine.process_email(message(received_at=utcnow() - timedelta(days=1)))
    assert result["matched_expectations"] == 0
    assert store.list_notifications()[0]["status"] == "preview"


def test_expired_watch_can_match_delayed_ingest_inside_receipt_window(setup):
    engine, store, _, _ = setup
    eid = store.add_expectation("example.com", "inspection")
    now = utcnow()
    with store.connect() as db:
        db.execute("UPDATE expectations SET created_at=?,expires_at=?,status='expired' WHERE id=?", ((now-timedelta(hours=3)).isoformat(), (now-timedelta(hours=1)).isoformat(), eid))
    result = engine.process_email(message(received_at=now-timedelta(hours=2)))
    assert result["matched_expectations"] == 1


def test_suspicious_expected_email_is_visible_with_warning(setup):
    engine, store, _, _ = setup
    store.add_expectation("example.com", "invoice")
    result = engine.process_email(message(subject="invoice — claim your prize"))
    assert result["status"] == "expected"
    assert result["is_spam"] is True
    assert len(store.list_notifications()) == 1


def test_rule_boundaries_do_not_treat_bid_inside_forbidden_as_urgent(setup):
    engine, _, _, _ = setup
    result = engine.process_email(message(subject="Forbidden city", text="Today"))
    assert result["urgency_score"] == 0


def test_cleanup_is_reversible_and_preserves_urgent_and_all_content(setup):
    engine, store, _, _ = setup
    first = engine.process_email(message(subject="Old record", text="retain me", received_at=utcnow()-timedelta(days=100)))
    engine.process_email(message("2", received_at=utcnow()-timedelta(days=100)))
    assert store.cleanup_review(90) == {"reviewed": 1, "deleted": 0, "mailbox_changes": 0}
    assert len(store.list_emails()) == 2
    assert store.restore_email(first["id"])
    assert next(e for e in store.list_emails() if e["id"] == first["id"])["text"] == "retain me"


def test_live_alert_rate_limit_quiet_hours_and_disabled_channels(setup):
    engine, store, notifier, cfg = setup
    cfg.settings.update(dry_run=False, max_alerts_per_hour=1)
    store.seed_settings(cfg.settings)
    store.update_settings({"max_alerts_per_hour": 1, "quiet_start": "00:00", "quiet_end": "23:59"})
    engine.process_email(message())
    engine.process_email(message("2"))
    now = utcnow().replace(hour=16, minute=0)
    engine.dispatch(now)
    assert notifier.calls == []
    store.update_settings({"quiet_start": "00:00", "quiet_end": "00:00"})
    engine.dispatch()
    assert len(notifier.calls) == 1
    engine.dispatch()
    assert len(notifier.calls) == 1
    store.update_settings({"sms_notifications": False})
    engine.dispatch()
    assert sum(n["status"] == "preview" for n in store.list_notifications()) == 1


def test_daily_summary_once_even_after_restart_and_timezone_dst(setup):
    engine, store, _, cfg = setup
    # 08:00 New York after the DST fall-back, 13:00 UTC.
    before = datetime(2026, 11, 1, 12, 59, tzinfo=timezone.utc)
    engine.tick(before)
    assert not store.list_notifications()
    engine.tick(before + timedelta(minutes=1))
    assert len(store.list_notifications()) == 1
    restarted = Engine(cfg, store, aggregator=NoMailbox(), notifier=FakeNotifier())
    restarted.tick(before + timedelta(hours=1))
    assert len(store.list_notifications()) == 1


def test_expired_watch_has_distinct_status_and_one_alert(setup):
    engine, store, _, _ = setup
    eid = store.add_expectation("example.com", "drawings", hours=1)
    with store.connect() as db:
        db.execute("UPDATE expectations SET expires_at=? WHERE id=?", ((utcnow()-timedelta(seconds=1)).isoformat(), eid))
    engine.tick()
    engine.tick()
    assert store.list_expectations()[0]["status"] == "expired"
    assert len([n for n in store.list_notifications() if n["dedupe_key"].startswith("expired:")]) == 1


def test_crash_during_send_becomes_uncertain_not_retried(setup):
    engine, store, notifier, cfg = setup
    engine.process_email(message())
    with store.connect() as db:
        db.execute("UPDATE notifications SET status='sending'")
    restarted = Engine(cfg, store, aggregator=NoMailbox(), notifier=notifier)
    restarted.dispatch()
    assert store.list_notifications()[0]["status"] == "uncertain"
    assert notifier.calls == []


def test_invalid_settings_naive_timestamp_and_credentials_fail(tmp_path, setup):
    _, store, _, _ = setup
    for change in ({"sms_notifications": "true"}, {"retention_days": -1}, {"dry_run": False}, {"demo_mode": True}, {"timezone": "wrong/zone"}):
        with pytest.raises(ValueError):
            store.update_settings(change)
    with pytest.raises(ValueError):
        message(received_at=datetime.now())
    path = tmp_path / "bad.yaml"
    path.write_text("accounts:\n - id: outlook\n   provider: outlook\n   auth: {type: app_password, password: secret}\n")
    with pytest.raises(ValueError):
        load_config(path)
