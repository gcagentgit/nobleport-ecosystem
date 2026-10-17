"""FastAPI surface: dashboard, mailboxes, urgent inbox, replies, brief, notifications, cleanup.

    steph-email serve            # dashboard + API on http://127.0.0.1:8030
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date as Date
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, EmailStr, Field

from . import __version__
from .config import settings
from .notify import DestinationError, NotifyError, VoiceError
from .providers import PROVIDERS, detect_provider
from .service import EmailEngine
from .urgency import LEVELS

log = logging.getLogger("steph_email.api")
logging.basicConfig(level=settings.log_level)

WEB_DIR = Path(__file__).parent / "web"


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
    expect_reply: bool = True
    due_days: int | None = None


class FlagIn(BaseModel):
    seen: bool | None = None
    flagged: bool | None = None
    push: bool = True


class TagIn(BaseModel):
    tags: list[str] = Field(min_length=1)


class BriefRunIn(BaseModel):
    date: Date | None = None
    deliver: bool = False
    voice: bool | None = None
    cleanup: bool | None = None


class SmsTestIn(BaseModel):
    body: str = "Steph Email Engine test message."


class VoiceVerifyIn(BaseModel):
    confirm_playback: bool = False
    device: str = ""
    note: str = ""
    sample_text: str = "Good morning, Michael. This is Stephanie. Your email brief is ready."


class CleanupRunIn(BaseModel):
    copy_to_server: bool | None = None


def build_app(engine: EmailEngine | None = None, *, background: bool | None = None) -> FastAPI:
    run_bg = settings.sync_interval_s > 0 if background is None else background

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = engine or EmailEngine(settings)
        app.state.lock = asyncio.Lock()
        task = asyncio.create_task(_scheduler(app)) if run_bg else None
        try:
            yield
        finally:
            if task:
                task.cancel()
            app.state.engine.db.close()

    app = FastAPI(title="Steph Email Engine", version=__version__, lifespan=lifespan)

    def get_engine(request: Request) -> EmailEngine:
        return request.app.state.engine

    def require_token(x_api_token: str | None = Header(default=None, alias="X-API-Token")):
        if settings.api_token and x_api_token != settings.api_token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="api_token_required")

    auth = [Depends(require_token)]

    # ---------------------------------------------------------------- basics
    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index():
        return (WEB_DIR / "index.html").read_text()

    @app.get("/api/status", dependencies=auth)
    def status_(eng: EmailEngine = Depends(get_engine)):
        return eng.status()

    @app.get("/api/overview", dependencies=auth)
    def overview(eng: EmailEngine = Depends(get_engine)):
        ov = eng.db.overview()
        ov["replies_summary"] = eng.replies.summary()
        return ov

    @app.get("/api/providers")
    def providers():
        return {k: {"name": p.name, "imap_host": p.imap_host, "smtp_host": p.smtp_host,
                    "auth_methods": list(p.auth_methods), "notes": p.notes} for k, p in PROVIDERS.items()}

    @app.get("/api/providers/detect")
    def providers_detect(address: str):
        p = detect_provider(address)
        return {"provider": p.key, "name": p.name, "auth_methods": list(p.auth_methods), "notes": p.notes}

    # ---------------------------------------------------------------- accounts
    @app.get("/api/accounts", dependencies=auth)
    def list_accounts(eng: EmailEngine = Depends(get_engine)):
        return [eng.describe_account(a) for a in eng.db.list_accounts()]

    @app.post("/api/accounts", dependencies=auth, status_code=201)
    async def add_account(body: AccountIn, eng: EmailEngine = Depends(get_engine)):
        try:
            return await asyncio.to_thread(eng.add_account, str(body.address), body.secret,
                                           **body.model_dump(exclude={"address", "secret"}))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"connection test failed: {exc}")

    @app.post("/api/accounts/{ident}/test", dependencies=auth)
    async def test_account(ident: str, eng: EmailEngine = Depends(get_engine)):
        acct = eng.db.get_account(ident)
        if not acct:
            raise HTTPException(404, "account not found")
        try:
            return await asyncio.to_thread(eng.test_account, acct)
        except Exception as exc:
            eng.db.update_account(acct["id"], last_error=str(exc)[:500])
            raise HTTPException(502, f"connection failed: {exc}")

    @app.delete("/api/accounts/{ident}", dependencies=auth)
    def delete_account(ident: str, eng: EmailEngine = Depends(get_engine)):
        if not eng.remove_account(ident):
            raise HTTPException(404, "account not found")
        return {"removed": ident}

    @app.post("/api/accounts/{ident}/enabled", dependencies=auth)
    def set_enabled(ident: str, enabled: bool, eng: EmailEngine = Depends(get_engine)):
        acct = eng.db.get_account(ident)
        if not acct:
            raise HTTPException(404, "account not found")
        eng.db.update_account(acct["id"], enabled=int(enabled))
        return eng.describe_account(eng.db.get_account(acct["id"]))

    # ---------------------------------------------------------------- sync
    @app.post("/api/sync", dependencies=auth)
    async def sync(request: Request, account: str | None = None, eng: EmailEngine = Depends(get_engine)):
        async with request.app.state.lock:
            if account:
                if not eng.db.get_account(account):
                    raise HTTPException(404, "account not found")
                reports = await asyncio.to_thread(eng.sync_account, account)
            else:
                reports = await asyncio.to_thread(eng.sync_all)
        return [r.as_dict() for r in reports]

    # ---------------------------------------------------------------- messages
    @app.get("/api/messages", dependencies=auth)
    def messages(eng: EmailEngine = Depends(get_engine), account: str | None = None, folder: str | None = None,
                 unread: bool = False, flagged: bool = False, tag: str | None = None, q: str | None = None,
                 min_urgency: str | None = Query(None, pattern="^(low|normal|high|critical)$"),
                 needs_reply: bool | None = None, archived: bool = False,
                 limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        account_id = None
        if account:
            acct = eng.db.get_account(account)
            if not acct:
                raise HTTPException(404, "account not found")
            account_id = acct["id"]
        return eng.db.list_messages(account_id=account_id, folder=folder, unread_only=unread, flagged_only=flagged,
                                    tag=tag, query=q, min_urgency=min_urgency, needs_reply=needs_reply,
                                    archived_only=archived, limit=limit, offset=offset)

    @app.get("/api/urgent", dependencies=auth)
    def urgent(eng: EmailEngine = Depends(get_engine), min_urgency: str = Query("high", pattern="^(low|normal|high|critical)$"),
               unread: bool = True, limit: int = Query(50, ge=1, le=500)):
        return {"min_urgency": min_urgency, "levels": list(LEVELS),
                "messages": eng.db.list_messages(min_urgency=min_urgency, unread_only=unread, limit=limit)}

    @app.get("/api/messages/{message_id}", dependencies=auth)
    def message(message_id: int, eng: EmailEngine = Depends(get_engine)):
        msg = eng.db.get_message(message_id)
        if not msg:
            raise HTTPException(404, "message not found")
        return msg

    @app.get("/api/messages/{message_id}/thread", dependencies=auth)
    def thread(message_id: int, eng: EmailEngine = Depends(get_engine)):
        msg = eng.db.get_message(message_id)
        if not msg:
            raise HTTPException(404, "message not found")
        return eng.db.thread(msg["thread_key"])

    @app.post("/api/messages/{message_id}/flags", dependencies=auth)
    async def flags(message_id: int, body: FlagIn, eng: EmailEngine = Depends(get_engine)):
        try:
            return await asyncio.to_thread(eng.mark, message_id, seen=body.seen, flagged=body.flagged, push=body.push)
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    @app.post("/api/messages/{message_id}/tags", dependencies=auth)
    def add_tags(message_id: int, body: TagIn, eng: EmailEngine = Depends(get_engine)):
        if not eng.db.get_message(message_id):
            raise HTTPException(404, "message not found")
        eng.db.add_tags(message_id, body.tags)
        return {"id": message_id, "tags": eng.db.tags_for(message_id)}

    @app.delete("/api/messages/{message_id}/tags/{tag}", dependencies=auth)
    def remove_tag(message_id: int, tag: str, eng: EmailEngine = Depends(get_engine)):
        eng.db.remove_tag(message_id, tag)
        return {"id": message_id, "tags": eng.db.tags_for(message_id)}

    @app.post("/api/messages/{message_id}/restore", dependencies=auth)
    def restore(message_id: int, eng: EmailEngine = Depends(get_engine)):
        try:
            return eng.cleaner.restore(message_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    @app.post("/api/send", dependencies=auth)
    async def send(body: SendIn, eng: EmailEngine = Depends(get_engine)):
        try:
            return await asyncio.to_thread(
                eng.send, body.account, [str(a) for a in body.to], body.subject, body.text, html=body.html,
                cc=[str(a) for a in body.cc] or None, bcc=[str(a) for a in body.bcc] or None,
                reply_to_message_id=body.reply_to_message_id, expect_reply=body.expect_reply, due_days=body.due_days)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"send failed: {exc}")

    # ---------------------------------------------------------------- replies
    @app.get("/api/replies", dependencies=auth)
    def replies(eng: EmailEngine = Depends(get_engine), overdue: bool = False):
        return {"waiting_on_them": eng.replies.waiting(overdue_only=overdue),
                "needs_my_reply": eng.replies.needs_my_reply(), "summary": eng.replies.summary()}

    @app.post("/api/replies/reconcile", dependencies=auth)
    def reconcile(eng: EmailEngine = Depends(get_engine)):
        return eng.replies.reconcile()

    @app.post("/api/replies/{rid}/close", dependencies=auth)
    def close_reply(rid: int, eng: EmailEngine = Depends(get_engine), status_: str = Query("closed", alias="status")):
        try:
            return eng.replies.close(rid, status_)
        except ValueError as exc:
            raise HTTPException(404 if "no expected" in str(exc) else 400, str(exc))

    @app.post("/api/replies/{rid}/nudge", dependencies=auth)
    def nudge(rid: int, eng: EmailEngine = Depends(get_engine)):
        try:
            return eng.replies.nudge(rid)
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    # ---------------------------------------------------------------- brief
    @app.get("/api/brief", dependencies=auth)
    def brief_latest(eng: EmailEngine = Depends(get_engine)):
        return eng.db.latest_brief() or {}

    @app.get("/api/brief/preview", dependencies=auth)
    def brief_preview(eng: EmailEngine = Depends(get_engine)):
        return eng.build_brief().as_dict()

    @app.get("/api/briefs", dependencies=auth)
    def briefs(eng: EmailEngine = Depends(get_engine)):
        return eng.db.list_briefs()

    @app.post("/api/brief/run", dependencies=auth)
    async def brief_run(body: BriefRunIn, eng: EmailEngine = Depends(get_engine)):
        return await asyncio.to_thread(eng.run_brief, body.date, deliver=body.deliver, voice=body.voice, cleanup=body.cleanup)

    @app.get("/api/brief/{brief_date}", dependencies=auth)
    def brief_by_date(brief_date: str, eng: EmailEngine = Depends(get_engine)):
        b = eng.db.get_brief(brief_date)
        if not b:
            raise HTTPException(404, "no brief for that date")
        return b

    @app.get("/audio/brief-{brief_date}.mp3", include_in_schema=False)
    def brief_audio(brief_date: str, eng: EmailEngine = Depends(get_engine)):
        b = eng.db.get_brief(brief_date)
        if not b or not b["audio_path"] or not Path(b["audio_path"]).exists():
            raise HTTPException(404, "no audio for that brief")
        return FileResponse(b["audio_path"], media_type="audio/mpeg")

    @app.api_route("/webhooks/twilio/brief/{brief_date}", methods=["GET", "POST"], include_in_schema=False)
    def twilio_brief(brief_date: str, eng: EmailEngine = Depends(get_engine)):
        b = eng.db.get_brief(brief_date)
        if not b:
            raise HTTPException(404, "no brief for that date")
        audio_url = f"{settings.public_base_url.rstrip('/')}/audio/brief-{brief_date}.mp3" if b["audio_path"] else None
        return Response(eng.notifier.twiml_for_brief(b["script"], audio_url), media_type="application/xml")

    # ---------------------------------------------------------------- notifications
    @app.get("/api/notifications", dependencies=auth)
    def notifications(eng: EmailEngine = Depends(get_engine), limit: int = Query(50, ge=1, le=500)):
        return {"transport": eng.notifier.transport.name, "truth_label": eng.notifier.truth_label,
                "items": eng.db.list_notifications(limit)}

    @app.post("/api/notify/test-sms", dependencies=auth)
    def test_sms(body: SmsTestIn, eng: EmailEngine = Depends(get_engine)):
        try:
            return eng.notifier.send_sms(body.body, purpose="test")
        except (DestinationError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        except NotifyError as exc:
            raise HTTPException(502, str(exc))

    @app.post("/api/voice/verify", dependencies=auth)
    async def voice_verify(body: VoiceVerifyIn, eng: EmailEngine = Depends(get_engine)):
        return await asyncio.to_thread(eng.notifier.verify_voice, sample_text=body.sample_text,
                                       confirm_playback=body.confirm_playback, device=body.device, note=body.note)

    @app.get("/api/voice/evidence", dependencies=auth)
    def voice_evidence(eng: EmailEngine = Depends(get_engine)):
        return eng.notifier.load_evidence()

    # ---------------------------------------------------------------- cleanup
    @app.get("/api/cleanup/plan", dependencies=auth)
    def cleanup_plan(eng: EmailEngine = Depends(get_engine)):
        return eng.cleanup(dry_run=True)

    @app.post("/api/cleanup/run", dependencies=auth)
    async def cleanup_run(body: CleanupRunIn, eng: EmailEngine = Depends(get_engine)):
        return await asyncio.to_thread(eng.cleanup, dry_run=False, copy_to_server=body.copy_to_server)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app


async def _scheduler(app: FastAPI) -> None:
    """Background loop: sync on the interval, run the brief once it is due."""
    eng: EmailEngine = app.state.engine
    while True:
        try:
            async with app.state.lock:
                reports = await asyncio.to_thread(eng.sync_all)
            fetched = sum(r.fetched for r in reports)
            if fetched:
                log.info("background sync fetched %d messages", fetched)
            if eng.brief_due():
                async with app.state.lock:
                    result = await asyncio.to_thread(eng.run_brief, None, deliver=True)
                log.info("morning brief %s delivered: %s", result["brief_date"], result["delivery"])
        except Exception:
            log.exception("scheduler pass failed")
        await asyncio.sleep(settings.sync_interval_s)


app = build_app()
