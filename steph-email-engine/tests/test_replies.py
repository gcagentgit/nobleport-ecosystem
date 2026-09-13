from datetime import timedelta

import pytest

from steph_email.db import Database
from steph_email.parser import parse_message
from steph_email.replies import ReplyTracker
from tests.conftest import NOW, ago, make_raw


def add_account(db: Database, address="me@gmail.com") -> int:
    return db.add_account(address=address, provider="gmail", imap_host="imap", username=address, secret_enc="x")


def put(db: Database, account_id: int, uid: int, raw: bytes, needs_reply=False) -> int:
    parsed = parse_message(raw)
    mid, _ = db.upsert_message(account_id, "INBOX", uid, parsed, False, False)
    db.set_urgency(mid, "normal", 30, ["asks for a response"] if needs_reply else [], needs_reply)
    return mid


@pytest.fixture
def tracker(db):
    aid = add_account(db)
    t = ReplyTracker(db, ["me@gmail.com"], default_due_days=3)
    t.account_id = aid  # type: ignore[attr-defined]
    return t


def test_track_sets_due_date(tracker):
    rec = tracker.track(account_id=tracker.account_id, account_address="me@gmail.com", message_id="<s1@me>", thread_key="<s1@me>",
                        to_addrs=["sub@vendor.com"], subject="COI request", sent_at=NOW.isoformat())
    assert rec["status"] == "open" and rec["due_at"].startswith((NOW + timedelta(days=3)).date().isoformat())
    rec2 = tracker.track(account_id=tracker.account_id, account_address="me@gmail.com", message_id="<s2@me>", thread_key="<s2@me>",
                         to_addrs=["a@b.com"], subject="x", sent_at=NOW.isoformat(), due_days=1)
    assert rec2["due_at"].startswith((NOW + timedelta(days=1)).date().isoformat())


def test_reconcile_marks_replied_when_thread_has_their_answer(tracker, db):
    rec = tracker.track(account_id=tracker.account_id, account_address="me@gmail.com", message_id="<s1@me>", thread_key="<s1@me>",
                        to_addrs=["sub@vendor.com"], subject="COI request", sent_at=(NOW - timedelta(days=1)).isoformat())
    # their reply arrives, threaded on our message id
    mid = put(db, tracker.account_id, 10, make_raw("Re: COI request", sender="sub@vendor.com", message_id="<r1@v>",
                                                  in_reply_to="<s1@me>", references="<s1@me>", date=ago(hours=2)))
    out = tracker.reconcile(now=NOW)
    assert out["replied"] == [rec["id"]]
    got = db.get_expected_reply(rec["id"])
    assert got["status"] == "replied" and got["replied_message_id"] == mid


def test_reconcile_ignores_our_own_follow_ups_and_flips_overdue(tracker, db):
    rec = tracker.track(account_id=tracker.account_id, account_address="me@gmail.com", message_id="<s1@me>", thread_key="<s1@me>",
                        to_addrs=["sub@vendor.com"], subject="COI request", sent_at=(NOW - timedelta(days=5)).isoformat())
    put(db, tracker.account_id, 11, make_raw("Re: COI request", sender="me@gmail.com", message_id="<f1@me>",
                                             in_reply_to="<s1@me>", references="<s1@me>", date=ago(days=1)))
    out = tracker.reconcile(now=NOW)
    assert out["replied"] == [] and out["overdue"] == [rec["id"]]
    waiting = tracker.waiting(now=NOW)
    assert waiting[0]["status"] == "overdue" and waiting[0]["days_overdue"] == 2
    # a second reconcile is idempotent
    assert tracker.reconcile(now=NOW) == {"replied": [], "overdue": []}


def test_waiting_sorts_overdue_first(tracker):
    a = tracker.track(account_id=tracker.account_id, account_address="me@gmail.com", message_id="<a@me>", thread_key="<a@me>",
                      to_addrs=["a@x.com"], subject="A", sent_at=NOW.isoformat())
    b = tracker.track(account_id=tracker.account_id, account_address="me@gmail.com", message_id="<b@me>", thread_key="<b@me>",
                      to_addrs=["b@x.com"], subject="B", sent_at=(NOW - timedelta(days=10)).isoformat())
    tracker.reconcile(now=NOW)
    ids = [w["id"] for w in tracker.waiting(now=NOW)]
    assert ids == [b["id"], a["id"]]
    assert [w["id"] for w in tracker.waiting(overdue_only=True, now=NOW)] == [b["id"]]


def test_close_and_nudge(tracker):
    rec = tracker.track(account_id=tracker.account_id, account_address="me@gmail.com", message_id="<a@me>", thread_key="<a@me>",
                        to_addrs=["sam@vendor.com"], subject="Change order 7", sent_at=NOW.isoformat())
    nudged = tracker.nudge(rec["id"])
    assert nudged["nudges"] == 1 and "Change order 7" in nudged["follow_up"] and nudged["follow_up"].startswith("Hi sam")
    assert tracker.close(rec["id"])["status"] == "closed"
    assert tracker.waiting(now=NOW) == []
    with pytest.raises(ValueError):
        tracker.close(rec["id"], "bogus")
    with pytest.raises(ValueError):
        tracker.nudge(999)


def test_needs_my_reply_excludes_answered_threads(tracker, db):
    asked = put(db, tracker.account_id, 20, make_raw("Can you approve?", sender="sub@vendor.com", message_id="<q1@v>", date=ago(hours=6)), needs_reply=True)
    answered = put(db, tracker.account_id, 21, make_raw("Need the COI?", sender="ins@x.com", message_id="<q2@v>", date=ago(hours=6)), needs_reply=True)
    put(db, tracker.account_id, 22, make_raw("Re: Need the COI?", sender="me@gmail.com", message_id="<r2@me>",
                                             in_reply_to="<q2@v>", references="<q2@v>", date=ago(hours=1)))
    ids = [m["id"] for m in tracker.needs_my_reply()]
    assert asked in ids and answered not in ids


def test_needs_my_reply_respects_sent_log(tracker, db):
    asked = put(db, tracker.account_id, 30, make_raw("Sign the addendum?", sender="law@x.com", message_id="<q3@v>", date=ago(hours=6)), needs_reply=True)
    assert asked in [m["id"] for m in tracker.needs_my_reply()]
    db.log_sent(tracker.account_id, "<r3@me>", ["law@x.com"], "Re: Sign the addendum?", in_reply_to="<q3@v>")
    assert asked not in [m["id"] for m in tracker.needs_my_reply()]


def test_summary_counts(tracker, db):
    tracker.track(account_id=tracker.account_id, account_address="me@gmail.com", message_id="<a@me>", thread_key="<a@me>",
                  to_addrs=["a@x.com"], subject="A", sent_at=(NOW - timedelta(days=10)).isoformat())
    put(db, tracker.account_id, 40, make_raw("Approve?", sender="sub@vendor.com", message_id="<q4@v>"), needs_reply=True)
    tracker.reconcile(now=NOW)
    s = tracker.summary(now=NOW)
    assert s == {"waiting_on_them": 1, "overdue": 1, "due_today": 0, "needs_my_reply": 1}
