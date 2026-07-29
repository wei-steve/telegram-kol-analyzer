from __future__ import annotations

import pytest

from telegram_kol_research.runtime_agent_exchange_snapshot import (
    RuntimeAgentExchangeSnapshotError,
    RuntimeAgentExchangeSnapshotRefresh,
    build_read_only_exchange_snapshot,
)


class _Client:
    def __init__(self, *, positions, orders):
        self.positions = positions
        self.orders = orders
        self.calls = []

    def list_positions(self):
        self.calls.append("positions")
        return self.positions

    def list_open_orders(self):
        self.calls.append("open_orders")
        return self.orders


def test_bounded_snapshot_is_stable_and_does_not_return_exchange_ids():
    first = _Client(
        positions=[
            {
                "posId": "position-secret-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "1",
                "markPx": "100",
            }
        ],
        orders=[
            {
                "ordId": "order-secret-1",
                "clOrdId": "client-secret-1",
                "instId": "BTC-USDT-SWAP",
                "state": "live",
                "side": "sell",
                "sz": "1",
                "uTime": "100",
            }
        ],
    )
    reordered_with_volatile_changes = _Client(
        positions=[
            {
                "markPx": "101",
                "pos": "1",
                "posSide": "long",
                "instId": "BTC-USDT-SWAP",
                "posId": "position-secret-1",
            }
        ],
        orders=[
            {
                "uTime": "200",
                "sz": "1",
                "side": "sell",
                "state": "live",
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "client-secret-1",
                "ordId": "order-secret-1",
            }
        ],
    )

    left = build_read_only_exchange_snapshot(first)
    right = build_read_only_exchange_snapshot(reordered_with_volatile_changes)

    assert left == right
    assert left["snapshot_kind"] == "bounded_read_only_exchange"
    assert left["complete"] is True
    assert left["position_count"] == 1
    assert left["open_order_count"] == 1
    assert len(left["fingerprint"]) == 64
    assert "position-secret-1" not in str(left)
    assert "order-secret-1" not in str(left)
    assert first.calls == ["positions", "open_orders"]


@pytest.mark.parametrize(
    ("positions", "orders"),
    [
        ([{}] * 201, []),
        ([], [{}] * 201),
        ({"not": "a-list"}, []),
        ([], ["not-an-object"]),
        ([{}], []),
        (
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "position-1",
                    "posSide": "long",
                }
            ],
            [],
        ),
        (
            [],
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-1",
                    "state": "live",
                    "side": "sell",
                }
            ],
        ),
    ],
)
def test_bounded_snapshot_rejects_oversized_or_malformed_sources(
    positions,
    orders,
):
    with pytest.raises(RuntimeAgentExchangeSnapshotError):
        build_read_only_exchange_snapshot(
            _Client(positions=positions, orders=orders)
        )


def test_refresh_verification_requires_two_matching_complete_reads():
    payload = {
        "snapshot_kind": "bounded_read_only_exchange",
        "complete": True,
        "position_count": 1,
        "open_order_count": 2,
        "fingerprint": "a" * 64,
    }
    reads = []

    def reader():
        reads.append("read")
        return dict(payload)

    refresh = RuntimeAgentExchangeSnapshotRefresh(reader=reader)

    assert refresh.refresh(
        incident_id=9,
        idempotency_key="runtime-incident:9:refresh:v1",
        expected_fingerprint="f" * 64,
    )
    comparison = refresh.consume_comparison(incident_id=9)

    assert reads == ["read", "read"]
    assert comparison == {
        "comparison_kind": "local_vs_coherent_read_only_snapshot",
        "applicable": True,
        "coherent": True,
        "complete": True,
        "matches": 1,
        "mismatches": 0,
        "unknown": 0,
        "position_count": 1,
        "open_order_count": 2,
    }
    assert refresh.has_capture(9) is False
    assert refresh.consume_comparison(incident_id=9) is None
    assert reads == ["read", "read"]


def test_refresh_verification_fails_closed_on_drift_or_missing_capture():
    payloads = iter(
        (
            {
                "snapshot_kind": "bounded_read_only_exchange",
                "complete": True,
                "position_count": 1,
                "open_order_count": 0,
                "fingerprint": "a" * 64,
            },
            {
                "snapshot_kind": "bounded_read_only_exchange",
                "complete": True,
                "position_count": 2,
                "open_order_count": 0,
                "fingerprint": "b" * 64,
            },
        )
    )
    refresh = RuntimeAgentExchangeSnapshotRefresh(
        reader=lambda: next(payloads)
    )

    assert refresh.has_capture(17) is False
    assert refresh.consume_comparison(incident_id=17) is None

    assert refresh.refresh(
        incident_id=17,
        idempotency_key="runtime-incident:17:refresh:v1",
        expected_fingerprint="e" * 64,
    )
    comparison = refresh.consume_comparison(incident_id=17)

    assert comparison["coherent"] is False
    assert comparison["complete"] is True
    assert comparison["mismatches"] == 1
    assert comparison["unknown"] == 0
    assert refresh.has_capture(17) is False


def test_failed_second_read_consumes_capture_and_cannot_retry():
    calls = [0]

    def reader():
        calls[0] += 1
        if calls[0] == 1:
            return {
                "snapshot_kind": "bounded_read_only_exchange",
                "complete": True,
                "position_count": 0,
                "open_order_count": 0,
                "fingerprint": "a" * 64,
            }
        raise RuntimeAgentExchangeSnapshotError("second read failed")

    refresh = RuntimeAgentExchangeSnapshotRefresh(reader=reader)
    refresh.refresh(
        incident_id=21,
        idempotency_key="runtime-incident:21:refresh:v1",
        expected_fingerprint="f" * 64,
    )

    with pytest.raises(RuntimeAgentExchangeSnapshotError):
        refresh.consume_comparison(incident_id=21)

    assert refresh.has_capture(21) is False
    assert refresh.consume_comparison(incident_id=21) is None
    assert calls == [2]


def test_refresh_rejects_incomplete_or_unbounded_endpoint_payload():
    incomplete = RuntimeAgentExchangeSnapshotRefresh(
        reader=lambda: {
            "snapshot_kind": "bounded_read_only_exchange",
            "complete": False,
            "position_count": 0,
            "open_order_count": 0,
            "fingerprint": None,
        }
    )
    oversized_count = RuntimeAgentExchangeSnapshotRefresh(
        reader=lambda: {
            "snapshot_kind": "bounded_read_only_exchange",
            "complete": True,
            "position_count": 201,
            "open_order_count": 0,
            "fingerprint": "a" * 64,
        }
    )

    for refresh in (incomplete, oversized_count):
        with pytest.raises(RuntimeAgentExchangeSnapshotError):
            refresh.refresh(
                incident_id=1,
                idempotency_key="runtime-incident:1:refresh:v1",
                expected_fingerprint="f" * 64,
            )


def test_refresh_keeps_only_a_bounded_number_of_ephemeral_captures():
    refresh = RuntimeAgentExchangeSnapshotRefresh(
        reader=lambda: {
            "snapshot_kind": "bounded_read_only_exchange",
            "complete": True,
            "position_count": 0,
            "open_order_count": 0,
            "fingerprint": "a" * 64,
        }
    )

    for incident_id in range(1, 34):
        refresh.refresh(
            incident_id=incident_id,
            idempotency_key=f"runtime-incident:{incident_id}:refresh:v1",
            expected_fingerprint="f" * 64,
        )

    assert refresh.has_capture(1) is False
    assert refresh.has_capture(2) is True
    assert refresh.has_capture(33) is True
