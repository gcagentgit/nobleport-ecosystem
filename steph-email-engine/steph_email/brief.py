"""The morning email brief.

Builds one structured brief from the last N hours of mail: what needs the
owner first, who still owes a reply, money / permits / deadlines, and the
housekeeping line. Renders it three ways — markdown for the dashboard, a
spoken script for the voice option, and a ≤320-character SMS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import Database
from .replies import ReplyTracker

# Spelled the way Stephanie's voice should say them (from the voice standard).
PRONUNCIATIONS: dict[str, str] = {
    "NoblePort": "Noble Port", "HVAC": "H-vack", "AHJ": "A-H-J", "AWO": "A-W-O", "RFI": "R-F-I", "RFP": "R-F-P",
    "COI": "C-O-I", "sq ft": "square feet", "Ipswich": "Ips-witch", "Newburyport": "Newbury-port",
}
_DOLLARS = re.compile(r"\$\s?(\d[\d,]*)(?:\.(\d{2}))?")
SIGN_OFF = "That is the brief. Nothing has been sent, archived or deleted on your behalf; originals are preserved."


@dataclass
class Brief:
    brief_date: str
    generated_at: str
    stats: dict
    sections: list[dict] = field(default_factory=list)   # {title, items:[{text, spoken, message_id?}]}
    markdown: str = ""
    script: str = ""
    sms: str = ""

    def as_dict(self) -> dict:
        return {"brief_date": self.brief_date, "generated_at": self.generated_at, "stats": self.stats,
                "sections": self.sections, "markdown": self.markdown, "script": self.script, "sms": self.sms}


def spoken_money(text: str) -> str:
    def repl(m: re.Match) -> str:
        whole = int(m.group(1).replace(",", ""))
        return f"{whole:,} dollars"
    return _DOLLARS.sub(repl, text)


def spoken(text: str) -> str:
    out = spoken_money(text)
    for term, say in PRONUNCIATIONS.items():
        out = re.sub(r"\b" + re.escape(term) + r"\b", say, out)
    out = out.replace("&", " and ").replace("#", "number ").replace("Re:", "reply on").replace("RE:", "reply on")
    return re.sub(r"\s+", " ", out).strip()


def _who(msg: dict) -> str:
    return msg.get("from_name") or msg.get("from_addr", "").split("@")[0] or "unknown sender"


def _age(msg: dict, now: datetime) -> str:
    if not msg.get("date"):
        return ""
    when = datetime.fromisoformat(msg["date"])
    hours = int((now - when).total_seconds() // 3600)
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


class BriefBuilder:
    def __init__(self, db: Database, replies: ReplyTracker, *, owner_name: str = "Michael",
                 tz: ZoneInfo | None = None, dashboard_url: str = ""):
        self.db = db
        self.replies = replies
        self.owner_name = owner_name
        self.tz = tz or ZoneInfo("America/New_York")
        self.dashboard_url = dashboard_url

    def build(self, for_date: date | None = None, now: datetime | None = None, lookback_hours: int = 24,
              archived_last_run: int | None = None) -> Brief:
        now = now or datetime.now(timezone.utc)
        for_date = for_date or now.astimezone(self.tz).date()
        since = (now - timedelta(hours=lookback_hours)).isoformat(timespec="seconds")

        new = self.db.list_messages(since=since, limit=500)
        unread_counts = self.db.urgency_counts(unread_only=True)
        needs_first = [m for m in self.db.list_messages(min_urgency="high", unread_only=True, limit=200)]
        needs_reply = self.replies.needs_my_reply(limit=10)
        waiting = self.replies.waiting(now=now)
        overdue = [w for w in waiting if w["days_overdue"] > 0]
        due_soon = [w for w in waiting if w["days_overdue"] == 0 and w["due_in_days"] <= 1]
        money = [m for m in new if set(m["tags"]) & {"invoice", "contract", "closing", "permit"} and m["urgency"] != "low"]
        accounts = self.db.account_stats()
        broken = [a for a in accounts if a.get("last_error")]

        stats = {
            "new_messages": len(new), "unread": sum(unread_counts.values()),
            "critical": unread_counts["critical"], "high": unread_counts["high"],
            "needs_my_reply": len(needs_reply), "waiting_on_them": len(waiting), "overdue_replies": len(overdue),
            "money_items": len(money), "accounts": len(accounts), "accounts_with_errors": len(broken),
            "archived_last_run": archived_last_run or 0, "lookback_hours": lookback_hours,
        }

        sections: list[dict] = []

        items = []
        for m in needs_first[:8]:
            why = m["urgency_reasons"][0] if m["urgency_reasons"] else m["urgency"]
            items.append({"text": f"**{m['urgency'].upper()}** — {_who(m)}: {m['subject'] or '(no subject)'} _({why}, {_age(m, now)})_",
                          "spoken": f"{m['urgency']}: {_who(m)} on {spoken(m['subject'] or 'no subject')}. {spoken(why)}.",
                          "message_id": m["id"]})
        sections.append({"title": "Needs you first", "items": items,
                         "empty": "Nothing critical or high is waiting unread."})

        items = []
        for m in needs_reply[:6]:
            items.append({"text": f"{_who(m)}: {m['subject'] or '(no subject)'} _({_age(m, now)})_",
                          "spoken": f"{_who(m)} on {spoken(m['subject'] or 'no subject')}.", "message_id": m["id"]})
        sections.append({"title": "Needs your reply", "items": items, "empty": "No open questions addressed to you."})

        items = []
        for w in overdue[:6]:
            who = ", ".join(w["to_addrs"]) or "recipient"
            items.append({"text": f"{who} — \"{w['subject']}\" is **{w['days_overdue']}d overdue** (sent {w['sent_at'][:10]})",
                          "spoken": f"{who.split('@')[0]} owes you a reply on {spoken(w['subject'])}, {w['days_overdue']} days overdue.",
                          "reply_id": w["id"]})
        for w in due_soon[:4]:
            who = ", ".join(w["to_addrs"]) or "recipient"
            items.append({"text": f"{who} — \"{w['subject']}\" due {'today' if w['due_in_days'] == 0 else 'tomorrow'}",
                          "spoken": f"{who.split('@')[0]} is due to reply on {spoken(w['subject'])} {'today' if w['due_in_days'] == 0 else 'tomorrow'}.",
                          "reply_id": w["id"]})
        sections.append({"title": "Waiting on them", "items": items, "empty": "Nobody is late replying to you."})

        items = []
        for m in money[:6]:
            tag = next(t for t in ("invoice", "closing", "contract", "permit") if t in m["tags"])
            items.append({"text": f"[{tag}] {_who(m)}: {m['subject'] or '(no subject)'}",
                          "spoken": f"{tag}: {_who(m)} on {spoken(m['subject'] or 'no subject')}.", "message_id": m["id"]})
        sections.append({"title": "Money, permits and deadlines", "items": items, "empty": "No new invoices, closings, contracts or permits."})

        items = [{"text": f"{len(new)} new messages in the last {lookback_hours}h across {len(accounts)} mailbox(es); {stats['unread']} unread.",
                  "spoken": f"{len(new)} new messages in the last {lookback_hours} hours; {stats['unread']} unread."}]
        if archived_last_run:
            items.append({"text": f"Cleanup archived {archived_last_run} low-priority messages. Originals are preserved on disk and on the server.",
                          "spoken": f"Cleanup archived {archived_last_run} low priority messages. Originals are preserved."})
        for a in broken:
            items.append({"text": f"⚠ {a['address']} needs attention: {a['last_error'][:80]}",
                          "spoken": f"Mailbox {a['address'].split('@')[0]} needs attention: it failed to sync."})
        sections.append({"title": "Housekeeping", "items": items, "empty": ""})

        brief = Brief(brief_date=for_date.isoformat(), generated_at=now.isoformat(timespec="seconds"), stats=stats, sections=sections)
        brief.markdown = self.render_markdown(brief, for_date)
        brief.script = self.render_script(brief, for_date)
        brief.sms = self.render_sms(brief, for_date, needs_first[:1])
        return brief

    # ------------------------------------------------------------ renderers
    def render_markdown(self, brief: Brief, for_date: date) -> str:
        s = brief.stats
        lines = [f"# Email brief — {for_date.strftime('%A, %B %-d, %Y')}",
                 "",
                 f"**{s['critical']} critical · {s['high']} high · {s['needs_my_reply']} need your reply · "
                 f"{s['overdue_replies']} overdue replies · {s['new_messages']} new**", ""]
        for sec in brief.sections:
            lines.append(f"## {sec['title']}")
            if sec["items"]:
                lines.extend(f"- {it['text']}" for it in sec["items"])
            elif sec.get("empty"):
                lines.append(f"- {sec['empty']}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def render_script(self, brief: Brief, for_date: date) -> str:
        s = brief.stats
        parts = [f"Good morning, {self.owner_name}. This is Stephanie with your email brief for "
                 f"{for_date.strftime('%A, %B')} {for_date.day}."]
        headline = (f"You have {s['critical']} critical and {s['high']} high priority messages unread, "
                    f"{s['needs_my_reply']} that need your reply, and {s['overdue_replies']} people are overdue replying to you.")
        parts.append(headline)
        for sec in brief.sections:
            if not sec["items"]:
                if sec.get("empty"):
                    parts.append(f"{sec['title']}. {sec['empty']}")
                continue
            block = [f"{sec['title']}."]
            for i, it in enumerate(sec["items"], 1):
                lead = "First" if i == 1 else ("Next" if i < len(sec["items"]) else "Last")
                block.append(f"{lead}: {it['spoken']}")
            parts.append(" ".join(block))
        parts.append(SIGN_OFF)
        return "\n\n".join(parts)

    def render_sms(self, brief: Brief, for_date: date, top: list[dict]) -> str:
        s = brief.stats
        text = (f"Steph brief {for_date.month}/{for_date.day}: {s['critical']} critical, {s['high']} high, "
                f"{s['needs_my_reply']} need reply, {s['overdue_replies']} overdue.")
        if top:
            m = top[0]
            text += f" Top: {_who(m)} - {m['subject'][:60]}."
        if self.dashboard_url:
            text += f" {self.dashboard_url}"
        return text[:320]
