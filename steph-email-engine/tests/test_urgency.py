import pytest

from steph_email.urgency import LEVELS, RANK, UrgencyScorer, at_least, filter_by_level, level_for


def score(**kw):
    kw.setdefault("subject", ""); kw.setdefault("from_addr", "x@y.com"); kw.setdefault("body_text", "")
    return UrgencyScorer(vip_senders=["inspector@town.gov"], vip_domains=["cooley.com"], owner_addresses=["me@gmail.com"]).score(**kw)


def test_stop_work_is_critical():
    r = score(subject="STOP WORK ORDER - 5 Elm St", body_text="OSHA inspector on site, injury reported. Respond by tomorrow.")
    assert r.level == "critical" and r.score >= 70
    assert any("stop" in why for why in r.reasons) and any("deadline" in why for why in r.reasons)


def test_past_due_invoice_with_money_is_high():
    r = score(subject="Invoice #4471 past due", from_addr="billing@supplyco.com",
              body_text="Payment due immediately. Balance $12,500 by Friday.", tags=["invoice", "urgent"], flagged=True)
    assert r.level in ("high", "critical") and "flagged" in r.reasons and any("$12,500" in w for w in r.reasons)


def test_newsletter_is_low_even_with_urgent_words():
    r = score(subject="URGENT: 20% off ends tonight!", from_addr="marketing@bigbox.com",
              body_text="Deadline tonight. Unsubscribe here.", tags=["newsletter", "urgent"])
    assert r.level == "low" and not r.asks_reply


def test_owner_sent_mail_is_low_and_never_asks():
    r = score(subject="Can you send the COI?", from_addr="me@gmail.com", body_text="please confirm")
    assert r.level == "low" and r.score == 0 and r.reasons == ["sent by you"]


def test_vip_sender_and_domain():
    a = score(subject="Site visit", from_addr="inspector@town.gov")
    b = score(subject="Site visit", from_addr="paralegal@cooley.com")
    c = score(subject="Site visit", from_addr="nobody@random.com")
    assert a.score > b.score > c.score
    assert "VIP sender" in a.reasons and "VIP domain" in b.reasons


def test_money_tiers():
    small = score(body_text="the bill is $900")
    mid = score(body_text="balance $25,000 outstanding")
    big = score(body_text="wire $1,250,000 at closing")
    assert small.score < mid.score < big.score
    assert any("large amount" in w for w in big.reasons)


def test_asks_reply_detected_and_addressed_to_owner():
    r = score(subject="Can you approve the change order?", from_addr="sub@vendor.com", body_text="Please confirm by tomorrow.",
              to_addrs=[{"address": "me@gmail.com"}])
    assert r.asks_reply and "asks for a response" in r.reasons and "addressed to you" in r.reasons


def test_cc_only_is_penalised():
    to = score(subject="FYI", body_text="just so you know", to_addrs=[{"address": "me@gmail.com"}])
    cc = score(subject="FYI", body_text="just so you know", to_addrs=[{"address": "other@x.com"}], cc_addrs=[{"address": "me@gmail.com"}])
    assert cc.score < to.score and "you are only cc'd" in cc.reasons


def test_automated_notification_pushed_down():
    r = score(subject="Your receipt from Home Depot", from_addr="receipts@homedepot.com", body_text="Thanks for your order")
    assert r.level == "low" and "automatic notification" in r.reasons


def test_permit_and_contract_tags_add_points():
    base = score(subject="hello", body_text="see attached")
    permit = score(subject="hello", body_text="see attached", tags=["permit"])
    contract = score(subject="hello", body_text="see attached", tags=["contract"])
    assert permit.score == base.score + 15 and contract.score == base.score + 10


def test_level_thresholds_and_ordering():
    assert level_for(0) == "low" and level_for(20) == "normal" and level_for(50) == "high" and level_for(85) == "critical"
    assert list(LEVELS) == ["low", "normal", "high", "critical"]
    assert at_least("critical", "high") and not at_least("normal", "high")
    assert RANK["critical"] > RANK["low"]


def test_filter_by_level_sorts_most_urgent_first():
    msgs = [{"id": 1, "urgency": "normal", "urgency_score": 30}, {"id": 2, "urgency": "critical", "urgency_score": 90},
            {"id": 3, "urgency": "high", "urgency_score": 50}, {"id": 4, "urgency": "high", "urgency_score": 60}]
    assert [m["id"] for m in filter_by_level(msgs, "high")] == [2, 4, 3]
    assert [m["id"] for m in filter_by_level(msgs, "low")] == [2, 4, 3, 1]
    with pytest.raises(ValueError):
        filter_by_level(msgs, "extreme")


def test_score_is_clamped():
    r = score(subject="URGENT STOP WORK lawsuit deadline by today $5,000,000", from_addr="inspector@town.gov",
              body_text="please confirm immediately, final notice, injury", tags=["legal", "permit", "contract"], flagged=True)
    assert r.score == 100 and r.level == "critical"
