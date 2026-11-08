# Verification evidence · Steph Email Engine 0.1.0

Build date: September 13, 2026. Final local suite: **71 passed in 1.29 seconds**, exit 0. This is evidence for the local implementation and simulated provider contracts, not evidence of live mailbox/telephony operation.

## Executed checks

| Check | Result | What it establishes |
|---|---|---|
| `python -m pytest -q --tb=short` | 71 passed; exit 0 | Integrated test suite against real SQLite/Flask and simulated provider transports |
| `python -m steph_email check-config` | Valid; exit 0 | Sample configuration parses, preview mode enabled, zero accounts enabled, America/New_York timezone |
| `python -m compileall -q steph_email` | Exit 0 | Python modules compile |
| `node --check steph_email/static/dashboard.js` | Exit 0 | Dashboard JavaScript syntax parses |
| `pip install --no-deps -e .` | Built and installed; exit 0 | Project metadata/entry-point installation succeeds |
| Real CLI/Waitress HTTP smoke | Passed within suite | Server boots, rejects anonymous mail access, session login succeeds, five demo emails load, CSS/JS served, reply watch created, settings persist, live-mode UI change blocked, logout works |
| Browser visual check | Not completed | No browser installed; Playwright Chromium download timed out. No claim of visually inspected desktop/mobile rendering |

## Behavioral coverage

- 23 mailbox/OAuth tests: read-only PEEK, MIME/HTML parsing, bounded oversized messages, UIDVALIDITY changes, resume after callback failure, initial import cutoff across restart, sparse UID scans, OAuth endpoint restrictions, encrypted refresh-token rotation, IDLE completion/recovery, immutable mailbox identity binding.
- 15 notification tests: dry-run/demo/channel gates, configured recipient, Twilio submission, Telnyx dial/answer/speak/hangup contracts, signature tampering and stale events, correlation and replay handling, timeout persistence, optional ElevenLabs request shape.
- 14 core tests: concurrent duplicate ingestion, mailbox identity separation, overlapping urgent/expected rules, exact domain matching, historical imports, delayed expected receipt, suspicious expected mail visibility, keyword boundaries, reversible cleanup, rate limits/quiet hours, daily DST scheduling, expiration, restart during submission, invalid settings/credentials/timestamps.
- 15 Flask tests: authentication, session/CSRF protection, login throttling, input validation, read-only deployment settings, API state changes, HTML injection safety, protected responses, webhook delegation, demo isolation.
- 2 review regressions: expiry uses original receipt time and a newly matched expected reply supersedes an unsent overdue alert.
- 1 startup regression: morning summary waits for initial ingestion and identifies incomplete mailbox coverage.
- 1 real-server smoke test as described above.

## Live checks still required

Mailbox provider consent and authenticated sync, a real Twilio receipt, Telnyx signed callbacks from the provider, audible phone playback, the approved Stephanie ElevenLabs voice, local espeak-ng/audio hardware, HTTPS deployment and VPS operation were not tested. No emails were sent, no SMS/calls were placed, and no mailbox messages were deleted or altered.

The spam classifier uses rules, not trained ML. No spam-accuracy percentage, arrival-latency guarantee or delivery SLA is claimed. Incoming mail remains untrusted content and cannot invoke system commands. This is a single-process pilot build; hosting readiness depends on the installation and live checks above.
