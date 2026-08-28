from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.deepcoin_maintenance_evidence import (
    DeepcoinMaintenanceEvidenceRefused,
    build_deepcoin_maintenance_evidence,
    require_canonical_remaining_pending_set,
    require_fresh_deepcoin_maintenance_evidence,
)
from telegram_kol_research.reviewed_pending_entry_cancel import (
    REVIEWED_PENDING_ENTRY_TARGETS,
)


NOW = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
INSTRUMENTS = tuple(
    sorted({target.instrument_id for target in REVIEWED_PENDING_ENTRY_TARGETS})
)
TARGET = REVIEWED_PENDING_ENTRY_TARGETS[0]


class CompleteEvidenceClient:
    def __init__(self) -> None:
        self.pending = [
            {"instId": target.instrument_id, "ordId": target.order_id}
            for target in REVIEWED_PENDING_ENTRY_TARGETS
        ]
        self.pending_calls = 0

    def list_positions(self, *, inst_id=None):
        return []

    def list_open_orders(self, *, inst_id=None):
        return []

    def read_trigger_orders_pending(self, *, inst_id):
        self.pending_calls += 1
        return {
            "code": "0",
            "data": [row for row in self.pending if row["instId"] == inst_id],
            "hasMore": False,
        }

    def list_trigger_order_history(self, *, inst_id):
        return []

    def list_trade_fills(self, *, inst_id=None):
        return []


def _build(client, *, observed_at=NOW):
    return build_deepcoin_maintenance_evidence(
        client,
        instruments=INSTRUMENTS,
        target_order_id=TARGET.order_id,
        observed_at=observed_at,
    )


def test_evidence_requires_complete_positions_regular_pending_and_exact_target_readback():
    client = CompleteEvidenceClient()
    evidence = _build(client)

    assert evidence.status == "complete"
    assert evidence.reason_code is None
    assert evidence.position_count == 0
    assert evidence.regular_order_count == 0
    assert evidence.target_pending_count == 1
    assert len(evidence.fingerprint) == 64

    client.pending = [
        row for row in client.pending if row["ordId"] != TARGET.order_id
    ]
    missing = _build(client)
    assert missing.status == "unknown"
    assert missing.reason_code == "target_pending_readback_not_exact"


def test_incomplete_query_gets_one_reasoned_retry_then_unknown():
    class IncompleteTwiceClient(CompleteEvidenceClient):
        def read_trigger_orders_pending(self, *, inst_id):
            self.pending_calls += 1
            return {"code": "0", "data": [], "hasMore": True}

    client = IncompleteTwiceClient()
    evidence = _build(client)

    assert evidence.status == "unknown"
    assert evidence.reason_code == "pending_query_incomplete"
    assert client.pending_calls == 2


def test_evidence_older_than_thirty_seconds_is_rejected():
    evidence = _build(CompleteEvidenceClient(), observed_at=NOW)

    with pytest.raises(
        DeepcoinMaintenanceEvidenceRefused,
        match="evidence_stale",
    ):
        require_fresh_deepcoin_maintenance_evidence(
            evidence,
            now=NOW + timedelta(seconds=31),
        )


def test_evidence_state_fingerprint_is_stable_across_observation_times():
    first = _build(CompleteEvidenceClient(), observed_at=NOW)
    second = _build(
        CompleteEvidenceClient(),
        observed_at=NOW + timedelta(seconds=1),
    )

    assert first.fingerprint == second.fingerprint


def test_remaining_pending_set_must_equal_canonical_unfinished_subset():
    evidence = _build(CompleteEvidenceClient())
    canonical_ids = tuple(
        target.order_id for target in REVIEWED_PENDING_ENTRY_TARGETS
    )

    require_canonical_remaining_pending_set(
        evidence,
        canonical_order_ids=canonical_ids,
        completed_order_ids=(),
    )

    with pytest.raises(
        DeepcoinMaintenanceEvidenceRefused,
        match="remaining_pending_set_mismatch",
    ):
        require_canonical_remaining_pending_set(
            evidence,
            canonical_order_ids=canonical_ids,
            completed_order_ids=(canonical_ids[0],),
        )


def test_noncanonical_pending_trigger_stops_without_exchange_write():
    client = CompleteEvidenceClient()
    client.pending.append(
        {"instId": TARGET.instrument_id, "ordId": "not-canonical"}
    )
    evidence = _build(client)

    with pytest.raises(
        DeepcoinMaintenanceEvidenceRefused,
        match="remaining_pending_set_mismatch",
    ):
        require_canonical_remaining_pending_set(
            evidence,
            canonical_order_ids=tuple(
                target.order_id for target in REVIEWED_PENDING_ENTRY_TARGETS
            ),
            completed_order_ids=(),
        )
