"""Verify the 100-connection map is complete and gated correctly."""

import pytest

from nobleport import NoblePortRegistry, Readiness
from nobleport.core.gate import AuthorityLevel


@pytest.fixture(scope="module")
def registry():
    return NoblePortRegistry()


@pytest.fixture
def fresh():
    return NoblePortRegistry()


def test_exactly_100_connections(registry):
    assert len(registry.connections()) == 100
    ids = [c.id for c in registry.connections()]
    assert ids == list(range(1, 101)), "IDs must be 1–100, no gaps"


def test_10_modules(registry):
    assert len(registry.graph.modules) == 10


def test_each_module_has_10_connections(registry):
    for name, module in registry.graph.modules.items():
        assert len(module.connections) == 10, f"{name} has {len(module.connections)}"


def test_module_id_ranges(registry):
    for idx, module in enumerate(registry.graph.modules.values()):
        assert module.ids() == list(range(idx * 10 + 1, idx * 10 + 11)), module.name


def test_stephanie_reaches_every_module(registry):
    report = registry.orchestration_report()
    assert len(report) == 9
    for module, reachable in report.items():
        assert reachable, f"Stephanie.ai cannot reach {module}"


def test_connection_1_fans_out_to_every_module(registry):
    g = registry.graph.graph
    targets = {v for _, v, k in g.out_edges("langgraph", keys=True) if k == 1}
    assert targets == set(registry.graph.modules) - {"stephanie.ai"}


def test_readiness_counts(registry):
    summary = registry.readiness_summary()
    assert sum(summary.values()) == 100
    assert summary["ARCHITECTURE"] == 4  # #27, #63, #93, #95


def test_architecture_connections_blocked(registry):
    arch = registry.graph.by_readiness(Readiness.ARCHITECTURE)
    assert len(arch) > 0
    for c in arch:
        result = c.fire(payload={"evidence_ref": "x"})
        assert result.success is False
        assert "Readiness" in result.reason


def test_simulation_connections_blocked(registry):
    for c in registry.graph.by_readiness(Readiness.SIMULATION):
        result = c.fire(payload={"evidence_ref": "x"}, human_reviewed=True)
        assert result.success is False
        assert "Readiness=SIMULATION" in result.reason


def test_live_connection_passes(registry):
    live = registry.graph.by_readiness(Readiness.LIVE)
    c = live[0]
    result = c.fire(payload={"evidence_ref": "canonical-src-001"})
    assert result.success is True


def test_missing_evidence_blocks(registry):
    result = registry.connection(11).fire(payload={})
    assert result.success is False
    assert "Missing source evidence" in result.reason


def test_l3_requires_human(registry):
    c22 = registry.connection(22)  # send_proposal → L3
    assert registry.matrix.level_for("send_proposal") == AuthorityLevel.L3_APPROVE
    blocked = c22.fire(payload={"evidence_ref": "x"})
    assert blocked.success is False
    allowed = c22.fire(payload={"evidence_ref": "x"}, human_reviewed=True)
    assert allowed.success is True


def test_hitl_console_approval_records_approver(fresh):
    c22 = fresh.connection(22)
    payload = {"evidence_ref": "hic-proposal-001"}
    decision = fresh.hitl.submit("req-1", "send_proposal", payload)
    assert decision.requires_human is True

    # Pending (not yet approved) id does not satisfy the gate
    assert c22.fire(payload=payload, approval_id="req-1").success is False

    assert fresh.hitl.approve("req-1", "licensed-pm:owner")
    result = c22.fire(payload=payload, approval_id="req-1")
    assert result.success is True
    assert fresh.audit_trail()[result.audit_seq].approved_by == "licensed-pm:owner"


def test_l4_cannot_be_executed_by_orchestrator(fresh):
    c100 = fresh.connection(100)  # treasury_transfer → L4
    assert c100.authority_level == AuthorityLevel.L4_HUMAN_ONLY
    by_ai = c100.fire(payload={"evidence_ref": "memo"}, human_reviewed=True)
    assert by_ai.success is False
    assert "executed by a human" in by_ai.reason
    by_human = c100.fire(
        payload={"evidence_ref": "memo"}, actor="cfo:owner", human_reviewed=True,
    )
    assert by_human.success is True


def test_rejected_claim_blocks(registry):
    registry.claim_register.reject("test-claim", "test")
    c13 = registry.connection(13)
    result = c13.fire(payload={
        "evidence_ref": "src",
        "claim_id": "test-claim",
    })
    assert result.success is False


def test_audit_written_before_gates(fresh):
    before = len(fresh.audit_trail())
    result = fresh.connection(22).fire(payload={"evidence_ref": "x"})  # blocked L3
    assert result.success is False
    assert len(fresh.audit_trail()) == before + 1
    assert result.audit_seq == before


def test_handler_runs_and_errors_fail_closed(fresh):
    c = fresh.connection(21)
    c.handler = lambda p: {"cost": p["sqft"] * 185}
    ok = c.fire(payload={"evidence_ref": "template-v1", "sqft": 400})
    assert ok.success and ok.output == {"cost": 74000}
    bad = c.fire(payload={"evidence_ref": "template-v1"})
    assert bad.success is False and bad.reason.startswith("Handler error")


def test_audit_chain_tamper_evident(registry):
    for cid in (1, 6, 9):
        c = registry.connection(cid)
        if c.readiness.permits_state_change():
            c.fire(payload={"evidence_ref": "x"})
    assert registry.audit.verify_chain() is True


def test_audit_chain_detects_tampering(fresh):
    for cid in (1, 6, 9):
        fresh.connection(cid).fire(payload={"evidence_ref": "x"})
    assert fresh.audit.verify_chain() is True
    fresh.audit._chain[1].actor = "attacker"
    assert fresh.audit.verify_chain() is False
