"""Regression cases found during independent integration review; no provider I/O."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from steph_email.config import Config, DEFAULTS
from steph_email.engine import Engine
from steph_email.models import Email
from steph_email.storage import Store


NOW = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


def build(tmp_path, monkeypatch):
    monkeypatch.setattr('steph_email.engine.utcnow', lambda: NOW)
    settings = {**DEFAULTS, 'dry_run': False, 'quiet_start': '00:00', 'quiet_end': '00:00',
                'summary_time': '23:59', 'daily_cleanup': False}
    config = Config(tmp_path, settings=settings)
    store = Store(tmp_path)
    transport = Mock()
    transport.send.return_value = {'status': 'submitted', 'provider_id': 'fake', 'detail': 'test'}
    engine = Engine(config, store, aggregator=Mock(), notifier=transport)
    return engine, store, transport


def mail(received):
    return Email('gmail', 'INBOX', '1', '1', 'field@example.com',
                 'Urgent inspection due today', received)


def test_dispatch_expiry_uses_message_receipt_not_import_time(tmp_path, monkeypatch):
    engine, store, transport = build(tmp_path, monkeypatch)
    engine.process_email(mail(NOW - timedelta(hours=1, minutes=59)))
    engine.dispatch(NOW + timedelta(minutes=2))
    transport.send.assert_not_called()
    assert store.list_notifications()[0]['status'] == 'preview'


def test_delayed_expected_match_retracts_unsent_overdue_alert(tmp_path, monkeypatch):
    engine, store, transport = build(tmp_path, monkeypatch)
    watch = store.add_expectation('field@example.com', 'inspection')
    with store.connect() as db:
        db.execute('UPDATE expectations SET created_at=?,expires_at=? WHERE id=?',
                   ((NOW - timedelta(hours=2)).isoformat(), (NOW - timedelta(minutes=1)).isoformat(), watch))
    store.update_settings({'quiet_start': '00:00', 'quiet_end': '23:59'})
    engine.tick(NOW)
    engine.process_email(mail(NOW - timedelta(minutes=10)))
    assert store.list_expectations()[0]['status'] == 'matched'
    store.update_settings({'quiet_start': '00:00', 'quiet_end': '00:00'})
    engine.dispatch(NOW)
    bodies = [call.args[1] for call in transport.send.call_args_list]
    assert len(bodies) == 1
    assert 'arrived' in bodies[0]
