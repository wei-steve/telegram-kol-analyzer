"""Bounded two-read proof for a read-only Runtime Agent exchange refresh."""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any


_MAX_SOURCE_ROWS = 200
_MAX_EPHEMERAL_CAPTURES = 32
_FINGERPRINT_PATTERN = re.compile(r"[a-f0-9]{64}")
_SNAPSHOT_FIELDS = frozenset(
    {
        "snapshot_kind",
        "complete",
        "position_count",
        "open_order_count",
        "fingerprint",
    }
)
_POSITION_FIELDS = (
    ("instrument", ("instId", "inst_id")),
    ("position_id", ("posId", "pos_id", "id")),
    ("side", ("posSide", "pos_side", "side")),
    ("size", ("pos", "size", "sz")),
)
_ORDER_FIELDS = (
    ("instrument", ("instId", "inst_id")),
    ("order_id", ("ordId", "orderId", "order_id", "id")),
    ("client_order_id", ("clOrdId", "clientOrderId", "client_order_id")),
    ("state", ("state", "status")),
    ("side", ("side", "posSide", "pos_side")),
    ("size", ("sz", "size", "remaining_size")),
)


class RuntimeAgentExchangeSnapshotError(ValueError):
    """The bounded read-only exchange proof is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class _CapturedProof:
    fingerprint: str
    position_count: int
    open_order_count: int


def _bounded_scalar(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, (str, int, float)):
        raise RuntimeAgentExchangeSnapshotError(
            "exchange snapshot contains a non-scalar field"
        )
    normalized = str(value).strip()
    if not normalized or len(normalized) > 128:
        raise RuntimeAgentExchangeSnapshotError(
            "exchange snapshot scalar is unbounded"
        )
    return normalized


def _first_scalar(
    row: Mapping[str, Any],
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return _bounded_scalar(row[key])
    return None


def _project_rows(
    value: Any,
    *,
    fields: tuple[tuple[str, tuple[str, ...]], ...],
    required: frozenset[str],
    identity_fields: frozenset[str] = frozenset(),
) -> list[dict[str, str | None]]:
    if not isinstance(value, list) or len(value) > _MAX_SOURCE_ROWS:
        raise RuntimeAgentExchangeSnapshotError(
            "exchange snapshot source is not a bounded list"
        )
    projected = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise RuntimeAgentExchangeSnapshotError(
                "exchange snapshot row must be an object"
            )
        row = {
            output_name: _first_scalar(raw, source_names)
            for output_name, source_names in fields
        }
        if any(row.get(name) is None for name in required) or (
            identity_fields
            and not any(row.get(name) is not None for name in identity_fields)
        ):
            raise RuntimeAgentExchangeSnapshotError(
                "exchange snapshot row is incomplete"
            )
        projected.append(row)
    return sorted(
        projected,
        key=lambda row: json.dumps(
            row,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def build_read_only_exchange_snapshot(client: Any) -> dict[str, Any]:
    """Read and fingerprint bounded stable account state without exposing rows."""

    positions = _project_rows(
        client.list_positions(),
        fields=_POSITION_FIELDS,
        required=frozenset(
            {"instrument", "position_id", "side", "size"}
        ),
    )
    open_orders = _project_rows(
        client.list_open_orders(),
        fields=_ORDER_FIELDS,
        required=frozenset({"instrument", "state", "side", "size"}),
        identity_fields=frozenset({"order_id", "client_order_id"}),
    )
    encoded = json.dumps(
        {
            "positions": positions,
            "open_orders": open_orders,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "snapshot_kind": "bounded_read_only_exchange",
        "complete": True,
        "position_count": len(positions),
        "open_order_count": len(open_orders),
        "fingerprint": hashlib.sha256(encoded).hexdigest(),
    }


def incomplete_read_only_exchange_snapshot() -> dict[str, Any]:
    """Return the only public failure shape; never expose provider errors."""

    return {
        "snapshot_kind": "bounded_read_only_exchange",
        "complete": False,
        "position_count": 0,
        "open_order_count": 0,
        "fingerprint": None,
    }


def build_broker_exchange_provider(client_reader: Callable[[], Any]):
    """Build the broker's exchange category from two read-only client calls."""

    def provider(request) -> dict[str, Any]:
        proof = build_read_only_exchange_snapshot(client_reader())
        return {
            "data": proof,
            "evidence_refs": [f"exchange-snapshot:{int(request.incident_id)}"],
        }

    return provider


def _validated_proof(value: Any) -> tuple[str, int, int]:
    if not isinstance(value, Mapping) or set(value) != _SNAPSHOT_FIELDS:
        raise RuntimeAgentExchangeSnapshotError(
            "read-only exchange proof has an invalid shape"
        )
    if (
        value["snapshot_kind"] != "bounded_read_only_exchange"
        or value["complete"] is not True
    ):
        raise RuntimeAgentExchangeSnapshotError(
            "read-only exchange proof is incomplete"
        )
    counts = []
    for name in ("position_count", "open_order_count"):
        count = value[name]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= _MAX_SOURCE_ROWS
        ):
            raise RuntimeAgentExchangeSnapshotError(
                "read-only exchange proof count is invalid"
            )
        counts.append(count)
    fingerprint = value["fingerprint"]
    if not isinstance(fingerprint, str) or not _FINGERPRINT_PATTERN.fullmatch(
        fingerprint
    ):
        raise RuntimeAgentExchangeSnapshotError(
            "read-only exchange proof fingerprint is invalid"
        )
    return fingerprint, counts[0], counts[1]


class RuntimeAgentExchangeSnapshotRefresh:
    """Hold one ephemeral capture and independently verify a second read."""

    def __init__(self, *, reader: Callable[[], Mapping[str, Any]]) -> None:
        self._reader = reader
        self._captures: OrderedDict[int, _CapturedProof] = OrderedDict()
        self._lock = Lock()

    def has_capture(self, incident_id: int) -> bool:
        with self._lock:
            return int(incident_id) in self._captures

    def refresh(
        self,
        *,
        incident_id: int,
        idempotency_key: str,
        expected_fingerprint: str,
    ) -> bool:
        fingerprint, position_count, open_order_count = _validated_proof(
            self._reader()
        )
        if (
            not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 255
            or not isinstance(expected_fingerprint, str)
            or not _FINGERPRINT_PATTERN.fullmatch(expected_fingerprint)
        ):
            raise RuntimeAgentExchangeSnapshotError(
                "refresh identity is invalid"
            )
        capture = _CapturedProof(
            fingerprint=fingerprint,
            position_count=position_count,
            open_order_count=open_order_count,
        )
        with self._lock:
            normalized_incident_id = int(incident_id)
            self._captures[normalized_incident_id] = capture
            self._captures.move_to_end(normalized_incident_id)
            while len(self._captures) > _MAX_EPHEMERAL_CAPTURES:
                self._captures.popitem(last=False)
        return True

    def consume_comparison(
        self,
        *,
        incident_id: int,
    ) -> dict[str, Any] | None:
        with self._lock:
            captured = self._captures.pop(int(incident_id), None)
        if captured is None:
            return None
        fingerprint, position_count, open_order_count = _validated_proof(
            self._reader()
        )
        coherent = (
            fingerprint == captured.fingerprint
            and position_count == captured.position_count
            and open_order_count == captured.open_order_count
        )
        return {
            "comparison_kind": "local_vs_coherent_read_only_snapshot",
            "applicable": True,
            "coherent": coherent,
            "complete": True,
            "matches": 1 if coherent else 0,
            "mismatches": 0 if coherent else 1,
            "unknown": 0,
            "position_count": position_count,
            "open_order_count": open_order_count,
        }
