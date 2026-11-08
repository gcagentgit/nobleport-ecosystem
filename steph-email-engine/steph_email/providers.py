"""Provider presets so connecting a mailbox needs only an address + secret.

Every preset carries the IMAP/SMTP endpoints, the auth methods it supports and
short human instructions (where to create an app password, etc.).  Unknown
domains fall back to ``generic`` and require explicit host settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    imap_host: str
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_ssl: bool = False            # True -> implicit TLS (465); False -> STARTTLS
    auth_methods: tuple[str, ...] = ("password",)
    oauth_token_url: str = ""
    oauth_scope: str = ""
    domains: tuple[str, ...] = ()
    notes: str = ""
    sent_folder: str = "Sent"
    extra: dict = field(default_factory=dict)


PROVIDERS: dict[str, Provider] = {
    "gmail": Provider(
        key="gmail",
        name="Gmail / Google Workspace",
        imap_host="imap.gmail.com",
        smtp_host="smtp.gmail.com",
        auth_methods=("password", "oauth2"),
        oauth_token_url="https://oauth2.googleapis.com/token",
        oauth_scope="https://mail.google.com/",
        domains=("gmail.com", "googlemail.com"),
        sent_folder="[Gmail]/Sent Mail",
        notes=(
            "Enable 2-Step Verification, then create an App Password at "
            "https://myaccount.google.com/apppasswords and use it as the secret. "
            "Google Workspace domains work the same way."
        ),
    ),
    "outlook": Provider(
        key="outlook",
        name="Outlook.com / Microsoft 365",
        imap_host="outlook.office365.com",
        smtp_host="smtp.office365.com",
        auth_methods=("oauth2", "password"),
        oauth_token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        oauth_scope=(
            "https://outlook.office.com/IMAP.AccessAsUser.All "
            "https://outlook.office.com/SMTP.Send offline_access"
        ),
        domains=("outlook.com", "hotmail.com", "live.com", "msn.com"),
        sent_folder="Sent Items",
        notes=(
            "Microsoft retired basic IMAP passwords for most tenants; use OAuth2 "
            "(client_id + refresh_token from an Azure app registration). Some "
            "personal accounts still accept an app password."
        ),
    ),
    "yahoo": Provider(
        key="yahoo",
        name="Yahoo Mail",
        imap_host="imap.mail.yahoo.com",
        smtp_host="smtp.mail.yahoo.com",
        smtp_port=465,
        smtp_ssl=True,
        domains=("yahoo.com", "ymail.com", "rocketmail.com"),
        notes="Create an app password under Account Security > Generate app password.",
    ),
    "icloud": Provider(
        key="icloud",
        name="iCloud Mail",
        imap_host="imap.mail.me.com",
        smtp_host="smtp.mail.me.com",
        domains=("icloud.com", "me.com", "mac.com"),
        sent_folder="Sent Messages",
        notes="Create an app-specific password at https://appleid.apple.com (Sign-In and Security).",
    ),
    "zoho": Provider(
        key="zoho",
        name="Zoho Mail",
        imap_host="imap.zoho.com",
        smtp_host="smtp.zoho.com",
        smtp_port=465,
        smtp_ssl=True,
        domains=("zoho.com", "zohomail.com"),
        notes="Enable IMAP in Zoho Mail settings; use an application-specific password if 2FA is on.",
    ),
    "godaddy": Provider(
        key="godaddy",
        name="GoDaddy Workspace Email",
        imap_host="imap.secureserver.net",
        smtp_host="smtpout.secureserver.net",
        smtp_port=465,
        smtp_ssl=True,
        domains=("secureserver.net",),
        notes="Legacy GoDaddy Workspace mailboxes. Newer GoDaddy plans are Microsoft 365: use the outlook preset.",
    ),
    "generic": Provider(
        key="generic",
        name="Custom IMAP / SMTP",
        imap_host="",
        smtp_host="",
        notes="Supply imap_host / smtp_host explicitly (company domains, cPanel hosts, Fastmail, Proton Bridge...).",
    ),
}


def detect_provider(address: str) -> Provider:
    """Best-effort preset from the mailbox domain; ``generic`` when unknown."""
    domain = address.rsplit("@", 1)[-1].lower().strip()
    for prov in PROVIDERS.values():
        if domain in prov.domains:
            return prov
    return PROVIDERS["generic"]


def get_provider(key: str) -> Provider:
    try:
        return PROVIDERS[key]
    except KeyError:
        raise ValueError(f"unknown provider '{key}'; choose one of {', '.join(PROVIDERS)}") from None
