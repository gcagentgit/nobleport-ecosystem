"""SQLite storage: accounts, sync cursors, unified messages with urgency,
expected replies, briefs, notifications and the cleanup log.

Single file, WAL mode, FTS5 over subject / sender / body.
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
    auth_method   TEXT NOT NULL DEFAULT 'password',
    username      TEXT NOT NULL,
    secret_enc    TEXT NOT NULL,
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
    date            TEXT,
    snippet         TEXT NOT NULL DEFAULT '',
    body_text       TEXT NOT NULL DEFAULT '',
    body_html       TEXT NOT NULL DEFAULT '',
    attachments     TEXT NOT NULL DEFAULT '[]',
    seen            INTEGER NOT NULL DEFAULT 0,
    flagged         INTEGER NOT NULL DEFAULT 0,
    size            INTEGER NOT NULL DEFAULT 0,
    synced_at       TEXT NOT NULL,
    urgency         TEXT NOT NULL DEFAULT 'normal',
    urgency_score   INTEGER NOT NULL DEFAULT 0,
    urgency_reasons TEXT NOT NULL DEFAULT '[]',
    needs_reply     INTEGER NOT NULL DEFAULT 0,
    archived        INTEGER NOT NULL DEFAULT 0,
    archived_at     TEXT,
    raw_path        TEXT NOT NULL DEFAULT '',
    UNIQUE (account_id, folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date DESC);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_key);
CREATE INDEX IF NOT EXISTS idx_messages_msgid ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_messages_urgency ON messages(urgency);

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

CREATE TABLE IF NOT EXISTS expected_replies (
    id                 INTEGER PRIMARY KEY,
    account_id         INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    message_id         TEXT NOT NULL,          -- Message-ID of what we sent
    thread_key         TEXT NOT NULL,
    to_addrs           TEXT NOT NULL,          -- JSON list
    subject            TEXT NOT NULL,
    sent_at            TEXT NOT NULL,
    due_at             TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'open',   -- open | overdue | replied | closed
    replied_message_id INTEGER,
    replied_at         TEXT,
    nudges             INTEGER NOT NULL DEFAULT 0,
    last_nudge_at      TEXT,
    note               TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_expected_status ON expected_replies(status);

CREATE TABLE IF NOT EXISTS briefs (
    id            INTEGER PRIMARY KEY,
    brief_date    TEXT NOT NULL UNIQUE,
    generated_at  TEXT NOT NULL,
    markdown      TEXT NOT NULL,
    script        TEXT NOT NULL,
    sms           TEXT NOT NULL,
    stats         TEXT NOT NULL DEFAULT '{}',
    audio_path    TEXT NOT NULL DEFAULT '',
    voice_source  TEXT NOT NULL DEFAULT 'none',
    delivery      TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS notifications (
    id                 INTEGER PRIMARY KEY,
    kind               TEXT NOT NULL,           -- sms | call
    purpose            TEXT NOT NULL,           -- alert | brief | test
    to_number          TEXT NOT NULL,
    body               TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL,
    provider_sid       TEXT NOT NULL DEFAULT '',
    transport          TEXT NOT NULL,
    truth_label        TEXT NOT NULL,
    related_message_id INTEGER,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cleanup_log (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL,
    message_id  INTEGER NOT NULL,
    action      TEXT NOT NULL,
    raw_path    TEXT NOT NULL DEFAULT '',
    server_copy TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);
"""

MESSAGE_LIST_COLUMNS = (
    "m.id, m.account_id, a.address AS account, m.folder, m.uid, m.message_id, m.in_reply_to, "
    "m.thread_key, m.subject, m.from_addr, m.from_name, m.to_addrs, m.cc_addrs, m.date, "
    "m.snippet, m.attachments, m.seen, m.flagged, m.size, m.synced_at, m.urgency, m.urgency_score, "
    "m.urgency_reasons, m.needs_reply, m.archived, m.archived_at, m.raw_path"
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fts_query(q: str) -> str:
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
        return self._conn.execute("SELECT * FROM accounts WHERE address=?", (str(ident).lower(),)).fetchone()

    def list_accounts(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM accounts" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
        return self._conn.execute(sql).fetchall()

    def update_account(self, account_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE accounts SET {sets} WHERE id=?", (*fields.values(), account_id))

    def remove_account(self, account_id: int) -> bool:
        return self._conn.execute("DELETE FROM accounts WHERE id=?", (account_id,)).rowcount > 0

    def account_stats(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT a.id, a.address, a.display_name, a.provider, a.enabled, a.last_sync_at, a.last_error,
                      COUNT(m.id) AS messages,
                      COALESCE(SUM(CASE WHEN m.seen=0 AND m.archived=0 THEN 1 ELSE 0 END),0) AS unread
               FROM accounts a LEFT JOIN messages m ON m.account_id=a.id
               GROUP BY a.id ORDER BY a.id"""
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ folder cursors
    def get_folder_state(self, account_id: int, folder: str) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT uidvalidity, last_uid FROM folder_state WHERE account_id=? AND folder=?", (account_id, folder)
        ).fetchone()
        return (row["uidvalidity"], row["last_uid"]) if row else (0, 0)

    def set_folder_state(self, account_id: int, folder: str, uidvalidity: int, last_uid: int) -> None:
        self._conn.execute(
            """INSERT INTO folder_state(account_id, folder, uidvalidity, last_uid) VALUES (?,?,?,?)
               ON CONFLICT(account_id, folder) DO UPDATE SET uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid""",
            (account_id, folder, uidvalidity, last_uid),
        )

    def reset_folder(self, account_id: int, folder: str) -> None:
        self._conn.execute("DELETE FROM messages WHERE account_id=? AND folder=?", (account_id, folder))
        self._conn.execute("DELETE FROM folder_state WHERE account_id=? AND folder=?", (account_id, folder))

    # ------------------------------------------------------------ messages
    def upsert_message(self, account_id: int, folder: str, uid: int, parsed: dict, seen: bool, flagged: bool) -> tuple[int, bool]:
        """Returns (local id, created)."""
        existing = self._conn.execute(
            "SELECT id FROM messages WHERE account_id=? AND folder=? AND uid=?", (account_id, folder, uid)
        ).fetchone()
        row = {
            "account_id": account_id, "folder": folder, "uid": uid,
            "message_id": parsed.get("message_id", ""), "in_reply_to": parsed.get("in_reply_to", ""),
            "thread_key": parsed.get("thread_key", ""), "subject": parsed.get("subject", ""),
            "from_addr": parsed.get("from_addr", ""), "from_name": parsed.get("from_name", ""),
            "to_addrs": json.dumps(parsed.get("to_addrs", [])), "cc_addrs": json.dumps(parsed.get("cc_addrs", [])),
            "date": parsed.get("date"), "snippet": parsed.get("snippet", ""),
            "body_text": parsed.get("body_text", ""), "body_html": parsed.get("body_html", ""),
            "attachments": json.dumps(parsed.get("attachments", [])),
            "seen": int(seen), "flagged": int(flagged), "size": int(parsed.get("size", 0)), "synced_at": utcnow(),
        }
        if existing:
            sets = ", ".join(f"{k}=?" for k in row if k not in ("account_id", "folder", "uid"))
            vals = [v for k, v in row.items() if k not in ("account_id", "folder", "uid")]
            self._conn.execute(f"UPDATE messages SET {sets} WHERE id=?", (*vals, existing["id"]))
            return int(existing["id"]), False
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = self._conn.execute(f"INSERT INTO messages ({cols}) VALUES ({marks})", tuple(row.values()))
        return int(cur.lastrowid), True

    def set_urgency(self, message_id: int, level: str, score: int, reasons: list[str], needs_reply: bool) -> None:
        self._conn.execute(
            "UPDATE messages SET urgency=?, urgency_score=?, urgency_reasons=?, needs_reply=? WHERE id=?",
            (level, int(score), json.dumps(reasons), int(needs_reply), message_id),
        )

    def set_raw_path(self, message_id: int, raw_path: str) -> None:
        self._conn.execute("UPDATE messages SET raw_path=? WHERE id=?", (raw_path, message_id))

    def set_flags(self, message_id: int, seen: bool | None = None, flagged: bool | None = None) -> None:
        if seen is not None:
            self._conn.execute("UPDATE messages SET seen=? WHERE id=?", (int(seen), message_id))
        if flagged is not None:
            self._conn.execute("UPDATE messages SET flagged=? WHERE id=?", (int(flagged), message_id))

    def set_archived(self, message_id: int, archived: bool) -> None:
        self._conn.execute("UPDATE messages SET archived=?, archived_at=? WHERE id=?",
                           (int(archived), utcnow() if archived else None, message_id))

    def get_message(self, message_id: int) -> dict | None:
        row = self._conn.execute(
            f"SELECT {MESSAGE_LIST_COLUMNS}, m.body_text, m.body_html FROM messages m JOIN accounts a ON a.id=m.account_id WHERE m.id=?",
            (message_id,),
        ).fetchone()
        return self._hydrate(row) if row else None

    def list_messages(
        self, *, account_id: int | None = None, folder: str | None = None, unread_only: bool = False,
        flagged_only: bool = False, tag: str | None = None, query: str | None = None,
        min_urgency: str | None = None, needs_reply: bool | None = None, since: str | None = None,
        include_archived: bool = False, archived_only: bool = False, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        from .urgency import RANK
        where: list[str] = []
        params: list[Any] = []
        joins = "JOIN accounts a ON a.id=m.account_id"
        if query:
            joins += " JOIN messages_fts f ON f.rowid=m.id"
            where.append("messages_fts MATCH ?"); params.append(_fts_query(query))
        if account_id is not None:
            where.append("m.account_id=?"); params.append(account_id)
        if folder:
            where.append("m.folder=?"); params.append(folder)
        if unread_only:
            where.append("m.seen=0")
        if flagged_only:
            where.append("m.flagged=1")
        if tag:
            where.append("EXISTS (SELECT 1 FROM message_tags t WHERE t.message_id=m.id AND t.tag=?)"); params.append(tag)
        if min_urgency:
            allowed = [lvl for lvl, r in RANK.items() if r >= RANK[min_urgency]]
            where.append("m.urgency IN (" + ",".join("?" for _ in allowed) + ")"); params.extend(allowed)
        if needs_reply is not None:
            where.append("m.needs_reply=?"); params.append(int(needs_reply))
        if since:
            where.append("m.date >= ?"); params.append(since)
        if archived_only:
            where.append("m.archived=1")
        elif not include_archived:
            where.append("m.archived=0")
        sql = f"SELECT {MESSAGE_LIST_COLUMNS} FROM messages m {joins}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.urgency_score DESC, m.date DESC, m.id DESC LIMIT ? OFFSET ?" if min_urgency else \
               " ORDER BY m.date DESC, m.id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [self._hydrate(r) for r in self._conn.execute(sql, params).fetchall()]

    def thread(self, thread_key: str) -> list[dict]:
        rows = self._conn.execute(
            f"SELECT {MESSAGE_LIST_COLUMNS}, m.body_text, m.body_html FROM messages m JOIN accounts a ON a.id=m.account_id "
            "WHERE m.thread_key=? ORDER BY m.date ASC, m.id ASC", (thread_key,),
        ).fetchall()
        return [self._hydrate(r) for r in rows]

    def urgency_counts(self, *, unread_only: bool = False) -> dict[str, int]:
        sql = "SELECT urgency, COUNT(*) AS n FROM messages WHERE archived=0" + (" AND seen=0" if unread_only else "") + " GROUP BY urgency"
        counts = {"critical": 0, "high": 0, "normal": 0, "low": 0}
        for r in self._conn.execute(sql).fetchall():
            counts[r["urgency"]] = r["n"]
        return counts

    def _hydrate(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in ("to_addrs", "cc_addrs", "attachments", "urgency_reasons"):
            if k in d and isinstance(d[k], str):
                d[k] = json.loads(d[k] or "[]")
        for k in ("seen", "flagged", "needs_reply", "archived"):
            if k in d:
                d[k] = bool(d[k])
        d["tags"] = self.tags_for(d["id"])
        return d

    # ------------------------------------------------------------ tags
    def add_tags(self, message_id: int, tags: list[str]) -> None:
        self._conn.executemany("INSERT OR IGNORE INTO message_tags(message_id, tag) VALUES (?,?)",
                               [(message_id, t.strip().lower()) for t in tags if t.strip()])

    def remove_tag(self, message_id: int, tag: str) -> None:
        self._conn.execute("DELETE FROM message_tags WHERE message_id=? AND tag=?", (message_id, tag.lower()))

    def tags_for(self, message_id: int) -> list[str]:
        return [r["tag"] for r in self._conn.execute(
            "SELECT tag FROM message_tags WHERE message_id=? ORDER BY tag", (message_id,)).fetchall()]

    def tag_counts(self) -> dict[str, int]:
        return {r["tag"]: r["n"] for r in self._conn.execute(
            "SELECT t.tag, COUNT(*) AS n FROM message_tags t JOIN messages m ON m.id=t.message_id "
            "WHERE m.archived=0 GROUP BY t.tag ORDER BY n DESC").fetchall()}

    # ------------------------------------------------------------ sent log
    def log_sent(self, account_id: int, message_id: str, to_addrs: list[str], subject: str, in_reply_to: str = "") -> None:
        self._conn.execute(
            "INSERT INTO sent_log(account_id, message_id, to_addrs, subject, in_reply_to, sent_at) VALUES (?,?,?,?,?,?)",
            (account_id, message_id, json.dumps(to_addrs), subject, in_reply_to, utcnow()),
        )

    # ------------------------------------------------------------ expected replies
    def add_expected_reply(self, **fields: Any) -> int:
        fields["to_addrs"] = json.dumps(fields.get("to_addrs", []))
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self._conn.execute(f"INSERT INTO expected_replies ({cols}) VALUES ({marks})", tuple(fields.values()))
        return int(cur.lastrowid)

    def get_expected_reply(self, rid: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM expected_replies WHERE id=?", (rid,)).fetchone()
        return self._hydrate_reply(row) if row else None

    def list_expected_replies(self, statuses: tuple[str, ...] | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM expected_replies"
        params: list[Any] = []
        if statuses:
            sql += " WHERE status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        sql += " ORDER BY due_at ASC, id ASC LIMIT ?"
        params.append(limit)
        return [self._hydrate_reply(r) for r in self._conn.execute(sql, params).fetchall()]

    def update_expected_reply(self, rid: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE expected_replies SET {sets} WHERE id=?", (*fields.values(), rid))

    def _hydrate_reply(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["to_addrs"] = json.loads(d.get("to_addrs") or "[]")
        return d

    # ------------------------------------------------------------ briefs
    def save_brief(self, brief_date: str, *, markdown: str, script: str, sms: str, stats: dict,
                   audio_path: str = "", voice_source: str = "none") -> int:
        self._conn.execute(
            """INSERT INTO briefs(brief_date, generated_at, markdown, script, sms, stats, audio_path, voice_source)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(brief_date) DO UPDATE SET generated_at=excluded.generated_at, markdown=excluded.markdown,
                 script=excluded.script, sms=excluded.sms, stats=excluded.stats,
                 audio_path=excluded.audio_path, voice_source=excluded.voice_source""",
            (brief_date, utcnow(), markdown, script, sms, json.dumps(stats), audio_path, voice_source),
        )
        return int(self._conn.execute("SELECT id FROM briefs WHERE brief_date=?", (brief_date,)).fetchone()["id"])

    def get_brief(self, brief_date: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM briefs WHERE brief_date=?", (brief_date,)).fetchone()
        return self._hydrate_brief(row) if row else None

    def latest_brief(self) -> dict | None:
        row = self._conn.execute("SELECT * FROM briefs ORDER BY brief_date DESC LIMIT 1").fetchone()
        return self._hydrate_brief(row) if row else None

    def list_briefs(self, limit: int = 30) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM briefs ORDER BY brief_date DESC LIMIT ?", (limit,)).fetchall()
        return [self._hydrate_brief(r) for r in rows]

    def append_brief_delivery(self, brief_date: str, record: dict) -> None:
        row = self._conn.execute("SELECT delivery FROM briefs WHERE brief_date=?", (brief_date,)).fetchone()
        if not row:
            return
        delivery = json.loads(row["delivery"] or "[]")
        delivery.append(record)
        self._conn.execute("UPDATE briefs SET delivery=? WHERE brief_date=?", (json.dumps(delivery), brief_date))

    def _hydrate_brief(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["stats"] = json.loads(d.get("stats") or "{}")
        d["delivery"] = json.loads(d.get("delivery") or "[]")
        return d

    # ------------------------------------------------------------ notifications
    def log_notification(self, **fields: Any) -> int:
        fields.setdefault("created_at", utcnow())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self._conn.execute(f"INSERT INTO notifications ({cols}) VALUES ({marks})", tuple(fields.values()))
        return int(cur.lastrowid)

    def list_notifications(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def notification_exists(self, purpose: str, related_message_id: int) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM notifications WHERE purpose=? AND related_message_id=? LIMIT 1", (purpose, related_message_id)
        ).fetchone() is not None

    # ------------------------------------------------------------ cleanup
    def log_cleanup(self, run_id: str, message_id: int, action: str, raw_path: str = "", server_copy: str = "") -> None:
        self._conn.execute(
            "INSERT INTO cleanup_log(run_id, message_id, action, raw_path, server_copy, at) VALUES (?,?,?,?,?,?)",
            (run_id, message_id, action, raw_path, server_copy, utcnow()),
        )

    def cleanup_runs(self, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT run_id, MIN(at) AS at, COUNT(*) AS archived FROM cleanup_log GROUP BY run_id ORDER BY at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def overview(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) AS n FROM messages WHERE archived=0").fetchone()["n"]
        unread = self._conn.execute("SELECT COUNT(*) AS n FROM messages WHERE seen=0 AND archived=0").fetchone()["n"]
        archived = self._conn.execute("SELECT COUNT(*) AS n FROM messages WHERE archived=1").fetchone()["n"]
        replies = {r["status"]: r["n"] for r in self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM expected_replies GROUP BY status").fetchall()}
        return {"accounts": self.account_stats(), "messages": total, "unread": unread, "archived": archived,
                "tags": self.tag_counts(), "urgency": self.urgency_counts(), "urgency_unread": self.urgency_counts(unread_only=True),
                "replies": replies}
