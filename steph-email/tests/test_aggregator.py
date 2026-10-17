from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import threading

from cryptography.fernet import Fernet
import pytest

from steph_email.aggregator import EmailAggregator, parse_message
from steph_email.oauth import AuthenticationError, TokenProvider

UTC = timezone.utc
RAW = b"From: Builder <builder@example.com>\r\nSubject: Permit arrived\r\nDate: Mon, 1 Jan 2001 00:00:00 +0000\r\nMessage-ID: <one@example.com>\r\n\r\nInspection Friday"


class MemoryStore:
    def __init__(self):
        self.cursors = {}
        self.accounts = {}
        self.settings = {}

    def get_cursor(self, account, folder):
        return self.cursors.get((account, folder))

    def set_cursor(self, account, folder, uidvalidity, last_uid):
        self.cursors[account, folder] = {"uidvalidity": str(uidvalidity), "last_uid": last_uid}

    def record_account(self, account, status, detail=""):
        self.accounts[account] = (status, detail)

    def get_settings(self):
        return self.settings


class FakeIMAP:
    def __init__(self, messages=None, validity=1, uidnext=None):
        self.messages = messages or {1: RAW}
        self.validity = validity
        self.uidnext = uidnext or max(self.messages) + 1
        self.fetches = []
        self.searches = []
        self.readonly = None
        self.in_idle = False
        self.events = []
        self.received = datetime.now(UTC)
        self.closed = False

    def login(self, username, password):
        self.username = username

    def oauth2_login(self, username, token):
        self.username = username

    def select_folder(self, folder, readonly=False):
        assert not self.in_idle
        self.readonly = readonly
        return {b"UIDVALIDITY": self.validity, b"UIDNEXT": self.uidnext}

    def search(self, criteria):
        assert not self.in_idle
        self.searches.append(criteria)
        start, end = map(int, criteria[1].split(":"))
        return [uid for uid in self.messages if start <= uid <= end]

    def fetch(self, uids, fields):
        assert not self.in_idle
        self.fetches.append((uids, fields))
        uid = uids[0]
        raw = self.messages[uid]
        if "RFC822.SIZE" in fields:
            return {uid: {b"RFC822.SIZE": len(raw), b"INTERNALDATE": self.received}}
        assert fields[0].startswith("BODY.PEEK[")
        if "HEADER" in fields[0]:
            return {uid: {b"BODY[HEADER]<0>": raw.split(b"\r\n\r\n")[0][:65536] + b"\r\n\r\n"}}
        return {uid: {b"BODY[]<0>": raw}}

    def has_capability(self, capability):
        return True

    def idle(self):
        assert not self.in_idle
        self.in_idle = True
        self.events.append("idle")

    def idle_check(self, timeout=None):
        assert self.in_idle
        self.events.append("check")
        return [(1, b"EXISTS")]

    def idle_done(self):
        assert self.in_idle
        self.in_idle = False
        self.events.append("done")
        return b"done", []

    def logout(self):
        assert not self.in_idle
        self.closed = True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_APP_PASSWORD", "fake-password")
    account = {"id": "test", "enabled": True, "provider": "imap", "host": "imap.invalid",
               "username": "mailbox@example.com", "auth": {"type": "app_password", "password_env": "TEST_APP_PASSWORD"}}
    config = SimpleNamespace(data_dir=tmp_path, accounts=[account], settings={"aggregation_enabled": True})
    return config, MemoryStore(), account


def aggregator(config, store, client):
    def factory(host, **kwargs):
        assert kwargs["ssl"] and kwargs["use_uid"]
        assert kwargs["ssl_context"].check_hostname
        return client
    return EmailAggregator(config, store, client_factory=factory)


def test_read_only_peek_arrival_and_cursor_after_commit(setup):
    config, store, _ = setup
    client = FakeIMAP()
    engine = aggregator(config, store, client)
    seen = []
    def commit(email):
        assert store.get_cursor("test", "INBOX") is None
        seen.append(email)
    result = engine.run_cycle(commit)
    assert result["test"]["processed"] == 1
    assert client.readonly is True
    assert client.normalise_times is False
    assert seen[0].received_at == client.received
    assert seen[0].received_at.year != 2001
    assert seen[0].sender == "builder@example.com"
    assert store.get_cursor("test", "INBOX")["last_uid"] == 1
    engine.run_cycle(lambda e: pytest.fail("duplicate"))


def test_callback_failure_retries_without_skipping_later_mail(setup):
    config, store, _ = setup
    client = FakeIMAP({1: RAW, 2: RAW})
    engine = aggregator(config, store, client)
    def failed(email):
        raise RuntimeError("PRIVATE MESSAGE CONTENT")
    result = engine.run_cycle(failed)
    assert result["test"]["status"] == "error"
    assert store.get_cursor("test", "INBOX") is None
    assert "PRIVATE" not in str(store.accounts)
    emails = []
    engine.run_cycle(emails.append)
    assert [e.uid for e in emails] == ["1", "2"]


def test_uidvalidity_reset_preserves_distinct_identity(setup):
    config, store, _ = setup
    store.set_cursor("test", "INBOX", "old", 900)
    emails = []
    aggregator(config, store, FakeIMAP(validity=2)).run_cycle(emails.append)
    assert emails[0].uidvalidity == "2"
    assert store.get_cursor("test", "INBOX")["last_uid"] == 1


@pytest.mark.parametrize("changed", [{"host": "another.invalid"}, {"port": 1993},
                                      {"username": "another@example.com"}])
def test_account_identity_change_rejected_before_network_after_restart(setup, changed):
    config, store, account = setup
    aggregator(config, store, FakeIMAP()).run_cycle(lambda e: None)
    original_cursor = dict(store.get_cursor("test", "INBOX"))
    account.update(changed)
    restarted = EmailAggregator(config, store,
        client_factory=lambda *a, **k: pytest.fail("changed mailbox must not connect"))
    result = restarted.run_cycle(lambda e: pytest.fail("changed mailbox must not ingest"))
    assert result["test"]["status"] == "error"
    assert "new account id" in result["test"]["detail"]
    assert store.get_cursor("test", "INBOX") == original_cursor


def test_new_account_id_allows_distinct_mailbox_and_host_case_is_normalized(setup):
    config, store, account = setup
    aggregator(config, store, FakeIMAP()).run_cycle(lambda e: None)
    account["host"] = "IMAP.INVALID."
    assert "status" not in aggregator(config, store, FakeIMAP()).run_cycle(lambda e: None)["test"]
    account.update(id="new", username="another@example.com")
    emails = []
    aggregator(config, store, FakeIMAP()).run_cycle(emails.append)
    assert len(emails) == 1 and emails[0].account == "new"


def test_first_sync_date_filter_is_durable_and_not_applied_to_new_mail(setup):
    config, store, account = setup
    account["batch_size"] = 1
    client = FakeIMAP({1: RAW, 2: RAW})
    aggregator(config, store, client).run_cycle(lambda e: None)
    assert "SINCE" in client.searches[0]
    # Restart with existing cursor; still honors original first-sync snapshot.
    engine = aggregator(config, store, client)
    engine.run_cycle(lambda e: None)
    assert "SINCE" in client.searches[-1]
    client.messages[3] = RAW
    client.uidnext = 4
    engine.run_cycle(lambda e: None)
    assert "SINCE" not in client.searches[-1]


def test_exact_lookback_does_not_import_old_mail(setup):
    config, store, _ = setup
    client = FakeIMAP()
    client.received -= timedelta(days=31)
    aggregator(config, store, client).run_cycle(lambda e: pytest.fail("outside lookback"))
    assert store.get_cursor("test", "INBOX")["last_uid"] == 1


def test_body_size_limit_uses_header_only_fetch(setup):
    config, store, account = setup
    account["max_message_bytes"] = 65536
    client = FakeIMAP({1: RAW + b"x" * 70000})
    emails = []
    aggregator(config, store, client).run_cycle(emails.append)
    assert client.fetches[-1][1] == ["BODY.PEEK[HEADER]<0.65536>"]
    assert emails[0].subject == "Permit arrived"
    assert emails[0].attachments[0]["omitted"] is True
    assert "Body omitted" in emails[0].text


def test_missing_timezone_blocks_cursor(setup):
    config, store, _ = setup
    client = FakeIMAP()
    client.received = datetime.now()
    assert aggregator(config, store, client).run_cycle(lambda e: None)["test"]["status"] == "error"
    assert store.get_cursor("test", "INBOX") is None


def test_outlook_basic_auth_blocked_before_network(setup):
    config, store, account = setup
    account["provider"] = "outlook"
    engine = EmailAggregator(config, store, client_factory=lambda *a, **k: pytest.fail("network"))
    assert engine.run_cycle(lambda e: None)["test"]["status"] == "error"


def test_disabled_aggregation_never_connects(setup):
    config, store, _ = setup
    store.settings["aggregation_enabled"] = False
    engine = EmailAggregator(config, store, client_factory=lambda *a, **k: pytest.fail("network"))
    assert engine.run_cycle(lambda e: None) == {"status": "disabled"}


def test_sparse_mailbox_scans_bounded_uid_ranges(setup):
    config, store, account = setup
    account["scan_windows_per_cycle"] = 2
    account["uid_scan_window"] = 1000
    client = FakeIMAP({99999: RAW})
    result = aggregator(config, store, client).run_cycle(lambda e: pytest.fail("not reached yet"))
    assert len(client.searches) == 2
    assert client.searches[0][1] == "1:1000"
    assert client.searches[1][1] == "1001:2000"
    assert result["test"]["backlog"] is True
    assert store.get_cursor("test", "INBOX")["last_uid"] == 2000


def test_idle_finishes_before_wakeup_or_logout(setup):
    config, store, account = setup
    client = FakeIMAP()
    engine = aggregator(config, store, client)
    stop = threading.Event()
    calls = []
    def wake():
        assert not client.in_idle
        calls.append(True)
        if len(calls) == 2:
            stop.set()
    engine._idle_loop(account, wake, stop)
    assert client.events == ["idle", "check", "done"]
    assert client.closed


def test_idle_read_failure_still_completes_done(setup):
    config, store, account = setup
    client = FakeIMAP()
    engine = aggregator(config, store, client)
    stop = threading.Event()
    def fail(timeout):
        stop.set()
        raise OSError("disconnected")
    client.idle_check = fail
    engine._idle_loop(account, lambda: None, stop)
    assert client.events == ["idle", "done"]
    assert client.closed


def test_mime_html_is_text_and_attachments_are_metadata():
    raw = (b"From: a@example.com\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
           b"--x\r\nContent-Type: text/html\r\n\r\n<script>SECRET()</script><p>Job ready</p><img src=https://tracker.invalid>\r\n"
           b"--x\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=bid.pdf\r\n\r\nPDFDATA\r\n--x--")
    email = parse_message("a", "INBOX", "1", "1", raw, datetime.now(UTC))
    assert "Job ready" in email.text
    assert "SECRET" not in email.text and "tracker.invalid" not in email.text
    assert email.attachments == [{"filename": "bid.pdf", "content_type": "application/pdf", "size": None}]


class FakeHTTPSession:
    def __init__(self):
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {
            "access_token": "ACCESS-SECRET", "refresh_token": "ROTATED-SECRET", "expires_in": 3600})


def oauth_account(monkeypatch):
    monkeypatch.setenv("TEST_CLIENT_ID", "client")
    monkeypatch.setenv("TEST_REFRESH_TOKEN", "INITIAL-SECRET")
    monkeypatch.setenv("STEPH_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return {"id": "gmail", "username": "a@example.com", "auth": {
        "type": "oauth2", "token_url": "https://oauth2.googleapis.com/token",
        "client_id_env": "TEST_CLIENT_ID", "refresh_token_env": "TEST_REFRESH_TOKEN"}}


def test_oauth_rotation_encrypted_and_survives_restart(tmp_path, monkeypatch):
    account = oauth_account(monkeypatch)
    session = FakeHTTPSession()
    provider = TokenProvider(tmp_path, session)
    assert provider.access_token(account) == "ACCESS-SECRET"
    assert provider.access_token(account) == "ACCESS-SECRET"
    assert len(session.calls) == 1
    saved = (tmp_path / "oauth-tokens.json").read_text()
    assert "SECRET" not in saved
    assert (tmp_path / "oauth-tokens.json").stat().st_mode & 0o777 == 0o600
    TokenProvider(tmp_path, session).access_token(account)
    assert session.calls[-1][1]["data"]["refresh_token"] == "ROTATED-SECRET"
    assert session.calls[-1][1]["allow_redirects"] is False


@pytest.mark.parametrize("url", ["http://oauth2.googleapis.com/token", "https://evil.invalid/token",
    "https://oauth2.googleapis.com/token?destination=evil", "https://user@oauth2.googleapis.com/token"])
def test_oauth_endpoint_allowlist(tmp_path, monkeypatch, url):
    account = oauth_account(monkeypatch)
    account["auth"]["token_url"] = url
    session = FakeHTTPSession()
    with pytest.raises(AuthenticationError):
        TokenProvider(tmp_path, session).access_token(account)
    assert not session.calls


def test_oauth_failure_does_not_expose_provider_response(tmp_path, monkeypatch):
    account = oauth_account(monkeypatch)
    session = SimpleNamespace(post=lambda *a, **k: SimpleNamespace(status_code=400, text="PRIVATE-TOKEN"))
    with pytest.raises(AuthenticationError) as error:
        TokenProvider(tmp_path, session).access_token(account)
    assert "PRIVATE" not in str(error.value)
