from mailhub.parser import html_to_text, normalize_subject, parse_message
from tests.conftest import make_raw


def test_parses_headers_body_and_date():
    p = parse_message(make_raw("Hi =?utf-8?q?caf=C3=A9?=", sender="Ann <ann@x.com>", body="hello world", message_id="<m1@x>"))
    assert p["subject"] == "Hi café"
    assert p["from_addr"] == "ann@x.com" and p["from_name"] == "Ann"
    assert p["to_addrs"] == [{"name": "", "address": "me@gmail.com"}]
    assert p["body_text"].strip() == "hello world"
    assert p["date"] == "2026-09-01T14:00:00+00:00"
    assert p["thread_key"] == "<m1@x>"
    assert p["size"] > 0


def test_html_only_gets_text_and_attachments_listed():
    raw = make_raw("Plan", body="", html="<p>Site <b>plan</b> attached</p><script>x()</script>", attachment=("plan.pdf", b"%PDF-1.4"))
    p = parse_message(raw)
    assert "plan attached" in p["body_text"].lower()
    assert "script" not in p["body_text"]
    assert p["attachments"] == [{"filename": "plan.pdf", "content_type": "application/pdf", "size": 8}]


def test_thread_key_prefers_references_root_then_in_reply_to_then_subject():
    assert parse_message(make_raw(message_id="<c@x>", in_reply_to="<b@x>", references="<a@x> <b@x>"))["thread_key"] == "<a@x>"
    assert parse_message(make_raw(message_id="<c@x>", in_reply_to="<b@x>"))["thread_key"] == "<b@x>"
    p1 = parse_message(make_raw("Re: Change order 7"))
    p2 = parse_message(make_raw("FW: change order 7"))
    assert p1["thread_key"].startswith("subj:") and p1["thread_key"] == p2["thread_key"]


def test_helpers():
    assert normalize_subject("RE: Fwd: Re:  Bid  ") == "bid"
    assert html_to_text("<div>a</div><br>b&amp;c") == "a\nb&c"


def test_malformed_message_does_not_crash():
    p = parse_message(b"Subject: broken\r\nDate: not a date\r\n\r\n\xff\xfe body")
    assert p["subject"] == "broken" and p["date"] is None and "body" in p["body_text"]
