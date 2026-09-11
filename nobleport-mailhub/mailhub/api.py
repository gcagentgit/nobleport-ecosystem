"""FastAPI surface: unified inbox, accounts, sync, send, search.

    uvicorn mailhub.api:app --host 127.0.0.1 --port 8025
    # or:  mailhub serve
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, EmailStr, Field

from . import __version__
from .config import settings
from .providers import PROVIDERS, detect_provider
from .service import MailHub

log = logging.getLogger("mailhub.api")
logging.basicConfig(level=settings.log_level)

WEB_DIR = Path(__file__).parent / "web"


# ------------------------------------------------------------------ models
class AccountIn(BaseModel):
    address: EmailStr
    secret: str = Field(min_length=1, description="app password, or OAuth2 refresh token")
    provider: str | None = None
    display_name: str = ""
    username: str | None = None
    auth_method: str = "password"
    imap_host: str | None = None
    imap_port: int | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_ssl: bool | None = None
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_token_url: str = ""
    sent_folder: str | None = None
    test: bool = True


class SendIn(BaseModel):
    account: str
    to: list[EmailStr] = Field(min_length=1)
    subject: str = ""
    text: str = ""
    html: str | None = None
    cc: list[EmailStr] = []
    bcc: list[EmailStr] = []
    reply_to_message_id: int | None = None


class FlagIn(BaseModel):
    seen: bool | None = None
    flagged: bool | None = None
    push: bool = True


class TagIn(BaseModel):
    tags: list[str] = Field(min_length=1)


def build_app(hub: MailHub | None = None, *, background_sync: bool | None = None) -> FastAPI:
    run_sync = settings.sync_interval_s > 0 if background_sync is None else background_sync

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.hub = hub or MailHub(settings)
        app.state.sync_lock = asyncio.Lock()
        task = asyncio.create_task(_sync_loop(app)) if run_sync else None
        try:
            yield
        finally:
            if task:
                task.cancel()
            app.state.hub.db.close()

    app = FastAPI(title="NoblePort MailHub", version=__version__, lifespan=lifespan)

    def get_hub(request: Request) -> MailHub:
        return request.app.state.hub

    def require_token(x_api_token: str | None = Header(default=None, alias="X-API-Token")):
        if settings.api_token and x_api_token != settings.api_token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="api_token_required")

    auth = [Depends(require_token)]

    # ---------------------------------------------------------------- routes
    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index():
        return (WEB_DIR / "index.html").read_text()

    @app.get("/api/providers")
    def providers():
        return {k: {"name": p.name, "imap_host": p.imap_host, "smtp_host": p.smtp_host,
                    "auth_methods": list(p.auth_methods), "notes": p.notes} for k, p in PROVIDERS.items()}

    @app.get("/api/providers/detect")
    def providers_detect(address: str):
        p = detect_provider(address)
        return {"provider": p.key, "name": p.name, "auth_methods": list(p.auth_methods), "notes": p.notes}

    @app.get("/api/overview", dependencies=auth)
    def overview(hub: MailHub = Depends(get_hub)):
        return hub.db.overview()

    @app.get("/api/accounts", dependencies=auth)
    def list_accounts(hub: MailHub = Depends(get_hub)):
        return [hub.describe_account(a) for a in hub.db.list_accounts()]

    @app.post("/api/accounts", dependencies=auth, status_code=201)
    async def add_account(body: AccountIn, hub: MailHub = Depends(get_hub)):
        try:
            return await asyncio.to_thread(hub.add_account, str(body.address), body.secret, **body.model_dump(exclude={"address", "secret"}))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"connection test failed: {exc}")

    @app.post("/api/accounts/{ident}/test", dependencies=auth)
    async def test_account(ident: str, hub: MailHub = Depends(get_hub)):
        acct = hub.db.get_account(ident)
        if not acct:
            raise HTTPException(404, "account not found")
        try:
            return await asyncio.to_thread(hub.test_account, acct)
        except Exception as exc:
            hub.db.update_account(acct["id"], last_error=str(exc)[:500])
            raise HTTPException(502, f"connection failed: {exc}")

    @app.delete("/api/accounts/{ident}", dependencies=auth)
    def delete_account(ident: str, hub: MailHub = Depends(get_hub)):
        if not hub.remove_account(ident):
            raise HTTPException(404, "account not found")
        return {"removed": ident}

    @app.post("/api/accounts/{ident}/enabled", dependencies=auth)
    def set_enabled(ident: str, enabled: bool, hub: MailHub = Depends(get_hub)):
        acct = hub.db.get_account(ident)
        if not acct:
            raise HTTPException(404, "account not found")
        hub.db.update_account(acct["id"], enabled=int(enabled))
        return hub.describe_account(hub.db.get_account(acct["id"]))

    @app.post("/api/sync", dependencies=auth)
    async def sync(request: Request, account: str | None = None, hub: MailHub = Depends(get_hub)):
        async with request.app.state.sync_lock:
            if account:
                if not hub.db.get_account(account):
                    raise HTTPException(404, "account not found")
                reports = await asyncio.to_thread(hub.sync_account, account)
            else:
                reports = await asyncio.to_thread(hub.sync_all)
        return [r.__dict__ for r in reports]

    @app.get("/api/messages", dependencies=auth)
    def messages(
        hub: MailHub = Depends(get_hub),
        account: str | None = None,
        folder: str | None = None,
        unread: bool = False,
        flagged: bool = False,
        tag: str | None = None,
        q: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        account_id = None
        if account:
            acct = hub.db.get_account(account)
            if not acct:
                raise HTTPException(404, "account not found")
            account_id = acct["id"]
        return hub.db.list_messages(account_id=account_id, folder=folder, unread_only=unread, flagged_only=flagged,
                                    tag=tag, query=q, limit=limit, offset=offset)

    @app.get("/api/messages/{message_id}", dependencies=auth)
    def message(message_id: int, hub: MailHub = Depends(get_hub)):
        msg = hub.db.get_message(message_id)
        if not msg:
            raise HTTPException(404, "message not found")
        return msg

    @app.get("/api/messages/{message_id}/thread", dependencies=auth)
    def thread(message_id: int, hub: MailHub = Depends(get_hub)):
        msg = hub.db.get_message(message_id)
        if not msg:
            raise HTTPException(404, "message not found")
        return hub.db.thread(msg["thread_key"])

    @app.post("/api/messages/{message_id}/flags", dependencies=auth)
    async def flags(message_id: int, body: FlagIn, hub: MailHub = Depends(get_hub)):
        try:
            return await asyncio.to_thread(hub.mark, message_id, seen=body.seen, flagged=body.flagged, push=body.push)
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    @app.post("/api/messages/{message_id}/tags", dependencies=auth)
    def add_tags(message_id: int, body: TagIn, hub: MailHub = Depends(get_hub)):
        if not hub.db.get_message(message_id):
            raise HTTPException(404, "message not found")
        hub.db.add_tags(message_id, body.tags)
        return {"id": message_id, "tags": hub.db.tags_for(message_id)}

    @app.delete("/api/messages/{message_id}/tags/{tag}", dependencies=auth)
    def remove_tag(message_id: int, tag: str, hub: MailHub = Depends(get_hub)):
        hub.db.remove_tag(message_id, tag)
        return {"id": message_id, "tags": hub.db.tags_for(message_id)}

    @app.post("/api/send", dependencies=auth)
    async def send(body: SendIn, hub: MailHub = Depends(get_hub)):
        try:
            return await asyncio.to_thread(
                hub.send, body.account, [str(a) for a in body.to], body.subject, body.text,
                html=body.html, cc=[str(a) for a in body.cc] or None, bcc=[str(a) for a in body.bcc] or None,
                reply_to_message_id=body.reply_to_message_id,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"send failed: {exc}")

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app


async def _sync_loop(app: FastAPI) -> None:
    hub: MailHub = app.state.hub
    while True:
        try:
            async with app.state.sync_lock:
                reports = await asyncio.to_thread(hub.sync_all)
            fetched = sum(r.fetched for r in reports)
            if fetched:
                log.info("background sync fetched %d messages", fetched)
        except Exception:
            log.exception("background sync failed")
        await asyncio.sleep(settings.sync_interval_s)


app = build_app()
