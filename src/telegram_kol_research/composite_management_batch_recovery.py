"""Closed, read-only planning for the approved composite batch incident."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence
import unicodedata

from sqlalchemy import String, cast, create_engine, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ContextResolutionAttempt,
    EntryAssemblyAttempt,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    InstructionExecutionContract,
    ManagementMessageTarget,
    MediaAsset,
    MessageEvidenceExtractionClaim,
    MessageEvidenceVersion,
    MessageInstructionItem,
    MimoRecognitionAttempt,
    MimoRecognitionRun,
    PositionMutationIntent,
    PositionProtectionLedger,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    Source,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
    StrategyRevisionBatch,
    TradeSignal,
    TradingSetting,
)
from telegram_kol_research.strategy_management_contracts import (
    management_contract_fingerprint,
    load_management_contract,
)
from telegram_kol_research.strategy_management_planner import (
    management_target_fingerprint,
)
from telegram_kol_research.strategy_management_components import (
    transition_component_for_exact_position_absent_recovery,
)
from telegram_kol_research.strategy_management_take_profit_consumption import (
    plan_take_profit_consumption,
)
from telegram_kol_research.position_attribution import (
    has_authoritative_persisted_position,
)


class CompositeBatchRecoveryRefusal(ValueError):
    """The supplied incident evidence cannot safely authorize recovery."""

    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


class CompositeBatchRecoveryConflict(RuntimeError):
    """The approved recovery plan no longer matches durable state."""

    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


@dataclass(frozen=True, slots=True)
class CompositeBatchRecoveryProfile:
    batch_id: int
    raw_message_id: int
    lifecycle_id: int
    trusted_start_size: str
    target_remaining_size: str
    instrument_id: str
    side: str


BATCH_119_RECOVERY = CompositeBatchRecoveryProfile(
    batch_id=119,
    raw_message_id=10532,
    lifecycle_id=794,
    trusted_start_size="38",
    target_remaining_size="19",
    instrument_id="BTC-USDT-SWAP",
    side="long",
)


@dataclass(frozen=True, slots=True)
class CompositeRecoveryPosition:
    disposition: Literal[
        "resume_to_target",
        "protection_only_at_target",
        "protection_only_below_target",
        "position_absent",
    ]
    current_size: str | None
    close_delta: str
    effective_remaining_size: str


@dataclass(frozen=True, slots=True)
class CompositeBatchRecoveryPlan:
    batch_id: int
    status: Literal["ready", "refused"]
    reason_code: str
    position: CompositeRecoveryPosition | None
    source_fingerprint: str
    exchange_snapshot_fingerprint: str
    evidence_fingerprint: str
    evidence: Mapping[str, Any]
    production_writes: int = 0
    exchange_calls: int = 0


@dataclass(frozen=True, slots=True)
class CompositeBatchRecoveryApplyResult:
    batch_id: int
    status: Literal["repaired", "already_repaired"]
    evidence_fingerprint: str
    audit_event_id: int


@dataclass(frozen=True, slots=True)
class CompositeBatchRecoveryResumeAuthorization:
    plan: CompositeBatchRecoveryPlan
    repair_result: CompositeBatchRecoveryApplyResult


@dataclass(frozen=True, slots=True)
class _Batch119ExactHistoryScope:
    instrument_id: str
    side: str
    scope_fingerprint: str
    position_id: str = field(repr=False)
    protection_orders: tuple[tuple[str, str], ...] = field(repr=False)
    protection_evidence_fingerprints: tuple[tuple[str, str], ...] = field(
        repr=False
    )


@dataclass
class Batch119ExactRecoverySnapshot:
    positions: list[dict[str, Any]] = field(default_factory=list, repr=False)
    position_history: list[dict[str, Any]] = field(
        default_factory=list, repr=False
    )
    open_orders: list[dict[str, Any]] = field(default_factory=list, repr=False)
    pending_trigger_orders: list[dict[str, Any]] = field(
        default_factory=list, repr=False
    )
    order_history: list[dict[str, Any]] = field(default_factory=list, repr=False)
    trade_fills: list[dict[str, Any]] = field(default_factory=list, repr=False)
    trigger_history: list[dict[str, Any]] = field(default_factory=list, repr=False)
    pending_tpsl_observations: list[dict[str, Any]] = field(
        default_factory=list, repr=False
    )
    errors: dict[str, str] = field(default_factory=dict)
    account_authority: Any = field(default=None, repr=False)
    collection_authority: tuple[Mapping[str, Any], ...] = field(
        default_factory=tuple, repr=False
    )
    exact_scope: _Batch119ExactHistoryScope | None = field(
        default=None, repr=False
    )
    capture_started_at: datetime | None = None
    capture_ended_at: datetime | None = None
    scope_fingerprint: str | None = None
    _capture_seal: str | None = field(default=None, repr=False)


_BATCH119_CAPTURE_HMAC_KEY = secrets.token_bytes(32)


def build_composite_batch_recovery_status_summary(
    session_factory,
    *,
    plan: CompositeBatchRecoveryPlan,
    repair_result: CompositeBatchRecoveryApplyResult,
    executor_calls: int,
) -> dict[str, Any]:
    """Return bounded durable state after the one-shot recovery invocation."""

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, BATCH_119_RECOVERY.batch_id)
        if batch is None:
            raise CompositeBatchRecoveryConflict("repaired_batch_missing")
        components = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
            .order_by(
                StrategyManagementComponent.sequence,
                StrategyManagementComponent.id,
            )
            .all()
        )
        status_counts: dict[str, int] = {}
        for component in components:
            status = str(component.status)
            status_counts[status] = status_counts.get(status, 0) + 1
        mutation_intents = (
            session.query(PositionMutationIntent)
            .filter(
                PositionMutationIntent.execution_binding_id
                == batch.execution_binding_id
            )
            .all()
        )
        component_prefixes = tuple(f"{int(row.id)}:" for row in components)
        component_mutation_intents = [
            row
            for row in mutation_intents
            if str(row.idempotency_key or "").startswith(component_prefixes)
        ]
        recovery_audit_event_count = (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action == _RECOVERY_AUDIT_ACTION,
                ExecutionEvent.notification_fingerprint
                == plan.evidence_fingerprint,
            )
            .count()
        )
    return {
        "audit_event_id": int(repair_result.audit_event_id),
        "batch_id": BATCH_119_RECOVERY.batch_id,
        "batch_reason_code": str(batch.reason_code or ""),
        "batch_status": str(batch.status),
        "component_count": len(components),
        "component_mutation_intent_count": len(component_mutation_intents),
        "component_status_counts": {
            key: status_counts[key] for key in sorted(status_counts)
        },
        "confirmed_close_intent_count": sum(
            str(row.operation) == "close_position"
            and str(row.status) == "confirmed"
            for row in component_mutation_intents
        ),
        "evidence_fingerprint": plan.evidence_fingerprint,
        "executor_calls": int(executor_calls),
        "position_disposition": (
            None if plan.position is None else plan.position.disposition
        ),
        "recovery_audit_event_count": int(recovery_audit_event_count),
        "repair_status": str(repair_result.status),
        "unresolved_mutation_intent_count": sum(
            str(row.status) not in _TERMINAL_MUTATION_STATUSES
            for row in component_mutation_intents
        ),
    }


_EXPECTED_COMPONENTS = (
    "consume_take_profit_stage",
    "converge_partial_close",
    "replace_remaining_protection",
)
_REQUIRED_SNAPSHOT_FIELDS = (
    "positions",
    "position_history",
    "open_orders",
    "pending_trigger_orders",
    "order_history",
    "trade_fills",
    "trigger_history",
    "pending_tpsl_observations",
    "errors",
)
_SAFE_TERMINAL_MANAGEMENT_STATUSES = frozenset(
    {"succeeded", "blocked", "resolved"}
)
_SAFE_TERMINAL_INSTRUCTION_STATUSES = frozenset({"succeeded", "failed"})
_INSTRUCTION_DISPOSITIONS = (
    "approved_historical_pending_frozen",
    "historical_unknown_frozen",
    "target_incident_frozen",
    "verified_terminal_mirror",
)
_MAX_INSTRUCTION_POPULATION = 4096
_MAX_INSTRUCTION_PAYLOAD_BYTES = 16_384
_MAX_INSTRUCTION_PAYLOAD_DEPTH = 64
_MAX_INSTRUCTION_PAYLOAD_NODES = 2048
_MAX_DURABLE_CLOSE_CANDIDATES = 256
_MAX_MANAGEMENT_ENCODED_JSON_LAYERS = 8
_MANAGEMENT_OWNER_KEYS = (
    "management_batch_id",
    "managementBatchId",
    "management_leg_id",
    "managementLegId",
)
_JSON_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_TERMINAL_MUTATION_STATUSES = frozenset({"confirmed", "rejected", "blocked"})
_SAFE_TERMINAL_COMPONENT_STATUSES = frozenset(
    {"confirmed", "operator_required", "safely_skipped"}
)
BATCH_119_RECOVERY_AUTHORIZATION = (
    "I_AUTHORIZE_BATCH_119_TO_REMAINING_19"
)
_RECOVERY_AUTHORIZATION = BATCH_119_RECOVERY_AUTHORIZATION

# Exact SHA-256 identities from the approved read-only production baseline.
# Raw message, strategy, and idempotency identifiers are deliberately not
# embedded in source or serialized recovery evidence.
_APPROVED_BATCH_119_PENDING_RESIDUE_IDENTITY_DIGESTS = frozenset(
    {
        "d1e645c74fa18c41cd45c8f910e2676feb5fa5cd09b6efad43c6e8b19d38c06e",
        "9bb950cc68e52b4378d7e33732698f3ca7bf455e29fccf6baf10c8fbb450a72c",
        "3c5a472352b2114d9a44109feae544b018a946a981b09e96573a31593d41d9e5",
        "27302072a8aa513970ec337921b72a927d0d11eecc5bc2472aede90d29ddb8fc",
        "ff1854e636bf9bf7fbcb90191caa8cbef18ac26de0b9ba01ac40a68d17ba9046",
    }
)
_RECOVERY_AUDIT_ACTION = "composite_batch_false_state_repaired"
_RECOVERY_REASON = "composite_recovery_false_submission_repaired"
_MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=1)


def create_composite_recovery_read_only_session_factory(
    database_path: str | Path,
) -> sessionmaker:
    """Open one existing SQLite database through an OS-enforced read-only URI."""

    resolved_path = Path(database_path).expanduser().resolve(strict=True)
    if not resolved_path.is_file():
        raise FileNotFoundError(resolved_path)
    sqlite_uri = f"file:{resolved_path.as_posix()}?mode=ro"
    engine = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(
            sqlite_uri,
            uri=True,
            timeout=30,
        ),
        future=True,
    )
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )


def load_composite_batch_recovery_snapshot_read_only(
    session_factory,
    *,
    client: Any,
):
    """Capture only the immutable exact-ID scope approved for batch 119."""

    from telegram_kol_research.deepcoin_snapshot_authority import (
        build_exchange_collection_evidence,
        capture_account_snapshot,
    )
    from telegram_kol_research.protection_snapshot import observe_pending_tpsl

    try:
        scope = _build_batch119_exact_history_scope(session_factory)
    except CompositeBatchRecoveryRefusal as exc:
        return Batch119ExactRecoverySnapshot(
            errors={"exact_scope": exc.reason_code}
        )
    uid_scope_hash = getattr(client, "uid_scope_hash", None)
    if not _is_sha256(uid_scope_hash):
        return Batch119ExactRecoverySnapshot(
            errors={"account_composite": "snapshot_uid_scope_unavailable"},
            scope_fingerprint=scope.scope_fingerprint,
        )

    snapshot = Batch119ExactRecoverySnapshot(
        scope_fingerprint=scope.scope_fingerprint,
        exact_scope=scope,
    )
    inner_authority: list[dict[str, Any]] = []

    def capture_collection(
        *,
        endpoint: str,
        reader,
        destination: list[dict[str, Any]],
        identity_validator=None,
    ) -> object:
        response = None
        try:
            response = reader()
        except Exception as exc:
            evidence = build_exchange_collection_evidence(
                endpoint=endpoint,
                response=None,
                read_error=exc,
            )
        else:
            evidence = build_exchange_collection_evidence(
                endpoint=endpoint,
                response=response,
            )
        inner_authority.append(_collection_authority_payload(evidence))
        if not evidence.schema_valid or not evidence.complete:
            snapshot.errors[endpoint] = str(
                evidence.reason_code or "snapshot_incomplete"
            )
            return response
        rows = [dict(row) for row in evidence.rows]
        if identity_validator is not None and not identity_validator(rows):
            snapshot.errors[endpoint] = "exact_scope_identity_mismatch"
            return response
        destination.extend(rows)
        return response

    snapshot.capture_started_at = datetime.now(UTC)

    def read_exact_account_composite() -> dict[str, Any]:
        capture_collection(
            endpoint="positions",
            reader=lambda: _required_exact_reader(
                client, "read_positions"
            )(inst_id=scope.instrument_id),
            destination=snapshot.positions,
        )
        capture_collection(
            endpoint="open_orders",
            reader=lambda: _required_exact_reader(
                client, "read_open_orders"
            )(inst_id=scope.instrument_id),
            destination=snapshot.open_orders,
        )
        pending_response = capture_collection(
            endpoint="pending_trigger_orders",
            reader=lambda: _required_exact_reader(
                client, "read_trigger_orders_pending"
            )(inst_id=scope.instrument_id),
            destination=snapshot.pending_trigger_orders,
        )
        if isinstance(pending_response, dict):
            observation = observe_pending_tpsl(
                instrument_id=scope.instrument_id,
                response=pending_response,
            )
        else:
            observation = {
                "instrument_id": scope.instrument_id,
                "complete": False,
                "reason": "pending_tpsl_read_error",
                "order_ids": [],
            }
        snapshot.pending_tpsl_observations.append(observation)
        if observation.get("complete") is not True:
            snapshot.errors["pending_trigger_orders"] = str(
                observation.get("reason") or "snapshot_incomplete"
            )
        capture_collection(
            endpoint="position_history",
            reader=lambda: _required_exact_reader(
                client, "read_position_history"
            )(
                inst_id=scope.instrument_id,
                pos_id=scope.position_id,
            ),
            destination=snapshot.position_history,
            identity_validator=lambda rows: _exact_position_rows_match_scope(
                rows,
                position_id=scope.position_id,
                instrument_id=scope.instrument_id,
                side=scope.side,
            ),
        )
        for purpose, order_id in scope.protection_orders:
            capture_collection(
                endpoint=f"trigger_history_{purpose}",
                reader=lambda order_id=order_id: _required_exact_reader(
                    client, "read_trigger_order_history"
                )(
                    inst_id=scope.instrument_id,
                    order_id=order_id,
                    limit=100,
                ),
                destination=snapshot.trigger_history,
                identity_validator=(
                    lambda rows, order_id=order_id: (
                        _exact_order_rows_match_scope(
                            rows,
                            order_id=order_id,
                            instrument_id=scope.instrument_id,
                            side=scope.side,
                        )
                    )
                ),
            )
        # The approved false-submission state contains no durable regular-order
        # identity. Empty exact scopes are authoritative and require no broad
        # history request.
        capture_collection(
            endpoint="order_history",
            reader=lambda: {"data": []},
            destination=snapshot.order_history,
        )
        capture_collection(
            endpoint="trade_fills",
            reader=lambda: {"data": []},
            destination=snapshot.trade_fills,
        )
        snapshot.positions = _deduplicate_exact_snapshot_rows(
            snapshot.positions
        )
        snapshot.open_orders = _deduplicate_exact_snapshot_rows(
            snapshot.open_orders
        )
        snapshot.pending_trigger_orders = _deduplicate_exact_snapshot_rows(
            snapshot.pending_trigger_orders
        )
        snapshot.position_history = _deduplicate_exact_snapshot_rows(
            snapshot.position_history
        )
        snapshot.trigger_history = _deduplicate_exact_snapshot_rows(
            snapshot.trigger_history
        )
        snapshot.capture_ended_at = datetime.now(UTC)
        if snapshot.errors:
            raise CompositeBatchRecoveryRefusal("snapshot_incomplete")
        return {
            "data": [
                {
                    "scope_fingerprint": scope.scope_fingerprint,
                    "capture_window_fingerprint": (
                        _batch119_capture_window_fingerprint(snapshot)
                    ),
                    "snapshot_collections_fingerprint": (
                        _batch119_snapshot_collections_fingerprint(snapshot)
                    ),
                    "collections": sorted(
                        inner_authority,
                        key=lambda row: str(row["endpoint"]),
                    ),
                }
            ]
        }

    snapshot.account_authority = capture_account_snapshot(
        session_factory,
        uid_scope_hash=uid_scope_hash,
        readers={"batch119_exact_account_composite": read_exact_account_composite},
    )
    snapshot.collection_authority = tuple(
        MappingProxyType(dict(row)) for row in inner_authority
    )
    if snapshot.capture_ended_at is None:
        snapshot.capture_ended_at = datetime.now(UTC)
    if not snapshot.account_authority.complete:
        snapshot.errors.setdefault(
            "account_composite",
            str(
                snapshot.account_authority.reason_code
                or "snapshot_incomplete"
            ),
        )
    try:
        return _seal_batch119_recovery_snapshot(snapshot)
    except (AttributeError, TypeError, ValueError, RecursionError, OverflowError):
        snapshot._capture_seal = None
        snapshot.errors.setdefault("capture_seal", "snapshot_seal_unavailable")
        return snapshot


def _build_batch119_exact_history_scope(
    session_factory,
) -> _Batch119ExactHistoryScope:
    with session_factory() as session:
        return _build_batch119_exact_history_scope_in_session(session)


def _build_batch119_exact_history_scope_in_session(
    session,
    *,
    allowed_unverified_order_refs: frozenset[str] | None = None,
) -> _Batch119ExactHistoryScope:
    profile = BATCH_119_RECOVERY
    batch = session.get(StrategyManagementBatch, profile.batch_id)
    lifecycle = session.get(StrategyLifecycle, profile.lifecycle_id)
    raw = session.get(RawMessage, profile.raw_message_id)
    if batch is None or lifecycle is None or raw is None:
        raise CompositeBatchRecoveryRefusal("durable_identity_mismatch")
    if (
        int(batch.raw_message_id or 0) != profile.raw_message_id
        or int(batch.target_lifecycle_id or 0) != profile.lifecycle_id
        or int(batch.id) != profile.batch_id
    ):
        raise CompositeBatchRecoveryRefusal("incident_identity_mismatch")
    binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
    legs = (
        session.query(StrategyManagementLeg)
        .filter_by(management_batch_id=profile.batch_id)
        .order_by(StrategyManagementLeg.leg_index, StrategyManagementLeg.id)
        .all()
    )
    if binding is None or len(legs) != 1:
        raise CompositeBatchRecoveryRefusal("durable_identity_mismatch")
    leg = legs[0]
    entry = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
    if entry is None:
        raise CompositeBatchRecoveryRefusal("durable_identity_mismatch")
    identity_reason = _durable_identity_refusal(
        session=session,
        batch=batch,
        raw=raw,
        lifecycle=lifecycle,
        binding=binding,
        entry=entry,
        leg=leg,
        profile=profile,
    )
    if identity_reason is not None:
        raise CompositeBatchRecoveryRefusal(identity_reason)
    rows = (
        session.query(PositionProtectionLedger)
        .filter(
            or_(
                PositionProtectionLedger.execution_binding_id
                == int(binding.id),
                PositionProtectionLedger.execution_order_leg_id
                == int(entry.id),
                PositionProtectionLedger.pos_id == str(leg.pos_id),
            )
        )
        .order_by(PositionProtectionLedger.id)
        .all()
    )
    if allowed_unverified_order_refs is None:
        invalid_audit, allowed_unverified_order_refs = (
            _canonical_recovery_audit_order_refs(
                session,
                batch=batch,
                binding=binding,
                leg=leg,
                entry=entry,
            )
        )
        if invalid_audit:
            raise CompositeBatchRecoveryRefusal(
                "exact_history_scope_invalid"
            )
    return _batch119_exact_history_scope_from_rows(
        rows,
        batch=batch,
        binding=binding,
        leg=leg,
        entry=entry,
        allowed_unverified_order_refs=allowed_unverified_order_refs,
    )


def _batch119_exact_history_scope_from_rows(
    rows,
    *,
    batch,
    binding,
    leg,
    entry,
    allowed_unverified_order_refs: frozenset[str] | None,
) -> _Batch119ExactHistoryScope:
    profile = BATCH_119_RECOVERY
    scoped: list[tuple[str, str]] = []
    scoped_evidence: list[tuple[str, str]] = []
    for row in rows:
        purpose = str(row.purpose or "").lower()
        exact_owner = (
            _exact_int(row.execution_binding_id) == int(binding.id)
            and _exact_int(row.execution_order_leg_id) == int(entry.id)
            and str(row.pos_id or "") == str(leg.pos_id)
        )
        if purpose == "take_profit" and exact_owner:
            continue
        if purpose not in {"stop_loss", "backup_stop"}:
            if exact_owner:
                raise CompositeBatchRecoveryRefusal(
                    "exact_history_scope_invalid"
                )
            continue
        order_id = str(row.order_id or "").strip()
        immutable_scope_matches = (
            exact_owner
            and str(row.venue or "").lower() == "deepcoin"
            and str(row.strategy_instance_id or "")
            == str(batch.strategy_instance_id)
            and str(row.instrument_id or "").upper()
            == profile.instrument_id.upper()
            and str(row.side or "").lower() == profile.side.lower()
            and _safe_exact_history_order_id(order_id)
        )
        if str(row.status or "").lower() != "verified":
            if not (
                allowed_unverified_order_refs is not None
                and str(row.status or "").lower() == "superseded"
                and immutable_scope_matches
                and str(row.evidence_source or "")
                == "entry_protection_response"
                and _redacted_ref("protection_order", order_id)
                in allowed_unverified_order_refs
            ):
                raise CompositeBatchRecoveryRefusal(
                    "exact_history_scope_invalid"
                )
            continue
        if not immutable_scope_matches:
            raise CompositeBatchRecoveryRefusal("exact_history_scope_invalid")
        scoped.append((purpose, order_id))
        scoped_evidence.append(
            (
                purpose,
                _batch119_protection_scope_evidence_fingerprint(
                    row=row,
                    purpose=purpose,
                    order_id=order_id,
                ),
            )
        )
    if (
        len(scoped) != 2
        or {purpose for purpose, _ in scoped}
        != {"stop_loss", "backup_stop"}
        or len({order_id for _, order_id in scoped}) != 2
    ):
        raise CompositeBatchRecoveryRefusal("exact_history_scope_invalid")
    scoped.sort(key=lambda item: item[0])
    scoped_evidence.sort(key=lambda item: item[0])
    protection_orders = tuple(scoped)
    protection_evidence_fingerprints = tuple(scoped_evidence)
    return _Batch119ExactHistoryScope(
        instrument_id=profile.instrument_id,
        side=profile.side,
        position_id=str(leg.pos_id),
        protection_orders=protection_orders,
        protection_evidence_fingerprints=protection_evidence_fingerprints,
        scope_fingerprint=_batch119_exact_scope_fingerprint(
            position_id=str(leg.pos_id),
            protection_orders=protection_orders,
            protection_evidence_fingerprints=(
                protection_evidence_fingerprints
            ),
        ),
    )


def _safe_exact_history_order_id(order_id: str) -> bool:
    try:
        from telegram_kol_research.deepcoin_client import (
            _optional_exact_exchange_id,
        )

        _optional_exact_exchange_id(order_id)
    except Exception:
        return False
    return True


def _canonical_recovery_audit_order_refs(
    session,
    *,
    batch,
    binding,
    leg,
    entry,
) -> tuple[bool, frozenset[str] | None]:
    events = (
        session.query(ExecutionEvent)
        .filter(
            ExecutionEvent.execution_binding_id == int(binding.id),
            ExecutionEvent.action == _RECOVERY_AUDIT_ACTION,
        )
        .all()
    )
    if not events:
        return False, None
    if len(events) != 1 or not _is_sha256(events[0].notification_fingerprint):
        return True, None
    try:
        after = _validated_resume_audit_event(
            events[0],
            expected_fingerprint=str(events[0].notification_fingerprint),
        )
        components = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=int(batch.id))
            .order_by(StrategyManagementComponent.sequence)
            .all()
        )
        contract = _validated_contract(batch, profile=BATCH_119_RECOVERY)
        target = _validated_target_snapshot(
            batch,
            binding=binding,
            leg=leg,
            entry=entry,
            profile=BATCH_119_RECOVERY,
        )
        if isinstance(contract, str) or isinstance(target, str):
            return True, None
        if not _recovery_audit_matches_current_durable_state(
            session,
            event=events[0],
            after=after,
            batch=batch,
            binding=binding,
            leg=leg,
            entry=entry,
            components=components,
            contract=contract,
            target=target,
        ):
            if _canonical_audit_original_rows_still_owned(
                session,
                after=after,
                batch=batch,
                binding=binding,
                leg=leg,
                entry=entry,
            ):
                return False, None
            return True, None
        return False, frozenset(after["original_owned_stop_refs"])
    except (
        CompositeBatchRecoveryConflict,
        CompositeBatchRecoveryRefusal,
        TypeError,
        ValueError,
        RecursionError,
        OverflowError,
    ):
        return True, None


def _canonical_audit_original_rows_still_owned(
    session,
    *,
    after: Mapping[str, Any],
    batch,
    binding,
    leg,
    entry,
) -> bool:
    original_refs = after.get("original_owned_stop_refs")
    if not isinstance(original_refs, list) or not original_refs:
        return False
    matches: dict[str, Any] = {}
    for row in session.query(PositionProtectionLedger).all():
        order_id = str(row.order_id or "").strip()
        if not _safe_exact_history_order_id(order_id):
            continue
        order_ref = _redacted_ref("protection_order", order_id)
        if order_ref not in original_refs:
            continue
        if order_ref in matches:
            return False
        matches[order_ref] = row
    if set(matches) != set(original_refs):
        return False
    for row in matches.values():
        if (
            _exact_int(row.execution_binding_id) != int(binding.id)
            or _exact_int(row.execution_order_leg_id) != int(entry.id)
            or str(row.pos_id or "") != str(leg.pos_id)
            or str(row.venue or "").lower() != "deepcoin"
            or str(row.strategy_instance_id or "")
            != str(batch.strategy_instance_id)
            or str(row.instrument_id or "").upper()
            != BATCH_119_RECOVERY.instrument_id.upper()
            or str(row.side or "").lower()
            != BATCH_119_RECOVERY.side.lower()
            or str(row.purpose or "").lower()
            not in {"stop_loss", "backup_stop"}
            or str(row.status or "").lower()
            not in {"verified", "superseded"}
            or str(row.evidence_source or "")
            != "entry_protection_response"
        ):
            return False
    return True


def _recovery_audit_matches_current_durable_state(
    session,
    *,
    event: ExecutionEvent,
    after: Mapping[str, Any],
    batch,
    binding,
    leg,
    entry,
    components,
    contract,
    target: Mapping[str, Any],
) -> bool:
    try:
        position = _resume_position(
            disposition=str(after["position_disposition"]),
            current_size=after["current_size"],
        )
    except (CompositeBatchRecoveryConflict, KeyError, TypeError, ValueError):
        return False
    position_absent = position.disposition == "position_absent"
    expected_batch_status = "resolved" if position_absent else "ready"
    expected_leg_status = "failed" if position_absent else "planned"
    expected_component_statuses = (
        ("safely_skipped",) * len(_EXPECTED_COMPONENTS)
        if position_absent
        else ("recovery_required", "pending", "pending")
    )
    recovery_reason = (
        "composite_recovery_exact_position_absent"
        if position_absent
        else _RECOVERY_REASON
    )
    evidence_fingerprint = str(after.get("evidence_fingerprint") or "")
    evidence_suffix = None
    expected_reason_codes: tuple[str | None, ...] = (
        "take_profit_exchange_snapshot_incomplete",
        None,
        None,
    )
    if position_absent:
        evidence_suffix = {
            "kind": "composite_recovery_exact_position_absent",
            "recovery_evidence_fingerprint": evidence_fingerprint,
        }
        expected_reason_codes = (recovery_reason,) * len(_EXPECTED_COMPONENTS)
    elif position.disposition == "protection_only_below_target":
        evidence_suffix = {
            "kind": "approved_under_target_recovery",
            "actual_remaining_size": position.effective_remaining_size,
            "original_target_remaining_size": (
                BATCH_119_RECOVERY.target_remaining_size
            ),
            "recovery_evidence_fingerprint": evidence_fingerprint,
        }
    component_attempt_counts = after.get("component_attempt_counts")
    try:
        current_component_attempt_counts = [
            int(row.attempt_count) for row in components
        ]
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        int(event.execution_binding_id or 0) != int(binding.id)
        or after.get("batch_status") != expected_batch_status
        or after.get("leg_status") != expected_leg_status
        or after.get("component_statuses")
        != list(expected_component_statuses)
        or not isinstance(component_attempt_counts, list)
        or current_component_attempt_counts != component_attempt_counts
        or str(batch.status) != expected_batch_status
        or str(batch.reason_code) != recovery_reason
        or batch.last_progress_at != event.created_at
        or batch.updated_at != event.created_at
        or batch.reconciled_at
        != (event.created_at if position_absent else None)
        or batch.completed_at
        != (event.created_at if position_absent else None)
        or str(leg.status) != expected_leg_status
        or leg.updated_at != event.created_at
        or _safe_json_value(leg.last_error)
        != {
            "reason": recovery_reason,
            "recovery_evidence_fingerprint": evidence_fingerprint,
        }
        or leg.request_json is not None
        or leg.response_json is not None
        or leg.client_order_id is not None
        or leg.exchange_order_id is not None
        or [str(row.status) for row in components]
        != list(expected_component_statuses)
        or any(
            row.completed_at
            != (event.created_at if position_absent else None)
            for row in components
        )
        or (
            (
                position_absent
                or position.disposition == "protection_only_below_target"
            )
            and any(row.updated_at != event.created_at for row in components)
        )
        or _component_topology_refusal(
            components,
            batch=batch,
            leg=leg,
            entry=entry,
            target=target,
            expected_contract_fingerprint=str(
                batch.management_contract_fingerprint
            ),
            evidence_suffix=evidence_suffix,
            expected_statuses=expected_component_statuses,
            expected_reason_codes=expected_reason_codes,
        )
        is not None
    ):
        return False
    try:
        original_owned_stop_refs = (
            []
            if position_absent
            else _original_owned_stop_refs(
                session,
                batch=batch,
                leg=leg,
                entry=entry,
                audit_created_at=event.created_at,
            )
        )
        current_instruction_population = _instruction_population_summary(
            _instruction_population_payload(
                session,
                batch=batch,
                profile=BATCH_119_RECOVERY,
            )
        )
    except (
        CompositeBatchRecoveryConflict,
        CompositeBatchRecoveryRefusal,
        TypeError,
        ValueError,
        RecursionError,
        OverflowError,
    ):
        return False
    return bool(
        after.get("original_owned_stop_refs") == original_owned_stop_refs
        and after.get("instruction_population")
        == current_instruction_population
        and _canonical_pre_repair_source_fingerprint_matches(
            session,
            event=event,
            after=after,
            batch=batch,
            binding=binding,
            leg=leg,
            entry=entry,
            components=components,
            contract=contract,
            target=target,
            original_owned_stop_refs=original_owned_stop_refs,
        )
        and _is_sha256(evidence_fingerprint)
        and evidence_fingerprint == event.notification_fingerprint
    )


def _canonical_pre_repair_source_fingerprint_matches(
    session,
    *,
    event: ExecutionEvent,
    after: Mapping[str, Any],
    batch,
    binding,
    leg,
    entry,
    components,
    contract,
    target: Mapping[str, Any],
    original_owned_stop_refs: Sequence[str],
) -> bool:
    expected_fingerprint = after.get("source_fingerprint")
    if not _is_sha256(expected_fingerprint):
        return False
    raw = session.get(RawMessage, BATCH_119_RECOVERY.raw_message_id)
    lifecycle = session.get(
        StrategyLifecycle,
        BATCH_119_RECOVERY.lifecycle_id,
    )
    if raw is None or lifecycle is None:
        return False
    try:
        instruction_population = _instruction_population_payload(
            session,
            batch=batch,
            profile=BATCH_119_RECOVERY,
        )
    except CompositeBatchRecoveryRefusal:
        return False
    original_ledger = (
        session.query(PositionProtectionLedger)
        .filter(
            PositionProtectionLedger.execution_binding_id == int(binding.id),
            PositionProtectionLedger.execution_order_leg_id == int(entry.id),
            PositionProtectionLedger.pos_id == str(leg.pos_id),
            PositionProtectionLedger.created_at <= event.created_at,
        )
        .order_by(PositionProtectionLedger.id)
        .all()
    )
    try:
        payload = _source_evidence_payload(
            batch=batch,
            raw=raw,
            lifecycle=lifecycle,
            binding=binding,
            entry=entry,
            leg=leg,
            components=components,
            target=target,
            contract=contract,
            protection_ledger=original_ledger,
            instruction_population=instruction_population,
        )
        before = _safe_json_value(event.before_json)
        if not isinstance(before, Mapping):
            return False
        payload["batch_status"] = before["batch_status"]
        payload["batch_reason_code"] = (
            "management_close_pending_exchange_confirmation"
        )
        payload["leg_status"] = before["leg_status"]
        before_statuses = before["component_statuses"]
        before_attempts = before["component_attempt_counts"]
        evidence_suffix_kind = (
            "composite_recovery_exact_position_absent"
            if after.get("position_disposition") == "position_absent"
            else "approved_under_target_recovery"
            if after.get("position_disposition")
            == "protection_only_below_target"
            else None
        )
        for index, component_payload in enumerate(payload["components"]):
            evidence = _safe_json_value(components[index].evidence_json)
            if not isinstance(evidence, list):
                return False
            if evidence_suffix_kind is not None:
                if (
                    not evidence
                    or not isinstance(evidence[-1], Mapping)
                    or evidence[-1].get("kind") != evidence_suffix_kind
                    or evidence[-1].get("recovery_evidence_fingerprint")
                    != after.get("evidence_fingerprint")
                ):
                    return False
                evidence = evidence[:-1]
            component_payload["status"] = before_statuses[index]
            component_payload["attempt_count"] = before_attempts[index]
            component_payload["reason_code"] = (
                "take_profit_exchange_snapshot_incomplete"
                if index == 0
                else None
            )
            component_payload["evidence_fingerprint"] = _fingerprint(evidence)
        original_refs = set(original_owned_stop_refs)
        for ledger_payload in payload["owned_protection"]:
            if ledger_payload["order_ref"] in original_refs:
                ledger_payload["status"] = "verified"
        last_error_candidates = (
            _fingerprint(None),
            _fingerprint({"reason": "management_close_order_not_found"}),
        )
        return any(
            _fingerprint(
                {
                    **payload,
                    "leg_last_error_fingerprint": last_error_fingerprint,
                }
            )
            == expected_fingerprint
            for last_error_fingerprint in last_error_candidates
        )
    except (
        CompositeBatchRecoveryRefusal,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        RecursionError,
        OverflowError,
    ):
        return False


def _batch119_exact_scope_fingerprint(
    *,
    position_id: str,
    protection_orders: Sequence[tuple[str, str]],
    protection_evidence_fingerprints: Sequence[tuple[str, str]],
) -> str:
    evidence_by_purpose = dict(protection_evidence_fingerprints)
    if (
        len(evidence_by_purpose) != len(protection_evidence_fingerprints)
        or set(evidence_by_purpose)
        != {purpose for purpose, _ in protection_orders}
        or not all(_is_sha256(value) for value in evidence_by_purpose.values())
    ):
        raise CompositeBatchRecoveryRefusal("exact_history_scope_invalid")
    return _fingerprint(
        {
            "schema_version": 3,
            "batch_id": BATCH_119_RECOVERY.batch_id,
            "position_ref": _redacted_ref(
                "recovery_position", position_id
            ),
            "protection_scope": sorted(
                (
                    {
                        "purpose": str(purpose),
                        "order_ref": _redacted_ref(
                            "protection_order", order_id
                        ),
                        "evidence_fingerprint": evidence_by_purpose[purpose],
                    }
                    for purpose, order_id in protection_orders
                ),
                key=lambda row: (row["purpose"], row["order_ref"]),
            ),
        }
    )


def _batch119_protection_scope_evidence_fingerprint(
    *,
    row,
    purpose: str,
    order_id: str,
) -> str:
    evidence_source = str(row.evidence_source or "")
    if (
        not evidence_source
        or len(evidence_source) > 64
        or any(
            not (
                character.isascii()
                and (character.isalnum() or character in {"_", "-", ":"})
            )
            for character in evidence_source
        )
    ):
        raise CompositeBatchRecoveryRefusal("exact_history_scope_invalid")
    try:
        return _fingerprint(
            {
                "schema_version": 1,
                "venue": str(row.venue or "").lower(),
                "strategy_ref": _redacted_ref(
                    "protection_strategy", row.strategy_instance_id
                ),
                "position_ref": _redacted_ref(
                    "protection_position", row.pos_id
                ),
                "instrument_id": str(row.instrument_id or "").upper(),
                "side": str(row.side or "").lower(),
                "purpose": str(purpose),
                "order_ref": _redacted_ref("protection_order", order_id),
                "trigger_price": _batch119_scope_decimal_marker(
                    row.trigger_price,
                    value_kind="trigger_price",
                ),
                "size": _batch119_scope_decimal_marker(
                    row.size_text,
                    value_kind="size",
                ),
                "status": str(row.status or "").lower(),
                "evidence_source_ref": _redacted_ref(
                    "protection_evidence_source", evidence_source
                ),
                "evidence_fingerprint": _optional_json_fingerprint(
                    row.evidence_json
                ),
            }
        )
    except (
        CompositeBatchRecoveryRefusal,
        TypeError,
        ValueError,
        RecursionError,
        OverflowError,
    ):
        raise CompositeBatchRecoveryRefusal(
            "exact_history_scope_invalid"
        ) from None


def _batch119_scope_decimal_marker(
    value: Any,
    *,
    value_kind: Literal["trigger_price", "size"],
) -> Mapping[str, str]:
    if value_kind not in {"trigger_price", "size"} or value is None:
        raise CompositeBatchRecoveryRefusal("exact_history_scope_invalid")
    if isinstance(value, bool):
        raise CompositeBatchRecoveryRefusal("exact_history_scope_invalid")
    try:
        raw_text = str(value)
    except (TypeError, ValueError, RecursionError):
        raise CompositeBatchRecoveryRefusal(
            "exact_history_scope_invalid"
        ) from None
    if (
        not raw_text
        or len(raw_text) > 64
        or not raw_text.isascii()
        or any(character not in "0123456789+-.eE" for character in raw_text)
    ):
        raise CompositeBatchRecoveryRefusal("exact_history_scope_invalid")
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        raise CompositeBatchRecoveryRefusal("exact_history_scope_invalid")
    decimal_tuple = decimal_value.as_tuple()
    if (
        len(decimal_tuple.digits) > 64
        or not isinstance(decimal_tuple.exponent, int)
        or abs(decimal_tuple.exponent) > 128
        or abs(decimal_value.adjusted()) > 128
    ):
        raise CompositeBatchRecoveryRefusal("exact_history_scope_invalid")
    if (
        (value_kind == "trigger_price" and decimal_value <= 0)
        or (value_kind == "size" and decimal_value < 0)
    ):
        raise CompositeBatchRecoveryRefusal("exact_history_scope_invalid")
    try:
        normalized = _decimal_text(decimal_value)
    except (ArithmeticError, TypeError, ValueError):
        raise CompositeBatchRecoveryRefusal(
            "exact_history_scope_invalid"
        ) from None
    return MappingProxyType(
        {
            "kind": "decimal",
            "value": normalized,
        }
    )


def _required_exact_reader(client: Any, name: str):
    reader = getattr(client, name, None)
    if not callable(reader):
        raise CompositeBatchRecoveryRefusal("snapshot_reader_unavailable")
    return reader


def _collection_authority_payload(evidence: Any) -> dict[str, Any]:
    return {
        "endpoint": str(evidence.endpoint),
        "available": bool(evidence.available),
        "schema_valid": bool(evidence.schema_valid),
        "complete": bool(evidence.complete),
        "row_count": int(evidence.row_count),
        "page_count": int(evidence.page_count),
        "fingerprint": evidence.fingerprint,
        "reason_code": evidence.reason_code,
    }


def _exact_position_rows_match_scope(
    rows: Sequence[Mapping[str, Any]],
    *,
    position_id: str,
    instrument_id: str,
    side: str,
) -> bool:
    for row in rows:
        identities = {
            str(row.get(key)).strip()
            for key in ("posId", "pos_id", "closePosId", "close_pos_id")
            if row.get(key) not in (None, "")
        }
        instruments = {
            str(row.get(key)).strip().upper()
            for key in ("instId", "instrument_id", "instrumentId")
            if row.get(key) not in (None, "")
        }
        sides = _exact_row_position_sides(row)
        if (
            identities != {position_id}
            or instruments != {instrument_id.upper()}
            or sides != {side.lower()}
        ):
            return False
    return True


def _exact_order_rows_match_scope(
    rows: Sequence[Mapping[str, Any]],
    *,
    order_id: str,
    instrument_id: str,
    side: str,
) -> bool:
    for row in rows:
        identities = {
            str(row.get(key)).strip()
            for key in ("ordId", "orderId", "order_id")
            if row.get(key) not in (None, "")
        }
        instruments = {
            str(row.get(key)).strip().upper()
            for key in ("instId", "instrument_id", "instrumentId")
            if row.get(key) not in (None, "")
        }
        sides = _exact_row_position_sides(row)
        if (
            identities != {order_id}
            or instruments != {instrument_id.upper()}
            or sides != {side.lower()}
        ):
            return False
    return True


def _exact_row_position_sides(row: Mapping[str, Any]) -> set[str]:
    position_sides = {
        str(row.get(key)).strip().lower()
        for key in ("posSide", "pos_side")
        if row.get(key) not in (None, "")
    }
    if position_sides:
        return position_sides
    side = row.get("side")
    return set() if side in (None, "") else {str(side).strip().lower()}


def _deduplicate_exact_snapshot_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        canonical = _canonical_json(dict(row))
        if canonical not in seen:
            seen.add(canonical)
            result.append(dict(row))
    return result


def _batch119_snapshot_collections_fingerprint(snapshot: Any) -> str:
    return _fingerprint(
        {
            "capture_window_fingerprint": (
                _batch119_capture_window_fingerprint(snapshot)
            ),
            "collections": {
                field_name: sorted(
                    _canonical_snapshot_row(row)
                    for row in getattr(snapshot, field_name)
                )
                for field_name in _REQUIRED_SNAPSHOT_FIELDS
                if field_name != "errors"
            },
        }
    )


def _batch119_capture_window_fingerprint(snapshot: Any) -> str:
    started_at = _normalize_aware_utc_datetime(
        getattr(snapshot, "capture_started_at", None)
    )
    ended_at = _normalize_aware_utc_datetime(
        getattr(snapshot, "capture_ended_at", None)
    )
    if started_at is None or ended_at is None or started_at > ended_at:
        raise CompositeBatchRecoveryRefusal("snapshot_capture_window_invalid")
    return _fingerprint(
        {
            "schema_version": 1,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
        }
    )


def _batch119_snapshot_authority_matches(
    snapshot: Any,
    *,
    profile: CompositeBatchRecoveryProfile,
) -> bool:
    try:
        return _batch119_snapshot_authority_matches_unchecked(
            snapshot,
            profile=profile,
        )
    except (
        CompositeBatchRecoveryRefusal,
        AttributeError,
        KeyError,
        RecursionError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return False


def _batch119_snapshot_authority_matches_unchecked(
    snapshot: Any,
    *,
    profile: CompositeBatchRecoveryProfile,
) -> bool:
    from telegram_kol_research.deepcoin_snapshot_authority import (
        build_exchange_collection_evidence,
    )

    scope = getattr(snapshot, "exact_scope", None)
    if (
        not isinstance(scope, _Batch119ExactHistoryScope)
        or scope.instrument_id != profile.instrument_id
        or scope.side != profile.side
        or len(scope.protection_orders) != 2
        or {purpose for purpose, _ in scope.protection_orders}
        != {"stop_loss", "backup_stop"}
        or len({order_id for _, order_id in scope.protection_orders}) != 2
        or len(scope.protection_evidence_fingerprints) != 2
        or {purpose for purpose, _ in scope.protection_evidence_fingerprints}
        != {"stop_loss", "backup_stop"}
        or not all(
            _is_sha256(fingerprint)
            for _, fingerprint in scope.protection_evidence_fingerprints
        )
    ):
        return False
    expected_scope_fingerprint = _batch119_exact_scope_fingerprint(
        position_id=scope.position_id,
        protection_orders=scope.protection_orders,
        protection_evidence_fingerprints=(
            scope.protection_evidence_fingerprints
        ),
    )
    if (
        scope.scope_fingerprint != expected_scope_fingerprint
        or snapshot.scope_fingerprint != expected_scope_fingerprint
        or not _exact_position_rows_match_scope(
            snapshot.position_history,
            position_id=scope.position_id,
            instrument_id=scope.instrument_id,
            side=scope.side,
        )
    ):
        return False

    trigger_rows_by_order_id = {
        order_id: [] for _, order_id in scope.protection_orders
    }
    for row in snapshot.trigger_history:
        order_identity = _unique_exact_row_identity(
            row,
            keys=("ordId", "orderId", "order_id"),
        )
        if order_identity not in trigger_rows_by_order_id:
            return False
        trigger_rows_by_order_id[order_identity].append(row)
    for _, order_id in scope.protection_orders:
        if not _exact_order_rows_match_scope(
            trigger_rows_by_order_id[order_id],
            order_id=order_id,
            instrument_id=scope.instrument_id,
            side=scope.side,
        ):
            return False

    rows_by_endpoint: dict[str, Sequence[Mapping[str, Any]]] = {
        "positions": snapshot.positions,
        "open_orders": snapshot.open_orders,
        "pending_trigger_orders": snapshot.pending_trigger_orders,
        "position_history": snapshot.position_history,
        "order_history": snapshot.order_history,
        "trade_fills": snapshot.trade_fills,
    }
    for purpose, order_id in scope.protection_orders:
        rows_by_endpoint[f"trigger_history_{purpose}"] = (
            trigger_rows_by_order_id[order_id]
        )
    expected_inner_authority = []
    for endpoint, rows in rows_by_endpoint.items():
        evidence = build_exchange_collection_evidence(
            endpoint=endpoint,
            response={"data": list(rows)},
        )
        expected_inner_authority.append(
            {
                "endpoint": endpoint,
                "available": True,
                "schema_valid": True,
                "complete": True,
                "row_count": len(rows),
                "page_count": 1,
                "fingerprint": evidence.fingerprint,
                "reason_code": None,
            }
        )
    expected_inner_authority.sort(key=lambda row: str(row["endpoint"]))
    stored_inner_authority = getattr(snapshot, "collection_authority", None)
    if not isinstance(stored_inner_authority, (list, tuple)):
        return False
    try:
        normalized_stored_authority = sorted(
            (dict(row) for row in stored_inner_authority),
            key=lambda row: str(row.get("endpoint")),
        )
    except (TypeError, ValueError):
        return False
    if normalized_stored_authority != expected_inner_authority:
        return False

    authority = snapshot.account_authority
    authority_collections = getattr(authority, "collections", None)
    if not isinstance(authority_collections, (list, tuple)) or len(
        authority_collections
    ) != 1:
        return False
    expected_outer = build_exchange_collection_evidence(
        endpoint="batch119_exact_account_composite",
        response={
            "data": [
                {
                    "scope_fingerprint": expected_scope_fingerprint,
                    "capture_window_fingerprint": (
                        _batch119_capture_window_fingerprint(snapshot)
                    ),
                    "snapshot_collections_fingerprint": (
                        _batch119_snapshot_collections_fingerprint(snapshot)
                    ),
                    "collections": expected_inner_authority,
                }
            ]
        },
    )
    actual_outer = authority_collections[0]
    return bool(
        actual_outer.endpoint == "batch119_exact_account_composite"
        and actual_outer.available is True
        and actual_outer.schema_valid is True
        and actual_outer.complete is True
        and actual_outer.row_count == 1
        and actual_outer.page_count == 1
        and actual_outer.reason_code is None
        and actual_outer.fingerprint == expected_outer.fingerprint
    )


def _unique_exact_row_identity(
    row: Mapping[str, Any],
    *,
    keys: Sequence[str],
) -> str | None:
    identities = {
        str(row.get(key)).strip()
        for key in keys
        if row.get(key) not in (None, "")
    }
    return next(iter(identities)) if len(identities) == 1 else None


def authorize_composite_batch_recovery_resume(
    session_factory,
    *,
    expected_fingerprint: str,
    snapshot: Any,
    require_mimo_v1: bool = True,
) -> CompositeBatchRecoveryResumeAuthorization:
    """Authorize only a progressed state descended from the exact repair audit."""

    if not _is_sha256(expected_fingerprint) or not _snapshot_is_complete(
        snapshot,
        profile=BATCH_119_RECOVERY,
    ):
        raise CompositeBatchRecoveryConflict("resume_evidence_invalid")
    with session_factory() as session:
        try:
            _acquire_recovery_write_lock(session)
        except SQLAlchemyError as exc:
            raise CompositeBatchRecoveryConflict(
                "resume_source_state_conflict"
            ) from exc
        event = _load_recovery_audit_event(
            session,
            evidence_fingerprint=expected_fingerprint,
        )
        if event is None:
            raise CompositeBatchRecoveryConflict("resume_audit_missing")
        after = _validated_resume_audit_event(
            event,
            expected_fingerprint=expected_fingerprint,
        )
        if not require_mimo_v1:
            raise CompositeBatchRecoveryConflict(
                "mimo_contract_gate_missing"
            )
        _require_locked_mimo_v1(session)
        batch = session.get(
            StrategyManagementBatch,
            BATCH_119_RECOVERY.batch_id,
        )
        lifecycle = session.get(
            StrategyLifecycle,
            BATCH_119_RECOVERY.lifecycle_id,
        )
        raw = session.get(RawMessage, BATCH_119_RECOVERY.raw_message_id)
        if batch is None or lifecycle is None or raw is None:
            raise CompositeBatchRecoveryConflict("resume_source_state_conflict")
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        legs = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
            .all()
        )
        components = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
            .order_by(
                StrategyManagementComponent.sequence,
                StrategyManagementComponent.id,
            )
            .all()
        )
        if binding is None or len(legs) != 1 or len(components) != 3:
            raise CompositeBatchRecoveryConflict("resume_source_state_conflict")
        if int(event.execution_binding_id) != int(binding.id):
            raise CompositeBatchRecoveryConflict("resume_audit_invalid")
        leg = legs[0]
        entry = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
        if entry is None or _durable_identity_refusal(
            session=session,
            batch=batch,
            raw=raw,
            lifecycle=lifecycle,
            binding=binding,
            entry=entry,
            leg=leg,
            profile=BATCH_119_RECOVERY,
        ) is not None:
            raise CompositeBatchRecoveryConflict("resume_source_state_conflict")
        contract = _validated_contract(batch, profile=BATCH_119_RECOVERY)
        target = _validated_target_snapshot(
            batch,
            binding=binding,
            leg=leg,
            entry=entry,
            profile=BATCH_119_RECOVERY,
        )
        if isinstance(contract, str) or isinstance(target, str):
            raise CompositeBatchRecoveryConflict("resume_source_state_conflict")
        disposition = str(after["position_disposition"])
        if disposition == "position_absent" and _has_durable_close_submission(
            session,
            batch=batch,
            leg=leg,
            entry=entry,
        ):
            raise CompositeBatchRecoveryConflict(
                "resume_source_state_conflict"
            )
        original_owned_stop_refs = (
            []
            if disposition == "position_absent"
            else _original_owned_stop_refs(
                session,
                batch=batch,
                leg=leg,
                entry=entry,
                audit_created_at=event.created_at,
            )
        )
        if after["original_owned_stop_refs"] != original_owned_stop_refs:
            raise CompositeBatchRecoveryConflict("resume_audit_invalid")
        try:
            current_instruction_population = _instruction_population_summary(
                _instruction_population_payload(
                    session,
                    batch=batch,
                    profile=BATCH_119_RECOVERY,
                )
            )
        except CompositeBatchRecoveryRefusal as exc:
            raise CompositeBatchRecoveryConflict(
                "additional_active_work_present"
            ) from exc
        if (
            current_instruction_population
            != after.get("instruction_population")
        ):
            raise CompositeBatchRecoveryConflict(
                "additional_active_work_present"
            )
        exact_audited_after_state = (
            str(batch.status) == str(after["batch_status"])
            and str(leg.status) == str(after["leg_status"])
            and [str(row.status) for row in components]
            == list(after["component_statuses"])
            and [int(row.attempt_count) for row in components]
            == list(after["component_attempt_counts"])
        )
        if exact_audited_after_state and not (
            _recovery_audit_matches_current_durable_state(
                session,
                event=event,
                after=after,
                batch=batch,
                binding=binding,
                leg=leg,
                entry=entry,
                components=components,
                contract=contract,
                target=target,
            )
        ):
            raise CompositeBatchRecoveryConflict(
                "resume_source_state_conflict"
            )
        _validate_progressed_recovery_state(
            session,
            batch=batch,
            leg=leg,
            entry=entry,
            components=components,
            contract=contract,
            target=target,
            disposition=disposition,
            expected_fingerprint=expected_fingerprint,
            snapshot=snapshot,
            approved_current_size=after["current_size"],
            original_owned_stop_refs=original_owned_stop_refs,
        )
        try:
            durable_scope = _build_batch119_exact_history_scope_in_session(
                session
            )
        except CompositeBatchRecoveryRefusal as exc:
            raise CompositeBatchRecoveryConflict(
                "resume_source_state_conflict"
            ) from exc
        if (
            snapshot.exact_scope != durable_scope
            or durable_scope.scope_fingerprint
            != str(snapshot.scope_fingerprint)
        ):
            raise CompositeBatchRecoveryConflict(
                "resume_source_state_conflict"
            )
        if not _resume_exchange_close_evidence_is_owned(
            session,
            snapshot=snapshot,
            batch=batch,
            raw=raw,
            leg=leg,
            entry=entry,
            components=components,
        ):
            raise CompositeBatchRecoveryConflict(
                "resume_exchange_close_unowned"
            )
        position = _resume_position(
            disposition=disposition,
            current_size=after["current_size"],
        )
        if disposition == "position_absent":
            _validate_locked_exact_snapshot(
                session,
                snapshot=snapshot,
                expected_position=position,
                expected_exchange_fingerprint=str(
                    after["exchange_snapshot_fingerprint"]
                ),
                expected_natural_stop=None,
            )
        audit_event_id = int(event.id)
        source_fingerprint = str(after["source_fingerprint"])
        exchange_fingerprint = str(after["exchange_snapshot_fingerprint"])

    plan = CompositeBatchRecoveryPlan(
        batch_id=BATCH_119_RECOVERY.batch_id,
        status="ready",
        reason_code="audited_recovery_resume_authorized",
        position=position,
        source_fingerprint=source_fingerprint,
        exchange_snapshot_fingerprint=exchange_fingerprint,
        evidence_fingerprint=expected_fingerprint,
        evidence=MappingProxyType({}),
    )
    return CompositeBatchRecoveryResumeAuthorization(
        plan=plan,
        repair_result=CompositeBatchRecoveryApplyResult(
            batch_id=BATCH_119_RECOVERY.batch_id,
            status="already_repaired",
            evidence_fingerprint=expected_fingerprint,
            audit_event_id=audit_event_id,
        ),
    )


def _validated_resume_audit_event(
    event: ExecutionEvent,
    *,
    expected_fingerprint: str,
) -> Mapping[str, Any]:
    before = _safe_json_value(event.before_json)
    after = _safe_json_value(event.after_json)
    expected_after_keys = {
        "batch_id",
        "batch_status",
        "leg_status",
        "component_statuses",
        "component_attempt_counts",
        "source_fingerprint",
        "exchange_snapshot_fingerprint",
        "evidence_fingerprint",
        "position_disposition",
        "current_size",
        "target_remaining_size",
        "exchange_call_possible",
        "original_owned_stop_refs",
        "instruction_population",
    }
    expected_before_keys = {
        "batch_id",
        "batch_status",
        "leg_status",
        "component_statuses",
        "component_attempt_counts",
    }
    if (
        not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
        or set(before) != expected_before_keys
        or set(after) != expected_after_keys
        or event.before_json != _canonical_json(before)
        or event.after_json != _canonical_json(after)
        or before.get("batch_id") != BATCH_119_RECOVERY.batch_id
        or before.get("batch_status") != "reconciling"
        or before.get("leg_status") != "submitted"
        or before.get("component_statuses")
        != ["recovery_required", "pending", "pending"]
        or after.get("batch_id") != BATCH_119_RECOVERY.batch_id
        or after.get("evidence_fingerprint") != expected_fingerprint
        or after.get("target_remaining_size")
        != BATCH_119_RECOVERY.target_remaining_size
        or after.get("exchange_call_possible") is not False
        or not _is_sha256(after.get("source_fingerprint"))
        or not _is_sha256(after.get("exchange_snapshot_fingerprint"))
        or not _instruction_population_summary_is_valid(
            after.get("instruction_population")
        )
        or event.action != _RECOVERY_AUDIT_ACTION
        or event.status != "resolved"
        or event.notification_fingerprint != expected_fingerprint
        or event.venue != "deepcoin"
        or event.execution_binding_id is None
    ):
        raise CompositeBatchRecoveryConflict("resume_audit_invalid")
    disposition = str(after.get("position_disposition") or "")
    position_absent = disposition == "position_absent"
    expected_reason = (
        "composite_recovery_exact_position_absent"
        if position_absent
        else _RECOVERY_REASON
    )
    if (
        disposition
        not in {
            "resume_to_target",
            "protection_only_at_target",
            "protection_only_below_target",
            "position_absent",
        }
        or after.get("batch_status")
        != ("resolved" if position_absent else "ready")
        or after.get("leg_status")
        != ("failed" if position_absent else "planned")
        or after.get("component_statuses")
        != (
            ["safely_skipped"] * 3
            if position_absent
            else ["recovery_required", "pending", "pending"]
        )
        or event.reason != expected_reason
        or not _valid_original_owned_stop_refs(
            after.get("original_owned_stop_refs"),
            position_absent=position_absent,
        )
        or any(
            getattr(event, field) is not None
            for field in (
                "trade_signal_id",
                "strategy_instance_id",
                "kol_id",
                "chat_id",
                "message_id",
                "source_message_id",
                "symbol",
                "side",
                "order_id",
                "client_order_id",
                "pos_id",
                "related_order_id",
                "request_json",
                "response_json",
                "exchange_event_time",
                "notification_status",
                "notification_error",
                "notification_next_attempt_at",
                "notification_claim_token",
                "notification_claimed_at",
                "notified_at",
            )
        )
        or int(event.notification_attempts or 0) != 0
    ):
        raise CompositeBatchRecoveryConflict("resume_audit_invalid")
    attempts = before.get("component_attempt_counts")
    after_attempts = after.get("component_attempt_counts")
    if (
        not isinstance(attempts, list)
        or attempts != after_attempts
        or len(attempts) != 3
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in attempts
        )
    ):
        raise CompositeBatchRecoveryConflict("resume_audit_invalid")
    return after


def _validate_progressed_recovery_state(
    session,
    *,
    batch,
    leg,
    entry,
    components,
    contract,
    target,
    disposition: str,
    expected_fingerprint: str,
    snapshot: Any,
    approved_current_size: Any,
    original_owned_stop_refs: list[str],
) -> None:
    position_absent = disposition == "position_absent"
    if str(batch.status) not in (
        {"resolved"} if position_absent else {"ready", "executing", "succeeded"}
    ) or str(leg.status) != ("failed" if position_absent else "planned"):
        raise CompositeBatchRecoveryConflict("resume_state_not_executable")
    expected_reason = (
        "composite_recovery_exact_position_absent"
        if position_absent
        else _RECOVERY_REASON
    )
    if _safe_json_value(leg.last_error) != {
        "reason": expected_reason,
        "recovery_evidence_fingerprint": expected_fingerprint,
    }:
        raise CompositeBatchRecoveryConflict("resume_state_not_executable")
    allowed_statuses = {
        "pending",
        "preflighting",
        "submitting",
        "awaiting_exchange",
        "confirmed",
        "definitely_rejected",
        "recovery_required",
        "operator_required",
        "safely_skipped",
    }
    statuses = [str(row.status) for row in components]
    if not _resume_component_status_order_is_valid(
        batch_status=str(batch.status),
        statuses=statuses,
        position_absent=position_absent,
    ):
        raise CompositeBatchRecoveryConflict("resume_component_conflict")
    mutable_keys = {
        "consume_take_profit_stage": "take_profit_consumption_execution",
        "converge_partial_close": "partial_close_execution",
        "replace_remaining_protection": "protection_replacement_execution",
    }
    for sequence, (component, kind) in enumerate(
        zip(components, _EXPECTED_COMPONENTS, strict=True)
    ):
        if (
            int(component.management_batch_id) != BATCH_119_RECOVERY.batch_id
            or int(component.strategy_management_leg_id or 0) != int(leg.id)
            or int(component.strategy_management_leg_scope) != int(leg.id)
            or int(component.sequence) != sequence
            or str(component.component_kind) != kind
            or str(component.status) not in allowed_statuses
        ):
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        desired = _safe_json_value(component.desired_json)
        evidence = _safe_json_value(component.evidence_json)
        if not isinstance(desired, Mapping) or not isinstance(evidence, list):
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        expected = {
            "contract_fingerprint": str(batch.management_contract_fingerprint),
            "pos_id": str(leg.pos_id),
            "execution_order_leg_id": int(entry.id),
            "trusted_start_size": str(target["trusted_start_size"]),
            "target_remaining_size": str(target["target_remaining_size"]),
            "avg_entry_price": str(target["avg_entry_price"]),
            "quantity_step": str(target["quantity_step"]),
            "min_quantity": str(target["min_quantity"]),
            "component_kind": kind,
        }
        desired_copy = dict(desired)
        execution = desired_copy.pop(mutable_keys[kind], None)
        if desired_copy != expected or (
            execution is not None and not isinstance(execution, Mapping)
        ):
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        if not _resume_component_execution_matches_locked_state(
            session,
            batch=batch,
            leg=leg,
            entry=entry,
            component=component,
            kind=kind,
            execution=execution,
            target=target,
            original_owned_stop_refs=original_owned_stop_refs,
        ):
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        expected_key = hashlib.sha256(
            (
                f"{batch.management_contract_fingerprint}|{batch.id}|"
                f"{leg.id}|{kind}"
            ).encode("utf-8")
        ).hexdigest()
        if str(component.idempotency_key) != expected_key:
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        try:
            _fingerprint(evidence)
        except (TypeError, ValueError, RecursionError, OverflowError):
            raise CompositeBatchRecoveryConflict(
                "resume_component_conflict"
            ) from None
        if str(component.status) == "confirmed" and not (
            _resume_confirmed_component_matches_locked_state(
                session,
                batch=batch,
                leg=leg,
                entry=entry,
                component=component,
                kind=kind,
                contract=contract,
                execution=execution,
                evidence=evidence,
                snapshot=snapshot,
                target=target,
                disposition=disposition,
                approved_current_size=approved_current_size,
            )
        ):
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        if disposition == "protection_only_below_target":
            attestations = [
                row
                for row in evidence
                if isinstance(row, Mapping)
                and row.get("kind") == "approved_under_target_recovery"
                and row.get("recovery_evidence_fingerprint")
                == expected_fingerprint
            ]
            if len(attestations) != 1:
                raise CompositeBatchRecoveryConflict(
                    "resume_component_conflict"
                )
    own_prefixes = tuple(f"{int(row.id)}:" for row in components)
    for intent in session.query(PositionMutationIntent).filter(
        PositionMutationIntent.status.notin_(_TERMINAL_MUTATION_STATUSES)
    ):
        if not str(intent.idempotency_key or "").startswith(own_prefixes):
            raise CompositeBatchRecoveryConflict("additional_active_work_present")
    try:
        _instruction_population_payload(
            session,
            batch=batch,
            profile=BATCH_119_RECOVERY,
        )
    except CompositeBatchRecoveryRefusal as exc:
        raise CompositeBatchRecoveryConflict(
            "additional_active_work_present"
        ) from exc
    if (
        session.query(StrategyManagementBatch.id)
        .filter(
            StrategyManagementBatch.id != BATCH_119_RECOVERY.batch_id,
            StrategyManagementBatch.status.notin_(
                _SAFE_TERMINAL_MANAGEMENT_STATUSES
            ),
        )
        .first()
        is not None
        or session.query(StrategyManagementComponent.id)
        .filter(
            StrategyManagementComponent.management_batch_id
            != BATCH_119_RECOVERY.batch_id,
            StrategyManagementComponent.status.notin_(
                _SAFE_TERMINAL_COMPONENT_STATUSES
            ),
        )
        .first()
        is not None
    ):
        raise CompositeBatchRecoveryConflict("additional_active_work_present")


def _resume_component_status_order_is_valid(
    *,
    batch_status: str,
    statuses: list[str],
    position_absent: bool,
) -> bool:
    if position_absent:
        return batch_status == "resolved" and statuses == [
            "safely_skipped",
            "safely_skipped",
            "safely_skipped",
        ]
    executable = {
        "pending",
        "preflighting",
        "submitting",
        "awaiting_exchange",
        "definitely_rejected",
        "recovery_required",
    }
    if len(statuses) != 3 or any(
        status not in executable | {"confirmed"} for status in statuses
    ):
        return False
    confirmed_count = 0
    for status in statuses:
        if status != "confirmed":
            break
        confirmed_count += 1
    if any(status == "confirmed" for status in statuses[confirmed_count:]):
        return False
    if any(status != "pending" for status in statuses[confirmed_count + 1 :]):
        return False
    if batch_status == "succeeded":
        return confirmed_count == 3
    if batch_status == "ready":
        return confirmed_count == 0 and statuses == [
            "recovery_required",
            "pending",
            "pending",
        ]
    return batch_status == "executing"


def _resume_confirmed_component_matches_locked_state(
    session,
    *,
    batch,
    leg,
    entry,
    component,
    kind: str,
    contract,
    execution: Any,
    evidence: list[Any],
    snapshot: Any,
    target: Mapping[str, Any],
    disposition: str,
    approved_current_size: Any,
) -> bool:
    if component.completed_at is None or int(component.attempt_count or 0) <= 0:
        return False
    intents = [
        row
        for row in session.query(PositionMutationIntent).all()
        if str(row.idempotency_key or "").startswith(f"{int(component.id)}:")
    ]
    if kind == "consume_take_profit_stage":
        return _resume_confirmed_take_profit_matches(
            session,
            batch=batch,
            leg=leg,
            entry=entry,
            component=component,
            contract=contract,
            execution=execution,
            evidence=evidence,
            intents=intents,
            snapshot=snapshot,
            target=target,
            approved_current_size=approved_current_size,
        )
    if kind == "converge_partial_close":
        return _resume_confirmed_close_matches(
            leg=leg,
            component=component,
            execution=execution,
            evidence=evidence,
            intents=intents,
            snapshot=snapshot,
            target=target,
            disposition=disposition,
            approved_current_size=approved_current_size,
        )
    return _resume_confirmed_protection_matches(
        session,
        batch=batch,
        leg=leg,
        entry=entry,
        component=component,
        execution=execution,
        evidence=evidence,
        intents=intents,
        snapshot=snapshot,
    )


def _resume_confirmed_take_profit_matches(
    session,
    *,
    batch,
    leg,
    entry,
    component,
    contract,
    execution: Any,
    evidence: list[Any],
    intents: list[Any],
    snapshot: Any,
    target: Mapping[str, Any],
    approved_current_size: Any,
) -> bool:
    if execution is None:
        if intents or len(evidence) < 2:
            return False
        phase = evidence[-2]
        result = evidence[-1]
        quantity = (
            _decimal_or_none(result.get("proven_filled_quantity"))
            if isinstance(result, Mapping)
            else None
        )
        if not (
            isinstance(phase, Mapping)
            and set(phase) == {"phase", "evidence_tier"}
            and phase.get("phase") == "no_cancel_required"
            and phase.get("evidence_tier")
            in {"exact_terminal_fill", "exact_terminal_no_fill"}
            and isinstance(result, Mapping)
            and set(result) == {"proven_filled_quantity"}
            and quantity is not None
            and quantity >= 0
        ):
            return False
        ledger_rows = session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.execution_binding_id
            == int(batch.execution_binding_id),
            PositionProtectionLedger.execution_order_leg_id == int(entry.id),
            PositionProtectionLedger.pos_id == str(leg.pos_id),
            PositionProtectionLedger.purpose == "take_profit",
        ).all()
        target_remaining = _decimal_or_none(
            target.get("target_remaining_size")
        )
        approved_current = _decimal_or_none(approved_current_size)
        if target_remaining is None or approved_current is None:
            return False
        effective_remaining = min(target_remaining, approved_current)
        try:
            rebuilt = plan_take_profit_consumption(
                contract=contract,
                target_leg={
                    "execution_binding_id": int(batch.execution_binding_id),
                    "execution_order_leg_id": int(entry.id),
                    "pos_id": str(leg.pos_id),
                    "instrument_id": BATCH_119_RECOVERY.instrument_id,
                    "side": BATCH_119_RECOVERY.side,
                },
                pending_orders=snapshot.pending_trigger_orders,
                trigger_history=snapshot.trigger_history,
                order_history=snapshot.order_history,
                trade_fills=snapshot.trade_fills,
                protection_ledger=ledger_rows,
                trusted_start_size=str(target["trusted_start_size"]),
                target_remaining_size=_decimal_text(effective_remaining),
            )
        except (TypeError, ValueError, RecursionError, OverflowError):
            return False
        return (
            rebuilt.refusal_code is None
            and not rebuilt.cancel_order_ids
            and not rebuilt.cancel_actions
            and rebuilt.evidence_tier == phase.get("evidence_tier")
            and rebuilt.proven_filled_quantity
            == str(result["proven_filled_quantity"])
        )
    order_ids = list(execution["cancel_order_ids"])
    intent_ids = list(execution["cancel_intent_ids"])
    selected = {int(row.id): row for row in intents}
    if set(selected) != set(intent_ids) or any(
        str(row.status) != "confirmed"
        or _safe_json_value(row.request_json)
        != {
            "instId": BATCH_119_RECOVERY.instrument_id,
            "instType": "SWAP",
            "ordId": str(row.order_id),
        }
        for row in selected.values()
    ):
        return False
    first_fact = {
        "cancel_order_ids": order_ids,
        "intent_id": intent_ids[0],
    }
    if first_fact not in evidence:
        return False
    terminal_fact = evidence[-1] if evidence else None
    terminal_valid = (
        isinstance(terminal_fact, Mapping)
        and set(terminal_fact) in (
            {"intent_id"},
            {"intent_id", "fill_race"},
        )
        and int(terminal_fact.get("intent_id") or 0) in set(intent_ids)
        and (
            "fill_race" not in terminal_fact
            or terminal_fact.get("fill_race") is True
        )
    ) or str(component.reason_code) == "take_profit_cancel_exchange_confirmed"
    if not terminal_valid:
        return False
    ledgers = session.query(PositionProtectionLedger).filter(
        PositionProtectionLedger.order_id.in_(order_ids)
    ).all()
    pending_ids = _snapshot_order_ids(snapshot.pending_trigger_orders)
    return len(ledgers) == len(order_ids) and all(
        str(row.venue) == "deepcoin"
        and int(row.execution_binding_id) == int(batch.execution_binding_id)
        and int(row.execution_order_leg_id) == int(entry.id)
        and str(row.pos_id) == str(leg.pos_id)
        and str(row.status) == "cancelled"
        for row in ledgers
    ) and not set(order_ids).intersection(pending_ids)


def _resume_confirmed_close_matches(
    *,
    leg,
    component,
    execution: Any,
    evidence: list[Any],
    intents: list[Any],
    snapshot: Any,
    target: Mapping[str, Any],
    disposition: str,
    approved_current_size: Any,
) -> bool:
    expected_remaining = (
        _decimal_or_none(approved_current_size)
        if disposition == "protection_only_below_target"
        else _decimal_or_none(target.get("target_remaining_size"))
    )
    live_remaining = _snapshot_exact_position_size(
        snapshot,
        pos_id=str(leg.pos_id),
    )
    if (
        expected_remaining is None
        or live_remaining is None
        or live_remaining != expected_remaining
    ):
        return False
    if execution is None:
        if intents or len(evidence) < 2:
            return False
        plan_fact = evidence[-2]
        terminal_fact = evidence[-1]
        return bool(
            isinstance(plan_fact, Mapping)
            and plan_fact == {"close_delta": "0"}
            and isinstance(terminal_fact, Mapping)
            and terminal_fact.get("remaining_size")
            == _decimal_text(expected_remaining)
            and terminal_fact.get("evidence_tier")
            in {
                "exact_position_target",
                "approved_under_target_recovery",
            }
        )
    intent_id = int(execution["intent_id"])
    if len(intents) != 1 or int(intents[0].id) != intent_id:
        return False
    intent = intents[0]
    if str(intent.status) != "confirmed":
        return False
    plan_fact = {
        "close_delta": str(execution["close_delta"]),
        "intent_id": intent_id,
    }
    if plan_fact not in evidence:
        return False
    terminal_fact = evidence[-1] if evidence else None
    return bool(
        isinstance(terminal_fact, Mapping)
        and int(terminal_fact.get("intent_id") or 0) == intent_id
        and (
            (
                terminal_fact.get("remaining_size")
                == _decimal_text(expected_remaining)
                and terminal_fact.get("evidence_tier")
                == "exact_position_target"
            )
            or terminal_fact.get("unresolved_delta") == "0"
        )
    )


def _resume_confirmed_protection_matches(
    session,
    *,
    batch,
    leg,
    entry,
    component,
    execution: Any,
    evidence: list[Any],
    intents: list[Any],
    snapshot: Any,
) -> bool:
    if not isinstance(execution, Mapping) or len(evidence) < 2:
        return False
    old_ids = list(execution["old_stop_order_ids"])
    terminal = evidence[-1]
    if (
        not isinstance(terminal, Mapping)
        or set(terminal) != {
            "new_stop_order_ids",
            "cancelled_old_stop_order_ids",
            "retained_take_profit_total",
            "effective_remaining_size",
        }
        or not _unique_nonempty_strings(terminal.get("new_stop_order_ids"))
        or len(terminal["new_stop_order_ids"]) != 2
        or terminal.get("cancelled_old_stop_order_ids") != sorted(old_ids)
        or terminal.get("retained_take_profit_total")
        != execution.get("retained_take_profit_total")
        or terminal.get("effective_remaining_size")
        != execution.get("effective_remaining_size")
    ):
        return False
    new_ids = list(terminal["new_stop_order_ids"])
    by_key = {str(row.idempotency_key): row for row in intents}
    expected_keys = {
        f"{int(component.id)}:set:primary",
        f"{int(component.id)}:set:backup",
        *(f"{int(component.id)}:cancel-old:{order_id}" for order_id in old_ids),
    }
    if set(by_key) != expected_keys or any(
        str(row.status) != "confirmed" for row in by_key.values()
    ):
        return False
    binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
    if binding is None:
        return False
    set_intents = [
        by_key[f"{int(component.id)}:set:{role}"]
        for role in ("primary", "backup")
    ]
    if {str(row.order_id or "") for row in set_intents} != set(new_ids):
        return False
    for old_order_id in old_ids:
        cancel_intent = by_key[
            f"{int(component.id)}:cancel-old:{old_order_id}"
        ]
        if _safe_json_value(cancel_intent.request_json) != {
            "instId": BATCH_119_RECOVERY.instrument_id,
            "instType": "SWAP",
            "ordId": old_order_id,
        }:
            return False
    old_ledgers = session.query(PositionProtectionLedger).filter(
        PositionProtectionLedger.order_id.in_(old_ids)
    ).all()
    new_ledgers = session.query(PositionProtectionLedger).filter(
        PositionProtectionLedger.order_id.in_(new_ids)
    ).all()
    if len(old_ledgers) != len(old_ids) or len(new_ledgers) != 2:
        return False
    if any(
        str(row.status) != "cancelled"
        or int(row.execution_binding_id) != int(batch.execution_binding_id)
        or int(row.execution_order_leg_id) != int(entry.id)
        or str(row.pos_id) != str(leg.pos_id)
        for row in old_ledgers
    ):
        return False
    expected_prices = {
        "stop_loss": _decimal_or_none(execution.get("primary_stop")),
        "backup_stop": _decimal_or_none(execution.get("backup_stop")),
    }
    expected_size = _decimal_or_none(execution.get("effective_remaining_size"))
    if expected_size is None or any(
        str(row.status) != "verified"
        or int(row.execution_binding_id) != int(batch.execution_binding_id)
        or int(row.execution_order_leg_id) != int(entry.id)
        or str(row.pos_id) != str(leg.pos_id)
        or str(row.instrument_id).upper() != BATCH_119_RECOVERY.instrument_id
        or str(row.side).lower() != BATCH_119_RECOVERY.side
        or str(row.purpose) not in expected_prices
        or _decimal_or_none(row.trigger_price)
        != expected_prices[str(row.purpose)]
        or _decimal_or_none(row.size_text) != expected_size
        for row in new_ledgers
    ):
        return False
    pending_rows = {
        order_id: [
            row
            for row in snapshot.pending_trigger_orders
            if isinstance(row, Mapping)
            and str(
                row.get("ordId")
                or row.get("orderId")
                or row.get("order_id")
                or ""
            )
            == order_id
        ]
        for order_id in new_ids
    }
    pending = {
        order_id: rows[0]
        for order_id, rows in pending_rows.items()
        if len(rows) == 1
    }
    if (
        any(len(rows) != 1 for rows in pending_rows.values())
        or set(old_ids).intersection(
            _snapshot_order_ids(snapshot.pending_trigger_orders)
        )
    ):
        return False
    ledger_by_id = {str(row.order_id): row for row in new_ledgers}
    for role, purpose in (("primary", "stop_loss"), ("backup", "backup_stop")):
        intent = by_key[f"{int(component.id)}:set:{role}"]
        order_id = str(intent.order_id or "")
        ledger = ledger_by_id.get(order_id)
        pending_row = pending.get(order_id)
        request = _safe_json_value(intent.request_json)
        if (
            ledger is None
            or pending_row is None
            or not isinstance(request, Mapping)
            or set(request) != {
                "_ledger_purpose",
                "instId",
                "instType",
                "mrgPosition",
                "posId",
                "posSide",
                "slOrdPx",
                "slTriggerPx",
                "slTriggerPxType",
                "sz",
                "tdMode",
            }
            or request.get("_ledger_purpose") != purpose
            or request.get("instId") != BATCH_119_RECOVERY.instrument_id
            or request.get("instType") != "SWAP"
            or str(request.get("mrgPosition") or "").lower()
            != str(binding.position_mode).lower()
            or request.get("posId") != str(leg.pos_id)
            or str(request.get("posSide") or "").lower()
            != BATCH_119_RECOVERY.side
            or request.get("slOrdPx") != "-1"
            or request.get("slTriggerPxType") != "last"
            or _decimal_or_none(request.get("slTriggerPx"))
            != expected_prices[purpose]
            or _decimal_or_none(request.get("sz")) != expected_size
            or str(request.get("tdMode") or "").lower()
            != str(binding.margin_mode).lower()
            or str(ledger.purpose) != purpose
            or _decimal_or_none(ledger.trigger_price)
            != expected_prices[purpose]
            or str(pending_row.get("posId") or pending_row.get("pos_id") or "")
            != str(leg.pos_id)
            or str(
                pending_row.get("instId")
                or pending_row.get("instrument_id")
                or ""
            ).upper()
            != BATCH_119_RECOVERY.instrument_id
            or str(
                pending_row.get("posSide")
                or pending_row.get("side")
                or ""
            ).lower()
            != BATCH_119_RECOVERY.side
            or _decimal_or_none(
                pending_row.get("sz") or pending_row.get("size")
            )
            != expected_size
            or _decimal_or_none(
                pending_row.get("slTriggerPx")
                or pending_row.get("slTriggerPrice")
                or pending_row.get("trigger_price")
            )
            != expected_prices[purpose]
        ):
            return False
    return len(pending) == 2


def _snapshot_order_ids(rows: Any) -> set[str]:
    return {
        order_id
        for row in rows
        if isinstance(row, Mapping)
        and (
            order_id := str(
                row.get("ordId")
                or row.get("orderId")
                or row.get("order_id")
                or ""
            )
        )
    }


def _snapshot_exact_position_size(snapshot: Any, *, pos_id: str) -> Decimal | None:
    matches = [
        row
        for row in snapshot.positions
        if isinstance(row, Mapping)
        and str(row.get("posId") or row.get("pos_id") or "") == pos_id
    ]
    return (
        _decimal_or_none(matches[0].get("pos") or matches[0].get("size"))
        if len(matches) == 1
        else None
    )


def _resume_component_execution_matches_locked_state(
    session,
    *,
    batch,
    leg,
    entry,
    component,
    kind: str,
    execution: Any,
    target: Mapping[str, Any],
    original_owned_stop_refs: list[str],
) -> bool:
    intents = [
        row
        for row in session.query(PositionMutationIntent).all()
        if str(row.idempotency_key or "").startswith(f"{int(component.id)}:")
    ]
    try:
        if any(
            not isinstance((request := _safe_json_value(row.request_json)), Mapping)
            or not _is_sha256(row.request_fingerprint)
            or _fingerprint(
                {
                    key: value
                    for key, value in request.items()
                    if key != "_ledger_purpose"
                }
            )
            != str(row.request_fingerprint)
            for row in intents
        ):
            return False
    except (TypeError, ValueError, RecursionError, OverflowError):
        return False
    expected_operations = {
        "consume_take_profit_stage": {"cancel_position_sltp"},
        "converge_partial_close": {"close_position"},
        "replace_remaining_protection": {
            "set_position_sltp",
            "cancel_position_sltp",
        },
    }[kind]
    if any(
        str(intent.operation) not in expected_operations
        or str(intent.venue) != "deepcoin"
        or str(intent.strategy_instance_id) != str(batch.strategy_instance_id)
        or int(intent.execution_binding_id) != int(batch.execution_binding_id)
        or int(intent.execution_order_leg_id) != int(entry.id)
        or str(intent.pos_id) != str(leg.pos_id)
        for intent in intents
    ):
        return False
    if execution is None:
        return not intents
    if kind == "consume_take_profit_stage":
        if set(execution) != {
            "cancel_order_ids",
            "cancel_intent_ids",
            "evidence_tier",
        }:
            return False
        order_ids = execution.get("cancel_order_ids")
        intent_ids = execution.get("cancel_intent_ids")
        if (
            not _unique_nonempty_strings(order_ids)
            or not _unique_positive_ints(intent_ids)
            or not isinstance(execution.get("evidence_tier"), str)
            or execution.get("evidence_tier")
            not in {
                "exact_pending_owned_order",
                "exact_terminal_fill",
                "exact_terminal_no_fill",
                "none",
            }
        ):
            return False
        selected = [row for row in intents if int(row.id) in set(intent_ids)]
        return (
            len(selected) == len(intent_ids)
            and len(selected) == len(intents)
            and all(
                str(row.operation) == "cancel_position_sltp"
                and str(row.order_id or "") in set(order_ids)
                and str(row.idempotency_key).startswith(
                    f"{int(component.id)}:cancel:{str(row.order_id)}:attempt:"
                )
                for row in selected
            )
        )
    if kind == "converge_partial_close":
        if set(execution) != {
            "close_delta",
            "client_order_id",
            "intent_id",
            "pre_submit_size",
        } or not _unique_positive_ints([execution.get("intent_id")]):
            return False
        selected = [
            row for row in intents
            if int(row.id) == int(execution["intent_id"])
        ]
        if len(selected) != 1 or len(intents) != 1:
            return False
        intent = selected[0]
        request = _safe_json_value(intent.request_json)
        binding = session.get(
            ExecutionBinding,
            int(batch.execution_binding_id),
        )
        close_delta = _decimal_or_none(execution.get("close_delta"))
        pre_submit_size = _decimal_or_none(execution.get("pre_submit_size"))
        target_remaining = _decimal_or_none(target.get("target_remaining_size"))
        return bool(
            isinstance(request, Mapping)
            and binding is not None
            and set(request) == {
                "clOrdId",
                "closePosId",
                "instId",
                "mrgPosition",
                "ordType",
                "posSide",
                "side",
                "sz",
                "tdMode",
            }
            and close_delta is not None
            and close_delta > 0
            and pre_submit_size is not None
            and target_remaining is not None
            and pre_submit_size - close_delta == target_remaining
            and str(intent.operation) == "close_position"
            and str(intent.idempotency_key).startswith(
                f"{int(component.id)}:close:attempt:"
            )
            and str(request.get("clOrdId") or "")
            == str(execution.get("client_order_id") or "")
            and _decimal_or_none(request.get("sz")) == close_delta
            and str(request.get("closePosId") or "") == str(leg.pos_id)
            and str(request.get("instId") or "").upper()
            == BATCH_119_RECOVERY.instrument_id
            and str(request.get("posSide") or "").lower()
            == BATCH_119_RECOVERY.side
            and str(request.get("side") or "").lower() == "sell"
            and str(request.get("ordType") or "").lower() == "market"
            and str(request.get("mrgPosition") or "").lower()
            == str(binding.position_mode).lower()
            and str(request.get("tdMode") or "").lower()
            == str(binding.margin_mode).lower()
        )
    if set(execution) != {
        "primary_stop",
        "backup_stop",
        "old_stop_order_ids",
        "retained_take_profit_total",
        "effective_remaining_size",
    }:
        return False
    old_order_ids = execution.get("old_stop_order_ids")
    if sorted(
        _redacted_ref("protection_order", order_id)
        for order_id in (old_order_ids or [])
    ) != original_owned_stop_refs:
        return False
    primary = _decimal_or_none(execution.get("primary_stop"))
    backup = _decimal_or_none(execution.get("backup_stop"))
    effective = _decimal_or_none(execution.get("effective_remaining_size"))
    retained = _decimal_or_none(execution.get("retained_take_profit_total"))
    target_remaining = _decimal_or_none(target.get("target_remaining_size"))
    if (
        not _unique_nonempty_strings(old_order_ids, allow_empty=True)
        or primary is None
        or backup is None
        or primary <= 0
        or backup <= 0
        or primary == backup
        or effective is None
        or target_remaining is None
        or effective <= 0
        or effective > target_remaining
        or retained is None
        or retained < 0
        or retained > effective
    ):
        return False
    ledger_rows = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.order_id.in_(list(old_order_ids)))
        .all()
        if old_order_ids
        else []
    )
    return len(ledger_rows) == len(old_order_ids) and all(
        str(row.venue) == "deepcoin"
        and int(row.execution_binding_id) == int(batch.execution_binding_id)
        and int(row.execution_order_leg_id) == int(entry.id)
        and str(row.pos_id) == str(leg.pos_id)
        and str(row.instrument_id).upper() == BATCH_119_RECOVERY.instrument_id
        and str(row.side).lower() == BATCH_119_RECOVERY.side
        and str(row.purpose) in {"stop_loss", "backup_stop"}
        for row in ledger_rows
    )


def _unique_nonempty_strings(value: Any, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and bool(item) for item in value)
        and len(set(value)) == len(value)
    )


def _unique_positive_ints(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            not isinstance(item, bool) and isinstance(item, int) and item > 0
            for item in value
        )
        and len(set(value)) == len(value)
    )


def _resume_exchange_close_evidence_is_owned(
    session,
    *,
    snapshot: Any,
    batch,
    raw,
    leg,
    entry,
    components,
) -> bool:
    if (
        str(batch.status) == "resolved"
        and str(leg.status) == "failed"
        and [str(row.status) for row in components]
        == ["safely_skipped", "safely_skipped", "safely_skipped"]
    ):
        ledger = (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.execution_binding_id
                == int(batch.execution_binding_id),
                PositionProtectionLedger.execution_order_leg_id
                == int(entry.id),
                PositionProtectionLedger.pos_id == str(leg.pos_id),
            )
            .order_by(PositionProtectionLedger.id)
            .all()
        )
        return not isinstance(
            _validated_natural_stop_proof(
                snapshot=snapshot,
                ledger=ledger,
                pos_id=str(leg.pos_id),
                profile=BATCH_119_RECOVERY,
                not_before_ms=_incident_not_before_ms(
                    batch=batch,
                    leg=leg,
                    raw=raw,
                ),
            ),
            str,
        )
    close_rows = _exchange_close_rows(snapshot, pos_id=str(leg.pos_id))
    if not close_rows:
        return True
    close_component = next(
        row
        for row in components
        if str(row.component_kind) == "converge_partial_close"
    )
    intents = [
        row
        for row in session.query(PositionMutationIntent).filter_by(
            execution_binding_id=int(batch.execution_binding_id),
            execution_order_leg_id=int(entry.id),
            pos_id=str(leg.pos_id),
            operation="close_position",
        )
        if str(row.idempotency_key or "").startswith(
            f"{int(close_component.id)}:close:attempt:"
        )
    ]
    order_ids: set[str] = set()
    client_order_ids: set[str] = set()
    for intent in intents:
        request = _safe_json_value(intent.request_json)
        response = _safe_json_value(intent.response_json)
        if not isinstance(request, Mapping):
            return False
        client_order_id = str(request.get("clOrdId") or "")
        if client_order_id:
            client_order_ids.add(client_order_id)
        order_id = str(intent.order_id or "")
        if isinstance(response, Mapping):
            data = response.get("data")
            if isinstance(data, Mapping):
                order_id = order_id or str(data.get("ordId") or "")
            order_id = order_id or str(response.get("ordId") or "")
        if order_id:
            order_ids.add(order_id)
    return bool(intents) and all(
        str(
            row.get("ordId")
            or row.get("orderId")
            or row.get("order_id")
            or ""
        )
        in order_ids
        or str(
            row.get("clOrdId")
            or row.get("clientOrderId")
            or row.get("client_order_id")
            or ""
        )
        in client_order_ids
        for row in close_rows
    )


def _exchange_close_rows(snapshot: Any, *, pos_id: str) -> list[Mapping]:
    rows: list[Mapping] = []
    for row in snapshot.position_history:
        if not isinstance(row, Mapping) or not _row_matches_position(
            row, pos_id=pos_id
        ):
            continue
        state = str(row.get("state") or row.get("status") or "").lower()
        close_size = _decimal_or_none(
            row.get("closeSz")
            or row.get("closedSize")
            or row.get("close_size")
        )
        if _row_matches_close_position(row, pos_id=pos_id) or state in {
            "closed",
            "filled",
            "completed",
            "exited",
        } or (close_size is not None and close_size > 0):
            rows.append(row)
    for field in ("open_orders", "order_history", "trade_fills"):
        for row in getattr(snapshot, field):
            if not isinstance(row, Mapping) or not _row_matches_position(
                row, pos_id=pos_id
            ):
                continue
            reduce_only = str(
                row.get("reduceOnly") or row.get("reduce_only") or ""
            ).lower() in {"true", "1", "yes"}
            if (
                _row_matches_close_position(row, pos_id=pos_id)
                or reduce_only
                or str(row.get("side") or "").lower() == "sell"
            ):
                rows.append(row)
    for row in snapshot.trigger_history:
        if not isinstance(row, Mapping) or not _row_matches_position(
            row, pos_id=pos_id
        ):
            continue
        state = str(row.get("state") or row.get("status") or "").lower()
        reduce_only = str(
            row.get("reduceOnly") or row.get("reduce_only") or ""
        ).lower() in {"true", "1", "yes"}
        if _row_matches_close_position(row, pos_id=pos_id) or (
            state in {"filled", "triggered", "completed"}
            and (reduce_only or str(row.get("side") or "").lower() == "sell")
        ):
            rows.append(row)
    return rows


def _resume_position(
    *,
    disposition: str,
    current_size: Any,
) -> CompositeRecoveryPosition:
    if disposition == "position_absent":
        if current_size is not None:
            raise CompositeBatchRecoveryConflict("resume_audit_invalid")
        return CompositeRecoveryPosition(
            disposition="position_absent",
            current_size=None,
            close_delta="0",
            effective_remaining_size="0",
        )
    current = _decimal_or_none(current_size)
    target = Decimal(BATCH_119_RECOVERY.target_remaining_size)
    if current is None or current <= 0:
        raise CompositeBatchRecoveryConflict("resume_audit_invalid")
    expected_relation = (
        "resume_to_target"
        if current > target
        else "protection_only_at_target"
        if current == target
        else "protection_only_below_target"
    )
    if disposition != expected_relation:
        raise CompositeBatchRecoveryConflict("resume_audit_invalid")
    return CompositeRecoveryPosition(
        disposition=disposition,
        current_size=_decimal_text(current),
        close_delta=(
            _decimal_text(current - target)
            if disposition == "resume_to_target"
            else "0"
        ),
        effective_remaining_size=(
            _decimal_text(current)
            if disposition == "protection_only_below_target"
            else BATCH_119_RECOVERY.target_remaining_size
        ),
    )


def _require_locked_mimo_v1(session) -> None:
    """Require the persisted MiMo gate from the already locked transaction."""

    try:
        rows = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == "global")
            .all()
        )
        if len(rows) > 1:
            raise CompositeBatchRecoveryConflict(
                "mimo_contract_mode_not_v1"
            )
        if not rows:
            mode = "v1"
        else:
            payload = json.loads(rows[0].value_json)
            if not isinstance(payload, Mapping):
                raise ValueError("settings payload must be an object")
            mode = payload.get("mimo_contract_mode", "v1")
    except CompositeBatchRecoveryConflict:
        raise
    except (SQLAlchemyError, TypeError, ValueError, RecursionError) as exc:
        raise CompositeBatchRecoveryConflict(
            "mimo_contract_mode_not_v1"
        ) from exc
    if mode != "v1":
        raise CompositeBatchRecoveryConflict("mimo_contract_mode_not_v1")


def _validate_locked_exact_snapshot(
    session,
    *,
    snapshot: Any,
    expected_position: CompositeRecoveryPosition,
    expected_exchange_fingerprint: str,
    expected_natural_stop: Mapping[str, Any] | None,
) -> None:
    """Rebuild exact exchange authority from locked DB and captured GETs."""

    if not _snapshot_is_complete(
        snapshot,
        profile=BATCH_119_RECOVERY,
    ):
        raise CompositeBatchRecoveryConflict(
            "position_absent_snapshot_invalid"
        )
    batch = session.get(
        StrategyManagementBatch,
        BATCH_119_RECOVERY.batch_id,
    )
    lifecycle = session.get(
        StrategyLifecycle,
        BATCH_119_RECOVERY.lifecycle_id,
    )
    raw = session.get(RawMessage, BATCH_119_RECOVERY.raw_message_id)
    if batch is None or lifecycle is None or raw is None:
        raise CompositeBatchRecoveryConflict("source_state_conflict")
    binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
    legs = (
        session.query(StrategyManagementLeg)
        .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
        .all()
    )
    if binding is None or len(legs) != 1:
        raise CompositeBatchRecoveryConflict("source_state_conflict")
    leg = legs[0]
    entry = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
    if entry is None or _durable_identity_refusal(
        session=session,
        batch=batch,
        raw=raw,
        lifecycle=lifecycle,
        binding=binding,
        entry=entry,
        leg=leg,
        profile=BATCH_119_RECOVERY,
    ) is not None:
        raise CompositeBatchRecoveryConflict("source_state_conflict")
    target = _validated_target_snapshot(
        batch,
        binding=binding,
        leg=leg,
        entry=entry,
        profile=BATCH_119_RECOVERY,
    )
    if isinstance(target, str) or _has_durable_close_submission(
        session,
        batch=batch,
        leg=leg,
        entry=entry,
    ):
        raise CompositeBatchRecoveryConflict("source_state_conflict")
    try:
        position = classify_recovery_position(
            profile=BATCH_119_RECOVERY,
            positions=list(snapshot.positions),
            expected_pos_id=str(leg.pos_id),
            instrument_id=BATCH_119_RECOVERY.instrument_id,
            side=BATCH_119_RECOVERY.side,
            quantity_step=str(target["quantity_step"]),
            min_quantity=str(target["min_quantity"]),
        )
    except CompositeBatchRecoveryRefusal as exc:
        raise CompositeBatchRecoveryConflict(
            "position_absent_snapshot_invalid"
        ) from exc
    if position != expected_position:
        raise CompositeBatchRecoveryConflict(
            "position_absent_snapshot_invalid"
        )
    ledger = (
        session.query(PositionProtectionLedger)
        .filter(
            PositionProtectionLedger.execution_binding_id == binding.id,
            PositionProtectionLedger.execution_order_leg_id == entry.id,
            PositionProtectionLedger.pos_id == str(leg.pos_id),
        )
        .order_by(PositionProtectionLedger.id)
        .all()
    )
    try:
        durable_scope = _build_batch119_exact_history_scope_in_session(
            session
        )
    except CompositeBatchRecoveryRefusal as exc:
        raise CompositeBatchRecoveryConflict(
            "position_absent_snapshot_conflict"
        ) from exc
    if (
        snapshot.exact_scope != durable_scope
        or str(snapshot.scope_fingerprint)
        != durable_scope.scope_fingerprint
    ):
        raise CompositeBatchRecoveryConflict(
            "position_absent_snapshot_conflict"
        )
    proof: Mapping[str, Any] | None = None
    if position.disposition == "position_absent":
        proof_result = _validated_natural_stop_proof(
            snapshot=snapshot,
            ledger=ledger,
            pos_id=str(leg.pos_id),
            profile=BATCH_119_RECOVERY,
            not_before_ms=_incident_not_before_ms(
                batch=batch,
                leg=leg,
                raw=raw,
            ),
        )
        if isinstance(proof_result, str):
            raise CompositeBatchRecoveryConflict(
                "position_absent_snapshot_invalid"
            )
        proof = proof_result
    elif _has_exchange_close_submission(snapshot, pos_id=str(leg.pos_id)):
        raise CompositeBatchRecoveryConflict("recovery_snapshot_invalid")
    protection_reason = _protection_ownership_refusal(
        snapshot.pending_trigger_orders,
        batch=batch,
        binding=binding,
        entry=entry,
        ledger=ledger,
        pos_id=str(leg.pos_id),
        position=position,
        profile=BATCH_119_RECOVERY,
    )
    if protection_reason is not None:
        raise CompositeBatchRecoveryConflict(
            "position_absent_snapshot_invalid"
        )
    exchange_payload = _exchange_evidence_payload(
        snapshot,
        position=position,
        pos_id=str(leg.pos_id),
        ledger=ledger,
        profile=BATCH_119_RECOVERY,
        natural_stop_proof=proof,
    )
    exchange_fingerprint = _fingerprint(exchange_payload)
    if (
        exchange_fingerprint != expected_exchange_fingerprint
        or (
            expected_natural_stop is not None
            and (
                proof is None
                or _plain_json_value(expected_natural_stop) != dict(proof)
            )
        )
    ):
        raise CompositeBatchRecoveryConflict(
            "position_absent_snapshot_conflict"
        )


def apply_composite_batch_false_state_repair(
    session_factory,
    *,
    plan: CompositeBatchRecoveryPlan,
    expected_fingerprint: str,
    authorization: str,
    applied_at: datetime | None = None,
    snapshot: Any = None,
    require_mimo_v1: bool = True,
) -> CompositeBatchRecoveryApplyResult:
    """Atomically repair only the proven batch-119 legacy false state."""

    if authorization != _RECOVERY_AUTHORIZATION:
        raise CompositeBatchRecoveryConflict("authorization_invalid")
    if (
        not isinstance(plan, CompositeBatchRecoveryPlan)
        or plan.status != "ready"
        or plan.batch_id != BATCH_119_RECOVERY.batch_id
    ):
        raise CompositeBatchRecoveryConflict("plan_not_actionable")
    try:
        if (
            str(expected_fingerprint) != plan.evidence_fingerprint
            or _fingerprint(plan.evidence) != plan.evidence_fingerprint
        ):
            raise CompositeBatchRecoveryConflict(
                "evidence_fingerprint_mismatch"
            )
        _validate_recovery_plan_consistency(plan)
    except CompositeBatchRecoveryConflict:
        raise
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise CompositeBatchRecoveryConflict("plan_evidence_invalid") from exc
    if not _snapshot_is_complete(
        snapshot,
        profile=BATCH_119_RECOVERY,
    ):
        raise CompositeBatchRecoveryConflict("recovery_snapshot_invalid")
    applied = applied_at or datetime.now(UTC)
    with session_factory() as session:
        _acquire_recovery_write_lock(session)
        if not require_mimo_v1:
            raise CompositeBatchRecoveryConflict(
                "mimo_contract_gate_missing"
            )
        _require_locked_mimo_v1(session)
        existing = _load_recovery_audit_event(
            session, evidence_fingerprint=plan.evidence_fingerprint
        )
        if existing is not None:
            _validate_locked_exact_snapshot(
                session,
                snapshot=snapshot,
                expected_position=plan.position,
                expected_exchange_fingerprint=(
                    plan.exchange_snapshot_fingerprint
                ),
                expected_natural_stop=plan.evidence.get("natural_stop"),
            )
            if _repaired_state_and_event_match(session, event=existing, plan=plan):
                return CompositeBatchRecoveryApplyResult(
                    batch_id=BATCH_119_RECOVERY.batch_id,
                    status="already_repaired",
                    evidence_fingerprint=plan.evidence_fingerprint,
                    audit_event_id=int(existing.id),
                )
            raise CompositeBatchRecoveryConflict("recovery_audit_conflict")
        source = _load_locked_recovery_source(session)
        if source is None:
            raise CompositeBatchRecoveryConflict("source_state_conflict")
        (
            batch,
            binding,
            entry,
            leg,
            components,
            ledger,
            source_fingerprint,
        ) = source
        if source_fingerprint != plan.source_fingerprint:
            raise CompositeBatchRecoveryConflict("source_fingerprint_conflict")
        _validate_locked_exact_snapshot(
            session,
            snapshot=snapshot,
            expected_position=plan.position,
            expected_exchange_fingerprint=(
                plan.exchange_snapshot_fingerprint
            ),
            expected_natural_stop=plan.evidence.get("natural_stop"),
        )
        evidence = _plain_json_value(plan.evidence)
        if (
            evidence["durable"]["component_attempt_counts"]
            != [int(row.attempt_count) for row in components]
            or evidence["exchange"]["owned_protection_count"] != len(ledger)
        ):
            raise CompositeBatchRecoveryConflict("plan_evidence_stale")
        if plan.position is None or plan.position.disposition not in {
            "resume_to_target",
            "protection_only_at_target",
            "protection_only_below_target",
            "position_absent",
        }:
            raise CompositeBatchRecoveryConflict("plan_disposition_not_supported")

        before = {
            "batch_id": int(batch.id),
            "batch_status": str(batch.status),
            "leg_status": str(leg.status),
            "component_statuses": [str(row.status) for row in components],
            "component_attempt_counts": [
                int(row.attempt_count) for row in components
            ],
        }
        position_absent = plan.position.disposition == "position_absent"
        recovery_reason = (
            "composite_recovery_exact_position_absent"
            if position_absent
            else _RECOVERY_REASON
        )
        batch.status = "resolved" if position_absent else "ready"
        batch.reason_code = recovery_reason
        batch.last_progress_at = applied
        batch.updated_at = applied
        if position_absent:
            batch.reconciled_at = applied
            batch.completed_at = applied
        leg.status = "failed" if position_absent else "planned"
        leg.last_error = _canonical_json(
            {
                "reason": recovery_reason,
                "recovery_evidence_fingerprint": plan.evidence_fingerprint,
            }
        )
        leg.updated_at = applied
        if position_absent:
            for component in components:
                if not transition_component_for_exact_position_absent_recovery(
                    session,
                    component_id=int(component.id),
                    expected_status=str(component.status),
                    recovery_evidence_fingerprint=plan.evidence_fingerprint,
                    now=applied,
                ):
                    raise CompositeBatchRecoveryConflict(
                        "component_state_conflict"
                    )
        elif plan.position.disposition == "protection_only_below_target":
            attestation = _under_target_attestation(plan)
            for component in components:
                evidence = _safe_json_value(component.evidence_json)
                if not isinstance(evidence, list):
                    raise CompositeBatchRecoveryConflict(
                        "component_evidence_invalid"
                    )
                component.evidence_json = _canonical_json(
                    [*evidence, attestation]
                )
                component.updated_at = applied
        after = _recovery_audit_after(
            plan,
            batch_status=str(batch.status),
            leg_status=str(leg.status),
            component_statuses=[str(row.status) for row in components],
            original_owned_stop_refs=(
                []
                if position_absent
                else _owned_stop_refs_from_ledger(ledger)
            ),
        )
        event = ExecutionEvent(
            execution_binding_id=int(binding.id),
            venue="deepcoin",
            action=_RECOVERY_AUDIT_ACTION,
            status="resolved",
            reason=recovery_reason,
            before_json=_canonical_json(before),
            after_json=_canonical_json(after),
            notification_fingerprint=plan.evidence_fingerprint,
            created_at=applied,
        )
        session.add(event)
        session.commit()
        return CompositeBatchRecoveryApplyResult(
            batch_id=BATCH_119_RECOVERY.batch_id,
            status="repaired",
            evidence_fingerprint=plan.evidence_fingerprint,
            audit_event_id=int(event.id),
        )


def build_composite_batch_recovery_plan(
    session_factory,
    *,
    profile: CompositeBatchRecoveryProfile,
    snapshot: Any,
    planned_at: Any = None,
) -> CompositeBatchRecoveryPlan:
    """Fail closed at every untrusted durable/snapshot decoding boundary."""

    try:
        return _build_composite_batch_recovery_plan(
            session_factory,
            profile=profile,
            snapshot=snapshot,
            planned_at=planned_at,
        )
    except CompositeBatchRecoveryRefusal as exc:
        return _refusal(_refusal_batch_id(profile), exc.reason_code)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return _refusal(_refusal_batch_id(profile), "planner_evidence_invalid")


def _build_composite_batch_recovery_plan(
    session_factory,
    *,
    profile: CompositeBatchRecoveryProfile,
    snapshot: Any,
    planned_at: Any = None,
) -> CompositeBatchRecoveryPlan:
    """Prove the single approved incident without writes or exchange access."""

    if profile != BATCH_119_RECOVERY:
        return _refusal(
            _refusal_batch_id(profile), "incident_profile_not_allowlisted"
        )
    _ = planned_at
    if not _snapshot_is_complete(snapshot, profile=profile):
        return _refusal(profile.batch_id, "exchange_snapshot_incomplete")

    with session_factory() as session:
        try:
            _acquire_recovery_write_lock(session)
        except SQLAlchemyError:
            return _refusal(profile.batch_id, "durable_evidence_invalid")
        batch = session.get(StrategyManagementBatch, profile.batch_id)
        if batch is None:
            return _refusal(profile.batch_id, "management_batch_missing")
        if (
            int(batch.raw_message_id) != profile.raw_message_id
            or int(batch.target_lifecycle_id) != profile.lifecycle_id
        ):
            return _refusal(profile.batch_id, "incident_identity_mismatch")
        lifecycle = session.get(StrategyLifecycle, profile.lifecycle_id)
        raw = session.get(RawMessage, profile.raw_message_id)
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        legs = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=batch.id)
            .order_by(StrategyManagementLeg.leg_index, StrategyManagementLeg.id)
            .all()
        )
        components = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=batch.id)
            .order_by(
                StrategyManagementComponent.sequence,
                StrategyManagementComponent.id,
            )
            .all()
        )
        if raw is None or lifecycle is None or binding is None or len(legs) != 1:
            return _refusal(profile.batch_id, "durable_identity_mismatch")
        leg = legs[0]
        entry = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
        if entry is None:
            return _refusal(profile.batch_id, "durable_identity_mismatch")

        identity_reason = _durable_identity_refusal(
            session=session,
            batch=batch,
            raw=raw,
            lifecycle=lifecycle,
            binding=binding,
            entry=entry,
            leg=leg,
            profile=profile,
        )
        if identity_reason is not None:
            return _refusal(profile.batch_id, identity_reason)

        try:
            durable_scope = _build_batch119_exact_history_scope_in_session(
                session
            )
        except CompositeBatchRecoveryRefusal:
            return _refusal(
                profile.batch_id,
                "durable_snapshot_scope_mismatch",
            )
        if (
            snapshot.exact_scope != durable_scope
            or durable_scope.scope_fingerprint
            != str(snapshot.scope_fingerprint)
        ):
            return _refusal(
                profile.batch_id,
                "durable_snapshot_scope_mismatch",
            )

        contract_result = _validated_contract(batch, profile=profile)
        if isinstance(contract_result, str):
            return _refusal(profile.batch_id, contract_result)
        contract = contract_result

        target_result = _validated_target_snapshot(
            batch, binding=binding, leg=leg, entry=entry, profile=profile
        )
        if isinstance(target_result, str):
            return _refusal(profile.batch_id, target_result)
        target = target_result

        topology_reason = _component_topology_refusal(
            components,
            batch=batch,
            leg=leg,
            entry=entry,
            target=target,
            expected_contract_fingerprint=str(
                batch.management_contract_fingerprint
            ),
        )
        if topology_reason is not None:
            return _refusal(profile.batch_id, topology_reason)
        if not _exact_false_submission_state(batch, leg=leg, components=components):
            return _refusal(profile.batch_id, "false_submission_state_mismatch")
        legacy_state_reason = _legacy_false_state_evidence_refusal(
            leg, profile=profile
        )
        if legacy_state_reason is not None:
            return _refusal(profile.batch_id, legacy_state_reason)
        if any(
            value not in (None, "")
            for value in (
                leg.request_json,
                leg.response_json,
                leg.client_order_id,
                leg.exchange_order_id,
            )
        ):
            return _refusal(
                profile.batch_id, "durable_close_submission_evidence_present"
            )
        if _has_durable_close_submission(
            session, batch=batch, leg=leg, entry=entry
        ):
            return _refusal(
                profile.batch_id, "durable_close_submission_evidence_present"
            )
        if _has_additional_active_database_work(session, batch_id=batch.id):
            return _refusal(profile.batch_id, "additional_active_work_present")
        instruction_population = _instruction_population_payload(
            session,
            batch=batch,
            profile=profile,
        )

        positions = list(snapshot.positions)
        try:
            position = classify_recovery_position(
                profile=profile,
                positions=positions,
                expected_pos_id=str(leg.pos_id),
                instrument_id=profile.instrument_id,
                side=profile.side,
                quantity_step=str(target["quantity_step"]),
                min_quantity=str(target["min_quantity"]),
            )
        except CompositeBatchRecoveryRefusal as exc:
            return _refusal(profile.batch_id, exc.reason_code)

        ledger = (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.execution_binding_id == binding.id,
                PositionProtectionLedger.execution_order_leg_id == entry.id,
                PositionProtectionLedger.pos_id == str(leg.pos_id),
            )
            .order_by(PositionProtectionLedger.id)
            .all()
        )
        natural_stop_proof: Mapping[str, Any] | None = None
        if position.disposition == "position_absent":
            try:
                not_before_ms = _incident_not_before_ms(
                    batch=batch,
                    leg=leg,
                    raw=raw,
                )
            except CompositeBatchRecoveryRefusal as exc:
                return _refusal(profile.batch_id, exc.reason_code)
            proof_result = _validated_natural_stop_proof(
                snapshot=snapshot,
                ledger=ledger,
                pos_id=str(leg.pos_id),
                profile=profile,
                not_before_ms=not_before_ms,
            )
            if isinstance(proof_result, str):
                return _refusal(profile.batch_id, proof_result)
            natural_stop_proof = proof_result
        elif _has_exchange_close_submission(
            snapshot, pos_id=str(leg.pos_id)
        ):
            return _refusal(
                profile.batch_id, "exchange_close_submission_evidence_present"
            )

        protection_reason = _protection_ownership_refusal(
            snapshot.pending_trigger_orders,
            batch=batch,
            binding=binding,
            entry=entry,
            ledger=ledger,
            pos_id=str(leg.pos_id),
            position=position,
            profile=profile,
        )
        if protection_reason is not None:
            return _refusal(profile.batch_id, protection_reason)

        try:
            source_payload = _source_evidence_payload(
                batch=batch,
                raw=raw,
                lifecycle=lifecycle,
                binding=binding,
                entry=entry,
                leg=leg,
                components=components,
                target=target,
                contract=contract,
                protection_ledger=ledger,
                instruction_population=instruction_population,
            )
        except CompositeBatchRecoveryRefusal:
            return _refusal(profile.batch_id, "durable_evidence_invalid")
        source_fingerprint = _fingerprint(source_payload)
        exchange_payload = _exchange_evidence_payload(
            snapshot,
            position=position,
            pos_id=str(leg.pos_id),
            ledger=ledger,
            profile=profile,
            natural_stop_proof=natural_stop_proof,
        )
        exchange_fingerprint = _fingerprint(exchange_payload)
        evidence = {
            "schema_version": 1,
            "batch_id": profile.batch_id,
            "decision": "repair_false_legacy_submission",
            "reason_code": "false_legacy_submission_proven",
            "source_fingerprint": source_fingerprint,
            "exchange_snapshot_fingerprint": exchange_fingerprint,
            "immutable_target": {
                "instrument_id": profile.instrument_id,
                "side": profile.side,
                "trusted_start_size": profile.trusted_start_size,
                "target_remaining_size": profile.target_remaining_size,
                "quantity_step": str(target["quantity_step"]),
                "min_quantity": str(target["min_quantity"]),
            },
            "position": _serialize_position(position),
            "durable": {
                "batch_status": str(batch.status),
                "leg_status": str(leg.status),
                "component_statuses": [str(row.status) for row in components],
                "component_attempt_counts": [
                    int(row.attempt_count) for row in components
                ],
                "component_count": len(components),
                "close_submission_evidence_count": 0,
                "instruction_population": _instruction_population_summary(
                    instruction_population
                ),
            },
            "exchange": {
                "snapshot_complete": True,
                "exact_position_count": 0 if position.current_size is None else 1,
                "regular_close_evidence_count": 0,
                "owned_protection_count": len(ledger),
            },
            "proposed_transition": _proposed_transition(position),
        }
        if natural_stop_proof is not None:
            evidence["natural_stop"] = dict(natural_stop_proof)
        evidence_fingerprint = _fingerprint(evidence)
        return CompositeBatchRecoveryPlan(
            batch_id=profile.batch_id,
            status="ready",
            reason_code="false_legacy_submission_proven",
            position=position,
            source_fingerprint=source_fingerprint,
            exchange_snapshot_fingerprint=exchange_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
            evidence=_freeze_mapping(evidence),
        )


def _acquire_recovery_write_lock(session) -> None:
    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def _load_locked_recovery_source(session):
    batch = session.get(StrategyManagementBatch, BATCH_119_RECOVERY.batch_id)
    if batch is None:
        return None
    lifecycle = session.get(StrategyLifecycle, BATCH_119_RECOVERY.lifecycle_id)
    raw = session.get(RawMessage, BATCH_119_RECOVERY.raw_message_id)
    binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
    legs = (
        session.query(StrategyManagementLeg)
        .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
        .order_by(StrategyManagementLeg.leg_index, StrategyManagementLeg.id)
        .all()
    )
    components = (
        session.query(StrategyManagementComponent)
        .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
        .order_by(
            StrategyManagementComponent.sequence,
            StrategyManagementComponent.id,
        )
        .all()
    )
    if (
        raw is None
        or lifecycle is None
        or binding is None
        or len(legs) != 1
        or len(components) != len(_EXPECTED_COMPONENTS)
    ):
        return None
    leg = legs[0]
    entry = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
    if entry is None:
        return None
    if _durable_identity_refusal(
        session=session,
        batch=batch,
        raw=raw,
        lifecycle=lifecycle,
        binding=binding,
        entry=entry,
        leg=leg,
        profile=BATCH_119_RECOVERY,
    ) is not None:
        return None
    contract = _validated_contract(batch, profile=BATCH_119_RECOVERY)
    if isinstance(contract, str):
        return None
    target = _validated_target_snapshot(
        batch,
        binding=binding,
        leg=leg,
        entry=entry,
        profile=BATCH_119_RECOVERY,
    )
    if isinstance(target, str):
        return None
    if _component_topology_refusal(
        components,
        batch=batch,
        leg=leg,
        entry=entry,
        target=target,
        expected_contract_fingerprint=str(batch.management_contract_fingerprint),
    ) is not None:
        return None
    if not _exact_false_submission_state(batch, leg=leg, components=components):
        return None
    if _legacy_false_state_evidence_refusal(
        leg, profile=BATCH_119_RECOVERY
    ) is not None:
        return None
    if any(
        value not in (None, "")
        for value in (
            leg.request_json,
            leg.response_json,
            leg.client_order_id,
            leg.exchange_order_id,
        )
    ):
        return None
    if _has_durable_close_submission(
        session, batch=batch, leg=leg, entry=entry
    ):
        return None
    if _has_additional_active_database_work(
        session, batch_id=BATCH_119_RECOVERY.batch_id
    ):
        return None
    try:
        instruction_population = _instruction_population_payload(
            session,
            batch=batch,
            profile=BATCH_119_RECOVERY,
        )
    except CompositeBatchRecoveryRefusal:
        return None
    ledger = (
        session.query(PositionProtectionLedger)
        .filter(
            PositionProtectionLedger.execution_binding_id == binding.id,
            PositionProtectionLedger.execution_order_leg_id == entry.id,
            PositionProtectionLedger.pos_id == str(leg.pos_id),
        )
        .order_by(PositionProtectionLedger.id)
        .all()
    )
    try:
        payload = _source_evidence_payload(
            batch=batch,
            raw=raw,
            lifecycle=lifecycle,
            binding=binding,
            entry=entry,
            leg=leg,
            components=components,
            target=target,
            contract=contract,
            protection_ledger=ledger,
            instruction_population=instruction_population,
        )
        source_fingerprint = _fingerprint(payload)
    except (CompositeBatchRecoveryRefusal, TypeError, ValueError, RecursionError):
        return None
    return batch, binding, entry, leg, components, ledger, source_fingerprint


def _recovery_audit_after(
    plan: CompositeBatchRecoveryPlan,
    *,
    batch_status: str,
    leg_status: str,
    component_statuses: list[str],
    original_owned_stop_refs: list[str],
) -> dict[str, Any]:
    return {
        "batch_id": BATCH_119_RECOVERY.batch_id,
        "batch_status": str(batch_status),
        "leg_status": str(leg_status),
        "component_statuses": list(component_statuses),
        "component_attempt_counts": _component_attempt_counts_from_plan(plan),
        "source_fingerprint": plan.source_fingerprint,
        "exchange_snapshot_fingerprint": plan.exchange_snapshot_fingerprint,
        "evidence_fingerprint": plan.evidence_fingerprint,
        "position_disposition": plan.position.disposition if plan.position else None,
        "current_size": plan.position.current_size if plan.position else None,
        "target_remaining_size": BATCH_119_RECOVERY.target_remaining_size,
        "exchange_call_possible": False,
        "original_owned_stop_refs": list(original_owned_stop_refs),
        "instruction_population": _instruction_population_from_plan(plan),
    }


def _owned_stop_refs_from_ledger(ledger: Sequence[Any]) -> list[str]:
    refs = sorted(
        _redacted_ref("protection_order", row.order_id)
        for row in ledger
        if str(row.purpose) in {"stop_loss", "backup_stop"}
    )
    if len(refs) != len(set(refs)):
        raise CompositeBatchRecoveryConflict("owned_stop_identity_conflict")
    return refs


def _valid_original_owned_stop_refs(
    value: Any,
    *,
    position_absent: bool,
) -> bool:
    expected_count = 0 if position_absent else 2
    return bool(
        isinstance(value, list)
        and len(value) == expected_count
        and value == sorted(value)
        and len(set(value)) == len(value)
        and all(_is_sha256(item) for item in value)
    )


def _original_owned_stop_refs(
    session,
    *,
    batch,
    leg,
    entry,
    audit_created_at: Any,
) -> list[str]:
    rows = (
        session.query(PositionProtectionLedger)
        .filter(
            PositionProtectionLedger.execution_binding_id
            == int(batch.execution_binding_id),
            PositionProtectionLedger.execution_order_leg_id == int(entry.id),
            PositionProtectionLedger.pos_id == str(leg.pos_id),
            PositionProtectionLedger.purpose.in_(("stop_loss", "backup_stop")),
            PositionProtectionLedger.created_at <= audit_created_at,
        )
        .all()
    )
    return _owned_stop_refs_from_ledger(rows)


def _under_target_attestation(
    plan: CompositeBatchRecoveryPlan,
) -> dict[str, str]:
    if (
        plan.position is None
        or plan.position.disposition != "protection_only_below_target"
    ):
        raise CompositeBatchRecoveryConflict("under_target_attestation_invalid")
    return {
        "kind": "approved_under_target_recovery",
        "actual_remaining_size": str(plan.position.effective_remaining_size),
        "original_target_remaining_size": (
            BATCH_119_RECOVERY.target_remaining_size
        ),
        "recovery_evidence_fingerprint": plan.evidence_fingerprint,
    }


def _position_absent_evidence(
    plan: CompositeBatchRecoveryPlan,
) -> dict[str, str]:
    if plan.position is None or plan.position.disposition != "position_absent":
        raise CompositeBatchRecoveryConflict("position_absent_evidence_invalid")
    return {
        "kind": "composite_recovery_exact_position_absent",
        "recovery_evidence_fingerprint": plan.evidence_fingerprint,
    }


def _load_recovery_audit_event(session, *, evidence_fingerprint: str):
    rows = (
        session.query(ExecutionEvent)
        .filter(
            ExecutionEvent.notification_fingerprint
            == str(evidence_fingerprint)
        )
        .all()
    )
    if len(rows) > 1:
        raise CompositeBatchRecoveryConflict("recovery_audit_conflict")
    return rows[0] if rows else None


def _repaired_state_and_event_match(
    session, *, event: ExecutionEvent, plan: CompositeBatchRecoveryPlan
) -> bool:
    batch = session.get(StrategyManagementBatch, BATCH_119_RECOVERY.batch_id)
    lifecycle = session.get(
        StrategyLifecycle, BATCH_119_RECOVERY.lifecycle_id
    )
    raw = session.get(RawMessage, BATCH_119_RECOVERY.raw_message_id)
    if batch is None or lifecycle is None or raw is None:
        return False
    binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
    legs = (
        session.query(StrategyManagementLeg)
        .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
        .all()
    )
    components = (
        session.query(StrategyManagementComponent)
        .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
        .order_by(StrategyManagementComponent.sequence)
        .all()
    )
    if binding is None or len(legs) != 1 or len(components) != 3:
        return False
    leg = legs[0]
    entry = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
    if entry is None:
        return False
    if _durable_identity_refusal(
        session=session,
        batch=batch,
        raw=raw,
        lifecycle=lifecycle,
        binding=binding,
        entry=entry,
        leg=leg,
        profile=BATCH_119_RECOVERY,
    ) is not None:
        return False
    contract = _validated_contract(batch, profile=BATCH_119_RECOVERY)
    if isinstance(contract, str):
        return False
    target = _validated_target_snapshot(
        batch,
        binding=binding,
        leg=leg,
        entry=entry,
        profile=BATCH_119_RECOVERY,
    )
    if isinstance(target, str):
        return False
    if plan.position is None:
        return False
    position_absent = plan.position.disposition == "position_absent"
    expected_component_statuses = (
        ("safely_skipped",) * len(_EXPECTED_COMPONENTS)
        if position_absent
        else ("recovery_required", "pending", "pending")
    )
    recovery_reason = (
        "composite_recovery_exact_position_absent"
        if position_absent
        else _RECOVERY_REASON
    )
    evidence_suffix = (
        _position_absent_evidence(plan)
        if position_absent
        else _under_target_attestation(plan)
        if plan.position.disposition == "protection_only_below_target"
        else None
    )
    if _component_topology_refusal(
        components,
        batch=batch,
        leg=leg,
        entry=entry,
        target=target,
        expected_contract_fingerprint=str(
            batch.management_contract_fingerprint
        ),
        evidence_suffix=evidence_suffix,
        expected_statuses=expected_component_statuses,
        expected_reason_codes=(
            (recovery_reason,) * len(_EXPECTED_COMPONENTS)
            if position_absent
            else (
                "take_profit_exchange_snapshot_incomplete",
                None,
                None,
            )
        ),
    ) is not None:
        return False
    if _legacy_false_exchange_snapshot_refusal(
        leg, profile=BATCH_119_RECOVERY
    ) is not None:
        return False
    if _has_durable_close_submission(
        session, batch=batch, leg=leg, entry=entry
    ):
        return False
    if _has_additional_active_database_work(
        session, batch_id=BATCH_119_RECOVERY.batch_id
    ):
        return False
    try:
        current_instruction_population = _instruction_population_summary(
            _instruction_population_payload(
                session,
                batch=batch,
                profile=BATCH_119_RECOVERY,
            )
        )
    except CompositeBatchRecoveryRefusal:
        return False
    if current_instruction_population != _instruction_population_from_plan(plan):
        return False
    component_attempt_counts = _component_attempt_counts_from_plan(plan)
    before = {
        "batch_id": BATCH_119_RECOVERY.batch_id,
        "batch_status": "reconciling",
        "leg_status": "submitted",
        "component_statuses": ["recovery_required", "pending", "pending"],
        "component_attempt_counts": component_attempt_counts,
    }
    after = _recovery_audit_after(
        plan,
        batch_status="resolved" if position_absent else "ready",
        leg_status="failed" if position_absent else "planned",
        component_statuses=list(expected_component_statuses),
        original_owned_stop_refs=(
            []
            if position_absent
            else _original_owned_stop_refs(
                session,
                batch=batch,
                leg=leg,
                entry=entry,
                audit_created_at=event.created_at,
            )
        ),
    )
    try:
        audited_after = _validated_resume_audit_event(
            event,
            expected_fingerprint=plan.evidence_fingerprint,
        )
    except CompositeBatchRecoveryConflict:
        return False
    return bool(
        event.before_json == _canonical_json(before)
        and audited_after == after
        and _recovery_audit_matches_current_durable_state(
            session,
            event=event,
            after=audited_after,
            batch=batch,
            binding=binding,
            leg=leg,
            entry=entry,
            components=components,
            contract=contract,
            target=target,
        )
    )


def _safe_json_value(value: str | None) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return None


def _validate_recovery_plan_consistency(plan: CompositeBatchRecoveryPlan) -> None:
    if (
        plan.reason_code != "false_legacy_submission_proven"
        or plan.production_writes != 0
        or plan.exchange_calls != 0
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    evidence = _plain_json_value(plan.evidence)
    if not isinstance(evidence, Mapping):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    expected_evidence_keys = {
        "schema_version",
        "batch_id",
        "decision",
        "reason_code",
        "source_fingerprint",
        "exchange_snapshot_fingerprint",
        "immutable_target",
        "position",
        "durable",
        "exchange",
        "proposed_transition",
    }
    position_absent = (
        isinstance(plan.position, CompositeRecoveryPosition)
        and plan.position.disposition == "position_absent"
    )
    if position_absent:
        expected_evidence_keys.add("natural_stop")
    immutable_target = evidence.get("immutable_target")
    durable = evidence.get("durable")
    exchange = evidence.get("exchange")
    if (
        set(evidence) != expected_evidence_keys
        or not isinstance(immutable_target, Mapping)
        or not isinstance(durable, Mapping)
        or not isinstance(exchange, Mapping)
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    attempt_counts = durable.get("component_attempt_counts")
    if (
        not isinstance(attempt_counts, list)
        or len(attempt_counts) != len(_EXPECTED_COMPONENTS)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in attempt_counts
        )
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    expected_durable = {
        "batch_status": "reconciling",
        "leg_status": "submitted",
        "component_statuses": ["recovery_required", "pending", "pending"],
        "component_attempt_counts": attempt_counts,
        "component_count": len(_EXPECTED_COMPONENTS),
        "close_submission_evidence_count": 0,
        "instruction_population": durable.get("instruction_population"),
    }
    instruction_population = durable.get("instruction_population")
    if (
        not isinstance(instruction_population, Mapping)
        or set(instruction_population)
        != {"schema_version", "total_count", "counts", "digest"}
        or instruction_population.get("schema_version") != 1
        or isinstance(instruction_population.get("total_count"), bool)
        or not isinstance(instruction_population.get("total_count"), int)
        or instruction_population.get("total_count", 0) < 1
        or instruction_population.get("total_count", 0)
        > _MAX_INSTRUCTION_POPULATION
        or not isinstance(instruction_population.get("counts"), Mapping)
        or set(instruction_population["counts"]) != set(_INSTRUCTION_DISPOSITIONS)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in instruction_population["counts"].values()
        )
        or sum(instruction_population["counts"].values())
        != instruction_population["total_count"]
        or instruction_population["counts"].get("target_incident_frozen") != 1
        or not _is_sha256(instruction_population.get("digest"))
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    expected_target = {
        "instrument_id": BATCH_119_RECOVERY.instrument_id,
        "side": BATCH_119_RECOVERY.side,
        "trusted_start_size": BATCH_119_RECOVERY.trusted_start_size,
        "target_remaining_size": BATCH_119_RECOVERY.target_remaining_size,
        "quantity_step": immutable_target.get("quantity_step"),
        "min_quantity": immutable_target.get("min_quantity"),
    }
    if not isinstance(
        plan.position, CompositeRecoveryPosition
    ) or not _position_matches_recovery_profile(
        plan.position,
        quantity_step=expected_target["quantity_step"],
        min_quantity=expected_target["min_quantity"],
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    expected_exchange = {
        "snapshot_complete": True,
        "exact_position_count": (
            0 if plan.position.disposition == "position_absent" else 1
        ),
        "regular_close_evidence_count": 0,
        "owned_protection_count": exchange.get("owned_protection_count"),
    }
    owned_protection_count = exchange.get("owned_protection_count")
    minimum_owned_protection = 2
    if (
        evidence.get("schema_version") != 1
        or evidence.get("batch_id") != BATCH_119_RECOVERY.batch_id
        or evidence.get("decision") != "repair_false_legacy_submission"
        or evidence.get("reason_code") != "false_legacy_submission_proven"
        or evidence.get("source_fingerprint") != plan.source_fingerprint
        or evidence.get("exchange_snapshot_fingerprint")
        != plan.exchange_snapshot_fingerprint
        or evidence.get("immutable_target") != expected_target
        or durable != expected_durable
        or not isinstance(owned_protection_count, int)
        or isinstance(owned_protection_count, bool)
        or owned_protection_count < minimum_owned_protection
        or exchange != expected_exchange
        or evidence.get("position") != _serialize_position(plan.position)
        or evidence.get("proposed_transition") != _proposed_transition(plan.position)
        or (
            position_absent
            and not _natural_stop_proof_payload_is_valid(
                evidence.get("natural_stop")
            )
        )
        or not all(
            _is_sha256(value)
            for value in (
                plan.source_fingerprint,
                plan.exchange_snapshot_fingerprint,
                plan.evidence_fingerprint,
            )
        )
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")


def _natural_stop_proof_payload_is_valid(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and set(value)
        == {
            "purpose",
            "trigger_status",
            "position_status",
            "time_relation",
            "trigger_count",
            "closed_position_count",
            "order_ref",
            "position_ref",
        }
        and value.get("purpose") in {"stop_loss", "backup_stop"}
        and value.get("trigger_status") == "successful_terminal"
        and value.get("position_status") == "closed"
        and value.get("time_relation") == "trigger_not_after_close"
        and value.get("trigger_count") == 1
        and not isinstance(value.get("trigger_count"), bool)
        and value.get("closed_position_count") == 1
        and not isinstance(value.get("closed_position_count"), bool)
        and _is_sha256(value.get("order_ref"))
        and _is_sha256(value.get("position_ref"))
    )


def _component_attempt_counts_from_plan(
    plan: CompositeBatchRecoveryPlan,
) -> list[int]:
    try:
        durable = _plain_json_value(plan.evidence)["durable"]
        values = durable["component_attempt_counts"]
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent") from exc
    if (
        not isinstance(values, list)
        or len(values) != len(_EXPECTED_COMPONENTS)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        )
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    return list(values)


def _position_matches_recovery_profile(
    position: CompositeRecoveryPosition,
    *,
    quantity_step: object,
    min_quantity: object,
) -> bool:
    try:
        step = Decimal(str(quantity_step))
        minimum = Decimal(str(min_quantity))
        close_delta = Decimal(str(position.close_delta))
        effective = Decimal(str(position.effective_remaining_size))
        current = (
            None
            if position.current_size is None
            else Decimal(str(position.current_size))
        )
    except (InvalidOperation, TypeError, ValueError):
        return False
    decimal_values = [close_delta, effective]
    if current is not None:
        decimal_values.append(current)
    if (
        any(not value.is_finite() for value in [step, minimum, *decimal_values])
        or step <= 0
        or minimum <= 0
        or any(value < 0 or value % step != 0 for value in decimal_values)
        or _decimal_text(close_delta) != str(position.close_delta)
        or _decimal_text(effective) != str(position.effective_remaining_size)
        or (
            current is not None
            and _decimal_text(current) != str(position.current_size)
        )
    ):
        return False
    trusted = Decimal(BATCH_119_RECOVERY.trusted_start_size)
    target = Decimal(BATCH_119_RECOVERY.target_remaining_size)
    expected_by_disposition = {
        "resume_to_target": (trusted, trusted - target, target),
        "protection_only_at_target": (target, Decimal(0), target),
        "position_absent": (None, Decimal(0), Decimal(0)),
    }
    if position.disposition == "protection_only_below_target":
        return (
            current is not None
            and minimum <= current < target
            and close_delta == 0
            and effective == current
        )
    expected = expected_by_disposition.get(position.disposition)
    return expected is not None and (current, close_delta, effective) == expected


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def serialize_composite_batch_recovery_plan(
    plan: CompositeBatchRecoveryPlan,
) -> dict[str, Any]:
    """Return the only supported, strictly allowlisted CLI serialization."""

    return {
        "batch_id": int(plan.batch_id),
        "status": str(plan.status),
        "reason_code": str(plan.reason_code),
        "position": (
            None if plan.position is None else _serialize_position(plan.position)
        ),
        "source_fingerprint": str(plan.source_fingerprint),
        "exchange_snapshot_fingerprint": str(
            plan.exchange_snapshot_fingerprint
        ),
        "evidence_fingerprint": str(plan.evidence_fingerprint),
        "evidence": _plain_json_value(plan.evidence),
        "production_writes": 0,
        "exchange_calls": 0,
    }


def _capture_collection_seal_payload(collection: Any) -> dict[str, Any]:
    rows = getattr(collection, "rows", None)
    if not isinstance(rows, (list, tuple)):
        raise TypeError("capture collection rows invalid")
    return {
        "endpoint": getattr(collection, "endpoint", None),
        "available": getattr(collection, "available", None),
        "schema_valid": getattr(collection, "schema_valid", None),
        "complete": getattr(collection, "complete", None),
        "rows": [_plain_json_value(row) for row in rows],
        "row_count": getattr(collection, "row_count", None),
        "page_count": getattr(collection, "page_count", None),
        "fingerprint": getattr(collection, "fingerprint", None),
        "reason_code": getattr(collection, "reason_code", None),
        "expected_order_ids_visible": getattr(
            collection,
            "expected_order_ids_visible",
            False,
        ),
    }


def _batch119_capture_seal_payload(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, Batch119ExactRecoverySnapshot):
        raise TypeError("batch119 exact snapshot required")
    authority = snapshot.account_authority
    authority_collections = getattr(authority, "collections", None)
    scope = snapshot.exact_scope
    if (
        authority is None
        or not isinstance(authority_collections, (list, tuple))
        or not isinstance(scope, _Batch119ExactHistoryScope)
    ):
        raise TypeError("batch119 capture authority invalid")
    started = _normalize_aware_utc_datetime(snapshot.capture_started_at)
    ended = _normalize_aware_utc_datetime(snapshot.capture_ended_at)
    if started is None or ended is None:
        raise TypeError("batch119 capture window invalid")
    return {
        "schema_version": 1,
        "collections": {
            field_name: [
                _plain_json_value(row)
                for row in getattr(snapshot, field_name)
            ]
            for field_name in _REQUIRED_SNAPSHOT_FIELDS
            if field_name != "errors"
        },
        "errors": _plain_json_value(snapshot.errors),
        "capture_started_at": started.isoformat(),
        "capture_ended_at": ended.isoformat(),
        "scope_fingerprint": snapshot.scope_fingerprint,
        "exact_scope": {
            "instrument_id": scope.instrument_id,
            "side": scope.side,
            "scope_fingerprint": scope.scope_fingerprint,
            "position_id": scope.position_id,
            "protection_orders": [list(row) for row in scope.protection_orders],
            "protection_evidence_fingerprints": [
                list(row) for row in scope.protection_evidence_fingerprints
            ],
        },
        "collection_authority": [
            _plain_json_value(row) for row in snapshot.collection_authority
        ],
        "account_authority": {
            "uid_scope_hash": getattr(authority, "uid_scope_hash", None),
            "start_write_generation": getattr(
                authority,
                "start_write_generation",
                None,
            ),
            "end_write_generation": getattr(
                authority,
                "end_write_generation",
                None,
            ),
            "complete": getattr(authority, "complete", None),
            "reason_code": getattr(authority, "reason_code", None),
            "collections": [
                _capture_collection_seal_payload(collection)
                for collection in authority_collections
            ],
        },
    }


def _batch119_capture_seal(snapshot: Any) -> str:
    payload = _canonical_json(_batch119_capture_seal_payload(snapshot)).encode(
        "utf-8"
    )
    return hmac.new(
        _BATCH119_CAPTURE_HMAC_KEY,
        payload,
        hashlib.sha256,
    ).hexdigest()


def _seal_batch119_recovery_snapshot(
    snapshot: Batch119ExactRecoverySnapshot,
) -> Batch119ExactRecoverySnapshot:
    snapshot._capture_seal = _batch119_capture_seal(snapshot)
    return snapshot


def _batch119_capture_seal_is_valid(snapshot: Any) -> bool:
    if not isinstance(snapshot, Batch119ExactRecoverySnapshot):
        return False
    seal = snapshot._capture_seal
    if not isinstance(seal, str) or not _is_sha256(seal):
        return False
    try:
        expected = _batch119_capture_seal(snapshot)
    except (AttributeError, TypeError, ValueError, RecursionError, OverflowError):
        return False
    return hmac.compare_digest(seal, expected)


def _snapshot_is_complete(
    snapshot: Any,
    *,
    profile: CompositeBatchRecoveryProfile,
) -> bool:
    if not _batch119_capture_seal_is_valid(snapshot):
        return False
    if any(not hasattr(snapshot, field) for field in _REQUIRED_SNAPSHOT_FIELDS):
        return False
    if any(
        not isinstance(getattr(snapshot, field), (list, tuple))
        for field in _REQUIRED_SNAPSHOT_FIELDS
        if field != "errors"
    ):
        return False
    errors = getattr(snapshot, "errors", None)
    if not isinstance(errors, Mapping) or errors:
        return False
    scope_fingerprint = getattr(snapshot, "scope_fingerprint", None)
    authority = getattr(snapshot, "account_authority", None)
    capture_started_at = getattr(snapshot, "capture_started_at", None)
    capture_ended_at = getattr(snapshot, "capture_ended_at", None)
    normalized_started_at = _normalize_aware_utc_datetime(capture_started_at)
    normalized_ended_at = _normalize_aware_utc_datetime(capture_ended_at)
    normalized_wall_clock = _normalize_aware_utc_datetime(_utc_wall_clock())
    authority_collections = getattr(authority, "collections", None)
    start_generation = getattr(authority, "start_write_generation", None)
    end_generation = getattr(authority, "end_write_generation", None)
    if (
        not _is_sha256(scope_fingerprint)
        or authority is None
        or getattr(authority, "complete", None) is not True
        or not _is_sha256(getattr(authority, "uid_scope_hash", None))
        or not isinstance(start_generation, int)
        or not isinstance(end_generation, int)
        or start_generation != end_generation
        or start_generation % 2 != 0
        or not isinstance(authority_collections, (list, tuple))
        or not authority_collections
        or any(
            getattr(collection, "complete", None) is not True
            or not _is_sha256(getattr(collection, "fingerprint", None))
            for collection in authority_collections
        )
        or normalized_started_at is None
        or normalized_ended_at is None
        or normalized_wall_clock is None
        or normalized_started_at > normalized_ended_at
        or normalized_ended_at
        > normalized_wall_clock + _MAX_FUTURE_CLOCK_SKEW
        or not _batch119_snapshot_authority_matches(
            snapshot,
            profile=profile,
        )
    ):
        return False
    try:
        for field in _REQUIRED_SNAPSHOT_FIELDS:
            if field == "errors":
                continue
            for row in getattr(snapshot, field):
                if not isinstance(row, Mapping):
                    return False
                _fingerprint(dict(row))
    except (TypeError, ValueError, RecursionError, OverflowError):
        return False
    observations = list(snapshot.pending_tpsl_observations)
    if not observations or any(
        not isinstance(row, Mapping) or row.get("complete") is not True
        for row in observations
    ):
        return False
    matching = [
        row
        for row in observations
        if isinstance(row, Mapping)
        and str(
            row.get("instrument_id")
            or row.get("instId")
            or row.get("instrumentId")
            or ""
        ).upper()
        == profile.instrument_id.upper()
    ]
    return len(matching) == 1


def _durable_identity_refusal(
    *, session, batch, raw, lifecycle, binding, entry, leg, profile
) -> str | None:
    expected_symbol = profile.instrument_id.split("-", 1)[0].upper()
    if (
        str(batch.intent) != "partial_then_break_even"
        or str(batch.effective_action) != "partial_then_break_even"
        or str(batch.execution_mode) != "live"
        or int(batch.execution_binding_id) != int(binding.id)
        or str(batch.strategy_instance_id) != str(binding.strategy_instance_id)
    ):
        return "management_batch_identity_mismatch"
    if (
        int(raw.id) != profile.raw_message_id
        or int(raw.chat_id) != int(lifecycle.chat_id)
    ):
        return "raw_message_identity_mismatch"
    if (
        int(lifecycle.id) != profile.lifecycle_id
        or int(lifecycle.execution_binding_id or 0) != int(binding.id)
        or str(lifecycle.symbol).upper() != expected_symbol
        or str(lifecycle.side).lower() != profile.side.lower()
        or str(lifecycle.lifecycle_status) != "entered"
    ):
        return "lifecycle_identity_mismatch"
    if (
        str(binding.venue).lower() != "deepcoin"
        or int(binding.chat_id) != int(lifecycle.chat_id)
        or int(binding.message_id) != int(lifecycle.message_id)
        or str(binding.symbol).upper() != expected_symbol
        or str(binding.side).lower() != profile.side.lower()
        or str(binding.status).lower() not in {"active", "open"}
        or str(binding.pos_id or "") != str(leg.pos_id)
    ):
        return "execution_binding_identity_mismatch"
    if (
        int(entry.execution_binding_id) != int(binding.id)
        or str(entry.strategy_instance_id or "") != str(batch.strategy_instance_id)
        or str(entry.pos_id or "") != str(leg.pos_id)
        or str(entry.venue).lower() != "deepcoin"
        or str(entry.purpose) != "entry"
        or str(entry.attribution_status) != "verified"
        or str(entry.status) not in {"active", "filled", "partially_filled"}
    ):
        return "execution_leg_identity_mismatch"
    if not has_authoritative_persisted_position(entry, session=session):
        return "position_ownership_evidence_not_authoritative"
    if (
        int(leg.management_batch_id) != profile.batch_id
        or int(leg.execution_order_leg_id) != int(entry.id)
        or int(leg.leg_index) != 0
        or str(leg.preflight_size) != profile.trusted_start_size
        or str(leg.planned_close_size) != (
            _decimal_text(
                Decimal(profile.trusted_start_size)
                - Decimal(profile.target_remaining_size)
            )
        )
    ):
        return "management_leg_identity_mismatch"
    return None


def _validated_contract(
    batch: StrategyManagementBatch, *, profile: CompositeBatchRecoveryProfile
):
    if (
        not batch.management_contract_json
        or not batch.management_contract_fingerprint
        or int(batch.contract_version or 0) != 2
    ):
        return "management_contract_missing"
    try:
        contract = load_management_contract(batch.management_contract_json)
        actual_fingerprint = management_contract_fingerprint(contract)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return "management_contract_invalid"
    if (
        actual_fingerprint != str(batch.management_contract_fingerprint)
    ):
        return "management_contract_fingerprint_mismatch"
    expected_symbol = profile.instrument_id.split("-", 1)[0].upper()
    if (
        int(contract.target_lifecycle_id or 0) != profile.lifecycle_id
        or str(contract.strategy_instance_id or "")
        != str(batch.strategy_instance_id)
        or str(contract.symbol or "").upper() != expected_symbol
        or str(contract.side or "").lower() != profile.side.lower()
        or str(contract.close_fraction) != "0.5"
        or contract.stop_mode != "actual_entry_price"
        or contract.take_profit_consumption != "consume_first_stage"
        or contract.cancel_deferred_entries is not True
        or tuple(contract.required_components) != _EXPECTED_COMPONENTS
    ):
        return "management_contract_identity_mismatch"
    return contract


def _validated_target_snapshot(batch, *, binding, leg, entry, profile):
    try:
        payload = json.loads(batch.target_snapshot_json)
        actual_target_fingerprint = management_target_fingerprint(payload)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return "target_snapshot_invalid"
    rows = payload.get("positions") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        return "target_snapshot_invalid"
    if actual_target_fingerprint != str(batch.target_fingerprint):
        return "target_snapshot_fingerprint_mismatch"
    identity = payload.get("identity")
    if not isinstance(identity, Mapping):
        return "target_snapshot_identity_mismatch"
    target_lifecycle_id = _exact_int(identity.get("target_lifecycle_id"))
    execution_binding_id = _exact_int(identity.get("execution_binding_id"))
    if (
        str(payload.get("execution_mode") or "") != str(batch.execution_mode)
        or target_lifecycle_id != int(batch.target_lifecycle_id)
        or execution_binding_id != int(batch.execution_binding_id)
        or str(identity.get("strategy_instance_id") or "")
        != str(batch.strategy_instance_id)
        or identity.get("manageable_entry_leg_ids") != [int(entry.id)]
        or identity.get("deferred_entry_leg_ids") != []
        or identity.get("capability_deferred_entry_leg_ids") != []
        or identity.get("capability_deferred_pos_ids") != []
        or payload.get("deferred_entry_legs") != []
    ):
        return "target_snapshot_identity_mismatch"
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("pos_id") or row.get("posId") or "") == str(leg.pos_id)
    ]
    if len(rows) != 1 or len(matches) != 1:
        return "target_snapshot_identity_mismatch"
    row = matches[0]
    required = (
        "trusted_start_size",
        "target_remaining_size",
        "avg_entry_price",
        "quantity_step",
        "min_quantity",
    )
    if any(row.get(key) in (None, "") for key in required):
        return "target_snapshot_identity_mismatch"
    if (
        str(row["trusted_start_size"]) != profile.trusted_start_size
        or str(row["target_remaining_size"]) != profile.target_remaining_size
        or str(row.get("instrument_id") or "").upper()
        != profile.instrument_id.upper()
        or str(row.get("side") or "").lower() != profile.side.lower()
        or str(row.get("size") or "") != profile.trusted_start_size
        or str(leg.avg_entry_price) != str(row["avg_entry_price"])
        or str(leg.quantity_step) != str(row["quantity_step"])
        or int(leg.execution_order_leg_id) != int(entry.id)
        or _exact_int(row.get("execution_order_leg_id")) != int(entry.id)
        or str(row.get("margin_mode") or "") != str(binding.margin_mode)
        or str(row.get("position_mode") or "") != str(binding.position_mode)
    ):
        return "target_snapshot_identity_mismatch"
    try:
        _positive_decimal(row["avg_entry_price"], "avg_entry_price")
        _positive_decimal(row["quantity_step"], "quantity_step")
        _positive_decimal(row["min_quantity"], "min_quantity")
    except CompositeBatchRecoveryRefusal:
        return "target_snapshot_identity_mismatch"
    return dict(row)


def _component_topology_refusal(
    components,
    *,
    batch,
    leg,
    entry,
    target,
    expected_contract_fingerprint: str,
    evidence_suffix: Mapping[str, Any] | None = None,
    expected_statuses: tuple[str, ...] = (
        "recovery_required",
        "pending",
        "pending",
    ),
    expected_reason_codes: tuple[str | None, ...] = (
        "take_profit_exchange_snapshot_incomplete",
        None,
        None,
    ),
) -> str | None:
    if len(components) != len(_EXPECTED_COMPONENTS):
        return "component_topology_mismatch"
    for sequence, (component, kind, status) in enumerate(
        zip(components, _EXPECTED_COMPONENTS, expected_statuses, strict=True)
    ):
        if (
            int(component.management_batch_id) != int(batch.id)
            or int(component.strategy_management_leg_id or 0) != int(leg.id)
            or int(component.strategy_management_leg_scope) != int(leg.id)
            or int(component.sequence) != sequence
            or str(component.component_kind) != kind
            or str(component.status) != status
        ):
            return "component_topology_mismatch"
        try:
            desired = json.loads(component.desired_json)
        except (TypeError, ValueError, RecursionError):
            return "component_topology_mismatch"
        if not isinstance(desired, Mapping):
            return "component_topology_mismatch"
        expected = {
            "contract_fingerprint": expected_contract_fingerprint,
            "pos_id": str(leg.pos_id),
            "execution_order_leg_id": int(entry.id),
            "trusted_start_size": str(target["trusted_start_size"]),
            "target_remaining_size": str(target["target_remaining_size"]),
            "avg_entry_price": str(target["avg_entry_price"]),
            "quantity_step": str(target["quantity_step"]),
            "min_quantity": str(target["min_quantity"]),
            "component_kind": kind,
        }
        if dict(desired) != expected:
            return "component_topology_mismatch"
        expected_idempotency_key = hashlib.sha256(
            (
                f"{expected_contract_fingerprint}|{int(batch.id)}|"
                f"{int(leg.id)}|{kind}"
            ).encode("utf-8")
        ).hexdigest()
        if str(component.idempotency_key) != expected_idempotency_key:
            return "component_topology_mismatch"
        try:
            evidence = json.loads(component.evidence_json)
        except (TypeError, ValueError, RecursionError):
            return "component_topology_mismatch"
        expected_attestation = (
            [dict(evidence_suffix)]
            if evidence_suffix is not None
            else []
        )
        if sequence == 0:
            if not (
                isinstance(evidence, list)
                and _is_bounded_snapshot_incomplete_evidence(evidence[:1])
                and evidence[1:] == expected_attestation
            ):
                return "component_topology_mismatch"
        elif evidence != expected_attestation:
            return "component_topology_mismatch"
    if tuple(row.reason_code for row in components) != expected_reason_codes:
        return "component_topology_mismatch"
    return None


def _exact_false_submission_state(batch, *, leg, components) -> bool:
    return (
        str(batch.status) == "reconciling"
        and str(batch.reason_code)
        == "management_close_pending_exchange_confirmation"
        and batch.reconciled_at is None
        and batch.completed_at is None
        and str(leg.status) == "submitted"
        and [str(row.status) for row in components]
        == ["recovery_required", "pending", "pending"]
        and all(row.completed_at is None for row in components)
    )


def _legacy_false_state_evidence_refusal(leg, *, profile) -> str | None:
    snapshot_reason = _legacy_false_exchange_snapshot_refusal(
        leg, profile=profile
    )
    if snapshot_reason is not None:
        return snapshot_reason
    if leg.last_error in (None, ""):
        return None
    try:
        last_error = json.loads(str(leg.last_error))
    except (TypeError, ValueError, RecursionError):
        return "durable_evidence_invalid"
    if last_error != {"reason": "management_close_order_not_found"}:
        return "false_submission_state_mismatch"
    return None


def _legacy_false_exchange_snapshot_refusal(leg, *, profile) -> str | None:
    try:
        snapshot = json.loads(str(leg.last_exchange_snapshot_json))
    except (TypeError, ValueError, RecursionError):
        return "durable_evidence_invalid"
    if not isinstance(snapshot, Mapping) or set(snapshot) != {
        "position_rows", "matching_regular_orders"
    }:
        return "false_submission_state_mismatch"
    position_rows = snapshot.get("position_rows")
    if (
        not isinstance(position_rows, list)
        or len(position_rows) != 1
        or snapshot.get("matching_regular_orders") != []
    ):
        return "false_submission_state_mismatch"
    position_row = position_rows[0]
    if not isinstance(position_row, Mapping) or set(position_row) != {
        "posId", "instId", "posSide", "pos"
    }:
        return "false_submission_state_mismatch"
    if (
        str(position_row.get("posId") or "") != str(leg.pos_id)
        or str(position_row.get("instId") or "").upper()
        != profile.instrument_id.upper()
        or str(position_row.get("posSide") or "").lower()
        != profile.side.lower()
        or str(position_row.get("pos") or "") != profile.trusted_start_size
    ):
        return "false_submission_state_mismatch"
    return None


def _has_durable_close_submission(session, *, batch, leg, entry) -> bool:
    payload_filters = _management_payload_key_filters()
    try:
        mutation_candidates = (
            session.query(
                PositionMutationIntent.id.label("id"),
                PositionMutationIntent.operation.label("operation"),
            )
            .order_by(PositionMutationIntent.id)
            .limit(_MAX_DURABLE_CLOSE_CANDIDATES + 1)
            .all()
        )
    except (SQLAlchemyError, TypeError, ValueError, OverflowError):
        return True
    if len(mutation_candidates) > _MAX_DURABLE_CLOSE_CANDIDATES:
        return True
    close_operations: dict[int, str] = {}
    for row in mutation_candidates:
        if type(row.id) is not int or int(row.id) <= 0:
            return True
        if not isinstance(row.operation, str):
            return True
        if _mutation_operation_is_close_looking(row.operation):
            close_operations[int(row.id)] = row.operation
    if close_operations:
        try:
            close_candidates = (
                session.query(
                    PositionMutationIntent.id.label("id"),
                    PositionMutationIntent.operation.label("operation"),
                    PositionMutationIntent.strategy_instance_id.label(
                        "strategy_instance_id"
                    ),
                    PositionMutationIntent.execution_binding_id.label(
                        "execution_binding_id"
                    ),
                    PositionMutationIntent.execution_order_leg_id.label(
                        "execution_order_leg_id"
                    ),
                    PositionMutationIntent.pos_id.label("pos_id"),
                    PositionMutationIntent.venue.label("venue"),
                    PositionMutationIntent.status.label("status"),
                    PositionMutationIntent.request_json.label("request_json"),
                    PositionMutationIntent.response_json.label("response_json"),
                    PositionMutationIntent.error_json.label("error_json"),
                    cast(PositionMutationIntent.reserved_at, String).label(
                        "reserved_at_raw"
                    ),
                    cast(PositionMutationIntent.submitted_at, String).label(
                        "submitted_at_raw"
                    ),
                    cast(PositionMutationIntent.confirmed_at, String).label(
                        "confirmed_at_raw"
                    ),
                    cast(PositionMutationIntent.created_at, String).label(
                        "created_at_raw"
                    ),
                    cast(PositionMutationIntent.updated_at, String).label(
                        "updated_at_raw"
                    ),
                )
                .filter(PositionMutationIntent.id.in_(tuple(close_operations)))
                .order_by(PositionMutationIntent.id)
                .limit(_MAX_DURABLE_CLOSE_CANDIDATES + 1)
                .all()
            )
        except (SQLAlchemyError, TypeError, ValueError, OverflowError):
            return True
        if (
            len(close_candidates) != len(close_operations)
            or {row.id for row in close_candidates} != set(close_operations)
        ):
            return True
        for row in close_candidates:
            if (
                row.operation != close_operations[int(row.id)]
                or not _mutation_close_projection_is_valid(row)
            ):
                return True
            try:
                targets_complete_other = _mutation_targets_complete_other_owner(
                    session,
                    mutation=row,
                    batch=batch,
                    leg=leg,
                    entry=entry,
                )
            except (SQLAlchemyError, TypeError, ValueError, OverflowError):
                return True
            if not targets_complete_other:
                return True
    try:
        event_candidates = (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action.like("%close%"),
                or_(
                    ExecutionEvent.execution_binding_id
                    == int(batch.execution_binding_id),
                    ExecutionEvent.strategy_instance_id
                    == str(batch.strategy_instance_id),
                    ExecutionEvent.pos_id == str(leg.pos_id),
                    *(
                        column.like(pattern)
                        for column in (
                            ExecutionEvent.before_json,
                            ExecutionEvent.after_json,
                            ExecutionEvent.request_json,
                            ExecutionEvent.response_json,
                        )
                        for pattern in payload_filters
                    ),
                ),
            )
            .order_by(ExecutionEvent.id)
            .limit(_MAX_DURABLE_CLOSE_CANDIDATES + 1)
            .all()
        )
        if len(event_candidates) > _MAX_DURABLE_CLOSE_CANDIDATES:
            return True
        return any(
            _is_durable_close_event_action(event.action)
            and not _event_targets_other_management_entry(
                session,
                event=event,
                batch=batch,
                leg=leg,
                entry=entry,
            )
            for event in event_candidates
        )
    except (SQLAlchemyError, TypeError, ValueError, OverflowError):
        return True


def _management_payload_key_filters() -> tuple[str, ...]:
    exact_key_filters = tuple(
        f'%"{key}"%'
        for key in _MANAGEMENT_OWNER_KEYS
    )
    return (*exact_key_filters, "%management%", "%\\u%")


def _management_owner_payload_refs(
    row: Any,
    *,
    field_names: Sequence[str],
) -> tuple[set[int], set[int]] | None:
    management_leg_refs: set[int] = set()
    management_batch_refs: set[int] = set()
    budget = _ManagementPayloadBudget()
    for field_name in field_names:
        raw = getattr(row, field_name, None)
        if raw in (None, ""):
            continue
        if not _scan_management_payload_text(
            raw,
            budget=budget,
            management_leg_refs=management_leg_refs,
            management_batch_refs=management_batch_refs,
            depth=1,
            encoded_layers=0,
        ):
            return None
    return management_leg_refs, management_batch_refs


@dataclass(slots=True)
class _ManagementPayloadBudget:
    bytes_used: int = 0
    nodes_used: int = 0


def _management_text_is_json_looking(value: str) -> bool:
    stripped = value.lstrip()
    return bool(stripped and stripped[0] in '{["')


def _management_text_has_exact_owner_key(value: str) -> bool:
    probe = value
    for _ in range(_MAX_MANAGEMENT_ENCODED_JSON_LAYERS + 1):
        if any(key in probe for key in _MANAGEMENT_OWNER_KEYS):
            return True
        decoded = _JSON_UNICODE_ESCAPE_RE.sub(
            lambda match: chr(int(match.group(1), 16)),
            probe,
        ).replace("\\\\", "\\")
        if decoded == probe:
            break
        probe = decoded
    return False


def _scan_management_payload_text(
    value: Any,
    *,
    budget: _ManagementPayloadBudget,
    management_leg_refs: set[int],
    management_batch_refs: set[int],
    depth: int,
    encoded_layers: int,
) -> bool:
    if not isinstance(value, str):
        return False
    json_looking = _management_text_is_json_looking(value)
    has_exact_owner_key = _management_text_has_exact_owner_key(value)
    if not json_looking and not has_exact_owner_key:
        return True
    encoded_size = len(value.encode("utf-8"))
    budget.bytes_used += encoded_size
    if (
        budget.bytes_used > _MAX_INSTRUCTION_PAYLOAD_BYTES
        or encoded_layers > _MAX_MANAGEMENT_ENCODED_JSON_LAYERS
    ):
        return False
    try:
        payload = json.loads(
            value,
            object_pairs_hook=_instruction_json_object,
            parse_constant=_reject_instruction_json_constant,
        )
    except (TypeError, ValueError, RecursionError, OverflowError):
        return not has_exact_owner_key
    if not isinstance(payload, (Mapping, list, str)):
        return not has_exact_owner_key
    return _collect_management_owner_refs(
        payload,
        budget=budget,
        management_leg_refs=management_leg_refs,
        management_batch_refs=management_batch_refs,
        depth=depth,
        encoded_layers=encoded_layers,
    )


def _collect_management_owner_refs(
    value: Any,
    *,
    budget: _ManagementPayloadBudget,
    management_leg_refs: set[int],
    management_batch_refs: set[int],
    depth: int,
    encoded_layers: int,
) -> bool:
    budget.nodes_used += 1
    if (
        budget.nodes_used > _MAX_INSTRUCTION_PAYLOAD_NODES
        or depth > _MAX_INSTRUCTION_PAYLOAD_DEPTH
    ):
        return False
    if isinstance(value, Mapping):
        for key, child in value.items():
            destination = (
                management_leg_refs
                if key in {"management_leg_id", "managementLegId"}
                else management_batch_refs
                if key in {"management_batch_id", "managementBatchId"}
                else None
            )
            if destination is not None and child not in (None, ""):
                if isinstance(child, bool):
                    return False
                try:
                    destination.add(int(str(child)))
                except (TypeError, ValueError):
                    return False
            if not _collect_management_owner_refs(
                child,
                budget=budget,
                management_leg_refs=management_leg_refs,
                management_batch_refs=management_batch_refs,
                depth=depth + 1,
                encoded_layers=encoded_layers,
            ):
                return False
        return True
    if isinstance(value, list):
        return all(
            _collect_management_owner_refs(
                child,
                budget=budget,
                management_leg_refs=management_leg_refs,
                management_batch_refs=management_batch_refs,
                depth=depth + 1,
                encoded_layers=encoded_layers,
            )
            for child in value
        )
    if isinstance(value, str) and (
        _management_text_is_json_looking(value)
        or _management_text_has_exact_owner_key(value)
    ):
        if encoded_layers >= _MAX_MANAGEMENT_ENCODED_JSON_LAYERS:
            return False
        return _scan_management_payload_text(
            value,
            budget=budget,
            management_leg_refs=management_leg_refs,
            management_batch_refs=management_batch_refs,
            depth=depth + 1,
            encoded_layers=encoded_layers + 1,
        )
    if isinstance(value, float) and not math.isfinite(value):
        return False
    if value is not None and not isinstance(value, (bool, int, float, str)):
        return False
    return True


def _mutation_operation_is_close_looking(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    start = 0
    end = len(normalized)
    while start < end and normalized[start].isspace():
        start += 1
    while end > start and normalized[end - 1].isspace():
        end -= 1
    edge_stripped = normalized[start:end]
    if edge_stripped == "close_position":
        return True
    ascii_skeleton = "".join(
        character
        for character in edge_stripped
        if (
            "a" <= character <= "z"
            or "0" <= character <= "9"
            or character == "_"
        )
    )
    return ascii_skeleton == "close_position"


def _raw_mutation_datetime(
    value: object,
    *,
    required: bool,
) -> tuple[bool, datetime | None]:
    if value is None:
        return (not required), None
    if not isinstance(value, str) or not value.strip():
        return False, None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (TypeError, ValueError, OverflowError):
        return False, None
    normalized = _normalize_utc_datetime(parsed)
    return (normalized is not None), normalized


def _mutation_close_projection_is_valid(mutation: Any) -> bool:
    if (
        type(mutation.id) is not int
        or int(mutation.id) <= 0
        or not isinstance(mutation.operation, str)
        or not _mutation_operation_is_close_looking(mutation.operation)
        or type(mutation.execution_binding_id) is not int
        or int(mutation.execution_binding_id) <= 0
        or type(mutation.execution_order_leg_id) is not int
        or int(mutation.execution_order_leg_id) <= 0
    ):
        return False
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (
            mutation.strategy_instance_id,
            mutation.pos_id,
            mutation.venue,
            mutation.status,
            mutation.request_json,
        )
    ):
        return False
    if any(
        value is not None and not isinstance(value, str)
        for value in (mutation.response_json, mutation.error_json)
    ):
        return False
    time_values: dict[str, datetime | None] = {}
    for field_name, required in (
        ("reserved_at", True),
        ("submitted_at", False),
        ("confirmed_at", False),
        ("created_at", True),
        ("updated_at", True),
    ):
        valid, normalized = _raw_mutation_datetime(
            getattr(mutation, f"{field_name}_raw", None),
            required=required,
        )
        if not valid:
            return False
        time_values[field_name] = normalized
    reserved_at = time_values["reserved_at"]
    submitted_at = time_values["submitted_at"]
    confirmed_at = time_values["confirmed_at"]
    created_at = time_values["created_at"]
    updated_at = time_values["updated_at"]
    if created_at > updated_at:
        return False
    if submitted_at is not None and submitted_at < reserved_at:
        return False
    if confirmed_at is not None and confirmed_at < reserved_at:
        return False
    if (
        submitted_at is not None
        and confirmed_at is not None
        and confirmed_at < submitted_at
    ):
        return False
    return True


def _mutation_targets_complete_other_owner(
    session,
    *,
    mutation,
    batch,
    leg,
    entry,
) -> bool:
    if (
        int(mutation.execution_binding_id) == int(batch.execution_binding_id)
        or int(mutation.execution_order_leg_id) == int(entry.id)
        or str(mutation.strategy_instance_id) == str(batch.strategy_instance_id)
        or str(mutation.pos_id) == str(leg.pos_id)
    ):
        return False
    referenced_binding = session.get(
        ExecutionBinding,
        int(mutation.execution_binding_id),
    )
    referenced_entry = session.get(
        ExecutionOrderLeg,
        int(mutation.execution_order_leg_id),
    )
    if referenced_binding is None or referenced_entry is None:
        return False
    if not (
        str(mutation.strategy_instance_id or "")
        and str(mutation.pos_id or "")
        and int(referenced_entry.execution_binding_id)
        == int(referenced_binding.id)
        and str(referenced_binding.strategy_instance_id)
        == str(referenced_entry.strategy_instance_id or "")
        == str(mutation.strategy_instance_id)
        and str(referenced_binding.pos_id or "")
        == str(referenced_entry.pos_id or "")
        == str(mutation.pos_id)
        and str(referenced_binding.venue) == str(mutation.venue)
        == str(referenced_entry.venue)
    ):
        return False
    refs = _management_owner_payload_refs(
        mutation,
        field_names=("request_json", "response_json", "error_json"),
    )
    if refs is None:
        return False
    management_leg_refs, management_batch_refs = refs
    if int(batch.id) in management_batch_refs or int(leg.id) in management_leg_refs:
        return False
    if not management_leg_refs and not management_batch_refs:
        return True
    if len(management_leg_refs) != 1 or len(management_batch_refs) != 1:
        return False
    referenced_leg = session.get(
        StrategyManagementLeg,
        next(iter(management_leg_refs)),
    )
    referenced_batch = session.get(
        StrategyManagementBatch,
        next(iter(management_batch_refs)),
    )
    return bool(
        referenced_leg is not None
        and referenced_batch is not None
        and int(referenced_leg.management_batch_id) == int(referenced_batch.id)
        and int(referenced_leg.execution_order_leg_id) == int(referenced_entry.id)
        and str(referenced_leg.pos_id) == str(mutation.pos_id)
        and int(referenced_batch.execution_binding_id)
        == int(referenced_binding.id)
        and str(referenced_batch.strategy_instance_id)
        == str(mutation.strategy_instance_id)
    )


def _is_durable_close_event_action(value: object) -> bool:
    action = str(value or "").strip().lower()
    return action == "strategy_management_close_submit" or "close" in action


def _event_targets_other_management_entry(
    session,
    *,
    event,
    batch,
    leg,
    entry,
) -> bool:
    refs = _management_owner_payload_refs(
        event,
        field_names=(
            "before_json",
            "after_json",
            "request_json",
            "response_json",
        ),
    )
    if refs is None:
        return False
    management_leg_refs, management_batch_refs = refs
    if (
        len(management_leg_refs) != 1
        or len(management_batch_refs) != 1
        or int(event.execution_binding_id or 0)
        == int(batch.execution_binding_id)
        or str(event.strategy_instance_id or "")
        == str(batch.strategy_instance_id)
        or str(event.pos_id or "") == str(leg.pos_id)
        or int(leg.id) in management_leg_refs
        or int(batch.id) in management_batch_refs
    ):
        return False
    referenced_leg = session.get(
        StrategyManagementLeg,
        next(iter(management_leg_refs)),
    )
    referenced_batch = session.get(
        StrategyManagementBatch,
        next(iter(management_batch_refs)),
    )
    if referenced_leg is None or referenced_batch is None:
        return False
    referenced_entry = session.get(
        ExecutionOrderLeg,
        int(referenced_leg.execution_order_leg_id),
    )
    return (
        referenced_entry is not None
        and bool(str(event.strategy_instance_id or ""))
        and bool(str(event.pos_id or ""))
        and int(referenced_leg.management_batch_id) == int(referenced_batch.id)
        and int(referenced_leg.execution_order_leg_id) == int(referenced_entry.id)
        and int(referenced_entry.id) != int(entry.id)
        and int(referenced_batch.execution_binding_id)
        == int(event.execution_binding_id)
        and int(referenced_entry.execution_binding_id)
        == int(event.execution_binding_id)
        and str(event.strategy_instance_id or "")
        == str(referenced_batch.strategy_instance_id)
        == str(referenced_entry.strategy_instance_id or "")
        and str(event.pos_id or "")
        == str(referenced_leg.pos_id or "")
        == str(referenced_entry.pos_id or "")
        and str(referenced_batch.status)
        in _SAFE_TERMINAL_MANAGEMENT_STATUSES
    )


def _instruction_population_payload(
    session,
    *,
    batch: StrategyManagementBatch,
    profile: CompositeBatchRecoveryProfile,
) -> dict[str, Any]:
    rows = (
        session.query(MessageInstructionItem)
        .filter(
            MessageInstructionItem.retired_at.is_(None),
            MessageInstructionItem.status.notin_(
                _SAFE_TERMINAL_INSTRUCTION_STATUSES
            ),
        )
        .order_by(MessageInstructionItem.id)
        .all()
    )
    if not rows or len(rows) > _MAX_INSTRUCTION_POPULATION:
        raise CompositeBatchRecoveryRefusal("additional_active_work_present")
    evidence_rows: list[dict[str, Any]] = []
    counts = {key: 0 for key in _INSTRUCTION_DISPOSITIONS}
    for item in rows:
        candidate = session.get(SignalCandidate, int(item.signal_candidate_id))
        raw = session.get(RawMessage, int(item.raw_message_id))
        if (
            candidate is None
            or raw is None
            or int(candidate.raw_message_id) != int(item.raw_message_id)
        ):
            raise CompositeBatchRecoveryRefusal(
                "additional_active_work_present"
            )
        disposition, facts = _classify_instruction_disposition(
            session,
            item=item,
            candidate=candidate,
            raw=raw,
            batch=batch,
            profile=profile,
        )
        counts[disposition] += 1
        evidence_rows.append(
            {
                "item_ref": _redacted_ref("instruction_item", item.id),
                "raw_ref": _redacted_ref("instruction_raw", item.raw_message_id),
                "candidate_ref": _redacted_ref(
                    "instruction_candidate", item.signal_candidate_id
                ),
                "strategy_ref": (
                    None
                    if item.strategy_instance_id in (None, "")
                    else _redacted_ref(
                        "instruction_strategy", item.strategy_instance_id
                    )
                ),
                "sequence": int(item.sequence),
                "instruction_kind": str(item.instruction_kind),
                "status": str(item.status),
                "event_type": str(candidate.event_type),
                "management_action": candidate.management_action,
                "disposition": disposition,
                "updated_at": str(item.updated_at),
                "item_fingerprint": _durable_row_fingerprint(item),
                "candidate_fingerprint": _durable_row_fingerprint(candidate),
                "raw_fingerprint": _durable_row_fingerprint(raw),
                "facts": facts,
            }
        )
    if counts["target_incident_frozen"] != 1:
        raise CompositeBatchRecoveryRefusal("additional_active_work_present")
    return {
        "schema_version": 1,
        "total_count": len(evidence_rows),
        "counts": counts,
        "rows": evidence_rows,
    }


def _classify_instruction_disposition(
    session,
    *,
    item: MessageInstructionItem,
    candidate: SignalCandidate,
    raw: RawMessage,
    batch: StrategyManagementBatch,
    profile: CompositeBatchRecoveryProfile,
) -> tuple[str, dict[str, Any]]:
    target_facts = _target_incident_instruction_facts(
        session,
        item=item,
        candidate=candidate,
        batch=batch,
        profile=profile,
    )
    if target_facts is not None:
        return "target_incident_frozen", target_facts
    if str(item.status) == "submitted":
        mirror_facts = _verified_terminal_mirror_facts(
            session,
            item=item,
            candidate=candidate,
            raw=raw,
        )
        if mirror_facts is not None:
            return "verified_terminal_mirror", mirror_facts
    elif str(item.status) == "pending":
        residue_facts = _historical_pending_residue_facts(
            session,
            item=item,
            candidate=candidate,
            raw=raw,
        )
        if residue_facts is not None:
            return "approved_historical_pending_frozen", residue_facts
    elif str(item.status) == "unknown":
        unknown_facts = _historical_unknown_facts(
            session,
            item=item,
            candidate=candidate,
        )
        if unknown_facts is not None:
            return "historical_unknown_frozen", unknown_facts
    raise CompositeBatchRecoveryRefusal("additional_active_work_present")


def _target_incident_instruction_facts(
    session,
    *,
    item: MessageInstructionItem,
    candidate: SignalCandidate,
    batch: StrategyManagementBatch,
    profile: CompositeBatchRecoveryProfile,
) -> dict[str, Any] | None:
    if not (
        int(item.raw_message_id) == profile.raw_message_id
        and str(item.instruction_kind) == "management"
        and str(item.status) == "unknown"
        and str(item.strategy_instance_id or "")
        == str(batch.strategy_instance_id)
        and int(candidate.target_lifecycle_id or 0) == profile.lifecycle_id
        and str(candidate.event_type) == "position_update"
        and str(candidate.management_action or "") == str(batch.intent)
        and str(candidate.symbol or "").upper()
        == profile.instrument_id.split("-", 1)[0].upper()
        and str(candidate.side or "").lower() == profile.side.lower()
        and _instruction_workflow_is_clear(item)
    ):
        return None
    payload = _bounded_instruction_payload(item.error_json)
    if (
        item.result_json is not None
        or payload is None
        or set(payload) != {"batch_id", "reason", "status", "submitted"}
        or _exact_int(payload.get("batch_id")) != int(batch.id)
        or payload.get("reason") not in (None, "")
        or payload.get("status") != "recovery_required"
        or payload.get("submitted") is not False
        or _instruction_contract_exists(session, item_id=item.id)
        or _management_target_exists(session, item_id=item.id)
    ):
        raise CompositeBatchRecoveryRefusal("additional_active_work_present")
    return {
        "batch_ref": _redacted_ref("instruction_batch", batch.id),
        "candidate_identity_fingerprint": _durable_row_fingerprint(candidate),
        "payload_fingerprint": _fingerprint(payload),
    }


def _verified_terminal_mirror_facts(
    session,
    *,
    item: MessageInstructionItem,
    candidate: SignalCandidate,
    raw: RawMessage,
) -> dict[str, Any] | None:
    if item.error_json is not None:
        return None
    payload = _bounded_instruction_payload(item.result_json)
    if payload is None:
        return None
    contract = _instruction_contract(session, item_id=item.id)
    if str(item.instruction_kind) == "entry":
        result = payload.get("result")
        if (
            str(candidate.event_type) != "entry_signal"
            or payload.get("status") != "submitted"
            or not isinstance(result, Mapping)
            or result.get("submitted") is not True
            or _exact_int(result.get("order_count")) is None
            or int(result["order_count"]) < 1
            or not isinstance(result.get("orders"), list)
            or len(result["orders"]) != int(result["order_count"])
            or (
                contract is not None
                and not (
                    str(contract.intent_kind) == "entry"
                    and str(contract.state) == "verified"
                    and str(contract.terminal_kind) == "verified_entry"
                    and str(contract.completion_scope) in {"full", "partial"}
                    and bool(contract.attempted_exchange_write)
                )
            )
        ):
            return None
        signal_id = _exact_int(result.get("signal_id"))
        signal = session.get(TradeSignal, signal_id) if signal_id else None
        bindings = _instruction_bindings(
            session, strategy_instance_id=item.strategy_instance_id
        )
        binding = bindings[0] if len(bindings) == 1 else None
        if (
            signal is None
            or binding is None
            or str(signal.status) != "submitted"
            or str(signal.source_type) != "recovery"
            or str(signal.action) != "open_position"
            or str(signal.venue) != "deepcoin"
            or str(signal.strategy_instance_id or "")
            != str(item.strategy_instance_id or "")
            or int(signal.chat_id) != int(raw.chat_id)
            or int(signal.message_id) != int(raw.message_id)
            or str(signal.symbol) != str(candidate.symbol)
            or str(signal.side) != str(candidate.side)
            or str(binding.strategy_instance_id or "")
            != str(item.strategy_instance_id or "")
            or int(binding.chat_id) != int(raw.chat_id)
            or int(binding.message_id) != int(raw.message_id)
            or str(binding.symbol) != str(candidate.symbol)
            or str(binding.side) != str(candidate.side)
            or str(binding.venue) != "deepcoin"
            or (
                contract is not None
                and not _verified_contract_identity_matches(
                    contract,
                    item=item,
                    intent_kind="entry",
                    trade_signal_id=int(signal.id),
                    execution_binding_id=int(binding.id),
                )
            )
        ):
            return None
        return {
            "binding_ref": _redacted_ref(
                "instruction_binding", binding.id
            ),
            "binding_status": str(binding.status),
            "binding_fingerprint": _durable_row_fingerprint(binding),
            "contract_fingerprint": _verified_contract_fingerprint(contract),
            "payload_fingerprint": _fingerprint(payload),
            "trade_signal_ref": _redacted_ref(
                "instruction_trade_signal", signal.id
            ),
            "trade_signal_status": str(signal.status),
            "trade_signal_fingerprint": _durable_row_fingerprint(signal),
        }
    if str(item.instruction_kind) != "management":
        return None
    batch_id = _exact_int(payload.get("batch_id"))
    linked = session.get(StrategyManagementBatch, batch_id) if batch_id else None
    lifecycle = (
        session.get(StrategyLifecycle, int(candidate.target_lifecycle_id))
        if candidate.target_lifecycle_id is not None
        else None
    )
    binding = (
        session.get(ExecutionBinding, int(linked.execution_binding_id))
        if linked is not None
        else None
    )
    if (
        linked is None
        or lifecycle is None
        or binding is None
        or str(candidate.event_type) not in {"position_update", "close_signal"}
        or payload.get("submitted") is not True
        or payload.get("status") not in {"reconciling", "submitted", "succeeded"}
        or str(linked.status) not in _SAFE_TERMINAL_MANAGEMENT_STATUSES
        or int(linked.raw_message_id) != int(item.raw_message_id)
        or int(linked.target_lifecycle_id)
        != int(candidate.target_lifecycle_id or 0)
        or str(linked.strategy_instance_id)
        != str(item.strategy_instance_id or "")
        or str(linked.intent) != str(candidate.management_action or "")
        or int(lifecycle.id) != int(linked.target_lifecycle_id)
        or lifecycle.execution_binding_id is None
        or int(lifecycle.execution_binding_id) != int(binding.id)
        or str(candidate.symbol or "") != str(lifecycle.symbol)
        or str(candidate.side or "") != str(lifecycle.side)
        or int(binding.id) != int(linked.execution_binding_id)
        or str(binding.strategy_instance_id or "")
        != str(item.strategy_instance_id or "")
        or int(binding.chat_id) != int(lifecycle.chat_id)
        or int(binding.message_id) != int(lifecycle.message_id)
        or str(binding.symbol) != str(lifecycle.symbol)
        or str(binding.side) != str(lifecycle.side)
        or str(binding.venue) != "deepcoin"
        or str(binding.status).lower() not in {"open", "active", "closed"}
        or (
            contract is not None
            and not _verified_contract_identity_matches(
                contract,
                item=item,
                intent_kind="management",
                trade_signal_id=None,
                execution_binding_id=int(linked.execution_binding_id),
            )
        )
        or (
            contract is not None
            and not (
                str(contract.intent_kind) == "management"
                and str(contract.state) == "verified"
                and str(contract.terminal_kind)
                in {"verified_management", "verified_cancel", "verified_exit"}
                and str(contract.completion_scope) in {"full", "partial"}
                and bool(contract.attempted_exchange_write)
            )
        )
    ):
        return None
    return {
        "batch_ref": _redacted_ref("instruction_batch", linked.id),
        "batch_fingerprint": _durable_row_fingerprint(linked),
        "batch_status": str(linked.status),
        "binding_fingerprint": _durable_row_fingerprint(binding),
        "contract_fingerprint": _verified_contract_fingerprint(contract),
        "lifecycle_fingerprint": _durable_row_fingerprint(lifecycle),
        "payload_fingerprint": _fingerprint(payload),
    }


def _verified_contract_identity_matches(
    contract: InstructionExecutionContract,
    *,
    item: MessageInstructionItem,
    intent_kind: str,
    trade_signal_id: int | None,
    execution_binding_id: int,
) -> bool:
    evidence_text = contract.evidence_refs_json
    evidence_refs = (
        _bounded_instruction_list(evidence_text)
        if isinstance(evidence_text, str)
        and len(evidence_text.encode("utf-8")) <= 4096
        else None
    )
    return bool(
        evidence_refs is not None
        and all(isinstance(row, Mapping) for row in evidence_refs)
        and int(contract.message_instruction_item_id) == int(item.id)
        and int(contract.raw_message_id) == int(item.raw_message_id)
        and int(contract.signal_candidate_id) == int(item.signal_candidate_id)
        and str(contract.strategy_instance_id or "")
        == str(item.strategy_instance_id or "")
        and str(contract.intent_kind) == intent_kind
        and (
            (contract.trade_signal_id is None and trade_signal_id is None)
            or (
                contract.trade_signal_id is not None
                and trade_signal_id is not None
                and int(contract.trade_signal_id) == int(trade_signal_id)
            )
        )
        and contract.execution_binding_id is not None
        and int(contract.execution_binding_id) == int(execution_binding_id)
    )


def _verified_contract_fingerprint(
    contract: InstructionExecutionContract | None,
) -> str | None:
    if contract is None:
        return None
    return _durable_row_fingerprint(contract)


def _instruction_workflow_is_clear(item: MessageInstructionItem) -> bool:
    return bool(
        item.visibility_first_failed_at is None
        and int(item.visibility_retry_attempts or 0) == 0
        and item.visibility_next_attempt_at is None
        and item.execution_deadline_at is None
        and item.operator_escalation_at is None
        and item.last_progress_at is None
        and item.escalation_state is None
        and item.escalation_notified_at is None
    )


def _historical_pending_residue_facts(
    session,
    *,
    item: MessageInstructionItem,
    candidate: SignalCandidate,
    raw: RawMessage,
) -> dict[str, Any] | None:
    if (
        item.result_json is not None
        or item.error_json is not None
        or item.visibility_first_failed_at is not None
        or int(item.visibility_retry_attempts or 0) != 0
        or item.visibility_next_attempt_at is not None
        or item.execution_deadline_at is not None
        or item.operator_escalation_at is not None
        or item.last_progress_at is not None
        or item.escalation_state is not None
        or item.escalation_notified_at is not None
        or _instruction_contract_exists(session, item_id=item.id)
        or _management_target_exists(session, item_id=item.id)
        or _has_nonterminal_descendant(session, raw_message_id=item.raw_message_id)
        or session.query(MimoRecognitionRun.id)
        .filter(
            MimoRecognitionRun.raw_message_id == int(item.raw_message_id),
            MimoRecognitionRun.status == "running",
        )
        .first()
        is not None
        or session.get(
            MessageEvidenceExtractionClaim,
            int(item.raw_message_id),
        )
        is not None
    ):
        return None
    lifecycle = (
        session.get(StrategyLifecycle, int(candidate.target_lifecycle_id))
        if candidate.target_lifecycle_id is not None
        else None
    )
    if candidate.target_lifecycle_id is not None and (
        lifecycle is None or str(lifecycle.lifecycle_status) != "exited"
    ):
        return None
    bindings = _instruction_bindings(
        session, strategy_instance_id=item.strategy_instance_id
    )
    if any(str(row.status) != "closed" for row in bindings):
        return None
    signal_exists = (
        session.query(TradeSignal.id)
        .filter(
            TradeSignal.chat_id == int(raw.chat_id),
            TradeSignal.message_id == int(raw.message_id),
            TradeSignal.symbol == candidate.symbol,
            TradeSignal.side == candidate.side,
        )
        .first()
        is not None
    )
    if signal_exists:
        return None
    identity_digest = _historical_pending_residue_identity_digest(
        session,
        item=item,
        candidate=candidate,
        raw=raw,
    )
    if (
        identity_digest
        not in _APPROVED_BATCH_119_PENDING_RESIDUE_IDENTITY_DIGESTS
    ):
        return None
    management_descendants = (
        session.query(StrategyManagementBatch)
        .filter(StrategyManagementBatch.raw_message_id == int(item.raw_message_id))
        .order_by(StrategyManagementBatch.id)
        .all()
    )
    revision_descendants = (
        session.query(StrategyRevisionBatch)
        .filter(StrategyRevisionBatch.raw_message_id == int(item.raw_message_id))
        .order_by(StrategyRevisionBatch.id)
        .all()
    )
    return {
        "approved_identity_digest": identity_digest,
        "binding_count": len(bindings),
        "binding_fingerprints": [
            _durable_row_fingerprint(row) for row in bindings
        ],
        "binding_states": sorted(str(row.status) for row in bindings),
        "management_descendant_fingerprints": [
            _durable_row_fingerprint(row) for row in management_descendants
        ],
        "revision_descendant_fingerprints": [
            _durable_row_fingerprint(row) for row in revision_descendants
        ],
        "lifecycle_ref": (
            None
            if lifecycle is None
            else _redacted_ref("instruction_lifecycle", lifecycle.id)
        ),
        "lifecycle_fingerprint": (
            None if lifecycle is None else _durable_row_fingerprint(lifecycle)
        ),
        "lifecycle_status": None if lifecycle is None else "exited",
    }


def _historical_pending_residue_identity_digest(
    session,
    *,
    item: MessageInstructionItem,
    candidate: SignalCandidate,
    raw: RawMessage,
) -> str:
    decisions = (
        session.query(RecognitionDecision)
        .filter(RecognitionDecision.raw_message_id == int(item.raw_message_id))
        .order_by(RecognitionDecision.id)
        .all()
    )
    context_attempts = (
        session.query(ContextResolutionAttempt)
        .filter(ContextResolutionAttempt.raw_message_id == int(item.raw_message_id))
        .order_by(ContextResolutionAttempt.id)
        .all()
    )
    entry_attempts = (
        session.query(EntryAssemblyAttempt)
        .filter(
            EntryAssemblyAttempt.strategy_raw_message_id
            == int(item.raw_message_id),
            EntryAssemblyAttempt.signal_candidate_id
            == int(item.signal_candidate_id),
        )
        .order_by(EntryAssemblyAttempt.id)
        .all()
    )
    source = (
        session.get(Source, int(candidate.source_id))
        if candidate.source_id is not None
        else None
    )
    media_assets = (
        session.query(MediaAsset)
        .filter(MediaAsset.raw_message_id == int(item.raw_message_id))
        .order_by(MediaAsset.id)
        .all()
    )
    evidence_versions = (
        session.query(MessageEvidenceVersion)
        .filter(MessageEvidenceVersion.raw_message_id == int(item.raw_message_id))
        .order_by(MessageEvidenceVersion.id)
        .all()
    )
    recognition_runs = (
        session.query(MimoRecognitionRun)
        .filter(MimoRecognitionRun.raw_message_id == int(item.raw_message_id))
        .order_by(MimoRecognitionRun.id)
        .all()
    )
    run_ids = [int(row.id) for row in recognition_runs]
    recognition_attempts = (
        []
        if not run_ids
        else session.query(MimoRecognitionAttempt)
        .filter(MimoRecognitionAttempt.run_id.in_(run_ids))
        .order_by(MimoRecognitionAttempt.run_id, MimoRecognitionAttempt.id)
        .all()
    )
    extraction_claim = session.get(
        MessageEvidenceExtractionClaim,
        int(item.raw_message_id),
    )
    return _fingerprint(
        {
            "schema_version": 1,
            "item_fingerprint": _durable_row_fingerprint(item),
            "candidate_fingerprint": _durable_row_fingerprint(candidate),
            "raw_fingerprint": _durable_row_fingerprint(raw),
            "source_fingerprint": (
                None if source is None else _durable_row_fingerprint(source)
            ),
            "media_fingerprints": [
                _durable_row_fingerprint(row) for row in media_assets
            ],
            "evidence_version_fingerprints": [
                _durable_row_fingerprint(row) for row in evidence_versions
            ],
            "recognition_run_fingerprints": [
                _durable_row_fingerprint(row) for row in recognition_runs
            ],
            "recognition_attempt_fingerprints": [
                _durable_row_fingerprint(row) for row in recognition_attempts
            ],
            "extraction_claim_fingerprint": (
                None
                if extraction_claim is None
                else _durable_row_fingerprint(extraction_claim)
            ),
            "recognition_decision_fingerprints": [
                _durable_row_fingerprint(row) for row in decisions
            ],
            "context_attempt_fingerprints": [
                _durable_row_fingerprint(row) for row in context_attempts
            ],
            "entry_attempt_fingerprints": [
                _durable_row_fingerprint(row) for row in entry_attempts
            ],
        }
    )


def _historical_unknown_facts(
    session,
    *,
    item: MessageInstructionItem,
    candidate: SignalCandidate,
) -> dict[str, Any] | None:
    if (
        item.result_json is not None
        or not _instruction_workflow_is_clear(item)
        or _instruction_contract_exists(session, item_id=item.id)
        or _management_target_exists(session, item_id=item.id)
    ):
        return None
    payload = _bounded_instruction_payload(item.error_json)
    batch_id = _exact_int(payload.get("batch_id")) if payload else None
    submitted = payload.get("submitted") if payload is not None else None
    if (
        payload is None
        or payload.get("status") != "recovery_required"
        or (submitted is not None and submitted is not False)
        or not batch_id
    ):
        return None
    lifecycle = (
        session.get(StrategyLifecycle, int(candidate.target_lifecycle_id))
        if candidate.target_lifecycle_id is not None
        else None
    )
    bindings = _instruction_bindings(
        session, strategy_instance_id=item.strategy_instance_id
    )
    if (
        str(item.instruction_kind) != "management"
        or lifecycle is None
        or str(lifecycle.lifecycle_status) != "exited"
        or not bindings
        or any(str(row.status) != "closed" for row in bindings)
        or str(candidate.symbol or "") != str(lifecycle.symbol)
        or str(candidate.side or "") != str(lifecycle.side)
        or any(
            str(row.strategy_instance_id or "")
            != str(item.strategy_instance_id or "")
            or int(row.chat_id) != int(lifecycle.chat_id)
            or int(row.message_id) != int(lifecycle.message_id)
            or str(row.symbol) != str(lifecycle.symbol)
            or str(row.side) != str(lifecycle.side)
            or str(row.venue) != "deepcoin"
            for row in bindings
        )
    ):
        return None
    linked: Any
    terminal_statuses: set[str] | frozenset[str]
    if str(candidate.event_type) == "strategy_revision":
        linked = session.get(StrategyRevisionBatch, batch_id)
        terminal_statuses = {"succeeded", "blocked"}
    elif str(candidate.event_type) in {"position_update", "close_signal"}:
        linked = session.get(StrategyManagementBatch, batch_id)
        terminal_statuses = _SAFE_TERMINAL_MANAGEMENT_STATUSES
    else:
        return None
    if (
        linked is None
        or str(linked.status) not in terminal_statuses
        or int(linked.raw_message_id) != int(item.raw_message_id)
        or int(linked.target_lifecycle_id) != int(candidate.target_lifecycle_id)
        or lifecycle.execution_binding_id is None
        or int(lifecycle.execution_binding_id) != int(linked.execution_binding_id)
        or int(linked.execution_binding_id)
        not in {int(row.id) for row in bindings}
        or (
            isinstance(linked, StrategyManagementBatch)
            and (
                str(linked.strategy_instance_id)
                != str(item.strategy_instance_id or "")
                or str(linked.intent) != str(candidate.management_action or "")
            )
        )
        or (
            isinstance(linked, StrategyRevisionBatch)
            and str(candidate.management_action or "") != "replace_entry"
        )
    ):
        return None
    return {
        "binding_fingerprints": sorted(
            _execution_binding_fingerprint(row) for row in bindings
        ),
        "candidate_fingerprint": _signal_candidate_fingerprint(candidate),
        "descendant_ref": _redacted_ref("instruction_descendant", linked.id),
        "descendant_fingerprint": _historical_descendant_fingerprint(linked),
        "descendant_status": str(linked.status),
        "lifecycle_ref": _redacted_ref(
            "instruction_lifecycle", lifecycle.id
        ),
        "lifecycle_fingerprint": _durable_row_fingerprint(lifecycle),
        "payload_fingerprint": _fingerprint(payload),
    }


def _signal_candidate_fingerprint(candidate: SignalCandidate) -> str:
    return _durable_row_fingerprint(candidate)


def _execution_binding_fingerprint(binding: ExecutionBinding) -> str:
    return _durable_row_fingerprint(binding)


def _historical_descendant_fingerprint(
    row: StrategyManagementBatch | StrategyRevisionBatch,
) -> str:
    return _durable_row_fingerprint(row)


def _bounded_instruction_payload(value: str | None) -> Mapping[str, Any] | None:
    payload = _bounded_instruction_json(value)
    return payload if isinstance(payload, Mapping) else None


def _bounded_instruction_list(value: str | None) -> list[Any] | None:
    payload = _bounded_instruction_json(value)
    return payload if isinstance(payload, list) else None


def _bounded_instruction_json(value: str | None) -> Any:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_INSTRUCTION_PAYLOAD_BYTES
    ):
        return None
    try:
        payload = json.loads(
            value,
            object_pairs_hook=_instruction_json_object,
            parse_constant=_reject_instruction_json_constant,
        )
        if not _instruction_json_shape_is_bounded(payload):
            return None
        _fingerprint(payload)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return None
    return payload


def _durable_row_fingerprint(row: Any) -> str:
    columns = getattr(getattr(row, "__table__", None), "columns", None)
    if columns is None:
        raise TypeError("durable ORM row required")
    return _fingerprint(
        {
            str(column.name): _durable_scalar(getattr(row, column.name))
            for column in columns
        }
    )


def _durable_scalar(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite durable float")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError("unsupported durable scalar")


def _instruction_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = item
    return result


def _reject_instruction_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def _instruction_json_shape_is_bounded(value: Any) -> bool:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if (
            nodes > _MAX_INSTRUCTION_PAYLOAD_NODES
            or depth > _MAX_INSTRUCTION_PAYLOAD_DEPTH
        ):
            return False
        if isinstance(current, Mapping):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, float) and not math.isfinite(current):
            return False
        elif current is not None and not isinstance(
            current,
            (bool, int, float, str),
        ):
            return False
    return True


def _instruction_bindings(
    session, *, strategy_instance_id: str | None
) -> list[ExecutionBinding]:
    if strategy_instance_id in (None, ""):
        return []
    return (
        session.query(ExecutionBinding)
        .filter(ExecutionBinding.strategy_instance_id == str(strategy_instance_id))
        .order_by(ExecutionBinding.id)
        .all()
    )


def _instruction_contract_exists(session, *, item_id: int) -> bool:
    return _instruction_contract(session, item_id=item_id) is not None


def _instruction_contract(
    session, *, item_id: int
) -> InstructionExecutionContract | None:
    return (
        session.query(InstructionExecutionContract)
        .filter(
            InstructionExecutionContract.message_instruction_item_id
            == int(item_id)
        )
        .one_or_none()
    )


def _management_target_exists(session, *, item_id: int) -> bool:
    return (
        session.query(ManagementMessageTarget.id)
        .filter(ManagementMessageTarget.message_instruction_item_id == int(item_id))
        .first()
        is not None
    )


def _has_nonterminal_descendant(session, *, raw_message_id: int) -> bool:
    return bool(
        session.query(StrategyManagementBatch.id)
        .filter(
            StrategyManagementBatch.raw_message_id == int(raw_message_id),
            StrategyManagementBatch.status.notin_(
                _SAFE_TERMINAL_MANAGEMENT_STATUSES
            ),
        )
        .first()
        or session.query(StrategyRevisionBatch.id)
        .filter(
            StrategyRevisionBatch.raw_message_id == int(raw_message_id),
            StrategyRevisionBatch.status.notin_(("succeeded", "blocked")),
        )
        .first()
    )


def _instruction_population_summary(
    population: Mapping[str, Any],
) -> dict[str, Any]:
    rows = population.get("rows")
    counts = population.get("counts")
    if not isinstance(rows, list) or not isinstance(counts, Mapping):
        raise CompositeBatchRecoveryRefusal("additional_active_work_present")
    return {
        "schema_version": 1,
        "total_count": int(population["total_count"]),
        "counts": {key: int(counts[key]) for key in _INSTRUCTION_DISPOSITIONS},
        "digest": _fingerprint(rows),
    }


def _instruction_population_from_plan(
    plan: CompositeBatchRecoveryPlan,
) -> dict[str, Any]:
    try:
        value = _plain_json_value(plan.evidence)["durable"][
            "instruction_population"
        ]
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise CompositeBatchRecoveryConflict(
            "plan_evidence_inconsistent"
        ) from exc
    if not _instruction_population_summary_is_valid(value):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    return {
        "schema_version": 1,
        "total_count": int(value["total_count"]),
        "counts": {
            key: int(value["counts"][key])
            for key in _INSTRUCTION_DISPOSITIONS
        },
        "digest": str(value["digest"]),
    }


def _instruction_population_summary_is_valid(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and set(value) == {"schema_version", "total_count", "counts", "digest"}
        and value.get("schema_version") == 1
        and not isinstance(value.get("total_count"), bool)
        and isinstance(value.get("total_count"), int)
        and 1 <= value.get("total_count") <= _MAX_INSTRUCTION_POPULATION
        and isinstance(value.get("counts"), Mapping)
        and set(value["counts"]) == set(_INSTRUCTION_DISPOSITIONS)
        and all(
            not isinstance(count, bool)
            and isinstance(count, int)
            and count >= 0
            for count in value["counts"].values()
        )
        and sum(value["counts"].values()) == value["total_count"]
        and value["counts"].get("target_incident_frozen") == 1
        and _is_sha256(value.get("digest"))
    )


def _has_additional_active_database_work(session, *, batch_id: int) -> bool:
    other_batch = (
        session.query(StrategyManagementBatch.id)
        .filter(
            StrategyManagementBatch.id != int(batch_id),
            StrategyManagementBatch.status.notin_(
                _SAFE_TERMINAL_MANAGEMENT_STATUSES
            ),
        )
        .first()
    )
    if other_batch is not None:
        return True
    if (
        session.query(StrategyManagementComponent.id)
        .filter(
            StrategyManagementComponent.management_batch_id != int(batch_id),
            StrategyManagementComponent.status.notin_(
                _SAFE_TERMINAL_COMPONENT_STATUSES
            ),
        )
        .first()
        is not None
    ):
        return True
    if (
        session.query(PositionMutationIntent.id)
        .filter(PositionMutationIntent.status.notin_(_TERMINAL_MUTATION_STATUSES))
        .first()
        is not None
    ):
        return True
    batch = session.get(StrategyManagementBatch, int(batch_id))
    if batch is None:
        return True
    try:
        _instruction_population_payload(
            session,
            batch=batch,
            profile=BATCH_119_RECOVERY,
        )
    except CompositeBatchRecoveryRefusal:
        return True
    return False


def _validated_natural_stop_proof(
    *,
    snapshot: Any,
    ledger: Sequence[Any],
    pos_id: str,
    profile: CompositeBatchRecoveryProfile,
    not_before_ms: int,
) -> Mapping[str, Any] | str:
    """Prove one exact owned natural stop without retaining raw evidence."""

    scope = getattr(snapshot, "exact_scope", None)
    if (
        not isinstance(scope, _Batch119ExactHistoryScope)
        or str(scope.position_id) != str(pos_id)
        or str(scope.instrument_id).upper() != profile.instrument_id.upper()
        or str(scope.side).lower() != profile.side.lower()
    ):
        return "natural_stop_proof_identity_invalid"
    if any(
        isinstance(row, Mapping)
        and _row_matches_position(row, pos_id=str(pos_id))
        for row in snapshot.positions
    ):
        return "natural_stop_proof_position_invalid"

    scoped_orders = dict(scope.protection_orders)
    if (
        len(scoped_orders) != 2
        or set(scoped_orders) != {"stop_loss", "backup_stop"}
        or len(set(scoped_orders.values())) != 2
    ):
        return "natural_stop_proof_identity_invalid"
    owned_by_id: dict[str, tuple[str, Any]] = {}
    for row in ledger:
        purpose = str(getattr(row, "purpose", "") or "").lower()
        order_id = str(getattr(row, "order_id", "") or "").strip()
        if (
            purpose not in scoped_orders
            or scoped_orders[purpose] != order_id
            or order_id in owned_by_id
            or str(getattr(row, "status", "") or "").lower() != "verified"
            or str(getattr(row, "venue", "") or "").lower() != "deepcoin"
            or str(getattr(row, "pos_id", "") or "") != str(pos_id)
            or str(getattr(row, "instrument_id", "") or "").upper()
            != profile.instrument_id.upper()
            or str(getattr(row, "side", "") or "").lower()
            != profile.side.lower()
        ):
            return "natural_stop_proof_identity_invalid"
        owned_by_id[order_id] = (purpose, row)
    if set(owned_by_id) != set(scoped_orders.values()):
        return "natural_stop_proof_identity_invalid"

    position_rows = list(snapshot.position_history)
    if len(position_rows) != 1:
        return "natural_stop_proof_position_invalid"
    position_row = position_rows[0]
    if not isinstance(position_row, Mapping) or not (
        _exact_position_rows_match_scope(
            [position_row],
            position_id=str(pos_id),
            instrument_id=profile.instrument_id,
            side=profile.side,
        )
    ):
        return "natural_stop_proof_position_invalid"
    position_state = _one_exact_text(position_row, "state", "status")
    if position_state is None or position_state.lower() not in {
        "closed",
        "filled",
        "completed",
        "exited",
    }:
        return "natural_stop_proof_position_invalid"
    close_times = _bounded_epoch_ms_values(
        position_row,
        "uTime",
        "closeTime",
        "updateTime",
        "updatedAt",
        "updated_at",
    )
    if close_times is None or not close_times:
        return "natural_stop_proof_time_invalid"

    capture_ended_at = getattr(snapshot, "capture_ended_at", None)
    if (
        not isinstance(capture_ended_at, datetime)
        or capture_ended_at.tzinfo is None
    ):
        return "natural_stop_proof_time_invalid"
    capture_ended_ms = int(capture_ended_at.timestamp() * 1000)
    if any(value < not_before_ms for value in close_times):
        return "natural_stop_proof_before_incident"
    if any(value > capture_ended_ms for value in close_times):
        return "natural_stop_proof_after_capture"

    successful: list[tuple[str, str, int]] = []
    seen_trigger_order_ids: set[str] = set()
    for row in snapshot.trigger_history:
        if not isinstance(row, Mapping):
            return "natural_stop_proof_trigger_invalid"
        order_id = _unique_exact_row_identity(
            row, keys=("ordId", "orderId", "order_id")
        )
        if (
            order_id not in owned_by_id
            or order_id in seen_trigger_order_ids
            or not _exact_order_rows_match_scope(
                [row],
                order_id=str(order_id or ""),
                instrument_id=profile.instrument_id,
                side=profile.side,
            )
        ):
            return "natural_stop_proof_trigger_invalid"
        seen_trigger_order_ids.add(order_id)
        state = _one_exact_text(row, "state", "status")
        if state is None:
            return "natural_stop_proof_trigger_invalid"
        normalized_state = state.lower()
        if normalized_state in {"filled", "triggered", "completed", "closed"}:
            if _one_exact_text(row, "errorCode") != "0":
                return "natural_stop_proof_trigger_invalid"
            trigger_times = _bounded_epoch_ms_values(row, "triggerTime")
            if trigger_times is None or len(trigger_times) != 1:
                return "natural_stop_proof_time_invalid"
            purpose = owned_by_id[order_id][0]
            successful.append((purpose, order_id, trigger_times[0]))
        elif normalized_state not in {
            "cancelled",
            "canceled",
            "failed",
            "rejected",
            "expired",
        }:
            return "natural_stop_proof_trigger_invalid"

    if len(successful) > 1:
        return "natural_stop_proof_ambiguous"
    if len(successful) != 1:
        return "natural_stop_proof_trigger_invalid"
    purpose, order_id, trigger_time = successful[0]
    if trigger_time < not_before_ms:
        return "natural_stop_proof_before_incident"
    if (
        trigger_time > capture_ended_ms
    ):
        return "natural_stop_proof_after_capture"
    if any(close_time < trigger_time for close_time in close_times):
        return "natural_stop_proof_time_invalid"
    if _natural_stop_has_residual_close_evidence(snapshot, pos_id=str(pos_id)):
        return "natural_stop_proof_residual_close_evidence"

    return MappingProxyType(
        {
            "purpose": purpose,
            "trigger_status": "successful_terminal",
            "position_status": "closed",
            "time_relation": "trigger_not_after_close",
            "trigger_count": 1,
            "closed_position_count": 1,
            "order_ref": _redacted_ref("natural_stop_order", order_id),
            "position_ref": _redacted_ref("natural_stop_position", pos_id),
        }
    )


def _one_exact_text(row: Mapping[str, Any], *keys: str) -> str | None:
    values = {
        str(row[key]).strip()
        for key in keys
        if row.get(key) not in (None, "")
    }
    return next(iter(values)) if len(values) == 1 else None


def _normalize_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_aware_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def _utc_wall_clock() -> datetime:
    return datetime.now(UTC)


def _incident_not_before_ms(*, batch: Any, leg: Any, raw: Any) -> int:
    # Dedicated batch-119 authority: the exact allowlisted raw message is the
    # immutable incident anchor; mutable planner/leg timestamps may only move
    # the lower bound later, never before that source message.
    candidates = [
        _normalize_utc_datetime(getattr(raw, "posted_at", None)),
        _normalize_utc_datetime(getattr(batch, "planned_at", None)),
        _normalize_utc_datetime(getattr(batch, "started_at", None)),
        _normalize_utc_datetime(getattr(leg, "created_at", None)),
    ]
    if any(value is None for value in candidates):
        raise CompositeBatchRecoveryRefusal(
            "natural_stop_time_authority_invalid"
        )
    raw_posted_at = candidates[0]
    wall_clock = _normalize_aware_utc_datetime(_utc_wall_clock())
    if (
        wall_clock is None
        or any(value < raw_posted_at for value in candidates[1:])
        or any(
            value > wall_clock + _MAX_FUTURE_CLOCK_SKEW
            for value in candidates
        )
    ):
        raise CompositeBatchRecoveryRefusal(
            "natural_stop_time_authority_invalid"
        )
    return max(int(value.timestamp() * 1000) for value in candidates)


def _incident_time_authority_payload(
    *, batch: Any, leg: Any, raw: Any
) -> dict[str, Any]:
    not_before_ms = _incident_not_before_ms(batch=batch, leg=leg, raw=raw)
    return {
        "schema_version": 1,
        "basis": "batch119_raw_posted_plus_durable_management_times",
        "basis_count": 4,
        "not_before_ref": _redacted_ref(
            "natural_stop_not_before_ms", not_before_ms
        ),
    }


def _bounded_epoch_ms_values(
    row: Mapping[str, Any], *keys: str
) -> list[int] | None:
    values: list[int] = []
    for key in keys:
        if row.get(key) in (None, ""):
            continue
        raw = row[key]
        if isinstance(raw, bool):
            return None
        text_value = str(raw).strip()
        if not text_value.isdigit():
            return None
        if len(text_value) > 20:
            return None
        try:
            parsed = int(text_value)
        except ValueError:
            return None
        if parsed <= 0 or str(parsed) != text_value:
            return None
        values.append(parsed)
    return values


def _natural_stop_has_residual_close_evidence(
    snapshot: Any, *, pos_id: str
) -> bool:
    for field in ("open_orders", "order_history", "trade_fills"):
        for row in getattr(snapshot, field):
            if not isinstance(row, Mapping) or not _row_matches_position(
                row, pos_id=pos_id
            ):
                continue
            reduce_only = str(
                row.get("reduceOnly") or row.get("reduce_only") or ""
            ).lower() in {"true", "1", "yes"}
            if (
                _row_matches_close_position(row, pos_id=pos_id)
                or reduce_only
                or str(row.get("side") or "").lower() == "sell"
            ):
                return True
    return False


def _has_exchange_close_submission(snapshot: Any, *, pos_id: str) -> bool:
    for row in snapshot.position_history:
        if not isinstance(row, Mapping):
            return True
        if not _row_matches_position(row, pos_id=pos_id):
            continue
        if _row_matches_close_position(row, pos_id=pos_id):
            return True
        state = str(row.get("state") or row.get("status") or "").lower()
        close_size = _decimal_or_none(
            row.get("closeSz")
            or row.get("closedSize")
            or row.get("close_size")
        )
        if state in {"closed", "filled", "completed", "exited"} or (
            close_size is not None and close_size > 0
        ):
            return True

    for field in ("open_orders", "order_history", "trade_fills"):
        for row in getattr(snapshot, field):
            if not isinstance(row, Mapping):
                return True
            if not _row_matches_position(row, pos_id=pos_id):
                continue
            if _row_matches_close_position(row, pos_id=pos_id):
                return True
            reduce_only = str(
                row.get("reduceOnly") or row.get("reduce_only") or ""
            ).lower() in {"true", "1", "yes"}
            side = str(row.get("side") or "").lower()
            if reduce_only or side == "sell":
                return True
    for row in snapshot.trigger_history:
        if not isinstance(row, Mapping):
            return True
        if not _row_matches_position(row, pos_id=pos_id):
            continue
        if _row_matches_close_position(row, pos_id=pos_id):
            return True
        state = str(row.get("state") or row.get("status") or "").lower()
        reduce_only = str(
            row.get("reduceOnly") or row.get("reduce_only") or ""
        ).lower() in {"true", "1", "yes"}
        side = str(row.get("side") or "").lower()
        if state in {"filled", "triggered", "completed"} and (
            reduce_only or side == "sell"
        ):
            return True
    return False


def _protection_ownership_refusal(
    pending_rows,
    *,
    batch,
    binding,
    entry,
    ledger,
    pos_id: str,
    position: CompositeRecoveryPosition,
    profile,
) -> str | None:
    ledger_by_id: dict[str, Any] = {}
    purpose_counts = {"stop_loss": 0, "backup_stop": 0, "take_profit": 0}
    for row in ledger:
        order_id = str(row.order_id or "")
        purpose = str(row.purpose or "")
        if (
            not order_id
            or order_id in ledger_by_id
            or str(row.venue or "").lower() != "deepcoin"
            or int(row.execution_binding_id) != int(binding.id)
            or int(row.execution_order_leg_id) != int(entry.id)
            or str(row.strategy_instance_id or "")
            != str(batch.strategy_instance_id)
            or str(row.pos_id or "") != str(pos_id)
            or str(row.instrument_id or "").upper()
            != profile.instrument_id.upper()
            or str(row.side or "").lower() != profile.side.lower()
            or purpose not in {"stop_loss", "backup_stop", "take_profit"}
            or str(row.status or "").lower() != "verified"
        ):
            return "unexpected_protection_ownership"
        try:
            _optional_json_fingerprint(row.evidence_json)
        except CompositeBatchRecoveryRefusal:
            return "durable_evidence_invalid"
        ledger_by_id[order_id] = row
        purpose_counts[purpose] += 1
    if position.current_size is not None and (
        purpose_counts["stop_loss"] != 1
        or purpose_counts["backup_stop"] != 1
    ):
        return "unexpected_protection_ownership"

    pending_by_id: dict[str, Mapping[str, object]] = {}
    for row in pending_rows:
        if not isinstance(row, Mapping):
            return "unexpected_protection_ownership"
        if str(row.get("posId") or row.get("pos_id") or "") != str(pos_id):
            continue
        order_id = str(
            row.get("ordId") or row.get("orderId") or row.get("order_id") or ""
        )
        if not order_id or order_id in pending_by_id:
            return "unexpected_protection_ownership"
        if (
            str(row.get("instId") or row.get("instrument_id") or "").upper()
            != profile.instrument_id.upper()
            or str(row.get("posSide") or row.get("side") or "").lower()
            != profile.side.lower()
            or str(row.get("triggerOrderType") or "").upper() != "TPSL"
            or str(row.get("state") or row.get("status") or "").lower()
            != "live"
        ):
            return "unexpected_protection_ownership"
        pending_by_id[order_id] = row
    if position.current_size is None and not pending_by_id:
        return None
    if set(pending_by_id) != set(ledger_by_id):
        return "unexpected_protection_ownership"
    for order_id, ledger_row in ledger_by_id.items():
        pending_row = pending_by_id[order_id]
        trigger_price = _pending_protection_trigger_price(
            pending_row, purpose=str(ledger_row.purpose)
        )
        if not _same_optional_decimal(trigger_price, ledger_row.trigger_price):
            return "unexpected_protection_ownership"
        pending_size = pending_row.get("sz")
        if pending_size in (None, ""):
            pending_size = pending_row.get("size")
        if not _same_optional_decimal(pending_size, ledger_row.size_text):
            return "unexpected_protection_ownership"
    return None


def _source_evidence_payload(
    *, batch, raw, lifecycle, binding, entry, leg, components, target, contract,
    protection_ledger, instruction_population
):
    return {
        "schema_version": 1,
        "batch_id": int(batch.id),
        "raw_message_id": int(batch.raw_message_id),
        "raw_chat_ref": _redacted_ref("raw_chat", raw.chat_id),
        "lifecycle_id": int(lifecycle.id),
        "lifecycle_chat_ref": _redacted_ref("lifecycle_chat", lifecycle.chat_id),
        "lifecycle_message_ref": _redacted_ref(
            "lifecycle_message", lifecycle.message_id
        ),
        "lifecycle_symbol": str(lifecycle.symbol),
        "lifecycle_side": str(lifecycle.side),
        "binding_ref": _redacted_ref("binding", binding.id),
        "strategy_ref": _redacted_ref("strategy", batch.strategy_instance_id),
        "entry_leg_ref": _redacted_ref("entry_leg", entry.id),
        "position_ref": _redacted_ref("position", leg.pos_id),
        "batch_status": str(batch.status),
        "batch_reason_code": str(batch.reason_code),
        "batch_intent": str(batch.intent),
        "batch_effective_action": str(batch.effective_action),
        "batch_execution_mode": str(batch.execution_mode),
        "lifecycle_status": str(lifecycle.lifecycle_status),
        "lifecycle_binding_ref": _redacted_ref(
            "lifecycle_binding", lifecycle.execution_binding_id
        ),
        "binding_status": str(binding.status),
        "binding_strategy_ref": _redacted_ref(
            "binding_strategy", binding.strategy_instance_id
        ),
        "binding_chat_ref": _redacted_ref("binding_chat", binding.chat_id),
        "binding_message_ref": _redacted_ref(
            "binding_message", binding.message_id
        ),
        "binding_venue": str(binding.venue),
        "binding_symbol": str(binding.symbol),
        "binding_side": str(binding.side),
        "binding_margin_mode": str(binding.margin_mode),
        "binding_position_mode": str(binding.position_mode),
        "binding_position_ref": _redacted_ref("binding_position", binding.pos_id),
        "entry_status": str(entry.status),
        "entry_strategy_ref": _redacted_ref(
            "entry_strategy", entry.strategy_instance_id
        ),
        "entry_venue": str(entry.venue),
        "entry_purpose": str(entry.purpose),
        "entry_leg_index": int(entry.leg_index),
        "entry_attribution_status": str(entry.attribution_status),
        "entry_binding_ref": _redacted_ref(
            "entry_binding", entry.execution_binding_id
        ),
        "entry_position_ref": _redacted_ref("entry_position", entry.pos_id),
        "leg_status": str(leg.status),
        "management_leg_ref": _redacted_ref("management_leg", leg.id),
        "management_leg_batch_id": int(leg.management_batch_id),
        "management_leg_entry_ref": _redacted_ref(
            "management_leg_entry", leg.execution_order_leg_id
        ),
        "management_leg_index": int(leg.leg_index),
        "management_leg_position_ref": _redacted_ref(
            "management_leg_position", leg.pos_id
        ),
        "leg_preflight_size": str(leg.preflight_size),
        "leg_planned_close_size": str(leg.planned_close_size),
        "leg_avg_entry_price": str(leg.avg_entry_price),
        "leg_quantity_step": str(leg.quantity_step),
        "leg_submission_fields_present": {
            "request": leg.request_json not in (None, ""),
            "response": leg.response_json not in (None, ""),
            "client_order_id": leg.client_order_id not in (None, ""),
            "exchange_order_id": leg.exchange_order_id not in (None, ""),
        },
        "leg_last_exchange_snapshot_fingerprint": _optional_json_fingerprint(
            leg.last_exchange_snapshot_json
        ),
        "leg_last_error_fingerprint": _optional_json_fingerprint(
            leg.last_error
        ),
        "natural_stop_time_authority": _incident_time_authority_payload(
            batch=batch,
            leg=leg,
            raw=raw,
        ),
        "components": [
            {
                "component_ref": _redacted_ref("component", row.id),
                "leg_ref": _redacted_ref(
                    "component_leg", row.strategy_management_leg_id
                ),
                "sequence": int(row.sequence),
                "kind": str(row.component_kind),
                "status": str(row.status),
                "idempotency_ref": _redacted_ref(
                    "component_idempotency", row.idempotency_key
                ),
                "reason_code": row.reason_code,
                "attempt_count": int(row.attempt_count),
                "desired_fingerprint": _optional_json_fingerprint(
                    row.desired_json
                ),
                "evidence_fingerprint": _optional_json_fingerprint(
                    row.evidence_json
                ),
            }
            for row in components
        ],
        "contract_fingerprint": str(batch.management_contract_fingerprint),
        "contract_version": int(contract.version),
        "target_fingerprint": str(batch.target_fingerprint),
        "target_snapshot_fingerprint": _fingerprint(
            json.loads(batch.target_snapshot_json)
        ),
        "trusted_start_size": str(target["trusted_start_size"]),
        "target_remaining_size": str(target["target_remaining_size"]),
        "quantity_step": str(target["quantity_step"]),
        "min_quantity": str(target["min_quantity"]),
        "owned_protection_count": len(protection_ledger),
        "owned_protection": [
            {
                "ledger_ref": _redacted_ref("protection_ledger", row.id),
                "binding_ref": _redacted_ref(
                    "protection_binding", row.execution_binding_id
                ),
                "entry_leg_ref": _redacted_ref(
                    "protection_entry_leg", row.execution_order_leg_id
                ),
                "venue": str(row.venue),
                "strategy_ref": _redacted_ref(
                    "protection_strategy", row.strategy_instance_id
                ),
                "position_ref": _redacted_ref("protection_position", row.pos_id),
                "order_ref": _redacted_ref("protection_order", row.order_id),
                "instrument_id": str(row.instrument_id),
                "side": str(row.side),
                "purpose": str(row.purpose),
                "size": str(row.size_text),
                "trigger_price": str(row.trigger_price),
                "status": str(row.status),
                "evidence_source": str(row.evidence_source),
                "evidence_fingerprint": _optional_json_fingerprint(
                    row.evidence_json
                ),
            }
            for row in protection_ledger
        ],
        "instruction_population": instruction_population,
        "submission_fields_present": 0,
        "durable_close_evidence_count": 0,
    }


def _exchange_evidence_payload(
    snapshot,
    *,
    position,
    pos_id: str,
    ledger,
    profile,
    natural_stop_proof: Mapping[str, Any] | None = None,
):
    owned_order_refs = sorted(
        _redacted_ref("protection_order", row.order_id) for row in ledger
    )
    exact_pending_refs = sorted(
        _redacted_ref(
            "pending_protection",
            row.get("ordId") or row.get("orderId") or row.get("order_id"),
        )
        for row in snapshot.pending_trigger_orders
        if isinstance(row, Mapping)
        and str(row.get("posId") or row.get("pos_id") or "") == pos_id
    )
    collection_digests = {
        field: {
            "count": len(getattr(snapshot, field)),
            "digest": _fingerprint(
                sorted(
                    _canonical_snapshot_row(row)
                    for row in getattr(snapshot, field)
                )
            ),
        }
        for field in (
            "positions",
            "position_history",
            "open_orders",
            "pending_trigger_orders",
            "order_history",
            "trade_fills",
            "trigger_history",
            "pending_tpsl_observations",
        )
    }
    payload = {
        "schema_version": 1,
        "instrument_id": profile.instrument_id,
        "side": profile.side,
        "scope_fingerprint": str(snapshot.scope_fingerprint),
        "capture_window": {
            "status": "valid",
            "time_relation": "started_not_after_ended_not_after_observed",
        },
        "account_authority": _account_authority_evidence_payload(
            snapshot.account_authority
        ),
        "position": _serialize_position(position),
        "collections": collection_digests,
        "owned_protection_refs": owned_order_refs,
        "pending_protection_refs": exact_pending_refs,
        "regular_close_evidence_count": 0,
        "snapshot_complete": True,
    }
    if natural_stop_proof is not None:
        payload["natural_stop"] = dict(natural_stop_proof)
    return payload


def _account_authority_evidence_payload(authority: Any) -> dict[str, Any]:
    return {
        "uid_scope_hash": str(authority.uid_scope_hash),
        "start_write_generation": int(authority.start_write_generation),
        "end_write_generation": int(authority.end_write_generation),
        "complete": bool(authority.complete),
        "reason_code": authority.reason_code,
        "collections": [
            {
                "endpoint": str(collection.endpoint),
                "available": bool(collection.available),
                "schema_valid": bool(collection.schema_valid),
                "complete": bool(collection.complete),
                "row_count": int(collection.row_count),
                "page_count": int(collection.page_count),
                "reason_code": collection.reason_code,
            }
            for collection in authority.collections
        ],
    }


def _refusal(batch_id: int, reason_code: str) -> CompositeBatchRecoveryPlan:
    evidence = {
        "schema_version": 1,
        "batch_id": int(batch_id),
        "decision": "refused",
        "reason_code": str(reason_code),
    }
    empty_source = _fingerprint(
        {"batch_id": int(batch_id), "source_state": "unproven"}
    )
    empty_exchange = _fingerprint(
        {"batch_id": int(batch_id), "exchange_state": "unproven"}
    )
    return CompositeBatchRecoveryPlan(
        batch_id=int(batch_id),
        status="refused",
        reason_code=str(reason_code),
        position=None,
        source_fingerprint=empty_source,
        exchange_snapshot_fingerprint=empty_exchange,
        evidence_fingerprint=_fingerprint(evidence),
        evidence=_freeze_mapping(evidence),
    )


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        _plain_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redacted_ref(kind: str, value: object) -> str:
    return hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()


def _serialize_position(position: CompositeRecoveryPosition) -> dict[str, Any]:
    return {
        "disposition": position.disposition,
        "current_size": position.current_size,
        "close_delta": position.close_delta,
        "effective_remaining_size": position.effective_remaining_size,
    }


def _proposed_transition(position: CompositeRecoveryPosition) -> dict[str, Any]:
    if position.disposition == "position_absent":
        return {
            "batch_status": "resolved",
            "batch_reason_code": "composite_recovery_exact_position_absent",
            "leg_status": "failed",
            "component_statuses": [
                "safely_skipped",
                "safely_skipped",
                "safely_skipped",
            ],
            "exchange_call_possible": False,
        }
    result: dict[str, Any] = {
        "batch_status": "ready",
        "leg_status": "planned",
        "component_statuses": ["recovery_required", "pending", "pending"],
        "exchange_call_possible": False,
    }
    if position.disposition == "protection_only_below_target":
        result.update(
            {
                "attestation_kind": "approved_under_target_recovery",
                "actual_remaining_size": position.effective_remaining_size,
                "original_target_remaining_size": (
                    BATCH_119_RECOVERY.target_remaining_size
                ),
                "append_component_attestation": True,
            }
        )
    return result


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            str(key): (
                _freeze_mapping(item)
                if isinstance(item, Mapping)
                else tuple(
                    _freeze_mapping(part) if isinstance(part, Mapping) else part
                    for part in item
                )
                if isinstance(item, (list, tuple))
                else item
            )
            for key, item in value.items()
        }
    )


def _plain_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    return value


def _canonical_snapshot_row(row: object) -> str:
    """Hash one raw row before it can enter retained evidence."""

    if not isinstance(row, Mapping):
        raise CompositeBatchRecoveryRefusal("exchange_snapshot_row_invalid")
    return _fingerprint(dict(row))


def _optional_json_fingerprint(value: str | None) -> str:
    if value in (None, ""):
        return _fingerprint(None)
    try:
        payload = json.loads(str(value))
        return _fingerprint(payload)
    except (TypeError, ValueError, RecursionError) as exc:
        raise CompositeBatchRecoveryRefusal("durable_json_invalid") from exc


def _exact_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and len(value) <= 20 and value.isdigit():
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if str(parsed) == value else None
    return None


def _refusal_batch_id(profile: object) -> int:
    value = getattr(profile, "batch_id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _is_bounded_snapshot_incomplete_evidence(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 1:
        return False
    fact = value[0]
    if not isinstance(fact, Mapping) or set(fact) != {"error_type"}:
        return False
    error_type = fact.get("error_type")
    return (
        isinstance(error_type, str)
        and 0 < len(error_type) <= 64
        and error_type.replace("_", "").replace(".", "").isalnum()
    )


def _row_matches_position(row: Mapping[str, object], *, pos_id: str) -> bool:
    return any(
        str(row.get(key) or "") == str(pos_id)
        for key in ("posId", "pos_id", "closePosId", "close_pos_id")
    )


def _row_matches_close_position(
    row: Mapping[str, object], *, pos_id: str
) -> bool:
    return any(
        str(row.get(key) or "") == str(pos_id)
        for key in ("closePosId", "close_pos_id")
    )


def _pending_protection_trigger_price(
    row: Mapping[str, object], *, purpose: str
) -> object:
    keys = (
        ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
        if purpose in {"stop_loss", "backup_stop"}
        else ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
    )
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _same_optional_decimal(left: object, right: object) -> bool:
    if left in (None, "") and right in (None, ""):
        return True
    if left in (None, "") or right in (None, ""):
        return False
    left_decimal = _decimal_or_none(left)
    right_decimal = _decimal_or_none(right)
    return left_decimal is not None and left_decimal == right_decimal


def _decimal_or_none(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _consistent_nonnegative_position_size(
    row: Mapping[str, object],
) -> Decimal | None:
    values: list[Decimal] = []
    for key in ("pos", "size", "sz", "positionSize", "position_size"):
        if row.get(key) in (None, ""):
            continue
        value = _decimal_or_none(row[key])
        if value is None or value < 0:
            return None
        values.append(value)
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def classify_recovery_position(
    *,
    profile: CompositeBatchRecoveryProfile,
    positions: Sequence[Mapping[str, object]],
    expected_pos_id: str,
    instrument_id: str,
    side: str,
    quantity_step: str,
    min_quantity: str,
) -> CompositeRecoveryPosition:
    """Classify one exact exchange position against an immutable target."""

    trusted = _positive_decimal(profile.trusted_start_size, "trusted_start_size")
    target = _positive_decimal(profile.target_remaining_size, "target_remaining_size")
    step = _positive_decimal(quantity_step, "quantity_step")
    minimum = _positive_decimal(min_quantity, "min_quantity")
    if target > trusted:
        raise CompositeBatchRecoveryRefusal("recovery_target_above_trusted_start")
    for value, reason in (
        (trusted, "trusted_start_not_step_aligned"),
        (target, "target_remaining_not_step_aligned"),
    ):
        if not _is_step_aligned(value, step):
            raise CompositeBatchRecoveryRefusal(reason)

    matches: list[Mapping[str, object]] = []
    for row in positions:
        if not isinstance(row, Mapping):
            raise CompositeBatchRecoveryRefusal(
                "exact_position_snapshot_invalid"
            )
        instruments = {
            str(row[key]).strip().upper()
            for key in ("instId", "instrument_id", "instrumentId")
            if row.get(key) not in (None, "")
        }
        sides = _exact_row_position_sides(row)
        size = _consistent_nonnegative_position_size(row)
        if (
            instruments != {str(instrument_id).upper()}
            or len(sides) != 1
            or size is None
        ):
            raise CompositeBatchRecoveryRefusal(
                "exact_position_snapshot_invalid"
            )
        identities = {
            str(row[key]).strip()
            for key in ("posId", "pos_id")
            if row.get(key) not in (None, "")
        }
        if sides != {str(side).lower()}:
            if (
                size > 0
                and (
                    len(identities) != 1
                    or str(expected_pos_id) in identities
                )
            ):
                raise CompositeBatchRecoveryRefusal(
                    "exact_position_snapshot_invalid"
                )
            continue
        if size == 0:
            continue
        if identities != {str(expected_pos_id)}:
            raise CompositeBatchRecoveryRefusal(
                "exact_position_identity_conflict"
            )
        matches.append(row)
    if len(matches) > 1:
        raise CompositeBatchRecoveryRefusal("exact_position_ambiguous")
    if not matches:
        return CompositeRecoveryPosition(
            disposition="position_absent",
            current_size=None,
            close_delta="0",
            effective_remaining_size="0",
        )

    row = matches[0]
    actual_instrument = str(
        row.get("instId") or row.get("instrument_id") or row.get("symbol") or ""
    ).upper()
    if actual_instrument != str(instrument_id).upper():
        raise CompositeBatchRecoveryRefusal("exact_position_instrument_mismatch")
    actual_side = str(row.get("posSide") or row.get("side") or "").lower()
    if actual_side != str(side).lower():
        raise CompositeBatchRecoveryRefusal("exact_position_side_mismatch")
    current = _positive_decimal(
        _first_present(row, "pos", "size", "sz", "positionSize", "position_size"),
        "current_size",
    )
    if current > trusted:
        raise CompositeBatchRecoveryRefusal("position_size_increased_after_snapshot")
    if current < minimum:
        raise CompositeBatchRecoveryRefusal("current_position_below_minimum")
    if not _is_step_aligned(current, step):
        raise CompositeBatchRecoveryRefusal("current_position_not_step_aligned")

    if current > target:
        delta = current - target
        if delta < minimum or not _is_step_aligned(delta, step):
            raise CompositeBatchRecoveryRefusal("target_remaining_delta_not_executable")
        return CompositeRecoveryPosition(
            disposition="resume_to_target",
            current_size=_decimal_text(current),
            close_delta=_decimal_text(delta),
            effective_remaining_size=_decimal_text(target),
        )
    if current == target:
        return CompositeRecoveryPosition(
            disposition="protection_only_at_target",
            current_size=_decimal_text(current),
            close_delta="0",
            effective_remaining_size=_decimal_text(target),
        )
    return CompositeRecoveryPosition(
        disposition="protection_only_below_target",
        current_size=_decimal_text(current),
        close_delta="0",
        effective_remaining_size=_decimal_text(current),
    )


def _positive_decimal(value: object, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CompositeBatchRecoveryRefusal(f"{field_name}_invalid") from exc
    if not result.is_finite() or result <= 0:
        raise CompositeBatchRecoveryRefusal(f"{field_name}_invalid")
    return result


def _is_step_aligned(value: Decimal, step: Decimal) -> bool:
    return (value / step) == (value / step).to_integral_value()


def _first_present(row: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    raise CompositeBatchRecoveryRefusal("current_size_missing")


def _decimal_text(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return "0" if normalized in {"", "-0"} else normalized
