# NoblePort Sales Rep Agent System

The **100 Salespeople** architecture. Every salesperson gets an AI support
team of ten sub-agents; management gets one consolidated command layer above
the reps. HubSpot is the CRM source of truth. Social, phone, website, email
and referral leads all land in one intake layer — one pipeline, one
`Opportunity`, tagged by `source`.

Truth label: **STAGED**. The loop, the gating and the HubSpot mapping are
wired and tested (69 tests, no database required). The HubSpot client raises
until a private-app token is configured; it never fabricates LIVE data.

## Operating loop

```
Lead enters -> dedupe -> qualify -> assign rep -> immediate response
  -> appointment -> scope/estimate -> follow-up -> contract/deposit
  -> production handoff -> closed won/lost -> rep learning loop
```

`PipelineStage` (models.py) is exactly that list. Agents may move a lead on
their own only through **assigned**; every later stage is reached by an
executed human-approved action (a signed send, a signed proposal, a signed
contract) or by the rep (booking the appointment, completing the site
visit). Closed won/lost is itself a signed, material CRM change that feeds
the rep learning loop.

## Object model — `rep_id`

The learning-agent `learner_id` is replaced by `rep_id` throughout
(`SalesRep`, `SalesAction.rep_id`, `Opportunity.assigned_rep`,
`RepLearning.rep_id`). Every `Opportunity` carries:

| field | notes |
|---|---|
| `contact_id`, `deal_id` | HubSpot ids, set by the lead_router's capture |
| `property_address` | fingerprinted for dedupe |
| `source` | social · phone · website · email · referral · other |
| `project_type` | same enum as `leads.projectType` in nobleport-gateway |
| `pipeline_stage` | the operating loop |
| `last_touch` | from the touch history (inbound / outbound, any channel) |
| `next_action`, `next_action_due` | never left empty past **assigned** |
| `assigned_rep` | `rep_id` |
| `confidence`, `qualification` | 0–1 and unqualified / qualifying / qualified / disqualified |

`REQUIRED_FIELDS_BY_STAGE` says when each becomes mandatory; the
compliance_data_agent flags gaps.

## The ten sub-agents (agents.py)

| agent | responsibility | runs at |
|---|---|---|
| `lead_router` | capture, dedupe, qualify, score, assign, stage | lead_entered → qualified |
| `followup_agent` | callbacks, stale leads, follow-up cadence | assigned → production_handoff |
| `outreach_agent` | draft SMS / email / social (send is gated) | stages with a template |
| `meeting_prep` | pre-call / site-visit brief | appointment, scope_estimate |
| `account_research` | homeowner / company / property / account context | lead_entered → appointment |
| `pipeline_analyst` | stalled deals, missing next actions, risk score | every open stage after routing |
| `estimate_agent` | scope lines, allowances, exclusions, estimate inputs | scope_estimate |
| `call_recap_agent` | transcript → note, tasks, next action, $ discussed | any stage, when a transcript is supplied |
| `sales_coach` | response speed, conversion, activity mix, coaching | any stage (per rep) |
| `compliance_data_agent` | dedupe, required fields, consent, sensitive data | every stage, including closed |

Same contract as the gateway's `subagents.py`: a handler takes an
`AgentContext`, returns a STAGED `AgentFinding` with proposed `SalesAction`s.
The orchestrator runs the selected agents concurrently.

## Autonomy contract (gating.py)

| autonomous (executes immediately) | human-gated (PARKs until signed) |
|---|---|
| research, monitoring, prioritization | **send outbound** SMS / email / social / proposal |
| drafting outreach, briefs, estimate inputs | **final pricing / estimates** |
| notes, tasks, next-action, scoring, dedupe flags | **contractual commitments** (contract, deposit, production handoff) |
| lead capture, *initial* assignment, staging to *assigned* | **material CRM changes** (reassign, merge, delete, amount, close won/lost, redaction) |

Enforcement is layered, fail-closed:

1. `gating.classify` stamps the policy gate + level on every proposal — an
   agent's own claim is ignored, so it cannot under-declare.
2. The orchestrator only executes `AUTONOMOUS`; everything else goes to the
   `ApprovalQueue`, and `approve()` refuses an empty signature.
3. `gating.to_envelope` wraps any action as an `McpEnvelope`; the existing
   governance gate (`core/gateway/governance.py`) PARKs L3/L4 writes and
   `human_approval_required` calls without a signature, and BLOCKs
   prohibited claims. Tested in `tests/test_gating.py`.
4. The CRM layer refuses material deal writes and outbound logging without a
   signature, even if a caller skipped the orchestrator.
5. The compliance_data_agent vetoes outbound channels without consent
   (SMS / email) or on a do-not-contact record — the proposal is rejected
   before it can be parked.

## HubSpot mapping (crm.py)

`DEAL_PROPERTY_MAP` / `CONTACT_PROPERTY_MAP` name the HubSpot properties.
Standard ones (`dealstage`, `amount`, `hubspot_owner_id`,
`notes_last_contacted`, `closed_lost_reason`) are built in; `np_*` custom
properties must be created on the portal. Pipeline stages default to ids
equal to the stage names; pass `stage_map=` for a portal's real stage ids.
Rep ↔ owner is `SalesRep.hubspot_owner_id`.

## Usage

```python
from core.sales.crm import HubSpotCrm, InMemoryCrm
from core.sales.models import RawLead, SalesRep
from core.sales.orchestrator import SalesOrchestrator

orch = SalesOrchestrator(InMemoryCrm(), [SalesRep(rep_id="rep_amy", name="Amy Chen",
                                                  territories=["newburyport"], hubspot_owner_id="101")])
result = await orch.intake(RawLead(channel="instagram", name="Jane Doe", phone="978-555-1234",
                                   property_address="12 High St, Newburyport MA",
                                   project_type="addition", consent_sms=True))
# -> scored, captured in the CRM, assigned, next action = immediate response
result = await orch.run(orch.opportunities[result.opportunity_id])
# -> drafts ready; the SMS is PARKED
for action in orch.pending(rep_id="rep_amy"):
    await orch.approve(action.action_id, signature="<human token>", approver="rep_amy")
orch.command_view()   # management: stages, sources, risk, approvals, per-rep KPIs
```

`sweep()` is the monitoring pass (run it on a schedule); `record_outcome()`
closes a deal with a signature and updates the rep's learning record.

## Files

```
core/sales/
  models.py        rep_id-keyed object model, stages, actions
  intake.py        channel -> source, normalization, dedupe, assignment
  scoring.py       port of nobleport-gateway/server/leadScoring.ts (parity-tested)
  gating.py        autonomy policy + McpEnvelope bridge to the governance gate
  agents.py        the ten sub-agents + stage-based selection
  orchestrator.py  loop, approval flow, sweep, command view
  crm.py           CrmClient protocol, InMemoryCrm, HubSpotCrm, property maps
  approvals.py     the human gate as a queue
  learning.py      RepLearning / LearningStore (the rep learning loop)
core/migrations/004_sales_rep_agents.sql
```

## Test

```bash
python -m pytest core/sales/tests -q     # from the repo root
```
