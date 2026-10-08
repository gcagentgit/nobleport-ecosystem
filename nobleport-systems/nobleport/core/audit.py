"""AuditBeacon — before any state change (connection #6, #66, #98)."""

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class AuditRecord:
    seq: int
    timestamp: float
    module: str
    action: str
    payload_hash: str
    prev_hash: str
    record_hash: str
    actor: str
    authority_level: int
    approved_by: Optional[str] = None
    on_chain_tx: Optional[str] = None  # Anchored to Arbitrum / Base


class AuditBeacon:
    """Hash-chained audit log. Write-before-execute.

    Every state mutation in NoblePort MUST call `emit(...)` first.
    The record hash chains to the previous one, matching the
    AuditBeacon anchoring pattern used in the eSign module.
    """

    GENESIS = "0x" + "00" * 32

    def __init__(self):
        self._chain: List[AuditRecord] = []

    @staticmethod
    def _hash(body: Dict[str, Any], prev_hash: str) -> str:
        blob = json.dumps(body, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob + prev_hash.encode()).hexdigest()

    @staticmethod
    def _body(rec: AuditRecord, prev_hash: str) -> Dict[str, Any]:
        return {
            "seq": rec.seq,
            "timestamp": rec.timestamp,
            "module": rec.module,
            "action": rec.action,
            "payload_hash": rec.payload_hash,
            "actor": rec.actor,
            "authority_level": rec.authority_level,
            "approved_by": rec.approved_by,
            "prev_hash": prev_hash,
        }

    def emit(
        self,
        module: str,
        action: str,
        payload: Dict[str, Any],
        actor: str,
        authority_level: int,
        approved_by: Optional[str] = None,
    ) -> AuditRecord:
        prev_hash = self._chain[-1].record_hash if self._chain else self.GENESIS
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()

        record = AuditRecord(
            seq=len(self._chain),
            timestamp=time.time(),
            module=module,
            action=action,
            payload_hash=payload_hash,
            prev_hash=prev_hash,
            record_hash="",
            actor=actor,
            authority_level=authority_level,
            approved_by=approved_by,
        )
        record.record_hash = self._hash(self._body(record, prev_hash), prev_hash)
        self._chain.append(record)
        return record

    def chain(self) -> List[AuditRecord]:
        return list(self._chain)

    def verify_chain(self) -> bool:
        """Re-derive the chain and confirm nothing was tampered with."""
        prev = self.GENESIS
        for rec in self._chain:
            if rec.prev_hash != prev:
                return False
            if self._hash(self._body(rec, prev), prev) != rec.record_hash:
                return False
            prev = rec.record_hash
        return True

    def anchor_to_chain(self, tx_hash: str) -> None:
        """Mark the latest record as anchored on-chain."""
        if self._chain:
            self._chain[-1].on_chain_tx = tx_hash
