"""Fail-closed gateway for exact, durable Deepcoin position writes."""

from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import (
    DeepcoinDefiniteRejection,
    DeepcoinPreSendUnavailable,
    DeepcoinReadUnavailable,
    DeepcoinRequestOutcomeUnknown,
)
from telegram_kol_research.deepcoin_execution_operations import (
    contains_credential_marker,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionMutationIntent,
    PositionProtectionLedger,
)
from telegram_kol_research.position_mutation_authority import (
    PositionMutationAuthority,
    PositionMutationAuthorityError,
    ProtectionOrderOwner,
    build_position_mutation_authority,
    position_authority_fingerprint,
    require_order_owned_by_authority,
)
from telegram_kol_research.position_mutation_intents import (
    PositionMutationIntentError,
    bound_set_position_authority_fingerprint,
    load_validated_set_position_request,
    reserve_position_mutation_intent,
    transition_position_mutation_intent,
)
from telegram_kol_research.deepcoin_snapshot_authority import (
    DeepcoinSnapshotUnavailable,
    build_exchange_collection_evidence,
    require_complete_collection,
)
from telegram_kol_research.position_attribution import (
    PositionAttributionError,
    require_verified_position_ownership,
)
from telegram_kol_research.protection_ledger import (
    upsert_protection_ledger_row,
)


_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9][a-z0-9_]{0,127}$")
_SAFE_EXCHANGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_SAFE_RESPONSE_CODE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass(frozen=True, slots=True)
class PositionMutationResult:
    status: str
    reason: str | None
    intent_id: int
    response: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PreparedPositionSltpIntent:
    intent_id: int
    request_fingerprint: str


class PositionMutationGateway:
    """Authorize, reserve, revalidate, and submit an exact exchange mutation."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker,
        deepcoin_client: Any,
        live_execution_gate: Callable[[], bool],
        now_provider: Callable[[], Any],
        before_exchange_write: Callable[[], None] | None = None,
        after_exchange_write: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = deepcoin_client
        self._live_execution_gate = live_execution_gate
        self._now = now_provider
        self._before_exchange_write = before_exchange_write
        self._after_exchange_write = after_exchange_write

    def cancel_owned_position_sltp(
        self,
        *,
        authority: PositionMutationAuthority,
        order_id: str,
        idempotency_key: str,
        before_submit: Callable[[int], None] | None = None,
        retry_pending_order: Mapping[str, Any] | None = None,
    ) -> PositionMutationResult:
        payload = {
            "instType": "SWAP",
            "instId": authority.instrument_id,
            "ordId": order_id,
        }
        intent = reserve_position_mutation_intent(
            self._session_factory,
            idempotency_key=idempotency_key,
            operation="cancel_position_sltp",
            strategy_instance_id=authority.strategy_instance_id,
            execution_binding_id=authority.execution_binding_id,
            execution_order_leg_id=authority.execution_order_leg_id,
            pos_id=authority.pos_id,
            order_id=order_id,
            authority_fingerprint=_authority_fingerprint(authority),
            request_fingerprint=_fingerprint(payload),
            request=payload,
            reserved_at=self._now(),
            venue=authority.venue,
        )
        intent_id = int(intent.id)
        current = self._intent_result(intent_id)
        if current.status == "rejected" and retry_pending_order is not None:
            # Re-arm only from fresh exact pending-order proof, never from a
            # caller assertion or a stale component snapshot.
            if not _pending_cancel_retry_matches_authority(
                retry_pending_order,
                authority=authority,
                order_id=order_id,
            ):
                return current
            # Re-arm the same exchange-write identity instead of creating a
            # second ambiguous cancel intent for the same order.
            transitioned = transition_position_mutation_intent(
                self._session_factory,
                intent_id,
                expected_statuses={"rejected"},
                new_status="reserved",
                transitioned_at=self._now(),
            )
            if transitioned:
                current = self._intent_result(intent_id)
        if current.status != "reserved":
            return current

        # The component orchestrator uses this hook to durably link the
        # reserved intent and enter its protected ``submitting`` state before
        # this gateway can make an exchange write.  A persistence failure here
        # deliberately leaves the intent reserved and performs no write.
        if before_submit is not None:
            before_submit(intent_id)

        reason = self._validate_persisted_authority(
            authority=authority,
            order_id=order_id,
        )
        if reason is not None:
            return self._block(intent_id, reason)

        positions = self._client.list_positions(inst_id=authority.instrument_id)
        matches = [
            row
            for row in positions
            if str(row.get("posId") or "") == authority.pos_id
        ]
        if len(matches) != 1:
            return self._block(intent_id, "position_not_unique")
        if position_authority_fingerprint(matches[0]) != authority.position_fingerprint:
            return self._block(intent_id, "position_fingerprint_changed")

        if not self._live_execution_gate():
            return self._block(intent_id, "live_execution_disabled")

        transitioned = transition_position_mutation_intent(
            self._session_factory,
            intent_id,
            expected_statuses={"reserved"},
            new_status="submitting",
            transitioned_at=self._now(),
        )
        if not transitioned:
            return self._intent_result(intent_id)

        try:
            response = self._call_client_write(
                "_cancel_position_sltp_unchecked",
                "cancel_position_sltp",
                payload,
            )
        except DeepcoinDefiniteRejection as exc:
            return self._finish_with_error(intent_id, "rejected", str(exc))
        except DeepcoinRequestOutcomeUnknown as exc:
            return self._finish_with_error(
                intent_id, "recovery_required", str(exc)
            )
        except Exception as exc:
            return self._finish_with_error(
                intent_id, "recovery_required", str(exc)
            )

        persisted = transition_position_mutation_intent(
            self._session_factory,
            intent_id,
            expected_statuses={"submitting"},
            new_status="submitted",
            transitioned_at=self._now(),
            response=response,
        )
        if not persisted:
            return self._intent_result(intent_id)
        return PositionMutationResult(
            status="submitted",
            reason=None,
            intent_id=intent_id,
            response=response,
        )

    def set_exact_position_sltp(
        self,
        *,
        authority: PositionMutationAuthority,
        purpose: str,
        ledger_purpose: str | None = None,
        trigger_price: str,
        size: str | None,
        idempotency_key: str,
        request_options: Mapping[str, Any] | None = None,
        before_submit: Callable[[int], None] | None = None,
        before_exchange_submit: Callable[[int], None] | None = None,
        expected_intent_id: int | None = None,
        propagate_outcome_unknown: bool = False,
        pre_submit_order_refs_provider: Callable[[], list[str]] | None = None,
    ) -> PositionMutationResult:
        trigger_field_by_purpose = {
            "stop_loss": "slTriggerPx",
            "take_profit": "tpTriggerPx",
        }
        if purpose not in trigger_field_by_purpose:
            raise ValueError("position_sltp_purpose_invalid")
        protected_identity = str(idempotency_key).startswith(
            "protected-entry:"
        )
        effective_order_refs_provider = pre_submit_order_refs_provider
        if protected_identity and effective_order_refs_provider is None:
            effective_order_refs_provider = lambda: (
                _complete_pending_tpsl_order_refs(
                    self._client,
                    instrument_id=authority.instrument_id,
                )
            )
        binding, reason = self._load_verified_binding(authority)
        if reason is not None or binding is None:
            payload = {
                "instType": "SWAP",
                "instId": authority.instrument_id,
                "posSide": authority.side,
                "mrgPosition": "",
                "posId": authority.pos_id,
                "tdMode": "",
                trigger_field_by_purpose[purpose]: str(trigger_price),
            }
            if size is not None:
                payload["sz"] = str(size)
            return self._reserve_and_block(
                authority=authority,
                operation="set_position_sltp",
                order_id=None,
                payload=payload,
                idempotency_key=idempotency_key,
                reason=reason or "execution_binding_missing",
            )
        payload = _exact_position_sltp_payload(
            authority=authority,
            binding=binding,
            purpose=purpose,
            trigger_price=trigger_price,
            size=size,
            request_options=request_options,
        )
        return self._submit_exact_position_write(
            authority=authority,
            operation="set_position_sltp",
            order_id=None,
            payload=payload,
            persisted_request={
                **payload,
                "_ledger_purpose": ledger_purpose or purpose,
                **(
                    {
                        "_base_authority_fingerprint":
                        _authority_fingerprint(authority)
                    }
                    if effective_order_refs_provider is not None
                    else {}
                ),
            },
            idempotency_key=idempotency_key,
            before_submit=before_submit,
            before_exchange_submit=before_exchange_submit,
            expected_intent_id=expected_intent_id,
            propagate_outcome_unknown=propagate_outcome_unknown,
            pre_submit_order_refs_provider=effective_order_refs_provider,
            submit=lambda: self._call_client_write(
                "_set_position_sltp_unchecked",
                "set_position_sltp",
                payload,
            ),
        )

    def close_exact_position(
        self,
        *,
        authority: PositionMutationAuthority,
        size: str,
        client_order_id: str | None,
        idempotency_key: str,
        before_submit: Callable[[int], None] | None = None,
    ) -> PositionMutationResult:
        binding, reason = self._load_verified_binding(authority)
        if reason is not None or binding is None:
            payload = {
                "instId": authority.instrument_id,
                "tdMode": "",
                "side": "",
                "posSide": authority.side,
                "ordType": "market",
                "sz": str(size),
                "mrgPosition": "",
                "closePosId": authority.pos_id,
                "clOrdId": client_order_id,
            }
            return self._reserve_and_block(
                authority=authority,
                operation="close_position",
                order_id=None,
                payload=payload,
                idempotency_key=idempotency_key,
                reason=reason or "execution_binding_missing",
            )
        close_side = "sell" if authority.side.lower() == "long" else "buy"
        payload = {
            "instId": authority.instrument_id,
            "tdMode": str(binding.margin_mode).lower(),
            "side": close_side,
            "posSide": authority.side.lower(),
            "ordType": "market",
            "sz": str(size),
            "mrgPosition": str(binding.position_mode).lower(),
            "closePosId": authority.pos_id,
        }
        if client_order_id:
            payload["clOrdId"] = client_order_id
        return self._submit_exact_position_write(
            authority=authority,
            operation="close_position",
            order_id=None,
            payload=payload,
            idempotency_key=idempotency_key,
            before_submit=before_submit,
            submit=lambda: self._call_client_write(
                "_place_position_close_unchecked",
                "place_order",
                payload,
            ),
        )

    def _submit_exact_position_write(
        self,
        *,
        authority: PositionMutationAuthority,
        operation: str,
        order_id: str | None,
        payload: Mapping[str, Any],
        persisted_request: Mapping[str, Any] | None = None,
        idempotency_key: str,
        submit: Callable[[], Mapping[str, Any]],
        before_submit: Callable[[int], None] | None = None,
        before_exchange_submit: Callable[[int], None] | None = None,
        expected_intent_id: int | None = None,
        propagate_outcome_unknown: bool = False,
        pre_submit_order_refs_provider: Callable[[], list[str]] | None = None,
    ) -> PositionMutationResult:
        intent = reserve_position_mutation_intent(
            self._session_factory,
            idempotency_key=idempotency_key,
            operation=operation,
            strategy_instance_id=authority.strategy_instance_id,
            execution_binding_id=authority.execution_binding_id,
            execution_order_leg_id=authority.execution_order_leg_id,
            pos_id=authority.pos_id,
            order_id=order_id,
            authority_fingerprint=_authority_fingerprint(authority),
            request_fingerprint=_fingerprint(payload),
            request=persisted_request or payload,
            reserved_at=self._now(),
            venue=authority.venue,
        )
        intent_id = int(intent.id)
        if (
            expected_intent_id is not None
            and intent_id != int(expected_intent_id)
        ):
            raise PositionMutationAuthorityError(
                "position_mutation_intent_identity_changed"
            )
        current = self._intent_result(intent_id)
        if current.status != "reserved":
            return current

        if operation == "close_position" and self._has_other_unresolved_close(
            pos_id=authority.pos_id,
            execution_order_leg_id=authority.execution_order_leg_id,
            exclude_intent_id=intent_id,
        ):
            return self._block(intent_id, "position_mutation_unresolved")

        if before_submit is not None:
            before_submit(intent_id)

        try:
            _, reason = self._load_verified_binding(authority)
            if reason is not None:
                return self._block(intent_id, reason)
            positions = self._client.list_positions(inst_id=authority.instrument_id)
            matches = [
                row
                for row in positions
                if str(row.get("posId") or "") == authority.pos_id
            ]
            if len(matches) != 1:
                return self._block(intent_id, "position_not_unique")
            if (
                position_authority_fingerprint(matches[0])
                != authority.position_fingerprint
            ):
                return self._block(intent_id, "position_fingerprint_changed")
            if not self._live_execution_gate():
                return self._block(intent_id, "live_execution_disabled")
            if pre_submit_order_refs_provider is not None:
                _attach_pre_submit_order_refs(
                    self._session_factory,
                    intent_id=intent_id,
                    request_fingerprint=str(intent.request_fingerprint),
                    order_refs=pre_submit_order_refs_provider(),
                )
        except DeepcoinReadUnavailable as exc:
            transitioned = transition_position_mutation_intent(
                self._session_factory,
                intent_id,
                expected_statuses={"reserved"},
                new_status="not_sent",
                transitioned_at=self._now(),
                error={
                    "safe_code": _safe_exchange_error_code(
                        exc, "position_mutation_not_sent"
                    )
                },
            )
            if not transitioned:
                raise PositionMutationAuthorityError(
                    "position_mutation_intent_state_conflict"
                ) from None
            raise
        if not transition_position_mutation_intent(
            self._session_factory,
            intent_id,
            expected_statuses={"reserved"},
            new_status="submitting",
            transitioned_at=self._now(),
        ):
            return self._intent_result(intent_id)
        try:
            if self._before_exchange_write is not None:
                self._before_exchange_write()
            try:
                if before_exchange_submit is not None:
                    before_exchange_submit(intent_id)
                response = submit()
            finally:
                if self._after_exchange_write is not None:
                    self._after_exchange_write()
        except DeepcoinPreSendUnavailable as exc:
            transitioned = transition_position_mutation_intent(
                self._session_factory,
                intent_id,
                expected_statuses={"submitting"},
                new_status="not_sent",
                transitioned_at=self._now(),
                error={
                    "safe_code": _safe_exchange_error_code(
                        exc, "position_mutation_not_sent"
                    )
                },
            )
            if not transitioned:
                raise PositionMutationAuthorityError(
                    "position_mutation_intent_state_conflict"
                ) from None
            raise
        except DeepcoinDefiniteRejection as exc:
            return self._finish_with_error(
                intent_id,
                "rejected",
                _safe_exchange_error_code(exc, "business_rejected"),
            )
        except DeepcoinRequestOutcomeUnknown as exc:
            result = self._finish_with_error(
                intent_id,
                "recovery_required",
                _safe_exchange_error_code(exc, "writer_outcome_unknown"),
            )
            if propagate_outcome_unknown:
                raise DeepcoinRequestOutcomeUnknown(
                    result.reason or "writer_outcome_unknown"
                ) from None
            return result
        except Exception:
            self._finish_with_error(
                intent_id,
                "recovery_required",
                "position_mutation_writer_failed",
            )
            raise DeepcoinRequestOutcomeUnknown(
                "position_mutation_writer_failed"
            ) from None
        response = _safe_mutation_success_response(response)
        persisted = transition_position_mutation_intent(
            self._session_factory,
            intent_id,
            expected_statuses={"submitting"},
            new_status="submitted",
            transitioned_at=self._now(),
            response=response,
        )
        if not persisted:
            return self._intent_result(intent_id)
        return PositionMutationResult(
            status="submitted",
            reason=None,
            intent_id=intent_id,
            response=response,
        )

    def _has_other_unresolved_close(
        self, *, pos_id: str, execution_order_leg_id: int,
        exclude_intent_id: int,
    ) -> bool:
        with self._session_factory() as session:
            return session.query(PositionMutationIntent.id).filter(
                PositionMutationIntent.pos_id == str(pos_id),
                PositionMutationIntent.execution_order_leg_id
                == int(execution_order_leg_id),
                PositionMutationIntent.operation == "close_position",
                PositionMutationIntent.id != int(exclude_intent_id),
                PositionMutationIntent.status.in_((
                    "reserved", "submitting", "submitted", "recovery_required"
                )),
            ).first() is not None

    def _reserve_and_block(
        self,
        *,
        authority: PositionMutationAuthority,
        operation: str,
        order_id: str | None,
        payload: Mapping[str, Any],
        idempotency_key: str,
        reason: str,
    ) -> PositionMutationResult:
        intent = reserve_position_mutation_intent(
            self._session_factory,
            idempotency_key=idempotency_key,
            operation=operation,
            strategy_instance_id=authority.strategy_instance_id,
            execution_binding_id=authority.execution_binding_id,
            execution_order_leg_id=authority.execution_order_leg_id,
            pos_id=authority.pos_id,
            order_id=order_id,
            authority_fingerprint=_authority_fingerprint(authority),
            request_fingerprint=_fingerprint(payload),
            request=payload,
            reserved_at=self._now(),
            venue=authority.venue,
        )
        current = self._intent_result(int(intent.id))
        if current.status != "reserved":
            return current
        return self._block(int(intent.id), reason)

    def _load_verified_binding(
        self,
        authority: PositionMutationAuthority,
    ) -> tuple[ExecutionBinding | None, str | None]:
        with self._session_factory() as session:
            binding = session.get(
                ExecutionBinding, authority.execution_binding_id
            )
            if binding is None:
                return None, "execution_binding_missing"
            expected_instrument_prefix = f"{str(binding.symbol).upper()}-"
            if (
                (
                    str(binding.venue or "").lower(),
                    str(binding.strategy_instance_id or ""),
                    str(binding.side or "").lower(),
                )
                != (
                    authority.venue.lower(),
                    authority.strategy_instance_id,
                    authority.side.lower(),
                )
                or not authority.instrument_id.upper().startswith(
                    expected_instrument_prefix
                )
                or str(binding.status or "").lower()
                not in {"active", "open", "partial"}
            ):
                return None, "execution_binding_mismatch"
            leg = session.get(
                ExecutionOrderLeg, authority.execution_order_leg_id
            )
            if leg is None:
                return None, "execution_order_leg_missing"
            if (
                int(leg.execution_binding_id),
                str(leg.strategy_instance_id or ""),
                str(leg.venue or "").lower(),
                str(leg.pos_id or ""),
                str(leg.attribution_status or "").lower(),
            ) != (
                authority.execution_binding_id,
                authority.strategy_instance_id,
                authority.venue.lower(),
                authority.pos_id,
                "verified",
            ):
                return None, "execution_order_leg_mismatch"
            try:
                authoritative_leg = require_verified_position_ownership(
                    session,
                    venue=authority.venue,
                    pos_id=authority.pos_id,
                )
            except PositionAttributionError as exc:
                return None, str(exc)
            if (
                authoritative_leg.id != authority.execution_order_leg_id
                or authoritative_leg.purpose != "entry"
            ):
                return None, "execution_order_leg_mismatch"
            session.expunge(binding)
            return binding, None

    def _validate_persisted_authority(
        self,
        *,
        authority: PositionMutationAuthority,
        order_id: str,
    ) -> str | None:
        with self._session_factory() as session:
            binding = session.get(
                ExecutionBinding, authority.execution_binding_id
            )
            if binding is None:
                return "execution_binding_missing"
            expected_instrument_prefix = f"{str(binding.symbol).upper()}-"
            binding_facts = (
                str(binding.venue or "").lower(),
                str(binding.strategy_instance_id or ""),
                str(binding.side or "").lower(),
            )
            authority_facts = (
                authority.venue.lower(),
                authority.strategy_instance_id,
                authority.side.lower(),
            )
            if (
                binding_facts != authority_facts
                or not authority.instrument_id.upper().startswith(
                    expected_instrument_prefix
                )
                or str(binding.status or "").lower()
                not in {"active", "open", "partial"}
            ):
                return "execution_binding_mismatch"

            leg = session.get(
                ExecutionOrderLeg, authority.execution_order_leg_id
            )
            if leg is None:
                return "execution_order_leg_missing"
            leg_facts = (
                int(leg.execution_binding_id),
                str(leg.strategy_instance_id or ""),
                str(leg.venue or "").lower(),
                str(leg.pos_id or ""),
                str(leg.attribution_status or "").lower(),
            )
            expected_leg_facts = (
                authority.execution_binding_id,
                authority.strategy_instance_id,
                authority.venue.lower(),
                authority.pos_id,
                "verified",
            )
            if leg_facts != expected_leg_facts:
                return "execution_order_leg_mismatch"
            try:
                authoritative_leg = require_verified_position_ownership(
                    session,
                    venue=authority.venue,
                    pos_id=authority.pos_id,
                )
            except PositionAttributionError as exc:
                return str(exc)
            if (
                authoritative_leg.id != authority.execution_order_leg_id
                or authoritative_leg.purpose != "entry"
            ):
                return "execution_order_leg_mismatch"

            protection = (
                session.query(PositionProtectionLedger)
                .filter(
                    PositionProtectionLedger.venue
                    == authority.venue.lower(),
                    PositionProtectionLedger.order_id == order_id,
                )
                .one_or_none()
            )
            owner = None
            if protection is not None:
                if (
                    str(protection.status or "").lower() != "verified"
                    or protection.purpose
                    not in {"stop_loss", "backup_stop", "take_profit"}
                ):
                    return "protection_order_not_active"
                if (
                    authority.protection_fingerprint is not None
                    and authority.protection_fingerprint
                    != _protection_fingerprint(protection)
                ):
                    return "protection_fingerprint_changed"
                owner = ProtectionOrderOwner(
                    venue=protection.venue,
                    order_id=protection.order_id,
                    strategy_instance_id=str(
                        protection.strategy_instance_id or ""
                    ),
                    execution_binding_id=protection.execution_binding_id,
                    execution_order_leg_id=protection.execution_order_leg_id,
                    pos_id=protection.pos_id,
                    instrument_id=protection.instrument_id,
                    side=protection.side,
                )
            try:
                require_order_owned_by_authority(
                    authority=authority,
                    owner=owner,
                )
            except PositionMutationAuthorityError as exc:
                return str(exc)
        return None

    def _block(self, intent_id: int, reason: str) -> PositionMutationResult:
        transitioned = transition_position_mutation_intent(
            self._session_factory,
            intent_id,
            expected_statuses={"reserved"},
            new_status="blocked",
            transitioned_at=self._now(),
            error={"reason": reason},
        )
        if not transitioned:
            return self._intent_result(intent_id)
        return PositionMutationResult(
            status="blocked",
            reason=reason,
            intent_id=intent_id,
        )

    def _finish_with_error(
        self, intent_id: int, status: str, reason: str
    ) -> PositionMutationResult:
        transitioned = transition_position_mutation_intent(
            self._session_factory,
            intent_id,
            expected_statuses={"submitting"},
            new_status=status,
            transitioned_at=self._now(),
            error={"reason": reason},
        )
        if not transitioned:
            return self._intent_result(intent_id)
        return PositionMutationResult(
            status=status,
            reason=reason,
            intent_id=intent_id,
        )

    def _intent_result(self, intent_id: int) -> PositionMutationResult:
        with self._session_factory() as session:
            row = session.get(PositionMutationIntent, intent_id)
            if row is None:
                raise RuntimeError("position_mutation_intent_missing")
            error = _load_json(row.error_json)
            response = _load_json(row.response_json)
            return PositionMutationResult(
                status=row.status,
                reason=str(error.get("reason")) if error.get("reason") else None,
                intent_id=intent_id,
                response=response or None,
            )

    def _call_client_write(
        self,
        unchecked_name: str,
        compatibility_name: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        writer = getattr(self._client, unchecked_name, None)
        if writer is None:
            writer = getattr(self._client, compatibility_name)
        return writer(dict(payload))


def _authority_fingerprint(authority: PositionMutationAuthority) -> str:
    return _fingerprint(
        {
            "venue": authority.venue.lower(),
            "strategy_instance_id": authority.strategy_instance_id,
            "execution_binding_id": authority.execution_binding_id,
            "execution_order_leg_id": authority.execution_order_leg_id,
            "pos_id": authority.pos_id,
            "instrument_id": authority.instrument_id.upper(),
            "side": authority.side.lower(),
            "position_fingerprint": authority.position_fingerprint,
            "protection_fingerprint": authority.protection_fingerprint,
        }
    )


def _pending_cancel_retry_matches_authority(
    row: Mapping[str, Any],
    *,
    authority: PositionMutationAuthority,
    order_id: str,
) -> bool:
    observed_order_id = str(
        row.get("ordId") or row.get("orderId") or row.get("order_id") or ""
    )
    observed_side = str(row.get("posSide") or row.get("side") or "").lower()
    observed_side = {"buy": "long", "sell": "short"}.get(
        observed_side, observed_side
    )
    return (
        observed_order_id == str(order_id)
        and str(row.get("posId") or row.get("pos_id") or "")
        == authority.pos_id
        and str(row.get("instId") or row.get("instrument_id") or "").upper()
        == authority.instrument_id.upper()
        and observed_side == authority.side.lower()
        and str(row.get("triggerOrderType") or "TPSL").upper() == "TPSL"
    )


def _protection_fingerprint(row: PositionProtectionLedger) -> str:
    return _fingerprint(
        {
            "venue": str(row.venue or "").lower(),
            "order_id": str(row.order_id or ""),
            "strategy_instance_id": str(row.strategy_instance_id or ""),
            "execution_binding_id": row.execution_binding_id,
            "execution_order_leg_id": row.execution_order_leg_id,
            "pos_id": str(row.pos_id or ""),
            "instrument_id": str(row.instrument_id or "").upper(),
            "side": str(row.side or "").lower(),
            "purpose": str(row.purpose or ""),
            "trigger_price": str(row.trigger_price or ""),
            "size_text": str(row.size_text or ""),
            "status": str(row.status or "").lower(),
        }
    )


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_exchange_error_code(exc: BaseException, fallback: str) -> str:
    fact = getattr(exc, "fact", None)
    safe_code = getattr(fact, "safe_code", None)
    if isinstance(safe_code, str) and _SAFE_ERROR_CODE.fullmatch(safe_code):
        return safe_code
    return fallback


def _safe_mutation_success_response(
    response: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        return {}
    projected: dict[str, Any] = {}
    code = response.get("code")
    if code not in (None, ""):
        code_text = str(code)
        if _SAFE_RESPONSE_CODE.fullmatch(code_text):
            projected["code"] = code_text
    order_id = _response_order_id(response)
    if order_id is not None and _safe_exchange_identity(order_id):
        projected["data"] = {"ordId": order_id}
    return projected


def _safe_exchange_identity(value: str) -> bool:
    return bool(
        _SAFE_EXCHANGE_ID.fullmatch(value)
        and not contains_credential_marker(value)
    )


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _complete_pending_tpsl_order_refs(
    deepcoin_client: Any,
    *,
    instrument_id: str,
) -> list[str]:
    raw_reader = getattr(
        deepcoin_client, "read_trigger_orders_pending", None
    )
    list_reader = getattr(
        deepcoin_client, "list_trigger_orders_pending", None
    )
    if callable(raw_reader):
        response = raw_reader(inst_id=instrument_id)
    elif callable(list_reader):
        response = list_reader(inst_id=instrument_id)
    else:
        raise DeepcoinSnapshotUnavailable(
            "snapshot_reader_unavailable"
        )
    evidence = build_exchange_collection_evidence(
        endpoint="pending_trigger_orders",
        response=response,
    )
    rows = require_complete_collection(evidence)
    refs: set[str] = set()
    for row in rows:
        order_id = str(
            row.get("ordId")
            or row.get("orderId")
            or row.get("order_id")
            or ""
        )
        if not order_id or len(order_id) > 255:
            raise DeepcoinSnapshotUnavailable(
                "snapshot_order_identity_invalid"
            )
        refs.add(_protection_order_ref(order_id))
    return sorted(refs)


def _attach_pre_submit_order_refs(
    session_factory: sessionmaker,
    *,
    intent_id: int,
    request_fingerprint: str,
    order_refs: object,
) -> None:
    if (
        not isinstance(order_refs, list)
        or order_refs != sorted(set(order_refs))
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(
                character not in "0123456789abcdef"
                for character in value
            )
            for value in order_refs
        )
    ):
        raise PositionMutationAuthorityError(
            "position_sltp_baseline_invalid"
        )
    with session_factory() as session:
        intent = session.get(PositionMutationIntent, int(intent_id))
        if (
            intent is None
            or intent.operation != "set_position_sltp"
            or intent.status != "reserved"
            or intent.request_fingerprint != request_fingerprint
        ):
            raise PositionMutationAuthorityError(
                "position_mutation_intent_state_conflict"
            )
        original_request_json = intent.request_json
        try:
            request = load_validated_set_position_request(
                original_request_json,
                request_fingerprint=request_fingerprint,
                authority_fingerprint=str(intent.authority_fingerprint),
            )
        except PositionMutationIntentError as exc:
            raise PositionMutationAuthorityError(
                "position_mutation_intent_request_conflict"
            ) from exc
        request["_pre_submit_order_refs"] = list(order_refs)
        original_authority_fingerprint = str(
            intent.authority_fingerprint
        )
        bound_authority_fingerprint = (
            bound_set_position_authority_fingerprint(
                base_authority_fingerprint=str(
                    request["_base_authority_fingerprint"]
                ),
                ledger_purpose=str(request["_ledger_purpose"]),
                pre_submit_order_refs=list(order_refs),
            )
        )
        request_json = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        updated = (
            session.query(PositionMutationIntent)
            .filter(
                PositionMutationIntent.id == int(intent_id),
                PositionMutationIntent.status == "reserved",
                PositionMutationIntent.request_json
                == original_request_json,
                PositionMutationIntent.authority_fingerprint
                == original_authority_fingerprint,
            )
            .update(
                {
                    PositionMutationIntent.request_json: request_json,
                    PositionMutationIntent.authority_fingerprint:
                    bound_authority_fingerprint,
                    PositionMutationIntent.updated_at: datetime.now(UTC),
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            session.rollback()
            raise PositionMutationAuthorityError(
                "position_mutation_intent_state_conflict"
            )
        session.commit()


def _protection_order_ref(order_id: str) -> str:
    return hashlib.sha256(
        f"protection_order:{order_id}".encode("utf-8")
    ).hexdigest()


def _exact_position_sltp_payload(
    *,
    authority: PositionMutationAuthority,
    binding: ExecutionBinding,
    purpose: str,
    trigger_price: str,
    size: str | None,
    request_options: Mapping[str, Any] | None,
) -> dict[str, Any]:
    trigger_field = {
        "stop_loss": "slTriggerPx",
        "take_profit": "tpTriggerPx",
    }.get(purpose)
    if trigger_field is None:
        raise ValueError("position_sltp_purpose_invalid")
    payload: dict[str, Any] = {
        "instType": "SWAP",
        "instId": authority.instrument_id,
        "posSide": authority.side.lower(),
        "mrgPosition": str(binding.position_mode).lower(),
        "posId": authority.pos_id,
        "tdMode": str(binding.margin_mode).lower(),
        trigger_field: str(trigger_price),
    }
    if size is not None:
        payload["sz"] = str(size)
    allowed_options = {
        "tpTriggerPxType",
        "tpOrdPx",
        "slTriggerPxType",
        "slOrdPx",
    }
    payload.update(
        {
            key: value
            for key, value in dict(request_options or {}).items()
            if key in allowed_options and value not in (None, "")
        }
    )
    return payload


def prepare_exact_position_sltp_intent(
    *,
    session_factory: sessionmaker,
    deepcoin_client: Any,
    pos_id: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    now_provider: Callable[[], Any],
    ledger_purpose: str | None = None,
) -> PreparedPositionSltpIntent:
    """Reserve the exact final HTTP identity without making a writer call."""

    authority = _build_fresh_authority(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        pos_id=pos_id,
        instrument_id=str(payload.get("instId") or ""),
    )
    if payload.get("slTriggerPx") not in (None, ""):
        purpose, trigger_field = "stop_loss", "slTriggerPx"
    elif payload.get("tpTriggerPx") not in (None, ""):
        purpose, trigger_field = "take_profit", "tpTriggerPx"
    else:
        raise PositionMutationAuthorityError("position_sltp_trigger_missing")
    with session_factory() as session:
        binding = session.get(
            ExecutionBinding, int(authority.execution_binding_id)
        )
        if binding is None:
            raise PositionMutationAuthorityError(
                "execution_binding_missing"
            )
        final_payload = _exact_position_sltp_payload(
            authority=authority,
            binding=binding,
            purpose=purpose,
            trigger_price=str(payload[trigger_field]),
            size=(str(payload["sz"]) if "sz" in payload else None),
            request_options=payload,
        )
    intent = reserve_position_mutation_intent(
        session_factory,
        idempotency_key=idempotency_key,
        operation="set_position_sltp",
        strategy_instance_id=authority.strategy_instance_id,
        execution_binding_id=authority.execution_binding_id,
        execution_order_leg_id=authority.execution_order_leg_id,
        pos_id=authority.pos_id,
        order_id=None,
        authority_fingerprint=_authority_fingerprint(authority),
        request_fingerprint=_fingerprint(final_payload),
        request={
            **final_payload,
            "_ledger_purpose": ledger_purpose or purpose,
            "_base_authority_fingerprint": _authority_fingerprint(
                authority
            ),
        },
        reserved_at=now_provider(),
        venue=authority.venue,
    )
    return PreparedPositionSltpIntent(
        intent_id=int(intent.id),
        request_fingerprint=str(intent.request_fingerprint),
    )


def submit_exact_position_sltp(
    *,
    session_factory: sessionmaker,
    deepcoin_client: Any,
    pos_id: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    live_execution_gate: Callable[[], bool],
    now_provider: Callable[[], Any],
    require_readback: bool = False,
    ledger_purpose: str | None = None,
    before_submit: Callable[[int], None] | None = None,
    before_exchange_submit: Callable[[int], None] | None = None,
    readback_deadline_monotonic: float | None = None,
    monotonic_factory: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    readback_scope: Any = None,
    before_exchange_write: Callable[[], None] | None = None,
    after_exchange_write: Callable[[], None] | None = None,
    expected_intent_id: int | None = None,
    require_complete_readback_identity: bool = False,
) -> Mapping[str, Any]:
    """Compatibility adapter that rebuilds authority before one exact TPSL set."""

    authority = _build_fresh_authority(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        pos_id=pos_id,
        instrument_id=str(payload.get("instId") or ""),
    )
    if payload.get("slTriggerPx") not in (None, ""):
        purpose, trigger_field = "stop_loss", "slTriggerPx"
    elif payload.get("tpTriggerPx") not in (None, ""):
        purpose, trigger_field = "take_profit", "tpTriggerPx"
    else:
        raise PositionMutationAuthorityError("position_sltp_trigger_missing")
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        live_execution_gate=live_execution_gate,
        now_provider=now_provider,
        before_exchange_write=before_exchange_write,
        after_exchange_write=after_exchange_write,
    )
    result = gateway.set_exact_position_sltp(
        authority=authority,
        purpose=purpose,
        ledger_purpose=ledger_purpose or purpose,
        trigger_price=str(payload[trigger_field]),
        size=(str(payload["sz"]) if "sz" in payload else None),
        idempotency_key=idempotency_key,
        request_options=payload,
        before_submit=before_submit,
        before_exchange_submit=before_exchange_submit,
        expected_intent_id=expected_intent_id,
        propagate_outcome_unknown=True,
        pre_submit_order_refs_provider=(
            (
                lambda: _complete_pending_tpsl_order_refs(
                    deepcoin_client,
                    instrument_id=authority.instrument_id,
                )
            )
            if require_complete_readback_identity
            else None
        ),
    )
    if require_readback and result.status in {
        "submitting",
        "recovery_required",
    }:
        result = _poll_unidentified_position_sltp_readback(
            session_factory=session_factory,
            gateway=gateway,
            deepcoin_client=deepcoin_client,
            intent_id=result.intent_id,
            deadline_monotonic=readback_deadline_monotonic,
            monotonic_factory=monotonic_factory,
            sleep_fn=sleep_fn,
            readback_scope=readback_scope,
            instrument_id=authority.instrument_id,
        )
    response = _require_submitted_response(result)
    if not require_readback:
        return response
    order_id = _response_order_id(response)
    if not order_id or not _safe_exchange_identity(order_id):
        raise DeepcoinRequestOutcomeUnknown(
            "position_sltp_response_missing_order_id"
        )
    _poll_position_sltp_readback(
        deepcoin_client=deepcoin_client,
        authority=authority,
        order_id=order_id,
        purpose=purpose,
        trigger_price=str(payload[trigger_field]),
        size=(str(payload["sz"]) if "sz" in payload else None),
        deadline_monotonic=readback_deadline_monotonic,
        monotonic_factory=monotonic_factory,
        sleep_fn=sleep_fn,
        readback_scope=readback_scope,
        require_complete_identity=require_complete_readback_identity,
    )
    if result.status == "confirmed":
        with session_factory() as session:
            ledger = session.query(PositionProtectionLedger).filter(
                PositionProtectionLedger.venue == authority.venue,
                PositionProtectionLedger.order_id == order_id,
            ).one_or_none()
            if (
                ledger is None
                or ledger.status != "verified"
                or ledger.execution_binding_id != authority.execution_binding_id
                or ledger.execution_order_leg_id != authority.execution_order_leg_id
                or ledger.pos_id != authority.pos_id
                or ledger.purpose != (ledger_purpose or purpose)
                or not _decimal_values_equal(
                    str(ledger.trigger_price), str(payload[trigger_field])
                )
                or (
                    "sz" in payload
                    and not _decimal_values_equal(
                        str(ledger.size_text), str(payload["sz"])
                    )
                )
            ):
                raise DeepcoinRequestOutcomeUnknown(
                    "position_sltp_confirmed_ledger_mismatch"
                )
        return response
    confirmed_at = now_provider()
    try:
        with session_factory() as session:
            intent = session.get(PositionMutationIntent, result.intent_id)
            binding = session.get(
                ExecutionBinding, authority.execution_binding_id
            )
            if (
                intent is None
                or intent.status != "submitted"
                or binding is None
            ):
                raise DeepcoinRequestOutcomeUnknown(
                    "position_sltp_readback_persistence_state_changed"
                )
            upsert_protection_ledger_row(
                session,
                venue=authority.venue,
                execution_binding_id=authority.execution_binding_id,
                execution_order_leg_id=authority.execution_order_leg_id,
                strategy_instance_id=authority.strategy_instance_id,
                pos_id=authority.pos_id,
                instrument_id=authority.instrument_id,
                side=authority.side,
                order_id=order_id,
                purpose=ledger_purpose or purpose,
                trigger_price=str(payload[trigger_field]),
                size_text=(str(payload["sz"]) if "sz" in payload else None),
                status="verified",
                evidence_source="position_mutation_intent_readback",
                evidence={
                    "intent_id": int(intent.id),
                    "require_readback": True,
                },
                seen_at=confirmed_at,
            )
            intent.response_json = json.dumps(
                dict(response), ensure_ascii=False, sort_keys=True
            )
            _confirm_intent(
                intent,
                order_id=order_id,
                reconciled_at=confirmed_at,
            )
            session.commit()
    except DeepcoinRequestOutcomeUnknown:
        raise
    except Exception as exc:
        raise DeepcoinRequestOutcomeUnknown(
            "position_sltp_readback_ledger_persistence_failed"
        ) from exc
    return response


def _poll_position_sltp_readback(
    *,
    deepcoin_client: Any,
    authority: PositionMutationAuthority,
    order_id: str,
    purpose: str,
    trigger_price: str,
    size: str | None,
    deadline_monotonic: float | None,
    monotonic_factory: Callable[[], float],
    sleep_fn: Callable[[float], None],
    readback_scope: Any,
    require_complete_identity: bool,
) -> None:
    delays = (0.0, 0.5, 1.0, 2.0, 3.0) if deadline_monotonic is not None else (0.0,)
    last_unavailable: Exception | None = None
    attempted = False
    for delay in delays:
        now = float(monotonic_factory())
        if deadline_monotonic is not None:
            remaining = float(deadline_monotonic) - now
            if remaining <= 0 or delay >= remaining:
                break
        if delay > 0:
            sleep_fn(delay)
        attempted = True
        try:
            scope_factory = getattr(deepcoin_client, "request_scope", None)
            scope_context = (
                scope_factory(readback_scope)
                if readback_scope is not None and callable(scope_factory)
                else nullcontext(deepcoin_client)
            )
            with scope_context:
                pending = deepcoin_client.list_trigger_orders_pending(
                    inst_id=authority.instrument_id
                )
        except Exception as exc:
            last_unavailable = exc
            continue
        if _set_position_sltp_readback_matches(
            pending,
            order_id=order_id,
            authority=authority,
            purpose=purpose,
            trigger_price=trigger_price,
            size=size,
            require_complete_identity=require_complete_identity,
        ):
            return
    if last_unavailable is not None and not attempted:
        raise DeepcoinRequestOutcomeUnknown(
            "position_sltp_readback_unavailable"
        ) from last_unavailable
    if last_unavailable is not None:
        raise DeepcoinRequestOutcomeUnknown(
            "position_sltp_readback_unavailable"
        ) from last_unavailable
    raise DeepcoinRequestOutcomeUnknown("position_sltp_pending_readback")


def _poll_unidentified_position_sltp_readback(
    *,
    session_factory: sessionmaker,
    gateway: PositionMutationGateway,
    deepcoin_client: Any,
    intent_id: int,
    deadline_monotonic: float | None,
    monotonic_factory: Callable[[], float],
    sleep_fn: Callable[[float], None],
    readback_scope: Any,
    instrument_id: str,
) -> PositionMutationResult:
    delays = (
        (0.0, 0.5, 1.0, 2.0, 3.0)
        if deadline_monotonic is not None
        else (0.0,)
    )
    for delay in delays:
        now = float(monotonic_factory())
        if deadline_monotonic is not None:
            remaining = float(deadline_monotonic) - now
            if remaining <= 0 or delay >= remaining:
                break
        if delay:
            sleep_fn(delay)
        try:
            scope_factory = getattr(deepcoin_client, "request_scope", None)
            scope_context = (
                scope_factory(readback_scope)
                if readback_scope is not None and callable(scope_factory)
                else nullcontext(deepcoin_client)
            )
            with scope_context:
                pending = deepcoin_client.list_trigger_orders_pending(
                    inst_id=instrument_id
                )
        except Exception:
            continue
        _reconcile_exact_set_intent_from_pending(
            session_factory,
            intent_id=intent_id,
            pending_trigger_orders=pending,
            reconciled_at=datetime.now(UTC),
        )
        result = gateway._intent_result(intent_id)
        if result.status == "confirmed":
            return result
    return gateway._intent_result(intent_id)


def _reconcile_exact_set_intent_from_pending(
    session_factory: sessionmaker,
    *,
    intent_id: int,
    pending_trigger_orders: Any,
    reconciled_at: datetime,
) -> bool:
    rows = [
        row
        for row in pending_trigger_orders
        if isinstance(row, Mapping)
    ] if isinstance(pending_trigger_orders, list) else []
    with session_factory() as session:
        intent = session.get(PositionMutationIntent, int(intent_id))
        if (
            intent is None
            or intent.operation != "set_position_sltp"
            or intent.status not in {
                "submitting",
                "submitted",
                "recovery_required",
            }
        ):
            return bool(intent is not None and intent.status == "confirmed")
        try:
            request = load_validated_set_position_request(
                intent.request_json,
                request_fingerprint=str(intent.request_fingerprint),
                authority_fingerprint=str(intent.authority_fingerprint),
                require_baseline=True,
            )
        except PositionMutationIntentError:
            return False
        baseline_refs = set(request["_pre_submit_order_refs"])
        purpose = str(request.get("_ledger_purpose") or "") or (
            "stop_loss"
            if request.get("slTriggerPx") not in (None, "")
            else "take_profit"
            if request.get("tpTriggerPx") not in (None, "")
            else ""
        )
        exchange_purpose = (
            "stop_loss"
            if purpose in {"stop_loss", "backup_stop"}
            else "take_profit"
            if purpose == "take_profit"
            else ""
        )
        trigger_field = (
            "slTriggerPx"
            if exchange_purpose == "stop_loss"
            else "tpTriggerPx"
        )
        if not exchange_purpose or request.get(trigger_field) in (None, ""):
            return False
        authority = PositionMutationAuthority(
            venue=str(intent.venue),
            strategy_instance_id=str(intent.strategy_instance_id),
            execution_binding_id=int(intent.execution_binding_id),
            execution_order_leg_id=int(intent.execution_order_leg_id),
            pos_id=str(intent.pos_id),
            instrument_id=str(request.get("instId") or ""),
            side=str(request.get("posSide") or ""),
            position_fingerprint="readback-only",
            protection_fingerprint=None,
        )
        matching_ids = {
            order_id
            for row in rows
            if (
                order_id := str(
                    row.get("ordId")
                    or row.get("orderId")
                    or row.get("order_id")
                    or ""
                )
            )
            and _safe_exchange_identity(order_id)
            and _set_position_sltp_readback_matches(
                [row],
                order_id=order_id,
                authority=authority,
                purpose=exchange_purpose,
                trigger_price=str(request[trigger_field]),
                size=(str(request["sz"]) if "sz" in request else None),
                require_complete_identity=True,
            )
            and _protection_order_ref(order_id) not in baseline_refs
        }
        if len(matching_ids) != 1:
            return False
        binding = session.get(
            ExecutionBinding, int(intent.execution_binding_id)
        )
        if binding is None:
            return False
        order_id = next(iter(matching_ids))
        _confirm_intent(
            intent,
            order_id=order_id,
            reconciled_at=reconciled_at,
        )
        upsert_protection_ledger_row(
            session,
            venue=str(intent.venue),
            execution_binding_id=int(intent.execution_binding_id),
            execution_order_leg_id=int(intent.execution_order_leg_id),
            strategy_instance_id=str(intent.strategy_instance_id),
            pos_id=str(intent.pos_id),
            instrument_id=str(request.get("instId") or ""),
            side=str(request.get("posSide") or binding.side),
            order_id=order_id,
            purpose=purpose,
            trigger_price=str(request[trigger_field]),
            size_text=(str(request["sz"]) if "sz" in request else None),
            status="verified",
            evidence_source="position_mutation_intent_readback",
            evidence={"intent_id": int(intent.id)},
            seen_at=reconciled_at,
        )
        session.commit()
        return True


def cancel_exact_position_sltp(
    *,
    session_factory: sessionmaker,
    deepcoin_client: Any,
    pos_id: str,
    order_id: str,
    instrument_id: str,
    idempotency_key: str,
    live_execution_gate: Callable[[], bool],
    now_provider: Callable[[], Any],
) -> Mapping[str, Any]:
    """Compatibility adapter that rebuilds authority before one exact TPSL cancel."""

    authority = _build_fresh_authority(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        pos_id=pos_id,
        instrument_id=instrument_id,
    )
    result = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        live_execution_gate=live_execution_gate,
        now_provider=now_provider,
    ).cancel_owned_position_sltp(
        authority=authority,
        order_id=order_id,
        idempotency_key=idempotency_key,
    )
    return _require_submitted_response(result)


def close_exact_position(
    *,
    session_factory: sessionmaker,
    deepcoin_client: Any,
    pos_id: str,
    instrument_id: str,
    size: str,
    client_order_id: str | None,
    idempotency_key: str,
    live_execution_gate: Callable[[], bool],
    now_provider: Callable[[], Any],
) -> Mapping[str, Any]:
    """Compatibility adapter that rebuilds authority before one exact close."""

    authority = _build_fresh_authority(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        pos_id=pos_id,
        instrument_id=instrument_id,
    )
    result = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        live_execution_gate=live_execution_gate,
        now_provider=now_provider,
    ).close_exact_position(
        authority=authority,
        size=size,
        client_order_id=client_order_id,
        idempotency_key=idempotency_key,
    )
    return _require_submitted_response(result)


def _build_fresh_authority(
    *,
    session_factory: sessionmaker,
    deepcoin_client: Any,
    pos_id: str,
    instrument_id: str,
) -> PositionMutationAuthority:
    positions = deepcoin_client.list_positions(inst_id=instrument_id)
    matches = [
        row for row in positions if str(row.get("posId") or "") == str(pos_id)
    ]
    if len(matches) != 1:
        raise PositionMutationAuthorityError("target_live_position_not_unique")
    with session_factory() as session:
        return build_position_mutation_authority(
            session,
            venue="deepcoin",
            pos_id=str(pos_id),
            live_position=matches[0],
        )


def _require_submitted_response(
    result: PositionMutationResult,
) -> Mapping[str, Any]:
    if result.status in {"submitted", "confirmed"} and result.response is not None:
        return result.response
    reason = result.reason or f"position_mutation_{result.status}"
    if result.status == "rejected":
        raise DeepcoinDefiniteRejection(reason)
    if result.status in {"submitting", "recovery_required"}:
        raise DeepcoinRequestOutcomeUnknown(reason)
    raise PositionMutationAuthorityError(reason)


def exact_position_write_gate(
    session_factory: sessionmaker,
    *,
    pos_id: str,
) -> bool:
    """Last-moment durable gate used by risk-reducing position mutations."""

    with session_factory() as session:
        try:
            leg = require_verified_position_ownership(
                session,
                venue="deepcoin",
                pos_id=str(pos_id),
            )
        except PositionAttributionError:
            return False
        return leg is not None


def reconcile_submitted_position_mutation_intents(
    session_factory: sessionmaker,
    *,
    pending_trigger_orders: Any = None,
    order_history: Any = None,
    trade_fills: Any = None,
    reconciled_at: Any,
) -> int:
    """Confirm durable intents only from exact exchange readback evidence."""

    pending_snapshot_available = isinstance(pending_trigger_orders, list)
    rows = [
        row for row in pending_trigger_orders
        if isinstance(row, Mapping)
    ] if pending_snapshot_available else []
    history_rows = [
        row for row in order_history
        if isinstance(row, Mapping)
    ] if isinstance(order_history, list) else []
    fill_rows = [
        row for row in trade_fills
        if isinstance(row, Mapping)
    ] if isinstance(trade_fills, list) else []
    confirmed_close_rows = [
        *fill_rows,
        *[
            row
            for row in history_rows
            if _close_terminal_status(row) == "confirmed"
        ],
    ]
    rejected_close_rows = [
        row
        for row in history_rows
        if _close_terminal_status(row) == "rejected"
    ]
    terminal_rows = [*confirmed_close_rows, *rejected_close_rows]
    confirmed_close_order_ids = {
        order_id
        for row in confirmed_close_rows
        if (
            order_id := str(
                row.get("ordId")
                or row.get("orderId")
                or row.get("order_id")
                or ""
            )
        )
    }
    rejected_close_order_ids = {
        order_id
        for row in rejected_close_rows
        if (
            order_id := str(
                row.get("ordId")
                or row.get("orderId")
                or row.get("order_id")
                or ""
            )
        )
    }
    pending_order_ids = {
        str(
            row.get("ordId")
            or row.get("orderId")
            or row.get("order_id")
            or ""
        )
        for row in rows
    }
    confirmed = 0
    with session_factory() as session:
        intents = (
            session.query(PositionMutationIntent)
            .filter(
                PositionMutationIntent.operation.in_(
                    (
                        "set_position_sltp",
                        "cancel_position_sltp",
                        "close_position",
                    )
                ),
                PositionMutationIntent.status.in_(
                    ("submitting", "submitted", "recovery_required")
                ),
            )
            .order_by(PositionMutationIntent.id.asc())
            .all()
        )
        for intent in intents:
            request = _load_json(intent.request_json)
            baseline_refs: set[str] = set()
            if intent.operation == "set_position_sltp":
                protected_identity = str(
                    intent.idempotency_key or ""
                ).startswith("protected-entry:")
                strict_identity = (
                    protected_identity
                    or "_base_authority_fingerprint" in request
                    or "_pre_submit_order_refs" in request
                )
                if strict_identity:
                    try:
                        request = load_validated_set_position_request(
                            intent.request_json,
                            request_fingerprint=str(
                                intent.request_fingerprint
                            ),
                            authority_fingerprint=str(
                                intent.authority_fingerprint
                            ),
                            require_baseline=protected_identity,
                        )
                    except PositionMutationIntentError:
                        continue
                    baseline_refs = set(
                        request.get("_pre_submit_order_refs") or []
                    )
            response = _load_json(intent.response_json)
            order_id = str(intent.order_id or "") or _response_order_id(
                response
            )
            if (
                not order_id
                and intent.operation == "close_position"
                and request.get("clOrdId")
            ):
                client_order_id = str(request["clOrdId"])
                matches = [
                    row
                    for row in terminal_rows
                    if str(
                        row.get("clOrdId")
                        or row.get("clientOrderId")
                        or row.get("client_order_id")
                        or ""
                    )
                    == client_order_id
                    and _close_readback_matches_request(row, request)
                ]
                matching_order_ids = {
                    candidate_order_id
                    for row in matches
                    if (
                        candidate_order_id := str(
                            row.get("ordId")
                            or row.get("orderId")
                            or row.get("order_id")
                            or ""
                        )
                    )
                }
                if len(matching_order_ids) == 1:
                    candidate_order_id = next(iter(matching_order_ids))
                    order_id = candidate_order_id
            if not order_id and intent.operation == "set_position_sltp":
                persisted_purpose = str(
                    request.get("_ledger_purpose") or ""
                ) or (
                    "stop_loss"
                    if request.get("slTriggerPx") not in (None, "")
                    else "take_profit"
                )
                exchange_purpose = (
                    "stop_loss"
                    if persisted_purpose in {"stop_loss", "backup_stop"}
                    else "take_profit"
                )
                trigger_field = (
                    "slTriggerPx"
                    if exchange_purpose == "stop_loss"
                    else "tpTriggerPx"
                )
                authority = PositionMutationAuthority(
                    venue=str(intent.venue),
                    strategy_instance_id=str(intent.strategy_instance_id),
                    execution_binding_id=int(intent.execution_binding_id),
                    execution_order_leg_id=int(intent.execution_order_leg_id),
                    pos_id=str(intent.pos_id),
                    instrument_id=str(request.get("instId") or ""),
                    side=str(request.get("posSide") or ""),
                    position_fingerprint="readback-only",
                    protection_fingerprint=None,
                )
                matching_ids = {
                    candidate_id
                    for row in rows
                    if (
                        candidate_id := str(
                            row.get("ordId")
                            or row.get("orderId")
                            or row.get("order_id")
                            or ""
                        )
                    )
                    and _safe_exchange_identity(candidate_id)
                    and _set_position_sltp_readback_matches(
                        [row],
                        order_id=candidate_id,
                        authority=authority,
                        purpose=exchange_purpose,
                        trigger_price=str(request.get(trigger_field) or ""),
                        size=(str(request["sz"]) if "sz" in request else None),
                        require_complete_identity=True,
                    )
                    and _protection_order_ref(candidate_id)
                    not in baseline_refs
                }
                if len(matching_ids) == 1:
                    order_id = next(iter(matching_ids))
            if not order_id or not _safe_exchange_identity(order_id):
                continue
            if intent.operation == "cancel_position_sltp":
                if (
                    not pending_snapshot_available
                    or order_id in pending_order_ids
                ):
                    continue
                ledger = (
                    session.query(PositionProtectionLedger)
                    .filter(
                        PositionProtectionLedger.venue
                        == str(intent.venue),
                        PositionProtectionLedger.order_id == order_id,
                    )
                    .one_or_none()
                )
                if ledger is not None:
                    ledger.status = "cancelled"
                    ledger.last_seen_at = reconciled_at
                _confirm_intent(
                    intent,
                    order_id=order_id,
                    reconciled_at=reconciled_at,
                )
                confirmed += 1
                continue
            if intent.operation == "close_position":
                if (
                    order_id in confirmed_close_order_ids
                    and order_id in rejected_close_order_ids
                ):
                    exact_fill_rows = [
                        row for row in confirmed_close_rows
                        if str(
                            row.get("ordId")
                            or row.get("orderId")
                            or row.get("order_id")
                            or ""
                        ) == order_id
                        and _close_readback_matches_request(row, request)
                    ]
                    if not exact_fill_rows:
                        continue
                    _confirm_intent(
                        intent,
                        order_id=order_id,
                        reconciled_at=reconciled_at,
                    )
                    intent.response_json = json.dumps(
                        {
                            "ordId": order_id,
                            "status": "partially_filled_terminal",
                            "reconciled_from_exchange_readback": True,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    confirmed += 1
                    continue
                rejected_matches = [
                    row
                    for row in rejected_close_rows
                    if str(
                        row.get("ordId")
                        or row.get("orderId")
                        or row.get("order_id")
                        or ""
                    )
                    == order_id
                ]
                if rejected_matches:
                    intent.status = "rejected"
                    intent.order_id = order_id
                    intent.response_json = json.dumps(
                        {
                            "ordId": order_id,
                            "status": _first_order_status(
                                rejected_matches[0]
                            ),
                            "reconciled_from_exchange_readback": True,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    intent.confirmed_at = reconciled_at
                    intent.updated_at = reconciled_at
                    confirmed += 1
                    continue
                if order_id not in confirmed_close_order_ids:
                    continue
                _confirm_intent(
                    intent,
                    order_id=order_id,
                    reconciled_at=reconciled_at,
                )
                confirmed += 1
                continue
            purpose = str(request.get("_ledger_purpose") or "") or (
                "stop_loss"
                if request.get("slTriggerPx") not in (None, "")
                else "take_profit"
                if request.get("tpTriggerPx") not in (None, "")
                else None
            )
            if purpose is None:
                continue
            exchange_purpose = (
                "stop_loss" if purpose in {"stop_loss", "backup_stop"}
                else "take_profit"
            )
            trigger_price = str(
                request[
                    "slTriggerPx"
                    if exchange_purpose == "stop_loss"
                    else "tpTriggerPx"
                ]
            )
            authority = PositionMutationAuthority(
                venue=str(intent.venue),
                strategy_instance_id=str(intent.strategy_instance_id),
                execution_binding_id=int(intent.execution_binding_id),
                execution_order_leg_id=int(intent.execution_order_leg_id),
                pos_id=str(intent.pos_id),
                instrument_id=str(request.get("instId") or ""),
                side=str(request.get("posSide") or ""),
                position_fingerprint="readback-only",
                protection_fingerprint=None,
            )
            if not _set_position_sltp_readback_matches(
                rows,
                order_id=order_id,
                authority=authority,
                purpose=exchange_purpose,
                trigger_price=trigger_price,
                size=(str(request["sz"]) if "sz" in request else None),
                require_complete_identity=True,
            ):
                continue
            binding = session.get(
                ExecutionBinding, int(intent.execution_binding_id)
            )
            if binding is None:
                continue
            _confirm_intent(
                intent,
                order_id=order_id,
                reconciled_at=reconciled_at,
            )
            upsert_protection_ledger_row(
                session,
                venue=str(intent.venue),
                execution_binding_id=int(intent.execution_binding_id),
                execution_order_leg_id=int(intent.execution_order_leg_id),
                strategy_instance_id=str(intent.strategy_instance_id),
                pos_id=str(intent.pos_id),
                instrument_id=str(request.get("instId") or ""),
                side=str(request.get("posSide") or binding.side),
                order_id=order_id,
                purpose=purpose,
                trigger_price=trigger_price,
                size_text=(
                    str(request["sz"]) if "sz" in request else None
                ),
                status="verified",
                evidence_source="position_mutation_intent_readback",
                evidence={"intent_id": int(intent.id)},
                seen_at=reconciled_at,
            )
            confirmed += 1
        session.commit()
    return confirmed


def _confirm_intent(
    intent: PositionMutationIntent,
    *,
    order_id: str,
    reconciled_at: Any,
) -> None:
    if not _safe_exchange_identity(order_id):
        raise PositionMutationAuthorityError(
            "position_mutation_exchange_identity_invalid"
        )
    intent.order_id = order_id
    if not intent.response_json:
        intent.response_json = json.dumps(
            {
                "code": "0",
                "data": {"ordId": order_id},
                "reconciled_from_exchange_readback": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    intent.status = "confirmed"
    intent.confirmed_at = reconciled_at
    intent.updated_at = reconciled_at


def _response_order_id(response: Mapping[str, Any]) -> str | None:
    data = response.get("data")
    candidates = [data, response]
    if isinstance(data, list):
        candidates = [*data, response]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if not isinstance(candidate, Mapping):
            continue
        for key in ("ordId", "orderId", "order_id", "orderSysID"):
            value = candidate.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _set_position_sltp_readback_matches(
    rows: Any,
    *,
    order_id: str,
    authority: PositionMutationAuthority,
    purpose: str,
    trigger_price: str,
    size: str | None = None,
    require_complete_identity: bool = False,
) -> bool:
    trigger_keys = (
        ("slTriggerPx", "slTriggerPrice", "triggerPrice")
        if purpose == "stop_loss"
        else ("tpTriggerPx", "tpTriggerPrice", "triggerPrice")
    )
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        if str(
            row.get("ordId")
            or row.get("orderId")
            or row.get("order_id")
            or ""
        ) != order_id:
            continue
        instrument = str(row.get("instId") or "").upper()
        if (
            (require_complete_identity and not instrument)
            or (
                instrument
                and instrument != authority.instrument_id.upper()
            )
        ):
            continue
        side = str(row.get("posSide") or "").lower()
        if (
            (require_complete_identity and not side)
            or (side and side != authority.side.lower())
        ):
            continue
        row_pos_id = str(row.get("posId") or row.get("pos_id") or "")
        if (
            (require_complete_identity and not row_pos_id)
            or (row_pos_id and row_pos_id != authority.pos_id)
        ):
            continue
        observed_trigger = next(
            (
                str(row[key])
                for key in trigger_keys
                if row.get(key) not in (None, "")
            ),
            None,
        )
        observed_size = row.get("sz") or row.get("size")
        if (
            _decimal_values_equal(observed_trigger, trigger_price)
            and (
                size is None
                or _decimal_values_equal(str(observed_size), str(size))
            )
        ):
            return True
    return False


def _decimal_values_equal(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    try:
        left_value = Decimal(left)
        right_value = Decimal(right)
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (
        left_value.is_finite()
        and right_value.is_finite()
        and left_value == right_value
    )


def _close_readback_matches_request(
    row: Mapping[str, Any],
    request: Mapping[str, Any],
) -> bool:
    comparisons = (
        (
            row.get("instId"),
            request.get("instId"),
            lambda value: str(value).upper(),
        ),
        (
            row.get("posSide"),
            request.get("posSide"),
            lambda value: str(value).lower(),
        ),
        (
            row.get("closePosId") or row.get("posId"),
            request.get("closePosId"),
            str,
        ),
    )
    for observed, expected, normalize in comparisons:
        if (
            observed not in (None, "")
            and expected not in (None, "")
            and normalize(observed) != normalize(expected)
        ):
            return False
    observed_size = row.get("sz") or row.get("size")
    expected_size = request.get("sz")
    if (
        observed_size not in (None, "")
        and expected_size not in (None, "")
        and not _decimal_values_equal(
            str(observed_size), str(expected_size)
        )
    ):
        return False
    return True


def _first_order_status(row: Mapping[str, Any]) -> str:
    return str(
        row.get("state")
        or row.get("status")
        or row.get("ordStatus")
        or row.get("orderStatus")
        or ""
    ).lower()


def _close_terminal_status(row: Mapping[str, Any]) -> str | None:
    status = _first_order_status(row)
    if status in {
        "filled",
        "fully_filled",
        "completed",
        "complete",
        "closed",
        "success",
        "succeeded",
    }:
        return "confirmed"
    if status in {
        "rejected",
        "failed",
        "cancelled",
        "canceled",
        "expired",
    }:
        return "rejected"
    return None
