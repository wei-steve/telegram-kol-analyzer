"""Bounded recovery of one proven stale contextual reanalysis claim."""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Callable

from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.context_resolution_worker import (
    DEFAULT_STALE_AFTER,
    TERMINAL_INSTRUCTION_STATUSES,
)
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MessageInstructionItem,
    RuntimeIncident,
    StrategyManagementBatch,
    StrategyRevisionBatch,
)


_FINGERPRINT_PATTERN = re.compile(r"[a-f0-9]{64}")
_MAX_CAPTURES = 32


class RuntimeAgentContextClaimRecoveryError(ValueError):
    """The requested contextual claim recovery is unsafe or unavailable."""


@dataclass(frozen=True, slots=True)
class _RecoveryProof:
    context_attempt_id: int

    def as_mapping(self) -> dict[str, Any]:
        return {
            "applicable": True,
            "safe_queue_restored": True,
            "claim_status": "pending",
            "business_write_owned": False,
            "context_attempt_id": self.context_attempt_id,
        }


class RuntimeAgentContextClaimRecovery:
    """Compare-and-set one stale context claim and prove that commit once."""

    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._captures: OrderedDict[int, _RecoveryProof] = OrderedDict()
        self._lock = Lock()

    def consume_verification(
        self,
        *,
        incident_id: int,
    ) -> dict[str, Any] | None:
        with self._lock:
            proof = self._captures.pop(int(incident_id), None)
        return proof.as_mapping() if proof is not None else None

    def recover(
        self,
        *,
        incident_id: int,
        idempotency_key: str,
        expected_fingerprint: str,
    ) -> bool:
        if (
            isinstance(incident_id, bool)
            or not isinstance(incident_id, int)
            or incident_id < 1
            or not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 255
            or not isinstance(expected_fingerprint, str)
            or not _FINGERPRINT_PATTERN.fullmatch(expected_fingerprint)
        ):
            raise RuntimeAgentContextClaimRecoveryError(
                "context claim recovery identity is invalid"
            )
        operation_now = self._clock()
        if not isinstance(operation_now, datetime):
            raise RuntimeAgentContextClaimRecoveryError(
                "context claim recovery clock is invalid"
            )
        database_now = (
            operation_now.replace(tzinfo=None)
            if operation_now.tzinfo is not None
            else operation_now
        )
        stale_before = database_now - DEFAULT_STALE_AFTER

        try:
            with self._session_factory() as session:
                incident = session.get(RuntimeIncident, int(incident_id))
                if (
                    incident is None
                    or incident.fingerprint != expected_fingerprint
                    or incident.incident_type != "context_worker_exhausted"
                    or incident.source_kind != "context_resolution_attempt"
                ):
                    raise RuntimeAgentContextClaimRecoveryError(
                        "context claim recovery proof is invalid"
                    )
                try:
                    summary = json.loads(incident.redacted_summary)
                    attempt_id = int(incident.source_record_id)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeAgentContextClaimRecoveryError(
                        "context claim recovery proof is invalid"
                    ) from exc
                if (
                    not isinstance(summary, dict)
                    or summary.get("claim_status") != "stale"
                    or summary.get("claim_side_effect_class") != "none"
                    or attempt_id < 1
                ):
                    raise RuntimeAgentContextClaimRecoveryError(
                        "context claim recovery proof is invalid"
                    )

                attempt = session.get(ContextResolutionAttempt, attempt_id)
                if (
                    attempt is None
                    or attempt.status != "running"
                    or not attempt.claim_token
                    or attempt.claimed_at is None
                    or attempt.claimed_at > stale_before
                    or self._business_write_owned(
                        session,
                        raw_message_id=attempt.raw_message_id,
                    )
                ):
                    raise RuntimeAgentContextClaimRecoveryError(
                        "context claim recovery target is not safe"
                    )
                observed_token = str(attempt.claim_token)
                observed_claimed_at = attempt.claimed_at
                result = session.execute(
                    update(ContextResolutionAttempt)
                    .where(
                        ContextResolutionAttempt.id == attempt_id,
                        ContextResolutionAttempt.status == "running",
                        ContextResolutionAttempt.claim_token
                        == observed_token,
                        ContextResolutionAttempt.claimed_at
                        == observed_claimed_at,
                    )
                    .values(
                        status="pending_reanalysis",
                        claim_token=None,
                        claimed_at=None,
                        next_attempt_at=database_now,
                        updated_at=database_now,
                    )
                )
                if result.rowcount != 1:
                    session.rollback()
                    raise RuntimeAgentContextClaimRecoveryError(
                        "context claim recovery lost compare-and-set"
                    )
                session.commit()
        except RuntimeAgentContextClaimRecoveryError:
            raise
        except Exception as exc:
            raise RuntimeAgentContextClaimRecoveryError(
                "context claim recovery unavailable"
            ) from exc

        with self._lock:
            self._captures[int(incident_id)] = _RecoveryProof(
                context_attempt_id=attempt_id
            )
            self._captures.move_to_end(int(incident_id))
            while len(self._captures) > _MAX_CAPTURES:
                self._captures.popitem(last=False)
        return True

    @staticmethod
    def _business_write_owned(session, *, raw_message_id: int) -> bool:
        terminal_instruction = (
            session.query(MessageInstructionItem.id)
            .filter(
                MessageInstructionItem.raw_message_id
                == int(raw_message_id),
                MessageInstructionItem.status.in_(
                    sorted(TERMINAL_INSTRUCTION_STATUSES)
                ),
            )
            .first()
        )
        management_batch = (
            session.query(StrategyManagementBatch.id)
            .filter(
                StrategyManagementBatch.raw_message_id
                == int(raw_message_id)
            )
            .first()
        )
        revision_batch = (
            session.query(StrategyRevisionBatch.id)
            .filter(
                StrategyRevisionBatch.raw_message_id == int(raw_message_id)
            )
            .first()
        )
        return any(
            value is not None
            for value in (
                terminal_instruction,
                management_batch,
                revision_batch,
            )
        )
