"""SQLite storage for accounts, sync cursors and the unified message index.

Single-file, zero-ops, WAL mode.  Full-text search over subject / sender /
body via FTS5 (external-content table kept in sync by triggers).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    address       TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL,
    imap_host     TEXT NOT NULL,
    imap_port     INTEGER NOT NULL DEFAULT 993,
    smtp_host     TEXT NOT NULL DEFAULT '',
    smtp_port     INTEGER NOT NULL DEFAULT 587,
    smtp_ssl      INTEGER NOT NULL DEFAULT 0,
    auth_method   TEXT NOT NULL DEFAULT 'password',   -- password | oauth2
    username      TEXT NOT NULL,
    secret_enc    TEXT NOT NULL,                       -- app password OR oauth refresh token
    oauth_client_id     TEXT NOT NULL DEFAULT '',
    oauth_client_secret_enc TEXT NOT NULL DEFAULT '',
    oauth_token_url     TEXT NOT NULL DEFAULT '',
    sent_folder   TEXT NOT NULL DEFAULT 'Sent',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_sync_at  TEXT,
    last_error    TEXT
);

CREATE TABLE IF NOT EXISTS folder_state (
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    folder       TEXT NOT NULL,
    uidvalidity  INTEGER NOT NULL DEFAULT 0,
    last_uid     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, folder)
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    folder          TEXT NOT NULL,
    uid             INTEGER NOT NULL,
    message_id      TEXT NOT NULL DEFAULT '',
    in_reply_to     TEXT NOT NULL DEFAULT '',
    thread_key      TEXT NOT NULL DEFAULT '',
    subject         TEXT NOT NULL DEFAULT '',
    from_addr       TEXT NOT NULL DEFAULT '',
    from_name       TEXT NOT NULL DEFAULT '',
    to_addrs        TEXT NOT NULL DEFAULT '[]',
    cc_addrs        TEXT NOT NULL DEFAULT '[]',
    date            TEXT,                         -- ISO-8601 UTC from the Date header
    snippet         TEXT NOT NULL DEFAULT '',
    body_text       TEXT NOT NULL DEFAULT '',
    body_html       TEXT NOT NULL DEFAULT '',
    attachments     TEXT NOT NULL DEFAULT '[]',   -- JSON [{filename,content_type,size}]
    seen            INTEGER NOT NULL DEFAULT 0,
    flagged         INTEGER NOT NULL DEFAULT 0,
    size            INTEGER NOT NULL DEFAULT 0,
    synced_at       TEXT NOT NULL,
    UNIQUE (account_id, folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date DESC);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_key);
CREATE INDEX IF NOT EXISTS idx_messages_msgid ON messages(message_id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, from_addr, from_name, body_text,
    content='messages', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, from_addr, from_name, body_text)
    VALUES (new.id, new.subject, new.from_addr, new.from_name, new.body_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, from_addr, from_name, body_text)
    VALUES ('delete', old.id, old.subject, old.from_addr, old.from_name, old.body_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF subject, from_addr, from_name, body_text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, from_addr, from_name, body_text)
    VALUES ('delete', old.id, old.subject, old.from_addr, old.from_name, old.body_text);
    INSERT INTO messages_fts(rowid, subject, from_addr, from_name, body_text)
    VALUES (new.id, new.subject, new.from_addr, new.from_name, new.body_text);
END;

CREATE TABLE IF NOT EXISTS message_tags (
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    PRIMARY KEY (message_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_message_tags_tag ON message_tags(tag);

CREATE TABLE IF NOT EXISTS sent_log (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    message_id  TEXT NOT NULL,
    to_addrs    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    in_reply_to TEXT NOT NULL DEFAULT '',
    sent_at     TEXT NOT NULL
);
"""

MESSAGE_LIST_COLUMNS = (
    "m.id, m.account_id, a.address AS account, m.folder, m.uid, m.message_id, m.in_reply_to, "
    "m.thread_key, m.subject, m.from_addr, m.from_name, m.to_addrs, m.cc_addrs, m.date, "
    "m.snippet, m.attachments, m.seen, m.flagged, m.size, m.synced_at"
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fts_query(q: str) -> str:
    """Turn free text into a safe FTS5 query: quoted prefix terms AND-ed."""
    terms = [t.replace('"', '""') for t in q.split() if t.strip()]
    return " ".join(f'"{t}"*' for t in terms) if terms else '""'


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ accounts
    def add_account(self, **fields: Any) -> int:
        fields.setdefault("created_at", utcnow())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self._conn.execute(f"INSERT INTO accounts ({cols}) VALUES ({marks})", tuple(fields.values()))
        return int(cur.lastrowid)

    def get_account(self, ident: int | str) -> sqlite3.Row | None:
        if isinstance(ident, int) or str(ident).isdigit():
            return self._conn.execute("SELECT * FROM accounts WHERE id=?", (int(ident),)).fetchone()
        return self._conn.execute("SELECT * FROM accounts WHERE address=?", (ident,)).fetchone()

    def list_accounts(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM accounts" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
        return self._conn.execute(sql).fetchall()

    def update_account(self, account_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE accounts SET {sets} WHERE id=?", (*fields.values(), account_id))

    def remove_account(self, account_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        return cur.rowcount > 0

    def account_stats(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT a.id, a.address, a.display_name, a.provider, a.enabled, a.last_sync_at, a.last_error,
                      COUNT(m.id) AS messages, COALESCE(SUM(CASE WHEN m.seen=0 THEN 1 ELSE 0 END),0) AS unread
               FROM accounts a LEFT JOIN messages m ON m.account_id=a.id
               GROUP BY a.id ORDER BY a.id"""
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ folder cursors
    def get_folder_state(self, account_id: int, folder: str) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT uidvalidity, last_uid FROM folder_state WHERE account_id=? AND folder=?",
            (account_id, folder),
        ).fetchone()
        return (row["uidvalidity"], row["last_uid"]) if row else (0, 0)

    def set_folder_state(self, account_id: int, folder: str, uidvalidity: int, last_uid: int) -> None:
        self._conn.execute(
            """INSERT INTO folder_state(account_id, folder, uidvalidity, last_uid) VALUES (?,?,?,?)
               ON CONFLICT(account_id, folder) DO UPDATE SET uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid""",
            (account_id, folder, uidvalidity, last_uid),
        )

    def reset_folder(self, account_id: int, folder: str) -> None:
        """UIDVALIDITY changed: the server renumbered everything, drop and refetch."""
        self._conn.execute("DELETE FROM messages WHERE account_id=? AND folder=?", (account_id, folder))
        self._conn.execute("DELETE FROM folder_state WHERE account_id=? AND folder=?", (account_id, folder))

    # ------------------------------------------------------------ messages
    def upsert_message(self, account_id: int, folder: str, uid: int, parsed: dict, seen: bool, flagged: bool) -> int:
        row = {
            "account_id": account_id,
            "folder": folder,
            "uid": uid,
            "message_id": parsed.get("message_id", ""),
            "in_reply_to": parsed.get("in_reply_to", ""),
            "thread_key": parsed.get("thread_key", ""),
            "subject": parsed.get("subject", ""),
            "from_addr": parsed.get("from_addr", ""),
            "from_name": parsed.get("from_name", ""),
            "to_addrs": json.dumps(parsed.get("to_addrs", [])),
            "cc_addrs": json.dumps(parsed.get("cc_addrs", [])),
            "date": parsed.get("date"),
            "snippet": parsed.get("snippet", ""),
            "body_text": parsed.get("body_text", ""),
            "body_html": parsed.get("body_html", ""),
            "attachments": json.dumps(parsed.get("attachments", [])),
            "seen": int(seen),
            "flagged": int(flagged),
            "size": int(parsed.get("size", 0)),
            "synced_at": utcnow(),
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k not in ("account_id", "folder", "uid"))
        self._conn.execute(
            f"INSERT INTO messages ({cols}) VALUES ({marks}) ON CONFLICT(account_id, folder, uid) DO UPDATE SET {updates}",
            tuple(row.values()),
        )
        return int(
            self._conn.execute(
                "SELECT id FROM messages WHERE account_id=? AND folder=? AND uid=?", (account_id, folder, uid)
            ).fetchone()["id"]
        )

    def set_flags(self, message_id: int, seen: bool | None = None, flagged: bool | None = None) -> None:
        if seen is not None:
            self._conn.execute("UPDATE messages SET seen=? WHERE id=?", (int(seen), message_id))
        if flagged is not None:
            self._conn.execute("UPDATE messages SET flagged=? WHERE id=?", (int(flagged), message_id))

    def get_message(self, message_id: int) -> dict | None:
        row = self._conn.execute(
            f"SELECT {MESSAGE_LIST_COLUMNS}, m.body_text, m.body_html FROM messages m JOIN accounts a ON a.id=m.account_id WHERE m.id=?",
            (message_id,),
        ).fetchone()
        return self._hydrate(row) if row else None

    def list_messages(
        self,
        *,
        account_id: int | None = None,
        folder: str | None = None,
        unread_only: bool = False,
        flagged_only: bool = False,
        tag: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        where: list[str] = []
        params: list[Any] = []
        joins = "JOIN accounts a ON a.id=m.account_id"
        if query:
            joins += " JOIN messages_fts f ON f.rowid=m.id"
            where.append("messages_fts MATCH ?")
            params.append(_fts_query(query))
        if account_id is not None:
            where.append("m.account_id=?"); params.append(account_id)
        if folder:
            where.append("m.folder=?"); params.append(folder)
        if unread_only:
            where.append("m.seen=0")
        if flagged_only:
            where.append("m.flagged=1")
        if tag:
            where.append("EXISTS (SELECT 1 FROM message_tags t WHERE t.message_id=m.id AND t.tag=?)")
            params.append(tag)
        sql = f"SELECT {MESSAGE_LIST_COLUMNS} FROM messages m {joins}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.date DESC, m.id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [self._hydrate(r) for r in self._conn.execute(sql, params).fetchall()]

    def thread(self, thread_key: str) -> list[dict]:
        rows = self._conn.execute(
            f"SELECT {MESSAGE_LIST_COLUMNS}, m.body_text, m.body_html FROM messages m JOIN accounts a ON a.id=m.account_id "
            "WHERE m.thread_key=? ORDER BY m.date ASC, m.id ASC",
            (thread_key,),
        ).fetchall()
        return [self._hydrate(r) for r in rows]

    def _hydrate(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in ("to_addrs", "cc_addrs", "attachments"):
            if k in d and isinstance(d[k], str):
                d[k] = json.loads(d[k] or "[]")
        d["seen"] = bool(d.get("seen"))
        d["flagged"] = bool(d.get("flagged"))
        d["tags"] = self.tags_for(d["id"])
        return d

    # ------------------------------------------------------------ tags
    def add_tags(self, message_id: int, tags: list[str]) -> None:
        self._conn.executemany(
            "INSERT OR IGNORE INTO message_tags(message_id, tag) VALUES (?,?)",
            [(message_id, t.strip().lower()) for t in tags if t.strip()],
        )

    def remove_tag(self, message_id: int, tag: str) -> None:
        self._conn.execute("DELETE FROM message_tags WHERE message_id=? AND tag=?", (message_id, tag.lower()))

    def tags_for(self, message_id: int) -> list[str]:
        return [r["tag"] for r in self._conn.execute(
            "SELECT tag FROM message_tags WHERE message_id=? ORDER BY tag", (message_id,)).fetchall()]

    def tag_counts(self) -> dict[str, int]:
        return {r["tag"]: r["n"] for r in self._conn.execute(
            "SELECT tag, COUNT(*) AS n FROM message_tags GROUP BY tag ORDER BY n DESC").fetchall()}

    # ------------------------------------------------------------ sent log
    def log_sent(self, account_id: int, message_id: str, to_addrs: list[str], subject: str, in_reply_to: str = "") -> None:
        self._conn.execute(
            "INSERT INTO sent_log(account_id, message_id, to_addrs, subject, in_reply_to, sent_at) VALUES (?,?,?,?,?,?)",
            (account_id, message_id, json.dumps(to_addrs), subject, in_reply_to, utcnow()),
        )

    def overview(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        unread = self._conn.execute("SELECT COUNT(*) AS n FROM messages WHERE seen=0").fetchone()["n"]
        return {"accounts": self.account_stats(), "messages": total, "unread": unread, "tags": self.tag_counts()}
