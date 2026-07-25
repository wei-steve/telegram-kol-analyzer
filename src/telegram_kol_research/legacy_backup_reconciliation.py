"""Database-only reconciliation for stale legacy generic backup-stop records.

The older generic trigger-order API can return an acknowledgement that never
becomes a live DeepCoin order.  This module never sends an exchange write: it
only compares durable local records with fresh read-only pending/history
snapshots, then marks conclusively absent legacy records as unverified.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from telegram_kol_research.models import PositionBackupStopOrder


@dataclass(frozen=True, slots=True)
class LegacyBackupReconciliationAction:
    row_id: int
    pos_id: str
    instrument_id: str
    order_id: str


@dataclass(frozen=True, slots=True)
class LegacyBackupReconciliationPlan:
    created_at: datetime
    actions: tuple[LegacyBackupReconciliationAction, ...]
    conflicts: tuple[dict[str, str], ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class LegacyBackupReconciliationResult:
    updated_pos_ids: tuple[str, ...]


def build_legacy_backup_reconciliation_plan(
    session_factory,
    *,
    deepcoin_client,
    now: datetime | None = None,
) -> LegacyBackupReconciliationPlan:
    """Build a read-only plan for conclusively absent generic backup orders."""

    created_at = now or datetime.now(UTC)
    actions: list[LegacyBackupReconciliationAction] = []
    conflicts: list[dict[str, str]] = []
    database_evidence: list[dict[str, str]] = []
    exchange_evidence: list[dict[str, object]] = []
    snapshots: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]] = {}

    with session_factory() as session:
        rows = (
            session.query(PositionBackupStopOrder)
            .filter(PositionBackupStopOrder.venue == "deepcoin")
            .filter(PositionBackupStopOrder.status == "active")
            .order_by(PositionBackupStopOrder.id.asc())
            .all()
        )
        for row in rows:
            if not _is_legacy_generic(row.request_json):
                continue
            row_id = int(row.id)
            pos_id = _text(row.pos_id)
            instrument_id = _text(row.instrument_id).upper()
            order_id = _text(row.order_id)
            database_evidence.append({
                "row_id": str(row_id), "pos_id": pos_id, "instrument_id": instrument_id,
                "order_id": order_id, "status": str(row.status),
                "request_sha256": _fingerprint(_json_value(row.request_json)),
            })
            if not pos_id or not instrument_id or not order_id:
                conflicts.append(_conflict(pos_id, "legacy_backup_identity_missing"))
                continue
            snapshot = snapshots.get(instrument_id)
            if snapshot is None:
                try:
                    snapshot = (
                        _dict_rows(deepcoin_client.list_positions(inst_id=instrument_id)),
                        _dict_rows(deepcoin_client.list_trigger_orders_pending(inst_id=instrument_id)),
                        _dict_rows(deepcoin_client.list_trigger_order_history(inst_id=instrument_id)),
                    )
                except Exception:
                    conflicts.append(_conflict(pos_id, "exchange_snapshot_unavailable"))
                    continue
                snapshots[instrument_id] = snapshot
                exchange_evidence.append({
                    "instrument_id": instrument_id,
                    "position_ids": sorted(_position_id(item) for item in snapshot[0] if _position_id(item)),
                    "pending_order_ids": sorted(_order_id(item) for item in snapshot[1] if _order_id(item)),
                    "history_order_ids": sorted(_order_id(item) for item in snapshot[2] if _order_id(item)),
                })
            _, pending, history = snapshot
            if _contains_order_id(pending, order_id) or _contains_order_id(history, order_id):
                conflicts.append(_conflict(pos_id, "legacy_order_present_or_ambiguous"))
                continue
            actions.append(LegacyBackupReconciliationAction(
                row_id=row_id, pos_id=pos_id, instrument_id=instrument_id, order_id=order_id,
            ))

    action_tuple = tuple(sorted(actions, key=lambda item: item.row_id))
    conflict_tuple = tuple(sorted(conflicts, key=lambda item: (item["pos_id"], item["reason"])))
    return LegacyBackupReconciliationPlan(
        created_at=created_at,
        actions=action_tuple,
        conflicts=conflict_tuple,
        fingerprint=_fingerprint({
            "actions": [asdict(action) for action in action_tuple],
            "conflicts": list(conflict_tuple),
            "database": database_evidence,
            "exchange": exchange_evidence,
        }),
    )


def apply_legacy_backup_reconciliation_plan(
    session_factory,
    plan: LegacyBackupReconciliationPlan,
    *,
    deepcoin_client,
    expected_fingerprint: str,
    now: datetime | None = None,
) -> LegacyBackupReconciliationResult:
    """Apply one reviewed plan using database writes only.

    A fresh read-only plan must produce the same fingerprint immediately before
    status changes, preventing an old dry-run from overriding new exchange
    evidence.  This function intentionally has no reference to exchange-write
    methods.
    """

    if not _text(expected_fingerprint):
        raise ValueError("expected fingerprint is required")
    if expected_fingerprint != plan.fingerprint:
        raise ValueError("reconciliation plan fingerprint mismatch")
    fresh = build_legacy_backup_reconciliation_plan(
        session_factory, deepcoin_client=deepcoin_client, now=now,
    )
    if fresh.fingerprint != expected_fingerprint:
        raise ValueError("reconciliation plan fingerprint changed")

    observed_at = now or datetime.now(UTC)
    updated_pos_ids: list[str] = []
    with session_factory() as session:
        for action in fresh.actions:
            row = session.get(PositionBackupStopOrder, action.row_id)
            if (
                row is None
                or row.status != "active"
                or _text(row.order_id) != action.order_id
                or not _is_legacy_generic(row.request_json)
            ):
                raise ValueError("reconciliation database state changed")
            row.status = "unverified_exchange"
            row.error_json = json.dumps({
                "reason": "legacy_order_absent_from_pending_and_history",
                "order_id": action.order_id,
                "pos_id": action.pos_id,
                "reconciled_at": observed_at.isoformat(),
            }, ensure_ascii=False, sort_keys=True)
            row.updated_at = observed_at
            updated_pos_ids.append(action.pos_id)
        session.commit()
    return LegacyBackupReconciliationResult(tuple(updated_pos_ids))


def _is_legacy_generic(request_json: str | None) -> bool:
    payload = _json_value(request_json)
    return (
        any(key in payload for key in ("triggerPrice", "closePosId", "orderType"))
        and "slTriggerPx" not in payload
        and "slTriggerPrice" not in payload
    )


def _dict_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)]


def _contains_order_id(rows: list[dict[str, Any]], order_id: str) -> bool:
    return any(_order_id(row) == order_id for row in rows)


def _order_id(row: dict[str, Any]) -> str:
    return _text(
        row.get("ordId") or row.get("orderId") or row.get("order_id")
        or row.get("algoId") or row.get("triggerOrderId")
    )


def _position_id(row: dict[str, Any]) -> str:
    return _text(row.get("posId") or row.get("pos_id") or row.get("id"))


def _conflict(pos_id: str, reason: str) -> dict[str, str]:
    return {"pos_id": pos_id, "reason": reason}


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
