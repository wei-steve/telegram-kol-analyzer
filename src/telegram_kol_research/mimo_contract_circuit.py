"""Durable future-message circuit state for MiMo v2 recognition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
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
    """Return durable breaker state, defaulting safely to closed."""

    with session_factory() as session:
        return _snapshot(
            session.get(MimoContractCircuitState, _SINGLETON_ID)
        )


def record_mimo_v2_outcome(
    session_factory: sessionmaker,
    *,
    outcome: str,
    observed_at: datetime | None = None,
) -> MimoContractCircuit:
    """Atomically apply one bounded v2 outcome after its provider call."""

    if outcome not in _OUTCOMES:
        raise ValueError("mimo_v2_outcome_invalid")
    now = _as_utc(observed_at or datetime.now(UTC))
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.get(MimoContractCircuitState, _SINGLETON_ID)
        if row is None:
            row = MimoContractCircuitState(
                id=_SINGLETON_ID,
                consecutive_transport_failures=0,
                is_open=False,
                updated_at=now,
            )
            session.add(row)
            session.flush()

        _apply_outcome(row, outcome=outcome, observed_at=now)
        snapshot = _snapshot(row)
        session.commit()
        return snapshot


def _apply_outcome(
    row: MimoContractCircuitState,
    *,
    outcome: str,
    observed_at: datetime,
) -> None:
    if outcome == "success":
        row.consecutive_transport_failures = 0
        row.last_success_at = observed_at
    elif outcome in _TRANSPORT_FAILURE_OUTCOMES:
        row.consecutive_transport_failures = (
            int(row.consecutive_transport_failures) + 1
        )
        if row.consecutive_transport_failures >= 3 and not row.is_open:
            row.is_open = True
            row.opened_reason = "consecutive_transport_failures"
            row.opened_at = observed_at
    elif outcome in _IMMEDIATE_OPEN_OUTCOMES and not row.is_open:
        row.is_open = True
        row.opened_reason = outcome
        row.opened_at = observed_at
    row.updated_at = observed_at


def _snapshot(row: MimoContractCircuitState | None) -> MimoContractCircuit:
    if row is None:
        return MimoContractCircuit()
    return MimoContractCircuit(
        consecutive_transport_failures=int(
            row.consecutive_transport_failures
        ),
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
