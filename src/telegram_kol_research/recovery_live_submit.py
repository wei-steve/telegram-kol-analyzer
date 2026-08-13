"""Live Deepcoin recovery order submission after explicit confirmation."""

from __future__ import annotations

import math
import time
import hashlib
import json
import re
import base64
import binascii
import zlib
from copy import deepcopy
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime, timedelta
from functools import wraps
from threading import RLock
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import DeepcoinRequestScope
from telegram_kol_research.deepcoin_client import DeepcoinPreSendUnavailable
from telegram_kol_research.deepcoin_client import DeepcoinDefiniteRejection
from telegram_kol_research.deepcoin_client import DeepcoinRequestOutcomeUnknown
from telegram_kol_research.deepcoin_client import DeepcoinTradingClientProtocol
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.deepcoin_order_builder import _coalesce_equivalent_entry_legs
from telegram_kol_research.entry_strategy_assembly import (
    build_bounded_entry_order_draft_snapshot,
    canonical_entry_assembly_fingerprint,
)
from telegram_kol_research.deepcoin_execution_actions import execute_deepcoin_management_signal
from telegram_kol_research.deepcoin_execution_operations import (
    DeepcoinOperationConflict,
    ExecutionOperationRecord,
    advance_account_write_generation,
    contains_credential_marker,
    defer_execution_operation_after_not_sent,
    load_operation_bundle,
    load_account_write_generation,
    record_snapshot_evidence,
    record_request_attempt,
    reserve_execution_operation,
    transition_execution_operation,
)
from telegram_kol_research.deepcoin_snapshot_authority import (
    DeepcoinSnapshotUnavailable,
    ExchangeCollectionEvidence,
    build_exchange_collection_evidence,
    capture_account_snapshot,
)
from telegram_kol_research.deepcoin_request_policy import OutcomeCertainty
from telegram_kol_research.deepcoin_request_policy import RequestPriority
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import ExecutionOrderLegRecord
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.execution_bindings import upsert_execution_order_leg
from telegram_kol_research.execution_events import ExecutionEventRecord
from telegram_kol_research.execution_events import record_execution_event
from telegram_kol_research.models import EntryStrategyAssembly
from telegram_kol_research.models import DeepcoinExecutionOperation
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import ExecutionEvent
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.models import PositionMutationIntent
from telegram_kol_research.models import PositionProtectionLedger
from telegram_kol_research.models import InstructionExecutionContract
from telegram_kol_research.models import StrategyRevisionBatch
from telegram_kol_research.models import StrategyRevisionLeg
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.models import TriggerProtectionIntent
from telegram_kol_research.models import TradeSignal
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.position_protection_legs import (
    bind_parent_entry_order,
    create_or_get_protection_leg,
)
from telegram_kol_research.position_mutation_gateway import (
    exact_position_write_gate,
    prepare_exact_position_sltp_intent,
    submit_exact_position_sltp,
)
from telegram_kol_research.position_mutation_intents import (
    PositionMutationIntentError,
    load_validated_set_position_request,
)
from telegram_kol_research.protection_revisions import activate_protection_revision
from telegram_kol_research.trigger_protection_intents import (
    create_or_get_trigger_protection_intent,
    record_trigger_protection_parent,
)
from telegram_kol_research.trigger_take_profit_convergence import (
    create_or_get_trigger_take_profit_convergence,
)
from telegram_kol_research.recovery_live_submit_gate import validate_recovery_live_submit_gate
from telegram_kol_research.recovery_order_confirmation import (
    attach_contract_spec_evidence,
    evaluate_deepcoin_entry_capability,
)
from telegram_kol_research.trade_signals import TradeSignalRecord
from telegram_kol_research.trade_signals import TradeSignalClaimError
from telegram_kol_research.trade_signals import MANUAL_MANAGEMENT_SOURCE_TYPES
from telegram_kol_research.trade_signals import MANAGEMENT_TRADE_SIGNAL_ACTIONS
from telegram_kol_research.trade_signals import canonical_management_batch_id
from telegram_kol_research.trade_signals import claim_pending_trade_signal
from telegram_kol_research.trade_signals import (
    finalize_trade_signal_from_execution_operation,
)
from telegram_kol_research.trade_signals import (
    freeze_trade_signal_for_protected_entry_recovery,
)
from telegram_kol_research.trade_signals import list_pending_trade_signals
from telegram_kol_research.trade_signals import load_or_create_trade_signal
from telegram_kol_research.trade_signals import load_trade_signal
from telegram_kol_research.trade_signals import mark_trade_signal_failed
from telegram_kol_research.trade_signals import mark_trade_signal_submitted
from telegram_kol_research.trading_settings import (
    PROTECTED_ENTRY_CONTRACT_VERSION,
    load_trading_settings,
    protected_entry_mode_for_signal,
    protected_entry_operation_access,
)
from telegram_kol_research.source_message_deletion import (
    serialized_source_message_execution,
    source_identity_execution_barrier,
    source_message_execution_authority,
)
from telegram_kol_research.take_profit_plan import TakeProfitPlanError
from telegram_kol_research.take_profit_plan import build_take_profit_plan


class RecoveryLiveSubmitError(RuntimeError):
    """Raised when a live recovery order cannot be submitted safely."""


_SAFE_PERSISTED_FAILURE = re.compile(
    r"^[a-z0-9][a-z0-9_:,-]{0,255}$"
)
_SAFE_PROTECTED_EXCHANGE_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$"
)


@dataclass(slots=True)
class _ProtectedEntryRuntime:
    operation: ExecutionOperationRecord
    deadline_monotonic: float
    monotonic_factory: Any
    sleep_fn: Any
    uid_scope_hash: str
    pre_submit_position_refs: frozenset[str]
    latest_protection_capture: "_PendingTpslCapture | None" = None


@dataclass(frozen=True, slots=True)
class _PendingTpslCapture:
    collection: ExchangeCollectionEvidence
    rows: tuple[Mapping[str, Any], ...]
    start_write_generation: int
    end_write_generation: int
    capture_started_at: datetime
    capture_ended_at: datetime
    normalized_baseline_json: str | None = None


@dataclass(slots=True)
class EntrySubmissionProgress:
    """Track entry exchange writes separately from pre-submit validation."""

    attempted_writes: int = 0
    confirmed_legs: int = 0

    def record_attempt(self) -> None:
        self.attempted_writes += 1

    def record_confirmed_leg(self) -> None:
        self.confirmed_legs += 1


class EntrySubmissionProgressError(RuntimeError):
    """Carry exact V2 entry-write progress across the submission boundary."""

    def __init__(
        self,
        cause: Exception,
        *,
        progress: EntrySubmissionProgress,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.progress = progress


def _report_entry_submission_progress(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        progress = kwargs.get("submission_progress")
        if not isinstance(progress, EntrySubmissionProgress):
            progress = EntrySubmissionProgress()
            kwargs["submission_progress"] = progress
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            raise EntrySubmissionProgressError(
                exc,
                progress=progress,
            ) from exc

    return wrapped


def _entry_submission_failure_status(
    exc: Exception,
    *,
    progress: EntrySubmissionProgress,
) -> str:
    if progress.attempted_writes == 0:
        return "failed"
    if progress.confirmed_legs > 0:
        return "partial_submission_failed"
    if isinstance(exc, DeepcoinDefiniteRejection):
        return "failed"
    return "unknown_exchange_outcome"


def _safe_protected_failure_code(error: BaseException) -> str:
    fact = getattr(error, "fact", None)
    safe_code = getattr(fact, "safe_code", None)
    if (
        isinstance(safe_code, str)
        and _SAFE_PERSISTED_FAILURE.fullmatch(safe_code)
        and not contains_credential_marker(safe_code)
    ):
        return safe_code
    message = str(error)
    if (
        isinstance(error, RecoveryLiveSubmitError)
        and _SAFE_PERSISTED_FAILURE.fullmatch(message)
        and not contains_credential_marker(message)
    ):
        return message
    return "protected_entry_execution_failed"


def _safe_protected_market_response(
    response: object,
    *,
    order_id: str,
    pos_id: str,
) -> dict[str, Any]:
    if (
        not _safe_protected_exchange_identity(order_id)
        or not _safe_protected_exchange_identity(pos_id)
    ):
        raise RecoveryLiveSubmitError(
            "protected_entry_exchange_identity_invalid"
        )
    projected: dict[str, Any] = {
        "data": {"ordId": order_id, "posId": pos_id}
    }
    if isinstance(response, Mapping):
        code = response.get("code")
        if (
            code not in (None, "")
            and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", str(code))
        ):
            projected["code"] = str(code)
    return projected


def _safe_protected_exchange_identity(value: str) -> bool:
    return bool(
        _SAFE_PROTECTED_EXCHANGE_ID.fullmatch(value)
        and not contains_credential_marker(value)
    )


def _require_synchronized_finalized_entry_assembly(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
) -> bool:
    """Fail closed when a V2 entry signal is stale at the execution boundary."""

    payload = trade_signal.payload
    top_evidence = (
        payload.get("entry_preamble_assembly")
        if isinstance(payload, Mapping)
        else None
    )
    draft = (
        payload.get("deepcoin_order_draft")
        if isinstance(payload, Mapping)
        else None
    )
    nested_evidence = (
        draft.get("entry_preamble_assembly")
        if isinstance(draft, Mapping)
        else None
    )
    declares_v2 = any(
        isinstance(evidence, Mapping)
        and (
            "assembly_id" in evidence
            or "strategy_instance_id" in evidence
        )
        for evidence in (top_evidence, nested_evidence)
    )
    strategy_instance_id = trade_signal.strategy_instance_id
    assembly = None
    if strategy_instance_id:
        with session_factory() as session:
            assembly = (
                session.query(EntryStrategyAssembly)
                .filter(
                    EntryStrategyAssembly.strategy_instance_id
                    == strategy_instance_id,
                    EntryStrategyAssembly.entry_preamble_id.is_(None),
                )
                .one_or_none()
            )
            if assembly is not None:
                assembly_id = int(assembly.id)
                assembly_fingerprint = str(assembly.fingerprint)
                try:
                    assembly_evidence = json.loads(
                        assembly.evidence_json or "{}"
                    )
                except (TypeError, ValueError):
                    assembly_evidence = None
    if assembly is None and not declares_v2:
        return False
    if (
        assembly is None
        or not strategy_instance_id
        or not isinstance(top_evidence, Mapping)
        or not isinstance(nested_evidence, Mapping)
        or not isinstance(draft, Mapping)
    ):
        raise RecoveryLiveSubmitError("entry_assembly_signal_not_synchronized")
    snapshot = (
        assembly_evidence.get("order_draft_snapshot")
        if isinstance(assembly_evidence, Mapping)
        else None
    )
    final_leg_count = (
        assembly_evidence.get("final_entry_leg_count")
        if isinstance(assembly_evidence, Mapping)
        else None
    )
    snapshot_legs = (
        snapshot.get("order_legs") if isinstance(snapshot, Mapping) else None
    )
    try:
        current_snapshot = build_bounded_entry_order_draft_snapshot(draft)
        canonical_fingerprint = canonical_entry_assembly_fingerprint(
            assembly_evidence
        )
    except (TypeError, ValueError):
        current_snapshot = None
        canonical_fingerprint = None
    current_legs = (
        current_snapshot.get("order_legs")
        if isinstance(current_snapshot, Mapping)
        else None
    )
    side_is_consistent = isinstance(current_legs, list) and all(
        isinstance(leg, Mapping)
        and leg.get("position_side") == trade_signal.side
        and leg.get("side")
        == ("buy" if trade_signal.side == "long" else "sell")
        for leg in current_legs
    )
    evidence_copies = (top_evidence, nested_evidence)
    if (
        not isinstance(snapshot, Mapping)
        or type(final_leg_count) is not int
        or final_leg_count <= 0
        or not isinstance(snapshot_legs, list)
        or len(snapshot_legs) != final_leg_count
        or current_snapshot != snapshot
        or canonical_fingerprint != assembly_fingerprint
        or not side_is_consistent
        or any(not isinstance(evidence, Mapping) for evidence in evidence_copies)
        or any(
            evidence.get("assembly_id") != assembly_id
            or evidence.get("strategy_instance_id") != strategy_instance_id
            or evidence.get("assembly_fingerprint") != assembly_fingerprint
            for evidence in evidence_copies
        )
    ):
        raise RecoveryLiveSubmitError("entry_assembly_signal_not_synchronized")
    return True


@contextmanager
def _entry_source_exchange_write_gate(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    source: dict[str, Any],
):
    """Serialize the final source check with one entry exchange write."""

    chat_id = int(source.get("chat_id") or trade_signal.chat_id)
    message_id = int(source.get("message_id") or trade_signal.message_id)
    with source_message_execution_authority(session_factory):
        barrier = source_identity_execution_barrier(
            session_factory,
            chat_id=chat_id,
            message_id=message_id,
        )
        if barrier.status != "allow":
            raise RecoveryLiveSubmitError(str(barrier.reason or "source_execution_blocked"))
        yield


_TRIGGER_PROTECTION_LOCKS: dict[tuple[str, str, str, str], RLock] = {}
_TRIGGER_PROTECTION_LOCKS_GUARD = RLock()


@contextmanager
def _trigger_protection_submission_lock(
    *,
    deepcoin_client: DeepcoinTradingClientProtocol,
    venue: str,
    inst_id: str,
    side: str,
):
    """Serialize one account/instrument/side trigger-protection handoff."""

    key = _trigger_protection_lock_key(
        deepcoin_client=deepcoin_client,
        venue=venue,
        inst_id=inst_id,
        side=side,
    )
    with _TRIGGER_PROTECTION_LOCKS_GUARD:
        lock = _TRIGGER_PROTECTION_LOCKS.setdefault(key, RLock())
    with lock:
        yield


def _trigger_protection_lock_key(
    *,
    deepcoin_client: DeepcoinTradingClientProtocol,
    venue: str,
    inst_id: str,
    side: str,
) -> tuple[str, str, str, str]:
    """Return a non-secret account-scoped lock key for trigger ownership."""

    return (
        venue.lower(),
        _trigger_protection_account_identity(deepcoin_client),
        inst_id.upper(),
        side.lower(),
    )


def _trigger_protection_account_identity(deepcoin_client: DeepcoinTradingClientProtocol) -> str:
    account_id = getattr(deepcoin_client, "account_id", None)
    if isinstance(account_id, str) and account_id.strip():
        raw_identity = f"account:{account_id.strip()}"
    else:
        credentials = getattr(deepcoin_client, "_credentials", None)
        api_key = getattr(credentials, "api_key", None)
        if isinstance(api_key, str) and api_key:
            raw_identity = f"api-key:{api_key}"
        else:
            raw_identity = "unknown-account"
    return hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()


def submit_recovery_order_live(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    submitted_at: datetime | None = None,
    max_order_legs: int | None = None,
) -> dict[str, Any]:
    """Enqueue and execute a confirmed recovery signal through the trade queue."""

    settings = load_trading_settings(session_factory)
    if not settings.auto_trade_enabled:
        raise RecoveryLiveSubmitError("auto_trade_disabled")

    trade_signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        contract_spec_provider=contract_spec_provider,
        enqueued_at=submitted_at,
    )
    return process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=deepcoin_client,
        contract_spec_provider=contract_spec_provider,
        processed_at=submitted_at,
        max_order_legs=max_order_legs,
    )


def submit_strategy_revision_replacement_live(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    draft: dict[str, Any],
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    submitted_at: datetime | None = None,
) -> dict[str, Any]:
    """Submit a prevalidated revision draft through the existing entry writer."""

    now = submitted_at or datetime.now(UTC)
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if (
            batch is None
            or batch.status != "submitting_replacements"
            or not batch.advance_claim_token
        ):
            raise RecoveryLiveSubmitError("revision_batch_not_reserved")
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        if binding is None:
            raise RecoveryLiveSubmitError("revision_binding_missing")
        strategy_instance_id = str(draft.get("strategy_instance_id") or "")
        source = draft.get("source") if isinstance(draft.get("source"), dict) else {}
        order_legs = (
            draft.get("order_legs")
            if isinstance(draft.get("order_legs"), list)
            else []
        )
        first_order_leg = (
            order_legs[0]
            if order_legs and isinstance(order_legs[0], dict)
            else {}
        )
        draft_side = str(
            draft.get("position_side")
            or draft.get("side")
            or first_order_leg.get("position_side")
            or ""
        ).lower()
        if (
            strategy_instance_id != str(binding.strategy_instance_id)
            or int(source.get("chat_id") or 0) != int(binding.chat_id)
            or int(source.get("message_id") or 0) != int(binding.message_id)
            or str(draft.get("symbol") or "").upper() != str(binding.symbol).upper()
            or draft_side != str(binding.side).lower()
        ):
            raise RecoveryLiveSubmitError("revision_draft_identity_mismatch")
        max_leg_index = max(
            (
                int(value)
                for (value,) in session.query(ExecutionOrderLeg.leg_index)
                .filter(
                    ExecutionOrderLeg.execution_binding_id == int(binding.id),
                    ExecutionOrderLeg.purpose == "entry",
                )
                .all()
            ),
            default=0,
        )
        binding_context = {
            "kol_id": binding.kol_id,
            "chat_id": int(binding.chat_id),
            "message_id": int(binding.message_id),
            "symbol": binding.symbol,
            "side": binding.side,
            "strategy_instance_id": binding.strategy_instance_id,
        }
    capability = evaluate_deepcoin_entry_capability(
        session_factory,
        symbol=str(binding_context["symbol"]),
        contract_spec_provider=contract_spec_provider,
    )
    if not capability.allowed:
        raise RecoveryLiveSubmitError(capability.reason)
    validated_draft = attach_contract_spec_evidence(dict(draft), capability)
    validated_draft["_entry_leg_index_offset"] = max_leg_index
    trade_signal = load_or_create_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="strategy_revision",
        kol_id=str(binding_context["kol_id"]),
        chat_id=int(binding_context["chat_id"]),
        message_id=int(batch_id),
        symbol=str(binding_context["symbol"]),
        side=str(binding_context["side"]),
        action="open_position",
        payload={
            "strategy_revision_batch_id": int(batch_id),
            "deepcoin_order_draft": validated_draft,
        },
        strategy_instance_id=str(binding_context["strategy_instance_id"]),
        enqueued_at=now,
    )
    try:
        trade_signal = claim_pending_trade_signal(
            session_factory,
            signal_id=trade_signal.id,
            claimed_at=now,
        )
    except TradeSignalClaimError as exc:
        raise RecoveryLiveSubmitError(str(exc)) from exc
    submission_progress = EntrySubmissionProgress()
    protected_entry_route_owned = (
        trade_signal.action == "open_position"
        and _protected_entry_route_access(
            session_factory,
            trade_signal_id=trade_signal.id,
        )
        in {"live", "readback_only"}
    )
    try:
        result = _submit_recovery_signal_direct(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=contract_spec_provider,
            submitted_at=now,
            validated_draft=validated_draft,
            submission_progress=submission_progress,
        )
    except Exception as exc:
        failure = exc
        if isinstance(exc, EntrySubmissionProgressError):
            failure = exc.cause
            submission_progress = exc.progress
        if _has_protected_entry_parent_operation(
            session_factory,
            trade_signal_id=trade_signal.id,
        ):
            finalize_trade_signal_from_execution_operation(
                session_factory,
                signal_id=trade_signal.id,
                finalized_at=now,
                expected_status="processing",
                safe_error_code=_safe_protected_failure_code(failure),
            )
        elif protected_entry_route_owned:
            freeze_trade_signal_for_protected_entry_recovery(
                session_factory,
                signal_id=trade_signal.id,
                frozen_at=now,
                expected_status="processing",
                safe_error_code=_safe_protected_failure_code(failure),
            )
        else:
            mark_trade_signal_failed(
                session_factory,
                signal_id=trade_signal.id,
                error=str(failure),
                failed_at=now,
                expected_status="processing",
                terminal_status=_entry_submission_failure_status(
                    failure,
                    progress=submission_progress,
                ),
            )
        if failure is not exc:
            raise failure from exc
        raise
    if _has_protected_entry_parent_operation(
        session_factory,
        trade_signal_id=trade_signal.id,
    ):
        finalize_trade_signal_from_execution_operation(
            session_factory,
            signal_id=trade_signal.id,
            result=result,
            finalized_at=now,
            expected_status="processing",
        )
    else:
        mark_trade_signal_submitted(
            session_factory,
            signal_id=trade_signal.id,
            result=result,
            processed_at=now,
            expected_status="processing",
        )
    return {"status": "submitted", **result}


def submit_entry_draft_revision_live(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    original_draft: dict[str, Any],
    operation: str,
    market_price,
    authorized_leg_indices: tuple[int, ...],
    expected_parent_fingerprint: str,
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    submitted_at: datetime | None = None,
) -> dict[str, Any]:
    """Apply an unchanged-fingerprint revision through the audited entry writer."""

    from telegram_kol_research.entry_draft_revisions import revise_entry_draft

    authoritative_draft, actual_fingerprint = load_entry_draft_revision_authority(
        session_factory,
        batch_id=int(batch_id),
        supplied_draft=original_draft,
    )
    _validate_entry_draft_revision_leg_authority(
        session_factory,
        batch_id=int(batch_id),
        authoritative_draft=authoritative_draft,
        authorized_leg_indices=authorized_leg_indices,
    )
    if str(expected_parent_fingerprint) != actual_fingerprint:
        raise RecoveryLiveSubmitError("entry_draft_parent_fingerprint_changed")
    revised = revise_entry_draft(
        authoritative_draft,
        operation=operation,
        market_price=market_price,
        authorized_leg_indices=authorized_leg_indices,
    )
    finalized_at = submitted_at or datetime.now(UTC)
    try:
        result = submit_strategy_revision_replacement_live(
            session_factory,
            batch_id=int(batch_id),
            draft=revised,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=contract_spec_provider,
            submitted_at=finalized_at,
        )
    except Exception:
        _finalize_entry_draft_revision_batch(
            session_factory,
            batch_id=int(batch_id),
            result=None,
            finalized_at=finalized_at,
        )
        raise
    _finalize_entry_draft_revision_batch(
        session_factory,
        batch_id=int(batch_id),
        result=result,
        finalized_at=finalized_at,
    )
    return result


def _finalize_entry_draft_revision_batch(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    result: dict[str, Any] | None,
    finalized_at: datetime,
) -> None:
    """Close the reservation boundary after the audited writer returns."""

    response_status = str((result or {}).get("status") or "").lower()
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if batch is None:
            raise RecoveryLiveSubmitError("revision_batch_missing")
        if batch.status != "submitting_replacements":
            raise RecoveryLiveSubmitError("revision_batch_state_changed")
        batch.replacement_response_json = (
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if result is not None
            else None
        )
        batch.advance_claim_token = None
        batch.advance_claimed_at = None
        batch.updated_at = finalized_at
        if response_status in {"confirmed", "succeeded"}:
            batch.status = "succeeded"
            batch.completed_at = finalized_at
        elif response_status == "submitted":
            batch.status = "reconciling"
        else:
            batch.status = "recovery_required"
            batch.reason_code = "revision_replacement_submit_unknown"
        session.commit()


def load_entry_draft_revision_authority(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    supplied_draft: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Load the exact bound draft and enrich only its durable execution deadline."""

    from telegram_kol_research.deepcoin_order_builder import (
        deepcoin_order_draft_fingerprint,
    )

    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if batch is None:
            raise RecoveryLiveSubmitError("revision_batch_missing")
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        try:
            binding_payload = json.loads(binding.payload_json or "{}") if binding else {}
        except (json.JSONDecodeError, TypeError):
            binding_payload = {}
        persisted_draft = (
            binding_payload.get("draft")
            if isinstance(binding_payload, dict)
            else None
        )
        if not isinstance(persisted_draft, dict):
            raise RecoveryLiveSubmitError("entry_draft_authority_missing")
        persisted_fingerprint = deepcoin_order_draft_fingerprint(persisted_draft)
        if supplied_draft is not None and (
            deepcoin_order_draft_fingerprint(supplied_draft)
            != persisted_fingerprint
        ):
            raise RecoveryLiveSubmitError("entry_draft_parent_fingerprint_changed")
        authoritative_draft = deepcopy(persisted_draft)
        if not (
            authoritative_draft.get("execution_deadline_at")
            or authoritative_draft.get("deadline_at")
        ):
            contracts = (
                session.query(InstructionExecutionContract)
                .filter(
                    InstructionExecutionContract.execution_binding_id
                    == int(batch.execution_binding_id),
                    InstructionExecutionContract.intent_kind == "entry",
                    InstructionExecutionContract.deadline_at.is_not(None),
                )
                .all()
            )
            deadlines = {
                _revision_deadline_iso(row.deadline_at)
                for row in contracts
                if row.deadline_at is not None
            }
            if len(deadlines) != 1:
                raise RecoveryLiveSubmitError(
                    "entry_draft_deadline_authority_missing"
                    if not deadlines
                    else "entry_draft_deadline_authority_ambiguous"
                )
            authoritative_draft["execution_deadline_at"] = deadlines.pop()
    return authoritative_draft, persisted_fingerprint


def _revision_deadline_iso(value: datetime) -> str:
    deadline = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return deadline.astimezone(UTC).isoformat()


def _validate_entry_draft_revision_leg_authority(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    authoritative_draft: dict[str, Any],
    authorized_leg_indices: tuple[int, ...],
) -> None:
    """Require exact known outcomes and durable absence for replaced legs."""

    draft_legs = authoritative_draft.get("order_legs")
    if not isinstance(draft_legs, list) or not draft_legs:
        raise RecoveryLiveSubmitError("entry_draft_authority_missing")
    parent_client_ids = [
        str(leg.get("client_order_id") or "") if isinstance(leg, dict) else ""
        for leg in draft_legs
    ]
    if (
        any(not value for value in parent_client_ids)
        or len(set(parent_client_ids)) != len(parent_client_ids)
    ):
        raise RecoveryLiveSubmitError("revision_leg_authority_incomplete")
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if (
            batch is None
            or batch.status != "submitting_replacements"
            or not batch.advance_claim_token
        ):
            raise RecoveryLiveSubmitError("revision_batch_not_reserved")
        execution_legs = (
            session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.execution_binding_id
                == int(batch.execution_binding_id),
                ExecutionOrderLeg.purpose == "entry",
            )
            .order_by(ExecutionOrderLeg.leg_index.asc())
            .all()
        )
        by_client_order_id: dict[str, list[ExecutionOrderLeg]] = {}
        for execution_leg in execution_legs:
            by_client_order_id.setdefault(
                str(execution_leg.client_order_id or ""), []
            ).append(execution_leg)
        by_index: dict[int, ExecutionOrderLeg] = {}
        for draft_index, client_order_id in enumerate(parent_client_ids, start=1):
            matches = by_client_order_id.get(client_order_id, [])
            if len(matches) != 1:
                raise RecoveryLiveSubmitError("revision_leg_authority_incomplete")
            by_index[draft_index] = matches[0]
        revision_by_execution_leg_id = {
            int(row.execution_order_leg_id): row
            for row in session.query(StrategyRevisionLeg)
            .filter(StrategyRevisionLeg.revision_batch_id == int(batch_id))
            .all()
        }
        authorized = set(int(value) for value in authorized_leg_indices)
        known_execution_statuses = {
            "pending",
            "submitted",
            "open",
            "active",
            "partially_filled",
            "filled",
            "partial_closed",
            "cancelled",
            "rejected",
            "failed",
            "expired",
        }
        for leg_index, execution_leg in by_index.items():
            execution_status = str(execution_leg.status or "").lower()
            attribution_status = str(
                execution_leg.attribution_status or ""
            ).lower()
            revision_leg = revision_by_execution_leg_id.get(int(execution_leg.id))
            revision_status = str(revision_leg.status or "").lower() if revision_leg else ""
            if (
                revision_status in {"submit_unknown", "cancel_submitting"}
                or execution_status not in known_execution_statuses
                or attribution_status
                in {
                    "ambiguous",
                    "attribution_conflict",
                    "evidence_unavailable",
                    "unknown",
                    "unverified",
                }
                or (execution_leg.pos_id and attribution_status != "verified")
                or (
                    execution_status in {"filled", "partially_filled", "partial_closed"}
                    and not execution_leg.pos_id
                )
            ):
                raise RecoveryLiveSubmitError("revision_leg_outcome_unknown")
            if leg_index not in authorized:
                continue
            if revision_leg is None:
                raise RecoveryLiveSubmitError("revision_leg_authority_incomplete")
            if revision_status not in {"cancelled", "terminal"}:
                raise RecoveryLiveSubmitError("revision_leg_not_confirmed_absent")
            if execution_status not in {"cancelled", "rejected", "failed", "expired"}:
                raise RecoveryLiveSubmitError("revision_leg_not_confirmed_absent")
            if execution_leg.pos_id or revision_leg.pos_id:
                raise RecoveryLiveSubmitError("revision_leg_position_already_exists")


def enqueue_recovery_trade_signal(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    enqueued_at: datetime | None = None,
    selected_entry_leg_indices: list[int] | tuple[int, ...] | None = None,
) -> TradeSignalRecord:
    """Send one confirmed recovery strategy into the durable trade-signal queue."""

    gate = validate_recovery_live_submit_gate(
        session_factory,
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        contract_spec_provider=contract_spec_provider,
    )
    if not gate["would_submit"]:
        raise RecoveryLiveSubmitError(
            "signal_enqueue_blocked:" + ",".join(str(code) for code in gate["reason_codes"])
        )

    draft = gate["deepcoin_order_draft"]
    if not isinstance(draft, dict):
        raise RecoveryLiveSubmitError("missing_deepcoin_order_draft")
    order_legs = draft.get("order_legs")
    if not isinstance(order_legs, list) or not order_legs:
        raise RecoveryLiveSubmitError("missing_order_legs")
    selected_indices = list(
        selected_entry_leg_indices
        if selected_entry_leg_indices is not None
        else range(1, len(order_legs) + 1)
    )
    if (
        not selected_indices
        or any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 1
            or index > len(order_legs)
            for index in selected_indices
        )
        or len(set(selected_indices)) != len(selected_indices)
    ):
        raise RecoveryLiveSubmitError("invalid_selected_entry_leg_indices")
    draft = {
        **draft,
        "selected_entry_leg_indices": selected_indices,
        "selected_entry_leg_count": len(selected_indices),
    }
    source = draft.get("source") if isinstance(draft.get("source"), dict) else {}
    return load_or_create_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id=str(source.get("kol_id") or "unknown"),
        chat_id=chat_id,
        message_id=message_id,
        symbol=str(draft.get("symbol") or symbol),
        side=side,
        action="open_position",
        payload={
            "source": {
                "chat_id": chat_id,
                "message_id": message_id,
                "symbol": str(draft.get("symbol") or symbol).upper(),
                "side": side.lower(),
            },
            "deepcoin_order_draft": draft,
        },
        strategy_instance_id=str(draft.get("strategy_instance_id") or ""),
        enqueued_at=enqueued_at,
    )


def process_trade_signal_live(
    session_factory: sessionmaker,
    *,
    signal_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    processed_at: datetime | None = None,
    max_order_legs: int | None = None,
    message_instruction_item_id: int | None = None,
    execution_contract_mode: str = "disabled",
    writer_boundary_at: datetime | None = None,
) -> dict[str, Any]:
    """Receive and execute one pending trade signal."""

    settings = load_trading_settings(session_factory)
    if not settings.auto_trade_enabled:
        raise RecoveryLiveSubmitError("auto_trade_disabled")

    now = processed_at or datetime.now(UTC)
    try:
        trade_signal = claim_pending_trade_signal(
            session_factory,
            signal_id=signal_id,
            claimed_at=now,
        )
    except TradeSignalClaimError as exc:
        raise RecoveryLiveSubmitError(str(exc)) from exc
    verified_v2_assembly = False
    submission_progress = EntrySubmissionProgress()
    execution_boundary_at = now
    protected_entry_route_owned = (
        trade_signal.action == "open_position"
        and _protected_entry_route_access(
            session_factory,
            trade_signal_id=trade_signal.id,
        )
        in {"live", "readback_only"}
    )
    try:
        if trade_signal.action == "open_position":
            verified_v2_assembly = _require_synchronized_finalized_entry_assembly(
                session_factory,
                trade_signal=trade_signal,
            )
            execution_boundary_at = writer_boundary_at or datetime.now(UTC)
            _prepare_instruction_entry_submission(
                session_factory,
                trade_signal=trade_signal,
                message_instruction_item_id=message_instruction_item_id,
                execution_contract_mode=execution_contract_mode,
                prepared_at=execution_boundary_at,
            )
            result = _submit_recovery_signal_direct(
                session_factory,
                trade_signal=trade_signal,
                deepcoin_client=deepcoin_client,
                contract_spec_provider=contract_spec_provider,
                submitted_at=execution_boundary_at,
                max_order_legs=max_order_legs,
                verified_v2_assembly=verified_v2_assembly,
                submission_progress=submission_progress,
            )
        else:
            if (
                trade_signal.action.lower() in MANAGEMENT_TRADE_SIGNAL_ACTIONS
                and trade_signal.source_type not in MANUAL_MANAGEMENT_SOURCE_TYPES
                and canonical_management_batch_id(trade_signal.payload) is None
            ):
                raise RecoveryLiveSubmitError(
                    "legacy_management_signal_requires_batch"
                )
            result = execute_deepcoin_management_signal(
                session_factory,
                trade_signal=trade_signal,
                deepcoin_client=deepcoin_client,
                executed_at=now,
            )
    except Exception as exc:
        failure = exc
        if isinstance(exc, EntrySubmissionProgressError):
            failure = exc.cause
            submission_progress = exc.progress
        terminal_status = _entry_submission_failure_status(
            failure,
            progress=submission_progress,
        )
        if _has_protected_entry_parent_operation(
            session_factory,
            trade_signal_id=signal_id,
        ):
            finalize_trade_signal_from_execution_operation(
                session_factory,
                signal_id=signal_id,
                finalized_at=execution_boundary_at,
                expected_status="processing",
                safe_error_code=_safe_protected_failure_code(failure),
            )
        elif protected_entry_route_owned:
            freeze_trade_signal_for_protected_entry_recovery(
                session_factory,
                signal_id=signal_id,
                frozen_at=execution_boundary_at,
                expected_status="processing",
                safe_error_code=_safe_protected_failure_code(failure),
            )
        else:
            mark_trade_signal_failed(
                session_factory,
                signal_id=signal_id,
                error=str(failure),
                failed_at=execution_boundary_at,
                expected_status="processing",
                terminal_status=terminal_status,
            )
        _project_instruction_entry_submission(
            session_factory,
            trade_signal=trade_signal,
            message_instruction_item_id=message_instruction_item_id,
            execution_contract_mode=execution_contract_mode,
            submission_progress=submission_progress,
            error=failure,
            projected_at=execution_boundary_at,
        )
        if failure is not exc:
            raise failure from exc
        raise
    if _has_protected_entry_parent_operation(
        session_factory,
        trade_signal_id=signal_id,
    ):
        finalize_trade_signal_from_execution_operation(
            session_factory,
            signal_id=signal_id,
            result=result,
            finalized_at=execution_boundary_at,
            expected_status="processing",
        )
    else:
        mark_trade_signal_submitted(
            session_factory,
            signal_id=signal_id,
            result=result,
            processed_at=execution_boundary_at,
            expected_status="processing",
        )
    _project_instruction_entry_submission(
        session_factory,
        trade_signal=trade_signal,
        message_instruction_item_id=message_instruction_item_id,
        execution_contract_mode=execution_contract_mode,
        submission_progress=submission_progress,
        error=None,
        projected_at=execution_boundary_at,
    )
    return result


def _prepare_instruction_entry_submission(
    session_factory,
    *,
    trade_signal: TradeSignalRecord,
    message_instruction_item_id: int | None,
    execution_contract_mode: str,
    prepared_at: datetime,
) -> None:
    if message_instruction_item_id is None or execution_contract_mode == "disabled":
        return
    from telegram_kol_research.instruction_execution_entry_adapter import (
        EntryExecutionContractBlocked,
        prepare_entry_submission_contract,
    )

    draft = (
        trade_signal.payload.get("deepcoin_order_draft")
        if isinstance(trade_signal.payload, dict)
        else None
    )
    try:
        prepare_entry_submission_contract(
            session_factory,
            message_instruction_item_id=int(message_instruction_item_id),
            trade_signal_id=int(trade_signal.id),
            draft=draft if isinstance(draft, dict) else {},
            prepared_at=prepared_at,
            mode=execution_contract_mode,
        )
    except EntryExecutionContractBlocked:
        raise
    except Exception:
        if execution_contract_mode == "live":
            raise


def _project_instruction_entry_submission(
    session_factory,
    *,
    trade_signal: TradeSignalRecord,
    message_instruction_item_id: int | None,
    execution_contract_mode: str,
    submission_progress: EntrySubmissionProgress,
    error: Exception | None,
    projected_at: datetime,
) -> None:
    if message_instruction_item_id is None or execution_contract_mode == "disabled":
        return
    from telegram_kol_research.instruction_execution_entry_adapter import (
        project_entry_submission_result,
    )

    try:
        project_entry_submission_result(
            session_factory,
            message_instruction_item_id=int(message_instruction_item_id),
            trade_signal_id=int(trade_signal.id),
            attempted_writes=int(submission_progress.attempted_writes),
            confirmed_legs=int(submission_progress.confirmed_legs),
            error=error,
            projected_at=projected_at,
            mode=execution_contract_mode,
        )
    except Exception:
        if execution_contract_mode == "live":
            raise


def process_next_trade_signal_live(
    session_factory: sessionmaker,
    *,
    deepcoin_client: DeepcoinTradingClientProtocol | None = None,
    deepcoin_client_factory=None,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    processed_at: datetime | None = None,
    max_order_legs: int | None = None,
) -> dict[str, Any] | None:
    """Receive and execute the oldest pending Deepcoin trade signal."""

    pending = list_pending_trade_signals(session_factory, venue="deepcoin", limit=1)
    if not pending:
        return None
    trade_signal = pending[0]
    if deepcoin_client is None and _is_automatic_legacy_management_signal(trade_signal):
        return process_trade_signal_live(
            session_factory,
            signal_id=trade_signal.id,
            deepcoin_client=None,
            contract_spec_provider=contract_spec_provider,
            processed_at=processed_at,
            max_order_legs=max_order_legs,
        )
    if deepcoin_client is None:
        if deepcoin_client_factory is None:
            raise RecoveryLiveSubmitError("missing_deepcoin_client")
        deepcoin_client = deepcoin_client_factory()
    return process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=deepcoin_client,
        contract_spec_provider=contract_spec_provider,
        processed_at=processed_at,
        max_order_legs=max_order_legs,
    )


def _is_automatic_legacy_management_signal(
    trade_signal: TradeSignalRecord,
) -> bool:
    return (
        trade_signal.action.lower() in MANAGEMENT_TRADE_SIGNAL_ACTIONS
        and trade_signal.source_type not in MANUAL_MANAGEMENT_SOURCE_TYPES
        and canonical_management_batch_id(trade_signal.payload) is None
    )


def _require_current_contract_spec_matches_queued(
    *,
    current_draft: dict[str, Any],
    queued_draft: dict[str, Any],
) -> None:
    """Reject a queued entry if its pinned sizing authority has drifted."""

    current_spec = current_draft.get("contract_spec")
    queued_spec = queued_draft.get("contract_spec")
    if not isinstance(current_spec, dict) or queued_spec != current_spec:
        raise RecoveryLiveSubmitError("queued_contract_spec_mismatch")
    current_snapshot = current_draft.get("contract_spec_snapshot")
    if current_snapshot is not None and (
        not isinstance(current_snapshot, dict)
        or queued_draft.get("contract_spec_snapshot") != current_snapshot
    ):
        raise RecoveryLiveSubmitError("queued_contract_spec_snapshot_mismatch")


def _protected_entry_operation_key(
    trade_signal_id: int,
    leg_index: int,
    *,
    protection_index: int | None = None,
) -> str:
    suffix = (
        f"protection:{protection_index}"
        if protection_index is not None
        else "entry"
    )
    return (
        f"protected-entry:v1:signal:{int(trade_signal_id)}:"
        f"leg:{int(leg_index)}:{suffix}"
    )


def _canonical_fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _protected_position_ref(pos_id: str) -> str:
    return hashlib.sha256(f"position:{pos_id}".encode("utf-8")).hexdigest()


def _operation_evidence(operation: ExecutionOperationRecord) -> dict[str, Any]:
    def reject_duplicate_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_operation_evidence_key")
            result[key] = value
        return result

    try:
        if (
            not isinstance(operation.evidence_json, str)
            or len(operation.evidence_json.encode("utf-8")) > 4096
        ):
            raise ValueError("operation_evidence_size_invalid")
        evidence = json.loads(
            operation.evidence_json,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("operation_evidence_constant_invalid")
            ),
        )
        if (
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            != operation.evidence_json
        ):
            raise ValueError("operation_evidence_not_canonical")
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise RecoveryLiveSubmitError(
            "protected_entry_operation_evidence_invalid"
        )
    if not isinstance(evidence, dict):
        raise RecoveryLiveSubmitError(
            "protected_entry_operation_evidence_invalid"
        )
    return evidence


def _baseline_position_refs(
    operation: ExecutionOperationRecord,
) -> frozenset[str]:
    values = _operation_evidence(operation).get("pre_submit_position_refs")
    if (
        not isinstance(values, list)
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in values
        )
        or values != sorted(set(values))
    ):
        raise RecoveryLiveSubmitError(
            "protected_entry_baseline_evidence_invalid"
        )
    return frozenset(values)


def _require_protected_entry_client(
    deepcoin_client: DeepcoinTradingClientProtocol,
) -> None:
    uid_scope_hash = getattr(deepcoin_client, "uid_scope_hash", None)
    if (
        not isinstance(uid_scope_hash, str)
        or len(uid_scope_hash) != 64
        or any(character not in "0123456789abcdef" for character in uid_scope_hash)
        or not callable(getattr(deepcoin_client, "request_scope", None))
        or not callable(getattr(deepcoin_client, "list_order_history", None))
    ):
        raise RecoveryLiveSubmitError(
            "protected_entry_client_contract_unavailable"
        )


def _protected_client_clock(deepcoin_client: Any):
    clock = getattr(deepcoin_client, "_monotonic_factory", None)
    return clock if callable(clock) else time.monotonic


def _protected_client_sleep(deepcoin_client: Any):
    sleeper = getattr(deepcoin_client, "_sleep_fn", None)
    return sleeper if callable(sleeper) else time.sleep


def _current_write_generation(
    session_factory: sessionmaker,
    *,
    uid_scope_hash: str,
) -> int:
    record = load_account_write_generation(
        session_factory,
        uid_scope_hash=uid_scope_hash,
    )
    return int(record.generation) if record is not None else 0


def _raw_pending_tpsl_reader(
    deepcoin_client: Any,
    *,
    inst_id: str,
):
    raw_reader = getattr(deepcoin_client, "read_trigger_orders_pending", None)
    list_reader = getattr(deepcoin_client, "list_trigger_orders_pending", None)
    if callable(raw_reader):
        return raw_reader(inst_id=inst_id)
    if callable(list_reader):
        return list_reader(inst_id=inst_id)
    raise DeepcoinSnapshotUnavailable("snapshot_reader_unavailable")


def _record_pending_tpsl_snapshot(
    session_factory: sessionmaker,
    *,
    operation: ExecutionOperationRecord,
    capture: _PendingTpslCapture,
    source: str,
    reused: bool,
) -> None:
    collection = capture.collection
    baseline_json = capture.normalized_baseline_json
    if baseline_json is None and collection.complete:
        try:
            baseline_json = _normalized_pending_tpsl_rows(capture.rows)
        except RecoveryLiveSubmitError:
            baseline_json = None
    durable_evidence: dict[str, Any] = {
        "source": source,
        "reused": reused,
        "snapshot_ref": hashlib.sha256(
            (
                "pending_tpsl:"
                + str(collection.fingerprint or "unavailable")
            ).encode("utf-8")
        ).hexdigest(),
    }
    if baseline_json is not None:
        compressed = base64.b64encode(
            zlib.compress(baseline_json.encode("utf-8"), level=9)
        ).decode("ascii")
        if len(compressed) <= 3000:
            durable_evidence.update(
                {
                    "baseline_deflate_b64": compressed,
                    "baseline_fingerprint": hashlib.sha256(
                        baseline_json.encode("utf-8")
                    ).hexdigest(),
                }
            )
    record_snapshot_evidence(
        session_factory,
        operation_id=operation.id,
        expected_operation_key=operation.operation_key,
        snapshot_kind="protection_pending",
        available=collection.available,
        schema_valid=collection.schema_valid,
        complete=collection.complete,
        row_count=collection.row_count,
        page_count=collection.page_count,
        collection_fingerprint=collection.fingerprint,
        start_write_generation=capture.start_write_generation,
        end_write_generation=capture.end_write_generation,
        capture_started_at=capture.capture_started_at,
        capture_ended_at=capture.capture_ended_at,
        evidence=durable_evidence,
        error_category=(
            None if collection.complete else "snapshot_incomplete"
        ),
        error_code=(
            None
            if collection.complete
            else collection.reason_code or "snapshot_incomplete"
        ),
    )


def _capture_pending_tpsl(
    session_factory: sessionmaker,
    *,
    deepcoin_client: Any,
    uid_scope_hash: str,
    inst_id: str,
) -> _PendingTpslCapture:
    started_at = datetime.now(UTC)
    snapshot = capture_account_snapshot(
        session_factory,
        uid_scope_hash=uid_scope_hash,
        readers={
            "pending_trigger_orders": lambda: _raw_pending_tpsl_reader(
                deepcoin_client,
                inst_id=inst_id,
            )
        },
    )
    ended_at = datetime.now(UTC)
    collection = snapshot.collections[0]
    return _PendingTpslCapture(
        collection=collection,
        rows=collection.rows,
        start_write_generation=snapshot.start_write_generation,
        end_write_generation=snapshot.end_write_generation,
        capture_started_at=started_at,
        capture_ended_at=ended_at,
    )


def _unavailable_pending_tpsl_capture(
    session_factory: sessionmaker,
    *,
    uid_scope_hash: str,
    reason_code: str,
) -> _PendingTpslCapture:
    captured_at = datetime.now(UTC)
    generation = _current_write_generation(
        session_factory,
        uid_scope_hash=uid_scope_hash,
    )
    collection = build_exchange_collection_evidence(
        endpoint="pending_trigger_orders",
        response=None,
        read_error=DeepcoinSnapshotUnavailable(reason_code),
    )
    return _PendingTpslCapture(
        collection=collection,
        rows=(),
        start_write_generation=generation,
        end_write_generation=generation,
        capture_started_at=captured_at,
        capture_ended_at=captured_at,
    )


class _PendingTpslRecordingClient:
    """Record the last complete protection readback without changing writers."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker,
        deepcoin_client: Any,
        uid_scope_hash: str,
    ) -> None:
        self._session_factory = session_factory
        self._client = deepcoin_client
        self._uid_scope_hash = uid_scope_hash
        self.latest_capture: _PendingTpslCapture | None = None

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    @contextmanager
    def request_scope(self, scope):
        factory = getattr(self._client, "request_scope", None)
        if not callable(factory):
            yield self
            return
        with factory(scope):
            yield self

    def list_trigger_orders_pending(self, *, inst_id: str):
        capture = _capture_pending_tpsl(
            self._session_factory,
            deepcoin_client=self._client,
            uid_scope_hash=self._uid_scope_hash,
            inst_id=inst_id,
        )
        self.latest_capture = capture
        if not capture.collection.complete:
            raise DeepcoinSnapshotUnavailable(
                capture.collection.reason_code or "snapshot_incomplete"
            )
        return [dict(row) for row in capture.rows]

    def read_trigger_orders_pending(self, *, inst_id: str):
        capture = _capture_pending_tpsl(
            self._session_factory,
            deepcoin_client=self._client,
            uid_scope_hash=self._uid_scope_hash,
            inst_id=inst_id,
        )
        self.latest_capture = capture
        if not capture.collection.complete:
            raise DeepcoinSnapshotUnavailable(
                capture.collection.reason_code or "snapshot_incomplete"
            )
        return {"data": [dict(row) for row in capture.rows]}


def _attempt_recorder(
    session_factory: sessionmaker,
    *,
    operation: ExecutionOperationRecord,
    uid_scope_hash: str,
):
    def record(fact) -> None:
        completed_at = datetime.now(UTC)
        started_at = completed_at - timedelta(
            milliseconds=max(0, int(fact.latency_ms))
        )
        record_request_attempt(
            session_factory,
            operation_id=operation.id,
            expected_operation_key=operation.operation_key,
            expected_request_fingerprint=operation.request_fingerprint,
            uid_scope_hash=uid_scope_hash,
            fact=fact,
            started_at=started_at,
            completed_at=completed_at,
        )

    return record


def _protected_request_scope(
    session_factory: sessionmaker,
    *,
    runtime: _ProtectedEntryRuntime,
    operation: ExecutionOperationRecord,
    phase: str,
) -> DeepcoinRequestScope:
    return DeepcoinRequestScope(
        phase=phase,
        priority=RequestPriority.CRITICAL,
        deadline_monotonic=runtime.deadline_monotonic,
        correlation_id=operation.operation_key,
        attempt_recorder=_attempt_recorder(
            session_factory,
            operation=operation,
            uid_scope_hash=runtime.uid_scope_hash,
        ),
    )


def _transition_protected_operation(
    session_factory: sessionmaker,
    operation: ExecutionOperationRecord,
    *,
    phase: str,
    state: str,
    certainty: str,
    reason_code: str,
    evidence: Mapping[str, Any],
    changed_at: datetime,
    writer_attempted_at: datetime | None = None,
    completed_at: datetime | None = None,
    error_category: str | None = None,
) -> ExecutionOperationRecord:
    prior_evidence = _operation_evidence(operation)
    next_evidence = dict(evidence)
    for immutable_key in (
        "leg_index",
        "expected_entry_leg_indices",
        "uid_scope_hash",
    ):
        immutable_value = prior_evidence.get(immutable_key)
        supplied_value = next_evidence.get(immutable_key, immutable_value)
        if immutable_value is not None and supplied_value != immutable_value:
            raise RecoveryLiveSubmitError(
                "protected_entry_operation_evidence_conflict"
            )
        if immutable_value is not None:
            next_evidence[immutable_key] = immutable_value
    return transition_execution_operation(
        session_factory,
        operation_id=operation.id,
        expected_operation_key=operation.operation_key,
        expected_state=operation.state,
        expected_state_version=operation.state_version,
        phase=phase,
        state=state,
        outcome_certainty=certainty,
        error_category=error_category,
        reason_code=reason_code,
        writer_attempted_at=writer_attempted_at,
        completed_at=completed_at,
        evidence=next_evidence,
        updated_at=changed_at,
    )


def _reserve_protected_entry_parent(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    leg: Mapping[str, Any],
    leg_index: int,
    order_payload: Mapping[str, Any],
    submitted_at: datetime,
    deadline_at: datetime,
    pre_submit_position_refs: frozenset[str],
    expected_entry_leg_indices: tuple[int, ...],
    uid_scope_hash: str,
) -> ExecutionOperationRecord:
    operation_key = _protected_entry_operation_key(
        trade_signal.id,
        leg_index,
    )
    request_fingerprint = _canonical_fingerprint(order_payload)
    economics_fingerprint = _canonical_fingerprint(
        {
            "instrument_id": order_payload.get("instId"),
            "side": order_payload.get("side"),
            "position_side": order_payload.get("posSide"),
            "quantity": order_payload.get("sz"),
            "client_order_id": order_payload.get("clOrdId"),
            "leg_index": leg_index,
        }
    )
    with session_factory() as session:
        existing_id = (
            session.query(DeepcoinExecutionOperation.id)
            .filter(
                DeepcoinExecutionOperation.operation_key == operation_key
            )
            .scalar()
        )
    if existing_id is not None:
        existing = load_operation_bundle(
            session_factory,
            operation_id=int(existing_id),
        ).operation
        if (
            existing.trade_signal_id != trade_signal.id
            or existing.parent_operation_id is not None
            or existing.contract_version != PROTECTED_ENTRY_CONTRACT_VERSION
            or existing.request_fingerprint != request_fingerprint
            or existing.economics_fingerprint != economics_fingerprint
        ):
            raise DeepcoinOperationConflict("operation_identity_conflict")
        existing_evidence = _operation_evidence(existing)
        _baseline_position_refs(existing)
        expected_indices_value = existing_evidence.get(
            "expected_entry_leg_indices"
        )
        uid_scope_value = existing_evidence.get("uid_scope_hash")
        if expected_indices_value is None and uid_scope_value is None:
            existing_bundle = load_operation_bundle(
                session_factory,
                operation_id=existing.id,
            )
            durable_uid_scope_hashes = {
                attempt.uid_scope_hash
                for attempt in existing_bundle.attempts
                if attempt.method == "POST"
                and attempt.request_fingerprint
                == existing.request_fingerprint
            }
            if (
                existing.reason_code is None
                or existing_evidence.get("leg_index") not in {None, leg_index}
                or existing.writer_attempted_at is None
                or durable_uid_scope_hashes != {uid_scope_hash}
            ):
                raise DeepcoinOperationConflict("operation_identity_conflict")
            existing = _transition_protected_operation(
                session_factory,
                existing,
                phase=existing.phase,
                state=existing.state,
                certainty=existing.outcome_certainty,
                error_category=existing.error_category,
                reason_code=existing.reason_code,
                evidence={
                    **existing_evidence,
                    "leg_index": leg_index,
                    "expected_entry_leg_indices": list(
                        expected_entry_leg_indices
                    ),
                    "uid_scope_hash": uid_scope_hash,
                },
                changed_at=datetime.now(UTC),
            )
        elif (
            expected_indices_value != list(expected_entry_leg_indices)
            or uid_scope_value != uid_scope_hash
        ):
            raise DeepcoinOperationConflict("operation_identity_conflict")
        return existing
    operation = reserve_execution_operation(
        session_factory,
        operation_key=operation_key,
        trade_signal_id=trade_signal.id,
        contract_version=PROTECTED_ENTRY_CONTRACT_VERSION,
        phase="entry_preflight",
        state="planned",
        outcome_certainty="not_sent",
        request_fingerprint=request_fingerprint,
        economics_fingerprint=economics_fingerprint,
        deadline_at=deadline_at,
        evidence={
            "contract_version": PROTECTED_ENTRY_CONTRACT_VERSION,
            "leg_index": leg_index,
            "expected_entry_leg_indices": list(expected_entry_leg_indices),
            "uid_scope_hash": uid_scope_hash,
            "client_order_ref": hashlib.sha256(
                str(order_payload.get("clOrdId") or "").encode("utf-8")
            ).hexdigest(),
            "pre_submit_position_refs": sorted(pre_submit_position_refs),
        },
        created_at=submitted_at,
    )
    if operation.state == "planned":
        operation = _transition_protected_operation(
            session_factory,
            operation,
            phase="entry_preflight",
            state="entry_prepared",
            certainty="not_sent",
            reason_code="entry_intent_prepared",
            evidence={
                "leg_index": leg_index,
                "writer_attempted": False,
                "pre_submit_position_refs": sorted(
                    pre_submit_position_refs
                ),
            },
            changed_at=submitted_at,
        )
    return operation


def _protected_entry_writer_allowed(
    session_factory: sessionmaker,
    *,
    trade_signal_id: int,
) -> bool:
    settings = load_trading_settings(session_factory)
    return (
        protected_entry_mode_for_signal(settings, int(trade_signal_id))
        == "live"
    )


def _protected_entry_route_access(
    session_factory: sessionmaker,
    *,
    trade_signal_id: int,
) -> str:
    """Pin an existing v1 writer to its contract after rollout disable."""

    with session_factory() as session:
        operations = (
            session.query(DeepcoinExecutionOperation)
            .filter(
                DeepcoinExecutionOperation.trade_signal_id
                == int(trade_signal_id),
                DeepcoinExecutionOperation.parent_operation_id.is_(None),
                DeepcoinExecutionOperation.operation_key.like(
                    "protected-entry:%"
                ),
            )
            .all()
        )
    settings = load_trading_settings(session_factory)
    if not operations:
        return (
            "live"
            if protected_entry_mode_for_signal(
                settings, int(trade_signal_id)
            )
            == "live"
            else "legacy"
        )
    if len(operations) != 1:
        raise RecoveryLiveSubmitError(
            "protected_entry_parent_operation_conflict"
        )
    operation = operations[0]
    access = protected_entry_operation_access(
        settings,
        signal_id=int(trade_signal_id),
        contract_version=operation.contract_version,
        writer_attempted=operation.writer_attempted_at is not None,
    )
    if access == "stop":
        raise RecoveryLiveSubmitError(
            "protected_entry_operation_stopped"
        )
    return access


def _has_protected_entry_parent_operation(
    session_factory: sessionmaker,
    *,
    trade_signal_id: int,
) -> bool:
    with session_factory() as session:
        return (
            session.query(DeepcoinExecutionOperation.id)
            .filter(
                DeepcoinExecutionOperation.trade_signal_id
                == int(trade_signal_id),
                DeepcoinExecutionOperation.parent_operation_id.is_(None),
                DeepcoinExecutionOperation.contract_version
                == PROTECTED_ENTRY_CONTRACT_VERSION,
            )
            .first()
            is not None
        )


def _later_leg_readback_operation_exists(
    session_factory: sessionmaker,
    *,
    trade_signal_id: int,
    leg_index: int,
) -> bool:
    with session_factory() as session:
        operation = (
            session.query(DeepcoinExecutionOperation)
            .filter(
                DeepcoinExecutionOperation.operation_key
                == _protected_entry_operation_key(
                    trade_signal_id,
                    leg_index,
                )
            )
            .one_or_none()
        )
        return bool(
            operation is not None
            and operation.contract_version
            == PROTECTED_ENTRY_CONTRACT_VERSION
            and operation.writer_attempted_at is not None
            and operation.state
            in {
                "entry_submitting",
                "entry_pending_readback",
                "entry_unknown",
                "completed",
            }
        )


def _any_later_leg_readback_operation_exists(
    session_factory: sessionmaker,
    *,
    trade_signal_id: int,
) -> bool:
    with session_factory() as session:
        return (
            session.query(DeepcoinExecutionOperation.id)
            .filter(
                DeepcoinExecutionOperation.trade_signal_id
                == int(trade_signal_id),
                DeepcoinExecutionOperation.parent_operation_id.is_not(None),
                DeepcoinExecutionOperation.phase.in_(
                    ("entry_submit", "entry_readback", "completed")
                ),
                DeepcoinExecutionOperation.writer_attempted_at.is_not(None),
                DeepcoinExecutionOperation.state.in_(
                    (
                        "entry_submitting",
                        "entry_pending_readback",
                        "entry_unknown",
                        "completed",
                    )
                ),
            )
            .first()
            is not None
        )


def _submit_protected_market_entry(
    *,
    session_factory: sessionmaker,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    draft: dict[str, Any],
    leg: Mapping[str, Any],
    leg_index: int,
    side: str,
    source: Mapping[str, Any],
    order_payload: Mapping[str, Any],
    progress: EntrySubmissionProgress,
    submitted_at: datetime,
    expected_entry_leg_indices: tuple[int, ...],
) -> tuple[dict[str, Any], str, str, _ProtectedEntryRuntime]:
    clock = _protected_client_clock(deepcoin_client)
    sleeper = _protected_client_sleep(deepcoin_client)
    deadline_monotonic = float(clock()) + 10.0
    deadline_at = submitted_at + timedelta(seconds=10)
    parent_operation_key = _protected_entry_operation_key(
        trade_signal.id,
        leg_index,
    )
    with session_factory() as session:
        resuming_existing_operation = (
            session.query(DeepcoinExecutionOperation.id)
            .filter(
                DeepcoinExecutionOperation.operation_key
                == parent_operation_key
            )
            .first()
            is not None
        )
    uid_scope_hash = str(getattr(deepcoin_client, "uid_scope_hash"))
    preflight_scope = DeepcoinRequestScope(
        phase="entry_preflight",
        priority=RequestPriority.CRITICAL,
        deadline_monotonic=deadline_monotonic,
        correlation_id=(
            f"protected-entry:v1:signal:{trade_signal.id}:"
            f"leg:{leg_index}:preflight"
        ),
    )
    with deepcoin_client.request_scope(preflight_scope):
        pre_submit_position_ids = _load_matching_position_ids(
            deepcoin_client,
            draft=draft,
            side=side,
        )
    current_position_refs = frozenset(
        _protected_position_ref(pos_id)
        for pos_id in pre_submit_position_ids
    )
    operation = _reserve_protected_entry_parent(
        session_factory,
        trade_signal=trade_signal,
        leg=leg,
        leg_index=leg_index,
        order_payload=order_payload,
        submitted_at=submitted_at,
        deadline_at=deadline_at,
        pre_submit_position_refs=current_position_refs,
        expected_entry_leg_indices=expected_entry_leg_indices,
        uid_scope_hash=uid_scope_hash,
    )
    if resuming_existing_operation:
        remaining_seconds = max(
            0.0,
            (
                operation.deadline_at
                - submitted_at.replace(tzinfo=None)
            ).total_seconds(),
        )
        deadline_monotonic = float(clock()) + remaining_seconds
    pre_submit_position_refs = _baseline_position_refs(operation)
    runtime = _ProtectedEntryRuntime(
        operation=operation,
        deadline_monotonic=deadline_monotonic,
        monotonic_factory=clock,
        sleep_fn=sleeper,
        uid_scope_hash=uid_scope_hash,
        pre_submit_position_refs=pre_submit_position_refs,
    )
    if operation.state == "protected":
        with session_factory() as session:
            leg_row = (
                session.query(ExecutionOrderLeg)
                .filter(
                    ExecutionOrderLeg.strategy_instance_id
                    == str(
                        draft.get("strategy_instance_id")
                        or trade_signal.strategy_instance_id
                        or ""
                    ),
                    ExecutionOrderLeg.leg_index == leg_index,
                    ExecutionOrderLeg.purpose == "entry",
                )
                .one_or_none()
            )
            order_id = str(leg_row.order_id or "") if leg_row else ""
            pos_id = str(leg_row.pos_id or "") if leg_row else ""
        if not (
            order_id
            and pos_id
            and _safe_protected_exchange_identity(order_id)
            and _safe_protected_exchange_identity(pos_id)
        ):
            raise RecoveryLiveSubmitError(
                "protected_entry_confirmed_identity_missing"
            )
        progress.record_confirmed_leg()
        return (
            {
                "code": "0",
                "data": {"ordId": order_id, "posId": pos_id},
            },
            order_id,
            pos_id,
            runtime,
        )
    if operation.writer_attempted_at is None:
        if operation.state != "entry_prepared" or not _protected_entry_writer_allowed(
            session_factory,
            trade_signal_id=trade_signal.id,
        ):
            raise RecoveryLiveSubmitError(
                "protected_entry_submit_not_authorized"
            )
        try:
            with _entry_source_exchange_write_gate(
                session_factory,
                trade_signal=trade_signal,
                source=dict(source),
            ):
                advance_account_write_generation(
                    session_factory,
                    uid_scope_hash=uid_scope_hash,
                )
                try:
                    if not _protected_entry_writer_allowed(
                        session_factory,
                        trade_signal_id=trade_signal.id,
                    ):
                        raise RecoveryLiveSubmitError(
                            "protected_entry_submit_not_authorized"
                        )
                    attempted_at = datetime.now(UTC)
                    operation = _transition_protected_operation(
                        session_factory,
                        operation,
                        phase="entry_submit",
                        state="entry_submitting",
                        certainty="not_sent",
                        reason_code="entry_submit_authorized",
                        evidence={
                            "leg_index": leg_index,
                            "writer_attempted": True,
                            "pre_submit_position_refs": sorted(
                                pre_submit_position_refs
                            ),
                        },
                        changed_at=attempted_at,
                        writer_attempted_at=attempted_at,
                    )
                    runtime.operation = operation
                    progress.record_attempt()
                    with deepcoin_client.request_scope(
                        _protected_request_scope(
                            session_factory,
                            runtime=runtime,
                            operation=operation,
                            phase="entry_submit",
                        )
                    ):
                        response = deepcoin_client.place_order(dict(order_payload))
                finally:
                    advance_account_write_generation(
                        session_factory,
                        uid_scope_hash=uid_scope_hash,
                    )
        except DeepcoinDefiniteRejection:
            if runtime.operation.writer_attempted_at is None:
                raise
            runtime.operation = _transition_protected_operation(
                session_factory,
                runtime.operation,
                phase="entry_submit",
                state="entry_rejected",
                certainty="rejected",
                error_category="business_rejected",
                reason_code="entry_submission_rejected",
                evidence={
                    "leg_index": leg_index,
                    "pre_submit_position_refs": sorted(
                        pre_submit_position_refs
                    ),
                },
                changed_at=datetime.now(UTC),
            )
            raise
        except DeepcoinRequestOutcomeUnknown:
            if runtime.operation.writer_attempted_at is None:
                raise
            runtime.operation = _transition_protected_operation(
                session_factory,
                runtime.operation,
                phase="entry_readback",
                state="entry_unknown",
                certainty="unknown",
                reason_code="entry_submission_unknown",
                evidence={
                    "leg_index": leg_index,
                    "pre_submit_position_refs": sorted(
                        pre_submit_position_refs
                    ),
                },
                changed_at=datetime.now(UTC),
            )
            raise
        except Exception as exc:
            if runtime.operation.writer_attempted_at is None:
                raise
            runtime.operation = _transition_protected_operation(
                session_factory,
                runtime.operation,
                phase="entry_readback",
                state="entry_unknown",
                certainty="unknown",
                reason_code="entry_submission_unknown",
                evidence={
                    "leg_index": leg_index,
                    "pre_submit_position_refs": sorted(
                        pre_submit_position_refs
                    ),
                },
                changed_at=datetime.now(UTC),
            )
            raise DeepcoinRequestOutcomeUnknown(
                "protected_entry_writer_outcome_unknown"
            ) from exc
        operation = _transition_protected_operation(
            session_factory,
            runtime.operation,
            phase="entry_readback",
            state="entry_pending_readback",
            certainty="accepted",
            reason_code="entry_submission_accepted",
            evidence={
                "leg_index": leg_index,
                "pre_submit_position_refs": sorted(
                    pre_submit_position_refs
                ),
            },
            changed_at=datetime.now(UTC),
        )
        runtime.operation = operation
    else:
        response = {}
        if operation.state not in {
            "entry_submitting",
            "entry_pending_readback",
            "entry_unknown",
            "entry_confirmed",
            "protection_prepared",
            "protected",
        }:
            raise RecoveryLiveSubmitError(
                "protected_entry_operation_state_conflict"
            )

    readback = _poll_protected_market_entry_readback(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        runtime=runtime,
        operation=runtime.operation,
        draft=draft,
        side=side,
        order_payload=order_payload,
        response=response,
        exclude_position_refs=pre_submit_position_refs,
    )
    if readback is None:
        raise DeepcoinRequestOutcomeUnknown(
            "protected_entry_readback_pending"
        )
    order_id, pos_id = readback
    if runtime.operation.state in {
        "entry_submitting",
        "entry_pending_readback",
        "entry_unknown",
    }:
        runtime.operation = _transition_protected_operation(
            session_factory,
            runtime.operation,
            phase="entry_readback",
            state="entry_confirmed",
            certainty="confirmed",
            reason_code="entry_readback_confirmed",
            evidence={
                "leg_index": leg_index,
                "order_ref": hashlib.sha256(
                    f"order:{order_id}".encode("utf-8")
                ).hexdigest(),
                "position_ref": hashlib.sha256(
                    f"position:{pos_id}".encode("utf-8")
                ).hexdigest(),
                "pre_submit_position_refs": sorted(
                    pre_submit_position_refs
                ),
            },
            changed_at=datetime.now(UTC),
        )
    progress.record_confirmed_leg()
    return (
        _safe_protected_market_response(
            response,
            order_id=order_id,
            pos_id=pos_id,
        ),
        order_id,
        pos_id,
        runtime,
    )


def _poll_protected_market_entry_readback(
    *,
    session_factory: sessionmaker,
    deepcoin_client: DeepcoinTradingClientProtocol,
    runtime: _ProtectedEntryRuntime,
    operation: ExecutionOperationRecord,
    draft: dict[str, Any],
    side: str,
    order_payload: Mapping[str, Any],
    response: Mapping[str, Any],
    exclude_position_refs: frozenset[str],
) -> tuple[str, str] | None:
    response_order_id = _extract_exact_market_order_id(dict(response))
    response_pos_id = _extract_position_id(dict(response))
    client_order_id = str(order_payload.get("clOrdId") or "")
    for delay in (0.0, 0.5, 1.0, 2.0, 3.0):
        remaining = runtime.deadline_monotonic - float(
            runtime.monotonic_factory()
        )
        if remaining <= 0 or delay >= remaining:
            break
        if delay:
            runtime.sleep_fn(delay)
        try:
            with deepcoin_client.request_scope(
                _protected_request_scope(
                    session_factory,
                    runtime=runtime,
                    operation=operation,
                    phase="entry_readback",
                )
            ):
                history = deepcoin_client.list_order_history(
                    inst_id=str(draft["instrument_id"])
                )
                positions = deepcoin_client.list_positions(
                    inst_id=str(draft["instrument_id"])
                )
        except Exception:
            continue
        matching_orders = [
            row
            for row in history
            if isinstance(row, Mapping)
            and _protected_entry_order_matches(
                row,
                instrument_id=str(draft["instrument_id"]),
                side=side,
                order_id=response_order_id,
                client_order_id=client_order_id,
            )
        ]
        if len(matching_orders) != 1:
            continue
        order_row = matching_orders[0]
        order_id = str(order_row.get("ordId") or "")
        preferred_pos_id = str(
            order_row.get("posId") or response_pos_id or ""
        )
        if not order_id or not preferred_pos_id:
            continue
        candidate_positions = [
            position
            for position in positions
            if (
                position_id := _first_payload_string(
                    position, "posId", "pos_id", "id"
                )
            )
            and _protected_position_ref(position_id)
            not in exclude_position_refs
        ]
        position = _select_matching_position(
            candidate_positions,
            draft=draft,
            side=side,
            preferred_pos_id=preferred_pos_id,
        )
        pos_id = (
            _first_payload_string(position, "posId", "pos_id", "id")
            if position is not None
            else None
        )
        if pos_id == preferred_pos_id:
            return order_id, pos_id
    return None


def _protected_entry_order_matches(
    row: Mapping[str, Any],
    *,
    instrument_id: str,
    side: str,
    order_id: str | None,
    client_order_id: str,
) -> bool:
    row_order_id = str(row.get("ordId") or "")
    row_client_id = str(row.get("clOrdId") or "")
    row_side = str(row.get("posSide") or "").lower()
    status = str(row.get("state") or row.get("status") or "").lower()
    return (
        bool(row_order_id)
        and bool(client_order_id)
        and row_client_id == client_order_id
        and (order_id is None or row_order_id == order_id)
        and str(row.get("instId") or "").upper() == instrument_id.upper()
        and row_side == side.lower()
        and status not in {"cancelled", "canceled", "rejected", "failed"}
    )


def _load_protected_entry_leg_id(
    session_factory: sessionmaker,
    *,
    binding_id: int,
    leg_index: int,
    pos_id: str,
) -> int:
    with session_factory() as session:
        rows = (
            session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.execution_binding_id == int(binding_id),
                ExecutionOrderLeg.leg_index == int(leg_index),
                ExecutionOrderLeg.purpose == "entry",
                ExecutionOrderLeg.pos_id == str(pos_id),
            )
            .all()
        )
        if len(rows) != 1:
            raise RecoveryLiveSubmitError(
                "protected_entry_execution_leg_missing"
            )
        return int(rows[0].id)


def _position_mutation_intent_status(
    session_factory: sessionmaker,
    intent_id: int | None,
) -> str | None:
    if type(intent_id) is not int or intent_id <= 0:
        return None
    with session_factory() as session:
        return session.query(PositionMutationIntent.status).filter(
            PositionMutationIntent.id == intent_id
        ).scalar()


def _confirmed_protection_response(
    session_factory: sessionmaker,
    *,
    child: ExecutionOperationRecord,
    intent_id: int,
    request_fingerprint: str,
    execution_binding_id: int,
    execution_order_leg_id: int,
    pos_id: str,
    payload: Mapping[str, Any],
    ledger_purpose: str,
) -> Mapping[str, Any] | None:
    """Return durable confirmed evidence for one completed protection child."""

    if (
        child.state != "protected"
        or child.outcome_certainty != "confirmed"
        or child.completed_at is None
        or child.request_fingerprint != request_fingerprint
    ):
        return None
    evidence = _operation_evidence(child)
    if evidence.get("position_mutation_intent_id") != intent_id:
        return None
    try:
        with session_factory() as session:
            intent = session.get(PositionMutationIntent, intent_id)
            if (
                intent is None
                or intent.operation != "set_position_sltp"
                or intent.status != "confirmed"
                or intent.confirmed_at is None
                or intent.execution_binding_id != execution_binding_id
                or intent.execution_order_leg_id != execution_order_leg_id
                or intent.pos_id != pos_id
                or intent.request_fingerprint != request_fingerprint
                or not isinstance(intent.request_json, str)
                or not isinstance(intent.response_json, str)
                or len(intent.request_json.encode("utf-8")) > 4096
                or len(intent.response_json.encode("utf-8")) > 4096
            ):
                return None
            request = load_validated_set_position_request(
                intent.request_json,
                request_fingerprint=request_fingerprint,
                authority_fingerprint=str(
                    intent.authority_fingerprint
                ),
                require_baseline=True,
            )
            response = json.loads(intent.response_json)
            if not isinstance(request, dict) or not isinstance(response, dict):
                return None
            persisted_purpose = request.get("_ledger_purpose")
            if persisted_purpose != ledger_purpose:
                return None
            response_data = response.get("data")
            candidates = (
                [*response_data, response]
                if isinstance(response_data, list)
                else [response_data, response]
            )
            order_ids = {
                str(candidate.get(key))
                for candidate in candidates
                if isinstance(candidate, Mapping)
                for key in ("ordId", "orderId", "order_id", "orderSysID")
                if candidate.get(key) not in (None, "")
            }
            if len(order_ids) != 1:
                return None
            order_id = next(iter(order_ids))
            ledger = (
                session.query(PositionProtectionLedger)
                .filter(
                    PositionProtectionLedger.venue == str(intent.venue).lower(),
                    PositionProtectionLedger.order_id == order_id,
                )
                .one_or_none()
            )
            trigger_field = (
                "slTriggerPx"
                if payload.get("slTriggerPx") not in (None, "")
                else "tpTriggerPx"
            )
            if (
                ledger is None
                or ledger.status != "verified"
                or ledger.execution_binding_id != execution_binding_id
                or ledger.execution_order_leg_id != execution_order_leg_id
                or ledger.pos_id != pos_id
                or ledger.purpose != ledger_purpose
                or ledger.instrument_id.upper()
                != str(request.get("instId") or "").upper()
                or ledger.side.lower()
                != str(request.get("posSide") or "").lower()
                or not _exact_decimal_equal(
                    ledger.trigger_price, request.get(trigger_field)
                )
                or not _optional_decimal_equal(
                    ledger.size_text, request.get("sz")
                )
            ):
                return None
            return response
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        RecursionError,
        TypeError,
        ValueError,
        InvalidOperation,
        PositionMutationIntentError,
    ):
        return None


def _exact_decimal_equal(left: Any, right: Any) -> bool:
    if left in (None, "") or right in (None, ""):
        return False
    left_value = Decimal(str(left))
    right_value = Decimal(str(right))
    return (
        left_value.is_finite()
        and right_value.is_finite()
        and left_value == right_value
    )


def _optional_decimal_equal(left: Any, right: Any) -> bool:
    if left in (None, "") and right in (None, ""):
        return True
    return _exact_decimal_equal(left, right)


def _submit_protected_entry_protections(
    *,
    session_factory: sessionmaker,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    parent: _ProtectedEntryRuntime,
    binding_id: int,
    leg_index: int,
    pos_id: str,
    payloads: list[dict[str, Any]],
    submitted_at: datetime,
) -> list[Mapping[str, Any]]:
    if not payloads:
        raise RecoveryLiveSubmitError(
            "protected_entry_protection_requirements_missing"
        )
    execution_leg_id = _load_protected_entry_leg_id(
        session_factory,
        binding_id=binding_id,
        leg_index=leg_index,
        pos_id=pos_id,
    )
    if parent.operation.state == "entry_confirmed":
        parent.operation = _transition_protected_operation(
            session_factory,
            parent.operation,
            phase="protection_submit",
            state="protection_prepared",
            certainty="confirmed",
            reason_code="protection_intents_prepared",
            evidence={
                "required_protection_count": len(payloads),
                "confirmed_protection_count": 0,
                "pre_submit_position_refs": sorted(
                    parent.pre_submit_position_refs
                ),
            },
            changed_at=datetime.now(UTC),
        )
    elif parent.operation.state == "protected":
        parent_evidence = _operation_evidence(parent.operation)
        if (
            parent.operation.outcome_certainty != "confirmed"
            or parent_evidence.get("required_protection_count")
            != len(payloads)
            or parent_evidence.get("confirmed_protection_count")
            != len(payloads)
            or parent_evidence.get("pre_submit_position_refs")
            != sorted(parent.pre_submit_position_refs)
        ):
            raise RecoveryLiveSubmitError(
                "protected_entry_parent_evidence_conflict"
            )
    elif parent.operation.state != "protection_prepared":
        raise RecoveryLiveSubmitError(
            "protected_entry_protection_state_conflict"
        )
    recording_client = _PendingTpslRecordingClient(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        uid_scope_hash=parent.uid_scope_hash,
    )
    prepared_intents = []
    for protection_index, payload in enumerate(payloads):
        idempotency_key = (
            f"protected-entry:{trade_signal.id}:{leg_index}:"
            f"set:{protection_index}"
        )
        with recording_client.request_scope(
            _protected_request_scope(
                session_factory,
                runtime=parent,
                operation=parent.operation,
                phase="protection_submit",
            )
        ):
            prepared_intents.append(
                prepare_exact_position_sltp_intent(
                    session_factory=session_factory,
                    deepcoin_client=recording_client,
                    pos_id=pos_id,
                    payload=payload,
                    idempotency_key=idempotency_key,
                    now_provider=lambda: submitted_at,
                    ledger_purpose=(
                        "stop_loss"
                        if protection_index == 0
                        else "backup_stop"
                    ),
                )
            )
    children = []
    for protection_index, (payload, prepared_intent) in enumerate(
        zip(payloads, prepared_intents, strict=True)
    ):
        children.append(
            reserve_execution_operation(
                session_factory,
                operation_key=_protected_entry_operation_key(
                    trade_signal.id,
                    leg_index,
                    protection_index=protection_index,
                ),
                trade_signal_id=trade_signal.id,
                parent_operation_id=parent.operation.id,
                execution_binding_id=binding_id,
                execution_order_leg_id=execution_leg_id,
                contract_version=PROTECTED_ENTRY_CONTRACT_VERSION,
                phase="protection_submit",
                state="protection_prepared",
                outcome_certainty="not_sent",
                request_fingerprint=prepared_intent.request_fingerprint,
                economics_fingerprint=_canonical_fingerprint(
                    {
                        "purpose": (
                            "stop_loss"
                            if protection_index == 0
                            else "backup_stop"
                        ),
                        "trigger_price": payload.get("slTriggerPx")
                        or payload.get("tpTriggerPx"),
                        "size": payload.get("sz"),
                        "pos_id": pos_id,
                    }
                ),
                deadline_at=parent.operation.deadline_at,
                evidence={
                    "protection_index": protection_index,
                    "required_protection_count": len(payloads),
                    "position_mutation_intent_id": (
                        prepared_intent.intent_id
                    ),
                },
                created_at=submitted_at,
            )
        )
    responses: list[Mapping[str, Any]] = []
    for protection_index, (payload, child, prepared_intent) in enumerate(
        zip(payloads, children, prepared_intents, strict=True)
    ):
        ledger_purpose = (
            "stop_loss" if protection_index == 0 else "backup_stop"
        )
        confirmed_response = _confirmed_protection_response(
            session_factory,
            child=child,
            intent_id=prepared_intent.intent_id,
            request_fingerprint=prepared_intent.request_fingerprint,
            execution_binding_id=binding_id,
            execution_order_leg_id=execution_leg_id,
            pos_id=pos_id,
            payload=payload,
            ledger_purpose=ledger_purpose,
        )
        if confirmed_response is not None:
            responses.append(confirmed_response)
            continue
        child_holder = {
            "operation": child,
            "intent_id": prepared_intent.intent_id,
        }

        def before_exchange_submit(intent_id: int) -> None:
            operation = child_holder["operation"]
            child_holder["intent_id"] = int(intent_id)
            child_holder["operation"] = _transition_protected_operation(
                session_factory,
                operation,
                phase="protection_submit",
                state="protection_prepared",
                certainty="not_sent",
                reason_code="protection_submit_authorized",
                evidence={
                    "protection_index": protection_index,
                    "position_mutation_intent_id": int(intent_id),
                },
                changed_at=datetime.now(UTC),
                writer_attempted_at=datetime.now(UTC),
            )

        try:
            submit_scope = _protected_request_scope(
                session_factory,
                runtime=parent,
                operation=child,
                phase="protection_submit",
            )
            readback_scope = _protected_request_scope(
                session_factory,
                runtime=parent,
                operation=child,
                phase="protection_readback",
            )
            with recording_client.request_scope(submit_scope):
                response = submit_exact_position_sltp(
                    session_factory=session_factory,
                    deepcoin_client=recording_client,
                    pos_id=pos_id,
                    payload=payload,
                    idempotency_key=(
                        f"protected-entry:{trade_signal.id}:{leg_index}:"
                        f"set:{protection_index}"
                    ),
                    expected_intent_id=prepared_intent.intent_id,
                    live_execution_gate=lambda: (
                        _protected_entry_writer_allowed(
                            session_factory,
                            trade_signal_id=trade_signal.id,
                        )
                        and exact_position_write_gate(
                            session_factory,
                            pos_id=pos_id,
                        )
                    ),
                    now_provider=lambda: submitted_at,
                    require_readback=True,
                        ledger_purpose=ledger_purpose,
                    before_exchange_submit=before_exchange_submit,
                    readback_deadline_monotonic=parent.deadline_monotonic,
                    monotonic_factory=parent.monotonic_factory,
                    sleep_fn=parent.sleep_fn,
                    readback_scope=readback_scope,
                    before_exchange_write=lambda: (
                        advance_account_write_generation(
                            session_factory,
                            uid_scope_hash=parent.uid_scope_hash,
                        )
                    ),
                    after_exchange_write=lambda: (
                        advance_account_write_generation(
                            session_factory,
                            uid_scope_hash=parent.uid_scope_hash,
                        )
                    ),
                    require_complete_readback_identity=True,
                )
            child = child_holder["operation"]
            child = _transition_protected_operation(
                session_factory,
                child,
                phase="protection_readback",
                state="protection_pending_readback",
                certainty="accepted",
                reason_code="protection_submission_accepted",
                evidence={
                    "protection_index": protection_index,
                    "position_mutation_intent_id": child_holder[
                        "intent_id"
                    ],
                },
                changed_at=datetime.now(UTC),
            )
            child = _transition_protected_operation(
                session_factory,
                child,
                phase="protection_readback",
                state="protected",
                certainty="confirmed",
                reason_code="protection_fully_confirmed",
                evidence={
                    "protection_index": protection_index,
                    "position_mutation_intent_id": child_holder[
                        "intent_id"
                    ],
                },
                changed_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            children[protection_index] = child
            responses.append(response)
        except DeepcoinDefiniteRejection:
            child = child_holder["operation"]
            if child.writer_attempted_at is not None:
                children[protection_index] = _transition_protected_operation(
                    session_factory,
                    child,
                    phase="protection_readback",
                    state="recovery_required",
                    certainty="rejected",
                    error_category="business_rejected",
                    reason_code="protection_submission_rejected",
                    evidence={
                        "protection_index": protection_index,
                        "position_mutation_intent_id": child_holder[
                            "intent_id"
                        ],
                    },
                    changed_at=datetime.now(UTC),
                )
            _mark_parent_protection_recovery(
                session_factory,
                parent=parent,
                reason_code="protection_submission_rejected",
            )
            raise
        except Exception:
            child = child_holder["operation"]
            if child.writer_attempted_at is not None:
                accepted_pending = (
                    _position_mutation_intent_status(
                        session_factory,
                        child_holder["intent_id"],
                    )
                    == "submitted"
                )
                children[protection_index] = _transition_protected_operation(
                    session_factory,
                    child,
                    phase="protection_readback",
                    state=(
                        "protection_pending_readback"
                        if accepted_pending
                        else "protection_unknown"
                    ),
                    certainty="accepted" if accepted_pending else "unknown",
                    reason_code=(
                        "protection_readback_pending"
                        if accepted_pending
                        else "protection_submission_unknown"
                    ),
                    evidence={
                        "protection_index": protection_index,
                        "position_mutation_intent_id": child_holder[
                            "intent_id"
                        ],
                    },
                    changed_at=datetime.now(UTC),
                )
            _mark_parent_protection_recovery(
                session_factory,
                parent=parent,
                reason_code="protection_incomplete",
            )
            raise
    if parent.operation.state != "protected":
        parent.operation = _transition_protected_operation(
            session_factory,
            parent.operation,
            phase="protection_readback",
            state="protected",
            certainty="confirmed",
            reason_code="protection_fully_confirmed",
            evidence={
                "required_protection_count": len(children),
                "confirmed_protection_count": len(children),
                "pre_submit_position_refs": sorted(
                    parent.pre_submit_position_refs
                ),
            },
            changed_at=datetime.now(UTC),
        )
    capture = recording_client.latest_capture
    if capture is not None:
        final_generation = _current_write_generation(
            session_factory,
            uid_scope_hash=parent.uid_scope_hash,
        )
        expected_local_writer_close = (
            capture.start_write_generation % 2 == 1
            and final_generation == capture.start_write_generation + 1
        )
        capture = _PendingTpslCapture(
            collection=capture.collection,
            rows=capture.rows,
            start_write_generation=(
                final_generation
                if expected_local_writer_close
                else capture.start_write_generation
            ),
            end_write_generation=final_generation,
            capture_started_at=capture.capture_started_at,
            capture_ended_at=datetime.now(UTC),
        )
        if (
            capture.start_write_generation != capture.end_write_generation
            or capture.end_write_generation % 2 != 0
        ):
            capture = _PendingTpslCapture(
                collection=ExchangeCollectionEvidence(
                    endpoint=capture.collection.endpoint,
                    available=capture.collection.available,
                    schema_valid=capture.collection.schema_valid,
                    complete=False,
                    rows=capture.collection.rows,
                    row_count=capture.collection.row_count,
                    page_count=capture.collection.page_count,
                    fingerprint=capture.collection.fingerprint,
                    reason_code="snapshot_write_generation_changed",
                ),
                rows=capture.rows,
                start_write_generation=capture.start_write_generation,
                end_write_generation=capture.end_write_generation,
                capture_started_at=capture.capture_started_at,
                capture_ended_at=capture.capture_ended_at,
            )
        parent.latest_protection_capture = capture
        _record_pending_tpsl_snapshot(
            session_factory,
            operation=parent.operation,
            capture=capture,
            source="protection_readback",
            reused=False,
        )
    return responses


def _load_confirmed_protection_responses(
    session_factory: sessionmaker,
    *,
    parent: _ProtectedEntryRuntime,
    expected_count: int,
) -> list[Mapping[str, Any]]:
    with session_factory() as session:
        children = (
            session.query(DeepcoinExecutionOperation)
            .filter(
                DeepcoinExecutionOperation.parent_operation_id
                == parent.operation.id,
                DeepcoinExecutionOperation.phase
                == "protection_readback",
            )
            .order_by(DeepcoinExecutionOperation.id)
            .all()
        )
        if (
            len(children) != expected_count
            or any(
                child.state != "protected"
                or child.outcome_certainty != "confirmed"
                for child in children
            )
        ):
            raise RecoveryLiveSubmitError(
                "protected_entry_protection_evidence_conflict"
            )
        responses: list[Mapping[str, Any]] = []
        for child in children:
            child_record = load_operation_bundle(
                session_factory,
                operation_id=int(child.id),
            ).operation
            evidence = _operation_evidence(child_record)
            intent_id = evidence.get("position_mutation_intent_id")
            if type(intent_id) is not int:
                raise RecoveryLiveSubmitError(
                    "protected_entry_protection_evidence_conflict"
                )
            intent = session.get(PositionMutationIntent, intent_id)
            order_id = str(intent.order_id or "") if intent else ""
            if (
                intent is None
                or intent.status != "confirmed"
                or intent.request_fingerprint
                != child.request_fingerprint
                or not order_id
                or not _safe_protected_exchange_identity(order_id)
            ):
                raise RecoveryLiveSubmitError(
                    "protected_entry_protection_evidence_conflict"
                )
            ledgers = (
                session.query(PositionProtectionLedger)
                .filter(
                    PositionProtectionLedger.venue == "deepcoin",
                    PositionProtectionLedger.order_id == order_id,
                    PositionProtectionLedger.execution_binding_id
                    == child.execution_binding_id,
                    PositionProtectionLedger.execution_order_leg_id
                    == child.execution_order_leg_id,
                    PositionProtectionLedger.status == "verified",
                )
                .all()
            )
            if len(ledgers) != 1:
                raise RecoveryLiveSubmitError(
                    "protected_entry_protection_evidence_conflict"
                )
            responses.append(
                {"code": "0", "data": {"ordId": order_id}}
            )
        return responses


def _mark_parent_protection_recovery(
    session_factory: sessionmaker,
    *,
    parent: _ProtectedEntryRuntime,
    reason_code: str,
) -> None:
    if parent.operation.state == "recovery_required":
        return
    parent.operation = _transition_protected_operation(
        session_factory,
        parent.operation,
        phase="protection_readback",
        state="recovery_required",
        certainty="unknown",
        reason_code=reason_code,
        evidence={
            "next_action": "supervision_only",
            "pre_submit_position_refs": sorted(
                parent.pre_submit_position_refs
            ),
        },
        changed_at=datetime.now(UTC),
    )


@serialized_source_message_execution
@_report_entry_submission_progress
def _submit_recovery_signal_direct(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    submitted_at: datetime | None = None,
    max_order_legs: int | None = None,
    validated_draft: dict[str, Any] | None = None,
    verified_v2_assembly: bool = False,
    submission_progress: EntrySubmissionProgress | None = None,
) -> dict[str, Any]:
    progress = submission_progress or EntrySubmissionProgress()
    if (
        validated_draft is None
        and _has_protected_entry_parent_operation(
            session_factory,
            trade_signal_id=trade_signal.id,
        )
        and isinstance(trade_signal.payload, dict)
        and isinstance(
            trade_signal.payload.get("deepcoin_order_draft"), dict
        )
    ):
        validated_draft = dict(
            trade_signal.payload["deepcoin_order_draft"]
        )
    if validated_draft is None:
        gate = validate_recovery_live_submit_gate(
            session_factory,
            chat_id=trade_signal.chat_id,
            message_id=trade_signal.message_id,
            symbol=trade_signal.symbol,
            side=trade_signal.side,
            contract_spec_provider=contract_spec_provider,
        )
        if not gate["would_submit"]:
            raise RecoveryLiveSubmitError(
                "live_submit_blocked:"
                + ",".join(str(code) for code in gate["reason_codes"])
            )
        draft = gate["deepcoin_order_draft"]
        if not isinstance(draft, dict):
            raise RecoveryLiveSubmitError("missing_deepcoin_order_draft")
    else:
        draft = validated_draft
    queued_draft = (
        trade_signal.payload.get("deepcoin_order_draft")
        if isinstance(trade_signal.payload, dict)
        else None
    )
    if isinstance(queued_draft, dict):
        _require_current_contract_spec_matches_queued(
            current_draft=draft,
            queued_draft=queued_draft,
        )
        draft = queued_draft
    order_legs = draft.get("order_legs")
    if not isinstance(order_legs, list) or not order_legs:
        raise RecoveryLiveSubmitError("missing_order_legs")

    submitted_orders: list[dict[str, Any]] = []
    now = submitted_at or datetime.now(UTC)
    source = draft.get("source") if isinstance(draft.get("source"), dict) else {}
    kol_id = str(source.get("kol_id") or "unknown")
    symbol_key = str(draft.get("symbol") or trade_signal.symbol).upper()
    side_key = trade_signal.side.lower()
    warnings = _protection_warnings(draft)
    protected_access = _protected_entry_route_access(
        session_factory,
        trade_signal_id=trade_signal.id,
    )
    protected_v1 = protected_access in {"live", "readback_only"}
    protected_parent: _ProtectedEntryRuntime | None = None

    revision_indices = draft.get("authorized_leg_indices")
    if isinstance(revision_indices, list):
        selected_indices = revision_indices
        if (
            not selected_indices
            or any(
                type(index) is not int or index < 1 or index > len(order_legs)
                for index in selected_indices
            )
            or len(set(selected_indices)) != len(selected_indices)
        ):
            raise RecoveryLiveSubmitError("invalid_authorized_leg_indices")
        selected_order_legs = [order_legs[index - 1] for index in selected_indices]
    elif verified_v2_assembly:
        selected_indices = draft.get("selected_entry_leg_indices")
        if not isinstance(selected_indices, list):
            raise RecoveryLiveSubmitError("invalid_selected_entry_leg_indices")
        selected_order_legs = [order_legs[index - 1] for index in selected_indices]
    else:
        selected_order_legs = order_legs[:max_order_legs] if max_order_legs else order_legs
        selected_indices = list(range(1, len(selected_order_legs) + 1))
    submission_order_legs = _submission_order_legs(draft, selected_order_legs)
    submission_indices = _submission_source_leg_indices(
        selected_indices=selected_indices,
        submission_order_legs=submission_order_legs,
    )
    if protected_v1:
        first_leg = submission_order_legs[0]
        if (
            not isinstance(first_leg, Mapping)
            or str(first_leg.get("order_type") or "").lower() != "market"
        ):
            raise RecoveryLiveSubmitError(
                "protected_entry_market_first_leg_required"
            )
        _require_protected_entry_client(deepcoin_client)
    leg_index_offset = int(draft.get("_entry_leg_index_offset") or 0)
    expected_entry_leg_indices = tuple(
        leg_index_offset + int(source_index)
        for source_index in submission_indices
    )
    for source_index, leg in zip(
        submission_indices,
        submission_order_legs,
        strict=True,
    ):
        index = leg_index_offset + int(source_index)
        if not isinstance(leg, dict):
            raise RecoveryLiveSubmitError("invalid_order_leg")
        if (
            protected_v1
            and source_index != submission_indices[0]
            and (
                protected_parent is None
                or protected_parent.operation.state != "protected"
            )
        ):
            raise RecoveryLiveSubmitError(
                "protected_entry_protection_gate_closed"
            )
        order_type = str(leg.get("order_type") or "").lower()
        if order_type == "market":
            order_payload = build_deepcoin_market_order_payload(draft, leg)
            if protected_v1:
                if protected_parent is not None:
                    raise RecoveryLiveSubmitError(
                        "protected_entry_later_market_leg_requires_preflight"
                    )
                (
                    response,
                    order_id,
                    pos_id,
                    protected_parent,
                ) = _submit_protected_market_entry(
                    session_factory=session_factory,
                    trade_signal=trade_signal,
                    deepcoin_client=deepcoin_client,
                    draft=draft,
                    leg=leg,
                    leg_index=index,
                    side=side_key,
                    source=source,
                    order_payload=order_payload,
                    progress=progress,
                    submitted_at=now,
                    expected_entry_leg_indices=expected_entry_leg_indices,
                )
            else:
                pre_submit_position_ids = _load_matching_position_ids(
                    deepcoin_client,
                    draft=draft,
                    side=side_key,
                )
                try:
                    with _entry_source_exchange_write_gate(
                        session_factory,
                        trade_signal=trade_signal,
                        source=source,
                    ):
                        progress.record_attempt()
                        response = deepcoin_client.place_order(order_payload)
                except DeepcoinClientError:
                    raise
                except Exception as exc:  # pragma: no cover - defensive boundary
                    raise DeepcoinClientError(f"Deepcoin client failed: {exc}") from exc

                order_id = _extract_exact_market_order_id(response)
                if not order_id:
                    raise DeepcoinRequestOutcomeUnknown(
                        "market order response missing exact order id"
                    )
                progress.record_confirmed_leg()
                pos_id = _extract_position_id(response) or _find_open_position_id(
                    deepcoin_client,
                    draft=draft,
                    side=side_key,
                    exclude_pos_ids=pre_submit_position_ids,
                )
            client_order_id = str(
                leg.get("client_order_id")
                or order_payload.get("clOrdId")
                or ""
            )
            provisional_order = {
                "leg_index": index,
                "execution_type": "market",
                "client_order_id": client_order_id,
                "order_id": order_id,
                "pos_id": pos_id,
                "request": _persisted_order_request(order_payload, leg),
                "response": response,
            }
            provisional_binding_id = _upsert_protection_failed_binding(
                session_factory,
                trade_signal=trade_signal,
                draft=draft,
                source=source,
                kol_id=kol_id,
                symbol_key=symbol_key,
                side_key=side_key,
                order=provisional_order,
            )
            _record_submitted_order_legs(
                session_factory,
                binding_id=provisional_binding_id,
                strategy_instance_id=str(
                    draft.get("strategy_instance_id")
                    or trade_signal.strategy_instance_id
                    or ""
                ),
                submitted_orders=[provisional_order],
            )
            if (
                protected_access == "readback_only"
                and protected_parent is not None
                and protected_parent.operation.state == "entry_confirmed"
            ):
                _mark_parent_protection_recovery(
                    session_factory,
                    parent=protected_parent,
                    reason_code="protected_entry_rollout_disabled",
                )
                raise RecoveryLiveSubmitError(
                    "protected_entry_readback_only"
                )
            try:
                protection_payloads = build_deepcoin_position_sltp_payloads(
                    draft,
                    pos_id=pos_id,
                    position_size=float(leg.get("quantity") or 0),
                    include_take_profit=False,
                )
                if protected_v1:
                    if protected_parent is None:
                        raise RecoveryLiveSubmitError(
                            "protected_entry_parent_operation_missing"
                        )
                    if protected_parent.operation.state == "protected":
                        protection_responses = (
                            _load_confirmed_protection_responses(
                                session_factory,
                                parent=protected_parent,
                                expected_count=len(protection_payloads),
                            )
                        )
                    else:
                        protection_responses = _submit_protected_entry_protections(
                            session_factory=session_factory,
                            trade_signal=trade_signal,
                            deepcoin_client=deepcoin_client,
                            parent=protected_parent,
                            binding_id=provisional_binding_id,
                            leg_index=index,
                            pos_id=str(pos_id),
                            payloads=protection_payloads,
                            submitted_at=now,
                        )
                else:
                    protection_responses = []
                    for protection_index, payload in enumerate(protection_payloads):
                        protection_responses.append(
                            submit_exact_position_sltp(
                                session_factory=session_factory,
                                deepcoin_client=deepcoin_client,
                                pos_id=str(pos_id),
                                payload=payload,
                                idempotency_key=(
                                    f"recovery:{trade_signal.id}:{index}:set:"
                                    f"{protection_index}"
                                ),
                                live_execution_gate=lambda: exact_position_write_gate(
                                    session_factory, pos_id=str(pos_id)
                                ),
                                now_provider=lambda: now,
                                require_readback=True,
                            )
                        )
                protection_payload = (
                    protection_payloads[0] if len(protection_payloads) == 1 else protection_payloads
                )
                protection_response = (
                    protection_responses[0]
                    if len(protection_responses) == 1
                    else protection_responses
                )
                if (
                    protected_access == "readback_only"
                    and not _any_later_leg_readback_operation_exists(
                        session_factory,
                        trade_signal_id=trade_signal.id,
                    )
                ):
                    raise RecoveryLiveSubmitError(
                        "protected_entry_readback_only"
                    )
            except Exception as exc:  # pragma: no cover - defensive boundary
                if protected_v1:
                    raise
                protection_payload = locals().get("protection_payloads") or locals().get("protection_payload")
                protection_response = {"error": str(exc)}
                warnings.append("position_protection_failed_after_entry_submitted")
        elif order_type == "limit":
            if (
                protected_access == "readback_only"
                and not _later_leg_readback_operation_exists(
                    session_factory,
                    trade_signal_id=trade_signal.id,
                    leg_index=index,
                )
            ):
                raise RecoveryLiveSubmitError(
                    "protected_entry_readback_only"
                )
            order_payload = build_deepcoin_trigger_order_payload(draft, leg)
            try:
                with _entry_source_exchange_write_gate(
                    session_factory,
                    trade_signal=trade_signal,
                    source=source,
                ):
                    if _has_embedded_trigger_protection(order_payload):
                        response = _submit_trigger_with_protection_intent(
                            session_factory,
                            deepcoin_client=deepcoin_client,
                            trade_signal=trade_signal,
                            draft=draft,
                            leg=leg,
                            leg_index=index,
                            binding_context={
                                "kol_id": kol_id,
                                "symbol": symbol_key,
                                "side": side_key,
                                "source": source,
                            },
                            order_payload=order_payload,
                            submission_progress=progress,
                            parent=protected_parent if protected_v1 else None,
                        )
                    else:
                        progress.record_attempt()
                        response = deepcoin_client.trigger_order(order_payload)
            except DeepcoinClientError:
                raise
            except RecoveryLiveSubmitError:
                raise
            except Exception as exc:  # pragma: no cover - defensive boundary
                raise DeepcoinClientError(f"Deepcoin client failed: {exc}") from exc

            order_id = _normalized_trigger_order_id(response)
            progress.record_confirmed_leg()
            pos_id = _extract_position_id(response)
            client_order_id = str(leg.get("client_order_id") or "")
            protection_payload = {
                key: order_payload[key]
                for key in ("tpTriggerPx", "slTriggerPx", "tpOrdPx", "slOrdPx")
                if key in order_payload
            }
            protection_response = {"code": "0", "data": {"attached_on_trigger_order": True}}
            order_type = "trigger_limit"
        else:
            if protected_access == "readback_only":
                raise RecoveryLiveSubmitError(
                    "protected_entry_readback_only"
                )
            order_payload = build_deepcoin_trigger_order_payload(draft, leg)
            try:
                with _entry_source_exchange_write_gate(
                    session_factory,
                    trade_signal=trade_signal,
                    source=source,
                ):
                    progress.record_attempt()
                    response = deepcoin_client.trigger_order(order_payload)
            except DeepcoinClientError:
                raise
            except Exception as exc:  # pragma: no cover - defensive boundary
                raise DeepcoinClientError(f"Deepcoin client failed: {exc}") from exc

            order_id = _extract_exact_trigger_order_id(response)
            if not order_id:
                raise DeepcoinRequestOutcomeUnknown(
                    "trigger order response missing exact order id"
                )
            progress.record_confirmed_leg()
            pos_id = _extract_position_id(response)
            client_order_id = str(leg.get("client_order_id") or "")
            protection_payload = {
                key: order_payload[key]
                for key in ("tpTriggerPx", "slTriggerPx", "tpOrdPx", "slOrdPx")
                if key in order_payload
            }
            protection_response = {"code": "0", "data": {"attached_on_trigger_order": True}}
        submitted_orders.append(
            {
                "leg_index": index,
                "execution_type": order_type or "limit",
                "client_order_id": client_order_id,
                "order_id": order_id,
                "pos_id": pos_id,
                "request": _persisted_order_request(order_payload, leg),
                "response": response,
                "protection_request": protection_payload,
                "protection_response": protection_response,
            }
        )

    protection_failed = any(
        _protection_response_has_error(order.get("protection_response"))
        for order in submitted_orders
    )
    binding_status = "active" if _join_ids(order["pos_id"] for order in submitted_orders) else "open"
    last_exchange_status = (
        "position_active_protection_failed"
        if protection_failed and binding_status == "active"
        else "order_open_protection_failed" if protection_failed else "submitted"
    )
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id=kol_id,
            chat_id=int(source.get("chat_id") or trade_signal.chat_id),
            message_id=int(source.get("message_id") or trade_signal.message_id),
            symbol=symbol_key,
            side=side_key,
            venue="deepcoin",
            order_id=_join_ids(order["order_id"] for order in submitted_orders),
            client_order_id=_join_ids(order["client_order_id"] for order in submitted_orders),
            pos_id=_join_ids(order["pos_id"] for order in submitted_orders),
            margin_mode=str(draft.get("margin_mode") or "cross"),
            position_mode=str(draft.get("position_mode") or "split"),
            payload={"draft": draft, "submitted_orders": submitted_orders},
            last_exchange_status=last_exchange_status,
            status=binding_status,
            strategy_instance_id=str(draft.get("strategy_instance_id") or ""),
        ),
    )
    _record_submitted_order_legs(
        session_factory,
        binding_id=binding_id,
        strategy_instance_id=str(draft.get("strategy_instance_id") or trade_signal.strategy_instance_id or ""),
        submitted_orders=submitted_orders,
    )
    _record_market_take_profit_convergences(
        session_factory,
        binding_id=binding_id,
        draft=draft,
        submitted_orders=submitted_orders,
        created_at=now,
    )
    if not protected_v1:
        _record_entry_protection_ledger_rows(
            session_factory,
            binding_id=binding_id,
            draft=draft,
            submitted_orders=submitted_orders,
            side=side_key,
            deepcoin_client=deepcoin_client,
            seen_at=now,
        )
    _attach_lifecycle_binding(
        session_factory,
        chat_id=int(source.get("chat_id") or trade_signal.chat_id),
        message_id=int(source.get("message_id") or trade_signal.message_id),
        symbol=symbol_key,
        side=side_key,
        binding_id=binding_id,
        entered=bool(_join_ids(order["pos_id"] for order in submitted_orders)),
        updated_at=now,
    )
    _record_submitted_order_events(
        session_factory,
        trade_signal=trade_signal,
        binding_id=binding_id,
        draft=draft,
        submitted_orders=submitted_orders,
        kol_id=kol_id,
        symbol_key=symbol_key,
        side_key=side_key,
        source=source,
        created_at=now,
    )

    return {
        "submitted": True,
        "venue": "deepcoin",
        "signal_id": trade_signal.id,
        "signal_uid": trade_signal.signal_uid,
        "submitted_at": now.isoformat(),
        "source": {
            "chat_id": trade_signal.chat_id,
            "message_id": trade_signal.message_id,
            "symbol": symbol_key,
            "side": side_key,
        },
        "order_count": len(submitted_orders),
        "orders": submitted_orders,
        "deepcoin_order_draft": draft,
        "warnings": warnings,
    }


def _submission_order_legs(
    draft: dict[str, Any],
    order_legs: list[Any],
) -> list[dict[str, Any]]:
    copied_legs = [dict(leg) if isinstance(leg, dict) else leg for leg in order_legs]
    if not all(isinstance(leg, dict) for leg in copied_legs):
        return copied_legs
    return _coalesce_equivalent_entry_legs(copied_legs)


def _submission_source_leg_indices(
    *,
    selected_indices: list[int],
    submission_order_legs: list[Any],
) -> list[int]:
    """Map coalesced local leg positions back to original draft indices."""

    mapped: list[int] = []
    for position, leg in enumerate(submission_order_legs, start=1):
        local_positions = (
            leg.get("merged_from_leg_indices")
            if isinstance(leg, dict)
            else None
        )
        local_position = (
            min(local_positions)
            if isinstance(local_positions, list) and local_positions
            else position
        )
        if type(local_position) is not int or not (1 <= local_position <= len(selected_indices)):
            raise RecoveryLiveSubmitError("invalid_coalesced_entry_leg_indices")
        mapped.append(int(selected_indices[local_position - 1]))
    return mapped


def _has_embedded_trigger_protection(order_payload: dict[str, Any]) -> bool:
    return any(
        order_payload.get(key) not in (None, "")
        for key in ("tpTriggerPx", "slTriggerPx")
    )


def _prepare_trigger_protection_intent(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    draft: dict[str, Any],
    leg: dict[str, Any],
    leg_index: int,
    binding_context: dict[str, Any],
    order_payload: dict[str, Any],
) -> int:
    """Create the local entry leg needed to durably identify the intent."""

    source = binding_context["source"]
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id=str(binding_context["kol_id"]),
            chat_id=int(source.get("chat_id") or trade_signal.chat_id),
            message_id=int(source.get("message_id") or trade_signal.message_id),
            symbol=str(binding_context["symbol"]),
            side=str(binding_context["side"]),
            venue="deepcoin",
            margin_mode=str(draft.get("margin_mode") or "cross"),
            position_mode=str(draft.get("position_mode") or "split"),
            payload={"draft": draft},
            last_exchange_status="submitting",
            status="open",
            strategy_instance_id=str(draft.get("strategy_instance_id") or ""),
        ),
    )
    return upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id=str(draft.get("strategy_instance_id") or trade_signal.strategy_instance_id or ""),
            leg_index=leg_index,
            purpose="entry",
            order_kind="trigger_limit",
            client_order_id=str(leg.get("client_order_id") or order_payload.get("clOrdId") or "") or None,
            status="submitting",
            request=_persisted_order_request(order_payload, leg),
        ),
    )


def _reserve_next_leg_operation(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    parent: _ProtectedEntryRuntime,
    execution_order_leg_id: int,
    leg_index: int,
    order_payload: Mapping[str, Any],
) -> ExecutionOperationRecord:
    with session_factory() as session:
        entry_leg = session.get(ExecutionOrderLeg, execution_order_leg_id)
        if entry_leg is None:
            raise RecoveryLiveSubmitError("trigger_protection_entry_leg_missing")
        binding_id = int(entry_leg.execution_binding_id)
    return reserve_execution_operation(
        session_factory,
        operation_key=_protected_entry_operation_key(
            trade_signal.id,
            leg_index,
        ),
        trade_signal_id=trade_signal.id,
        parent_operation_id=parent.operation.id,
        execution_binding_id=binding_id,
        execution_order_leg_id=execution_order_leg_id,
        contract_version=PROTECTED_ENTRY_CONTRACT_VERSION,
        phase="next_leg_preflight",
        state="next_leg_preflight",
        outcome_certainty="not_sent",
        request_fingerprint=_trigger_protection_request_fingerprint(
            dict(order_payload)
        ),
        economics_fingerprint=_canonical_fingerprint(
            {
                "instrument_id": order_payload.get("instId"),
                "position_side": order_payload.get("posSide"),
                "quantity": order_payload.get("sz"),
                "price": order_payload.get("price"),
                "trigger_price": order_payload.get("triggerPrice"),
                "client_order_id": order_payload.get("clOrdId"),
                "leg_index": leg_index,
            }
        ),
        deadline_at=parent.operation.deadline_at,
        evidence={
            "leg_index": leg_index,
            "parent_operation_ref": hashlib.sha256(
                f"operation:{parent.operation.id}".encode("utf-8")
            ).hexdigest(),
            "writer_attempted": False,
        },
        created_at=parent.operation.created_at,
    )


def _normalized_pending_tpsl_rows(
    rows: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> str:
    normalized: list[dict[str, str | None]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise RecoveryLiveSubmitError(
                "trigger_protection_baseline_malformed"
            )
        mutable = dict(row)
        if str(mutable.get("triggerOrderType") or "").upper() != "TPSL":
            continue
        normalized_row = _normalized_tpsl_row(mutable)
        order_id = normalized_row["ord_id"] or ""
        instrument = normalized_row["instrument"] or ""
        side = normalized_row["side"] or ""
        if (
            not _safe_protected_exchange_identity(order_id)
            or not re.fullmatch(r"[A-Z0-9-]{1,64}", instrument)
            or side not in {"long", "short"}
        ):
            raise RecoveryLiveSubmitError(
                "trigger_protection_baseline_malformed"
            )
        for key in (
            "size",
            "take_profit_trigger_price",
            "stop_loss_trigger_price",
        ):
            value = normalized_row[key]
            if value is None:
                continue
            try:
                decimal_value = Decimal(value)
            except (InvalidOperation, ValueError):
                raise RecoveryLiveSubmitError(
                    "trigger_protection_baseline_malformed"
                )
            if not decimal_value.is_finite() or len(value) > 64:
                raise RecoveryLiveSubmitError(
                    "trigger_protection_baseline_malformed"
                )
        for key in ("exchange_created_at", "exchange_updated_at"):
            value = normalized_row[key]
            if value is not None and (
                len(value) > 64 or contains_credential_marker(value)
            ):
                raise RecoveryLiveSubmitError(
                    "trigger_protection_baseline_malformed"
                )
        normalized.append(normalized_row)
    normalized.sort(
        key=lambda row: (
            row["ord_id"] or "",
            row["exchange_created_at"] or "",
        )
    )
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _complete_reusable_protection_capture(
    session_factory: sessionmaker,
    *,
    parent: _ProtectedEntryRuntime,
) -> _PendingTpslCapture | None:
    capture = parent.latest_protection_capture
    if capture is None:
        bundle = load_operation_bundle(
            session_factory,
            operation_id=parent.operation.id,
        )
        current_generation = _current_write_generation(
            session_factory,
            uid_scope_hash=parent.uid_scope_hash,
        )
        for snapshot in reversed(bundle.snapshots):
            if (
                snapshot.snapshot_kind != "protection_pending"
                or not snapshot.complete
                or snapshot.collection_fingerprint is None
                or snapshot.start_write_generation
                != snapshot.end_write_generation
                or snapshot.end_write_generation != current_generation
                or snapshot.end_write_generation % 2 != 0
            ):
                continue
            baseline_json = _snapshot_baseline_json(
                snapshot.evidence_json
            )
            if baseline_json is None:
                continue
            collection = ExchangeCollectionEvidence(
                endpoint="pending_trigger_orders",
                available=snapshot.available,
                schema_valid=snapshot.schema_valid,
                complete=snapshot.complete,
                rows=(),
                row_count=snapshot.row_count,
                page_count=snapshot.page_count,
                fingerprint=snapshot.collection_fingerprint,
                reason_code=None,
            )
            capture = _PendingTpslCapture(
                collection=collection,
                rows=(),
                start_write_generation=snapshot.start_write_generation,
                end_write_generation=snapshot.end_write_generation,
                capture_started_at=snapshot.capture_started_at,
                capture_ended_at=snapshot.capture_ended_at,
                normalized_baseline_json=baseline_json,
            )
            break
    if capture is None or not capture.collection.complete:
        return None
    if (
        capture.end_write_generation % 2 != 0
        or capture.end_write_generation
        != _current_write_generation(
            session_factory,
            uid_scope_hash=parent.uid_scope_hash,
        )
        or capture.capture_ended_at.replace(tzinfo=None)
        < parent.operation.updated_at
    ):
        return None
    return capture


def _snapshot_baseline_json(evidence_json: str) -> str | None:
    try:
        if not isinstance(evidence_json, str) or len(evidence_json) > 4096:
            return None
        evidence = json.loads(evidence_json)
        if not isinstance(evidence, dict):
            return None
        encoded = evidence.get("baseline_deflate_b64")
        expected = evidence.get("baseline_fingerprint")
        if (
            not isinstance(encoded, str)
            or len(encoded) > 3000
            or not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
        ):
            return None
        compressed = base64.b64decode(encoded, validate=True)
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(compressed, 65_537)
        if (
            len(decoded) > 65_536
            or not decompressor.eof
            or decompressor.unused_data
        ):
            return None
        baseline_json = decoded.decode("utf-8")
        if hashlib.sha256(decoded).hexdigest() != expected:
            return None
        parsed = json.loads(baseline_json)
        if (
            not isinstance(parsed, list)
            or json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            != baseline_json
        ):
            return None
        return baseline_json
    except (
        binascii.Error,
        UnicodeDecodeError,
        ValueError,
        zlib.error,
        RecursionError,
    ):
        return None


def _capture_next_leg_baseline(
    session_factory: sessionmaker,
    *,
    deepcoin_client: DeepcoinTradingClientProtocol,
    parent: _ProtectedEntryRuntime,
    operation: ExecutionOperationRecord,
    inst_id: str,
) -> tuple[str, _PendingTpslCapture]:
    prior_attempt_count = len(
        load_operation_bundle(
            session_factory,
            operation_id=operation.id,
        ).snapshots
    )
    max_attempts = 4
    reusable = _complete_reusable_protection_capture(
        session_factory,
        parent=parent,
    )
    if reusable is not None and prior_attempt_count < max_attempts:
        _record_pending_tpsl_snapshot(
            session_factory,
            operation=operation,
            capture=reusable,
            source="post_protection_snapshot",
            reused=True,
        )
        prior_attempt_count += 1
        try:
            return (
                reusable.normalized_baseline_json
                or _normalized_pending_tpsl_rows(reusable.rows),
                reusable,
            )
        except RecoveryLiveSubmitError:
            pass

    last_capture: _PendingTpslCapture | None = None
    delays = (0.0, 0.5, 1.0, 2.0)
    attempt_count = prior_attempt_count
    for attempt_index in range(prior_attempt_count, max_attempts):
        delay = delays[attempt_index]
        remaining = parent.deadline_monotonic - float(
            parent.monotonic_factory()
        )
        if remaining <= 0 or delay >= remaining:
            break
        if delay:
            parent.sleep_fn(delay)
        attempt_count += 1
        try:
            with deepcoin_client.request_scope(
                _protected_request_scope(
                    session_factory,
                    runtime=parent,
                    operation=operation,
                    phase="next_leg_preflight",
                )
            ):
                last_capture = _capture_pending_tpsl(
                    session_factory,
                    deepcoin_client=deepcoin_client,
                    uid_scope_hash=parent.uid_scope_hash,
                    inst_id=inst_id,
                )
        except Exception:
            last_capture = _unavailable_pending_tpsl_capture(
                session_factory,
                uid_scope_hash=parent.uid_scope_hash,
                reason_code="snapshot_read_unavailable",
            )
        _record_pending_tpsl_snapshot(
            session_factory,
            operation=operation,
            capture=last_capture,
            source="next_leg_preflight",
            reused=False,
        )
        if last_capture.collection.complete:
            try:
                return (
                    _normalized_pending_tpsl_rows(last_capture.rows),
                    last_capture,
                )
            except RecoveryLiveSubmitError:
                continue

    operation = _transition_protected_operation(
        session_factory,
        operation,
        phase="next_leg_preflight",
        state="pre_submit_deferred",
        certainty="not_sent",
        error_category="snapshot_incomplete",
        reason_code="next_leg_preflight_deferred",
        evidence={
            "attempt_count": attempt_count,
            "deadline_at": parent.operation.deadline_at.isoformat(),
            "last_complete_snapshot_ref": (
                hashlib.sha256(
                    (
                        "pending_tpsl:"
                        + str(last_capture.collection.fingerprint)
                    ).encode("utf-8")
                ).hexdigest()
                if last_capture is not None
                and last_capture.collection.complete
                and last_capture.collection.fingerprint is not None
                else None
            ),
            "writer_attempted": False,
        },
        changed_at=datetime.now(UTC),
    )
    raise RecoveryLiveSubmitError("protected_entry_pre_submit_deferred")


def _require_next_leg_parent_authority(
    session_factory: sessionmaker,
    *,
    parent: _ProtectedEntryRuntime,
    capture: _PendingTpslCapture,
) -> None:
    current = load_operation_bundle(
        session_factory,
        operation_id=parent.operation.id,
    ).operation
    evidence = _operation_evidence(current)
    required = evidence.get("required_protection_count")
    confirmed = evidence.get("confirmed_protection_count")
    if (
        current.state != "protected"
        or current.outcome_certainty != "confirmed"
        or type(required) is not int
        or required <= 0
        or confirmed != required
        or capture.end_write_generation % 2 != 0
        or capture.end_write_generation
        != _current_write_generation(
            session_factory,
            uid_scope_hash=parent.uid_scope_hash,
        )
    ):
        raise RecoveryLiveSubmitError(
            "protected_entry_next_leg_authority_changed"
        )
    parent.operation = current


def _trigger_history_response(
    deepcoin_client: Any,
    *,
    inst_id: str,
):
    raw_reader = getattr(deepcoin_client, "read_trigger_order_history", None)
    list_reader = getattr(deepcoin_client, "list_trigger_order_history", None)
    if callable(raw_reader):
        return raw_reader(inst_id=inst_id)
    if callable(list_reader):
        return list_reader(inst_id=inst_id)
    raise DeepcoinSnapshotUnavailable("snapshot_reader_unavailable")


def _matching_later_trigger_order_id(
    rows: tuple[Mapping[str, Any], ...],
    *,
    order_payload: Mapping[str, Any],
) -> str | None:
    matches: list[str] = []
    for row in rows:
        order_id = str(row.get("ordId") or row.get("orderId") or "")
        state = str(row.get("state") or "").lower()
        if (
            not order_id
            or not _safe_protected_exchange_identity(order_id)
            or state
            not in {
                "live",
                "effective",
                "open",
                "submitted",
                "partially_filled",
                "filled",
            }
            or str(row.get("triggerOrderType") or "").lower()
            not in {"trigger", "conditional"}
            or str(row.get("orderType") or "").lower()
            not in {"limit", "trigger_limit"}
            or str(row.get("clOrdId") or "")
            != str(order_payload.get("clOrdId") or "")
            or str(row.get("instId") or "").upper()
            != str(order_payload.get("instId") or "").upper()
            or str(row.get("posSide") or "").lower()
            != str(order_payload.get("posSide") or "").lower()
            or not _exact_decimal_equal(
                row.get("sz"), order_payload.get("sz")
            )
            or not _exact_decimal_equal(
                row.get("price"), order_payload.get("price")
            )
            or not _exact_decimal_equal(
                row.get("triggerPrice"),
                order_payload.get("triggerPrice"),
            )
        ):
            continue
        matches.append(order_id)
    return matches[0] if len(set(matches)) == 1 else None


def _readback_later_trigger(
    session_factory: sessionmaker,
    *,
    deepcoin_client: DeepcoinTradingClientProtocol,
    parent: _ProtectedEntryRuntime,
    operation: ExecutionOperationRecord,
    order_payload: Mapping[str, Any],
) -> str | None:
    inst_id = str(order_payload.get("instId") or "")
    readers = (
        (
            "trigger_orders_history",
            lambda: _trigger_history_response(
                deepcoin_client,
                inst_id=inst_id,
            ),
        ),
        (
            "pending_trigger_orders",
            lambda: _raw_pending_tpsl_reader(
                deepcoin_client,
                inst_id=inst_id,
            ),
        ),
    )
    for endpoint, reader in readers:
        try:
            with deepcoin_client.request_scope(
                _protected_request_scope(
                    session_factory,
                    runtime=parent,
                    operation=operation,
                    phase="entry_readback",
                )
            ):
                response = reader()
        except Exception:
            continue
        evidence = build_exchange_collection_evidence(
            endpoint=endpoint,
            response=response,
        )
        if not evidence.complete:
            continue
        order_id = _matching_later_trigger_order_id(
            evidence.rows,
            order_payload=order_payload,
        )
        if order_id is not None:
            return order_id
    return None


def _persist_trigger_parent_confirmation(
    session_factory: sessionmaker,
    *,
    execution_order_leg_id: int,
    parent_order_id: str,
) -> None:
    with session_factory() as session:
        intent = (
            session.query(TriggerProtectionIntent)
            .filter(TriggerProtectionIntent.venue == "deepcoin")
            .filter(
                TriggerProtectionIntent.execution_order_leg_id
                == execution_order_leg_id
            )
            .one_or_none()
        )
        if intent is None:
            raise RecoveryLiveSubmitError(
                "trigger_protection_intent_missing"
            )
        if intent.parent_trigger_order_id not in (None, parent_order_id):
            raise RecoveryLiveSubmitError(
                "trigger_protection_parent_identity_conflict"
            )
        record_trigger_protection_parent(
            session,
            intent,
            parent_trigger_order_id=parent_order_id,
        )
        for protection_leg in _protection_legs_for_entry(
            session,
            execution_order_leg_id=execution_order_leg_id,
        ):
            bind_parent_entry_order(
                session,
                protection_leg,
                parent_entry_order_id=parent_order_id,
            )
        session.commit()


def _require_later_trigger_intent_identity(
    session_factory: sessionmaker,
    *,
    operation: ExecutionOperationRecord,
    execution_order_leg_id: int,
) -> TriggerProtectionIntent:
    evidence = _operation_evidence(operation)
    baseline_fingerprint = evidence.get("baseline_fingerprint")
    if (
        not isinstance(baseline_fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", baseline_fingerprint)
    ):
        raise RecoveryLiveSubmitError(
            "trigger_protection_intent_identity_conflict"
        )
    with session_factory() as session:
        intent = (
            session.query(TriggerProtectionIntent)
            .filter(TriggerProtectionIntent.venue == "deepcoin")
            .filter(
                TriggerProtectionIntent.execution_order_leg_id
                == execution_order_leg_id
            )
            .one_or_none()
        )
        if (
            intent is None
            or intent.request_fingerprint
            != operation.request_fingerprint
            or hashlib.sha256(
                str(intent.pre_submit_tpsl_baseline_json).encode("utf-8")
            ).hexdigest()
            != baseline_fingerprint
        ):
            raise RecoveryLiveSubmitError(
                "trigger_protection_intent_identity_conflict"
            )
        session.expunge(intent)
        return intent


def _load_confirmed_trigger_parent_id(
    session_factory: sessionmaker,
    *,
    execution_order_leg_id: int,
) -> str:
    with session_factory() as session:
        intent = (
            session.query(TriggerProtectionIntent)
            .filter(TriggerProtectionIntent.venue == "deepcoin")
            .filter(
                TriggerProtectionIntent.execution_order_leg_id
                == execution_order_leg_id
            )
            .one_or_none()
        )
        order_id = (
            str(intent.parent_trigger_order_id)
            if intent is not None and intent.parent_trigger_order_id
            else ""
        )
    if not order_id or not _safe_protected_exchange_identity(order_id):
        raise RecoveryLiveSubmitError(
            "trigger_protection_parent_identity_invalid"
        )
    return order_id


def _submit_trigger_with_protection_intent(
    session_factory: sessionmaker,
    *,
    deepcoin_client: DeepcoinTradingClientProtocol,
    trade_signal: TradeSignalRecord,
    draft: dict[str, Any],
    leg: dict[str, Any],
    leg_index: int,
    binding_context: dict[str, Any],
    order_payload: dict[str, Any],
    submission_progress: EntrySubmissionProgress,
    parent: _ProtectedEntryRuntime | None = None,
) -> dict[str, Any]:
    """Snapshot, persist intent, submit parent, and bind its returned identity."""

    inst_id = str(order_payload.get("instId") or "").upper()
    side = str(order_payload.get("posSide") or order_payload.get("side") or "").lower()
    if not inst_id or not side:
        raise RecoveryLiveSubmitError("missing_trigger_protection_identity")
    if parent is not None:
        execution_order_leg_id = _prepare_trigger_protection_intent(
            session_factory,
            trade_signal=trade_signal,
            draft=draft,
            leg=leg,
            leg_index=leg_index,
            binding_context=binding_context,
            order_payload=order_payload,
        )
        operation = _reserve_next_leg_operation(
            session_factory,
            trade_signal=trade_signal,
            parent=parent,
            execution_order_leg_id=execution_order_leg_id,
            leg_index=leg_index,
            order_payload=order_payload,
        )
        if operation.state == "pre_submit_deferred":
            raise RecoveryLiveSubmitError(
                "protected_entry_pre_submit_deferred"
            )
        if operation.state == "completed":
            _require_later_trigger_intent_identity(
                session_factory,
                operation=operation,
                execution_order_leg_id=execution_order_leg_id,
            )
            order_id = _load_confirmed_trigger_parent_id(
                session_factory,
                execution_order_leg_id=execution_order_leg_id,
            )
            return {"code": "0", "data": {"ordId": order_id}}
        won_entry_submit = False
        if operation.state == "next_leg_preflight":
            baseline_json, capture = _capture_next_leg_baseline(
                session_factory,
                deepcoin_client=deepcoin_client,
                parent=parent,
                operation=operation,
                inst_id=inst_id,
            )
            correlation_id = f"trigger-protection:{execution_order_leg_id}"
            baseline_fingerprint = hashlib.sha256(
                baseline_json.encode("utf-8")
            ).hexdigest()
            with session_factory() as session:
                create_or_get_trigger_protection_intent(
                    session,
                    venue="deepcoin",
                    execution_order_leg_id=execution_order_leg_id,
                    request_fingerprint=(
                        _trigger_protection_request_fingerprint(order_payload)
                    ),
                    pre_submit_tpsl_baseline_json=baseline_json,
                    correlation_id=correlation_id,
                )
                take_profit_legs = draft.get("take_profit_legs")
                if isinstance(take_profit_legs, list) and take_profit_legs:
                    create_or_get_trigger_take_profit_convergence(
                        session,
                        venue="deepcoin",
                        execution_order_leg_id=execution_order_leg_id,
                        desired_take_profits=take_profit_legs,
                    )
                _create_trigger_protection_leg_plan(
                    session,
                    execution_order_leg_id=execution_order_leg_id,
                    order_payload=order_payload,
                    draft=draft,
                )
                session.commit()
            _require_next_leg_parent_authority(
                session_factory,
                parent=parent,
                capture=capture,
            )
            if (
                parent.deadline_monotonic
                <= float(parent.monotonic_factory())
                or not _protected_entry_writer_allowed(
                    session_factory,
                    trade_signal_id=trade_signal.id,
                )
            ):
                durable_attempt_count = len(
                    load_operation_bundle(
                        session_factory,
                        operation_id=operation.id,
                    ).snapshots
                )
                operation = _transition_protected_operation(
                    session_factory,
                    operation,
                    phase="next_leg_preflight",
                    state="pre_submit_deferred",
                    certainty="not_sent",
                    error_category="snapshot_incomplete",
                    reason_code="next_leg_preflight_deferred",
                    evidence={
                        "attempt_count": durable_attempt_count,
                        "deadline_at": parent.operation.deadline_at.isoformat(),
                        "last_complete_snapshot_ref": hashlib.sha256(
                            (
                                "pending_tpsl:"
                                + str(capture.collection.fingerprint)
                            ).encode("utf-8")
                        ).hexdigest(),
                        "writer_attempted": False,
                    },
                    changed_at=datetime.now(UTC),
                )
                raise RecoveryLiveSubmitError(
                    "protected_entry_pre_submit_deferred"
                )
            attempted_at = datetime.now(UTC)
            operation = _transition_protected_operation(
                session_factory,
                operation,
                phase="entry_submit",
                state="entry_submitting",
                certainty="not_sent",
                reason_code="next_leg_submit_authorized",
                evidence={
                    "leg_index": leg_index,
                    "snapshot_ref": hashlib.sha256(
                        (
                            "pending_tpsl:"
                            + str(capture.collection.fingerprint)
                        ).encode("utf-8")
                    ).hexdigest(),
                    "baseline_fingerprint": baseline_fingerprint,
                    "writer_attempted": True,
                },
                changed_at=attempted_at,
                writer_attempted_at=attempted_at,
            )
            won_entry_submit = True
        if operation.state == "entry_submitting" and won_entry_submit:
            baseline_fingerprint = _operation_evidence(operation).get(
                "baseline_fingerprint"
            )
            _require_later_trigger_intent_identity(
                session_factory,
                operation=operation,
                execution_order_leg_id=execution_order_leg_id,
            )
            try:
                advance_account_write_generation(
                    session_factory,
                    uid_scope_hash=parent.uid_scope_hash,
                )
                try:
                    if not _protected_entry_writer_allowed(
                        session_factory,
                        trade_signal_id=trade_signal.id,
                    ):
                        raise RecoveryLiveSubmitError(
                            "protected_entry_submit_not_authorized"
                        )
                    submission_progress.record_attempt()
                    with deepcoin_client.request_scope(
                        _protected_request_scope(
                            session_factory,
                            runtime=parent,
                            operation=operation,
                            phase="entry_submit",
                        )
                    ):
                        response = deepcoin_client.trigger_order(order_payload)
                finally:
                    advance_account_write_generation(
                        session_factory,
                        uid_scope_hash=parent.uid_scope_hash,
                    )
            except DeepcoinDefiniteRejection:
                _transition_protected_operation(
                    session_factory,
                    operation,
                    phase="entry_readback",
                    state="entry_rejected",
                    certainty="rejected",
                    error_category="business_rejected",
                    reason_code="entry_submission_rejected",
                    evidence={"leg_index": leg_index},
                    changed_at=datetime.now(UTC),
                )
                raise
            except DeepcoinPreSendUnavailable as exc:
                if exc.fact.outcome_certainty != OutcomeCertainty.NOT_SENT:
                    raise RecoveryLiveSubmitError(
                        "protected_entry_pre_send_fact_conflict"
                    ) from None
                prior_evidence = _operation_evidence(operation)
                operation = defer_execution_operation_after_not_sent(
                    session_factory,
                    operation_id=operation.id,
                    expected_operation_key=operation.operation_key,
                    expected_request_fingerprint=operation.request_fingerprint,
                    expected_economics_fingerprint=operation.economics_fingerprint,
                    expected_state_version=operation.state_version,
                    evidence={
                        "baseline_fingerprint": baseline_fingerprint,
                        "deadline_at": parent.operation.deadline_at.isoformat(),
                        "last_complete_snapshot_ref": prior_evidence.get(
                            "snapshot_ref"
                        ),
                        "leg_index": leg_index,
                        "reason_code": str(exc.fact.safe_code),
                        "writer_attempted": False,
                    },
                    error_category=exc.fact.category,
                    reason_code="next_leg_preflight_deferred",
                    updated_at=datetime.now(UTC),
                )
                raise RecoveryLiveSubmitError(
                    "protected_entry_pre_submit_deferred"
                ) from None
            except Exception:
                operation = _transition_protected_operation(
                    session_factory,
                    operation,
                    phase="entry_readback",
                    state="entry_unknown",
                    certainty="unknown",
                    reason_code="entry_submission_unknown",
                    evidence={
                        "leg_index": leg_index,
                        "baseline_fingerprint": baseline_fingerprint,
                    },
                    changed_at=datetime.now(UTC),
                )
                response = {}
            else:
                operation = _transition_protected_operation(
                    session_factory,
                    operation,
                    phase="entry_readback",
                    state="entry_pending_readback",
                    certainty="accepted",
                    reason_code="entry_submission_accepted",
                    evidence={
                        "leg_index": leg_index,
                        "baseline_fingerprint": baseline_fingerprint,
                    },
                    changed_at=datetime.now(UTC),
                )
        elif operation.state == "entry_submitting":
            baseline_fingerprint = _operation_evidence(operation).get(
                "baseline_fingerprint"
            )
            _require_later_trigger_intent_identity(
                session_factory,
                operation=operation,
                execution_order_leg_id=execution_order_leg_id,
            )
            operation = _transition_protected_operation(
                session_factory,
                operation,
                phase="entry_readback",
                state="entry_unknown",
                certainty="unknown",
                reason_code="entry_submission_unknown",
                evidence={
                    "leg_index": leg_index,
                    "baseline_fingerprint": baseline_fingerprint,
                },
                changed_at=datetime.now(UTC),
            )
            response = {}
        elif operation.state in {"entry_pending_readback", "entry_unknown"}:
            baseline_fingerprint = _operation_evidence(operation).get(
                "baseline_fingerprint"
            )
            _require_later_trigger_intent_identity(
                session_factory,
                operation=operation,
                execution_order_leg_id=execution_order_leg_id,
            )
            response = {}
        else:
            raise RecoveryLiveSubmitError(
                "protected_entry_next_leg_state_conflict"
            )
        order_id = _readback_later_trigger(
            session_factory,
            deepcoin_client=deepcoin_client,
            parent=parent,
            operation=operation,
            order_payload=order_payload,
        )
        if order_id is None:
            raise DeepcoinRequestOutcomeUnknown(
                "protected_entry_later_leg_readback_pending"
            )
        _persist_trigger_parent_confirmation(
            session_factory,
            execution_order_leg_id=execution_order_leg_id,
            parent_order_id=order_id,
        )
        _transition_protected_operation(
            session_factory,
            operation,
            phase="completed",
            state="completed",
            certainty="confirmed",
            reason_code="entry_sequence_completed",
            evidence={
                "leg_index": leg_index,
                "order_ref": hashlib.sha256(
                    f"order:{order_id}".encode("utf-8")
                ).hexdigest(),
                "baseline_fingerprint": baseline_fingerprint,
            },
            changed_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        return {"code": "0", "data": {"ordId": order_id}}
    with _trigger_protection_submission_lock(
        deepcoin_client=deepcoin_client,
        venue="deepcoin",
        inst_id=inst_id,
        side=side,
    ):
        baseline_json = _normalized_pending_tpsl_baseline(
            deepcoin_client,
            inst_id=inst_id,
        )
        execution_order_leg_id = _prepare_trigger_protection_intent(
            session_factory,
            trade_signal=trade_signal,
            draft=draft,
            leg=leg,
            leg_index=leg_index,
            binding_context=binding_context,
            order_payload=order_payload,
        )
        request_fingerprint = _trigger_protection_request_fingerprint(order_payload)
        correlation_id = f"trigger-protection:{execution_order_leg_id}"
        with session_factory() as session:
            intent = create_or_get_trigger_protection_intent(
                session,
                venue="deepcoin",
                execution_order_leg_id=execution_order_leg_id,
                request_fingerprint=request_fingerprint,
                pre_submit_tpsl_baseline_json=baseline_json,
                correlation_id=correlation_id,
            )
            take_profit_legs = draft.get("take_profit_legs")
            if isinstance(take_profit_legs, list) and take_profit_legs:
                create_or_get_trigger_take_profit_convergence(
                    session,
                    venue="deepcoin",
                    execution_order_leg_id=execution_order_leg_id,
                    desired_take_profits=take_profit_legs,
                )
            _create_trigger_protection_leg_plan(
                session,
                execution_order_leg_id=execution_order_leg_id,
                order_payload=order_payload,
                draft=draft,
            )
            session.commit()
        try:
            submission_progress.record_attempt()
            response = deepcoin_client.trigger_order(order_payload)
        except DeepcoinClientError:
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise DeepcoinClientError(f"Deepcoin client failed: {exc}") from exc
        parent_order_id = _normalized_trigger_order_id(response)
        with session_factory() as session:
            intent = (
                session.query(TriggerProtectionIntent)
                .filter(TriggerProtectionIntent.venue == "deepcoin")
                .filter(TriggerProtectionIntent.execution_order_leg_id == execution_order_leg_id)
                .one_or_none()
            )
            if intent is None:  # pragma: no cover - durable intent was just committed
                raise RecoveryLiveSubmitError("trigger_protection_intent_missing")
            record_trigger_protection_parent(
                session,
                intent,
                parent_trigger_order_id=parent_order_id,
            )
            for protection_leg in _protection_legs_for_entry(
                session, execution_order_leg_id=execution_order_leg_id
            ):
                bind_parent_entry_order(
                    session,
                    protection_leg,
                    parent_entry_order_id=parent_order_id,
                )
            entry_leg = session.get(ExecutionOrderLeg, execution_order_leg_id)
            if entry_leg is None:  # pragma: no cover - durable leg was just used for the intent
                raise RecoveryLiveSubmitError("trigger_protection_entry_leg_missing")
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    action="create_trigger_entry",
                    reason="live_signal_auto_trade",
                    after=_extract_tpsl_snapshot(_persisted_order_request(order_payload, leg)),
                    request=_persisted_order_request(order_payload, leg),
                    response=response,
                    execution_binding_id=entry_leg.execution_binding_id,
                    trade_signal_id=trade_signal.id,
                    strategy_instance_id=entry_leg.strategy_instance_id,
                    kol_id=str(binding_context["kol_id"]),
                    chat_id=int(binding_context["source"].get("chat_id") or trade_signal.chat_id),
                    message_id=int(binding_context["source"].get("message_id") or trade_signal.message_id),
                    source_message_id=trade_signal.message_id,
                    symbol=str(binding_context["symbol"]),
                    side=str(binding_context["side"]),
                    order_id=parent_order_id,
                    client_order_id=str(leg.get("client_order_id") or order_payload.get("clOrdId") or "") or None,
                ),
                session=session,
            )
            session.commit()
        return response


def _create_trigger_protection_leg_plan(
    session,
    *,
    execution_order_leg_id: int,
    order_payload: dict[str, Any],
    draft: dict[str, Any],
) -> None:
    """Create one immutable local record for every planned trigger protection."""

    stop_price = _optional_snapshot_text(
        order_payload, "slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice"
    )
    if stop_price is not None:
        create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=execution_order_leg_id,
            role="primary_stop",
            leg_index=1,
            planned_trigger_price=stop_price,
            planned_size=_optional_snapshot_text(order_payload, "sz", "size"),
        )
        # The backup price is derived only after the exact filled position and
        # primary stop are verified, but its audit identity exists now.
        create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=execution_order_leg_id,
            role="backup_stop",
            leg_index=1,
            planned_trigger_price=None,
            planned_size=None,
        )
    take_profit_legs = draft.get("take_profit_legs")
    if not isinstance(take_profit_legs, list):
        return
    for index, target in enumerate(take_profit_legs, start=1):
        if not isinstance(target, dict):
            continue
        price = _optional_snapshot_text(target, "price")
        if price is None:
            continue
        create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=execution_order_leg_id,
            role="take_profit",
            leg_index=index,
            planned_trigger_price=price,
            planned_size=_optional_snapshot_text(target, "size", "allocation_pct"),
        )


def _protection_legs_for_entry(session, *, execution_order_leg_id: int):
    from telegram_kol_research.models import PositionProtectionLeg

    return (
        session.query(PositionProtectionLeg)
        .filter(PositionProtectionLeg.execution_order_leg_id == execution_order_leg_id)
        .order_by(PositionProtectionLeg.id.asc())
        .all()
    )


def _normalized_pending_tpsl_baseline(
    deepcoin_client: DeepcoinTradingClientProtocol, *, inst_id: str
) -> str:
    method = getattr(deepcoin_client, "list_trigger_orders_pending", None)
    if method is None:
        raise RecoveryLiveSubmitError("trigger_protection_baseline_unavailable")
    try:
        rows = method(inst_id=inst_id)
    except Exception as exc:
        raise RecoveryLiveSubmitError("trigger_protection_baseline_unavailable") from exc
    if not isinstance(rows, list):
        raise RecoveryLiveSubmitError("trigger_protection_baseline_malformed")
    normalized: list[dict[str, str | None]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RecoveryLiveSubmitError("trigger_protection_baseline_malformed")
        if str(row.get("triggerOrderType") or "").upper() != "TPSL":
            continue
        normalized.append(_normalized_tpsl_row(row))
    normalized.sort(key=lambda row: (row["ord_id"] or "", row["exchange_created_at"] or ""))
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _trigger_protection_request_fingerprint(order_payload: dict[str, Any]) -> str:
    """Fingerprint an attached-protection request with both protection sides explicit."""

    fingerprint_payload = dict(order_payload)
    for key in ("tpTriggerPx", "slTriggerPx"):
        fingerprint_payload[key] = order_payload.get(key)
    return hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _normalized_tpsl_row(row: dict[str, Any]) -> dict[str, str | None]:
    ord_id = _required_snapshot_text(row, "ordId", "orderId", "order_id", "algoId")
    instrument = _required_snapshot_text(row, "instId", "instrumentId")
    side = _required_snapshot_text(row, "posSide", "side").lower()
    return {
        "ord_id": ord_id,
        "instrument": instrument.upper(),
        "side": side,
        "trigger_order_type": "TPSL",
        "size": _optional_snapshot_text(row, "sz", "size", "quantity"),
        "take_profit_trigger_price": _optional_snapshot_text(row, "tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice"),
        "stop_loss_trigger_price": _optional_snapshot_text(row, "slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice"),
        "exchange_created_at": _optional_snapshot_text(row, "cTime", "createdAt", "created_at", "createdTime"),
        "exchange_updated_at": _optional_snapshot_text(row, "uTime", "updatedAt", "updated_at", "updatedTime"),
    }


def _required_snapshot_text(row: dict[str, Any], *keys: str) -> str:
    value = _optional_snapshot_text(row, *keys)
    if value is None:
        raise RecoveryLiveSubmitError("trigger_protection_baseline_malformed")
    return value


def _optional_snapshot_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text
    return None


def _normalized_trigger_order_id(response: Any) -> str:
    if not isinstance(response, dict):
        raise DeepcoinClientError("Deepcoin trigger order response missing order id")
    order_id = _extract_exact_trigger_order_id(response)
    if order_id is not None:
        return order_id
    raise DeepcoinClientError("Deepcoin trigger order response missing order id")


def _persisted_order_request(
    order_payload: dict[str, Any],
    leg: dict[str, Any],
) -> dict[str, Any]:
    persisted_request = dict(order_payload)
    merged_from_leg_indices = leg.get("merged_from_leg_indices")
    if isinstance(merged_from_leg_indices, list):
        persisted_request["merged_from_leg_indices"] = list(merged_from_leg_indices)
    return persisted_request


def _split_quantity_by_allocations(
    *,
    quantity: float,
    allocations: list[float],
    quantity_step: float,
) -> list[float]:
    if quantity <= 0 or not allocations:
        return []
    step = quantity_step if quantity_step > 0 else 0.000001
    sizes: list[float] = []
    remaining = quantity
    total = sum(allocations)
    for allocation in allocations[:-1]:
        raw_size = quantity * allocation / total
        size = _round_down_to_step(raw_size, step)
        sizes.append(size)
        remaining -= size
    sizes.append(_round_down_to_step(remaining, step))
    return [float(f"{size:g}") for size in sizes]


def _quantity_step_from_draft(draft: dict[str, Any]) -> float:
    contract_spec = draft.get("contract_spec")
    if isinstance(contract_spec, dict):
        try:
            return float(contract_spec.get("quantity_step") or 0.000001)
        except (TypeError, ValueError):
            pass
    return 0.000001


def _minimum_quantity_from_draft(draft: dict[str, Any]) -> float:
    contract_spec = draft.get("contract_spec")
    if isinstance(contract_spec, dict):
        try:
            return float(contract_spec.get("min_quantity") or 0.000001)
        except (TypeError, ValueError):
            pass
    return 0.000001


def _round_down_to_step(value: float, step: float) -> float:
    rounded = math.floor((value / step) + 1e-12) * step
    return float(f"{rounded:.12g}")


def _protection_response_has_error(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("error"))
    if isinstance(value, list):
        return any(_protection_response_has_error(item) for item in value)
    return False


def _record_submitted_order_legs(
    session_factory: sessionmaker,
    *,
    binding_id: int,
    strategy_instance_id: str,
    submitted_orders: list[dict[str, Any]],
) -> None:
    for order in submitted_orders:
        execution_type = str(order.get("execution_type") or "unknown").lower()
        pos_id = str(order.get("pos_id") or "") or None
        stored_response = (
            dict(order.get("response"))
            if isinstance(order.get("response"), dict)
            else {}
        )
        if pos_id:
            stored_response["posId"] = pos_id
        upsert_execution_order_leg(
            session_factory,
            ExecutionOrderLegRecord(
                execution_binding_id=binding_id,
                strategy_instance_id=strategy_instance_id or None,
                leg_index=int(order.get("leg_index") or 0),
                purpose="entry",
                order_kind=execution_type,
                order_id=str(order.get("order_id") or "") or None,
                client_order_id=str(order.get("client_order_id") or "") or None,
                pos_id=pos_id,
                status="active" if pos_id else "open",
                attribution_status="verified" if pos_id else None,
                request=order.get("request") if isinstance(order.get("request"), dict) else None,
                response=stored_response or None,
            ),
        )


def _record_entry_protection_ledger_rows(
    session_factory: sessionmaker,
    *,
    binding_id: int,
    draft: dict[str, Any],
    submitted_orders: list[dict[str, Any]],
    side: str,
    deepcoin_client: DeepcoinTradingClientProtocol,
    seen_at: datetime,
) -> None:
    inst_id = str(draft.get("instrument_id") or "")
    if not inst_id:
        return
    pending_tpsl_rows = _safe_pending_tpsl_rows(deepcoin_client, inst_id=inst_id)
    with session_factory() as session:
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == int(binding_id))
            .filter(ExecutionOrderLeg.purpose == "entry")
            .all()
        )
        leg_by_pos_id = {str(leg.pos_id): leg for leg in legs if leg.pos_id}
        for order in submitted_orders:
            if str(order.get("execution_type") or "").lower() != "market":
                continue
            pos_id = str(order.get("pos_id") or "")
            if not pos_id:
                continue
            leg = leg_by_pos_id.get(pos_id)
            if leg is None:
                continue
            rows = _entry_protection_ledger_rows(
                protection_request=order.get("protection_request"),
                protection_response=order.get("protection_response"),
                pending_tpsl_rows=pending_tpsl_rows,
                inst_id=inst_id,
                side=side,
                pos_id=pos_id,
                seen_at=seen_at,
            )
            for row in rows:
                upsert_protection_ledger_row(
                    session,
                    venue=str(leg.venue or "deepcoin"),
                    execution_binding_id=binding_id,
                    execution_order_leg_id=leg.id,
                    strategy_instance_id=leg.strategy_instance_id,
                    pos_id=pos_id,
                    instrument_id=inst_id,
                    side=side,
                    order_id=row["order_id"],
                    purpose=row["purpose"],
                    trigger_price=row["trigger_price"],
                    size_text=row.get("size_text"),
                    status="verified",
                    evidence_source="entry_protection_response",
                    evidence=_entry_protection_ledger_evidence(row),
                    seen_at=seen_at,
                )
            if rows:
                activate_protection_revision(
                    session,
                    venue=str(leg.venue or "deepcoin"),
                    execution_binding_id=binding_id,
                    execution_order_leg_id=leg.id,
                    strategy_instance_id=leg.strategy_instance_id,
                    pos_id=pos_id,
                    source="entry_protection",
                    protection_json={"order_ids": [row["order_id"] for row in rows], "rows": rows},
                )
        session.commit()


def _record_market_take_profit_convergences(
    session_factory: sessionmaker,
    *,
    binding_id: int,
    draft: dict[str, Any],
    submitted_orders: list[dict[str, Any]],
    created_at: datetime,
) -> None:
    """Persist deferred TP work for market entries before their next reconciliation."""

    take_profit_legs = draft.get("take_profit_legs")
    if not isinstance(take_profit_legs, list) or not take_profit_legs:
        return
    market_pos_ids = {
        str(order.get("pos_id") or "")
        for order in submitted_orders
        if str(order.get("execution_type") or "").lower() == "market"
        and str(order.get("pos_id") or "")
    }
    if not market_pos_ids:
        return
    with session_factory() as session:
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == int(binding_id))
            .filter(ExecutionOrderLeg.purpose == "entry")
            .filter(ExecutionOrderLeg.order_kind == "market")
            .filter(ExecutionOrderLeg.pos_id.in_(sorted(market_pos_ids)))
            .all()
        )
        for leg in legs:
            create_or_get_trigger_take_profit_convergence(
                session,
                venue=str(leg.venue or "deepcoin"),
                execution_order_leg_id=int(leg.id),
                desired_take_profits=take_profit_legs,
                created_at=created_at,
            )
        session.commit()


def _entry_protection_ledger_rows(
    *,
    protection_request: Any,
    protection_response: Any,
    pending_tpsl_rows: list[dict[str, Any]],
    inst_id: str,
    side: str,
    pos_id: str,
    seen_at: datetime | None = None,
) -> list[dict[str, str | None]]:
    expected_rows = _expected_protection_rows(protection_request)
    if not expected_rows:
        return []
    response_order_ids = _protection_response_order_ids(protection_response)
    response_ids_align_with_rows = len(response_order_ids) == len(expected_rows)
    if response_ids_align_with_rows:
        return [
            {
                **expected,
                "order_id": response_order_ids[index],
                "evidence_match": "exchange_returned_order_id_exact_readback",
            }
            for index, expected in enumerate(expected_rows)
        ]
    if response_order_ids:
        anchored_rows = _response_anchored_entry_protection_rows(
            expected_rows=expected_rows,
            response_order_ids=response_order_ids,
            pending_tpsl_rows=pending_tpsl_rows,
            inst_id=inst_id,
            side=side,
            pos_id=pos_id,
            seen_at=seen_at,
        )
        if anchored_rows:
            return anchored_rows
    return []


def _response_anchored_entry_protection_rows(
    *,
    expected_rows: list[dict[str, str | None]],
    response_order_ids: list[str],
    pending_tpsl_rows: list[dict[str, Any]],
    inst_id: str,
    side: str,
    pos_id: str,
    seen_at: datetime | None,
) -> list[dict[str, str | None]]:
    pending_by_order_id = {
        order_id: row
        for row in pending_tpsl_rows
        if (order_id := _first_nonzero_text(
            row, "ordId", "orderId", "order_id", "algoId", "triggerOrderId", "id"
        ))
        and _pending_tpsl_row_matches_exchange_identity(
            row, inst_id=inst_id, side=side, pos_id=pos_id
        )
    }
    returned_rows = [
        (order_id, pending_by_order_id.get(order_id)) for order_id in response_order_ids
    ]
    if any(row is None for _, row in returned_rows):
        return []
    anchor_times = [
        _pending_tpsl_row_time(row) for _, row in returned_rows if row is not None
    ]
    if not anchor_times or any(value is None for value in anchor_times):
        return []
    event_time = _coerce_utc_naive(seen_at) if seen_at is not None else None
    if event_time is not None and any(
        abs((value - event_time).total_seconds()) > 120
        for value in anchor_times
        if value is not None
    ):
        return []

    expected_by_purpose = {str(row["purpose"]): row for row in expected_rows}
    resolved: list[dict[str, str | None]] = []
    matched_purposes: set[str] = set()
    returned_order_id_set = {order_id for order_id, _ in returned_rows}
    for order_id, row in returned_rows:
        if row is None:
            continue
        purpose = _pending_tpsl_row_matching_expected_purpose(row, expected_rows)
        if purpose is None:
            return []
        matched_purposes.add(purpose)
        resolved.append(
            {
                **expected_by_purpose[purpose],
                "order_id": order_id,
                "evidence_match": "response_anchored_order",
            }
        )

    for purpose, expected in expected_by_purpose.items():
        if purpose in matched_purposes:
            continue
        candidates = [
            row
            for row in pending_tpsl_rows
            if _first_nonzero_text(
                row, "ordId", "orderId", "order_id", "algoId", "triggerOrderId", "id"
            )
            not in returned_order_id_set
            and _pending_tpsl_row_matches_exchange_identity(
                row, inst_id=inst_id, side=side, pos_id=pos_id
            )
            and _pending_tpsl_row_matches_expected(row, expected)
            and _pending_tpsl_row_time_within(row, anchor_times, seconds=3)
            and (
                event_time is None
                or _pending_tpsl_row_time_within(row, [event_time], seconds=120)
            )
        ]
        unique_order_ids = sorted(
            {
                order_id
                for row in candidates
                if (order_id := _first_nonzero_text(
                    row, "ordId", "orderId", "order_id", "algoId", "triggerOrderId", "id"
                ))
            }
        )
        if len(unique_order_ids) != 1:
            return []
        resolved.append(
            {
                **expected,
                "order_id": unique_order_ids[0],
                "evidence_match": "response_anchored_sibling_tpsl",
            }
        )
    return resolved


def _combined_entry_protection_ledger_row(
    *,
    expected_rows: list[dict[str, str | None]],
    pending_tpsl_rows: list[dict[str, Any]],
    inst_id: str,
    side: str,
    pos_id: str,
) -> dict[str, str | None] | None:
    expected_by_purpose = {
        str(row["purpose"]): str(row["trigger_price"])
        for row in expected_rows
        if row.get("purpose") in {"stop_loss", "take_profit"}
        and row.get("trigger_price") is not None
    }
    if set(expected_by_purpose) != {"stop_loss", "take_profit"}:
        return None
    matches: list[dict[str, Any]] = []
    for row in pending_tpsl_rows:
        if not _pending_tpsl_row_matches_identity(
            row, inst_id=inst_id, side=side, pos_id=pos_id
        ):
            continue
        stop_price = _first_nonzero_text(
            row, "slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice"
        )
        take_profit_price = _first_nonzero_text(
            row, "tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice"
        )
        if (
            stop_price is not None
            and take_profit_price is not None
            and _same_numeric_text(stop_price, expected_by_purpose["stop_loss"])
            and _same_numeric_text(take_profit_price, expected_by_purpose["take_profit"])
        ):
            matches.append(row)
    if len(matches) != 1:
        return None
    order_id = _first_nonzero_text(
        matches[0], "ordId", "orderId", "order_id", "algoId", "triggerOrderId", "id"
    )
    if not order_id:
        return None
    size_text = str(matches[0].get("sz")) if matches[0].get("sz") is not None else None
    return {
        "purpose": "combined",
        "trigger_price": None,
        "size_text": size_text,
        "order_id": order_id,
        "evidence_match": "pending_tpsl_combined_exact_pos_id",
        "stop_loss": expected_by_purpose["stop_loss"],
        "take_profit": expected_by_purpose["take_profit"],
    }


def _entry_protection_ledger_evidence(row: dict[str, str | None]) -> dict[str, Any]:
    evidence: dict[str, Any] = {"match": row["evidence_match"]}
    for key in ("stop_loss", "take_profit"):
        if row.get(key) is not None:
            evidence[key] = row[key]
    return evidence


def _expected_protection_rows(protection_request: Any) -> list[dict[str, str | None]]:
    requests = protection_request if isinstance(protection_request, list) else [protection_request]
    rows: list[dict[str, str | None]] = []
    for request in requests:
        if not isinstance(request, dict):
            continue
        size_text = str(request.get("sz")) if request.get("sz") is not None else None
        for purpose, keys in (
            ("stop_loss", ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")),
            ("take_profit", ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")),
        ):
            trigger_price = _first_nonzero_text(request, *keys)
            if trigger_price is None:
                continue
            rows.append(
                {
                    "purpose": purpose,
                    "trigger_price": trigger_price,
                    "size_text": size_text,
                }
            )
    return rows


def _protection_response_order_ids(protection_response: Any) -> list[str]:
    responses = protection_response if isinstance(protection_response, list) else [protection_response]
    order_ids: list[str] = []
    for response in responses:
        if not isinstance(response, dict):
            continue
        order_id = _extract_order_id(response)
        if order_id and order_id not in order_ids:
            order_ids.append(order_id)
    return order_ids


def _safe_pending_tpsl_rows(
    deepcoin_client: DeepcoinTradingClientProtocol, *, inst_id: str
) -> list[dict[str, Any]]:
    method = getattr(deepcoin_client, "list_trigger_orders_pending", None)
    if method is None:
        return []
    try:
        rows = method(inst_id=inst_id)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _match_pending_tpsl_order_id(
    pending_tpsl_rows: list[dict[str, Any]],
    *,
    inst_id: str,
    side: str,
    pos_id: str,
    purpose: str,
    trigger_price: str,
) -> str | None:
    matches: list[str] = []
    for row in pending_tpsl_rows:
        if not _pending_tpsl_row_matches_identity(
            row, inst_id=inst_id, side=side, pos_id=pos_id
        ):
            continue
        keys = (
            ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
            if purpose == "stop_loss"
            else ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
        )
        row_price = _first_nonzero_text(row, *keys)
        if row_price is None or not _same_numeric_text(row_price, trigger_price):
            continue
        order_id = _first_nonzero_text(
            row, "ordId", "orderId", "order_id", "algoId", "triggerOrderId", "id"
        )
        if order_id:
            matches.append(order_id)
    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None


def _pending_tpsl_row_matches_identity(
    row: dict[str, Any], *, inst_id: str, side: str, pos_id: str
) -> bool:
    if str(row.get("triggerOrderType") or "TPSL").upper() != "TPSL":
        return False
    if str(row.get("instId") or "").upper() != inst_id.upper():
        return False
    if str(row.get("posSide") or row.get("side") or "").lower() != side.lower():
        return False
    row_pos_id = _first_nonzero_text(
        row, "closePosId", "close_pos_id", "closePositionId", "posId", "pos_id", "positionId"
    )
    return row_pos_id == str(pos_id)


def _pending_tpsl_row_matches_exchange_identity(
    row: dict[str, Any], *, inst_id: str, side: str, pos_id: str
) -> bool:
    if str(row.get("triggerOrderType") or "TPSL").upper() != "TPSL":
        return False
    if str(row.get("instId") or "").upper() != inst_id.upper():
        return False
    if str(row.get("posSide") or row.get("side") or "").lower() != side.lower():
        return False
    row_pos_id = _first_nonzero_text(
        row, "closePosId", "close_pos_id", "closePositionId", "posId", "pos_id", "positionId"
    )
    return row_pos_id in (None, str(pos_id))


def _pending_tpsl_row_matching_expected_purpose(
    row: dict[str, Any], expected_rows: list[dict[str, str | None]]
) -> str | None:
    matches = [
        str(expected["purpose"])
        for expected in expected_rows
        if _pending_tpsl_row_matches_expected(row, expected)
    ]
    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None


def _pending_tpsl_row_matches_expected(
    row: dict[str, Any], expected: dict[str, str | None]
) -> bool:
    expected_price = expected.get("trigger_price")
    if expected_price is None:
        return False
    purpose = str(expected.get("purpose") or "")
    keys = (
        ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
        if purpose == "stop_loss"
        else ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
    )
    row_price = _first_nonzero_text(row, *keys)
    if row_price is None:
        row_price = _first_nonzero_text(row, "triggerPx", "triggerPrice")
    return bool(row_price and _same_numeric_text(row_price, expected_price))


def _pending_tpsl_row_time_within(
    row: dict[str, Any], anchors: list[datetime | None], *, seconds: int
) -> bool:
    row_time = _pending_tpsl_row_time(row)
    if row_time is None:
        return False
    return any(
        anchor is not None and abs((row_time - anchor).total_seconds()) <= seconds
        for anchor in anchors
    )


def _pending_tpsl_row_time(row: dict[str, Any]) -> datetime | None:
    for key in ("cTime", "uTime", "createdAt", "created_at", "createdTime"):
        value = row.get(key)
        parsed = _parse_deepcoin_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _parse_deepcoin_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            numeric = int(text)
            if numeric > 10_000_000_000:
                return datetime.fromtimestamp(numeric / 1000, UTC).replace(tzinfo=None)
            return datetime.fromtimestamp(numeric, UTC).replace(tzinfo=None)
        return _coerce_utc_naive(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except (OSError, OverflowError, ValueError):
        return None


def _coerce_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _same_numeric_text(left: str, right: str) -> bool:
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


def _first_nonzero_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value in (None, "", "0", 0):
            continue
        return str(value)
    return None


def _upsert_protection_failed_binding(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    draft: dict[str, Any],
    source: dict[str, Any],
    kol_id: str,
    symbol_key: str,
    side_key: str,
    order: dict[str, Any],
) -> int:
    return upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id=kol_id,
            chat_id=int(source.get("chat_id") or trade_signal.chat_id),
            message_id=int(source.get("message_id") or trade_signal.message_id),
            symbol=symbol_key,
            side=side_key,
            venue="deepcoin",
            order_id=str(order.get("order_id") or ""),
            client_order_id=str(order.get("client_order_id") or ""),
            pos_id=str(order.get("pos_id") or ""),
            margin_mode=str(draft.get("margin_mode") or "cross"),
            position_mode=str(draft.get("position_mode") or "split"),
            payload={"draft": draft, "submitted_orders": [order]},
            last_exchange_status="position_active_protection_failed",
            status="active",
            strategy_instance_id=str(draft.get("strategy_instance_id") or ""),
        ),
    )


def _attach_lifecycle_binding(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    binding_id: int,
    entered: bool,
    updated_at: datetime,
) -> None:
    with session_factory() as session:
        lifecycle = (
            session.query(StrategyLifecycle)
            .filter(StrategyLifecycle.chat_id == chat_id)
            .filter(StrategyLifecycle.message_id == message_id)
            .filter(StrategyLifecycle.symbol == symbol)
            .filter(StrategyLifecycle.side == side)
            .order_by(StrategyLifecycle.id.desc())
            .first()
        )
        if lifecycle is None:
            return
        lifecycle.execution_binding_id = binding_id
        if entered and lifecycle.lifecycle_status == "pending_entry":
            lifecycle.lifecycle_status = "entered"
            lifecycle.entered_at = updated_at
        lifecycle.updated_at = updated_at
        session.commit()


def _record_submitted_order_events(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    binding_id: int,
    draft: dict[str, Any],
    submitted_orders: list[dict[str, Any]],
    kol_id: str,
    symbol_key: str,
    side_key: str,
    source: dict[str, Any],
    created_at: datetime,
) -> None:
    strategy_instance_id = str(draft.get("strategy_instance_id") or trade_signal.strategy_instance_id or "")
    chat_id = int(source.get("chat_id") or trade_signal.chat_id)
    message_id = int(source.get("message_id") or trade_signal.message_id)
    for order in submitted_orders:
        execution_type = str(order.get("execution_type") or "").lower()
        base = {
            "execution_binding_id": binding_id,
            "trade_signal_id": trade_signal.id,
            "strategy_instance_id": strategy_instance_id or None,
            "kol_id": kol_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "source_message_id": trade_signal.message_id,
            "symbol": symbol_key,
            "side": side_key,
            "order_id": str(order.get("order_id") or "") or None,
            "client_order_id": str(order.get("client_order_id") or "") or None,
            "pos_id": str(order.get("pos_id") or "") or None,
            "created_at": created_at,
        }
        if execution_type == "market":
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    action="open_market_position",
                    reason="live_signal_auto_trade",
                    request=order.get("request"),
                    response=order.get("response"),
                    **base,
                ),
            )
            protection_request = order.get("protection_request")
            protection_response = order.get("protection_response")
            protection_requests = (
                protection_request
                if isinstance(protection_request, list)
                else [protection_request]
            )
            protection_responses = (
                protection_response
                if isinstance(protection_response, list)
                else [protection_response]
            )
            for item_index, request_item in enumerate(protection_requests):
                if not isinstance(request_item, dict):
                    continue
                response_item = (
                    protection_responses[item_index]
                    if item_index < len(protection_responses)
                    else protection_response
                )
                record_execution_event(
                    session_factory,
                    ExecutionEventRecord(
                        action="set_position_tpsl",
                        reason="entry_protection",
                        after=_extract_tpsl_snapshot(request_item),
                        request=request_item,
                        response=response_item if isinstance(response_item, dict) else None,
                        related_order_id=base["order_id"],
                        **base,
                    ),
                )
        else:
            request = order.get("request") if isinstance(order.get("request"), dict) else {}
            action = "create_limit_entry" if execution_type == "limit" else "create_trigger_entry"
            if action == "create_trigger_entry" and _trigger_parent_event_exists(
                session_factory,
                execution_binding_id=binding_id,
                order_id=base["order_id"],
            ):
                continue
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    action=action,
                    reason="live_signal_auto_trade",
                    after=_extract_tpsl_snapshot(request),
                    request=request,
                    response=order.get("response"),
                    **base,
                ),
            )


def _trigger_parent_event_exists(
    session_factory: sessionmaker,
    *,
    execution_binding_id: int,
    order_id: str | None,
) -> bool:
    if not order_id:
        return False
    with session_factory() as session:
        return (
            session.query(ExecutionEvent.id)
            .filter(ExecutionEvent.execution_binding_id == execution_binding_id)
            .filter(ExecutionEvent.action == "create_trigger_entry")
            .filter(ExecutionEvent.order_id == order_id)
            .first()
            is not None
        )


def _extract_tpsl_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for source_key, target_key in (
        ("tpTriggerPx", "take_profit"),
        ("tpTriggerPrice", "take_profit"),
        ("closeTPTriggerPrice", "take_profit"),
        ("slTriggerPx", "stop_loss"),
        ("slTriggerPrice", "stop_loss"),
        ("closeSLTriggerPrice", "stop_loss"),
    ):
        value = payload.get(source_key)
        if value in (None, "", "0", 0):
            continue
        snapshot[target_key] = value
    return snapshot


def build_deepcoin_place_order_payload(
    draft: dict[str, Any],
    leg: dict[str, Any],
) -> dict[str, Any]:
    """Convert one internal order leg to Deepcoin's place-order payload."""

    quantity = leg.get("quantity")
    if not isinstance(quantity, int | float) or quantity <= 0:
        raise RecoveryLiveSubmitError("non_positive_quantity")
    price = leg.get("price")
    if not isinstance(price, int | float) or price <= 0:
        raise RecoveryLiveSubmitError("non_positive_price")

    return {
        "instId": str(draft["instrument_id"]),
        "tdMode": _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross")),
        "side": str(leg["side"]).lower(),
        "posSide": str(leg["position_side"]).lower(),
        "ordType": "limit",
        "px": str(price),
        "sz": str(quantity),
        "clOrdId": str(leg.get("client_order_id") or ""),
        "mrgPosition": _deepcoin_position_mode(str(draft.get("position_mode") or "split")),
    }


def build_deepcoin_market_order_payload(
    draft: dict[str, Any],
    leg: dict[str, Any],
) -> dict[str, Any]:
    """Convert one internal market leg to Deepcoin's place-order payload."""

    quantity = leg.get("quantity")
    if not isinstance(quantity, int | float) or quantity <= 0:
        raise RecoveryLiveSubmitError("non_positive_quantity")

    return {
        "instId": str(draft["instrument_id"]),
        "tdMode": _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross")),
        "side": str(leg["side"]).lower(),
        "posSide": str(leg["position_side"]).lower(),
        "ordType": "market",
        "sz": str(quantity),
        "clOrdId": str(leg.get("client_order_id") or ""),
        "mrgPosition": _deepcoin_position_mode(str(draft.get("position_mode") or "split")),
    }


def build_deepcoin_trigger_order_payload(
    draft: dict[str, Any],
    leg: dict[str, Any],
) -> dict[str, Any]:
    """Convert one internal limit leg to Deepcoin's trigger-order payload."""

    quantity = leg.get("quantity")
    if not isinstance(quantity, int | float) or quantity <= 0:
        raise RecoveryLiveSubmitError("non_positive_quantity")
    price = leg.get("price")
    if not isinstance(price, int | float) or price <= 0:
        raise RecoveryLiveSubmitError("non_positive_price")

    payload: dict[str, Any] = {
        "instId": str(draft["instrument_id"]),
        "productGroup": "Swap",
        "sz": str(quantity),
        "side": str(leg["side"]).lower(),
        "posSide": str(leg["position_side"]).lower(),
        "price": str(price),
        "isCrossMargin": (
            "1"
            if _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross")) == "cross"
            else "0"
        ),
        "orderType": "limit",
        "triggerPrice": str(price),
        "triggerPxType": "last",
        "mrgPosition": _deepcoin_position_mode(str(draft.get("position_mode") or "split")),
        "tdMode": _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross")),
    }
    if leg.get("client_order_id"):
        payload["clOrdId"] = str(leg.get("client_order_id"))
    take_profit_leg = leg.get("take_profit_leg") if isinstance(leg.get("take_profit_leg"), dict) else None
    payload.update(_deepcoin_embedded_sltp_fields(draft, take_profit_leg=take_profit_leg))
    return payload


def build_deepcoin_position_sltp_payload(
    draft: dict[str, Any],
    *,
    pos_id: str | None,
) -> dict[str, Any]:
    """Convert an internal draft to Deepcoin's position TP/SL payload."""

    stop_loss = draft.get("stop_loss")
    if not isinstance(stop_loss, int | float) or stop_loss <= 0:
        raise RecoveryLiveSubmitError("missing_stop_loss_for_protection")
    take_profit_price = _first_take_profit_price(draft)

    payload: dict[str, Any] = {
        "instType": "SWAP",
        "instId": str(draft["instrument_id"]),
        "posSide": _position_side_from_draft(draft),
        "mrgPosition": _deepcoin_position_mode(str(draft.get("position_mode") or "split")),
        "tdMode": _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross")),
        "slTriggerPx": str(stop_loss),
        "slTriggerPxType": "last",
        "slOrdPx": "-1",
    }
    if take_profit_price is not None:
        payload.update(
            {
                "tpTriggerPx": str(take_profit_price),
                "tpTriggerPxType": "last",
                "tpOrdPx": "-1",
            }
        )
    if payload["mrgPosition"] == "split":
        if not pos_id:
            raise RecoveryLiveSubmitError("missing_pos_id_for_split_position_sltp")
        payload["posId"] = str(pos_id)
    return payload


def build_deepcoin_position_sltp_payloads(
    draft: dict[str, Any],
    *,
    pos_id: str | None,
    position_size: float,
    include_take_profit: bool = True,
) -> list[dict[str, Any]]:
    """Build one full-position SL payload plus partial TP payloads when needed."""

    base_payload = _deepcoin_position_sltp_base_payload(draft, pos_id=pos_id)
    payloads: list[dict[str, Any]] = []
    stop_loss = draft.get("stop_loss")
    if not isinstance(stop_loss, int | float) or stop_loss <= 0:
        raise RecoveryLiveSubmitError("missing_stop_loss_for_protection")
    if not include_take_profit:
        return [{
            **base_payload,
            "slTriggerPx": str(stop_loss),
            "slTriggerPxType": "last",
            "slOrdPx": "-1",
        }]
    take_profit_legs = draft.get("take_profit_legs")
    if not isinstance(take_profit_legs, list) or len(take_profit_legs) <= 1:
        return [build_deepcoin_position_sltp_payload(draft, pos_id=pos_id)]
    payloads.append(
        {
            **base_payload,
            "slTriggerPx": str(stop_loss),
            "slTriggerPxType": "last",
            "slOrdPx": "-1",
        }
    )
    valid_legs = [item for item in take_profit_legs if isinstance(item, dict)]
    try:
        plan = build_take_profit_plan(
            prices=[item.get("price") for item in valid_legs],
            side=_position_side_from_draft(draft),
            configured_allocations=[item.get("allocation_pct") for item in valid_legs],
            quantity=position_size,
            quantity_step=_quantity_step_from_draft(draft),
            minimum_quantity=_minimum_quantity_from_draft(draft),
        )
    except TakeProfitPlanError as exc:
        raise RecoveryLiveSubmitError(str(exc)) from exc
    for take_profit_leg in plan.legs:
        payloads.append(
            {
                **base_payload,
                "tpTriggerPx": str(float(take_profit_leg.price)),
                "tpTriggerPxType": "last",
                "tpOrdPx": "-1",
                "sz": str(take_profit_leg.quantity),
            }
        )
    if not payloads:
        raise RecoveryLiveSubmitError("missing_tpsl_for_protection")
    return payloads


def _deepcoin_position_sltp_base_payload(
    draft: dict[str, Any],
    *,
    pos_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "instType": "SWAP",
        "instId": str(draft["instrument_id"]),
        "posSide": _position_side_from_draft(draft),
        "mrgPosition": _deepcoin_position_mode(str(draft.get("position_mode") or "split")),
        "tdMode": _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross")),
    }
    if payload["mrgPosition"] == "split":
        if not pos_id:
            raise RecoveryLiveSubmitError("missing_pos_id_for_split_position_sltp")
        payload["posId"] = str(pos_id)
    return payload


def _deepcoin_embedded_sltp_fields(
    draft: dict[str, Any],
    take_profit_leg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return stop-only trigger protection; TP waits for exact filled ``posId``."""

    del take_profit_leg
    stop_loss = draft.get("stop_loss")
    if not isinstance(stop_loss, int | float) or stop_loss <= 0:
        raise RecoveryLiveSubmitError("missing_stop_loss_for_protection")
    return {
        "slTriggerPx": str(stop_loss),
        "slTriggerPxType": "last",
        "slOrdPx": "-1",
    }


def _first_take_profit_price(draft: dict[str, Any]) -> float | None:
    take_profit_legs = draft.get("take_profit_legs")
    if not isinstance(take_profit_legs, list) or not take_profit_legs:
        return None
    first_take_profit = take_profit_legs[0]
    if not isinstance(first_take_profit, dict):
        raise RecoveryLiveSubmitError("invalid_take_profit_for_protection")
    take_profit_price = first_take_profit.get("price")
    if take_profit_price in (None, ""):
        return None
    if not isinstance(take_profit_price, int | float) or take_profit_price <= 0:
        raise RecoveryLiveSubmitError("invalid_take_profit_for_protection")
    return float(take_profit_price)


def _protection_warnings(draft: dict[str, Any]) -> list[str]:
    return []


def _position_side_from_draft(draft: dict[str, Any]) -> str:
    order_legs = draft.get("order_legs")
    if isinstance(order_legs, list):
        for leg in order_legs:
            if isinstance(leg, dict) and leg.get("position_side"):
                return str(leg["position_side"]).lower()
    source_side = str((draft.get("source") or {}).get("side") or "").lower()
    return source_side if source_side in {"long", "short"} else "long"


def _deepcoin_margin_mode(value: str) -> str:
    return "cross" if value.lower() in {"cross", "crossed", "full", "全仓"} else "isolated"


def _deepcoin_position_mode(value: str) -> str:
    return "split" if value.lower() in {"split", "hedge", "long_short", "分仓"} else "merge"


def _cancel_unprotected_order(
    deepcoin_client: DeepcoinTradingClientProtocol,
    *,
    draft: dict[str, Any],
    order_id: str | None,
    client_order_id: str | None,
) -> None:
    payload: dict[str, Any] = {
        "instId": str(draft["instrument_id"]),
        "mrgPosition": _deepcoin_position_mode(str(draft.get("position_mode") or "split")),
    }
    if order_id:
        payload["ordId"] = str(order_id)
    if client_order_id:
        payload["clOrdId"] = str(client_order_id)
    try:
        deepcoin_client.cancel_order(payload)
    except Exception as exc:  # pragma: no cover - best-effort exchange cleanup
        raise DeepcoinClientError(
            f"Deepcoin protection failed and cancel also failed: {exc}"
        ) from exc


def _find_open_position_id(
    deepcoin_client: DeepcoinTradingClientProtocol,
    *,
    draft: dict[str, Any],
    side: str,
    preferred_pos_id: str | None = None,
    exclude_pos_ids: set[str] | None = None,
    attempts: int = 5,
    delay_seconds: float = 0.5,
) -> str | None:
    for attempt in range(attempts):
        try:
            positions = deepcoin_client.list_positions(inst_id=str(draft["instrument_id"]))
        except Exception:
            positions = []
        position = _select_matching_position(
            positions,
            draft=draft,
            side=side,
            preferred_pos_id=preferred_pos_id,
            exclude_pos_ids=exclude_pos_ids,
        )
        pos_id = _first_payload_string(position, "posId", "pos_id", "id") if position else None
        if pos_id:
            return pos_id
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    return None


def _load_matching_position_ids(
    deepcoin_client: DeepcoinTradingClientProtocol,
    *,
    draft: dict[str, Any],
    side: str,
) -> set[str]:
    try:
        positions = deepcoin_client.list_positions(inst_id=str(draft["instrument_id"]))
    except Exception as exc:
        # A market entry may share symbol and side with an older position.  Without
        # a successful pre-submit snapshot, a later lookup cannot prove which
        # position was created by this order, so fail before placing the order.
        raise RecoveryLiveSubmitError(
            "pre_submit_position_snapshot_unavailable"
        ) from exc
    result: set[str] = set()
    for position in _matching_positions(positions, draft=draft, side=side):
        pos_id = _first_payload_string(position, "posId", "pos_id", "id")
        if pos_id:
            result.add(pos_id)
    return result


def _select_matching_position(
    positions: list[dict[str, Any]],
    *,
    draft: dict[str, Any],
    side: str,
    preferred_pos_id: str | None = None,
    exclude_pos_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    matches = _matching_positions(positions, draft=draft, side=side)
    if exclude_pos_ids is not None:
        matches = [
            match
            for match in matches
            if _first_payload_string(match, "posId", "pos_id", "id") not in exclude_pos_ids
        ]
    if not matches:
        return None
    if preferred_pos_id:
        for match in matches:
            if _first_payload_string(match, "posId", "pos_id", "id") == str(preferred_pos_id):
                return match
    if len(matches) != 1:
        return None
    return matches[0]


def _matching_positions(
    positions: list[dict[str, Any]],
    *,
    draft: dict[str, Any],
    side: str,
) -> list[dict[str, Any]]:
    instrument_id = str(draft["instrument_id"]).upper()
    margin_mode = _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross"))
    position_mode = _deepcoin_position_mode(str(draft.get("position_mode") or "split"))
    matches = []
    for position in positions:
        if str(position.get("instId") or "").upper() != instrument_id:
            continue
        if str(position.get("posSide") or "").lower() != side.lower():
            continue
        if str(position.get("mrgPosition") or position.get("posMode") or "").lower() not in {
            "",
            position_mode,
        }:
            continue
        if str(position.get("mgnMode") or position.get("tdMode") or "").lower() not in {
            "",
            margin_mode,
        }:
            continue
        try:
            size = abs(float(position.get("pos") or position.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            continue
        matches.append(position)
    return sorted(
        matches,
        key=lambda item: int(float(item.get("uTime") or item.get("cTime") or 0)),
        reverse=True,
    )


def _first_payload_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _extract_order_id(response: dict[str, Any]) -> str | None:
    for payload in _response_payloads(response):
        for key in ("ordId", "orderId", "order_id", "id", "orderSysID", "OrderSysID"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _extract_exact_market_order_id(response: dict[str, Any]) -> str | None:
    return _extract_endpoint_entry_order_id(
        response,
        fields=("ordId",),
    )


def _extract_exact_trigger_order_id(response: dict[str, Any]) -> str | None:
    return _extract_endpoint_entry_order_id(
        response,
        fields=("ordId",),
    )


def _extract_endpoint_entry_order_id(
    response: dict[str, Any],
    *,
    fields: tuple[str, ...],
) -> str | None:
    for payload in _response_payloads(response):
        for field in fields:
            order_id = payload.get(field)
            if (
                isinstance(order_id, str)
                and order_id
                and order_id.strip() == order_id
            ):
                return order_id
    return None


def _extract_position_id(response: dict[str, Any]) -> str | None:
    for payload in _response_payloads(response):
        for key in ("posId", "pos_id", "positionId"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _join_ids(values) -> str | None:
    items = [str(value) for value in values if value not in (None, "")]
    return ",".join(items) if items else None


def _response_payloads(response: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = [response]
    data = response.get("data")
    if isinstance(data, dict):
        payloads.append(data)
    elif isinstance(data, list):
        payloads.extend(item for item in data if isinstance(item, dict))
    return payloads
