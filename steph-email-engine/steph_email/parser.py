"""RFC 822 -> plain dict.  Handles encoded headers, multipart, attachments."""

from __future__ import annotations

import email
import hashlib
import re
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import getaddresses, parsedate_to_datetime

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_RE_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|sv|tr)\s*:\s*)+", re.IGNORECASE)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:  # malformed header — keep whatever we can read
        return _WS.sub(" ", str(value)).strip()


def _addresses(msg: Message, header: str) -> list[dict]:
    raw = msg.get_all(header, [])
    out = []
    for name, addr in getaddresses([_decode(v) for v in raw]):
        if addr:
            out.append({"name": name.strip(), "address": addr.strip().lower()})
    return out


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = _TAG.sub(" ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def normalize_subject(subject: str) -> str:
    return _RE_PREFIX.sub("", subject).strip().lower()


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def parse_message(raw: bytes) -> dict:
    msg: EmailMessage = email.message_from_bytes(raw, policy=policy.default)  # type: ignore[assignment]

    body_text, body_html = "", ""
    attachments: list[dict] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if filename or disp.startswith("attachment"):
            payload = part.get_payload(decode=True) or b""
            attachments.append({
                "filename": _decode(filename) or "attachment",
                "content_type": ctype,
                "size": len(payload),
            })
            continue
        if ctype == "text/plain" and not body_text.strip():
            body_text = _part_text(part)
        elif ctype == "text/html" and not body_html:
            body_html = _part_text(part)

    if not body_text.strip() and body_html:
        body_text = html_to_text(body_html)

    date_iso = None
    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
        if dt is not None:
            if dt.tzinfo is None:
                from datetime import timezone
                dt = dt.replace(tzinfo=timezone.utc)
            date_iso = dt.astimezone(__import__("datetime").timezone.utc).isoformat(timespec="seconds")
    except Exception:
        date_iso = None

    subject = _decode(msg.get("Subject"))
    message_id = (msg.get("Message-ID") or "").strip()
    in_reply_to = (msg.get("In-Reply-To") or "").strip()
    references = (msg.get("References") or "").split()

    # Thread key: root of the References chain, else In-Reply-To, else own id,
    # else a hash of the normalised subject so orphan replies still group.
    if references:
        thread_key = references[0]
    elif in_reply_to:
        thread_key = in_reply_to
    elif message_id:
        thread_key = message_id
    else:
        thread_key = "subj:" + hashlib.sha1(normalize_subject(subject).encode()).hexdigest()[:16]

    sender = _addresses(msg, "From")
    from_addr = sender[0]["address"] if sender else ""
    from_name = sender[0]["name"] if sender else ""

    snippet = _WS.sub(" ", body_text).strip()[:200]

    return {
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "thread_key": thread_key,
        "subject": subject,
        "from_addr": from_addr,
        "from_name": from_name,
        "to_addrs": _addresses(msg, "To"),
        "cc_addrs": _addresses(msg, "Cc"),
        "date": date_iso,
        "snippet": snippet,
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
        "size": len(raw),
    }
