# NoblePort MailHub

**Every mailbox in one place.** Gmail, Outlook / Microsoft 365, Yahoo, iCloud,
Zoho, GoDaddy and any IMAP/SMTP account (company domains, cPanel hosts,
Fastmail, Proton Bridge…) pulled into a single local inbox on Linux.

Pure Python 3.11+, SQLite with full-text search, no external services.
Passwords are encrypted at rest. Ships with a web inbox, a REST API, a CLI
and systemd units.

```
 Gmail ─┐
 M365  ─┤  IMAP (pull)          ┌─────────────┐   HTTP   ┌──────────────┐
 Yahoo ─┼──────────────────────►│  MailHub    │◄────────►│ web inbox    │
 iCloud─┤                       │  SQLite+FTS │          │ CLI / curl   │
 custom─┘◄──────────────────────│  tagger     │          │ your scripts │
           SMTP (send / reply)  └─────────────┘          └──────────────┘
```

## Quick start (any Linux box)

```bash
cd nobleport-mailhub
python3 -m venv .venv && . .venv/bin/activate
pip install .

mailhub accounts add you@gmail.com           # detects Gmail, prompts for the app password
mailhub accounts add you@company.com --provider generic \
    --imap-host mail.company.com --smtp-host mail.company.com --smtp-port 465 --smtp-ssl
mailhub sync                                 # pull the last 500 messages per mailbox
mailhub inbox --unread                       # unified inbox
mailhub search "change order"                # full-text across every account
mailhub serve                                # web inbox + API on http://127.0.0.1:8025
```

### Connecting each provider

| Provider | What you need | Where to get it |
|---|---|---|
| Gmail / Google Workspace | App password | Google Account → Security → 2-Step Verification → App passwords |
| Outlook.com / Microsoft 365 | OAuth2 refresh token + client id (basic auth is retired) | Azure app registration with `IMAP.AccessAsUser.All`, `SMTP.Send`, `offline_access`; use `--auth oauth2 --oauth-client-id …` and paste the refresh token as the secret |
| Yahoo | App password | Account Security → Generate app password |
| iCloud | App-specific password | appleid.apple.com → Sign-In and Security |
| Zoho | Password / app password | Zoho Mail settings (enable IMAP) |
| GoDaddy Workspace | Mailbox password | legacy plans only; M365 plans use the `outlook` preset |
| Anything else | `--provider generic` + IMAP/SMTP hosts | your hosting control panel |

Gmail can also use OAuth2 (`--auth oauth2`). Secrets are stored Fernet-encrypted;
the key lives in `MAILHUB_SECRET_KEY` or `<data_dir>/secret.key` (mode 0600).

## What it does

- **Incremental sync** per folder using IMAP UIDs and UIDVALIDITY (a server
  renumbering triggers a clean refetch). Login failures on one account never
  block the others.
- **One inbox** ordered by date across accounts, with unread/flagged filters,
  thread view (References / In-Reply-To / normalised subject) and FTS5 search
  over subject, sender and body.
- **Auto-tags for construction & real estate**: permit, bid, invoice, contract,
  closing, lease, insurance, schedule, safety, supplier, legal, urgent, plus
  sender-based tags (finance, docusign). Extend with `<data_dir>/rules.json`:
  ```json
  {"oak-street": ["12 oak st", "oak street job"], "permit": ["permit", "co issued"]}
  ```
- **Send & reply from any account** over its own SMTP server with correct
  threading headers; a copy is appended to the Sent folder for non-Gmail
  providers. Attachments supported from the CLI.
- **Flags round-trip**: marking read/flagged in MailHub pushes the IMAP flags
  back to the server.

## API

Run `mailhub serve` and open `http://127.0.0.1:8025/docs` for the interactive
OpenAPI page. Main endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/overview` | per-account counts, unread, tag totals |
| GET/POST/DELETE | `/api/accounts[/{id}]` | connect (tests the login), list, remove |
| POST | `/api/accounts/{id}/test` | re-test a connection |
| POST | `/api/sync?account=` | pull new mail now |
| GET | `/api/messages?q=&account=&tag=&unread=&flagged=&limit=&offset=` | unified inbox / search |
| GET | `/api/messages/{id}` · `/thread` | full message, its thread |
| POST | `/api/messages/{id}/flags` | `{"seen": true, "flagged": false}` |
| POST/DELETE | `/api/messages/{id}/tags[/{tag}]` | manual tags |
| POST | `/api/send` | `{"account":"you@gmail.com","to":["…"],"subject":"…","text":"…","reply_to_message_id":42}` |

Set `MAILHUB_API_TOKEN` to require an `X-API-Token` header on every `/api/*`
route (do this before binding to anything other than `127.0.0.1`).

## Running as a Linux service

```bash
sudo bash deploy/install.sh
```

Creates a `mailhub` system user, a venv in `/opt/nobleport-mailhub`, data in
`/var/lib/nobleport-mailhub`, config in `/etc/nobleport-mailhub/env`, and
enables `mailhub.service` (API + web inbox with an in-process sync loop) and
`mailhub-sync.timer` (a hardened one-shot sync every 5 minutes as a fallback).

Docker is also supported: `cp .env.example .env && docker compose up -d`.

## Configuration

All settings are environment variables prefixed `MAILHUB_` (or a `.env` file);
see `.env.example`. The important ones:

| Variable | Default | Meaning |
|---|---|---|
| `MAILHUB_DATA_DIR` | `~/.local/share/nobleport-mailhub` | SQLite db, secret key, rules.json |
| `MAILHUB_SECRET_KEY` | auto-generated | Fernet key for credentials at rest |
| `MAILHUB_FOLDERS` | `INBOX` | comma-separated IMAP folders to watch |
| `MAILHUB_SYNC_INTERVAL_S` | `120` | background sync cadence in `serve` (0 disables) |
| `MAILHUB_INITIAL_BACKFILL` | `500` | messages fetched on a folder's first sync |
| `MAILHUB_SYNC_BATCH` | `200` | max new messages per folder per pass |
| `MAILHUB_API_TOKEN` | empty | require `X-API-Token` on the API |

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests use an in-memory IMAP/SMTP stand-in (`tests/conftest.py`), so the
suite runs offline in under a second. Layout:

```
mailhub/
  providers.py   presets + domain auto-detect
  crypto.py      Fernet secret store
  db.py          SQLite schema, FTS5, queries
  parser.py      RFC 822 → dict (headers, bodies, attachments, thread key)
  imap_client.py imaplib wrapper (UID sync, flags, XOAUTH2)
  smtp_client.py message builder + STARTTLS/SSL sender
  oauth.py       refresh-token exchange for Gmail / Microsoft
  rules.py       keyword tagger
  service.py     MailHub orchestration (accounts, sync, send)
  api.py         FastAPI app
  cli.py         `mailhub` command
  web/index.html unified inbox UI
```
