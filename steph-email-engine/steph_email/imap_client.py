"""Thin, testable wrapper over ``imaplib`` (adapted from NoblePort MailHub).

The engine only relies on the ``MailboxClient`` protocol below, so tests
substitute an in-memory fake and never open a socket.

There is deliberately no DELETE / EXPUNGE / MOVE here: the engine can COPY a
message into an archive folder, but the original is never removed.
"""

from __future__ import annotations

import imaplib
import re
import ssl
from dataclasses import dataclass
from typing import Iterable, Protocol

_UID = re.compile(rb"UID (\d+)")
_FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")


@dataclass
class FetchedMessage:
    uid: int
    raw: bytes
    seen: bool
    flagged: bool


class MailboxClient(Protocol):
    def select(self, folder: str) -> tuple[int, int]: ...          # (uidvalidity, uidnext)
    def uids_after(self, last_uid: int, limit: int) -> list[int]: ...
    def fetch(self, uids: Iterable[int]) -> list[FetchedMessage]: ...
    def set_seen(self, uid: int, seen: bool) -> None: ...
    def set_flagged(self, uid: int, flagged: bool) -> None: ...
    def list_folders(self) -> list[str]: ...
    def append(self, folder: str, raw: bytes) -> None: ...
    def copy_to(self, uid: int, folder: str) -> None: ...
    def ensure_folder(self, folder: str) -> None: ...
    def close(self) -> None: ...


class ImapClient:
    def __init__(self, host: str, port: int = 993, timeout: float = 30.0):
        ctx = ssl.create_default_context()
        self._imap = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=timeout)
        self._folder: str | None = None

    # ---- auth
    def login(self, user: str, password: str) -> None:
        self._imap.login(user, password)

    def login_oauth2(self, user: str, access_token: str) -> None:
        from .oauth import xoauth2_string
        self._imap.authenticate("XOAUTH2", lambda _: xoauth2_string(user, access_token).encode())

    # ---- protocol
    def select(self, folder: str) -> tuple[int, int]:
        typ, data = self._imap.select(_quote(folder), readonly=False)
        if typ != "OK":
            raise RuntimeError(f"cannot select folder {folder!r}: {data}")
        self._folder = folder
        return self._status_int(folder, "UIDVALIDITY"), self._status_int(folder, "UIDNEXT")

    def _status_int(self, folder: str, item: str) -> int:
        typ, data = self._imap.status(_quote(folder), f"({item})")
        if typ != "OK" or not data or data[0] is None:
            return 0
        blob = data[0] if isinstance(data[0], bytes) else str(data[0]).encode()
        m = re.search((item + r" (\d+)").encode(), blob)
        return int(m.group(1)) if m else 0

    def uids_after(self, last_uid: int, limit: int) -> list[int]:
        typ, data = self._imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if typ != "OK" or not data or not data[0]:
            return []
        uids = sorted(int(u) for u in data[0].split())
        uids = [u for u in uids if u > last_uid]   # servers echo N for "N:*" when nothing is newer
        return uids[-limit:] if limit and len(uids) > limit else uids

    def fetch(self, uids: Iterable[int]) -> list[FetchedMessage]:
        uids = list(uids)
        if not uids:
            return []
        typ, data = self._imap.uid("FETCH", ",".join(str(u) for u in uids), "(UID FLAGS BODY.PEEK[])")
        if typ != "OK":
            raise RuntimeError(f"FETCH failed: {data}")
        out: list[FetchedMessage] = []
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            meta, raw = item[0], item[1]
            m_uid = _UID.search(meta)
            if not m_uid:
                continue
            flags = _FLAGS.search(meta)
            flagset = flags.group(1).decode(errors="ignore") if flags else ""
            out.append(FetchedMessage(uid=int(m_uid.group(1)), raw=raw,
                                      seen="\\Seen" in flagset, flagged="\\Flagged" in flagset))
        return out

    def set_seen(self, uid: int, seen: bool) -> None:
        self._imap.uid("STORE", str(uid), "+FLAGS" if seen else "-FLAGS", "(\\Seen)")

    def set_flagged(self, uid: int, flagged: bool) -> None:
        self._imap.uid("STORE", str(uid), "+FLAGS" if flagged else "-FLAGS", "(\\Flagged)")

    def list_folders(self) -> list[str]:
        typ, data = self._imap.list()
        if typ != "OK":
            return []
        names: list[str] = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            m = re.search(rb'\) "?([^"]*)"? (?:"(.*)"|(\S+))$', line)
            if m:
                name = m.group(2) if m.group(2) is not None else m.group(3)
                names.append(name.decode("utf-8", errors="replace"))
        return names

    def append(self, folder: str, raw: bytes) -> None:
        self._imap.append(_quote(folder), "(\\Seen)", None, raw)

    def copy_to(self, uid: int, folder: str) -> None:
        """Server-side COPY. The message stays where it is; a duplicate lands in ``folder``."""
        typ, data = self._imap.uid("COPY", str(uid), _quote(folder))
        if typ != "OK":
            raise RuntimeError(f"COPY to {folder!r} failed: {data}")

    def ensure_folder(self, folder: str) -> None:
        if folder in self.list_folders():
            return
        typ, data = self._imap.create(_quote(folder))
        if typ != "OK":
            raise RuntimeError(f"cannot create folder {folder!r}: {data}")

    def close(self) -> None:
        try:
            if self._folder:
                self._imap.close()
        except Exception:
            pass
        try:
            self._imap.logout()
        except Exception:
            pass


def _quote(folder: str) -> str:
    return '"' + folder.replace('"', '\\"') + '"'
