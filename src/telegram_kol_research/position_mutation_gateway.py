"""Fail-closed gateway for exact, durable Deepcoin position writes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
    build_position_mutation_authority,
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
from telegram_kol_research.protection_ledger import (
    upsert_protection_ledger_row,
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
        size: str | None,
        idempotency_key: str,
        request_options: Mapping[str, Any] | None = None,
    ) -> PositionMutationResult:
        trigger_field_by_purpose = {
            "stop_loss": "slTriggerPx",
            "take_profit": "tpTriggerPx",
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
        trigger_field = trigger_field_by_purpose[purpose]
        payload = {
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
        client_order_id: str | None,
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
        }
        if client_order_id:
            payload["clOrdId"] = client_order_id
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
    result = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        live_execution_gate=live_execution_gate,
        now_provider=now_provider,
    ).set_exact_position_sltp(
        authority=authority,
        purpose=purpose,
        trigger_price=str(payload[trigger_field]),
        size=(str(payload["sz"]) if "sz" in payload else None),
        idempotency_key=idempotency_key,
        request_options=payload,
    )
    response = _require_submitted_response(result)
    if not require_readback:
        return response
    order_id = _response_order_id(response)
    if not order_id:
        raise DeepcoinRequestOutcomeUnknown(
            "position_sltp_response_missing_order_id"
        )
    try:
        pending = deepcoin_client.list_trigger_orders_pending(
            inst_id=authority.instrument_id
        )
    except Exception as exc:
        raise DeepcoinRequestOutcomeUnknown(
            "position_sltp_readback_unavailable"
        ) from exc
    if not _set_position_sltp_readback_matches(
        pending,
        order_id=order_id,
        authority=authority,
        purpose=purpose,
        trigger_price=str(payload[trigger_field]),
    ):
        raise DeepcoinRequestOutcomeUnknown(
            "position_sltp_pending_readback"
        )
    transition_position_mutation_intent(
        session_factory,
        result.intent_id,
        expected_statuses={"submitted"},
        new_status="confirmed",
        transitioned_at=now_provider(),
        response=response,
    )
    return response


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
                    ("submitted", "recovery_required")
                ),
            )
            .order_by(PositionMutationIntent.id.asc())
            .all()
        )
        for intent in intents:
            request = _load_json(intent.request_json)
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
                    if not (
                        candidate_order_id in confirmed_close_order_ids
                        and candidate_order_id in rejected_close_order_ids
                    ):
                        order_id = candidate_order_id
            if not order_id:
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
            purpose = (
                "stop_loss"
                if request.get("slTriggerPx") not in (None, "")
                else "take_profit"
                if request.get("tpTriggerPx") not in (None, "")
                else None
            )
            if purpose is None:
                continue
            trigger_price = str(
                request[
                    "slTriggerPx"
                    if purpose == "stop_loss"
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
                purpose=purpose,
                trigger_price=trigger_price,
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
        if instrument and instrument != authority.instrument_id.upper():
            continue
        side = str(row.get("posSide") or "").lower()
        if side and side != authority.side.lower():
            continue
        row_pos_id = str(row.get("posId") or row.get("pos_id") or "")
        if row_pos_id and row_pos_id != authority.pos_id:
            continue
        observed_trigger = next(
            (
                str(row[key])
                for key in trigger_keys
                if row.get(key) not in (None, "")
            ),
            None,
        )
        if _decimal_values_equal(observed_trigger, trigger_price):
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
