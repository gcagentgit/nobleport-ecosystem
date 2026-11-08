"""Human-gate policy — what agents may do alone and what needs a signature.

Autonomous : research, monitoring, prioritization, drafting, non-material
             CRM hygiene (notes, tasks, next-action, initial capture/assign).
Human-gated: sending outbound communications, final pricing / estimates,
             contractual commitments, and material CRM changes.

The policy is expressed as gateway approval levels so a gated action can be
wrapped in an ``McpEnvelope`` and run through the *existing* governance gate
(core/gateway/governance.py) — which PARKs L3/L4 writes without a human
signature. Nothing here bypasses that gate; it front-loads the same answer so
the orchestrator never queues an unsigned outbound send as if it had run.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..gateway.envelope import McpEnvelope
from .models import ActionType, Gate, SalesAction


@dataclass(frozen=True)
class GateRule:
    gate: Gate
    approval_level: str          # L0-L4, the gateway ladder
    target_agent: str            # which MCP agent executes it
    tool: str                    # gateway tool name (registered or sales.*)
    force_human: bool = False    # set human_approval_required on the envelope


_AUTO = Gate.AUTONOMOUS
_HUMAN = Gate.HUMAN_GATED

POLICY: dict[ActionType, GateRule] = {
    # --- autonomous -------------------------------------------------------
    ActionType.RESEARCH_ACCOUNT:      GateRule(_AUTO, "L0", "Kuzo.io", "kuzo.show_project_portal_status"),
    ActionType.SCORE_LEAD:            GateRule(_AUTO, "L1", "Stephanie.ai", "sales.score_lead"),
    ActionType.DEDUPE_FLAG:           GateRule(_AUTO, "L1", "Stephanie.ai", "sales.dedupe_flag"),
    ActionType.CAPTURE_LEAD:          GateRule(_AUTO, "L2", "Kuzo.io", "kuzo.capture_lead"),
    ActionType.ASSIGN_REP:            GateRule(_AUTO, "L2", "Stephanie.ai", "sales.assign_rep"),
    ActionType.ADVANCE_STAGE:         GateRule(_AUTO, "L2", "Stephanie.ai", "sales.advance_stage"),
    ActionType.SET_NEXT_ACTION:       GateRule(_AUTO, "L2", "Stephanie.ai", "sales.set_next_action"),
    ActionType.ADD_NOTE:              GateRule(_AUTO, "L2", "Kuzo.io", "kuzo.update_customer_profile"),
    ActionType.ADD_TASK:              GateRule(_AUTO, "L2", "Stephanie.ai", "sales.add_task"),
    ActionType.DRAFT_OUTREACH:        GateRule(_AUTO, "L1", "Stephanie.ai", "sales.draft_outreach"),
    ActionType.DRAFT_MEETING_BRIEF:   GateRule(_AUTO, "L1", "Stephanie.ai", "sales.draft_meeting_brief"),
    ActionType.DRAFT_ESTIMATE_INPUTS: GateRule(_AUTO, "L1", "GCagent.ai", "gcagent.create_scope"),
    ActionType.PRIORITIZE:            GateRule(_AUTO, "L0", "Stephanie.ai", "sales.prioritize"),
    ActionType.STALE_FLAG:            GateRule(_AUTO, "L0", "Stephanie.ai", "sales.stale_flag"),
    ActionType.DATA_QUALITY_FLAG:     GateRule(_AUTO, "L0", "Cyborg.ai", "cyborg.policy_check"),
    ActionType.COACHING_INSIGHT:      GateRule(_AUTO, "L0", "Stephanie.ai", "sales.coaching_insight"),
    # --- human-gated ------------------------------------------------------
    ActionType.SEND_OUTBOUND:         GateRule(_HUMAN, "L3", "Kuzo.io", "kuzo.send_customer_update"),
    ActionType.FINAL_PRICING:         GateRule(_HUMAN, "L4", "GCagent.ai", "gcagent.price_estimate", force_human=True),
    ActionType.SEND_PROPOSAL:         GateRule(_HUMAN, "L4", "Kuzo.io", "kuzo.send_customer_update", force_human=True),
    ActionType.CONTRACT_COMMITMENT:   GateRule(_HUMAN, "L4", "Kuzo.io", "kuzo.capture_customer_approval"),
    ActionType.DEPOSIT_REQUEST:       GateRule(_HUMAN, "L4", "Kuzo.io", "kuzo.capture_customer_approval"),
    ActionType.REASSIGN_REP:          GateRule(_HUMAN, "L3", "Stephanie.ai", "sales.reassign_rep", force_human=True),
    ActionType.MERGE_CONTACT:         GateRule(_HUMAN, "L3", "Kuzo.io", "kuzo.update_customer_profile", force_human=True),
    ActionType.DELETE_RECORD:         GateRule(_HUMAN, "L4", "Kuzo.io", "kuzo.update_customer_profile", force_human=True),
    ActionType.CHANGE_DEAL_AMOUNT:    GateRule(_HUMAN, "L4", "Kuzo.io", "kuzo.update_customer_profile", force_human=True),
    ActionType.CLOSE_DEAL:            GateRule(_HUMAN, "L3", "Kuzo.io", "kuzo.update_customer_profile", force_human=True),
    ActionType.PRODUCTION_HANDOFF:    GateRule(_HUMAN, "L3", "Stephanie.ai", "sales.production_handoff", force_human=True),
    ActionType.REDACT_SENSITIVE:      GateRule(_HUMAN, "L3", "Kuzo.io", "kuzo.update_customer_profile", force_human=True),
}

HUMAN_GATED_ACTIONS = frozenset(a for a, r in POLICY.items() if r.gate is Gate.HUMAN_GATED)
AUTONOMOUS_ACTIONS = frozenset(a for a, r in POLICY.items() if r.gate is Gate.AUTONOMOUS)

# Plain-language groups, for the command layer and the docs.
GATED_GROUPS: dict[str, frozenset[ActionType]] = {
    "outbound_communications": frozenset({ActionType.SEND_OUTBOUND, ActionType.SEND_PROPOSAL}),
    "final_pricing_estimates": frozenset({ActionType.FINAL_PRICING, ActionType.SEND_PROPOSAL,
                                          ActionType.CHANGE_DEAL_AMOUNT}),
    "contractual_commitments": frozenset({ActionType.CONTRACT_COMMITMENT, ActionType.DEPOSIT_REQUEST,
                                          ActionType.PRODUCTION_HANDOFF}),
    "material_crm_changes": frozenset({ActionType.REASSIGN_REP, ActionType.MERGE_CONTACT,
                                       ActionType.DELETE_RECORD, ActionType.CHANGE_DEAL_AMOUNT,
                                       ActionType.CLOSE_DEAL, ActionType.REDACT_SENSITIVE}),
}


def rule_for(action_type: ActionType) -> GateRule:
    return POLICY[action_type]


def classify(action: SalesAction) -> SalesAction:
    """Stamp the policy gate + level onto an action. The agent's own claim is
    ignored — a sub-agent cannot under-declare its way past the gate."""
    rule = rule_for(action.action_type)
    action.gate = rule.gate
    action.approval_level = rule.approval_level
    return action


def requires_human(action: SalesAction) -> bool:
    return rule_for(action.action_type).gate is Gate.HUMAN_GATED


def to_envelope(action: SalesAction, *, requesting_agent: str = "Stephanie.ai",
                module: str = "Sales Orchestrator", message: str = "",
                requestor_region: str | None = None) -> McpEnvelope:
    """Wrap a sales action as an MCP envelope for the governance gate.

    The registered tool level is a floor inside the gate; ``force_human`` adds
    ``human_approval_required`` for gated actions that map onto a lower-level
    registered tool (e.g. final pricing rides gcagent.price_estimate, an L1
    draft tool, but must PARK until a human signs).
    """
    rule = rule_for(action.action_type)
    return McpEnvelope(
        requesting_agent=requesting_agent,
        target_agent=rule.target_agent,
        module=module,
        action=rule.tool,
        approval_level=rule.approval_level,
        message=message or action.rationale or action.action_type.value,
        payload={"sales_action": action.action_type.value,
                 "action_id": action.action_id,
                 "opportunity_id": action.opportunity_id,
                 "rep_id": action.rep_id,
                 **action.payload},
        project_id=action.opportunity_id,
        customer_id=action.payload.get("contact_id"),
        human_approval_required=rule.force_human or rule.gate is Gate.HUMAN_GATED,
        human_signature=action.human_signature,
        requestor_region=requestor_region,
    )
