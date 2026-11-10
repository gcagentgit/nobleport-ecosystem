# NoblePort MCP Gateway

A controlled internal connection layer that sits between the **Stephanie.ai**
orchestrator and the five NoblePort MCP servers — **GCagent.ai**,
**PermitStream.ai**, **Cyborg.ai**, **Borg.ai**, **Kuzo.io** — across the
50 NoblePort Systems modules.

Truth label: this is the **execution + governance + audit + KPI** layer.
It is wired and tested. KPI values are **BLOCKED until real telemetry is
connected** — the gateway will not fabricate LIVE numbers.

## Pipeline (fail-closed, audit-before-state-change)

```
POST /agent/invoke  (MCP envelope)
   ↓
Bearer Auth        — signature + audience + issuer + expiry + scope, token bound to agent
   ↓
Sanitizer          — NFKC + control / zero-width / bidi stripping, depth + size caps
   ↓
Kill Switch        — global / per-agent instant halt (fail-closed)
   ↓
Governance Gate    — L0–L4, driven by core/config/launch-gates.json
   ↓
Rate Limit         — per requesting agent (Redis)
   ↓
Cache              — read-only (L0, non-write) calls only
   ↓
AuditBeacon PRE    — hash-chained INSERT into audit_logs (REQUIRED; blocks on failure)
   ↓
Supervisor         — main agent delegates to its sub-agents (concurrent)
   ↓                  each finding packed into a compression packet
AuditBeacon POST   — result + latency + optimization meta
   ↓
mcp_call_log       — operational ledger row (incl. bytes + sub-agent count)
```

Nothing executes before the AuditBeacon pre-write commits. Any unexpected
error in the kill switch or governance gate resolves to **BLOCK**, never to a
silent pass. A request that fails authentication never reaches the pipeline
at all (401/403 before any audit or executor work). Delegation happens *after* the gate and the pre-write, so
sub-agents are workers of an already-authorized call — they never bypass
enforcement.

## Sub-agents (helping the main agents)

Each main agent owns a small set of specialized sub-agents. When a call comes
in, the **Supervisor** selects the sub-agents that serve the requested tool,
runs them concurrently, and aggregates their findings:

```
Stephanie.ai      router · summarizer
GCagent.ai        estimator · scoper · scheduler
PermitStream.ai   ahj_analyst · deficiency_scanner
Cyborg.ai         policy_auditor · risk_scorer
Borg.ai           job_runner · health_monitor
Kuzo.io           intake_clerk · notifier
```

Routing: a *specialist* whose `serves` set contains the action is preferred;
if none match, the parent's *generalists* help. Sub-agents return STAGED
partials — they propose, they never assert LIVE truth. The set is seeded into
`mcp_subagent_registry` on startup.

## Built-in compression packets

Every payload that crosses an internal boundary — cache entries and sub-agent
findings — is serialized into a self-describing packet (`compression.py`):

```
[ MAGIC | version | flags | orig_len | body (zlib | raw) ]
```

Compression is smart: payloads under 256 B are stored raw (the zlib header
would cost more than it saves); larger ones are deflated. The realized ratio is
measured per call and rolled up at `/api/metrics/optimization`, so the saving
is auditable, not assumed. The Redis cache is compressed at rest and on the
wire by default.

## Operational-truth feature matrix

`/health/features` returns an honest, evidence-backed classification of every
feature surface this repo actually contains — the anti-inflation layer. Each
feature points at a real file, and the status reflects *code maturity*, not
marketing:

| Label | Meaning |
|-------|---------|
| LIVE | implemented, tested, self-instrumenting (needs only Postgres+Redis) |
| STAGED | built + tested; needs deployment / external credentials |
| MODELED | model or spec exists; logic not running |
| INTERNAL_RD | research / aspirational (contracts, tokenization) |

Today the matrix declares **27 features** (14 LIVE, 6 STAGED, 4 MODELED,
3 INTERNAL_RD) and **14 frozen commands** — actions the system refuses to run
autonomously (money movement, token issuance, contract generation, permit
filing), derived from the launch-gates RED scopes plus the governance
human-gate. Status reflects code state in this repo; public KPIs still require
deployment and connected telemetry. No inflated counts.

## Governance ladder (L0–L4)

| Level | Meaning                         | Behavior in the gate                              |
|-------|---------------------------------|---------------------------------------------------|
| L0    | read-only                       | topology check; unknown target agent → BLOCK      |
| L1    | draft                           | permitted after claim/scope screening             |
| L2    | internal update                 | oversize payload → BLOCK; rate-limit applies       |
| L3    | customer / vendor-facing        | NY geo-block; write needs human signature → PARK  |
| L4    | money / legal / permit-critical | prohibited claim / RED-gate scope → BLOCK; write needs signature → PARK |

Prohibited claims, RED-gate scopes, and the NY geo-block are **not** hard-coded
here — they are read from `core/config/launch-gates.json`, the same config the
pre-launch law review governs.

## Decisions

- **PERMIT** → executed; result returned.
- **PARK** → sensitive write without a human signature; audited, queued, not executed.
- **BLOCK** → denied; audited as a terminal event in both ledgers.

## Security (fail-closed, no insecure defaults)

See [`SECURITY_VALIDATION.md`](SECURITY_VALIDATION.md) for the control matrix,
tests, and the measured benchmark. In short (`security.py`):

* **Signing key required.** `JWT_SECRET` (HS256, ≥ 32 chars) or
  `JWT_PUBLIC_KEY` / `OIDC_JWK` (PEM or JWK for RS/ES/EdDSA). No default key;
  placeholder values and `alg=none` are rejected; boot refuses without one.
* **Audience and issuer always verified.** Every token must carry
  `aud == JWT_AUDIENCE` and `iss == JWT_ISSUER`, plus `sub`, `iat`, `exp`.
* **CORS allowlist.** `FRONTEND_ORIGIN` is an explicit comma-separated list;
  `*` is rejected; production requires https origins and refuses to boot
  without any.
* **Every route is authenticated** except `/health` and `/ready`. Scopes:
  `gateway:invoke`, `gateway:read`, `gateway:upload`, `gateway:admin`. A
  token may only invoke as the agent named in its `agent` claim. The kill
  switch needs `gateway:admin` **and** `X-Admin-Token`.
* **Sanitizer.** Invisible / bidi / fullwidth tricks are normalized away
  before the governance claim screen sees the text.

## Endpoints

| Method | Path                          | Auth (scope)        | Purpose                                  |
|--------|-------------------------------|---------------------|------------------------------------------|
| GET    | `/health`, `/ready`           | none                | liveness / readiness                     |
| GET    | `/health/features`            | `gateway:read`      | operational-truth feature matrix         |
| GET    | `/health/frozen`              | `gateway:read`      | frozen commands (refused autonomously)   |
| POST   | `/agent/invoke`               | `gateway:invoke` + agent claim | run an MCP call through the gateway |
| POST   | `/agent/upload`               | `gateway:upload`    | evidence upload (MIME allowlist, size cap, SHA-256 audited) |
| GET    | `/api/kpi/modules`            | `gateway:read`      | all 50 modules + latest snapshot         |
| GET    | `/api/kpi/module/{id}`        | `gateway:read`      | one module + snapshot history            |
| GET    | `/api/kpi/agent/{name}`       | `gateway:read`      | modules owned by an agent                |
| GET    | `/api/mcp/calls`              | `gateway:read`      | operational call log                     |
| GET    | `/api/audit/events`           | `gateway:read`      | immutable hash-chained audit events      |
| GET    | `/api/metrics/p95`            | `gateway:read`      | P50/P95/P99 latency (JSON)               |
| GET    | `/api/metrics/p95.csv`        | `gateway:read`      | **G1 P95 export** (CSV, per agent)       |
| GET    | `/api/metrics/optimization`   | `gateway:read`      | compression savings + sub-agent dispatch |
| GET    | `/api/killswitch/status`      | `gateway:read`      | current kill-switch state                |
| POST   | `/api/killswitch/engage`      | `gateway:admin` + `X-Admin-Token` | halt execution             |
| POST   | `/api/killswitch/release`     | `gateway:admin` + `X-Admin-Token` | resume execution           |

## Run

```bash
cd core/gateway
cp .env.example .env
# REQUIRED: JWT_SECRET (>= 32 chars), JWT_AUDIENCE, JWT_ISSUER, ADMIN_TOKEN,
#           FRONTEND_ORIGIN (explicit https origins in production)
python -c "import secrets; print(secrets.token_urlsafe(48))"   # -> JWT_SECRET
docker compose up --build
```

The gateway refuses to start if any of those is missing or a placeholder.
Check the configuration without starting the server:

```bash
python -m core.gateway.security validate
```

Postgres initializes from `core/migrations/` in order (001 → 003). On startup
the gateway seeds the agent / tool / module / sub-agent registries and starts
the KPI snapshot worker.

### Smoke

```bash
curl localhost:8080/health

# mint a token for Stephanie.ai (HS256 secrets only; the server never mints)
TOKEN=$(python -m core.gateway.security mint --sub stephanie --agent Stephanie.ai \
          --scope gateway:invoke --scope gateway:read)

# unauthenticated -> 401
curl -i -X POST localhost:8080/agent/invoke -H 'Content-Type: application/json' -d '{}'

curl -X POST localhost:8080/agent/invoke -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{
  "requesting_agent":"Stephanie.ai","target_agent":"GCagent.ai",
  "module":"Estimate Engine","action":"gcagent.price_estimate",
  "message":"Explain contractor deposits"}'

# governance must block this (even with invisible characters in the claim):
curl -X POST localhost:8080/agent/invoke -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{
  "requesting_agent":"Stephanie.ai","target_agent":"GCagent.ai",
  "module":"Estimate Engine","action":"gcagent.price_estimate",
  "message":"guaranteed returns on real estate"}'
```

## Test

```bash
pip install -r core/gateway/requirements.txt
python -m pytest core/gateway/tests -q        # run from the repo root
```

The suite (118 tests) covers the governance ladder, kill-switch fail-closed
behavior, P95 math, registry integrity (50 modules), envelope validation, and
the security controls end-to-end over the real ASGI app (every route's denial
without a token, audience/issuer/expiry/scope/agent-binding rejections, upload
auth, CORS allow/deny, claim-screen evasion) — no database required.

### Benchmark

```bash
python -m core.gateway.benchmark_e2e --requests 3000 --concurrency 16 --markdown
```

Authenticated end-to-end run through the real app (in-process ASGI, in-memory
Postgres/Redis): denial accuracy, authorization accuracy, P50/P95/P99 and
requests/sec. Results and caveats in `SECURITY_VALIDATION.md`.

## KPI truth discipline

Every module seeds as **BLOCKED**. The snapshot worker only flips a module to
**LIVE** when a resolver reads a real value from a real table. Today only the
modules backed by tables that already exist (`audit_logs`, `mcp_call_log`,
`workflow_states`, `kpi_snapshot`, the registry itself) report LIVE; the rest
stay BLOCKED with a reason until their source is wired in
(`kpi_worker.RESOLVERS`). The worker is append-only — it never overwrites
history.

## Code stack

The package is organized by responsibility — one concern per module, layered
from the entrypoint down to the codecs:

```
app.py            FastAPI entrypoint — create_app() factory, wiring + HTTP surface
├─ security.py    bearer auth (key/aud/iss/scope), CORS allowlist, sanitizer, boot validation
├─ uploads.py     authenticated evidence upload (MIME allowlist, size cap, SHA-256)
└─ gateway.py     pipeline orchestrator (the order of the gates)
   ├─ governance/      governance.py     L0–L4 gate, fail-closed
   ├─ control/         killswitch.py     global / per-agent kill switch
   │                   cache.py          rate limit + compressed cache
   ├─ agents/          registry.py       5 agents · 30 tools · 50 modules
   │                   subagents.py      specialized sub-agents per agent
   │                   supervisor.py     delegation + aggregation executor
   │                   executors.py      Stub (STAGED) / Http (live) inner
   ├─ audit/           audit.py          AuditBeacon hash chain + call log
   ├─ transport/       compression.py    self-describing compression packets
   │                   envelope.py       MCP envelope + result models
   └─ observability/   metrics.py        P50/P95/P99 + CSV export
                       kpi_worker.py     seeding + KPI snapshot worker

config.py             settings (env-driven; security settings have no insecure defaults)
benchmark_e2e.py      authenticated end-to-end benchmark (denial / authz accuracy, latency)

core/migrations/
  002_mcp_gateway.sql           agents, tools, modules, call log, snapshots, kill switch
  003_subagents_compression.sql sub-agent registry + compression telemetry columns
```

(Files are flat within `core/gateway/`; the grouping above is the logical
layering — imports flow strictly downward, entrypoint → pipeline → codecs.)
