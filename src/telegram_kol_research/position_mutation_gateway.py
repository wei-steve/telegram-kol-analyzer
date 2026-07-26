"""Fail-closed gateway for exact, durable Deepcoin position writes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import (
    DeepcoinDefiniteRejection,
    DeepcoinRequestOutcomeUnknown,
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
    position_authority_fingerprint,
    require_order_owned_by_authority,
)
from telegram_kol_research.position_mutation_intents import (
    reserve_position_mutation_intent,
    transition_position_mutation_intent,
)
from telegram_kol_research.position_attribution import (
    PositionAttributionError,
    require_verified_position_ownership,
)


@dataclass(frozen=True, slots=True)
class PositionMutationResult:
    status: str
    reason: str | None
    intent_id: int
    response: Mapping[str, Any] | None = None


class PositionMutationGateway:
    """Authorize, reserve, revalidate, and submit an exact exchange mutation."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker,
        deepcoin_client: Any,
        live_execution_gate: Callable[[], bool],
        now_provider: Callable[[], Any],
    ) -> None:
        self._session_factory = session_factory
        self._client = deepcoin_client
        self._live_execution_gate = live_execution_gate
        self._now = now_provider

    def cancel_owned_position_sltp(
        self,
        *,
        authority: PositionMutationAuthority,
        order_id: str,
        idempotency_key: str,
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
        if current.status != "reserved":
            return current

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
        trigger_price: str,
        size: str,
        idempotency_key: str,
    ) -> PositionMutationResult:
        trigger_field_by_purpose = {
            "stop_loss": ("slTriggerPx", "slSize"),
            "take_profit": ("tpTriggerPx", "tpSize"),
        }
        if purpose not in trigger_field_by_purpose:
            raise ValueError("position_sltp_purpose_invalid")
        binding, reason = self._load_verified_binding(authority)
        if reason is not None or binding is None:
            payload = {
                "instType": "SWAP",
                "instId": authority.instrument_id,
                "posSide": authority.side,
                "mrgPosition": "",
                "posId": authority.pos_id,
                "tdMode": "",
                trigger_field_by_purpose[purpose][0]: str(trigger_price),
                trigger_field_by_purpose[purpose][1]: str(size),
            }
            return self._reserve_and_block(
                authority=authority,
                operation="set_position_sltp",
                order_id=None,
                payload=payload,
                idempotency_key=idempotency_key,
                reason=reason or "execution_binding_missing",
            )
        trigger_field, size_field = trigger_field_by_purpose[purpose]
        payload = {
            "instType": "SWAP",
            "instId": authority.instrument_id,
            "posSide": authority.side.lower(),
            "mrgPosition": str(binding.position_mode).lower(),
            "posId": authority.pos_id,
            "tdMode": str(binding.margin_mode).lower(),
            trigger_field: str(trigger_price),
            size_field: str(size),
        }
        return self._submit_exact_position_write(
            authority=authority,
            operation="set_position_sltp",
            order_id=None,
            payload=payload,
            idempotency_key=idempotency_key,
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
        client_order_id: str,
        idempotency_key: str,
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
            "clOrdId": client_order_id,
        }
        return self._submit_exact_position_write(
            authority=authority,
            operation="close_position",
            order_id=None,
            payload=payload,
            idempotency_key=idempotency_key,
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
        idempotency_key: str,
        submit: Callable[[], Mapping[str, Any]],
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
        intent_id = int(intent.id)
        current = self._intent_result(intent_id)
        if current.status != "reserved":
            return current

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
        if position_authority_fingerprint(matches[0]) != authority.position_fingerprint:
            return self._block(intent_id, "position_fingerprint_changed")
        if not self._live_execution_gate():
            return self._block(intent_id, "live_execution_disabled")
        if not transition_position_mutation_intent(
            self._session_factory,
            intent_id,
            expected_statuses={"reserved"},
            new_status="submitting",
            transitioned_at=self._now(),
        ):
            return self._intent_result(intent_id)
        try:
            response = submit()
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
                    not in {"stop_loss", "take_profit"}
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


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
