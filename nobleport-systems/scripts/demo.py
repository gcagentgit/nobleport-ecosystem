"""Full 100-connection demo."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobleport import NoblePortRegistry  # noqa: E402


def main() -> None:
    registry = NoblePortRegistry()

    print("=" * 60)
    print("NoblePort Systems — Connection Map")
    print("=" * 60)

    # 1. Count
    total = len(registry.connections())
    print(f"\nTotal connections: {total}")
    assert total == 100, f"Expected 100, got {total}"

    # 2. Readiness summary
    print("\nReadiness Matrix:")
    for k, v in registry.readiness_summary().items():
        print(f"  {k:15s} {v}")

    # 3. Orchestration invariant
    print("\nOrchestration (Stephanie.ai reachability):")
    report = registry.orchestration_report()
    for module, reachable in sorted(report.items()):
        mark = "✓" if reachable else "✗"
        print(f"  {mark} {module}")

    # 4. Fire a LIVE connection with full gating
    print("\n-- Fire connection #6 (AuditBeacon before state change) --")
    c6 = registry.connection(6)
    print(f"  {c6.id}: {c6.label} [{c6.readiness.value}]")
    result = c6.fire(
        payload={"evidence_ref": "ipfs://Qm...", "action": "prepare_briefing"},
        actor="stephanie.ai",
    )
    print(f"  success={result.success} audit_seq={result.audit_seq}")

    # 5. Fire an ARCHITECTURE connection — should be blocked
    print("\n-- Fire connection #93 (NBPT ERC-1400, ARCHITECTURE) --")
    c93 = registry.connection(93)
    print(f"  {c93.id}: {c93.label} [{c93.readiness.value}]")
    result = c93.fire(payload={"evidence_ref": "cooley-opinion-001"})
    print(f"  success={result.success} reason={result.reason}")

    # 6. Fire an L3 gated action without human review — should be blocked
    print("\n-- Fire connection #22 (send_proposal, L3) without HITL --")
    c22 = registry.connection(22)
    result = c22.fire(payload={"evidence_ref": "hic-proposal-001"})
    print(f"  success={result.success} reason={result.reason}")

    # 7. Same connection after a named human approves it in the HITL console
    print("\n-- Submit #22 to HITL console, human approves, re-fire --")
    payload = {"evidence_ref": "hic-proposal-001"}
    decision = registry.hitl.submit("req-22-001", "send_proposal", payload)
    print(f"  submitted: requires_human={decision.requires_human} ({decision.reason})")
    registry.hitl.approve("req-22-001", approver="licensed-pm:owner")
    result = c22.fire(payload=payload, approval_id="req-22-001")
    print(f"  success={result.success} audit_seq={result.audit_seq} "
          f"approved_by={registry.audit_trail()[-1].approved_by}")

    # 8. L4 treasury transfer — Stephanie may never execute it herself
    print("\n-- Fire connection #100 (treasury_transfer, L4) as stephanie.ai --")
    c100 = registry.connection(100)
    result = c100.fire(
        payload={"evidence_ref": "treasury-memo-007"}, human_reviewed=True,
    )
    print(f"  success={result.success} reason={result.reason}")

    # 9. Rejected claim check
    print("\n-- Reject a fabricated claim, then retry --")
    registry.claim_register.reject("claim-42", "Fabricated EIN reference")
    c13 = registry.connection(13)  # permit anchor
    result = c13.fire(payload={
        "evidence_ref": "essex-registry-2024",
        "claim_id": "claim-42",
    })
    print(f"  success={result.success} reason={result.reason}")

    # 10. Audit chain integrity
    print("\n-- Audit Chain --")
    print(f"  Length: {len(registry.audit_trail())}")
    print(f"  Tamper check: {registry.audit.verify_chain()}")

    print("\n  Last 5 records:")
    for rec in registry.audit_trail()[-5:]:
        print(f"    seq={rec.seq} {rec.module:20s} {rec.action[:40]}")


if __name__ == "__main__":
    main()
