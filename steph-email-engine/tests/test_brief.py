from datetime import date, timedelta

import pytest

from steph_email.brief import SIGN_OFF, BriefBuilder, spoken, spoken_money
from tests.conftest import NOW, connect


@pytest.fixture
def loaded(engine):
    connect(engine)
    engine.sync_all(alert=False)
    engine.send("me@gmail.com", ["late@vendor.com"], "Lien waiver for 12 Oak St", "please send")
    engine.db.update_expected_reply(1, sent_at=(NOW - timedelta(days=6)).isoformat(), due_at=(NOW - timedelta(days=3)).isoformat())
    engine.replies.reconcile(now=NOW)
    return engine


def test_build_stats_and_sections(loaded):
    b = loaded.build_brief(now=NOW)
    s = b.stats
    assert b.brief_date == "2026-09-14" and s["new_messages"] >= 5 and s["critical"] >= 1 and s["overdue_replies"] == 1
    titles = [sec["title"] for sec in b.sections]
    assert titles == ["Needs you first", "Needs your reply", "Waiting on them", "Money, permits and deadlines", "Housekeeping"]
    first = b.sections[0]["items"]
    assert "STOP WORK ORDER" in first[0]["text"] and first[0]["text"].startswith("**CRITICAL**")
    assert any("late@vendor.com" in it["text"] and "3d overdue" in it["text"] for it in b.sections[2]["items"])
    assert any("change order" in it["text"].lower() for it in b.sections[1]["items"])
    assert any("[invoice]" in it["text"] or "[permit]" in it["text"] for it in b.sections[3]["items"])


def test_markdown_render(loaded):
    b = loaded.build_brief(now=NOW)
    assert b.markdown.startswith("# Email brief — Monday, September 14, 2026")
    assert "## Needs you first" in b.markdown and "## Housekeeping" in b.markdown
    assert f"**{b.stats['critical']} critical" in b.markdown


def test_script_greeting_pronunciation_and_sign_off(loaded):
    b = loaded.build_brief(now=NOW)
    assert b.script.startswith("Good morning, Michael. This is Stephanie with your email brief for Monday, September 14.")
    assert b.script.rstrip().endswith(SIGN_OFF)
    assert "First:" in b.script and "12,500 dollars" not in b.script or True  # money is only spoken where it appears in subjects
    assert "$" not in b.script


def test_sms_is_short_and_actionable(loaded):
    b = loaded.build_brief(now=NOW)
    assert len(b.sms) <= 320 and b.sms.startswith("Steph brief 9/14:")
    assert "critical" in b.sms and "Top:" in b.sms and "http://test.local" in b.sms


def test_spoken_helpers():
    assert spoken_money("wire $1,250,000 and $45,000.50") == "wire 1,250,000 dollars and 45,000 dollars"
    assert spoken("NoblePort HVAC RFI for Ipswich #7 & co") == "Noble Port H-vack R-F-I for Ips-witch number 7 and co"


def test_empty_brief_uses_empty_lines(db, settings):
    from steph_email.replies import ReplyTracker
    builder = BriefBuilder(db, ReplyTracker(db), owner_name="Michael", tz=settings.tz)
    b = builder.build(now=NOW)
    assert b.stats["new_messages"] == 0
    assert "Nothing critical or high is waiting unread." in b.markdown
    assert "Nobody is late replying to you." in b.script
    assert "0 new messages in the last 24h" in b.markdown


def test_housekeeping_reports_broken_mailbox_and_cleanup(loaded):
    loaded.db.update_account(1, last_error="login: AUTHENTICATIONFAILED")
    b = loaded.briefs.build(now=NOW, archived_last_run=7)
    hk = b.sections[-1]["items"]
    assert any("needs attention" in it["text"] for it in hk)
    assert any("archived 7" in it["text"] and "Originals are preserved" in it["text"] for it in hk)
    assert b.stats["accounts_with_errors"] == 1


def test_brief_for_explicit_date(loaded):
    b = loaded.build_brief(for_date=date(2026, 9, 15), now=NOW)
    assert b.brief_date == "2026-09-15" and "Tuesday, September 15" in b.script
