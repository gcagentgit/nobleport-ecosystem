"""Expected-reply tracking.

Two directions:

* **Waiting on them** — every message the owner sends (through the engine)
  opens an expectation with a due date. Each sync reconciles the thread: a
  later message from anyone else closes it as *replied*; past the due date it
  becomes *overdue* and the brief nags about it with a ready-to-send follow-up.
* **Needs your reply** — inbound messages the urgency scorer marked as asking
  for a response, where nobody from the owner's side has answered in the
  thread since.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .db import Database, utcnow


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ReplyTracker:
    def __init__(self, db: Database, owner_addresses: list[str] | set[str] = (), default_due_days: int = 3):
        self.db = db
        self.owner_addresses = {a.lower() for a in owner_addresses}
        self.default_due_days = default_due_days

    # ------------------------------------------------------------ outbound
    def track(self, *, account_id: int, account_address: str, message_id: str, thread_key: str, to_addrs: list[str],
              subject: str, sent_at: str | None = None, due_days: int | None = None, note: str = "") -> dict:
        sent = _parse(sent_at) or datetime.now(timezone.utc)
        due = sent + timedelta(days=due_days if due_days is not None else self.default_due_days)
        self.owner_addresses.add(account_address.lower())
        rid = self.db.add_expected_reply(
            account_id=account_id, message_id=message_id, thread_key=thread_key or message_id, to_addrs=to_addrs,
            subject=subject, sent_at=sent.isoformat(timespec="seconds"), due_at=due.isoformat(timespec="seconds"),
            status="open", note=note,
        )
        return self.db.get_expected_reply(rid) or {}

    def reconcile(self, now: datetime | None = None) -> dict[str, list[int]]:
        """Match open expectations against synced mail; flip overdue ones."""
        now = now or datetime.now(timezone.utc)
        replied: list[int] = []
        overdue: list[int] = []
        for rec in self.db.list_expected_replies(statuses=("open", "overdue")):
            acct = self.db.get_account(rec["account_id"])
            ours = set(self.owner_addresses)
            if acct:
                ours.add(acct["address"].lower())
            sent_at = _parse(rec["sent_at"])
            answer = None
            for msg in self.db.thread(rec["thread_key"]):
                if msg["from_addr"].lower() in ours:
                    continue
                if msg["message_id"] == rec["message_id"]:
                    continue
                when = _parse(msg["date"])
                if when and sent_at and when < sent_at:
                    continue
                if msg["in_reply_to"] == rec["message_id"] or when is None or sent_at is None or when >= sent_at:
                    answer = msg
                    break
            if answer:
                self.db.update_expected_reply(rec["id"], status="replied", replied_message_id=answer["id"],
                                              replied_at=answer["date"] or utcnow())
                replied.append(rec["id"])
            elif rec["status"] == "open" and _parse(rec["due_at"]) and now > _parse(rec["due_at"]):
                self.db.update_expected_reply(rec["id"], status="overdue")
                overdue.append(rec["id"])
        return {"replied": replied, "overdue": overdue}

    def waiting(self, *, overdue_only: bool = False, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        statuses = ("overdue",) if overdue_only else ("open", "overdue")
        out = []
        for rec in self.db.list_expected_replies(statuses=statuses):
            due = _parse(rec["due_at"])
            rec["days_overdue"] = max(0, (now - due).days) if due and now > due else 0
            rec["due_in_days"] = (due - now).days if due and now <= due else 0
            out.append(rec)
        out.sort(key=lambda r: (-r["days_overdue"], r["due_at"]))
        return out

    def close(self, rid: int, status: str = "closed") -> dict:
        if status not in ("closed", "replied"):
            raise ValueError("status must be 'closed' or 'replied'")
        if not self.db.get_expected_reply(rid):
            raise ValueError(f"no expected reply {rid}")
        self.db.update_expected_reply(rid, status=status)
        return self.db.get_expected_reply(rid) or {}

    def nudge(self, rid: int) -> dict:
        rec = self.db.get_expected_reply(rid)
        if not rec:
            raise ValueError(f"no expected reply {rid}")
        self.db.update_expected_reply(rid, nudges=rec["nudges"] + 1, last_nudge_at=utcnow())
        rec = self.db.get_expected_reply(rid) or {}
        rec["follow_up"] = self.draft_follow_up(rec)
        return rec

    @staticmethod
    def draft_follow_up(rec: dict) -> str:
        who = ", ".join(a.split("@")[0] for a in rec.get("to_addrs", [])) or "there"
        sent = _parse(rec.get("sent_at"))
        when = sent.strftime("%B %-d") if sent else "earlier"
        return (f"Hi {who},\n\nFollowing up on my note from {when} regarding \"{rec.get('subject', '')}\". "
                "Could you let me know where this stands? A quick reply either way helps me keep the schedule.\n\nThanks,\n")

    # ------------------------------------------------------------ inbound
    def needs_my_reply(self, limit: int = 50) -> list[dict]:
        """Inbound messages that ask for a response and have no later reply from our side."""
        out: list[dict] = []
        for msg in self.db.list_messages(needs_reply=True, limit=limit * 3):
            if msg["from_addr"].lower() in self.owner_addresses:
                continue
            answered = False
            when = _parse(msg["date"])
            for other in self.db.thread(msg["thread_key"]):
                if other["id"] == msg["id"] or other["from_addr"].lower() not in self.owner_addresses:
                    continue
                other_when = _parse(other["date"])
                if when is None or other_when is None or other_when >= when:
                    answered = True
                    break
            if not answered and not self._replied_via_sent_log(msg):
                out.append(msg)
            if len(out) >= limit:
                break
        return out

    def _replied_via_sent_log(self, msg: dict) -> bool:
        row = self.db._conn.execute(
            "SELECT 1 FROM sent_log WHERE in_reply_to=? AND in_reply_to<>'' LIMIT 1", (msg["message_id"],)
        ).fetchone()
        return row is not None

    def summary(self, now: datetime | None = None) -> dict:
        waiting = self.waiting(now=now)
        return {
            "waiting_on_them": len(waiting),
            "overdue": sum(1 for w in waiting if w["days_overdue"] > 0),
            "due_today": sum(1 for w in waiting if w["days_overdue"] == 0 and w["due_in_days"] == 0),
            "needs_my_reply": len(self.needs_my_reply()),
        }
