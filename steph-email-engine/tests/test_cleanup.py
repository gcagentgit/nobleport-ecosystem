from pathlib import Path

import pytest

from steph_email.cleanup import PROTECTED_TAGS, Cleaner, preserve_raw, raw_path_for
from tests.conftest import NOW, FakeImap, connect, make_raw, ago


@pytest.fixture
def synced(engine):
    connect(engine)
    engine.sync_all(alert=False)
    return engine


def test_plan_selects_only_old_low_priority_mail(synced):
    plan = synced.cleaner.plan(now=NOW)
    subjects = {c["subject"] for c in plan.candidates}
    assert subjects == {"Weekly deals - 20% off lumber", "Old lunch thread"}
    assert plan.skipped == {} or all(v > 0 for v in plan.skipped.values())
    assert plan.cutoff.startswith("2026-08-15")


def test_plan_skip_reasons(synced):
    db = synced.db
    old = next(m for m in db.list_messages(limit=100) if m["subject"] == "Old lunch thread")
    db.set_flags(old["id"], flagged=True)
    deals = next(m for m in db.list_messages(limit=100) if "Weekly deals" in m["subject"])
    db.add_tags(deals["id"], ["invoice"])
    plan = synced.cleaner.plan(now=NOW)
    assert plan.candidates == [] and plan.skipped == {"flagged": 1, "protected tag": 1}
    assert "legal" in PROTECTED_TAGS and "permit" in PROTECTED_TAGS


def test_plan_protects_threads_awaiting_reply(synced):
    old = next(m for m in synced.db.list_messages(limit=100) if m["subject"] == "Old lunch thread")
    synced.replies.track(account_id=1, account_address="me@gmail.com", message_id="<s@me>", thread_key=old["thread_key"],
                         to_addrs=["friend@example.com"], subject="Old lunch thread", sent_at=NOW.isoformat())
    plan = synced.cleaner.plan(now=NOW)
    assert old["id"] not in {c["id"] for c in plan.candidates} and plan.skipped.get("thread awaiting a reply") == 1


def test_run_archives_with_original_on_disk_and_never_deletes(synced, fake_imap):
    result = synced.cleanup(dry_run=False, now=NOW)
    assert result["archived"] == 2 and result["preserved_now"] == 0 and result["server_copies"] == 0 and result["errors"] == []
    for c in synced.db.list_messages(archived_only=True, limit=10):
        assert c["archived"] and Path(c["raw_path"]).exists() and Path(c["raw_path"]).read_bytes().startswith(b"Subject:")
    assert len(synced.db.list_messages(limit=100)) == 5          # archived mail leaves the working view
    assert len(fake_imap.folders["INBOX"]) == 7                 # server untouched
    assert fake_imap.copied == []
    assert synced.db.overview()["archived"] == 2
    runs = synced.db.cleanup_runs()
    assert runs[0]["archived"] == 2 and runs[0]["run_id"] == result["run_id"]


def test_run_fetches_missing_original_before_archiving(synced, fake_imap):
    old = next(m for m in synced.db.list_messages(limit=100) if m["subject"] == "Old lunch thread")
    Path(old["raw_path"]).unlink()
    synced.db.set_raw_path(old["id"], "")
    result = synced.cleanup(dry_run=False, now=NOW)
    assert result["archived"] == 2 and result["preserved_now"] == 1
    assert Path(synced.db.get_message(old["id"])["raw_path"]).exists()


def test_run_refuses_when_original_cannot_be_preserved(settings, db, fake_smtp):
    from steph_email.crypto import SecretBox
    from steph_email.notify import Notifier, SimulatedTransport
    from steph_email.service import EmailEngine
    settings.store_raw = False
    imap = FakeImap({"INBOX": {1: (make_raw("Old lunch thread", sender="friend@example.com", body="pizza next week", date=ago(days=60)), True, False)}})
    calls = {"n": 0}

    def factory(acct, token):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("server down")
        return imap

    eng = EmailEngine(settings, db=db, secrets=SecretBox.load(settings.data_dir), imap_factory=factory,
                      smtp_factory=lambda a: fake_smtp, notifier=Notifier(db, settings, transport=SimulatedTransport(), voice=None))
    eng.add_account("me@gmail.com", "pw", test=False)
    eng.sync_all(alert=False)
    result = eng.cleanup(dry_run=False, now=NOW)
    assert result["archived"] == 0 and any("left untouched" in e for e in result["errors"])
    assert not db.list_messages(limit=10)[0]["archived"]


def test_copy_to_server_copies_but_keeps_original(synced, fake_imap):
    result = synced.cleanup(dry_run=False, now=NOW, copy_to_server=True)
    assert result["server_copies"] == 2
    assert sorted(fake_imap.copied) == [(4, "Steph/Archive"), (7, "Steph/Archive")]
    assert "Steph/Archive" in fake_imap.created
    assert len(fake_imap.folders["INBOX"]) == 7


def test_restore_and_raw_path_helpers(synced, tmp_path):
    synced.cleanup(dry_run=False, now=NOW)
    archived = synced.db.list_messages(archived_only=True, limit=1)[0]
    back = synced.cleaner.restore(archived["id"])
    assert not back["archived"] and archived["id"] in {m["id"] for m in synced.db.list_messages(limit=100)}
    with pytest.raises(ValueError):
        synced.cleaner.restore(9999)
    p = raw_path_for(tmp_path / "raw", "a b@x.com", "[Gmail]/All Mail", 1725000000, 5)
    assert p == tmp_path / "raw" / "a_b@x.com" / "_Gmail_All_Mail" / "1725000000" / "5.eml"
    preserve_raw(tmp_path / "raw", "a@x.com", "INBOX", 1, 1, b"first")
    preserve_raw(tmp_path / "raw", "a@x.com", "INBOX", 1, 1, b"second")
    assert (tmp_path / "raw" / "a@x.com" / "INBOX" / "1" / "1.eml").read_bytes() == b"first"   # never overwritten


def test_raw_files_and_data_dir_are_owner_only(synced, settings):
    import os, stat
    msg = synced.db.list_messages(limit=1)[0]
    raw = Path(msg["raw_path"])
    assert stat.S_IMODE(os.stat(raw).st_mode) == 0o600
    for d in (settings.raw_dir, raw.parent, raw.parent.parent):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(settings.resolved_db_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(settings.resolved_db_path.parent).st_mode) == 0o700


def test_uidvalidity_reset_never_reuses_old_originals(synced, fake_imap):
    old = next(m for m in synced.db.list_messages(limit=100) if m["subject"] == "Old lunch thread")
    old_path = Path(old["raw_path"])
    assert "/1/" in str(old_path)
    # The server renumbers: uid 7 now belongs to a different, recent message.
    fake_imap.uidvalidity = 2
    fake_imap.folders["INBOX"] = {7: (make_raw("Brand new note", sender="new@x.com", body="hello", message_id="<n7@x>"), True, False)}
    synced.sync_all(alert=False)
    fresh = next(m for m in synced.db.list_messages(limit=100) if m["subject"] == "Brand new note")
    assert fresh["raw_path"] != str(old_path) and "/2/7.eml" in fresh["raw_path"]
    assert Path(fresh["raw_path"]).read_bytes() != old_path.read_bytes()
    assert b"Brand new note" in Path(fresh["raw_path"]).read_bytes()


def test_run_refuses_to_preserve_across_uidvalidity_change(synced, fake_imap):
    old = next(m for m in synced.db.list_messages(limit=100) if m["subject"] == "Old lunch thread")
    Path(old["raw_path"]).unlink()
    synced.db.set_raw_path(old["id"], "")
    fake_imap.uidvalidity = 2          # server renumbered since the last sync
    result = synced.cleanup(dry_run=False, now=NOW)
    assert any("UIDVALIDITY changed" in e for e in result["errors"])
    assert not synced.db.get_message(old["id"])["archived"]


def test_plan_finds_old_mail_behind_thousands_of_recent_messages(synced):
    from steph_email.parser import parse_message
    acct = synced.db.get_account("me@gmail.com")["id"]
    with synced.db.tx():
        for uid in range(100, 2600):
            parsed = parse_message(make_raw(f"Recent {uid}", sender=f"r{uid}@x.com", body="hi", message_id=f"<r{uid}@x>", date=ago(hours=1)))
            mid, _ = synced.db.upsert_message(acct, "INBOX", uid, parsed, True, False)
            synced.db.set_urgency(mid, "low", 5, [], False)
    assert len(synced.db.list_messages(limit=10000)) > 2500
    plan = synced.cleaner.plan(now=NOW)
    assert {c["subject"] for c in plan.candidates} == {"Weekly deals - 20% off lumber", "Old lunch thread"}
    assert synced.cleaner.plan(now=NOW, limit=1).candidates[0]["subject"] in {"Weekly deals - 20% off lumber", "Old lunch thread"}
