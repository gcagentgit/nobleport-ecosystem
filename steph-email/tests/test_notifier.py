import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from steph_email.notifier import Notifier
from steph_email.storage import Store


NOW = 1800000000
SMS_ID = 'SM' + '1' * 32
CALL_ID = 'v3:controlled_call_token'


@pytest.fixture
def rig(tmp_path, monkeypatch):
    settings = dict(dry_run=False, sms_notifications=True, voice_notifications=True, notification_channel='both')
    config = SimpleNamespace(settings=settings, data_dir=tmp_path)
    store = Store(tmp_path)
    store.seed_settings(settings)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    values = dict(NOTIFY_PHONE='+15555550100', TWILIO_ACCOUNT_SID='AC'+'a'*32,
                  TWILIO_AUTH_TOKEN='test-only-secret', TWILIO_FROM_NUMBER='+15555550101',
                  TELNYX_API_KEY='test-only-key', TELNYX_CONNECTION_ID='123456',
                  TELNYX_FROM_NUMBER='+15555550102', TELNYX_WEBHOOK_URL='https://operator.example/webhooks/telnyx',
                  TELNYX_PUBLIC_KEY=base64.b64encode(public).decode(), TELNYX_VOICE='female',
                  TELNYX_ELEVENLABS_API_KEY_REF='')
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    http = Mock()
    notifier = Notifier(config, store, session=http, clock=lambda: NOW)
    return SimpleNamespace(config=config, store=store, notifier=notifier, http=http, private=private)


def response(code=200, data=None):
    value = Mock(status_code=code)
    value.json.return_value = data or {'data': {'result': 'ok'}}
    return value


def start_call(rig, ident='alert-1'):
    rig.http.post.return_value = response(data={'data': {'call_control_id': CALL_ID}})
    assert rig.notifier.send('voice', 'Stephanie alert. Check the dashboard.', ident)['status'] == 'submitted'
    with rig.store.connect() as db:
        return dict(db.execute('SELECT * FROM voice_calls WHERE notification_id=?', (ident,)).fetchone())


def signed(rig, call, event_id='event-1', kind='call.answered', *, timestamp=NOW, **overrides):
    payload = dict(call_control_id=CALL_ID, client_state=call['token'], connection_id='123456')
    if kind == 'call.answered':
        payload.update(to='+15555550100', **{'from': '+15555550102'})
    payload.update(overrides)
    raw = json.dumps({'data': {'id': event_id, 'event_type': kind, 'occurred_at': '2027-01-15T08:00:00Z', 'payload': payload}}).encode()
    stamp = str(timestamp)
    signature = rig.private.sign(stamp.encode() + b'|' + raw)
    return raw, {'Telnyx-Timestamp': stamp, 'Telnyx-Signature-Ed25519': base64.b64encode(signature).decode()}


def test_dry_run_and_disabled_channels_never_touch_provider(rig):
    rig.store.seed_settings({**rig.config.settings, 'dry_run': True})
    assert rig.notifier.send('sms', 'Preview', 'p1')['status'] == 'preview'
    rig.store.seed_settings(rig.config.settings)
    rig.store.update_settings({'sms_notifications': False})
    assert rig.notifier.send('sms', 'Preview', 'p2')['status'] == 'preview'
    rig.store.update_settings({'voice_notifications': False})
    assert rig.notifier.send('voice', 'Preview', 'p3')['status'] == 'preview'
    rig.http.post.assert_not_called()


def test_expectation_voice_can_override_global_urgent_channel(rig):
    rig.store.update_settings({'notification_channel': 'sms'})
    start_call(rig)
    assert rig.http.post.call_count == 1


def test_demo_mode_always_previews_even_when_dry_run_false(rig):
    rig.store.seed_settings({**rig.config.settings, 'demo_mode': True})
    assert rig.notifier.send('sms', 'Preview', 'demo')['status'] == 'preview'
    assert rig.notifier.send('voice', 'Preview', 'demo')['status'] == 'preview'
    rig.http.post.assert_not_called()


def test_sms_acceptance_is_submitted_and_same_id_cannot_send_twice(rig):
    rig.http.post.return_value = response(data={'sid': SMS_ID, 'status': 'queued'})
    first = rig.notifier.send('sms', 'Check your dashboard.', 'n1')
    assert first['status'] == 'submitted'
    assert first['provider_id'] == SMS_ID
    assert rig.notifier.send('sms', 'Changed content', 'n1') == first
    assert rig.http.post.call_count == 1
    kwargs = rig.http.post.call_args.kwargs
    assert kwargs['data']['To'] == '+15555550100'
    assert kwargs['allow_redirects'] is False


def test_timeout_survives_restart_without_retry_or_exception_secret(rig):
    rig.http.post.side_effect = requests.Timeout('secret-auth-token full-email-body')
    first = rig.notifier.send('sms', 'Check your dashboard.', 'timeout')
    assert first['status'] == 'uncertain'
    assert 'secret' not in first['detail']
    second = Notifier(rig.config, rig.store, session=rig.http, clock=lambda: NOW)
    assert second.send('sms', 'Check your dashboard.', 'timeout') == first
    assert rig.http.post.call_count == 1


@pytest.mark.parametrize('code,status', [(401, 'failed'), (429, 'failed'), (503, 'uncertain')])
def test_provider_http_failure_does_not_claim_delivery_or_retry(rig, code, status):
    rig.http.post.return_value = response(code, {'error': 'sensitive provider text'})
    outcome = rig.notifier.send('sms', 'Alert', 'failure')
    assert outcome['status'] == status
    assert 'sensitive' not in outcome['detail']
    rig.notifier.send('sms', 'Alert', 'failure')
    assert rig.http.post.call_count == 1


def test_voice_dials_then_waits_for_verified_answer_and_rejects_tampering(rig):
    call = start_call(rig)
    assert rig.notifier.drain_voice_events() == []
    raw, headers = signed(rig, call)
    assert rig.notifier.handle_telnyx_webhook(raw.replace(b'call.answered', b'call.hangup'), headers)[1] == 401
    assert rig.notifier.handle_telnyx_webhook(raw, headers) == ({'status': 'queued'}, 200)
    assert rig.http.post.call_count == 1  # HTTP webhook response does no network I/O.
    rig.http.post.return_value = response()
    assert rig.notifier.drain_voice_events()[0]['status'] == 'submitted'
    assert rig.http.post.call_count == 2
    assert rig.http.post.call_args.args[0].endswith('/actions/speak')
    assert rig.http.post.call_args.kwargs['json']['payload_type'] == 'text'
    assert rig.notifier.handle_telnyx_webhook(raw, headers) == ({'status': 'duplicate'}, 200)
    raw2, headers2 = signed(rig, call, event_id='different-id-same-answer')
    rig.notifier.handle_telnyx_webhook(raw2, headers2)
    assert rig.notifier.drain_voice_events() == []
    assert rig.http.post.call_count == 2


def test_stale_or_wrong_recipient_answer_cannot_speak(rig):
    call = start_call(rig)
    assert rig.notifier.handle_telnyx_webhook(*signed(rig, call, timestamp=NOW-301))[1] == 401
    assert rig.notifier.handle_telnyx_webhook(*signed(rig, call, to='+15555550999'))[1] == 400
    assert rig.notifier.handle_telnyx_webhook(*signed(rig, call, call_control_id='v3:other-call'))[1] == 400
    assert rig.notifier.drain_voice_events() == []
    assert rig.http.post.call_count == 1


def test_voice_disable_after_dial_suppresses_speech(rig):
    call = start_call(rig)
    rig.notifier.handle_telnyx_webhook(*signed(rig, call))
    rig.store.update_settings({'voice_notifications': False})
    assert rig.notifier.drain_voice_events()[0]['status'] == 'preview'
    assert rig.http.post.call_count == 1


def test_speak_timeout_is_not_replayed_and_hangup_can_reconcile(rig):
    call = start_call(rig)
    rig.notifier.handle_telnyx_webhook(*signed(rig, call))
    rig.http.post.side_effect = requests.Timeout('secret')
    assert rig.notifier.drain_voice_events()[0]['status'] == 'uncertain'
    assert rig.notifier.drain_voice_events() == []
    # Speak ended has no to/from fields in the documented Telnyx schema.
    assert rig.notifier.handle_telnyx_webhook(*signed(rig, call, 'ended', 'call.speak.ended'))[1] == 200
    rig.http.post.side_effect = None
    rig.http.post.return_value = response()
    assert rig.notifier.drain_voice_events()[0]['status'] == 'submitted'
    assert rig.http.post.call_args.args[0].endswith('/actions/hangup')
    assert rig.notifier.drain_voice_events() == []


def test_hangup_before_delayed_answer_prevents_speech(rig):
    call = start_call(rig)
    rig.notifier.handle_telnyx_webhook(*signed(rig, call, 'hung-up', 'call.hangup'))
    rig.notifier.handle_telnyx_webhook(*signed(rig, call, 'delayed-answer'))
    assert rig.notifier.drain_voice_events() == []
    assert rig.http.post.call_count == 1


def test_timeout_dial_can_correlate_later_signed_answer_without_redial(rig):
    rig.http.post.side_effect = requests.Timeout()
    assert rig.notifier.send('voice', 'Check dashboard', 't1')['status'] == 'uncertain'
    with rig.store.connect() as db:
        call = dict(db.execute('SELECT * FROM voice_calls').fetchone())
    rig.notifier.handle_telnyx_webhook(*signed(rig, call))
    rig.http.post.side_effect = None
    rig.http.post.return_value = response()
    assert rig.notifier.drain_voice_events()[0]['status'] == 'submitted'
    assert rig.notifier.send('voice', 'Check dashboard', 't1')['status'] == 'uncertain'
    assert rig.http.post.call_count == 2


def test_elevenlabs_voice_requires_secret_reference_and_uses_plain_text(rig, monkeypatch):
    monkeypatch.setenv('TELNYX_VOICE', 'ElevenLabs.eleven_multilingual_v2.approved_voice_id')
    assert rig.notifier.send('voice', 'Alert', 'eleven')['status'] == 'failed'
    rig.http.post.assert_not_called()
    monkeypatch.setenv('TELNYX_ELEVENLABS_API_KEY_REF', 'operator-steph-voice')
    call = start_call(rig, 'eleven')
    rig.notifier.handle_telnyx_webhook(*signed(rig, call))
    rig.http.post.return_value = response()
    rig.notifier.drain_voice_events()
    body = rig.http.post.call_args.kwargs['json']
    assert body['voice_settings'] == {'type': 'elevenlabs', 'api_key_ref': 'operator-steph-voice'}
    assert body['service_level'] == 'premium'
    assert body['payload_type'] == 'text'
