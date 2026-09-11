import pytest

from mailhub.service import MailHub


def connect(hub: MailHub, address="me@gmail.com", **kw) -> dict:
    return hub.add_account(address, "app-pass", **kw)


def test_add_account_uses_preset_and_encrypts_secret(hub):
    acct = connect(hub, display_name="Mike")
    assert acct["provider"] == "gmail" and acct["imap_host"] == "imap.gmail.com" and acct["smtp_host"] == "smtp.gmail.com"
    assert acct["sent_folder"] == "[Gmail]/Sent Mail"
    row = hub.db.get_account("me@gmail.com")
    assert row["secret_enc"] != "app-pass" and hub.secrets.decrypt(row["secret_enc"]) == "app-pass"
    assert "secret_enc" not in acct


def test_add_account_rolls_back_on_failed_test(hub):
    with pytest.raises(RuntimeError):
        connect(hub, "broken@gmail.com")
    assert hub.db.get_account("broken@gmail.com") is None
    assert connect(hub, "broken@gmail.com", test=False)["address"] == "broken@gmail.com"


def test_generic_requires_hosts_and_rejects_duplicates(hub):
    with pytest.raises(ValueError):
        connect(hub, "ops@company.com")
    connect(hub, "ops@company.com", imap_host="mail.company.com", smtp_host="mail.company.com", smtp_port=465, smtp_ssl=True)
    with pytest.raises(ValueError):
        connect(hub, "ops@company.com", imap_host="mail.company.com")
    with pytest.raises(ValueError):
        connect(hub, "x@gmail.com", auth_method="oauth2")  # missing client id


def test_sync_backfill_then_incremental_with_tags(hub, fake_imap):
    connect(hub)
    reports = hub.sync_all()
    assert [r.fetched for r in reports] == [3] and not reports[0].error
    msgs = hub.db.list_messages()
    assert len(msgs) == 3
    invoice = next(m for m in msgs if "Invoice" in m["subject"])
    assert invoice["flagged"] and not invoice["seen"] and "invoice" in invoice["tags"] and "urgent" in invoice["tags"]
    assert hub.db.get_folder_state(hub.db.get_account("me@gmail.com")["id"], "INBOX") == (1, 3)

    # Nothing new -> nothing fetched
    assert hub.sync_all()[0].fetched == 0

    # Batches cap incremental pulls (sync_batch=2): 3 new messages -> newest 2 first.
    from tests.conftest import make_raw
    for uid in (4, 5, 6):
        fake_imap.folders["INBOX"][uid] = (make_raw(f"Bid {uid}", message_id=f"<b{uid}@x>"), False, False)
    assert hub.sync_all()[0].fetched == 2
    assert hub.sync_all()[0].fetched == 0  # uids 5,6 were taken; uid 4 is below the cursor and skipped by design
    assert len(hub.db.list_messages(limit=100)) == 5
    assert fake_imap.closed


def test_uidvalidity_change_resets_folder(hub, fake_imap):
    connect(hub)
    hub.sync_all()
    fake_imap.uidvalidity = 99
    report = hub.sync_all()[0]
    assert report.reset and report.fetched == 3
    assert len(hub.db.list_messages()) == 3


def test_login_failure_is_reported_not_raised(hub):
    connect(hub, "broken@gmail.com", test=False)
    reports = hub.sync_all()
    assert reports[0].error and "AUTHENTICATIONFAILED" in reports[0].error
    assert "login" in hub.db.get_account("broken@gmail.com")["last_error"]


def test_search_threads_and_flags(hub, fake_imap):
    connect(hub)
    hub.sync_all()
    hits = hub.db.list_messages(query="permit oak")
    assert {h["subject"] for h in hits} == {"Permit approved for 12 Oak St", "Re: Permit approved for 12 Oak St"}
    assert hub.db.list_messages(query="supplyco")[0]["from_addr"] == "billing@supplyco.com"
    thread = hub.db.thread(hits[0]["thread_key"])
    assert [m["uid"] for m in thread] == [1, 3]

    root = next(m for m in hits if m["uid"] == 1)
    updated = hub.mark(root["id"], seen=False, flagged=True)
    assert not updated["seen"] and updated["flagged"]
    assert (1, "seen", False) in fake_imap.stored_flags and (1, "flagged", True) in fake_imap.stored_flags
    assert root["id"] in {m["id"] for m in hub.db.list_messages(unread_only=True, flagged_only=True)}


def test_send_and_reply_threading(hub, fake_imap, fake_smtp):
    connect(hub, "ops@company.com", display_name="Ops", imap_host="mail.company.com", smtp_host="mail.company.com")
    hub.sync_all()
    original = next(m for m in hub.db.list_messages() if m["uid"] == 1)

    res = hub.send("ops@company.com", ["sub@vendor.com"], "", "On it.", reply_to_message_id=original["id"])
    msg, creds = fake_smtp.sent[-1]
    assert res["subject"] == "Re: Permit approved for 12 Oak St"
    assert msg["In-Reply-To"] == "<a1@x>" and msg["References"] == "<a1@x>"
    assert msg["From"] == "Ops <ops@company.com>" and creds["password"] == "app-pass"
    # Non-Gmail providers get a copy appended to the Sent folder.
    assert fake_imap.appended and fake_imap.appended[-1][0] == "Sent"

    hub.send("ops@company.com", ["a@b.com"], "Plain", "hi", cc=["c@d.com"])
    assert fake_smtp.sent[-1][0]["Cc"] == "c@d.com"
    with pytest.raises(ValueError):
        hub.send("nobody@x.com", ["a@b.com"], "x", "y")


def test_remove_account_cascades(hub):
    connect(hub)
    hub.sync_all()
    assert hub.remove_account("me@gmail.com")
    assert hub.db.list_messages() == [] and hub.db.overview()["accounts"] == []
    assert not hub.remove_account("me@gmail.com")
