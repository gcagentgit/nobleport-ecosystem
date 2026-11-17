"""HTTP-level denial / authorization tests.

Every request goes through the real ASGI app: CORS middleware, bearer auth,
scope check, sanitizer, governance, audit chain and executor — only Postgres
and Redis are in-memory fakes.
"""

import io
import time

import pytest

from core.gateway.security import (SCOPE_ADMIN, SCOPE_INVOKE, SCOPE_READ, SCOPE_UPLOAD,
                                   mint_token)

from .fakes import TEST_ORIGIN, client_for, make_app, make_settings, strong_secret

pytestmark = pytest.mark.asyncio


def bearer(cfg, *, agent="Stephanie.ai", scopes=(SCOPE_INVOKE,), subject="stephanie", **kw):
    base = dict(key=cfg.jwt_secret, algorithm="HS256", audience=cfg.jwt_audience,
                issuer=cfg.jwt_issuer, subject=subject, agent=agent, scopes=list(scopes))
    base.update(kw)
    return {"Authorization": f"Bearer {mint_token(**base)}"}


def envelope(**kw):
    base = dict(requesting_agent="Stephanie.ai", target_agent="GCagent.ai",
                module="Estimate Engine", action="gcagent.price_estimate",
                message="Explain contractor deposits")
    base.update(kw)
    return base


@pytest.fixture
def cfg():
    return make_settings()


@pytest.fixture
async def client(cfg):
    async with client_for(make_app(cfg)) as c:
        yield c


# --- Probes stay public; everything else is authenticated -----------------
async def test_probes_are_public(client):
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/ready")).status_code == 200


async def test_every_other_route_requires_bearer(client):
    for method, path in [
        ("GET", "/health/features"), ("GET", "/health/frozen"),
        ("POST", "/agent/invoke"), ("POST", "/agent/upload"),
        ("GET", "/api/kpi/modules"), ("GET", "/api/kpi/module/1"),
        ("GET", "/api/kpi/agent/GCagent.ai"), ("GET", "/api/mcp/calls"),
        ("GET", "/api/audit/events"), ("GET", "/api/metrics/p95"),
        ("GET", "/api/metrics/p95.csv"), ("GET", "/api/metrics/optimization"),
        ("GET", "/api/killswitch/status"), ("POST", "/api/killswitch/engage"),
        ("POST", "/api/killswitch/release"),
    ]:
        r = await client.request(method, path, json=envelope() if path == "/agent/invoke" else None)
        assert r.status_code == 401, (method, path, r.status_code)
        assert r.headers.get("www-authenticate", "").startswith("Bearer"), (method, path)


# --- Invoke: denial matrix ------------------------------------------------
async def test_invoke_without_token_401(client):
    r = await client.post("/agent/invoke", json=envelope())
    assert r.status_code == 401 and r.json()["detail"] == "bearer_token_required"


async def test_invoke_with_basic_auth_401(client):
    r = await client.post("/agent/invoke", json=envelope(), headers={"Authorization": "Basic abc"})
    assert r.status_code == 401


async def test_invoke_forged_signature_401(client, cfg):
    r = await client.post("/agent/invoke", json=envelope(), headers=bearer(cfg, key=strong_secret()))
    assert r.status_code == 401 and r.json()["detail"] == "invalid_signature"


async def test_invoke_wrong_audience_401(client, cfg):
    r = await client.post("/agent/invoke", json=envelope(), headers=bearer(cfg, audience="other-api"))
    assert r.status_code == 401 and r.json()["detail"] == "invalid_audience"


async def test_invoke_wrong_issuer_401(client, cfg):
    r = await client.post("/agent/invoke", json=envelope(),
                          headers=bearer(cfg, issuer="https://evil.example"))
    assert r.status_code == 401 and r.json()["detail"] == "invalid_issuer"


async def test_invoke_expired_401(client, cfg):
    r = await client.post("/agent/invoke", json=envelope(),
                          headers=bearer(cfg, issued_at=int(time.time()) - 7200, ttl_s=3600))
    assert r.status_code == 401 and r.json()["detail"] == "token_expired"


async def test_invoke_wrong_scope_403(client, cfg):
    r = await client.post("/agent/invoke", json=envelope(), headers=bearer(cfg, scopes=[SCOPE_READ]))
    assert r.status_code == 403 and r.json()["detail"] == f"insufficient_scope:{SCOPE_INVOKE}"


async def test_invoke_agent_mismatch_403(client, cfg):
    # Token issued to GCagent.ai tries to invoke as Stephanie.ai.
    r = await client.post("/agent/invoke", json=envelope(), headers=bearer(cfg, agent="GCagent.ai"))
    assert r.status_code == 403 and r.json()["detail"].startswith("requesting_agent_mismatch")


async def test_invoke_without_agent_claim_403(client, cfg):
    r = await client.post("/agent/invoke", json=envelope(), headers=bearer(cfg, agent=None))
    assert r.status_code == 403 and r.json()["detail"] == "agent_claim_required"


async def test_denied_calls_never_reach_the_executor_or_audit(client, cfg):
    store = client._transport.app.state.store
    await client.post("/agent/invoke", json=envelope(), headers=bearer(cfg, scopes=[SCOPE_READ]))
    await client.post("/agent/invoke", json=envelope())
    assert store["calls"] == [] and store["audit"] == []


# --- Invoke: permitted path -----------------------------------------------
async def test_invoke_valid_token_succeeds_and_is_audited(client, cfg):
    r = await client.post("/agent/invoke", json=envelope(), headers=bearer(cfg))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "SUCCESS" and body["decision"] == "PERMIT"
    store = client._transport.app.state.store
    assert [a["action"] for a in store["audit"]] == ["MCP_AUDIT_PREWRITE", "MCP_AUDIT_POSTWRITE"]
    assert store["audit"][1]["previous_hash"] == store["audit"][0]["payload_hash"]
    assert store["calls"][-1]["status"] == "SUCCESS"


async def test_governance_still_blocks_prohibited_claims_for_valid_tokens(client, cfg):
    r = await client.post("/agent/invoke", headers=bearer(cfg),
                          json=envelope(message="guaranteed returns on real estate"))
    assert r.status_code == 200 and r.json()["status"] == "BLOCKED"
    assert r.json()["reason"] == "prohibited_claim:guaranteed returns"


async def test_zero_width_evasion_is_blocked_after_sanitizing(client, cfg):
    r = await client.post("/agent/invoke", headers=bearer(cfg),
                          json=envelope(message="guar​anteed​ returns on real estate"))
    assert r.status_code == 200 and r.json()["status"] == "BLOCKED"


async def test_fullwidth_evasion_is_blocked_after_sanitizing(client, cfg):
    r = await client.post("/agent/invoke", headers=bearer(cfg),
                          json=envelope(message="ｇｕａｒａｎｔｅｅｄ ｒｅｔｕｒｎｓ"))
    assert r.status_code == 200 and r.json()["status"] == "BLOCKED"


async def test_oversized_payload_structure_400(client, cfg):
    r = await client.post("/agent/invoke", headers=bearer(cfg),
                          json=envelope(payload={str(i): i for i in range(600)}))
    assert r.status_code == 400 and r.json()["detail"].startswith("sanitize:")


async def test_rate_limit_denies_after_threshold():
    cfg = make_settings(rate_limit_per_min=3)
    async with client_for(make_app(cfg)) as c:
        statuses = []
        for i in range(5):
            r = await c.post("/agent/invoke", headers=bearer(cfg), json=envelope(message=f"call {i}"))
            statuses.append(r.json()["status"])
        assert statuses == ["SUCCESS", "SUCCESS", "SUCCESS", "BLOCKED", "BLOCKED"]


# --- Upload: authenticated, scoped, capped ---------------------------------
def png(n=32):
    return ("evidence.png", io.BytesIO(b"\x89PNG" + b"\x00" * n), "image/png")


async def test_upload_without_token_401(client):
    r = await client.post("/agent/upload", files={"file": png()})
    assert r.status_code == 401


async def test_upload_wrong_scope_403(client, cfg):
    r = await client.post("/agent/upload", files={"file": png()}, headers=bearer(cfg))
    assert r.status_code == 403 and r.json()["detail"] == f"insufficient_scope:{SCOPE_UPLOAD}"


async def test_upload_with_scope_stores_hashes_and_audits(client, cfg):
    r = await client.post("/agent/upload", files={"file": png()}, data={"run_id": "run-1"},
                          headers=bearer(cfg, scopes=[SCOPE_UPLOAD]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bytes"] == 36 and len(body["sha256"]) == 64 and body["uploaded_by"] == "stephanie"
    store = client._transport.app.state.store
    assert store["audit"][-1]["action"] == "MCP_UPLOAD"


async def test_upload_disallowed_mime_415(client, cfg):
    r = await client.post("/agent/upload", headers=bearer(cfg, scopes=[SCOPE_UPLOAD]),
                          files={"file": ("x.exe", io.BytesIO(b"MZ" * 10), "application/octet-stream")})
    assert r.status_code == 415


async def test_upload_too_large_413(client, cfg):
    big = ("big.png", io.BytesIO(b"\x00" * (cfg.upload_max_bytes + 1)), "image/png")
    r = await client.post("/agent/upload", files={"file": big}, headers=bearer(cfg, scopes=[SCOPE_UPLOAD]))
    assert r.status_code == 413


async def test_upload_filename_is_sanitized(client, cfg):
    r = await client.post("/agent/upload", headers=bearer(cfg, scopes=[SCOPE_UPLOAD]),
                          files={"file": ("../../etc/passwd​.png", io.BytesIO(b"x" * 8), "image/png")})
    assert r.status_code == 200 and r.json()["filename"] == "passwd_.png"


# --- Admin: bearer gateway:admin AND X-Admin-Token --------------------------
async def test_killswitch_needs_admin_scope_and_header(client, cfg):
    hdr = {"X-Admin-Token": cfg.admin_token}
    assert (await client.post("/api/killswitch/engage", headers=hdr)).status_code == 401
    r = await client.post("/api/killswitch/engage", headers={**bearer(cfg, scopes=[SCOPE_READ]), **hdr})
    assert r.status_code == 403 and r.json()["detail"].startswith("insufficient_scope")
    r = await client.post("/api/killswitch/engage", headers=bearer(cfg, scopes=[SCOPE_ADMIN]))
    assert r.status_code == 403 and r.json()["detail"] == "admin_token_required"
    r = await client.post("/api/killswitch/engage",
                          headers={**bearer(cfg, scopes=[SCOPE_ADMIN]), "X-Admin-Token": "wrong"})
    assert r.status_code == 403


async def test_killswitch_engage_blocks_invoke_then_release(client, cfg):
    admin = {**bearer(cfg, scopes=[SCOPE_ADMIN], subject="ops-admin"), "X-Admin-Token": cfg.admin_token}
    assert (await client.post("/api/killswitch/engage", params={"reason": "drill"}, headers=admin)).status_code == 200
    r = await client.post("/agent/invoke", json=envelope(), headers=bearer(cfg))
    assert r.json()["status"] == "BLOCKED" and r.json()["reason"] == "kill_switch_engaged:global"
    store = client._transport.app.state.store
    assert store["killswitch_events"][-1]["actor"] == "ops-admin"
    assert (await client.post("/api/killswitch/release", headers=admin)).status_code == 200
    r = await client.post("/agent/invoke", json=envelope(message="after release"), headers=bearer(cfg))
    assert r.json()["status"] == "SUCCESS"


# --- CORS ------------------------------------------------------------------
async def test_cors_allowed_origin_gets_headers(client):
    r = await client.options("/agent/invoke", headers={
        "Origin": TEST_ORIGIN, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == TEST_ORIGIN
    assert r.headers["access-control-allow-credentials"] == "true"


async def test_cors_disallowed_origin_denied(client):
    r = await client.options("/agent/invoke", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers
    r = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


async def test_cors_never_wildcard(client):
    r = await client.get("/health", headers={"Origin": TEST_ORIGIN})
    assert r.headers.get("access-control-allow-origin") == TEST_ORIGIN
    assert r.headers.get("access-control-allow-origin") != "*"


async def test_no_origin_configured_emits_no_cors_headers():
    cfg = make_settings(environment="development", frontend_origin="", admin_token="")
    async with client_for(make_app(cfg)) as c:
        r = await c.options("/agent/invoke", headers={"Origin": "https://anything.example",
                                                      "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in r.headers


# --- Readiness never leaks internals --------------------------------------
async def test_ready_hides_error_details(cfg):
    from .fakes import FakeRedis
    redis = FakeRedis()
    redis.fail = True
    async with client_for(make_app(cfg, redis=redis)) as c:
        r = await c.get("/ready")
        assert r.status_code == 503 and r.json()["detail"] == "not_ready"
