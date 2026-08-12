"""Durable future-message circuit state for MiMo v2 recognition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import MimoContractCircuitState


_SINGLETON_ID = 1
_IMMEDIATE_OPEN_OUTCOMES = frozenset(
    {"contract_validation_failed", "adapter_failure"}
)
_TRANSPORT_FAILURE_OUTCOMES = frozenset(
    {"provider_timeout", "provider_http_error", "invalid_json"}
)
_NONTECHNICAL_OUTCOMES = frozenset({"business_outcome", "safety_refusal"})
_OUTCOMES = frozenset(
    {
        "success",
        *_IMMEDIATE_OPEN_OUTCOMES,
        *_TRANSPORT_FAILURE_OUTCOMES,
        *_NONTECHNICAL_OUTCOMES,
    }
)


@dataclass(frozen=True, slots=True)
class MimoContractCircuit:
    consecutive_transport_failures: int = 0
    is_open: bool = False
    opened_reason: str | None = None
    opened_at: datetime | None = None
    last_success_at: datetime | None = None
    updated_at: datetime | None = None


def load_mimo_contract_circuit(
    session_factory: sessionmaker,
) -> MimoContractCircuit:
    """Return the durable breaker state, defaulting safely to closed."""

    with session_factory() as session:
        row = session.get(MimoContractCircuitState, _SINGLETON_ID)
        return _snapshot(row)


def record_mimo_v2_outcome(
    session_factory: sessionmaker,
    *,
    outcome: str,
    observed_at: datetime | None = None,
) -> MimoContractCircuit:
    """Apply one bounded v2 technical outcome without replaying any message."""

    if outcome not in _OUTCOMES:
        raise ValueError("mimo_v2_outcome_invalid")
    now = _as_utc(observed_at or datetime.now(UTC))
    with session_factory() as session:
        row = session.get(MimoContractCircuitState, _SINGLETON_ID)
        if row is None:
            row = MimoContractCircuitState(id=_SINGLETON_ID)
            session.add(row)
            session.flush()

        if outcome == "success":
            row.consecutive_transport_failures = 0
            row.last_success_at = now
        elif outcome in _TRANSPORT_FAILURE_OUTCOMES:
            row.consecutive_transport_failures += 1
            if (
                row.consecutive_transport_failures >= 3
                and not row.is_open
            ):
                row.is_open = True
                row.opened_reason = "consecutive_transport_failures"
                row.opened_at = now
        elif outcome in _IMMEDIATE_OPEN_OUTCOMES and not row.is_open:
            row.is_open = True
            row.opened_reason = outcome
            row.opened_at = now

        row.updated_at = now
        session.commit()
        session.refresh(row)
        return _snapshot(row)


def _snapshot(row: MimoContractCircuitState | None) -> MimoContractCircuit:
    if row is None:
        return MimoContractCircuit()
    return MimoContractCircuit(
        consecutive_transport_failures=int(row.consecutive_transport_failures),
        is_open=bool(row.is_open),
        opened_reason=row.opened_reason,
        opened_at=_as_utc(row.opened_at),
        last_success_at=_as_utc(row.last_success_at),
        updated_at=_as_utc(row.updated_at),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
