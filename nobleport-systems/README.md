# NoblePort Systems — Connection Map

The 100-connection NoblePort orchestration map as executable Python. Each connection
is a typed, gated, auditable edge; Stephanie.ai is the single orchestrator.

```bash
pip install -r requirements.txt
python scripts/demo.py
pytest -v
```

## Gate stack (every `Connection.fire()`)

1. **Readiness** — only `LIVE` / `PILOT` may change state; `SIMULATION` / `ARCHITECTURE` are blocked.
2. **AuditBeacon** — hash-chained record written *before* any gate decision or handler.
3. **Canonical Truth Standard** — source evidence required; rejected claims blocked.
4. **Authority Matrix / HITL** — L3 needs human review; L4 must be executed by a human actor, never `stephanie.ai`.
5. **Handler** — exceptions fail closed.

Human review: approve a request in `registry.hitl` and pass `approval_id=...` (records the named
approver), or pass `human_reviewed=True` (recorded as `attested-by:<actor>`).

## Layout

| Path | Contents |
|---|---|
| `nobleport/core/` | `readiness`, `truth`, `audit`, `gate`, `connection`, `module`, `graph` |
| `nobleport/modules/` | 10 modules × 10 connections (IDs 1–100) |
| `nobleport/registry.py` | `NoblePortRegistry` — builds and wires all 100 |
