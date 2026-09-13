"""``steph-email`` command line.

    steph-email status
    steph-email accounts add you@gmail.com | list | test ADDR | remove ADDR | providers
    steph-email sync [--account X] [--loop]
    steph-email inbox [--urgency high] [--unread] [--tag permit] [--needs-reply] [-n 30] [--json]
    steph-email urgent                      # unread high + critical
    steph-email search "change order"
    steph-email show ID
    steph-email send --from A --to B --subject S --body T [--due-days 3] [--no-track]
    steph-email replies [--overdue] | replies close ID | replies nudge ID
    steph-email brief [--date YYYY-MM-DD] [--deliver] [--voice] [--print]
    steph-email notify test-sms [--body ...]
    steph-email voice verify [--confirm-playback --device "iPhone" --note "..."]
    steph-email cleanup [--run] [--copy-to-server]
    steph-email serve
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from datetime import date
from pathlib import Path

from .config import Settings
from .providers import PROVIDERS, detect_provider
from .service import EmailEngine


def _engine(settings: Settings | None = None) -> EmailEngine:
    return EmailEngine(settings or Settings())


def _table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.upper().ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def _rows(msgs: list[dict]) -> list[dict]:
    return [{"id": m["id"], "urg": m["urgency"][:4].upper(), "score": m["urgency_score"],
             "flag": ("*" if not m["seen"] else " ") + ("!" if m["flagged"] else " ") + ("?" if m["needs_reply"] else " "),
             "date": (m["date"] or "")[:16].replace("T", " "), "from": (m["from_name"] or m["from_addr"])[:22],
             "subject": m["subject"][:56], "why": (m["urgency_reasons"][0] if m["urgency_reasons"] else "")[:34]} for m in msgs]


def cmd_status(args) -> int:
    st = _engine().status()
    if args.json:
        print(json.dumps(st, indent=2)); return 0
    print(f"Steph Email Engine v{st['version']}  ({st['now_local']})")
    mb, nt, vo, br = st["mailboxes"], st["notifications"], st["voice"], st["brief"]
    print(f"  mailboxes     : {mb['healthy']}/{mb['enabled']} healthy" + (f"  errors: {mb['errors']}" if mb["errors"] else ""))
    print(f"  notifications : {nt['transport']} [{nt['truth_label']}]  alerts>={nt['alert_min_urgency']}")
    print(f"  voice         : {vo['source']} configured={vo['configured']} verified={vo['verified']}")
    print(f"  brief         : {br['scheduled_local']}  latest={br['latest_date']}  sms={br['sms']} call={br['call']}")
    print(f"  cleanup       : after {st['cleanup']['after_days']}d, auto={st['cleanup']['auto']}, deletes: {st['cleanup']['deletes']}")
    print("LIVE" if st["live"] else "ACTIVATION PENDING:")
    for p in st["activation_pending"]:
        print(f"  - {p}")
    return 0


def cmd_accounts(args) -> int:
    eng = _engine()
    if args.action == "list":
        _table(eng.db.account_stats(), ["id", "address", "provider", "enabled", "messages", "unread", "last_sync_at", "last_error"])
        return 0
    if args.action == "add":
        prov = PROVIDERS[args.provider] if args.provider else detect_provider(args.address)
        print(f"Provider: {prov.name}")
        if prov.notes:
            print(f"  {prov.notes}")
        secret = args.secret or getpass.getpass("App password / refresh token: ")
        try:
            acct = eng.add_account(args.address, secret, provider=args.provider, display_name=args.name or "",
                                   username=args.username, auth_method=args.auth, imap_host=args.imap_host,
                                   imap_port=args.imap_port, smtp_host=args.smtp_host, smtp_port=args.smtp_port,
                                   smtp_ssl=args.smtp_ssl, oauth_client_id=args.oauth_client_id or "",
                                   oauth_client_secret=args.oauth_client_secret or "", sent_folder=args.sent_folder,
                                   test=not args.no_test)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr); return 1
        print(f"connected {acct['address']} (id {acct['id']}) via {acct['imap_host']}")
        return 0
    if args.action == "test":
        acct = eng.db.get_account(args.address)
        if not acct:
            print("account not found", file=sys.stderr); return 1
        try:
            print(json.dumps(eng.test_account(acct), indent=2)); return 0
        except Exception as exc:
            print(f"connection failed: {exc}", file=sys.stderr); return 1
    if args.action == "remove":
        ok = eng.remove_account(args.address)
        print("removed" if ok else "account not found")
        return 0 if ok else 1
    if args.action == "providers":
        for k, p in PROVIDERS.items():
            print(f"{k:9} {p.name:32} imap={p.imap_host or '-'} smtp={p.smtp_host or '-'} auth={'/'.join(p.auth_methods)}")
        return 0
    return 2


def cmd_sync(args) -> int:
    settings = Settings()
    eng = _engine(settings)
    while True:
        reports = eng.sync_account(args.account) if args.account else eng.sync_all()
        for r in reports:
            state = f"error: {r.error}" if r.error else f"+{r.fetched}" + (" (reset)" if r.reset else "") + \
                    (f"  urgent: {len(r.urgent_new)}" if r.urgent_new else "")
            print(f"{r.account:36} {r.folder:20} {state}")
        if not args.loop:
            return 1 if any(r.error for r in reports) else 0
        time.sleep(settings.sync_interval_s)


def cmd_inbox(args) -> int:
    eng = _engine()
    account_id = None
    if args.account:
        acct = eng.db.get_account(args.account)
        if not acct:
            print("account not found", file=sys.stderr); return 1
        account_id = acct["id"]
    urgent_mode = getattr(args, "urgent_mode", False)
    msgs = eng.db.list_messages(account_id=account_id, unread_only=args.unread or urgent_mode, flagged_only=args.flagged,
                                tag=args.tag, query=getattr(args, "query", None),
                                min_urgency=("high" if urgent_mode else args.urgency),
                                needs_reply=(True if args.needs_reply else None), limit=args.limit)
    if args.json:
        print(json.dumps(msgs, indent=2))
    else:
        _table(_rows(msgs), ["id", "urg", "score", "flag", "date", "from", "subject", "why"])
    return 0


def cmd_show(args) -> int:
    eng = _engine()
    msg = eng.db.get_message(args.id)
    if not msg:
        print("message not found", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(msg, indent=2)); return 0
    print(f"From:    {msg['from_name']} <{msg['from_addr']}>")
    print(f"To:      {', '.join(a['address'] for a in msg['to_addrs'])}")
    print(f"Date:    {msg['date']}")
    print(f"Account: {msg['account']}  Folder: {msg['folder']}  Tags: {', '.join(msg['tags']) or '-'}")
    print(f"Urgency: {msg['urgency']} ({msg['urgency_score']}) — {'; '.join(msg['urgency_reasons']) or '-'}")
    print(f"Subject: {msg['subject']}")
    if msg["raw_path"]:
        print(f"Original: {msg['raw_path']}")
    print("-" * 72)
    print(msg["body_text"])
    if args.mark_read and not msg["seen"]:
        eng.mark(msg["id"], seen=True)
    return 0


def cmd_send(args) -> int:
    eng = _engine()
    body = args.body if args.body is not None else sys.stdin.read()
    try:
        result = eng.send(args.sender, args.to, args.subject or "", body, cc=args.cc or None,
                          reply_to_message_id=args.reply_to, attachments=[Path(p) for p in args.attach] if args.attach else None,
                          expect_reply=not args.no_track, due_days=args.due_days)
    except Exception as exc:
        print(f"send failed: {exc}", file=sys.stderr); return 1
    print(f"sent {result['message_id']} from {result['from']} to {', '.join(result['to'])}")
    if result["expected_reply"]:
        print(f"tracking reply #{result['expected_reply']['id']} due {result['expected_reply']['due_at'][:10]}")
    return 0


def cmd_replies(args) -> int:
    eng = _engine()
    if args.action == "close":
        print(json.dumps(eng.replies.close(args.id), indent=2)); return 0
    if args.action == "nudge":
        rec = eng.replies.nudge(args.id)
        print(rec["follow_up"]); return 0
    eng.replies.reconcile()
    waiting = eng.replies.waiting(overdue_only=args.overdue)
    if args.json:
        print(json.dumps({"waiting_on_them": waiting, "needs_my_reply": eng.replies.needs_my_reply()}, indent=2)); return 0
    print("WAITING ON THEM")
    _table([{"id": w["id"], "to": ", ".join(w["to_addrs"])[:30], "subject": w["subject"][:40], "sent": w["sent_at"][:10],
             "due": w["due_at"][:10], "status": w["status"], "overdue_d": w["days_overdue"], "nudges": w["nudges"]} for w in waiting],
           ["id", "to", "subject", "sent", "due", "status", "overdue_d", "nudges"])
    if not args.overdue:
        print("\nNEEDS YOUR REPLY")
        _table(_rows(eng.replies.needs_my_reply()), ["id", "urg", "date", "from", "subject", "why"])
    return 0


def cmd_brief(args) -> int:
    eng = _engine()
    for_date = date.fromisoformat(args.date) if args.date else None
    result = eng.run_brief(for_date, deliver=args.deliver, voice=args.voice)
    if args.json:
        print(json.dumps(result, indent=2, default=str)); return 0
    print(result["markdown"])
    if args.print_script:
        print("---- spoken script ----\n" + result["script"])
    print(f"---- voice: {result['voice_source']}" + (f" ({result['voice_error']})" if result["voice_error"] else ""))
    for d in result["delivery"]:
        print(f"---- {d['kind']}: {'ok' if d['ok'] else 'FAILED'} {d.get('status') or d.get('error')} [{d.get('truth_label', '')}]")
    return 0


def cmd_notify(args) -> int:
    eng = _engine()
    if args.action == "test-sms":
        try:
            rec = eng.notifier.send_sms(args.body, purpose="test")
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr); return 1
        print(f"{rec['kind']} {rec['status']} to {rec['to_number']} via {rec['transport']} [{rec['truth_label']}]")
        return 0
    return 2


def cmd_voice(args) -> int:
    eng = _engine()
    ev = eng.notifier.verify_voice(sample_text=args.sample, confirm_playback=args.confirm_playback,
                                   device=args.device or "", note=args.note or "")
    for step, ok in ev["steps"].items():
        print(f"  {'✔' if ok else '✘'} {step}")
    if ev.get("error"):
        print(f"  error: {ev['error']}")
    if ev.get("sample"):
        print(f"  sample: {ev['sample']['path']} ({ev['sample']['bytes']} bytes) — play it on the phone, then re-run with --confirm-playback")
    print("VERIFIED" if ev["verified"] else "NOT VERIFIED")
    return 0 if ev["verified"] else 2


def cmd_cleanup(args) -> int:
    eng = _engine()
    if not args.run:
        plan = eng.cleanup(dry_run=True)
        print(f"cleanup plan: {plan['count']} candidates older than {plan['cutoff'][:10]} (dry run; nothing changed)")
        _table([{"id": c["id"], "date": (c["date"] or "")[:10], "from": c["from_addr"][:28], "subject": c["subject"][:50],
                 "original": "on disk" if c["raw_path"] else "will fetch"} for c in plan["candidates"][:50]],
               ["id", "date", "from", "subject", "original"])
        print(f"skipped: {plan['skipped']}")
        print("run with --run to archive (originals are preserved; nothing is ever deleted).")
        return 0
    result = eng.cleanup(dry_run=False, copy_to_server=args.copy_to_server or None)
    print(f"archived {result['archived']} (preserved {result['preserved_now']} originals now, {result['server_copies']} server copies)")
    for e in result["errors"]:
        print(f"  ! {e}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    settings = Settings()
    uvicorn.run("steph_email.api:app", host=args.host or settings.host, port=args.port or settings.port,
                log_level=settings.log_level.lower())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="steph-email", description="Steph Email Engine — Stephanie.ai's inbox layer.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="activation checklist and live state"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    a = sub.add_parser("accounts", help="connect / list / test / remove mailboxes")
    asub = a.add_subparsers(dest="action", required=True)
    add = asub.add_parser("add"); add.add_argument("address")
    add.add_argument("--provider", choices=list(PROVIDERS)); add.add_argument("--name"); add.add_argument("--username")
    add.add_argument("--secret"); add.add_argument("--auth", choices=["password", "oauth2"], default="password")
    add.add_argument("--imap-host"); add.add_argument("--imap-port", type=int)
    add.add_argument("--smtp-host"); add.add_argument("--smtp-port", type=int)
    add.add_argument("--smtp-ssl", action=argparse.BooleanOptionalAction, default=None)
    add.add_argument("--oauth-client-id"); add.add_argument("--oauth-client-secret"); add.add_argument("--sent-folder")
    add.add_argument("--no-test", action="store_true")
    for name in ("test", "remove"):
        x = asub.add_parser(name); x.add_argument("address")
    asub.add_parser("list"); asub.add_parser("providers")
    a.set_defaults(fn=cmd_accounts)

    s = sub.add_parser("sync"); s.add_argument("--account"); s.add_argument("--loop", action="store_true"); s.set_defaults(fn=cmd_sync)

    for name in ("inbox", "search", "urgent"):
        i = sub.add_parser(name)
        if name == "search":
            i.add_argument("query")
        i.add_argument("--account"); i.add_argument("--tag"); i.add_argument("--urgency", choices=["low", "normal", "high", "critical"])
        i.add_argument("--unread", action="store_true"); i.add_argument("--flagged", action="store_true")
        i.add_argument("--needs-reply", action="store_true")
        i.add_argument("-n", "--limit", type=int, default=30); i.add_argument("--json", action="store_true")
        i.set_defaults(fn=cmd_inbox, urgent_mode=(name == "urgent"))

    sh = sub.add_parser("show"); sh.add_argument("id", type=int); sh.add_argument("--json", action="store_true")
    sh.add_argument("--mark-read", action="store_true"); sh.set_defaults(fn=cmd_show)

    sd = sub.add_parser("send"); sd.add_argument("--from", dest="sender", required=True)
    sd.add_argument("--to", nargs="+", required=True); sd.add_argument("--cc", nargs="*"); sd.add_argument("--subject")
    sd.add_argument("--body"); sd.add_argument("--reply-to", type=int); sd.add_argument("--attach", nargs="*")
    sd.add_argument("--due-days", type=int); sd.add_argument("--no-track", action="store_true"); sd.set_defaults(fn=cmd_send)

    r = sub.add_parser("replies", help="expected-reply tracking")
    r.add_argument("action", nargs="?", choices=["list", "close", "nudge"], default="list"); r.add_argument("id", nargs="?", type=int)
    r.add_argument("--overdue", action="store_true"); r.add_argument("--json", action="store_true"); r.set_defaults(fn=cmd_replies)

    b = sub.add_parser("brief", help="build (and optionally deliver) the morning email brief")
    b.add_argument("--date"); b.add_argument("--deliver", action="store_true"); b.add_argument("--voice", action="store_true", default=None)
    b.add_argument("--print", dest="print_script", action="store_true"); b.add_argument("--json", action="store_true"); b.set_defaults(fn=cmd_brief)

    n = sub.add_parser("notify"); nsub = n.add_subparsers(dest="action", required=True)
    t = nsub.add_parser("test-sms"); t.add_argument("--body", default="Steph Email Engine test message."); n.set_defaults(fn=cmd_notify)

    v = sub.add_parser("voice"); vsub = v.add_subparsers(dest="action", required=True)
    vv = vsub.add_parser("verify"); vv.add_argument("--confirm-playback", action="store_true"); vv.add_argument("--device"); vv.add_argument("--note")
    vv.add_argument("--sample", default="Good morning, Michael. This is Stephanie. Your email brief is ready."); v.set_defaults(fn=cmd_voice)

    c = sub.add_parser("cleanup", help="archive old low-priority mail (originals preserved, never deleted)")
    c.add_argument("--run", action="store_true"); c.add_argument("--copy-to-server", action="store_true"); c.set_defaults(fn=cmd_cleanup)

    sv = sub.add_parser("serve"); sv.add_argument("--host"); sv.add_argument("--port", type=int); sv.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
