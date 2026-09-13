# Steph Email Engine · NoblePort Construction LLC

A runnable email operating service: multiple IMAP inboxes, construction urgency rules, expected-email watches, a private dashboard, and SMS/voice adapters. Version 0.1.0 is an implementation for a controlled pilot. No real account connection, SMS delivery, telephone playback, or VPS deployment was verified in this build.

## Start the working demo

Requires Python 3.11+ and a computer or Linux server. From this extracted folder:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.lock
venv/bin/pip install --no-deps -e .
venv/bin/python -m steph_email new-secrets > demo.env
chmod 600 demo.env
venv/bin/python -m steph_email demo --env-file demo.env
```

Open http://127.0.0.1:5050 and log in using the `STEPH_DASHBOARD_TOKEN` value in your private `demo.env` file. Five clearly marked synthetic messages demonstrate an urgent inspection, drawings watch, decking credit, spam review, and mobilization notes. The demo has no mailbox connections and cannot send alerts. It uses a separate `demo-data/` directory. Stop with Ctrl+C.

The dashboard works on phone-sized screens. An iPhone can access it after it is hosted behind HTTPS on your VPS; opening this source archive on an iPhone does not run the server.

## What is implemented

| Function | Behavior |
|---|---|
| Gmail, Outlook, Yahoo and custom IMAP | TLS, OAuth2/app-password environment references, read-only folder selection, PEEK reads; enabled accounts synchronized independently |
| Near-real-time ingestion | IMAP IDLE wakes a worker; reconnect/backoff and 60-second polling fallback. No subsecond latency guarantee |
| First import | Configurable 30-day lookback, bounded UID scanning, durable resume cursor; historical alerts remain previews |
| Duplicate control | One stored record for each account/folder/UIDVALIDITY/UID; one alert per message per channel across overlapping rules |
| Spam review | Transparent phrases and blocked-sender rules; reversible local quarantine. No trained ML model or claimed accuracy |
| Construction urgency | Safety, inspections, bid deadlines, permits, invoices, AWOs, drawings and credits; score and reasons visible |
| Expected mail | Exact sender address/domain plus subject phrase and arrival window; pending, matched, expired and cancelled states; overdue alerts |
| SMS | Twilio API adapter, fixed operator recipient, generic message by default |
| Voice calls | Telnyx dial, signed answer callback, plaintext speech, completion hangup. Optional approved Stephanie ElevenLabs voice through Telnyx |
| Local speech | `steph-email speak-brief` uses locally installed espeak-ng and speakers; no cloud API |
| Quiet hours and limits | Default 8 PM–7 AM Eastern; 6 alerts/hour across channels; stale alerts suppressed after 2 hours |
| Daily email brief | Once per local date after 8 AM America/New_York, persistent across restart, DST aware |
| Daily cleanup review | Once per local date after 3 AM; normal messages older than 90 days enter local review; mailbox originals preserved |
| Private dashboard | Authenticated session or bearer API, CSRF controls, escaped text, no remote email HTML/assets, persistent settings |
| Delivery evidence | Preview, pending, submitted, failed or uncertain. Provider submission is not recipient delivery |

Multiple inboxes containing a copy of the same email remain distinct records. Cross-account `Message-ID` deduplication is deliberately absent because that header is sender controlled and would hide independently delivered records. Attachment metadata is recorded; file bytes are not downloaded to disk. Bodies are capped at 20,000 characters and oversized messages are stored as header-only records. Use the original mailbox for full records and attachments.

## Connect your installation

1. Read [MAILBOX_SETUP.md](docs/MAILBOX_SETUP.md). Register the mailbox OAuth clients and complete provider consent; the engine consumes the resulting authorized credentials. An interactive OAuth consent UI is not included.
2. Put credentials in a protected environment file using the names in `.env.example`. `--env-file` reads literal `KEY=value` lines, without shell execution or substitution. Files and environment variables are private operator inputs.
3. Set each real address in `config/settings.yaml` and enable the intended accounts. Keep `settings.dry_run: true` during the first sync. Run `steph-email check-config`, then `steph-email once` to ingest a cycle, or `steph-email serve` for continuous operation.
4. Use the dashboard to inspect authenticated mailbox status, urgency reasons and notification previews. Rule scores are routing hints; a From match does not establish sender identity.
5. Read [NOTIFICATION_SETUP.md](docs/NOTIFICATION_SETUP.md), configure your actual recipient/sending numbers and provider credentials. Voice requires the HTTPS signed-webhook endpoint. Set the approved ElevenLabs voice reference to use Stephanie's voice; this build does not itself train or clone a voice.
6. Set `dry_run: false` in configuration and restart when the preview results and recipient configuration are correct. Enable the desired channel switches. Existing preview alerts are never promoted or replayed. Verify one new email through to actual receipt before relying on alerts.

Dashboard settings survive restarts. `dry_run` and `demo_mode` are startup-only values. Account authentication and rules are loaded on startup. Daily schedules catch up once on startup after the configured time; they do not replay every missed day. Changing time zones mid-day is an operator configuration change and can change that day's scheduling boundary.

Expected replies worth setting up first: Kyle's drawings, Luigi's decking credit, John Cherry's window response, and rough-plumbing invoices. Enter their actual sender addresses; no contact addresses were guessed. The 8 AM digest covers this email engine only. HubSpot, calendar, voicemail, and the wider Jarvis brief are separate integrations.

## VPS deployment

This build includes systemd and Caddy examples; it has not installed or changed your VPS.

- Create a dedicated `steph-email` system user. Extract the project to `/opt/steph-email`, create `/opt/steph-email/venv`, and install the locked dependencies and project there.
- Create `/etc/steph-email.env` with mode `0600` and the required credentials. Use separate random dashboard and session keys. The included `new-secrets` command generates local secrets; keep its output private.
- Copy `deploy/steph-email.service` into `/etc/systemd/system/` after reviewing paths. The service uses `/var/lib/steph-email` for state and binds only to `127.0.0.1:5050`.
- Configure your HTTPS reverse proxy using `deploy/Caddyfile.example`; replace the sample hostname. Keep port 5050 off the public network. `STEPH_COOKIE_SECURE=true` is set by the service.
- Run `systemctl daemon-reload` and `systemctl enable --now steph-email`. Inspect `systemctl status steph-email` and the private dashboard. Use one application process; the package is not designed for multiple replicas sharing this SQLite installation.

The minimal `/health` endpoint confirms the HTTP process responds. It does not prove mailbox freshness or notification delivery. The dashboard shows each account's last sync timestamp and outcome. After an uncertain send, reconcile the provider record manually; there is no automatic retry button that could place a duplicate call.

Local mail text is stored in SQLite with a private directory and file permissions, not database encryption. Use encrypted server storage and encrypted backups appropriate for your installation. OAuth refresh tokens are separately encrypted using `STEPH_TOKEN_ENCRYPTION_KEY`; retain that key with your protected recovery material. Do not treat this database as the archive of record: bodies are bounded and attachments are metadata only.

For backup, stop the service and copy the complete state directory together with the protected configuration and encryption key to your encrypted backup destination; restart after copying. This keeps the email database, ingestion cursors, and token state consistent. For rollback, restore the tested package and a matching complete state backup. Never overwrite a live database with an older backup while the service is running.

## Verification

```bash
venv/bin/python -m pytest -q
venv/bin/python -m steph_email check-config
```

The tests exercise real local SQLite and Flask behavior and fake provider transports. See [VERIFICATION.md](docs/VERIFICATION.md) for the final evidence and boundaries. Provider documentation references are in the mailbox and notification setup guides.

## Corrections to the supplied draft

The draft contained an undefined database, unused settings, a missing training dataset, unsafe pickle loading, naive/aware timestamp arithmetic, substring sender matching, repeated 24-hour fetch alerts, unchecked browser HTML, credentials in account configuration, immediate speech before a call was answered, and automatic deletion of construction correspondence. Those are replaced with implemented persistence, validated controls, explainable rules, exact matching, UTC timestamps, durable cursors/outbox, escaped text, protected secret references, signed voice events, and reversible cleanup review.

There is no ML training, permanent email deletion, reply sending, voice daemon, or guaranteed message-delivery SLA in this release. Local speech is an on-demand command, not an automatic fallback that might expose private mail on server speakers. Live telephony, local audio playback and mailbox consent remain installation steps; tests here do not establish them.
