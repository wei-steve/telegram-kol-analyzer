"""Read-before-write planning for exact-leg staged take-profit convergence."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_trigger_rows import (
    take_profit_present_failing_closed,
)
from telegram_kol_research.deepcoin_client import (
    DeepcoinDefiniteRejection,
    DeepcoinRequestOutcomeUnknown,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionAttributionAudit,
    PositionBackupStopOrder,
    PositionProtectionLeg,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
    TriggerTakeProfitConvergence,
    utc_now,
)
from telegram_kol_research.position_authority_lock import serialized_position_authority_mutation
from telegram_kol_research.position_protection_legs import (
    bind_filled_position,
    create_or_get_protection_leg,
    protection_write_block_reason,
)
from telegram_kol_research.position_mutation_gateway import (
    exact_position_write_gate,
    submit_exact_position_sltp,
)
from telegram_kol_research.position_take_profit_orders import (
    record_take_profit_order,
)
from telegram_kol_research.trigger_take_profit_convergence import (
    AUTOMATIC_ENTRY_ORDER_KINDS,
)
from telegram_kol_research.position_protection_legs import bind_verified_exchange_order
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.protection_snapshot import (
    read_complete_pending_tpsl_snapshot,
)
from telegram_kol_research.take_profit_plan import TakeProfitPlanError, build_take_profit_plan
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.native_tpsl import (
    is_protection_order_row,
    native_tpsl_order_id_is_unique,
    protection_order_position_sides,
    protection_order_sides_consistent,
    NativeTpslExpectation,
    NativeTpslOrder,
    match_native_tpsl_order,
    native_tpsl_take_profit_is_market,
    normalize_native_tpsl,
)


class _RefusalReason(str):
    """A refusal reason code that also carries the row fields that produced it.

    Refusals travel through this module as plain reason-code strings.  Keeping
    the deciding field values attached to the same value lets the caller persist
    attributable evidence without changing any control flow or adding a column.
    """

    __slots__ = ("refusal_detail",)

    def __new__(cls, reason: str, refusal_detail: dict[str, object]):
        instance = super().__new__(cls, reason)
        instance.refusal_detail = refusal_detail
        return instance


def _refusal_detail_json(reason: object) -> str | None:
    detail = getattr(reason, "refusal_detail", None)
    if not detail:
        return None
    return json.dumps(
        {"reason_code": str(reason), "refusal_detail": detail}, ensure_ascii=False
    )


logger = logging.getLogger(__name__)

#: A-15-1. Positions cleared to receive the first take-profit orders ever placed
#: for a plain limit entry. Empty on purpose: the three gates that used to stop
#: those orders are fixed, but "the code is now correct" and "start writing to
#: the exchange" are two decisions, and the second one is the user's. A position
#: that is not listed here still gets its whole plan computed and then held, so
#: what would be sent is on the record before anybody approves it.
#:
#: 1001125216121996 was released on 2026-09-11 after the user confirmed the
#: exact orders it would place, which the shadow window had by then recomputed
#: identically on every pass for fifteen hours: 79800 x 7 and 81900 x 8 against
#: a 15-contract position, no cancellations, the primary and backup stops
#: untouched. The second position of that pair (1001125216153672) is
#: deliberately still held -- one position first, verified order by order,
#: then the other.
#:
#: Both went back out again. The user closed the pair by hand on 2026-09-11 at
#: 78611.6; the two orders released above had been live for five hours, were
#: voided with the position, and the second position never got its own release.
#: Leaving a pos_id here that no longer exists would be a dead value in live
#: code -- it reads like a standing permission and grants nothing.
#:
#: **The two ETH ids are phase 6j**, released on 2026-09-11 under the authority
#: the user granted that day ("授权放开", recorded at commit d91a4179). Binding
#: 352, both legs ``order_kind=limit`` and ``attribution_status=verified`` by
#: ``direct_order_position_id``. The withheld record was already on file before
#: the release: audits 4023/4025, one tier, ``trigger_price=2790``,
#: ``size=0.9`` against a ``position_size`` of 0.9 -- and 2790 is the take
#: profit the KOL's own message names.
#:
#: **Releasing by ``order_kind == "limit"`` instead of by position id was
#: proposed on 2026-09-11 and narrowed back**, by two objections that arrived
#: independently: that the enumeration's cost is the design rather than a
#: defect, and that ``order_kind=limit`` does not by itself carry "opened by
#: this system in an auto_trade group", so a source-shaped release would be
#: wider than the authority it is drawn from. That change is phase 6k; its
#: precondition is the real execution these two ids are about to produce, and
#: its predicate has to carry the auto_trade and verified conditions
#: explicitly.
#: **Retired on 2026-09-12 (phase 6k).** The enumeration is replaced by
#: ``source_release.evaluate_source_release``: a plain limit entry, in a group
#: configured ``auto_trade``, whose entry leg's attribution is ``verified``.
#: The two ids that were here -- 1001125231241107 and 1001125231241310 -- both
#: satisfy that predicate, so nothing they were permitted stops being
#: permitted; what changes is that the next such position does not need a
#: deploy. The per-position lever that remains is
#: ``source_release.SOURCE_RELEASE_BLOCKED_POS_IDS``, and it holds rather than
#: releases.

from telegram_kol_research.source_release import (
    evaluate_source_release,
    resolve_group_trading_mode,
)

WOULD_PLACE_EVENT = "take_profit_would_place"
WOULD_PLACE_ENDPOINT = "POST /deepcoin/trade/set-position-sltp"


def _limit_entry_release_withheld(
    session_factory,
    *,
    convergence_id: int,
    plan,
    now: datetime,
    group_trading_mode_provider=None,
) -> dict[str, object] | None:
    """Hold a plain limit entry's take profits, recording what would be sent.

    Returns ``None`` when this convergence is not withheld -- either its entry
    is not a plain limit entry (those paths are unchanged by A-15-1) or its
    position has been released.
    """

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        if convergence is None:
            return None
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        if leg is None or str(leg.order_kind or "") != "limit":
            return None
        pos_id = str(convergence.pos_id or "")
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        release = evaluate_source_release(
            pos_id=pos_id,
            kind="take_profit_limit_entry",
            entry_order_kind=getattr(leg, "order_kind", None),
            attribution_status=getattr(leg, "attribution_status", None),
            group_trading_mode=resolve_group_trading_mode(
                group_trading_mode_provider, getattr(binding, "chat_id", None)
            ),
        )
        if release.released:
            return None
        for tier_index, payload in enumerate(plan.payloads, start=1):
            # Logged every round on purpose: an observation window needs to see
            # the same four lines again and again, not one line and then
            # silence. The audit row below is the deduplicated half.
            logger.info(
                "%s convergence=%s pos_id=%s tier=%s trigger_price=%s size=%s "
                "endpoint=%s position_size=%s",
                WOULD_PLACE_EVENT,
                convergence_id,
                pos_id,
                tier_index,
                payload.get("tpTriggerPx"),
                payload.get("sz"),
                WOULD_PLACE_ENDPOINT,
                plan.position_size_text,
            )
            _record_would_place_audit(
                session,
                binding=binding,
                leg=leg,
                pos_id=pos_id,
                tier_index=tier_index,
                payload=payload,
                position_size_text=plan.position_size_text,
                created_at=now,
            )
        session.commit()
    return {
        "convergence_id": convergence_id,
        "status": "withheld",
        "reason": "take_profit_limit_entry_release_withheld",
        # Phase 6k: which condition refused. Under the per-id gate there was
        # one answer; a predicate has four, and they call for different
        # actions -- a notify_only group is a decision, an unknown group mode
        # is a missing config entry, an unverified attribution is a data
        # problem, a blocked id is somebody's deliberate hold.
        "release_reason": release.reason,
    }


def _record_would_place_audit(
    session,
    *,
    binding,
    leg,
    pos_id: str,
    tier_index: int,
    payload: dict[str, str],
    position_size_text: str | None,
    created_at: datetime,
) -> None:
    """One audit row per tier, deduplicated on what would actually be sent."""

    identity = {
        "event_type": WOULD_PLACE_EVENT,
        "pos_id": pos_id,
        "tier_index": int(tier_index),
        "trigger_price": str(payload.get("tpTriggerPx") or ""),
        "size": str(payload.get("sz") or ""),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    exists = (
        session.query(PositionAttributionAudit.id)
        .filter(PositionAttributionAudit.fingerprint == fingerprint)
        .first()
    )
    if exists is not None:
        return
    session.add(
        PositionAttributionAudit(
            execution_binding_id=int(leg.execution_binding_id),
            execution_order_leg_id=int(leg.id),
            venue=str(leg.venue or "deepcoin"),
            pos_id=pos_id,
            event_type=WOULD_PLACE_EVENT,
            prior_state="ready",
            new_state="withheld",
            fingerprint=fingerprint,
            evidence_json=json.dumps(
                {
                    **identity,
                    "endpoint": WOULD_PLACE_ENDPOINT,
                    "position_size": str(position_size_text or ""),
                    "instrument_id": str(payload.get("instId") or ""),
                    "position_side": str(payload.get("posSide") or ""),
                    "symbol": str(getattr(binding, "symbol", "") or ""),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            created_at=created_at,
        )
    )
    session.flush()


@dataclass(frozen=True, slots=True)
class TriggerTakeProfitConvergencePlan:
    status: str
    reason_code: str | None = None
    cancel_order_ids: tuple[str, ...] = ()
    payloads: tuple[dict[str, str], ...] = ()
    position_size_text: str | None = None


def plan_trigger_take_profit_convergence(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    deepcoin_client,
    contract_spec_provider=None,
    planned_at: datetime | None = None,
    allow_logical_adoption: bool = False,
) -> TriggerTakeProfitConvergencePlan:
    """Produce a TP-only plan or fail closed before any exchange mutation."""

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, int(convergence_id))
        if convergence is None:
            return TriggerTakeProfitConvergencePlan("blocked", "convergence_not_found")
        if str(convergence.status) != "ready":
            return TriggerTakeProfitConvergencePlan(
                "blocked", convergence.reason_code or "convergence_not_ready"
            )
        prepared = _prepare_plan(
            session,
            convergence=convergence,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=(
                contract_spec_provider
                if contract_spec_provider is not None
                else getattr(deepcoin_client, "contract_spec_provider", None)
            ),
            allow_logical_adoption=allow_logical_adoption,
        )
        if isinstance(prepared, str):
            if prepared == "convergence_take_profit_already_converged":
                if allow_logical_adoption:
                    session.commit()
                return TriggerTakeProfitConvergencePlan("already_converged", prepared)
            convergence.status = (
                "waiting_backup_stop"
                if prepared == "convergence_waiting_backup_stop"
                else "conflicted" if prepared.startswith("convergence_") else "blocked"
            )
            convergence.reason_code = str(prepared)
            convergence.error_json = _refusal_detail_json(prepared)
            if planned_at is not None:
                convergence.updated_at = planned_at
            session.commit()
            return TriggerTakeProfitConvergencePlan(convergence.status, prepared)
        cancel_order_ids, payloads, position_size_text = prepared
        session.commit()
        return TriggerTakeProfitConvergencePlan(
            "ready",
            cancel_order_ids=tuple(cancel_order_ids),
            payloads=tuple(payloads),
            position_size_text=position_size_text,
        )


@serialized_position_authority_mutation
def execute_trigger_take_profit_convergence(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    deepcoin_client,
    contract_spec_provider=None,
    executed_at: datetime | None = None,
    group_trading_mode_provider=None,
) -> dict[str, object]:
    """Cancel exact-leg TP orders, then create the replacement TP set once."""

    now = executed_at or utc_now()
    plan = plan_trigger_take_profit_convergence(
        session_factory,
        convergence_id=convergence_id,
        deepcoin_client=deepcoin_client,
        contract_spec_provider=contract_spec_provider,
        planned_at=now,
        allow_logical_adoption=True,
    )
    if plan.status != "ready":
        if plan.status == "already_converged":
            with session_factory() as session:
                convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
                if convergence is not None and convergence.status == "ready":
                    convergence.status = "submitted"
                    convergence.reason_code = None
                    convergence.completed_at = now
                    convergence.updated_at = now
                    session.commit()
                    return {"convergence_id": convergence_id, "status": "submitted", "reason": None}
        return {"convergence_id": convergence_id, "status": plan.status, "reason": plan.reason_code}
    withheld = _limit_entry_release_withheld(
        session_factory,
        convergence_id=convergence_id,
        plan=plan,
        now=now,
        group_trading_mode_provider=group_trading_mode_provider,
    )
    if withheld is not None:
        return withheld
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        if convergence is None or convergence.status != "ready":
            return {"convergence_id": convergence_id, "status": "blocked", "reason": "convergence_not_ready"}
        convergence.status = "reserved"
        convergence.reason_code = None
        convergence.reserved_at = now
        convergence.updated_at = now
        session.commit()

    for payload_index, payload in enumerate(plan.payloads):
        prewrite_reason = _revalidate_take_profit_write(
            session_factory,
            convergence_id=convergence_id,
            deepcoin_client=deepcoin_client,
            payload=payload,
            expected_position_size=plan.position_size_text,
        )
        if prewrite_reason is not None:
            return _freeze(
                session_factory,
                convergence_id,
                now,
                prewrite_reason,
            )
        try:
            response = submit_exact_position_sltp(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                pos_id=str(payload["posId"]),
                payload=payload,
                idempotency_key=(
                    f"tp-convergence:{convergence_id}:set:{payload_index}"
                ),
                live_execution_gate=lambda target_pos_id=str(
                    payload["posId"]
                ): exact_position_write_gate(
                    session_factory, pos_id=target_pos_id
                ),
                now_provider=lambda: now,
                require_readback=True,
            )
        except DeepcoinDefiniteRejection as exc:
            return _freeze(
                session_factory,
                convergence_id,
                now,
                "convergence_submit_rejected",
                error=exc,
            )
        except DeepcoinRequestOutcomeUnknown as exc:
            return _freeze(
                session_factory,
                convergence_id,
                now,
                "convergence_take_profit_submit_unknown",
                error=exc,
            )
        except Exception as exc:
            return _freeze(session_factory, convergence_id, now, "convergence_submit_unknown", error=exc)
        order_id = _response_order_id(response)
        if order_id is None:
            return _freeze(session_factory, convergence_id, now, "convergence_submit_unknown", error="missing order ID")
        try:
            open_positions = list(deepcoin_client.list_positions())
            exact_position = _exact_live_position(
                open_positions,
                pos_id=str(payload["posId"]),
                inst_id=str(payload["instId"]),
                side=str(payload["posSide"]),
            )
            pending = read_complete_pending_tpsl_snapshot(
                deepcoin_client,
                instrument_id=str(payload["instId"]),
            )
            verified = _verified_native_take_profit(
                position=exact_position,
                open_positions=open_positions,
                pending=pending,
                order_id=order_id,
                payload=payload,
            )
        except Exception as exc:
            return _freeze(
                session_factory,
                convergence_id,
                now,
                "convergence_take_profit_submit_unknown",
                error=exc,
            )
        if verified is None:
            reason = (
                "convergence_take_profit_pending_readback"
                if _pending_contains_order_id(pending, order_id=order_id)
                else "convergence_take_profit_submit_unknown"
            )
            return _freeze(
                session_factory,
                convergence_id,
                now,
                reason,
                error="native TPSL take-profit was not verified in pending orders",
            )
        try:
            _persist_verified_take_profit(
                session_factory,
                convergence_id=convergence_id,
                payload=payload,
                order_id=order_id,
                response=response,
                verified=verified,
                persisted_at=now,
            )
        except Exception as exc:
            return _freeze(
                session_factory,
                convergence_id,
                now,
                "convergence_logical_protection_persist_unknown",
                error=exc,
            )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        if convergence is None or convergence.status != "reserved":
            return _freeze(session_factory, convergence_id, now, "convergence_completion_persist_conflict")
        convergence.status = "submitted"
        convergence.completed_at = now
        convergence.updated_at = now
        session.commit()
    return {"convergence_id": convergence_id, "status": "submitted", "reason": None}


def _persist_verified_take_profit(
    session_factory,
    *,
    convergence_id: int,
    payload: dict[str, str],
    order_id: str,
    response: object,
    verified: NativeTpslOrder,
    persisted_at: datetime,
) -> None:
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        if convergence is None or convergence.status != "reserved":
            raise ValueError("convergence_response_persist_conflict")
        protection_legs = _matching_take_profit_protection_legs(
            session,
            execution_binding_id=int(convergence.execution_binding_id),
            execution_order_leg_id=int(convergence.execution_order_leg_id),
            pos_id=str(convergence.pos_id),
            trigger_price=payload["tpTriggerPx"],
        )
        if (
            len(protection_legs) != 1
            or protection_legs[0].exchange_order_id is not None
            or str(protection_legs[0].status or "")
            not in {"waiting_fill", "protection_recovery_pending"}
        ):
            raise ValueError("convergence_logical_protection_persist_conflict")
        record_take_profit_order(
            session,
            venue="deepcoin",
            execution_binding_id=int(convergence.execution_binding_id),
            execution_order_leg_id=int(convergence.execution_order_leg_id),
            trigger_take_profit_convergence_id=int(convergence.id),
            pos_id=str(convergence.pos_id),
            order_id=order_id,
            trigger_price=str(payload["tpTriggerPx"]),
            size_text=str(payload["sz"]),
            created_at=persisted_at,
            evidence={
                "source": "native_tpsl_pending_readback",
                "response": _response_dict(response),
                "native_tpsl": verified.raw,
            },
        )
        bind_verified_exchange_order(
            session,
            protection_legs[0],
            exchange_order_id=order_id,
            readback_evidence={
                "response": _response_dict(response),
                "native_tpsl": verified.raw,
            },
        )
        entry_leg = session.get(
            ExecutionOrderLeg, int(convergence.execution_order_leg_id)
        )
        if entry_leg is None:
            raise ValueError("convergence_entry_leg_persist_conflict")
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=int(convergence.execution_binding_id),
            execution_order_leg_id=int(convergence.execution_order_leg_id),
            strategy_instance_id=entry_leg.strategy_instance_id,
            pos_id=str(convergence.pos_id),
            instrument_id=str(payload["instId"]),
            side=str(payload["posSide"]),
            order_id=order_id,
            purpose="take_profit",
            trigger_price=str(payload["tpTriggerPx"]),
            size_text=str(payload["sz"]),
            status="verified",
            evidence_source="trigger_take_profit_pending_readback",
            evidence={"native_tpsl": verified.raw},
            seen_at=persisted_at,
        )
        session.commit()


def execute_ready_trigger_take_profit_convergences(
    session_factory: sessionmaker,
    *,
    deepcoin_client,
    contract_spec_provider=None,
    processed_at: datetime | None = None,
    limit: int = 5,
    group_trading_mode_provider=None,
) -> int:
    """Run a bounded set of durable ready tasks; terminal tasks are skipped."""

    rollout_mode = load_trading_settings(
        session_factory
    ).effective_position_management_liveness_v2_mode
    if rollout_mode == "disabled":
        return 0
    with session_factory() as session:
        identifiers = [
            int(row.id)
            for row in (
                session.query(TriggerTakeProfitConvergence.id)
                .filter(TriggerTakeProfitConvergence.status == "ready")
                .order_by(TriggerTakeProfitConvergence.created_at, TriggerTakeProfitConvergence.id)
                .limit(max(0, int(limit)))
                .all()
            )
        ]
    completed = 0
    for convergence_id in identifiers:
        if rollout_mode == "shadow":
            plan_trigger_take_profit_convergence(
                session_factory,
                convergence_id=convergence_id,
                deepcoin_client=deepcoin_client,
                contract_spec_provider=contract_spec_provider,
                planned_at=processed_at,
            )
            continue
        result = execute_trigger_take_profit_convergence(
            session_factory,
            convergence_id=convergence_id,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=contract_spec_provider,
            executed_at=processed_at,
            group_trading_mode_provider=group_trading_mode_provider,
        )
        if result.get("status") in {"submitted", "conflicted", "submit_unknown"}:
            completed += 1
    return completed


def _freeze(session_factory, convergence_id: int, now: datetime, reason: str, error: object | None = None):
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        if convergence is not None and convergence.status in {"ready", "reserved"}:
            convergence.status = "submit_unknown" if reason.endswith("unknown") else "conflicted"
            convergence.reason_code = str(reason)
            convergence.error_json = (
                json.dumps({"type": type(error).__name__, "message": str(error)[:512]}, ensure_ascii=False)
                if error is not None else _refusal_detail_json(reason)
            )
            convergence.completed_at = now
            convergence.updated_at = now
            session.commit()
            return {"convergence_id": convergence_id, "status": convergence.status, "reason": reason}
    return {"convergence_id": convergence_id, "status": "conflicted", "reason": reason}


def _response_dict(response: object) -> dict[str, object]:
    return response if isinstance(response, dict) else {"raw": str(response)[:512]}


def _response_order_id(response: object) -> str | None:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    data = data if isinstance(data, dict) else response
    value = data.get("ordId") or data.get("orderId")
    return str(value) if value not in (None, "") else None


def _pending_contains_order_id(
    pending: list[dict[str, object]],
    *,
    order_id: str,
) -> bool:
    return any(
        normalized is not None and normalized.ord_id == str(order_id)
        for row in pending
        if isinstance(row, dict)
        for normalized in (normalize_native_tpsl(row),)
    )


def _record_preplanned_take_profit_leg_binding(
    session,
    *,
    binding,
    leg,
    pos_id: str,
    bound_legs,
) -> None:
    """Audit the A-15-1 binding of pre-planned take-profit legs.

    One row per (leg, pos_id, leg ids) so a repeated round adds nothing, and the
    evidence carries what was bound rather than a count -- a count cannot be
    checked against the ledger afterwards.
    """

    evidence = {
        "reason": "preplanned_take_profit_legs_bound_by_convergence",
        "entry_order_kind": str(leg.order_kind or ""),
        "protection_leg_ids": [int(row.id) for row in bound_legs],
        "planned_trigger_prices": [
            str(row.planned_trigger_price or "") for row in bound_legs
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "pos_id": str(pos_id),
                "execution_order_leg_id": int(leg.id),
                "protection_leg_ids": evidence["protection_leg_ids"],
                "event_type": "preplanned_take_profit_legs_bound",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    exists = (
        session.query(PositionAttributionAudit.id)
        .filter(PositionAttributionAudit.fingerprint == fingerprint)
        .first()
    )
    if exists is not None:
        return
    session.add(
        PositionAttributionAudit(
            execution_binding_id=int(binding.id),
            execution_order_leg_id=int(leg.id),
            venue=str(leg.venue or "deepcoin"),
            pos_id=str(pos_id),
            event_type="preplanned_take_profit_legs_bound",
            prior_state="planned",
            new_state="protection_recovery_pending",
            fingerprint=fingerprint,
            evidence_json=json.dumps(
                evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
            created_at=utc_now(),
        )
    )
    session.flush()


def _prepare_plan(
    session,
    *,
    convergence,
    deepcoin_client,
    contract_spec_provider,
    allow_logical_adoption: bool,
):
    if str(convergence.status) != "ready":
        return "convergence_not_ready"
    leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
    binding = session.get(ExecutionBinding, convergence.execution_binding_id)
    if (
        leg is None
        or binding is None
        or int(leg.execution_binding_id) != int(binding.id)
        or str(leg.purpose) != "entry"
        # One vocabulary, shared with the planner that created this row. It used
        # to be a local ``{"trigger_limit", "market"}``, and when ``limit`` joined
        # the planner's set in phase 5 (d3e423bf) this copy was not updated: the
        # planner staged take profits for every plain limit entry and this gate
        # refused all of them as ``convergence_exact_leg_not_verified``. Sixteen
        # take-profit legs were planned that way and none reached the exchange.
        or str(leg.order_kind) not in AUTOMATIC_ENTRY_ORDER_KINDS
        or str(leg.status).lower() != "active"
        or str(leg.attribution_status) != "verified"
        or not str(convergence.pos_id or "").strip()
        or str(leg.pos_id or "") != str(convergence.pos_id)
        or str(convergence.pos_id) not in _split_ids(binding.pos_id)
    ):
        return "convergence_exact_leg_not_verified"
    pos_id = str(convergence.pos_id)
    if block_reason := protection_write_block_reason(session, pos_id=pos_id):
        return f"convergence_{block_reason}"
    inst_id = f"{str(binding.symbol).upper()}-USDT-SWAP"
    try:
        positions = deepcoin_client.list_positions(inst_id=inst_id)
        pending = read_complete_pending_tpsl_snapshot(
            deepcoin_client,
            instrument_id=inst_id,
        )
    except Exception:
        return "convergence_exchange_preflight_unavailable"
    try:
        position = _exact_live_position(
            [row for row in positions if isinstance(row, dict)],
            pos_id=pos_id,
            inst_id=inst_id,
            side=str(binding.side).lower(),
        )
    except Exception:
        return "convergence_exact_live_position_not_verified"
    if detail := _pending_alias_conflict_detail(pending):
        return _RefusalReason("convergence_pending_alias_conflict", detail)
    size = _positive_decimal(position.get("pos") or position.get("size"))
    assert size is not None
    stop_rows = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.execution_binding_id == binding.id)
        .filter(PositionProtectionLedger.execution_order_leg_id == leg.id)
        .filter(PositionProtectionLedger.pos_id == pos_id)
        .filter(PositionProtectionLedger.status == "verified")
        .filter(PositionProtectionLedger.purpose.in_(("stop_loss", "combined")))
        .all()
    )
    verified_primary = _verified_native_primary_stop_row(
        stop_rows=stop_rows,
        position=position,
        open_positions=positions,
        pending=pending,
        position_size=size,
    )
    has_backup = has_verified_exact_backup_stop(
        session,
        binding_id=int(binding.id),
        leg_id=int(leg.id),
        pos_id=pos_id,
        inst_id=inst_id,
        side=str(binding.side).lower(),
        pending=pending,
        position=position,
        open_positions=positions,
    )
    if verified_primary is None and not has_backup:
        return "convergence_verified_stop_missing"
    targets = _targets(convergence.desired_take_profits_json)
    if isinstance(targets, str):
        return targets
    try:
        spec = (
            contract_spec_provider.get_contract_spec(inst_id)
            if contract_spec_provider is not None
            else None
        )
    except Exception:
        return "convergence_target_contract_spec_unavailable"
    if spec is None:
        return "convergence_target_contract_spec_unavailable"
    sizes = _allocate_sizes(
        size,
        [allocation for _, allocation in targets],
        quantity_step=getattr(spec, "quantity_step", None),
        minimum_quantity=getattr(spec, "min_quantity", None),
    )
    if isinstance(sizes, str):
        return sizes
    common = {
        "instType": "SWAP", "instId": inst_id, "posId": pos_id,
        "posSide": str(binding.side).lower(), "mrgPosition": "split",
        "tdMode": str(binding.margin_mode).lower(),
    }
    payloads = [
        {
            **common, "tpTriggerPx": price, "tpTriggerPxType": "last",
            "tpOrdPx": "-1", "sz": _decimal_text(quantity),
        }
        for (price, _), quantity in zip(targets, sizes)
        if quantity > 0
    ]
    if not payloads:
        return "convergence_target_size_invalid"
    existing_protection_targets = (
        session.query(PositionProtectionLeg.id)
        .filter(PositionProtectionLeg.execution_order_leg_id == leg.id)
        .filter(PositionProtectionLeg.role == "take_profit")
        .count()
    )
    if existing_protection_targets == 0:
        try:
            if verified_primary is not None:
                from telegram_kol_research.position_protection_legs import (
                    materialize_verified_position_protection,
                )

                materialize_verified_position_protection(
                    session,
                    venue="deepcoin",
                    execution_order_leg_id=int(leg.id),
                    pos_id=pos_id,
                    primary_order_id=str(verified_primary.order_id),
                    primary_stop=str(verified_primary.trigger_price),
                    take_profits=[
                        (payload["tpTriggerPx"], payload["sz"]) for payload in payloads
                    ],
                )
            else:
                for index, payload in enumerate(payloads, start=1):
                    target = create_or_get_protection_leg(
                        session,
                        venue="deepcoin",
                        execution_order_leg_id=int(leg.id),
                        role="take_profit",
                        leg_index=index,
                        planned_trigger_price=payload["tpTriggerPx"],
                        planned_size=payload["sz"],
                    )
                    bind_filled_position(session, target, pos_id=pos_id)
        except ValueError:
            return "convergence_protection_leg_conflict"
    else:
        # A-15-1 gate 3. The branch above only builds protection legs when this
        # entry has none. A plain limit entry always has some: recovery_live_
        # submit._create_trigger_protection_leg_plan writes one ``planned`` row
        # per staged tier at submit time. Nothing then bound them, because the
        # one online binder is reached only for ``trigger_limit`` entries that
        # also carried a stop and a take profit on the entry request -- a plain
        # limit entry carries neither condition. So the legs stayed ``planned``
        # with an empty ``pos_id``, the matcher below (which selects by
        # ``pos_id``) found none, and every round ended in
        # ``convergence_protection_leg_conflict``. Measured on a copy of
        # production before this branch existed.
        #
        # Binding here rather than widening the online adoption path keeps the
        # change inside this file: the adoption path's own conditions (entry
        # kind, and a stop and take profit on the entry request) are not
        # touched. bind_verified_filled_position_protection re-checks that the
        # entry leg is active, verified, and carries exactly this pos_id, and
        # raises otherwise -- so an entry that does not own this position
        # cannot bind its legs.
        unbound_targets = (
            session.query(PositionProtectionLeg)
            .filter(PositionProtectionLeg.execution_order_leg_id == leg.id)
            .filter(PositionProtectionLeg.role == "take_profit")
            .filter(PositionProtectionLeg.status == "planned")
            .filter(PositionProtectionLeg.exchange_order_id.is_(None))
            .filter(
                (PositionProtectionLeg.pos_id.is_(None))
                | (PositionProtectionLeg.pos_id == "")
            )
            .order_by(PositionProtectionLeg.id.asc())
            .all()
        )
        if unbound_targets:
            from telegram_kol_research.position_protection_legs import (
                bind_verified_filled_position_protection,
            )

            try:
                bind_verified_filled_position_protection(
                    session,
                    execution_order_leg_id=int(leg.id),
                    pos_id=pos_id,
                )
            except ValueError:
                return "convergence_protection_leg_conflict"
            _record_preplanned_take_profit_leg_binding(
                session,
                binding=binding,
                leg=leg,
                pos_id=pos_id,
                bound_legs=unbound_targets,
            )
    active_orders = (
        session.query(PositionTakeProfitOrder)
        .filter(PositionTakeProfitOrder.venue == "deepcoin")
        .filter(PositionTakeProfitOrder.execution_binding_id == binding.id)
        .filter(PositionTakeProfitOrder.execution_order_leg_id == leg.id)
        .filter(PositionTakeProfitOrder.pos_id == pos_id)
        .filter(PositionTakeProfitOrder.status == "active")
        .order_by(PositionTakeProfitOrder.id.asc())
        .all()
    )
    desired_by_price = {payload["tpTriggerPx"]: payload for payload in payloads}
    known_order_position_ids = {
        str(row.order_id): str(row.pos_id)
        for row in session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == "deepcoin")
        .filter(PositionProtectionLedger.status == "verified")
        .filter(PositionProtectionLedger.order_id.is_not(None))
        .all()
        if str(row.order_id or "").strip() and str(row.pos_id or "").strip()
    }
    satisfied_order_ids: set[str] = set()
    for order in active_orders:
        payload = desired_by_price.get(str(order.trigger_price))
        if payload is None or str(order.size_text or "") != payload["sz"]:
            return "convergence_owned_take_profit_mismatch"
        verified_take_profit = _verified_native_take_profit(
            position=position,
            open_positions=positions,
            pending=pending,
            order_id=str(order.order_id),
            payload=payload,
        )
        if verified_take_profit is None:
            return "convergence_take_profit_missing_on_exchange"
        logical_matches = _matching_take_profit_protection_legs(
            session,
            execution_binding_id=int(binding.id),
            execution_order_leg_id=int(leg.id),
            pos_id=pos_id,
            trigger_price=payload["tpTriggerPx"],
        )
        if len(logical_matches) != 1:
            return "convergence_protection_leg_conflict"
        logical_leg = logical_matches[0]
        if logical_leg.exchange_order_id is None:
            if str(logical_leg.status or "") not in {
                "waiting_fill",
                "protection_recovery_pending",
            }:
                return "convergence_protection_leg_conflict"
            if allow_logical_adoption:
                bind_verified_exchange_order(
                    session,
                    logical_leg,
                    exchange_order_id=str(order.order_id),
                    readback_evidence={
                        "source": "existing_native_tpsl_pending_readback",
                        "native_tpsl": verified_take_profit.raw,
                    },
                )
        elif (
            str(logical_leg.exchange_order_id) != str(order.order_id)
            or str(logical_leg.status or "") != "verified"
        ):
            return "convergence_protection_leg_conflict"
        satisfied_order_ids.add(str(order.order_id))
    if _unowned_pending_take_profit_present(
        pending=pending,
        inst_id=inst_id,
        side=str(binding.side).lower(),
        pos_id=pos_id,
        owned_order_ids=satisfied_order_ids,
        known_order_position_ids=known_order_position_ids,
    ):
        return "convergence_unowned_take_profit_present"
    missing_payloads = [
        payload
        for payload in payloads
        if not any(
            str(order.trigger_price) == payload["tpTriggerPx"]
            and str(order.size_text or "") == payload["sz"]
            for order in active_orders
        )
    ]
    for payload in missing_payloads:
        logical_matches = _matching_take_profit_protection_legs(
            session,
            execution_binding_id=int(binding.id),
            execution_order_leg_id=int(leg.id),
            pos_id=pos_id,
            trigger_price=payload["tpTriggerPx"],
        )
        if (
            len(logical_matches) != 1
            or logical_matches[0].exchange_order_id is not None
            or str(logical_matches[0].status or "")
            not in {"waiting_fill", "protection_recovery_pending"}
        ):
            return "convergence_protection_leg_conflict"
    if not missing_payloads:
        return "convergence_take_profit_already_converged"
    return [], missing_payloads, _decimal_text(size)


def _revalidate_take_profit_write(
    session_factory,
    *,
    convergence_id: int,
    deepcoin_client,
    payload: dict[str, str],
    expected_position_size: str | None,
) -> str | None:
    """Re-prove the exact position and protection absence before every TP write."""

    try:
        positions = list(deepcoin_client.list_positions(inst_id=payload["instId"]))
        position = _exact_live_position(
            positions,
            pos_id=payload["posId"],
            inst_id=payload["instId"],
            side=payload["posSide"],
        )
        pending = read_complete_pending_tpsl_snapshot(
            deepcoin_client,
            instrument_id=payload["instId"],
        )
    except Exception:
        return "convergence_exchange_prewrite_snapshot_incomplete"
    if detail := _pending_alias_conflict_detail(pending):
        return _RefusalReason(
            "convergence_pending_alias_conflict_before_write", detail
        )
    if _positive_decimal(position.get("pos")) != _positive_decimal(
        expected_position_size
    ):
        return "convergence_position_size_changed_before_write"
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        if convergence is None or str(convergence.status) != "reserved":
            return "convergence_reservation_lost_before_write"
        if block_reason := protection_write_block_reason(
            session, pos_id=str(convergence.pos_id)
        ):
            return f"convergence_{block_reason}"
        active_orders = (
            session.query(PositionTakeProfitOrder)
            .filter(PositionTakeProfitOrder.venue == "deepcoin")
            .filter(
                PositionTakeProfitOrder.execution_binding_id
                == convergence.execution_binding_id
            )
            .filter(
                PositionTakeProfitOrder.execution_order_leg_id
                == convergence.execution_order_leg_id
            )
            .filter(PositionTakeProfitOrder.pos_id == convergence.pos_id)
            .filter(PositionTakeProfitOrder.status == "active")
            .all()
        )
        owned_order_ids = {
            str(order.order_id)
            for order in active_orders
            if str(order.order_id or "").strip()
        }
        known_order_position_ids = {
            str(row.order_id): str(row.pos_id)
            for row in session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.venue == "deepcoin")
            .filter(PositionProtectionLedger.status == "verified")
            .filter(PositionProtectionLedger.order_id.is_not(None))
            .all()
            if str(row.order_id or "").strip() and str(row.pos_id or "").strip()
        }
        if _unowned_pending_take_profit_present(
            pending=pending,
            inst_id=payload["instId"],
            side=payload["posSide"],
            pos_id=payload["posId"],
            owned_order_ids=owned_order_ids,
            known_order_position_ids=known_order_position_ids,
        ):
            return "convergence_unowned_take_profit_before_write"
        if any(
            str(order.trigger_price) == payload["tpTriggerPx"]
            and str(order.size_text or "") == payload["sz"]
            for order in active_orders
        ):
            return "convergence_target_already_present_before_write"
    return None


def has_verified_exact_backup_stop(
    session,
    *,
    binding_id: int,
    leg_id: int,
    pos_id: str,
    inst_id: str,
    side: str,
    pending: list[dict[str, object]],
    position: dict[str, object],
    open_positions: list[dict[str, object]],
) -> bool:
    """Require persisted exact ownership plus a same-order pending exchange read-back."""

    rows = (
        session.query(PositionBackupStopOrder)
        .filter(PositionBackupStopOrder.execution_binding_id == binding_id)
        .filter(PositionBackupStopOrder.execution_order_leg_id == leg_id)
        .filter(PositionBackupStopOrder.pos_id == pos_id)
        .filter(PositionBackupStopOrder.instrument_id == inst_id)
        .filter(PositionBackupStopOrder.side == side)
        .filter(PositionBackupStopOrder.status == "active")
        .filter(PositionBackupStopOrder.order_id.is_not(None))
        .order_by(PositionBackupStopOrder.id.asc())
        .all()
    )
    if not _live_position_aliases_match(
        position, pos_id=pos_id, inst_id=inst_id, side=side
    ):
        return False
    for row in rows:
        try:
            request = json.loads(str(row.request_json or "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        if (
            str(request.get("instId") or "").upper() != inst_id
            or str(request.get("posId") or "") != pos_id
            or str(request.get("posSide") or "").lower() != side
            or str(request.get("slOrdPx") or request.get("price") or "") != "-1"
            or _positive_decimal(
                request.get("slTriggerPx")
                or request.get("slTriggerPrice")
                or request.get("triggerPrice")
            )
            != _positive_decimal(row.trigger_price)
        ):
            continue
        if not native_tpsl_order_id_is_unique(pending, str(row.order_id)):
            continue
        same_order_pending = [
            item
            for item in pending
            if isinstance(item, dict)
            and _native_tpsl_aliases_consistent(item)
            and _text_alias_values(
                item, "ordId", "orderId", "order_id", "id"
            )
            == {str(row.order_id)}
        ]
        if len(same_order_pending) != 1:
            continue
        pending_match = same_order_pending[0]
        if not (
            _text_alias_values(
                pending_match,
                "instId",
                "instrument_id",
                "instrumentId",
                transform=str.upper,
            )
            == {inst_id}
            and _text_alias_values(
                pending_match, "posId", "pos_id", "closePosId"
            )
            == {pos_id}
            and protection_order_position_sides(pending_match)
            == {_normalize_position_side_alias(side)}
            and _numeric_alias_values(
                pending_match,
                "slTriggerPx",
                "slTriggerPrice",
                "closeSLTriggerPrice",
            )
            == {_positive_decimal(row.trigger_price)}
            and _native_stop_loss_is_market(pending_match)
        ):
            continue
        match = match_native_tpsl_order(
            position,
            [pending_match],
            NativeTpslExpectation(
                purpose="stop_loss", trigger_price=str(row.trigger_price), size="0",
                ord_id=str(row.order_id),
            ),
            open_positions=[item for item in open_positions if isinstance(item, dict)],
        )
        if match.status == "verified":
            return True
    return False


def has_verified_exact_owned_stop(
    session,
    *,
    binding_id: int,
    leg_id: int,
    pos_id: str,
    inst_id: str,
    side: str,
    stop_rows: list[PositionProtectionLedger],
    pending: list[dict[str, object]],
    position: dict[str, object],
    open_positions: list[dict[str, object]],
    position_size: Decimal,
) -> bool:
    """Accept one verified exact stop source; never require two independent stops."""

    return _has_verified_native_primary_stop(
        stop_rows=stop_rows,
        position=position,
        open_positions=open_positions,
        pending=pending,
        position_size=position_size,
    ) or has_verified_exact_backup_stop(
        session,
        binding_id=binding_id,
        leg_id=leg_id,
        pos_id=pos_id,
        inst_id=inst_id,
        side=side,
        pending=pending,
        position=position,
        open_positions=open_positions,
    )


def exact_owned_stop_evidence_fingerprint(
    session,
    *,
    binding_id: int,
    leg_id: int,
    pos_id: str,
    inst_id: str,
    side: str,
    stop_rows: list[PositionProtectionLedger],
    pending: list[dict[str, object]],
    position: dict[str, object],
    open_positions: list[dict[str, object]],
    position_size: Decimal,
) -> str | None:
    """Fingerprint the bounded local-and-exchange proof that unlocked TP writes."""

    if not has_verified_exact_owned_stop(
        session,
        binding_id=binding_id,
        leg_id=leg_id,
        pos_id=pos_id,
        inst_id=inst_id,
        side=side,
        stop_rows=stop_rows,
        pending=pending,
        position=position,
        open_positions=open_positions,
        position_size=position_size,
    ):
        return None
    backup_rows = (
        session.query(PositionBackupStopOrder)
        .filter(PositionBackupStopOrder.execution_binding_id == binding_id)
        .filter(PositionBackupStopOrder.execution_order_leg_id == leg_id)
        .filter(PositionBackupStopOrder.pos_id == pos_id)
        .filter(PositionBackupStopOrder.status == "active")
        .filter(PositionBackupStopOrder.order_id.is_not(None))
        .all()
    )
    owned_order_ids = sorted({
        str(row.order_id)
        for row in [*stop_rows, *backup_rows]
        if str(row.order_id or "").strip()
    })
    exchange_rows = []
    for raw in pending:
        if not isinstance(raw, dict):
            continue
        if not _native_tpsl_aliases_consistent(raw):
            continue
        order_id = str(raw.get("ordId") or raw.get("orderId") or "")
        if order_id not in owned_order_ids:
            continue
        exchange_rows.append({
            key: raw.get(key)
            for key in (
                "instId", "posId", "closePosId", "posSide", "ordId",
                "orderId", "triggerOrderType", "slTriggerPx", "slOrdPx",
                "triggerPrice", "orderType", "sz",
            )
            if raw.get(key) is not None
        })
    evidence = {
        "binding_id": binding_id,
        "leg_id": leg_id,
        "pos_id": pos_id,
        "inst_id": inst_id,
        "side": side,
        "owned_order_ids": owned_order_ids,
        "pending": exchange_rows,
    }
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _has_verified_native_primary_stop(
    *,
    stop_rows: list[PositionProtectionLedger],
    position: dict[str, object],
    open_positions: list[dict[str, object]],
    pending: list[dict[str, object]],
    position_size: Decimal,
) -> bool:
    return _verified_native_primary_stop_row(
        stop_rows=stop_rows,
        position=position,
        open_positions=open_positions,
        pending=pending,
        position_size=position_size,
    ) is not None


def _verified_native_primary_stop_row(
    *,
    stop_rows: list[PositionProtectionLedger],
    position: dict[str, object],
    open_positions: list[dict[str, object]],
    pending: list[dict[str, object]],
    position_size: Decimal,
) -> PositionProtectionLedger | None:
    for row in stop_rows:
        if not row.order_id or row.trigger_price is None:
            continue
        if not native_tpsl_order_id_is_unique(pending, str(row.order_id)):
            continue
        for size in (position_size, Decimal("0")):
            match = match_native_tpsl_order(
                position,
                [
                    item
                    for item in pending
                    if isinstance(item, dict)
                    and _native_tpsl_aliases_consistent(item)
                ],
                NativeTpslExpectation(
                    purpose="stop_loss",
                    trigger_price=str(row.trigger_price),
                    size=size,
                    ord_id=str(row.order_id),
                ),
                open_positions=[item for item in open_positions if isinstance(item, dict)],
            )
            if match.status == "verified" and match.order is not None:
                return row
    return None


def _verified_native_take_profit(
    *,
    position: dict[str, object],
    open_positions: list[dict[str, object]],
    pending: list[dict[str, object]],
    order_id: str,
    payload: dict[str, str],
) -> NativeTpslOrder | None:
    if not native_tpsl_order_id_is_unique(pending, order_id):
        return None
    match = match_native_tpsl_order(
        position,
        [
            item
            for item in pending
            if isinstance(item, dict)
            and _native_tpsl_aliases_consistent(item)
        ],
        NativeTpslExpectation(
            purpose="take_profit",
            trigger_price=payload["tpTriggerPx"], size=payload["sz"], ord_id=order_id,
        ),
        open_positions=[item for item in open_positions if isinstance(item, dict)],
    )
    if match.status != "verified" or match.order is None:
        return None
    if not native_tpsl_take_profit_is_market(match.order.raw):
        return None
    return match.order


def _unowned_pending_take_profit_present(
    *,
    pending: list[dict[str, object]],
    inst_id: str,
    side: str,
    pos_id: str,
    owned_order_ids: set[str],
    known_order_position_ids: dict[str, str],
) -> bool:
    """Fail closed on any TP that could affect this exact side but lacks local ownership."""

    for raw in pending:
        if not isinstance(raw, dict):
            continue
        if not _row_has_take_profit_fields(raw):
            continue
        order_types = _text_alias_values(
            raw, "triggerOrderType", "trigger_order_type", transform=str.upper
        )
        if order_types and order_types != {"TPSL"}:
            if len(order_types) == 1:
                continue
            return True
        if (
            order_types != {"TPSL"}
            or not _native_tpsl_aliases_consistent(raw)
        ):
            return True
        instrument_ids = _text_alias_values(
            raw,
            "instId",
            "instrument_id",
            "instrumentId",
            transform=str.upper,
        )
        sides = protection_order_position_sides(raw)
        if len(instrument_ids) != 1 or len(sides) != 1:
            return True
        if (
            instrument_ids != {inst_id.upper()}
            or sides != {_normalize_position_side_alias(side)}
        ):
            continue
        order = normalize_native_tpsl(raw)
        if order is None or order.take_profit_trigger_price is None:
            return True
        if order.inst_id != inst_id or order.pos_side != side:
            continue
        if order.pos_id is not None and order.pos_id != pos_id:
            continue
        if (
            order.ord_id is not None
            and known_order_position_ids.get(order.ord_id) not in (None, pos_id)
        ):
            continue
        if order.ord_id not in owned_order_ids:
            return True
    return False


def _exact_live_position(
    positions: list[dict[str, object]],
    *,
    pos_id: str,
    inst_id: str,
    side: str,
) -> dict[str, object]:
    if any(
        not _live_position_aliases_consistent(row)
        for row in positions
        if isinstance(row, dict)
    ):
        raise RuntimeError("live_position_snapshot_alias_conflict")
    matches = [
        row for row in positions
        if isinstance(row, dict)
        and _live_position_aliases_match(
            row, pos_id=pos_id, inst_id=inst_id, side=side
        )
    ]
    if len(matches) != 1:
        raise RuntimeError("live_position_snapshot_not_unique_or_mismatched")
    return matches[0]


def _live_position_aliases_match(
    row: dict[str, object], *, pos_id: str, inst_id: str, side: str
) -> bool:
    return (
        _live_position_aliases_consistent(row)
        and
        _text_alias_values(row, "posId", "pos_id") == {str(pos_id)}
        and _text_alias_values(
            row, "instId", "instrument_id", "instrumentId", transform=str.upper
        )
        == {str(inst_id).upper()}
        and _text_alias_values(
            row,
            "posSide",
            "pos_side",
            "side",
            transform=_normalize_position_side_alias,
        )
        == {_normalize_position_side_alias(side)}
        and _text_alias_values(
            row,
            "mrgPosition",
            "posMode",
            "positionMode",
            transform=str.lower,
        )
        == {"split"}
    )


def _live_position_aliases_consistent(row: dict[str, object]) -> bool:
    size_values = _numeric_alias_values(row, "pos", "size", "sz")
    return (
        len(_text_alias_values(row, "posId", "pos_id")) == 1
        and len(
            _text_alias_values(
                row,
                "instId",
                "instrument_id",
                "instrumentId",
                transform=str.upper,
            )
        )
        == 1
        and len(
            _text_alias_values(
                row,
                "posSide",
                "pos_side",
                "side",
                transform=_normalize_position_side_alias,
            )
        )
        == 1
        and _text_alias_values(
            row,
            "mrgPosition",
            "posMode",
            "positionMode",
            transform=str.lower,
        )
        == {"split"}
        and len(size_values) == 1
        and next(iter(size_values)) > 0
    )


def _native_tpsl_aliases_consistent(row: dict[str, object]) -> bool:
    text_groups = (
        (("ordId", "orderId", "order_id", "id"), str),
        (("instId", "instrument_id", "instrumentId"), str.upper),
        (("posId", "pos_id", "closePosId"), str),
        (("posSide", "pos_side"), _normalize_position_side_alias),
        (("triggerOrderType", "trigger_order_type"), str.upper),
    )
    numeric_groups = (
        ("sz", "size", "orderSize"),
        ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice"),
        ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice"),
        ("slOrdPx", "slOrderPrice"),
        ("tpOrdPx", "tpOrderPrice"),
    )
    return protection_order_sides_consistent(row) and all(
        len(_text_alias_values(row, *keys, transform=transform)) <= 1
        for keys, transform in text_groups
    ) and all(len(_numeric_alias_values(row, *keys)) <= 1 for keys in numeric_groups)


def _normalize_position_side_alias(value: str) -> str:
    normalized = str(value).strip().lower()
    return {"buy": "long", "sell": "short"}.get(normalized, normalized)


# Every field the pending-row alias check reads, so a refusal can quote the
# exact payload text it decided on rather than only the conclusion.
_ALIAS_DECISION_KEYS = (
    "ordId", "orderId", "order_id", "id",
    "instId", "instrument_id", "instrumentId",
    "posId", "pos_id", "closePosId",
    "posSide", "pos_side", "side",
    "triggerOrderType", "trigger_order_type",
    "sz", "size", "orderSize",
    "slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice",
    "tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice",
    "slOrdPx", "slOrderPrice", "tpOrdPx", "tpOrderPrice",
)


def _pending_alias_conflict_detail(pending) -> dict[str, object] | None:
    """Veto only on protection rows whose own aliases disagree with each other.

    An unfilled entry trigger order carries ``closeSLTriggerPrice`` for the stop
    it will attach on fill, and its ``side`` opens rather than closes the
    position, so protective-close invariants do not apply to it.
    ``is_protection_order_row`` decides that structurally, from the order-type
    field alone.
    """

    conflicts = [
        row
        for row in pending
        if isinstance(row, dict)
        and is_protection_order_row(row)
        and _row_has_protection_fields(row)
        and not _native_tpsl_aliases_consistent(row)
    ]
    if not conflicts:
        return None
    return {
        "conflicting_rows": [
            {key: row[key] for key in _ALIAS_DECISION_KEYS if key in row}
            for row in conflicts
        ]
    }


def _row_has_protection_fields(row: dict[str, object]) -> bool:
    return any(
        row.get(key) not in (None, "")
        for key in (
            "slTriggerPx",
            "slTriggerPrice",
            "closeSLTriggerPrice",
            "tpTriggerPx",
            "tpTriggerPrice",
            "closeTPTriggerPrice",
        )
    )


def _row_has_take_profit_fields(row: dict[str, object]) -> bool:
    """Report whether a row actually carries a take-profit trigger price.

    DeepCoin returns a stop-only ``TPSL`` with its take-profit aliases present
    but zeroed (``tpTriggerPrice="0"``, ``closeTPTriggerPrice="0"``), which is
    the exchange saying "no take profit" rather than "a take profit I cannot
    read".  Treating that literal ``0`` as a present field made
    ``_unowned_pending_take_profit_present`` reject every convergence on an
    instrument that merely had a stop attached: ``normalize_native_tpsl``
    resolves the same aliases through ``_first_positive_decimal`` and returns
    ``None`` for ``0``, so the caller concluded an unreadable take profit was
    live.  The two now agree that only a positive value is a take-profit price.

    A value that cannot be parsed at all still counts as present, so an
    unrecognized payload keeps failing closed instead of being waved through.
    """

    # A-14: same keys, same order, same fail-closed rule, now named. Kept
    # separate from the price reader on purpose -- see that function's
    # docstring for why "unparseable counts as present" must not be shared.
    return take_profit_present_failing_closed(row)


def _text_alias_values(
    row: dict[str, object], *keys: str, transform=lambda value: value
) -> set[str]:
    return {
        transform(str(row[key]).strip())
        for key in keys
        if row.get(key) is not None and str(row[key]).strip()
    }


def _numeric_alias_values(
    row: dict[str, object], *keys: str
) -> set[Decimal]:
    values: set[Decimal] = set()
    for key in keys:
        if row.get(key) in (None, ""):
            continue
        parsed = _decimal(row[key])
        if parsed is None:
            return {Decimal("0"), Decimal("1")}
        values.add(parsed)
    return values


def _targets(value: str):
    try:
        rows = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return "convergence_target_plan_invalid"
    if not isinstance(rows, list) or not rows:
        return "convergence_target_plan_invalid"
    targets: list[tuple[str, Decimal]] = []
    total = Decimal("0")
    for row in rows:
        if not isinstance(row, dict):
            return "convergence_target_plan_invalid"
        price = _positive_decimal(row.get("price"))
        allocation = _positive_decimal(row.get("allocation_pct"))
        if price is None or allocation is None:
            return "convergence_target_plan_invalid"
        targets.append((_decimal_text(price), allocation))
        total += allocation
    return targets if total == Decimal("100") else "convergence_target_plan_invalid"


def _allocate_sizes(
    size: Decimal,
    allocations: list[Decimal],
    *,
    quantity_step: object,
    minimum_quantity: object,
):
    """Stage as many take-profit tiers as the position can actually pay for.

    A-5 task 5, user decision ``small_position_tiering_shrink_to_allocatable``
    (2026-09-08). Position 1001125178552543 held three lots against a
    50/30/20 plan: 1.5/0.9/0.6 lots, two tiers under the one-lot minimum, so
    the whole convergence failed closed with
    ``convergence_target_size_below_minimum`` and the position ended up with a
    stop and no take-profit at all. Neither half of that was wrong on its own
    -- three tiers really are impossible -- but "no take-profit" was never the
    intended answer.

    So the tier *count* shrinks until the position can fill it, down to a
    single tier, and the shares of the surviving tiers are renormalised to
    100%. The caller zips the returned sizes against its ordered targets, so
    the tiers that survive are always the nearest ones. Only a
    below-minimum failure is retried this way: a step or plan error says the
    inputs are wrong and fewer tiers would not make them right.
    """

    outcome = "convergence_target_size_below_minimum"
    for count in range(len(allocations), 0, -1):
        scaled = _rescale_allocations(allocations[:count])
        if scaled is None:
            continue
        outcome = _allocate_exact_sizes(
            size,
            scaled,
            quantity_step=quantity_step,
            minimum_quantity=minimum_quantity,
        )
        if not isinstance(outcome, str):
            return outcome
        if outcome != "convergence_target_size_below_minimum":
            return outcome
    return outcome


def _allocate_exact_sizes(
    size: Decimal,
    allocations: list[Decimal],
    *,
    quantity_step: object,
    minimum_quantity: object,
):
    try:
        plan = build_take_profit_plan(
            prices=range(1, len(allocations) + 1),
            side="long",
            configured_allocations=allocations,
            quantity=size,
            quantity_step=quantity_step,
            minimum_quantity=minimum_quantity,
        )
    except TakeProfitPlanError as exc:
        if "minimum" in str(exc):
            return "convergence_target_size_below_minimum"
        return "convergence_target_size_step_unverified"
    quantities = [Decimal(str(leg.quantity)) for leg in plan.legs]
    return quantities if all(quantity > 0 for quantity in quantities) else "convergence_target_size_invalid"


def _rescale_allocations(allocations: list[Decimal]) -> list[Decimal] | None:
    """Renormalise a prefix of the plan's shares to exactly 100.

    Exactly, not approximately: ``build_take_profit_plan`` silently replaces a
    set of shares that does not sum to 100 with its own defaults, so a rounding
    remainder here would quietly change the trader's requested distribution.
    The remainder is therefore carried by the last surviving tier.
    """

    if not allocations:
        return None
    total = sum(allocations, Decimal("0"))
    if total <= 0:
        return None
    if total == Decimal("100"):
        return list(allocations)
    scaled = [
        (value * Decimal("100") / total).quantize(Decimal("0.000001"))
        for value in allocations[:-1]
    ]
    remainder = Decimal("100") - sum(scaled, Decimal("0"))
    if remainder <= 0:
        return None
    scaled.append(remainder)
    return scaled


def _positive_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _matching_take_profit_protection_legs(
    session,
    *,
    execution_binding_id: int,
    execution_order_leg_id: int,
    pos_id: str,
    trigger_price: object,
) -> list[PositionProtectionLeg]:
    expected = _positive_decimal(trigger_price)
    if expected is None:
        return []
    rows = (
        session.query(PositionProtectionLeg)
        .filter(PositionProtectionLeg.venue == "deepcoin")
        .filter(
            PositionProtectionLeg.execution_binding_id
            == int(execution_binding_id)
        )
        .filter(
            PositionProtectionLeg.execution_order_leg_id
            == int(execution_order_leg_id)
        )
        .filter(PositionProtectionLeg.role == "take_profit")
        .filter(PositionProtectionLeg.pos_id == str(pos_id))
        .order_by(PositionProtectionLeg.id.asc())
        .all()
    )
    return [
        row
        for row in rows
        if _positive_decimal(row.planned_trigger_price) == expected
    ]


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _split_ids(value: object) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def _native_stop_loss_is_market(payload: dict[str, object]) -> bool:
    values = [
        payload.get(key)
        for key in ("slOrdPx", "slPrice")
        if payload.get(key) not in (None, "")
    ]
    if not values:
        return False
    return all(
        (price := _decimal(value)) is not None
        and price in {Decimal("-1"), Decimal("0")}
        for value in values
    )


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None
