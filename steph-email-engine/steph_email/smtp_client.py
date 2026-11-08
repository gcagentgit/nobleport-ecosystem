"""Outbound mail through the account's own SMTP server (STARTTLS or implicit TLS)."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path


def build_message(
    *,
    from_addr: str,
    from_name: str,
    to: list[str],
    subject: str,
    text: str,
    html: str | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str = "",
    references: str = "",
    attachments: list[Path] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.rsplit("@", 1)[-1] or None)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
    msg.set_content(text or "")
    if html:
        msg.add_alternative(html, subtype="html")
    for path in attachments or []:
        data = Path(path).read_bytes()
        maintype, subtype = _guess_type(Path(path).name)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=Path(path).name)
    return msg


def _guess_type(filename: str) -> tuple[str, str]:
    import mimetypes
    ctype, _ = mimetypes.guess_type(filename)
    if not ctype:
        return "application", "octet-stream"
    main, sub = ctype.split("/", 1)
    return main, sub


class SmtpSender:
    def __init__(self, host: str, port: int, use_ssl: bool, timeout: float = 30.0):
        self.host, self.port, self.use_ssl, self.timeout = host, port, use_ssl, timeout

    def _connect(self) -> smtplib.SMTP:
        ctx = ssl.create_default_context()
        if self.use_ssl:
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ctx)
        s = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        return s

    def send(self, msg: EmailMessage, *, user: str, password: str | None = None, access_token: str | None = None) -> str:
        s = self._connect()
        try:
            if access_token:
                from .oauth import xoauth2_b64
                code, resp = s.docmd("AUTH", "XOAUTH2 " + xoauth2_b64(user, access_token))
                if code != 235:
                    raise smtplib.SMTPAuthenticationError(code, resp)
            else:
                s.login(user, password or "")
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:
                pass
        return msg["Message-ID"]
