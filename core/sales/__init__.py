"""NoblePort Sales Rep Agent System.

The "100 Salespeople" architecture: every salesperson gets an AI support team
of ten specialized sub-agents, and management gets one consolidated command
layer (the Sales Orchestrator) above the reps.

    Lead enters -> dedupe -> qualify -> assign rep -> immediate response
      -> appointment -> scope/estimate -> follow-up -> contract/deposit
      -> production handoff -> closed won/lost -> rep learning loop

Object model is keyed on ``rep_id`` (replacing the learning-agent
``learner_id``). HubSpot is the CRM source of truth; every inbound channel
(social, phone, website, email, referral) lands in the same intake layer.

Autonomy contract (see gating.py):
  * autonomous  — research, monitoring, prioritization, drafting
  * human-gated — sending outbound communications, final pricing / estimates,
                  contractual commitments, material CRM changes
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
