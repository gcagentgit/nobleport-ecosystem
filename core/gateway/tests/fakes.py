"""In-memory stand-ins for Postgres and Redis, plus an app factory that wires
the real pipeline (auth -> sanitizer -> governance -> audit -> executor) on
top of them. Shared by the test suite and the end-to-end benchmark so both
exercise the same code path the production app runs, minus the network."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import Any

from ..audit import AuditBeacon, CallLog
from ..cache import Cache, RateLimiter
from ..config import Settings
from ..executors import StubExecutor
from ..gateway import Gateway
from ..governance import GovernanceGate
from ..killswitch import KillSwitch
from ..supervisor import Supervisor


class FakeRedis:
    """Minimal async Redis stand-in for unit tests."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.fail = False

    async def get(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key, value):
        self.store[key] = value

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def expire(self, key, ttl):
        return True

    async def ping(self):
        if self.fail:
            raise RuntimeError("redis down")
        return True

    async def aclose(self):
        return None


def new_store() -> dict[str, list]:
    return {"audit": [], "calls": [], "killswitch_events": []}


class _NullTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, store: dict[str, list]):
        self.store = store

    def transaction(self):
        return _NullTx()

    async def fetchval(self, sql: str, *args):
        if "FROM audit_logs" in sql:
            return self.store["audit"][-1]["payload_hash"] if self.store["audit"] else None
        if sql.strip() == "SELECT 1":
            return 1
        return 0

    async def fetchrow(self, sql: str, *args):
        return None

    async def fetch(self, sql: str, *args):
        return []

    async def execute(self, sql: str, *args):
        if "INSERT INTO audit_logs" in sql:
            self.store["audit"].append({
                "action": args[0], "entity_type": args[1], "entity_id": args[2],
                "payload_hash": args[3], "previous_hash": args[4], "raw_payload": args[5],
            })
        elif "INSERT INTO mcp_call_log" in sql:
            self.store["calls"].append({
                "run_id": args[0], "requesting_agent": args[1], "target_agent": args[2],
                "module": args[3], "tool": args[4], "truth_label": args[6],
                "level": args[7], "status": args[8], "latency_ms": args[9], "error": args[10],
            })
        elif "INSERT INTO kill_switch_events" in sql:
            self.store["killswitch_events"].append(
                {"scope": args[0], "engaged": args[1], "actor": args[2], "reason": args[3]})
        return "OK"


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, store: dict[str, list] | None = None):
        self.store = store if store is not None else new_store()

    def acquire(self):
        return _Acquire(FakeConn(self.store))

    async def close(self):
        return None


# --- Test configuration ----------------------------------------------------
TEST_AUDIENCE = "nobleport-mcp-gateway"
TEST_ISSUER = "https://auth.nobleport.test"
TEST_ORIGIN = "https://ops.nobleport.test"


def strong_secret() -> str:
    return secrets.token_urlsafe(48)


def make_settings(**overrides: Any) -> Settings:
    """A fully valid production-grade configuration for tests. Overrides let
    a test poke one hole at a time."""
    base: dict[str, Any] = dict(
        environment="production",
        jwt_secret=strong_secret(),
        jwt_audience=TEST_AUDIENCE,
        jwt_issuer=TEST_ISSUER,
        frontend_origin=TEST_ORIGIN,
        admin_token=secrets.token_urlsafe(24),
        rate_limit_per_min=1_000_000,
        enable_kpi_worker=False,
        upload_dir="/tmp/nobleport-test-uploads",
        upload_max_bytes=64 * 1024,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def make_app(cfg: Settings, *, store: dict[str, list] | None = None, executor=None,
             redis: FakeRedis | None = None):
    """The real FastAPI app (create_app) with an in-memory lifespan."""
    from ..app import create_app

    store = store if store is not None else new_store()
    redis = redis or FakeRedis()

    @asynccontextmanager
    async def lifespan(app):
        pool = FakePool(store)
        gate = GovernanceGate(cfg.launch_gates_path, cfg.max_message_chars)
        killswitch = KillSwitch(redis, pool)
        beacon = AuditBeacon(pool)
        app.state.pg = pool
        app.state.redis = redis
        app.state.store = store
        app.state.killswitch = killswitch
        app.state.beacon = beacon
        app.state.gateway = Gateway(
            pool=pool, gate=gate, killswitch=killswitch,
            cache=Cache(redis, cfg.cache_ttl_s),
            ratelimiter=RateLimiter(redis, cfg.rate_limit_per_min),
            beacon=beacon, calllog=CallLog(pool),
            executor=executor or Supervisor(inner=StubExecutor()),
        )
        yield

    return create_app(cfg, lifespan=lifespan)


@asynccontextmanager
async def client_for(app):
    """An httpx client bound to the ASGI app with its lifespan running, so
    requests traverse middleware, auth, sanitizer, governance, audit and the
    executor exactly as they would over the network."""
    import httpx

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway.test") as client:
            yield client
