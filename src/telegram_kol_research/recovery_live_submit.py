"""Live Deepcoin recovery order submission after explicit confirmation."""

from __future__ import annotations

import math
import time
import hashlib
import json
from copy import deepcopy
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from threading import RLock
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import DeepcoinClientError
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
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import ExecutionOrderLegRecord
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.execution_bindings import upsert_execution_order_leg
from telegram_kol_research.execution_events import ExecutionEventRecord
from telegram_kol_research.execution_events import record_execution_event
from telegram_kol_research.models import EntryStrategyAssembly
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import ExecutionEvent
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.models import InstructionExecutionContract
from telegram_kol_research.models import StrategyRevisionBatch
from telegram_kol_research.models import StrategyRevisionLeg
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.models import TriggerProtectionIntent
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.position_protection_legs import (
    bind_parent_entry_order,
    create_or_get_protection_leg,
)
from telegram_kol_research.position_mutation_gateway import (
    exact_position_write_gate,
    submit_exact_position_sltp,
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
from telegram_kol_research.trade_signals import list_pending_trade_signals
from telegram_kol_research.trade_signals import load_or_create_trade_signal
from telegram_kol_research.trade_signals import load_trade_signal
from telegram_kol_research.trade_signals import mark_trade_signal_failed
from telegram_kol_research.trade_signals import mark_trade_signal_submitted
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
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
        mark_trade_signal_failed(
            session_factory,
            signal_id=signal_id,
            error=str(failure),
            failed_at=execution_boundary_at,
            expected_status="processing",
            terminal_status=_entry_submission_failure_status(
                failure,
                progress=submission_progress,
            ),
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


@serialized_position_authority_mutation
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
    leg_index_offset = int(draft.get("_entry_leg_index_offset") or 0)
    for source_index, leg in zip(
        submission_indices,
        submission_order_legs,
        strict=True,
    ):
        index = leg_index_offset + int(source_index)
        if not isinstance(leg, dict):
            raise RecoveryLiveSubmitError("invalid_order_leg")
        order_type = str(leg.get("order_type") or "").lower()
        if order_type == "market":
            pre_submit_position_ids = _load_matching_position_ids(
                deepcoin_client,
                draft=draft,
                side=side_key,
            )
            order_payload = build_deepcoin_market_order_payload(draft, leg)
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
            client_order_id = str(leg.get("client_order_id") or order_payload.get("clOrdId") or "")
            pos_id = _extract_position_id(response) or _find_open_position_id(
                deepcoin_client,
                draft=draft,
                side=side_key,
                exclude_pos_ids=pre_submit_position_ids,
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
            try:
                protection_payloads = build_deepcoin_position_sltp_payloads(
                    draft,
                    pos_id=pos_id,
                    position_size=float(leg.get("quantity") or 0),
                    include_take_profit=False,
                )
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
            except Exception as exc:  # pragma: no cover - defensive boundary
                protection_payload = locals().get("protection_payloads") or locals().get("protection_payload")
                protection_response = {"error": str(exc)}
                warnings.append("position_protection_failed_after_entry_submitted")
        elif order_type == "limit":
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
) -> dict[str, Any]:
    """Snapshot, persist intent, submit parent, and bind its returned identity."""

    inst_id = str(order_payload.get("instId") or "").upper()
    side = str(order_payload.get("posSide") or order_payload.get("side") or "").lower()
    if not inst_id or not side:
        raise RecoveryLiveSubmitError("missing_trigger_protection_identity")
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
