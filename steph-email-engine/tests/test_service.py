from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.conftest import NOW, connect, make_raw, ago


def test_add_account_uses_preset_and_encrypts_secret(engine):
    acct = connect(engine, display_name="Michael")
    assert acct["provider"] == "gmail" and acct["imap_host"] == "imap.gmail.com" and acct["sent_folder"] == "[Gmail]/Sent Mail"
    row = engine.db.get_account("me@gmail.com")
    assert row["secret_enc"] != "app-pass" and engine.secrets.decrypt(row["secret_enc"]) == "app-pass"
    assert "secret_enc" not in acct
    assert "me@gmail.com" in engine.owner_addresses() and "me@gmail.com" in engine.scorer.owner_addresses


def test_add_account_rolls_back_on_failed_test(engine):
    with pytest.raises(RuntimeError):
        connect(engine, "broken@gmail.com")
    assert engine.db.get_account("broken@gmail.com") is None
    assert connect(engine, "broken@gmail.com", test=False)["address"] == "broken@gmail.com"


def test_generic_requires_hosts_and_rejects_duplicates(engine):
    with pytest.raises(ValueError):
        connect(engine, "ops@company.com")
    connect(engine, "ops@company.com", imap_host="mail.company.com", smtp_host="mail.company.com", smtp_port=465, smtp_ssl=True)
    with pytest.raises(ValueError):
        connect(engine, "ops@company.com", imap_host="mail.company.com")
    with pytest.raises(ValueError):
        connect(engine, "x@gmail.com", auth_method="oauth2")


def test_sync_scores_urgency_tags_and_preserves_raw(engine, fake_imap, settings):
    connect(engine)
    reports = engine.sync_all(alert=False)
    assert reports[0].fetched == 7 and not reports[0].error
    msgs = {m["subject"]: m for m in engine.db.list_messages(limit=100)}
    stop = msgs["STOP WORK ORDER - 5 Elm St"]
    assert stop["urgency"] == "critical" and "safety" in stop["tags"]
    assert msgs["Weekly deals - 20% off lumber"]["urgency"] == "low" and "newsletter" in msgs["Weekly deals - 20% off lumber"]["tags"]
    assert msgs["Can you approve the change order?"]["needs_reply"]
    assert msgs["Permit approved for 12 Oak St"]["urgency"] in ("high", "critical")   # VIP + permit
    assert reports[0].urgent_new == [stop["id"]]                                         # only unread criticals alert
    for m in msgs.values():
        assert Path(m["raw_path"]).exists() and str(settings.raw_dir) in m["raw_path"]
    assert engine.db.urgency_counts()["critical"] >= 1
    assert engine.sync_all(alert=False)[0].fetched == 0


def test_sync_batches_and_uidvalidity_reset(engine, fake_imap):
    connect(engine)
    engine.sync_all(alert=False)
    for uid in (8, 9, 10):
        fake_imap.folders["INBOX"][uid] = (make_raw(f"Bid {uid}", message_id=f"<b{uid}@x>"), False, False)
    assert engine.sync_all(alert=False)[0].fetched == 2      # sync_batch=2
    fake_imap.uidvalidity = 99
    report = engine.sync_all(alert=False)[0]
    assert report.reset and report.fetched == 10 and fake_imap.closed


def test_login_failure_is_reported_not_raised(engine):
    connect(engine, "broken@gmail.com", test=False)
    reports = engine.sync_all()
    assert reports[0].error and "AUTHENTICATIONFAILED" in reports[0].error
    assert "login" in engine.db.get_account("broken@gmail.com")["last_error"]


def test_search_threads_and_flags(engine, fake_imap):
    connect(engine)
    engine.sync_all(alert=False)
    hits = engine.db.list_messages(query="permit oak")
    assert {h["subject"] for h in hits} == {"Permit approved for 12 Oak St", "Re: Permit approved for 12 Oak St"}
    thread = engine.db.thread(hits[0]["thread_key"])
    assert [m["uid"] for m in thread] == [1, 3]
    root = next(m for m in hits if m["uid"] == 1)
    updated = engine.mark(root["id"], seen=False, flagged=True)
    assert not updated["seen"] and updated["flagged"]
    assert (1, "flagged", True) in fake_imap.stored_flags
    assert len(engine.db.list_messages(min_urgency="high", limit=100)) >= 3
    assert all(m["urgency"] in ("high", "critical") for m in engine.db.list_messages(min_urgency="high", limit=100))


def test_send_reply_threads_and_tracks_expected_reply(engine, fake_imap, fake_smtp):
    connect(engine, "ops@company.com", display_name="Ops", imap_host="mail.company.com", smtp_host="mail.company.com")
    engine.sync_all(alert=False)
    original = next(m for m in engine.db.list_messages(limit=100) if m["uid"] == 6)
    res = engine.send("ops@company.com", ["sub@vendor.com"], "", "Approved.", reply_to_message_id=original["id"], due_days=2)
    msg, creds = fake_smtp.sent[-1]
    assert res["subject"] == "Re: Can you approve the change order?" and msg["In-Reply-To"] == "<a6@x>"
    assert msg["From"] == "Ops <ops@company.com>" and creds["password"] == "app-pass"
    assert fake_imap.appended[-1][0] == "Sent"
    exp = res["expected_reply"]
    assert exp["thread_key"] == "<a6@x>" and exp["status"] == "open" and exp["to_addrs"] == ["sub@vendor.com"]
    # answering closes "needs my reply" for the original
    assert original["id"] not in {m["id"] for m in engine.replies.needs_my_reply()}
    untracked = engine.send("ops@company.com", ["a@b.com"], "Plain", "hi", expect_reply=False)
    assert untracked["expected_reply"] is None
    with pytest.raises(ValueError):
        engine.send("nobody@x.com", ["a@b.com"], "x", "y")


def test_reply_reconciled_on_next_sync(engine, fake_imap):
    connect(engine)
    engine.sync_all(alert=False)
    res = engine.send("me@gmail.com", ["sub@vendor.com"], "COI please", "Send it over.")
    our_id = res["message_id"]
    fake_imap.folders["INBOX"][8] = (make_raw("Re: COI please", sender="sub@vendor.com", message_id="<r@v>", in_reply_to=our_id,
                                              references=our_id, date=ago(minutes=5)), False, False)
    engine.sync_all(alert=False)
    rec = engine.db.get_expected_reply(res["expected_reply"]["id"])
    assert rec["status"] == "replied" and rec["replied_message_id"]


def test_critical_mail_sends_one_sms_alert(engine, transport):
    connect(engine)
    engine.sync_all()
    alerts = [n for n in engine.db.list_notifications() if n["purpose"] == "alert"]
    assert len(alerts) == 1 and "STOP WORK ORDER" in alerts[0]["body"] and alerts[0]["truth_label"] == "STAGED"
    assert transport.outbox[-1]["to"] == "+19785551234"
    stop = next(m for m in engine.db.list_messages(limit=100) if "STOP WORK" in m["subject"])
    assert engine.alert_for_message(stop["id"]) is None     # deduplicated
    assert len([n for n in engine.db.list_notifications() if n["purpose"] == "alert"]) == 1


def test_alert_skipped_without_owner_phone_or_when_disabled(engine, settings, transport):
    settings.owner_phone = ""
    connect(engine)
    engine.sync_all()
    assert transport.outbox == []
    settings.owner_phone = "+19785551234"
    settings.alerts_enabled = False
    engine.db.reset_folder(1, "INBOX")
    engine.sync_all()
    assert transport.outbox == []


def test_run_brief_stores_and_delivers_sms(engine, transport):
    connect(engine)
    engine.sync_all(alert=False)
    out = engine.run_brief(deliver=True, now=NOW)
    assert out["brief_date"] == "2026-09-14" and out["voice_source"] == "none"
    assert [d["kind"] for d in out["delivery"]] == ["sms"] and out["delivery"][0]["ok"]
    stored = engine.db.get_brief("2026-09-14")
    assert stored["markdown"].startswith("# Email brief") and stored["delivery"][0]["kind"] == "sms"
    assert transport.outbox[-1]["body"].startswith("Steph brief 9/14")
    assert engine.db.latest_brief()["brief_date"] == "2026-09-14"


def test_run_brief_with_call_falls_back_to_twilio_say_without_elevenlabs(engine, settings, transport):
    connect(engine)
    engine.sync_all(alert=False)
    settings.brief_call = True
    out = engine.run_brief(deliver=True, now=NOW)
    kinds = [d["kind"] for d in out["delivery"]]
    assert kinds == ["sms", "call"] and out["voice_source"] == "twilio-say" and out["audio_path"] == ""
    assert transport.outbox[-1]["url"] == "http://test.local/webhooks/twilio/brief/2026-09-14"


def test_brief_due_and_cleanup_auto(engine, settings):
    connect(engine)
    engine.sync_all(alert=False)
    early = datetime(2026, 9, 14, 9, 30, tzinfo=timezone.utc)     # 05:30 New York
    late = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)      # 08:00 New York
    assert not engine.brief_due(early) and engine.brief_due(late)
    out = engine.run_brief(now=late, cleanup=True)
    assert out["cleanup"]["archived"] == 2 and out["stats"]["archived_last_run"] == 2
    assert not engine.brief_due(late)


def test_status_activation_checklist(engine, settings):
    st = engine.status(now=NOW)
    assert not st["live"] and st["mailboxes"]["authorized"] is False
    assert any("Authorize at least one mailbox" in p for p in st["activation_pending"])
    assert any("Twilio" in p for p in st["activation_pending"]) and any("ElevenLabs" in p for p in st["activation_pending"])
    connect(engine)
    engine.sync_all(alert=False)
    st = engine.status(now=NOW)
    assert st["mailboxes"]["authorized"] and st["brief"]["due_now"] and st["cleanup"]["deletes"] == "never"
    assert not any("mailbox" in p.lower() for p in st["activation_pending"])
    assert st["notifications"]["truth_label"] == "STAGED" and st["voice"]["source"] == "none"


def test_remove_account_cascades(engine):
    connect(engine)
    engine.sync_all(alert=False)
    engine.send("me@gmail.com", ["a@b.com"], "x", "y")
    assert engine.remove_account("me@gmail.com")
    assert engine.db.list_messages() == [] and engine.db.overview()["accounts"] == [] and engine.db.list_expected_replies() == []
    assert not engine.remove_account("me@gmail.com")
