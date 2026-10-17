"""Exercise the real ImapClient parsing against a stubbed imaplib connection."""

from steph_email.imap_client import ImapClient


class StubImap:
    def __init__(self):
        self.calls = []

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder)); return "OK", [b"3"]

    def status(self, folder, item):
        return ("OK", [b'"INBOX" (UIDVALIDITY 1725000000)']) if "UIDVALIDITY" in item else ("OK", [b'"INBOX" (UIDNEXT 42)'])

    def uid(self, cmd, *args):
        self.calls.append(("uid", cmd, args))
        if cmd == "SEARCH":
            return "OK", [b"10 11 12"]
        if cmd == "FETCH":
            return "OK", [(b"1 (UID 11 FLAGS (\\Seen \\Flagged) BODY[] {20}", b"Subject: a\r\n\r\nbody"), b")",
                          (b"2 (UID 12 FLAGS () BODY[] {20}", b"Subject: b\r\n\r\nbody"), b")"]
        return "OK", [b""]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "[Gmail]/Sent Mail"', b'(\\HasNoChildren) "." Archive']

    def create(self, folder):
        self.calls.append(("create", folder)); return "OK", [b""]

    def append(self, folder, flags, date, raw):
        self.calls.append(("append", folder))

    def close(self): pass
    def logout(self): pass


def make_client():
    client = ImapClient.__new__(ImapClient)
    stub = StubImap()
    client._imap = stub
    client._folder = None
    return client, stub


def test_select_status_and_uid_search_filtering():
    client, stub = make_client()
    assert client.select("INBOX") == (1725000000, 42)
    assert stub.calls[0] == ("select", '"INBOX"')
    assert client.uids_after(10, 0) == [11, 12]
    assert client.uids_after(10, 1) == [12]
    assert client.uids_after(12, 5) == []


def test_fetch_parses_uid_flags_and_body():
    client, _ = make_client()
    msgs = client.fetch([11, 12])
    assert [(m.uid, m.seen, m.flagged) for m in msgs] == [(11, True, True), (12, False, False)]
    assert msgs[0].raw.startswith(b"Subject: a")


def test_list_folders_flags_and_append():
    client, stub = make_client()
    assert client.list_folders() == ["INBOX", "[Gmail]/Sent Mail", "Archive"]
    client.set_seen(5, True); client.set_flagged(5, False); client.append("Sent", b"raw")
    assert ("uid", "STORE", ("5", "+FLAGS", "(\\Seen)")) in stub.calls
    assert ("uid", "STORE", ("5", "-FLAGS", "(\\Flagged)")) in stub.calls
    assert ("append", '"Sent"') in stub.calls


def test_copy_and_ensure_folder_never_delete():
    client, stub = make_client()
    client.ensure_folder("Archive")                # exists -> no create
    client.ensure_folder("Steph/Archive")          # missing -> create
    client.copy_to(11, "Steph/Archive")
    assert ("create", '"Steph/Archive"') in stub.calls
    assert ("uid", "COPY", ("11", '"Steph/Archive"')) in stub.calls
    assert not any(c[1] in ("EXPUNGE", "MOVE") or (c[1] == "STORE" and "Deleted" in str(c)) for c in stub.calls if c[0] == "uid")
    assert not hasattr(client, "delete") and not hasattr(client, "expunge") and not hasattr(client, "move")
