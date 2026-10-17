# Mailbox connection setup

This build has been tested with simulated IMAP and OAuth providers. No live mailbox has been connected. Account status becomes `connected` only after a successful authenticated, read-only sync. Keep notifications in preview mode during first import.

## Supported connection paths

| Provider | IMAP host / TLS port | Authentication in this build |
|---|---|---|
| Gmail / Google Workspace | `imap.gmail.com:993` | OAuth2 refresh token; account app password where Google permits it |
| Microsoft 365 / Outlook.com | `outlook.office365.com:993` | Delegated OAuth2; app passwords are rejected |
| Yahoo | `imap.mail.yahoo.com:993` | Account-generated app password; verify availability in account security |
| Other IMAP | Operator-specified host, TLS 993 by default | App-password environment reference, or externally managed OAuth access token |

The configured folder defaults to `INBOX`. Only configured folders are read; this is not a historical export of every folder. To include another folder, add a separate account entry with a distinct `id`, the same mailbox identity, and its exact folder name. The same message appearing in two accounts/folders is retained as two source records. No fuzzy cross-provider deduplication is performed.

Account IDs are permanent mailbox identities. The first connection attempt binds an `id` to its host, port, and exact configured username in `ingestion.db`. Reusing that ID with a different mailbox is rejected before connecting, preventing cursor reuse and missing messages. Hostname case and a trailing DNS dot are normalized; username case is preserved. Use a new ID for a replacement mailbox or a changed connection identity, including when correcting the identity after a first attempted setup.

For Yahoo, the official support endpoint could not be retrieved during this build; its live authentication path remains unverified. Consult [Yahoo's app-password help](https://help.yahoo.com/kb/SLN15241.html) inside your account before enabling it.

## Configuration shape

```yaml
accounts:
  - id: construction_gmail
    enabled: false
    provider: gmail
    host: imap.gmail.com
    port: 993
    username: YOUR_ACTUAL_MAILBOX
    folder: INBOX
    lookback_days: 30
    batch_size: 100
    max_message_bytes: 2097152
    uid_scan_window: 10000
    scan_windows_per_cycle: 20
    timeout_seconds: 30
    idle_enabled: true
    auth:
      type: oauth2
      token_url: https://oauth2.googleapis.com/token
      client_id_env: GMAIL_CLIENT_ID
      client_secret_env: GMAIL_CLIENT_SECRET
      refresh_token_env: GMAIL_REFRESH_TOKEN
```

`*_env` fields contain environment variable **names**, never credential values or `${...}` substitutions. The engine resolves those references directly. The local protected environment file must be loaded into the process by the deployment launcher. Do not put tokens in dashboard fields, URLs, screenshots, source control, or chat.

For app-password authentication, replace the `auth` mapping with:

```yaml
auth:
  type: app_password
  password_env: YAHOO_APP_PASSWORD
```

For a short-lived access token managed by an existing approved identity service:

```yaml
auth:
  type: oauth2
  access_token_env: MAILBOX_ACCESS_TOKEN
```

That mode does not refresh tokens automatically. The external manager must refresh credentials and restart the process when its inherited environment changes.

## Gmail consent and refresh tokens

Register your own OAuth client and configure its consent screen and authorized redirect URI. Obtain user consent through Google's authorization-code flow using `https://mail.google.com/` and `access_type=offline`; exchange the authorization code through the registered client to obtain the refresh token. Store the client ID, client secret when applicable, and refresh token in the environment variables above. This package consumes the resulting credentials; it does not include an interactive consent server.

Gmail's IMAP scope grants broad mail access even though this adapter only reads. Google recommends the Gmail API with more granular scopes for applications that do not need the full mail scope. A Gmail API ingestion adapter is a future option; it is not implemented here. Public distribution may require Google's application verification. See [Gmail's XOAUTH2 scope documentation](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol) and [Google's server-side authorization flow](https://developers.google.com/workspace/gmail/api/auth/web-server).

Google app-password availability depends on account configuration and normally requires 2-Step Verification; some organizational and security configurations do not expose it. See [Google's app-password instructions](https://support.google.com/accounts/answer/185833).

## Microsoft consent and refresh tokens

Register the client in Microsoft Entra with the correct supported account type. Use a delegated authorization-code or device-code flow requesting:

```text
https://outlook.office.com/IMAP.AccessAsUser.All offline_access
```

Complete the mailbox owner's sign-in and any required tenant consent. Configure:

```yaml
auth:
  type: oauth2
  token_url: https://login.microsoftonline.com/YOUR_TENANT_ID/oauth2/v2.0/token
  client_id_env: OUTLOOK_CLIENT_ID
  # Include only for a confidential client that has a client secret:
  client_secret_env: OUTLOOK_CLIENT_SECRET
  refresh_token_env: OUTLOOK_REFRESH_TOKEN
  scope: https://outlook.office.com/IMAP.AccessAsUser.All offline_access
```

A public/device-code client omits `client_secret_env`. Tenant or account policy must allow IMAP. Application-only service-principal authentication is a different setup and is not implemented. See [Microsoft's IMAP OAuth documentation](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth).

## Encrypted refresh-token rotation

Set `STEPH_TOKEN_ENCRYPTION_KEY` to a Fernet key generated locally using `cryptography.fernet.Fernet.generate_key()`. Keep this key in the protected service environment, separately from data backups. Without it, refresh-token authentication stops before requesting a token.

Rotated refresh tokens are encrypted into `data_dir/oauth-tokens.json` using atomic replacement and owner-only file permissions. Access tokens remain in process memory. A successful rotation is persisted before the access token is used. The cache is bound to account ID, mailbox username, client ID, and token endpoint. Run one engine process per data directory.

Microsoft can rotate and revoke refresh tokens; a revoked grant requires interactive sign-in again. See [Microsoft's refresh-token guidance](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens). After reauthorization, stop the service, replace the environment credentials, remove the obsolete encrypted cache entry (or cache file when deliberately reauthorizing every entry), and restart. Preserve the encryption key across ordinary restarts.

## Ingestion behavior and operational limits

- TLS certificate and hostname validation are always enabled. Every folder is selected read-only, and message content uses `BODY.PEEK`. There are no IMAP delete, move, archive, mark-read, or expunge operations.
- Unique source identity is account + folder + UIDVALIDITY + UID. Cursors advance after the engine commits a message. A crash between processing and cursor update replays the same identity; the engine's unique key absorbs it. A changed UIDVALIDITY creates a new source namespace and resets initial sync.
- The first sync uses a durable 30-day cutoff by default. Server `INTERNALDATE`, converted to UTC, controls arrival time. An email's `Date` header does not control matching or alert freshness. Messages intentionally outside the initial cutoff are skipped.
- UID searches use bounded numeric windows. Each cycle processes at most 100 messages and scans at most 20 windows of 10,000 UID values by default. Very large or sparse mailboxes can take several cycles to reach recent mail; the account detail reports its cursor and snapshot. Initial cutoff state lives in `ingestion.db` and survives restarts.
- Messages larger than 2 MiB default to header-only ingestion with an explicit body-omission marker. Subject-based urgency and expectation matching still work; body-based classification and attachment review are incomplete for these records. Inspect the original mailbox. Attachment bytes are not persisted.
- Parsed text is limited to 20,000 characters, individual text parts to 65,536 decoded bytes, traversal to 100 MIME parts and 20 nesting levels. HTML becomes inert plain text; no remote images, links, or scripts are fetched or executed. Attachment metadata reports an unknown decoded size as `null`.
- IDLE uses a dedicated authenticated read-only connection. The listener completes `DONE` before waking the engine; the engine fetches on a separate connection. It renews connections after 20 minutes and reconnects with bounded backoff. Scheduled UID polling remains the fallback. There is no guaranteed subsecond arrival or notification latency. See [IMAPClient's API lifecycle](https://imapclient.readthedocs.io/en/3.0.1/api.html).

## First connection check

1. Populate the real mailbox identity and protected environment values locally; leave notification `dry_run: true`.
2. Enable one mailbox in the configuration and start the engine. Check account status and the cursor details in the private dashboard.
3. Receive a harmless test message in the source mailbox. Confirm it appears once, its unread state remains unchanged, and any urgency/expected alert is a preview.
4. Restart and confirm no duplicate source record is created. Then connect the remaining mailboxes. Real SMS/voice delivery is configured separately in `NOTIFICATION_SETUP.md`.

The automated tests cover these ingestion mechanisms with simulated providers. Live sign-in, network availability, provider throttling, mailbox policy, and actual delivery latency remain deployment checks.
