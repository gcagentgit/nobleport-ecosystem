# Steph SMS and voice setup

The adapters are implemented and exercised with simulated provider responses. No SMS, phone call, provider authentication, or Stephanie voice playback has been verified against a live account. Default operation creates dashboard previews.

## Delivery controls

Set credentials in the deployment environment or protected environment file, never in YAML, source code, a browser URL, or a chat message. The only permitted destination is the operator's `NOTIFY_PHONE`; email content cannot choose a recipient.

| Variable | Purpose |
| --- | --- |
| `NOTIFY_PHONE` | Michael's chosen receiving number in E.164 format, e.g. `+1` followed by ten digits |
| `TWILIO_ACCOUNT_SID` | Account SID beginning `AC` |
| `TWILIO_AUTH_TOKEN` | Twilio account authentication secret |
| `TWILIO_FROM_NUMBER` | SMS-capable Twilio number in E.164 format |
| `TELNYX_API_KEY` | API key for the Telnyx account |
| `TELNYX_CONNECTION_ID` | Call Control application's connection ID |
| `TELNYX_FROM_NUMBER` | Authorized Telnyx caller number in E.164 format |
| `TELNYX_PUBLIC_KEY` | Base64 Ed25519 public key from the Telnyx account |
| `TELNYX_WEBHOOK_URL` | Public HTTPS URL ending `/webhooks/telnyx`, with no query or credentials |
| `TELNYX_VOICE` | Optional; `female` by default, or a supported provider voice string |
| `TELNYX_ELEVENLABS_API_KEY_REF` | Only for ElevenLabs: the name of an integration secret already stored in Telnyx |

Start in `dry_run: true`. Review the queued alert text and routing. To enable actual delivery, configure `dry_run: false` at startup, enable `sms_notifications` and/or `voice_notifications`, and select urgent/summary routing under `notification_channel` (`sms`, `voice`, or `both`). Individual expected-email watches choose their own channel while respecting each channel master switch. Demo mode always blocks provider sends. Settings are rechecked for every new SMS/dial request and before voice speech. The engine applies quiet hours, message-age limits and the hourly cap before dispatch. Notifications default to generic dashboard prompts to limit information on a locked phone or voicemail.

## Twilio SMS

The adapter uses a form-encoded HTTPS POST with `To`, `From`, `Body` and a five-minute provider queue validity period. It records Twilio's message SID. `submitted` means API acceptance; this version does not implement a Twilio delivery-status callback. Confirm the chosen sender and receiver are permitted by the account; trial accounts require verified recipient numbers. [Twilio Message resource](https://www.twilio.com/docs/messaging/api/message-resource)

No automatic resend occurs after a timeout, connection interruption, server error or unparseable acceptance. The outcome becomes `uncertain`, and reusing the notification ID returns the recorded outcome. Before manually creating a new attempt, reconcile the original request in the provider console. This intentionally trades automatic retry for protection against duplicate texts or calls. Local process crashes retain the same uncertainty state.

## Telnyx phone voice

Configure the Voice API application to send POST callbacks to the configured HTTPS endpoint. The application accepts only valid Ed25519 signatures over the exact `timestamp|body` bytes, within five minutes of the local clock. It verifies the local random call correlation token, connection ID and call ID; answer events also must match the configured caller and recipient. Event IDs are durably deduplicated. [Telnyx call answered schema and signing format](https://developers.telnyx.com/api-reference/callbacks/call-answered)

A dial request has a 30-second answer timeout and a 90-second maximum answered-call duration. The response registers the call ID; the adapter also handles an authenticated callback that arrives after a dial timeout or before its HTTP response. The stored dial result may remain `uncertain` while separate voice-event evidence confirms later activity. [Telnyx Dial](https://developers.telnyx.com/api-reference/call-commands/dial)

The webhook acknowledges after durable validation/queueing; it does not call the provider while handling the HTTP request. `Engine.tick()` drains queued voice actions. A verified `call.answered` enables one plaintext speak command. A verified `call.speak.ended` enables one hangup command. A prior hangup prevents a delayed answer event from triggering speech. Deterministic command IDs and persisted local claims prevent repeat commands across duplicate events, concurrent workers and restarts. A speak timeout remains uncertain and is never replayed automatically. [Telnyx receiving webhooks](https://developers.telnyx.com/docs/development/api-fundamentals/webhooks/receiving-webhooks), [Speak ended schema](https://developers.telnyx.com/api-reference/callbacks/call-speak-ended)

Keep server time synchronized. Reverse proxies must preserve raw request bytes and the `telnyx-timestamp` / `telnyx-signature-ed25519` headers. Expose the webhook through HTTPS and keep dashboard authentication enabled. The worker must remain running to drain events. Webhooks older than the signature window and calls older than fifteen minutes are rejected or expired. If a worker is down or speech is suppressed after dial, the provider's call-duration limit bounds the call.

`notifier.list_voice_status()` provides read-only operator diagnostics: whether an answer, speech completion, or hangup was recorded, speech/hangup command status and sanitized error detail. Neither an answered call nor completed synthesis proves Michael heard the message. The app does not record calls, detect human versus voicemail, or support a two-way Jarvis conversation.

## Optional Stephanie voice through ElevenLabs

The Telnyx speak API supports ElevenLabs as a TTS provider. After selecting an approved Stephanie voice in the ElevenLabs account and storing its API key as a Telnyx integration secret, set:

```dotenv
TELNYX_VOICE=ElevenLabs.eleven_multilingual_v2.YOUR_APPROVED_VOICE_ID
TELNYX_ELEVENLABS_API_KEY_REF=YOUR_TELNYX_INTEGRATION_SECRET_NAME
```

The adapter supplies the secret reference in `voice_settings`, keeps the payload plain text and selects premium synthesis. Missing secret references fail before placing a call. This path is verified against a mocked request contract; its credentials, voice ID, quality and end-to-end playback require a live operator test. No separate ElevenLabs SDK is needed for this provider-hosted path. This is an outbound spoken email alert, not the separate LiveKit conversational agent. [Telnyx Speak text and ElevenLabs voice settings](https://developers.telnyx.com/api-reference/call-commands/speak-text)

## Focused verification

```bash
python -m pytest -q tests/test_notifier.py
```

The tests use generated Ed25519 keys and fake HTTP transport: dry-run/disabled gates, duplicate dispatch IDs, timeout persistence after restart, sanitized HTTP failures, signed-body tampering, expired signatures, wrong call/recipient, webhook replay, answer-before-speak ordering, hangup ordering, and ElevenLabs secret-reference requirements. They send nothing externally.
