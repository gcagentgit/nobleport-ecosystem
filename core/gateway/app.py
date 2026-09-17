"""NoblePort MCP Gateway — FastAPI entrypoint.

    uvicorn core.gateway.app:app --host 0.0.0.0 --port 8080

The application is built by `create_app()`, which validates the security
configuration first (signing key, audience, issuer, CORS allowlist, admin
token) and refuses to boot if any of it is missing or unsafe. Every route
except the liveness/readiness probes requires a bearer token with the right
scope. See security.py.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import (APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, Request,
                     Response, UploadFile, status)

from . import metrics
from . import operational_truth
from . import security
from . import uploads
from .audit import AuditBeacon, CallLog
from .cache import Cache, RateLimiter
from .config import Settings, settings
from .envelope import InvokeResult, McpEnvelope
from .executors import HttpExecutor, StubExecutor
from .gateway import Gateway
from .governance import GovernanceGate
from .killswitch import GLOBAL, KillSwitch
from .kpi_worker import KpiWorker
from .security import (SCOPE_ADMIN, SCOPE_INVOKE, SCOPE_READ, SCOPE_UPLOAD, Principal,
                       require_scope)
from .supervisor import Supervisor

logging.basicConfig(level=settings.log_level)
log = logging.getLogger("nobleport.gateway.app")


@asynccontextmanager
async def default_lifespan(app: FastAPI):
    import asyncpg
    from redis.asyncio import Redis

    cfg: Settings = app.state.settings
    app.state.pg = await asyncpg.create_pool(cfg.database_url, min_size=1, max_size=10)
    app.state.redis = Redis.from_url(cfg.redis_url, decode_responses=True)

    gate = GovernanceGate(cfg.launch_gates_path, cfg.max_message_chars)
    killswitch = KillSwitch(app.state.redis, app.state.pg)
    cache = Cache(app.state.redis, cfg.cache_ttl_s)
    ratelimiter = RateLimiter(app.state.redis, cfg.rate_limit_per_min)
    beacon = AuditBeacon(app.state.pg)
    calllog = CallLog(app.state.pg)

    if cfg.use_http_executor:
        async with app.state.pg.acquire() as conn:
            rows = await conn.fetch("SELECT agent_name, endpoint FROM mcp_agent_registry")
        inner = HttpExecutor({r["agent_name"]: r["endpoint"] for r in rows}, cfg.tool_timeout_s)
    else:
        inner = StubExecutor()
    # Main agents delegate to sub-agents; compression is applied to the findings.
    executor = Supervisor(inner=inner)

    app.state.killswitch = killswitch
    app.state.beacon = beacon
    app.state.gateway = Gateway(
        pool=app.state.pg, gate=gate, killswitch=killswitch, cache=cache,
        ratelimiter=ratelimiter, beacon=beacon, calllog=calllog, executor=executor,
    )

    app.state.kpi = KpiWorker(app.state.pg, cfg.kpi_interval_s)
    await app.state.kpi.seed()
    if cfg.enable_kpi_worker:
        app.state.kpi.start()

    try:
        yield
    finally:
        if cfg.enable_kpi_worker:
            await app.state.kpi.stop()
        await app.state.pg.close()
        await app.state.redis.aclose()


def require_admin(request: Request,
                  x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    """Second factor for the kill switch: the shared ADMIN_TOKEN header, in
    addition to a bearer token carrying gateway:admin."""
    cfg: Settings = request.app.state.settings
    if not cfg.admin_token or x_admin_token != cfg.admin_token:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin_token_required")


router = APIRouter()
read_only = [Depends(require_scope(SCOPE_READ))]
admin_only = [Depends(require_scope(SCOPE_ADMIN)), Depends(require_admin)]


# --- Probes (unauthenticated: liveness / readiness only) --------------------
@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request):
    try:
        async with request.app.state.pg.acquire() as conn:
            await conn.fetchval("SELECT 1")
        await request.app.state.redis.ping()
    except Exception as exc:
        log.warning("readiness check failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="not_ready")
    return {"status": "ready"}


# --- Operational truth (authenticated read) ---------------------------------
@router.get("/health/features", dependencies=read_only)
async def health_features(request: Request):
    """Operational-truth feature matrix — honest status counts + frozen commands."""
    return operational_truth.report(request.app.state.settings.launch_gates_path)


@router.get("/health/frozen", dependencies=read_only)
async def health_frozen(request: Request):
    """Actions the system refuses to run autonomously."""
    frozen = operational_truth.load_frozen_commands(request.app.state.settings.launch_gates_path)
    return {"count": len(frozen), "commands": frozen}


# --- Core invocation -------------------------------------------------------
@router.post("/agent/invoke", response_model=InvokeResult)
async def invoke(env: McpEnvelope, request: Request,
                 principal: Principal = Depends(require_scope(SCOPE_INVOKE))) -> InvokeResult:
    # Sanitize before anything reads the text: invisible / bidi / homoglyph
    # tricks are removed so the governance claim screen sees the real words.
    try:
        env = security.sanitize_envelope(env)
    except security.SanitizeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"sanitize:{exc}")
    # A token may only act as the agent it was issued to.
    security.authorize_invoke(principal, env.requesting_agent)
    return await request.app.state.gateway.invoke(env)


# --- Evidence upload (authenticated, size-capped, MIME-allowlisted) ---------
@router.post("/agent/upload")
async def upload(request: Request,
                 file: UploadFile = File(...),
                 run_id: Optional[str] = Form(default=None),
                 principal: Principal = Depends(require_scope(SCOPE_UPLOAD))):
    cfg: Settings = request.app.state.settings
    if run_id is not None:
        run_id = security.sanitize_text(run_id)[:64]
    record = await uploads.store_upload(
        file, upload_dir=cfg.upload_dir, max_bytes=cfg.upload_max_bytes,
        allowed_mime=uploads.parse_mime_allowlist(cfg.upload_allowed_mime),
    )
    audit = await request.app.state.beacon.record_upload(
        principal.subject, principal.agent, record, run_id)
    return {**record, "run_id": run_id, "uploaded_by": principal.subject, "audit": audit}


# --- Dashboard API ---------------------------------------------------------
@router.get("/api/kpi/modules", dependencies=read_only)
async def kpi_modules(request: Request):
    async with request.app.state.pg.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.module_id, m.module_name, m.owner_agent, m.kpi_name,
                   m.source_table, m.truth_label, m.last_verified_at,
                   s.kpi_value, s.kpi_unit, s.source_ref, s.reason, s.measured_at
              FROM nobleport_module_registry m
              LEFT JOIN LATERAL (
                  SELECT kpi_value, kpi_unit, source_ref, reason, measured_at
                    FROM kpi_snapshot WHERE module_id = m.module_id
                ORDER BY measured_at DESC LIMIT 1
              ) s ON TRUE
             ORDER BY m.module_id
            """
        )
    return [dict(r) for r in rows]


@router.get("/api/kpi/module/{module_id}", dependencies=read_only)
async def kpi_module(module_id: int, request: Request):
    async with request.app.state.pg.acquire() as conn:
        mod = await conn.fetchrow(
            "SELECT * FROM nobleport_module_registry WHERE module_id=$1", module_id
        )
        if not mod:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="module_not_found")
        history = await conn.fetch(
            "SELECT kpi_value, kpi_unit, truth_label, source_ref, reason, measured_at "
            "FROM kpi_snapshot WHERE module_id=$1 ORDER BY measured_at DESC LIMIT 50",
            module_id,
        )
    return {"module": dict(mod), "history": [dict(h) for h in history]}


@router.get("/api/kpi/agent/{agent_name}", dependencies=read_only)
async def kpi_agent(agent_name: str, request: Request):
    async with request.app.state.pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT module_id, module_name, kpi_name, truth_label, last_verified_at "
            "FROM nobleport_module_registry WHERE owner_agent=$1 ORDER BY module_id",
            agent_name,
        )
    return [dict(r) for r in rows]


@router.get("/api/mcp/calls", dependencies=read_only)
async def mcp_calls(request: Request, limit: int = 100):
    async with request.app.state.pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT run_id, requesting_agent, target_agent, module_name, tool_name, "
            "truth_label, approval_level, status, latency_ms, error_message, created_at "
            "FROM mcp_call_log ORDER BY created_at DESC LIMIT $1",
            min(limit, 500),
        )
    return [dict(r) for r in rows]


@router.get("/api/audit/events", dependencies=read_only)
async def audit_events(request: Request, limit: int = 100):
    async with request.app.state.pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, entity_type, entity_id, payload_hash, previous_hash, timestamp "
            "FROM audit_logs ORDER BY timestamp DESC LIMIT $1",
            min(limit, 500),
        )
    return [dict(r) for r in rows]


# --- Metrics (G1 P95 export) ----------------------------------------------
@router.get("/api/metrics/p95", dependencies=read_only)
async def metrics_p95(request: Request, agent: str | None = None, window_hours: int = 24):
    return await metrics.p95_report(request.app.state.pg, agent=agent, window_hours=window_hours)


@router.get("/api/metrics/p95.csv", dependencies=read_only)
async def metrics_p95_csv(request: Request, window_hours: int = 24):
    body = await metrics.export_csv(request.app.state.pg, window_hours=window_hours)
    return Response(
        content=body, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=nobleport_p95.csv"},
    )


@router.get("/api/metrics/optimization", dependencies=read_only)
async def metrics_optimization(request: Request, window_hours: int = 24):
    """Realized compression savings + sub-agent delegation, last N hours."""
    async with request.app.state.pg.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE bytes_raw IS NOT NULL) AS measured_calls,
                   COALESCE(SUM(bytes_raw), 0)    AS bytes_raw,
                   COALESCE(SUM(bytes_packed), 0) AS bytes_packed,
                   COALESCE(SUM(subagent_count), 0) AS subagent_dispatches,
                   COALESCE(AVG(subagent_count), 0) AS avg_subagents_per_call
              FROM mcp_call_log
             WHERE created_at >= NOW() - ($1 || ' hours')::interval
            """,
            str(window_hours),
        )
    raw = row["bytes_raw"] or 0
    packed = row["bytes_packed"] or 0
    return {
        "window_hours": window_hours,
        "measured_calls": row["measured_calls"],
        "bytes_raw": raw,
        "bytes_packed": packed,
        "saved_bytes": max(raw - packed, 0),
        "compression_ratio": round(packed / raw, 4) if raw else 1.0,
        "subagent_dispatches": row["subagent_dispatches"],
        "avg_subagents_per_call": round(float(row["avg_subagents_per_call"]), 2),
    }


# --- Kill switch (admin: bearer gateway:admin AND X-Admin-Token) ------------
@router.get("/api/killswitch/status", dependencies=read_only)
async def killswitch_status(request: Request):
    return await request.app.state.killswitch.status()


@router.post("/api/killswitch/engage", dependencies=admin_only)
async def killswitch_engage(request: Request, scope: str = GLOBAL, reason: str = "",
                            principal: Principal = Depends(security.authenticate)):
    await request.app.state.killswitch.engage(scope, actor=principal.subject, reason=reason)
    return {"scope": scope, "engaged": True}


@router.post("/api/killswitch/release", dependencies=admin_only)
async def killswitch_release(request: Request, scope: str = GLOBAL, reason: str = "",
                             principal: Principal = Depends(security.authenticate)):
    await request.app.state.killswitch.release(scope, actor=principal.subject, reason=reason)
    return {"scope": scope, "engaged": False}


# --- Factory ---------------------------------------------------------------
def create_app(cfg: Settings | None = None, *, lifespan=None) -> FastAPI:
    """Build the application. Raises SecurityConfigError (boot refused) when
    the security configuration is missing or unsafe."""
    cfg = cfg or settings
    summary = security.validate(cfg)
    app = FastAPI(title="NoblePort MCP Gateway", version="1.1.0",
                  lifespan=lifespan or default_lifespan)
    app.state.settings = cfg
    app.state.verifier = security.TokenVerifier.from_settings(cfg)
    security.install_cors(app, cfg)
    app.include_router(router)
    log.info("security configuration OK: %s", summary)
    return app


# `uvicorn core.gateway.app:app` resolves the attribute lazily (PEP 562), so
# importing this module has no side effects, while booting the server still
# runs the full security validation and refuses to start when it fails.
_app: FastAPI | None = None


def __getattr__(name: str):
    global _app
    if name == "app":
        if _app is None:
            try:
                _app = create_app()
            except security.SecurityConfigError as exc:
                log.critical("REFUSING TO START — %s", exc)
                raise
        return _app
    raise AttributeError(name)
