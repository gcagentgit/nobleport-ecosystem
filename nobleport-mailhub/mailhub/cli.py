"""``mailhub`` command line.

    mailhub accounts add you@gmail.com            # prompts for the app password
    mailhub accounts add ops@company.com --provider generic --imap-host mail.company.com --smtp-host mail.company.com
    mailhub accounts list | test | remove
    mailhub sync [--account X] [--loop]
    mailhub inbox [--unread] [--tag permit] [--account X] [-n 30]
    mailhub search "change order"
    mailhub show 42
    mailhub send --from you@gmail.com --to sub@vendor.com --subject "..." --body "..."
    mailhub serve
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from pathlib import Path

from .config import Settings
from .providers import PROVIDERS, detect_provider
from .service import MailHub


def _hub(settings: Settings | None = None) -> MailHub:
    return MailHub(settings or Settings())


def _print_table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.upper().ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def cmd_accounts(args: argparse.Namespace) -> int:
    hub = _hub()
    if args.action == "list":
        rows = [dict(a) for a in hub.db.account_stats()]
        _print_table(rows, ["id", "address", "provider", "enabled", "messages", "unread", "last_sync_at", "last_error"])
        return 0
    if args.action == "add":
        prov = PROVIDERS[args.provider] if args.provider else detect_provider(args.address)
        print(f"Provider: {prov.name}")
        if prov.notes:
            print(f"  {prov.notes}")
        secret = args.secret or getpass.getpass("App password / refresh token: ")
        try:
            acct = hub.add_account(
                args.address, secret, provider=args.provider, display_name=args.name or "", username=args.username,
                auth_method=args.auth, imap_host=args.imap_host, imap_port=args.imap_port, smtp_host=args.smtp_host,
                smtp_port=args.smtp_port, smtp_ssl=args.smtp_ssl, oauth_client_id=args.oauth_client_id or "",
                oauth_client_secret=args.oauth_client_secret or "", sent_folder=args.sent_folder, test=not args.no_test,
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"connected {acct['address']} (id {acct['id']}) via {acct['imap_host']}")
        return 0
    if args.action == "test":
        acct = hub.db.get_account(args.address)
        if not acct:
            print("account not found", file=sys.stderr)
            return 1
        try:
            print(json.dumps(hub.test_account(acct), indent=2))
            return 0
        except Exception as exc:
            print(f"connection failed: {exc}", file=sys.stderr)
            return 1
    if args.action == "remove":
        ok = hub.remove_account(args.address)
        print("removed" if ok else "account not found")
        return 0 if ok else 1
    if args.action == "providers":
        for k, p in PROVIDERS.items():
            print(f"{k:9} {p.name:32} imap={p.imap_host or '-'} smtp={p.smtp_host or '-'} auth={'/'.join(p.auth_methods)}")
        return 0
    return 2


def cmd_sync(args: argparse.Namespace) -> int:
    settings = Settings()
    hub = _hub(settings)
    while True:
        reports = hub.sync_account(args.account) if args.account else hub.sync_all()
        for r in reports:
            state = f"error: {r.error}" if r.error else f"+{r.fetched}" + (" (reset)" if r.reset else "")
            print(f"{r.account:36} {r.folder:20} {state}")
        if not args.loop:
            return 1 if any(r.error for r in reports) else 0
        time.sleep(settings.sync_interval_s)


def _fmt_rows(msgs: list[dict]) -> list[dict]:
    return [{
        "id": m["id"], "date": (m["date"] or "")[:16].replace("T", " "), "acct": m["account"].split("@")[0][:14],
        "flag": ("*" if not m["seen"] else " ") + ("!" if m["flagged"] else " "),
        "from": (m["from_name"] or m["from_addr"])[:24], "subject": m["subject"][:60],
        "tags": ",".join(m["tags"]), "att": len(m["attachments"]) or "",
    } for m in msgs]


def cmd_inbox(args: argparse.Namespace) -> int:
    hub = _hub()
    account_id = None
    if args.account:
        acct = hub.db.get_account(args.account)
        if not acct:
            print("account not found", file=sys.stderr)
            return 1
        account_id = acct["id"]
    msgs = hub.db.list_messages(account_id=account_id, unread_only=args.unread, flagged_only=args.flagged,
                                tag=args.tag, query=getattr(args, "query", None), limit=args.limit)
    if args.json:
        print(json.dumps(msgs, indent=2))
    else:
        _print_table(_fmt_rows(msgs), ["id", "flag", "date", "acct", "from", "subject", "tags", "att"])
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    hub = _hub()
    msg = hub.db.get_message(args.id)
    if not msg:
        print("message not found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(msg, indent=2))
        return 0
    print(f"From:    {msg['from_name']} <{msg['from_addr']}>")
    print(f"To:      {', '.join(a['address'] for a in msg['to_addrs'])}")
    print(f"Date:    {msg['date']}")
    print(f"Account: {msg['account']}  Folder: {msg['folder']}  Tags: {', '.join(msg['tags']) or '-'}")
    print(f"Subject: {msg['subject']}")
    if msg["attachments"]:
        print("Attachments: " + ", ".join(f"{a['filename']} ({a['size']}B)" for a in msg["attachments"]))
    print("-" * 72)
    print(msg["body_text"])
    if args.mark_read and not msg["seen"]:
        hub.mark(msg["id"], seen=True)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    hub = _hub()
    body = args.body
    if body is None:
        body = sys.stdin.read()
    try:
        result = hub.send(
            args.sender, args.to, args.subject or "", body, cc=args.cc or None, reply_to_message_id=args.reply_to,
            attachments=[Path(p) for p in args.attach] if args.attach else None,
        )
    except Exception as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return 1
    print(f"sent {result['message_id']} from {result['from']} to {', '.join(result['to'])}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = Settings()
    uvicorn.run("mailhub.api:app", host=args.host or settings.host, port=args.port or settings.port,
                log_level=settings.log_level.lower())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailhub", description="NoblePort MailHub — every mailbox in one place.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("accounts", help="connect / list / test / remove mailboxes")
    asub = a.add_subparsers(dest="action", required=True)
    add = asub.add_parser("add")
    add.add_argument("address")
    add.add_argument("--provider", choices=list(PROVIDERS))
    add.add_argument("--name", help="display name used on outgoing mail")
    add.add_argument("--username", help="login user if different from the address")
    add.add_argument("--secret", help="app password / refresh token (prompted when omitted)")
    add.add_argument("--auth", choices=["password", "oauth2"], default="password")
    add.add_argument("--imap-host"); add.add_argument("--imap-port", type=int)
    add.add_argument("--smtp-host"); add.add_argument("--smtp-port", type=int)
    add.add_argument("--smtp-ssl", action=argparse.BooleanOptionalAction, default=None)
    add.add_argument("--oauth-client-id"); add.add_argument("--oauth-client-secret")
    add.add_argument("--sent-folder")
    add.add_argument("--no-test", action="store_true", help="skip the connection test")
    for name in ("test", "remove"):
        s = asub.add_parser(name); s.add_argument("address", help="address or id")
    asub.add_parser("list")
    asub.add_parser("providers")
    a.set_defaults(fn=cmd_accounts)

    s = sub.add_parser("sync", help="pull new mail from all (or one) accounts")
    s.add_argument("--account"); s.add_argument("--loop", action="store_true", help="keep syncing every MAILHUB_SYNC_INTERVAL_S")
    s.set_defaults(fn=cmd_sync)

    for name, helptext in (("inbox", "unified inbox listing"), ("search", "full-text search")):
        i = sub.add_parser(name, help=helptext)
        if name == "search":
            i.add_argument("query")
        i.add_argument("--account"); i.add_argument("--tag")
        i.add_argument("--unread", action="store_true"); i.add_argument("--flagged", action="store_true")
        i.add_argument("-n", "--limit", type=int, default=30); i.add_argument("--json", action="store_true")
        i.set_defaults(fn=cmd_inbox)

    sh = sub.add_parser("show", help="print one message")
    sh.add_argument("id", type=int); sh.add_argument("--json", action="store_true")
    sh.add_argument("--mark-read", action="store_true")
    sh.set_defaults(fn=cmd_show)

    sd = sub.add_parser("send", help="send (or reply) from any connected account")
    sd.add_argument("--from", dest="sender", required=True, help="account address or id")
    sd.add_argument("--to", nargs="+", required=True); sd.add_argument("--cc", nargs="*")
    sd.add_argument("--subject"); sd.add_argument("--body", help="omit to read the body from stdin")
    sd.add_argument("--reply-to", type=int, help="local message id to reply to (threads correctly)")
    sd.add_argument("--attach", nargs="*")
    sd.set_defaults(fn=cmd_send)

    sv = sub.add_parser("serve", help="run the API + web inbox")
    sv.add_argument("--host"); sv.add_argument("--port", type=int)
    sv.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
