"""Bounded one-shot Telegram endpoint evidence for Runtime Agent recovery."""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    RuntimeIncident,
    StrategyManagementNotification,
)


_FINGERPRINT_PATTERN = re.compile(r"[a-f0-9]{64}")
_PROOF_FIELDS = frozenset(
    {
        "probe_complete",
        "endpoint_reachable",
        "bot_identity_available",
        "target_chat_available",
    }
)
_MAX_CAPTURES = 32


class RuntimeAgentTelegramEvidenceError(ValueError):
    """Telegram recovery evidence is unavailable, invalid, or inapplicable."""


@dataclass(frozen=True, slots=True)
class _TelegramEvidenceProof:
    probe_complete: bool
    endpoint_reachable: bool
    bot_identity_available: bool
    target_chat_available: bool

    @property
    def evidence_available(self) -> bool:
        return (
            self.probe_complete
            and self.endpoint_reachable
            and self.bot_identity_available
            and self.target_chat_available
        )

    def as_mapping(self) -> dict[str, bool]:
        return {
            "evidence_fetched": self.probe_complete,
            "evidence_available": self.evidence_available,
            "probe_complete": self.probe_complete,
            "endpoint_reachable": self.endpoint_reachable,
            "bot_identity_available": self.bot_identity_available,
            "target_chat_available": self.target_chat_available,
        }

    def as_endpoint_mapping(self) -> dict[str, bool]:
        return {
            "probe_complete": self.probe_complete,
            "endpoint_reachable": self.endpoint_reachable,
            "bot_identity_available": self.bot_identity_available,
            "target_chat_available": self.target_chat_available,
        }


def _project_proof(value: Any) -> _TelegramEvidenceProof:
    if not isinstance(value, Mapping) or set(value) != _PROOF_FIELDS:
        raise RuntimeAgentTelegramEvidenceError(
            "Telegram evidence proof is invalid"
        )
    if any(not isinstance(value[field], bool) for field in _PROOF_FIELDS):
        raise RuntimeAgentTelegramEvidenceError(
            "Telegram evidence proof is invalid"
        )
    proof = _TelegramEvidenceProof(
        probe_complete=value["probe_complete"],
        endpoint_reachable=value["endpoint_reachable"],
        bot_identity_available=value["bot_identity_available"],
        target_chat_available=value["target_chat_available"],
    )
    if (
        not proof.probe_complete
        and (
            proof.endpoint_reachable
            or proof.bot_identity_available
            or proof.target_chat_available
        )
    ) or (
        not proof.endpoint_reachable
        and (
            proof.bot_identity_available
            or proof.target_chat_available
        )
    ):
        raise RuntimeAgentTelegramEvidenceError(
            "Telegram evidence proof is inconsistent"
        )
    return proof


def project_bounded_telegram_evidence(value: Any) -> dict[str, bool]:
    """Strip a main-process probe result to the fixed sidecar projection."""

    if not isinstance(value, Mapping):
        raise RuntimeAgentTelegramEvidenceError(
            "Telegram evidence proof is invalid"
        )
    projected = {
        field: value.get(field)
        for field in _PROOF_FIELDS
    }
    return _project_proof(projected).as_endpoint_mapping()


def build_broker_telegram_evidence_provider(
    reader: Callable[[int], Mapping[str, Any]],
):
    """Build the broker's Telegram category without exposing Bot credentials."""

    def provider(request) -> dict[str, Any]:
        incident_id = int(request.incident_id)
        return {
            "data": project_bounded_telegram_evidence(reader(incident_id)),
            "evidence_refs": [f"telegram-evidence:{incident_id}"],
        }

    return provider


class RuntimeAgentTelegramEvidenceRefresh:
    """Fetch one exact Telegram endpoint proof and expose it exactly once."""

    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        reader: Callable[[str], Mapping[str, Any]],
    ) -> None:
        self._session_factory = session_factory
        self._reader = reader
        self._captures: OrderedDict[
            int, _TelegramEvidenceProof
        ] = OrderedDict()
        self._lock = Lock()

    def consume_verification(
        self,
        *,
        incident_id: int,
    ) -> dict[str, bool] | None:
        with self._lock:
            proof = self._captures.pop(int(incident_id), None)
        return proof.as_mapping() if proof is not None else None

    def is_applicable(
        self,
        *,
        incident_id: int,
        expected_fingerprint: str,
    ) -> bool:
        """Refuse unsupported durable incident families before reservation."""

        try:
            self._validate_source(
                incident_id=int(incident_id),
                expected_fingerprint=str(expected_fingerprint),
            )
        except (RuntimeAgentTelegramEvidenceError, TypeError, ValueError):
            return False
        return True

    def refresh(
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
            raise RuntimeAgentTelegramEvidenceError(
                "Telegram evidence identity is invalid"
            )
        channel = self._validate_source(
            incident_id=incident_id,
            expected_fingerprint=expected_fingerprint,
        )
        try:
            proof = _project_proof(self._reader(channel))
        except RuntimeAgentTelegramEvidenceError:
            raise
        except Exception as exc:
            raise RuntimeAgentTelegramEvidenceError(
                "Telegram evidence is unavailable"
            ) from exc
        with self._lock:
            self._captures[int(incident_id)] = proof
            self._captures.move_to_end(int(incident_id))
            while len(self._captures) > _MAX_CAPTURES:
                self._captures.popitem(last=False)
        return True

    def _validate_source(
        self,
        *,
        incident_id: int,
        expected_fingerprint: str,
    ) -> str:
        with self._session_factory() as session:
            incident = session.get(RuntimeIncident, int(incident_id))
            if (
                incident is None
                or incident.fingerprint != expected_fingerprint
                or incident.incident_type
                != "notification_delivery_failure"
            ):
                raise RuntimeAgentTelegramEvidenceError(
                    "Telegram evidence incident is invalid"
                )
            try:
                source_id = int(incident.source_record_id)
            except (TypeError, ValueError) as exc:
                raise RuntimeAgentTelegramEvidenceError(
                    "Telegram evidence source is invalid"
                ) from exc
            if source_id < 1:
                raise RuntimeAgentTelegramEvidenceError(
                    "Telegram evidence source is invalid"
                )
            if incident.source_kind == "runtime_incident_notification":
                source = session.get(RuntimeIncident, source_id)
                if (
                    source is None
                    or source.notification_status != "failed"
                ):
                    raise RuntimeAgentTelegramEvidenceError(
                        "Telegram evidence source is not failed"
                    )
                return "system_operator"
            if (
                incident.source_kind
                == "strategy_management_notification"
            ):
                source = session.get(
                    StrategyManagementNotification,
                    source_id,
                )
                if source is None or source.status != "failed":
                    raise RuntimeAgentTelegramEvidenceError(
                        "Telegram evidence source is not failed"
                    )
                return "notification"
        raise RuntimeAgentTelegramEvidenceError(
            "Telegram evidence source is unsupported"
        )
