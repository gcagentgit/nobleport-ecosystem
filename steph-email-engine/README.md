# Steph Email Engine v0.1.0

**Stephanie.ai's inbox layer for Michael.** Every mailbox in one place, scored
for urgency, with expected-reply tracking, a morning email brief delivered by
SMS and (optionally) a phone call in Stephanie's ElevenLabs voice, and a
cleanup pass that never deletes an original.

```
 Gmail ─┐  IMAP (pull)   ┌────────────────────────┐  SMS / call   ┌──────────────┐
 M365  ─┤ ─────────────► │  Steph Email Engine    │ ────────────► │ Michael's    │
 Yahoo ─┼                │  SQLite + FTS          │   (Twilio,    │ phone        │
 iCloud─┤  SMTP (send)   │  urgency · replies     │   ElevenLabs  └──────────────┘
 custom─┘ ◄───────────── │  brief · cleanup       │   voice)      ┌──────────────┐
                         └────────────────────────┘ ◄───────────► │ dashboard    │
                                                         HTTP     │ CLI · API    │
                                                                  └──────────────┘
```

> **Two builds live side by side.** `../steph-email/` is the delivered
> Steph_Email_Engine_v0.1.0 pilot (Flask, Telnyx voice, 71 tests). This
> directory is the extended build (FastAPI, Twilio calls, direct ElevenLabs
> synthesis, 97 tests). Both install a package named `steph_email`, so give
> each its own virtualenv.

Pure Python 3.11+, SQLite, no external services required to run. 97 automated
tests run offline in about three seconds (`pytest`).

## What it does

| Piece | What you get |
|---|---|
| **Mailbox adapters** | Gmail / Google Workspace, Outlook / Microsoft 365, Yahoo, iCloud, Zoho, GoDaddy and any IMAP/SMTP host. App passwords or OAuth2 refresh tokens, encrypted at rest. Incremental UID sync, flags round-trip, send and reply with correct threading. |
| **Urgency filtering** | Every message gets a 0–100 score, a level (`critical` / `high` / `normal` / `low`) and the reasons: safety or legal stops, urgent language, deadlines, dollar amounts, permits, VIP senders, whether it asks you for something. Newsletters and no-reply senders are pushed down. `critical` mail (configurable) triggers an immediate SMS. |
| **Expected-reply tracking** | Everything you send through the engine opens an expectation with a due date. Each sync reconciles the thread: a reply closes it, a missed due date makes it *overdue* and the brief nags with a ready-to-send follow-up. Inbound questions with no answer from you show up under *Needs your reply*. |
| **Morning email brief** | At the configured time (default 07:00 America/New_York): *Needs you first*, *Needs your reply*, *Waiting on them*, *Money, permits and deadlines*, *Housekeeping*. Rendered as markdown (dashboard), a spoken script (voice) and a ≤320-char SMS. |
| **SMS / voice** | Twilio for SMS and calls. The call reads the brief: with ElevenLabs configured it plays Stephanie's own voice (MP3 served by the engine); without it, Twilio's built-in voice, and the record says which. The engine will **only ever contact the configured owner phone**. |
| **Cleanup** | Archives old, low-priority, already-read mail out of the working views. Before anything is archived its original `.eml` is on disk; optionally a server-side **COPY** lands in `Steph/Archive`. There is no delete, expunge or move code path. |
| **Dashboard** | Activation checklist, urgency lanes, message viewer with *why*, brief with Play button, waiting-on-them with one-click follow-ups, cleanup plan/run, notification log. |

## Quick start (any Linux / macOS box)

```bash
cd steph-email-engine
python3 -m venv .venv && . .venv/bin/activate
pip install .

export STEPH_EMAIL_OWNER_PHONE=+19785550100        # the only number the engine will text/call
steph-email accounts add you@gmail.com             # detects Gmail, prompts for the app password
steph-email sync                                   # pull the last 500 messages, score them
steph-email urgent                                 # unread high + critical, with reasons
steph-email brief --print                          # today's brief (markdown + spoken script)
steph-email status                                 # activation checklist
steph-email serve                                  # dashboard + API on http://127.0.0.1:8030
```

Connecting each provider:

| Provider | What you need | Where to get it |
|---|---|---|
| Gmail / Google Workspace | App password | Google Account → Security → 2-Step Verification → App passwords |
| Outlook.com / Microsoft 365 | OAuth2 refresh token + client id | Azure app registration with `IMAP.AccessAsUser.All`, `SMTP.Send`, `offline_access`; `--auth oauth2 --oauth-client-id …`, paste the refresh token as the secret |
| Yahoo | App password | Account Security → Generate app password |
| iCloud | App-specific password | appleid.apple.com → Sign-In and Security |
| Zoho / GoDaddy legacy | Mailbox or app password | provider settings (enable IMAP) |
| Anything else | `--provider generic --imap-host … --smtp-host …` | your hosting control panel |

## Activation checklist (what is still pending)

`steph-email status` prints this live. Until every non-optional line clears the
engine reports **ACTIVATION PENDING** and every SMS / call is recorded with
`truth_label: STAGED` — nothing leaves the box.

1. **Mailbox authorization** — connect at least one mailbox and get a clean sync
   (`accounts add`, `sync`, `accounts list` shows no `last_error`).
2. **Notification credentials** — set `STEPH_EMAIL_OWNER_PHONE` and the three
   `STEPH_EMAIL_TWILIO_*` values. Test: `steph-email notify test-sms` → `[LIVE]`.
3. **Verified voice playback** (optional but required before calling it "Stephanie's voice") —
   set `STEPH_EMAIL_ELEVENLABS_API_KEY` and `STEPH_EMAIL_ELEVENLABS_VOICE_ID`, then:
   ```bash
   steph-email voice verify                 # key → auth → voice id → synthesis; writes evidence/voice-sample.mp3
   # play evidence/voice-sample.mp3 on the phone, then
   steph-email voice verify --confirm-playback --device "iPhone 15 Pro" --note "clear, correct voice"
   ```
   The evidence file `evidence/voice-verification.json` says `verified: true`
   only when all five steps pass, including the human playback step. There is
   no fallback voice that pretends to be Stephanie.

## Configuration

All settings are environment variables prefixed `STEPH_EMAIL_` (or a `.env`
file). See `.env.example` for the full list. The ones that matter most:

| Variable | Default | Meaning |
|---|---|---|
| `OWNER_PHONE` | empty | E.164 number; the only destination for SMS / calls |
| `OWNER_ADDRESSES` | empty | extra addresses that count as "me" for reply tracking |
| `VIP_SENDERS` / `VIP_DOMAINS` | empty | senders that always score higher |
| `ALERT_MIN_URGENCY` | `critical` | SMS immediately at or above this level |
| `REPLY_DUE_DAYS` | `3` | default expectation window for mail you send |
| `BRIEF_HOUR` / `BRIEF_MINUTE` / `TIMEZONE` | `7` / `0` / `America/New_York` | when the morning brief runs |
| `BRIEF_SMS` / `BRIEF_CALL` | `true` / `false` | deliver the brief by SMS, and/or read it in a call |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` | empty | leave blank to stay STAGED |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | empty | the voice option |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8030` | what Twilio fetches TwiML and brief audio from (must be HTTPS and reachable for calls) |
| `CLEANUP_AFTER_DAYS` | `30` | age threshold for the cleanup pass |
| `CLEANUP_COPY_TO_SERVER` | `false` | also COPY archived mail into `Steph/Archive` on the server |
| `CLEANUP_AUTO` | `false` | run cleanup before each morning brief |
| `STORE_RAW` | `true` | keep every original `.eml` under `<data_dir>/raw` |
| `API_TOKEN` | empty | require `X-API-Token` on `/api/*` (set it before binding to anything but 127.0.0.1) |

## VPS deployment (Ubuntu 22.04 / Debian 12 / Rocky 9)

```bash
# 1. get the code onto the box
git clone https://github.com/gcagentgit/nobleport-ecosystem.git
cd nobleport-ecosystem/steph-email-engine

# 2. install as a hardened systemd service (creates user `steph`, venv in /opt, data in /var/lib)
sudo bash deploy/install.sh

# 3. put the owner phone, Twilio and (optionally) ElevenLabs keys in the config
sudo nano /etc/steph-email-engine/env
sudo systemctl restart steph-email

# 4. connect mailboxes as the service user so the secret store matches
sudo -u steph env $(grep -v '^#' /etc/steph-email-engine/env | xargs) steph-email accounts add you@gmail.com
sudo -u steph env $(grep -v '^#' /etc/steph-email-engine/env | xargs) steph-email sync
sudo -u steph env $(grep -v '^#' /etc/steph-email-engine/env | xargs) steph-email status

# 5. watch it
journalctl -u steph-email -f
```

What the installer sets up:

* `steph-email.service` — dashboard + API + in-process sync loop + brief scheduler.
* `steph-email-sync.timer` — a one-shot sync every 5 minutes as a fallback.
* `/var/lib/steph-email-engine` — SQLite db, `secret.key`, `raw/` originals, `audio/`, `evidence/`.
* `/etc/steph-email-engine/env` — config (mode 640, root:steph).

**Voice calls need a public HTTPS URL.** Twilio fetches the TwiML and the MP3
from `STEPH_EMAIL_PUBLIC_BASE_URL`. Put Caddy or nginx in front
(`deploy/Caddyfile.example` gives auto-TLS in three lines), set `API_TOKEN`,
and open only 443. The dashboard reads the token from
`localStorage.steph_email_token` in the browser.

Docker is also supported: `cp .env.example .env && docker compose up -d`
(port 8030 bound to 127.0.0.1; data in the `steph-email-data` volume).

### Backups

Everything lives under the data directory. `raw/` is the archive of originals
and is safe to sync to object storage; the SQLite file is safe to copy while
the service runs (WAL mode).

## API

`steph-email serve` → `http://127.0.0.1:8030/docs` for the interactive OpenAPI page.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | activation checklist, live/staged state |
| GET | `/api/overview` | counts per account, urgency lanes, reply summary |
| GET/POST/DELETE | `/api/accounts[/{id}]` | connect (tests the login), list, remove |
| POST | `/api/sync?account=` | pull new mail now; returns `urgent_new` ids |
| GET | `/api/messages?min_urgency=high&unread=true&needs_reply=true&q=&tag=&archived=` | unified inbox / search |
| GET | `/api/urgent` | unread high + critical, most urgent first |
| GET | `/api/messages/{id}` · `/thread` | full message with reasons; its thread |
| POST | `/api/messages/{id}/flags` · `/tags` · `/restore` | flags, manual tags, un-archive |
| POST | `/api/send` | `{"account","to","subject","text","reply_to_message_id","expect_reply","due_days"}` |
| GET | `/api/replies?overdue=` | waiting-on-them + needs-your-reply |
| POST | `/api/replies/{id}/nudge` · `/close` | draft a follow-up; close |
| GET/POST | `/api/brief` · `/api/brief/preview` · `/api/brief/run` · `/api/brief/{date}` | latest / preview / build+deliver / by date |
| GET | `/audio/brief-{date}.mp3` | brief audio for Twilio `<Play>` |
| GET/POST | `/webhooks/twilio/brief/{date}` | TwiML for the call (unauthenticated by design) |
| GET/POST | `/api/notifications` · `/api/notify/test-sms` | log; send a test SMS to the owner |
| POST/GET | `/api/voice/verify` · `/api/voice/evidence` | run the voice checks; read the evidence |
| GET/POST | `/api/cleanup/plan` · `/api/cleanup/run` | dry run; archive |

## CLI

```
steph-email status [--json]
steph-email accounts add|list|test|remove|providers
steph-email sync [--account X] [--loop]
steph-email inbox|search|urgent [--urgency high] [--unread] [--needs-reply] [--tag permit] [--json]
steph-email show ID [--mark-read]
steph-email send --from A --to B --subject S --body T [--reply-to ID] [--due-days 3] [--no-track]
steph-email replies [--overdue] | replies nudge ID | replies close ID
steph-email brief [--date YYYY-MM-DD] [--deliver] [--voice] [--print] [--json]
steph-email notify test-sms
steph-email voice verify [--confirm-playback --device "…" --note "…"]
steph-email cleanup [--run] [--copy-to-server]
steph-email serve
```

## Governance notes (NoblePort rules)

* Outbound SMS / calls are restricted to the single configured owner number;
  any other destination raises `DestinationError`. The engine cannot be turned
  into a broadcast tool.
* Every notification is logged with a `truth_label` (`STAGED` / `LIVE`) and the
  transport that carried it. Nothing is called "live" without the credentials
  that make it so.
* Voice is `verified` only with the human playback step recorded in
  `evidence/voice-verification.json`. No fallback voice is ever labelled as
  Stephanie's.
* Cleanup preserves originals on disk before acting and never deletes; the
  cleanup log records every action with the path of the preserved original.
* Sending mail still happens from your own account through your own SMTP
  server; the engine drafts follow-ups but you press send.

## Development

```bash
pip install -e ".[dev]"
pytest            # 97 tests, all offline (in-memory IMAP/SMTP, mocked Twilio/ElevenLabs)
```

```
steph_email/
  config.py       settings (STEPH_EMAIL_*)
  providers.py    presets + domain auto-detect       ┐
  crypto.py       Fernet secret store                │ mailbox adapters
  parser.py       RFC 822 → dict, thread keys        │ (adapted from
  imap_client.py  imaplib wrapper: UID sync, COPY    │  NoblePort MailHub)
  smtp_client.py  message builder + sender           │
  oauth.py        refresh-token exchange             ┘
  rules.py        construction / real-estate tagger
  urgency.py      0–100 scoring, levels, reasons, needs-reply detection
  replies.py      expected-reply tracking (waiting on them / needs your reply)
  brief.py        morning brief → markdown, spoken script, SMS
  notify.py       Simulated / Twilio transports, ElevenLabs voice, TwiML, verification evidence
  cleanup.py      originals-preserving archive pass (no delete anywhere)
  service.py      EmailEngine orchestration + activation status
  api.py          FastAPI app + scheduler (sync loop, brief at the configured time)
  cli.py          `steph-email`
  web/index.html  dashboard
deploy/           install.sh, systemd units, Caddyfile example
```
