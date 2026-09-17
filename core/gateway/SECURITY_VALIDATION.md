# NoblePort MCP Gateway — Security Validation

Evidence-backed status of the five security controls a defensive review of
the `NoblePort_MCP_Gateway_Replit_0824` archive found open, as implemented in
this repository's gateway (`core/gateway`). Nothing here is a certification
claim: OSCP, CISSP, NSE 8, GSE and similar are human examinations. This is an
automated **NoblePort defensive validation matrix** — reproducible from the
commands below.

## Control closure

| # | Control | Before | Now | Enforced in | Proven by |
|---|---------|--------|-----|-------------|-----------|
| 1 | Hardcoded / insecure signing key | `OIDC_JWK = os.getenv("OIDC_JWK", "insecure-dev-key")` | **No default key.** `JWT_SECRET` (≥ 32 chars) or `JWT_PUBLIC_KEY` / `OIDC_JWK` (PEM/JWK) is required; placeholder values (`insecure-dev-key`, `change-me`, …), short or low-variety secrets, `alg=none`, and HMAC↔RSA algorithm confusion are rejected. Boot refuses otherwise, in every environment. | `security.load_key_material`, `security.validate` | `tests/test_security.py` (key section) |
| 2 | JWT audience | `options={"verify_aud": False}` | **Always verified.** `aud` must equal `JWT_AUDIENCE`; missing or wrong audience → 401. There is no option to disable it. | `security.TokenVerifier.verify` | `test_wrong_audience_rejected`, `test_missing_audience_rejected`, HTTP `test_invoke_wrong_audience_401` |
| 3 | JWT issuer | absent | **Always verified.** `iss` must equal `JWT_ISSUER`; missing or wrong issuer → 401. | `security.TokenVerifier.verify` | `test_wrong_issuer_rejected`, `test_missing_issuer_rejected`, HTTP `test_invoke_wrong_issuer_401` |
| 4 | Production CORS | `FRONTEND_ORIGIN` defaulted to `"*"` | **Explicit allowlist, fail-closed.** `"*"` (any wildcard) rejected everywhere; empty allowlist = no cross-origin access in development and boot failure in production; production origins must be https. Disallowed origins get no CORS headers. | `security.parse_origins`, `security.install_cors` | `test_wildcard_origin_rejected_everywhere`, `test_production_requires_origin`, HTTP CORS tests |
| 5 | Upload authentication | `/images/upload` had no JWT dependency | **`POST /agent/upload` requires a bearer token with scope `gateway:upload`**, plus MIME allowlist, size cap, filename sanitization, SHA-256 anchored in the audit chain. | `app.upload`, `uploads.store_upload` | HTTP `test_upload_*` |

**Closure: 5/5.** Every other route except the `/health` and `/ready` probes
also requires a bearer token with the right scope (`gateway:invoke`,
`gateway:read`, `gateway:admin`), and invocation is bound to the `agent`
claim in the token — a token issued to GCagent.ai cannot invoke as
Stephanie.ai. The kill switch needs both `gateway:admin` and the
`X-Admin-Token` header.

## Additional hardening shipped with the controls

* **Request sanitizer** (`security.sanitize_envelope`): NFKC normalization and
  removal of control, format, zero-width and bidi-override characters before
  the governance claim screen. `guar​anteed returns` and fullwidth
  `ｇｕａｒａｎｔｅｅｄ` are now BLOCKED; previously they slipped past the
  prohibited-claim list. Payload depth and item caps stop hostile structures.
* **Token lifetime**: `exp`, `nbf`, `iat`, `sub` required; TTL above
  `JWT_MAX_TTL_S` rejected; unknown scopes rejected.
* **Readiness probe** no longer leaks the database/Redis error text.
* **Kill-switch events** record the authenticated subject, not a fixed "admin".

## Automated coverage

```
python -m pytest core/gateway/tests -q      # 118 passed (46 pre-existing + 72 new)
```

* `tests/test_security.py` — 41 unit tests: key policy, audience, issuer,
  lifetime, algorithm confusion, CORS parsing, sanitizer.
* `tests/test_authz_http.py` — 31 HTTP-level tests through the real ASGI app:
  every route's denial without a token, each 401/403 reason, agent binding,
  upload scope/MIME/size/filename, kill-switch two-factor, CORS allow/deny,
  rate limiting, evasion of the claim screen.

## Authenticated end-to-end benchmark

```
python -m core.gateway.benchmark_e2e --requests 3000 --concurrency 16 --markdown
```

Run: 2026-09-17T00:22:40Z · Python 3.11.15
Harness: in-process ASGI (httpx.ASGITransport); Postgres/Redis in-memory fakes; HS256 bearer tokens; environment=production; CORS allowlist set

| Metric | Result |
|---|---|
| Denial accuracy | 750/750 = 100.0% |
| Authorization accuracy | 11/11 = 100.0% |
| Audit chain intact | True (6390 events) |
| Security-control closure | 5/5 |

**Full pipeline (cache-miss)** — 3000 requests, concurrency 16

| req/s | P50 ms | P95 ms | P99 ms | outcomes |
|---|---|---|---|---|
| 1072.1 | 7.6234 | 9.5403 | 31.1845 | {'200:SUCCESS': 3000} |

**Read-only cached path** — 3000 requests, concurrency 16

| req/s | P50 ms | P95 ms | P99 ms | outcomes |
|---|---|---|---|---|
| 1383.4 | 0.6733 | 0.7776 | 1.0664 | {'200:SUCCESS': 16, '200:CACHED': 2984} |

**Forged-token rejection** — 3000 requests, concurrency 16

| req/s | P50 ms | P95 ms | P99 ms | outcomes |
|---|---|---|---|---|
| 1993.2 | 0.4699 | 0.5706 | 0.6939 | {'401': 3000} |

**Sanitizer micro-benchmark** — 10000 calls: mean 0.010351 ms, P95 0.015134 ms, P99 0.025437 ms, ≈96610.8 ops/s

**Denial matrix**

| Case | Expected | Observed | Runs | Pass |
|---|---|---|---|---|
| invoke: no Authorization header | http=401 | http=401 | 25 | PASS |
| invoke: Basic auth instead of Bearer | http=401 | http=401 | 25 | PASS |
| invoke: garbage token | http=401 | http=401 | 25 | PASS |
| invoke: forged signature (wrong key) | http=401 | http=401 | 25 | PASS |
| invoke: wrong audience | http=401 | http=401 | 25 | PASS |
| invoke: wrong issuer | http=401 | http=401 | 25 | PASS |
| invoke: expired token | http=401 | http=401 | 25 | PASS |
| invoke: not-yet-valid token | http=401 | http=401 | 25 | PASS |
| invoke: excessive TTL | http=401 | http=401 | 25 | PASS |
| invoke: read-only scope | http=403 | http=403 | 25 | PASS |
| invoke: token for another agent | http=403 | http=403 | 25 | PASS |
| invoke: no agent claim | http=403 | http=403 | 25 | PASS |
| invoke: prohibited claim (governance) | http=200 status=BLOCKED | http=200 status=BLOCKED reason=prohibited_claim:guaranteed returns | 25 | PASS |
| invoke: zero-width evasion of claim screen | http=200 status=BLOCKED | http=200 status=BLOCKED reason=prohibited_claim:guaranteed returns | 25 | PASS |
| invoke: fullwidth evasion of claim screen | http=200 status=BLOCKED | http=200 status=BLOCKED reason=prohibited_claim:guaranteed returns | 25 | PASS |
| invoke: unknown target agent | http=200 status=BLOCKED | http=200 status=BLOCKED reason=unknown_target_agent:Rogue.ai | 25 | PASS |
| invoke: RED-gate scope | http=200 status=BLOCKED | http=200 status=BLOCKED reason=red_gate_blocked:nbpt_sale | 25 | PASS |
| invoke: L4 write without human signature | http=200 status=PARKED | http=200 status=PARKED reason=human_approval_required | 25 | PASS |
| invoke: hostile payload structure | http=400 | http=400 | 25 | PASS |
| upload: no token | http=401 | http=401 | 25 | PASS |
| upload: invoke scope only | http=403 | http=403 | 25 | PASS |
| upload: disallowed MIME | http=415 | http=415 | 25 | PASS |
| upload: over size cap | http=413 | http=413 | 25 | PASS |
| metrics: no token | http=401 | http=401 | 25 | PASS |
| audit log: no token | http=401 | http=401 | 25 | PASS |
| features: no token | http=401 | http=401 | 25 | PASS |
| killswitch engage: no token | http=401 | http=401 | 25 | PASS |
| killswitch engage: admin header but read scope | http=403 | http=403 | 25 | PASS |
| killswitch engage: admin scope but no header | http=403 | http=403 | 25 | PASS |
| CORS preflight: disallowed origin | http=400 | http=400 | 25 | PASS |

**Authorization matrix**

| Case | Expected | Observed | Pass |
|---|---|---|---|
| invoke: valid token, matching agent, invoke scope | http=200 status=SUCCESS | http=200 status=SUCCESS reason=None | PASS |
| invoke: L4 write WITH human signature | http=200 status=SUCCESS | http=200 status=SUCCESS reason=None | PASS |
| metrics: read scope | http=200 | http=200 | PASS |
| features: read scope | http=200 | http=200 | PASS |
| upload: upload scope, allowed MIME, under cap | http=200 | http=200 | PASS |
| killswitch engage: admin scope + header | http=200 | http=200 | PASS |
| invoke while kill switch engaged | http=200 status=BLOCKED | http=200 status=BLOCKED reason=kill_switch_engaged:global | PASS |
| killswitch release: admin scope + header | http=200 | http=200 | PASS |
| invoke after release | http=200 status=SUCCESS | http=200 status=SUCCESS reason=None | PASS |
| CORS preflight: allowed origin | http=200 | http=200 | PASS |
| liveness probe: public | http=200 | http=200 | PASS |

### How to read these numbers

* **What is measured**: the gateway's own request path — CORS middleware,
  bearer verification (HS256), scope check, sanitizer, governance gate,
  audit hash chain (pre + post write), sub-agent supervisor and stub
  executor — driven over an in-process ASGI transport.
* **What is not**: network, TLS, a real Postgres/Redis round-trip, or the
  live MCP agent servers. These are code-path numbers, not a deployment SLO.
  "Live deployment verification" and "authenticated production round trip"
  remain **NOT TESTED** until the stack is deployed with real credentials.
* The sanitizer micro-benchmark is included only for parity with the
  earlier isolated measurement (≈100k ops/s); it is not throughput.

## Reproduce

```
pip install -r core/gateway/requirements.txt
python -m pytest core/gateway/tests -q
python -m core.gateway.benchmark_e2e --markdown
python -m core.gateway.security validate     # boot-time config check (needs .env)
python -m core.gateway.security mint --sub stephanie --agent Stephanie.ai --scope gateway:invoke
```
