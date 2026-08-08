"""Deterministic, bounded helpers for the durable runtime-incident ledger."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    MessageOperationContract,
    RuntimeIncident,
    RuntimeIncidentAffectedMessage,
)
from telegram_kol_research.runtime_agent_playbooks import (
    get_runtime_agent_playbook,
)


MAX_REDACTED_SUMMARY_LENGTH = 2048
MAX_DIAGNOSIS_JSON_LENGTH = 8192
MAX_EVIDENCE_REFS_JSON_LENGTH = 4096
MAX_CLAIMABLE_LIMIT = 100

_SENSITIVE_KEY_MARKERS = (
    "secret",
    "token",
    "credential",
    "cookie",
    "sessionid",
    "apikey",
    "apisecret",
    "accesstoken",
    "refreshtoken",
    "password",
    "passphrase",
    "authorization",
    "bearer",
    "privatekey",
    "dcaccesskey",
    "dcaccesssign",
    "dcaccesspassphrase",
)
_CREDENTIAL_PATTERNS = (
    re.compile(r"\bsk-[a-z0-9_-]{12,}", re.IGNORECASE),
    re.compile(r"\beyj[a-z0-9_-]{12,}\.[a-z0-9_-]{6,}\.", re.IGNORECASE),
    re.compile(r"\b\d{6,12}:[a-z0-9_-]{20,}\b", re.IGNORECASE),
    re.compile(r"\b(?:akia|asia)[a-z0-9]{16}\b", re.IGNORECASE),
    re.compile(r"\bapi[_-]?key\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bauthorization\s*:\s*bearer\s+\S+", re.IGNORECASE),
    re.compile(
        r"\bdc-access-(?:key|sign|passphrase)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_OPAQUE_VALUE_PATTERN = re.compile(r"[A-Za-z0-9_+/=-]{32,}")
_SUMMARY_FIELDS = frozenset(
    {
        "component",
        "containment",
        "business_write_owned",
        "claim_side_effect_class",
        "claim_status",
        "error_code",
        "error_type",
        "impact",
        "incident_state",
        "notification_status",
        "operation",
        "provider_status",
        "reason_code",
        "retry_count",
        "source_status",
        "worker_kind",
    }
)
_DIAGNOSIS_FIELDS = frozenset(
    {
        "attempted_queries",
        "auto_handle_eligible",
        "codex_handoff_required",
        "confidence",
        "hypothesis",
        "missing_evidence",
        "recommended_playbook",
        "remaining_risk",
        "recovery_playbook_policy",
        "shadow_playbook_policy",
    }
)
_SHADOW_POLICY_FIELDS = frozenset(
    {
        "mode",
        "policy_version",
        "nominated_playbook",
        "playbook_version",
        "accepted",
        "refusal_reasons",
        "verification_query",
        "would_execute",
        "action_executed",
    }
)
_RECOVERY_POLICY_FIELDS = frozenset(
    {
        "mode",
        "policy_version",
        "nominated_playbook",
        "playbook_version",
        "accepted",
        "refusal_reasons",
        "verification_query",
        "would_execute",
        "action_executed",
        "verification_status",
        "attempt_id",
        "evidence_references",
    }
)
_EVIDENCE_REFERENCE_PATTERN = re.compile(
    r"[a-z][a-z0-9_-]{1,31}:[A-Za-z0-9._-]{1,128}"
)
_PLAYBOOK_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{1,127}")
_CLAIMABLE_STATUS = "pending"
_CLAIMED_STATUS = "claimed"
_SEVERITY_RANKS = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
_ALLOWED_TRANSITIONS = {
    "pending": frozenset({"claimed", "closed"}),
    "claimed": frozenset({"diagnosed", "retry_pending", "escalated", "resolved"}),
    "diagnosed": frozenset({"escalated", "resolved", "closed"}),
    "retry_pending": frozenset({"claimed", "escalated", "closed"}),
    "escalated": frozenset({"resolved", "closed"}),
    "resolved": frozenset({"closed"}),
    "closed": frozenset(),
}


class RuntimeIncidentBoundsError(ValueError):
    """Raised before an unbounded or apparently sensitive value reaches storage."""


def _sensitive_key(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]+", "", key.lower())
    return any(marker in compact for marker in _SENSITIVE_KEY_MARKERS)


def _looks_like_opaque_secret(value: str) -> bool:
    for candidate in _OPAQUE_VALUE_PATTERN.findall(value):
        classes = sum(
            (
                any(character.islower() for character in candidate),
                any(character.isupper() for character in candidate),
                any(character.isdigit() for character in candidate),
                any(character in "_+/=-" for character in candidate),
            )
        )
        if classes >= 3 and len(set(candidate)) >= 12:
            return True
    return False


def _contains_sensitive_material(
    value: str,
    *,
    detect_opaque: bool = False,
) -> bool:
    return any(pattern.search(value) for pattern in _CREDENTIAL_PATTERNS) or (
        detect_opaque and _looks_like_opaque_secret(value)
    )


def _walk_json_values(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key), nested
            yield from _walk_json_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_json_values(nested)
    else:
        yield None, value


def _validate_redacted_json_contract(name: str, normalized: str) -> None:
    try:
        parsed = json.loads(normalized)
    except (TypeError, ValueError) as exc:
        raise RuntimeIncidentBoundsError(
            f"{name} must be bounded JSON"
        ) from exc

    if name == "redacted_summary":
        if not isinstance(parsed, dict) or not parsed:
            raise RuntimeIncidentBoundsError(
                "redacted_summary must be a non-empty structured object"
            )
        unknown_fields = set(parsed) - _SUMMARY_FIELDS
        if unknown_fields:
            if any(_sensitive_key(str(key)) for key in unknown_fields):
                raise RuntimeIncidentBoundsError(
                    "redacted_summary contains sensitive material"
                )
            raise RuntimeIncidentBoundsError(
                f"redacted_summary contains unsupported fields: "
                f"{sorted(unknown_fields)!r}"
            )
        if any(isinstance(value, (dict, list)) for value in parsed.values()):
            raise RuntimeIncidentBoundsError(
                "redacted_summary fields must be scalar"
            )
    elif name == "evidence_refs_json" and not isinstance(parsed, list):
        raise RuntimeIncidentBoundsError(
            "evidence_refs_json must be a list of stable references"
        )
    elif name == "evidence_refs_json":
        if not all(
            isinstance(reference, str)
            and _EVIDENCE_REFERENCE_PATTERN.fullmatch(reference)
            for reference in parsed
        ):
            raise RuntimeIncidentBoundsError(
                "evidence_refs_json must be a list of stable references"
            )
    elif name == "diagnosis_json":
        if not isinstance(parsed, dict):
            raise RuntimeIncidentBoundsError(
                "diagnosis_json must be a structured object"
            )
        unknown_fields = set(parsed) - _DIAGNOSIS_FIELDS
        if unknown_fields:
            if any(_sensitive_key(str(key)) for key in unknown_fields):
                raise RuntimeIncidentBoundsError(
                    "diagnosis_json contains sensitive material"
                )
            raise RuntimeIncidentBoundsError(
                f"diagnosis_json contains unsupported fields: "
                f"{sorted(unknown_fields)!r}"
            )
        shadow_policy = parsed.get("shadow_playbook_policy")
        if shadow_policy is not None and (
            not isinstance(shadow_policy, dict)
            or set(shadow_policy) != _SHADOW_POLICY_FIELDS
        ):
            raise RuntimeIncidentBoundsError(
                "diagnosis_json shadow policy fields are invalid"
            )
        if shadow_policy is not None:
            nominated = shadow_policy["nominated_playbook"]
            playbook_version = shadow_policy["playbook_version"]
            accepted = shadow_policy["accepted"]
            refusal_reasons = shadow_policy["refusal_reasons"]
            verification_query = shadow_policy["verification_query"]
            catalog_playbook = get_runtime_agent_playbook(nominated)
            if (
                shadow_policy["mode"] != "shadow"
                or shadow_policy["policy_version"]
                != "runtime-shadow-policy-v1"
                or shadow_policy["would_execute"] is not False
                or shadow_policy["action_executed"] is not False
                or not isinstance(accepted, bool)
                or (
                    nominated is not None
                    and (
                        not isinstance(nominated, str)
                        or not _PLAYBOOK_NAME_PATTERN.fullmatch(nominated)
                    )
                )
                or (
                    playbook_version is not None
                    and (
                        isinstance(playbook_version, bool)
                        or not isinstance(playbook_version, int)
                        or playbook_version < 1
                    )
                )
                or not isinstance(refusal_reasons, list)
                or len(refusal_reasons) > 8
                or not all(
                    isinstance(reason, str) and 0 < len(reason) <= 128
                    for reason in refusal_reasons
                )
                or (
                    verification_query is not None
                    and (
                        not isinstance(verification_query, str)
                        or not 0 < len(verification_query) <= 64
                    )
                )
                or (
                    nominated is None
                    and (
                        accepted
                        or playbook_version is not None
                        or verification_query is not None
                        or refusal_reasons != ["no_nomination"]
                    )
                )
                or (
                    nominated is not None
                    and (
                        playbook_version is None
                        and refusal_reasons != ["unknown_playbook"]
                    )
                )
                or (accepted and refusal_reasons)
                or (not accepted and nominated is not None and not refusal_reasons)
                or (
                    accepted
                    and (
                        catalog_playbook is None
                        or catalog_playbook.version != playbook_version
                        or catalog_playbook.verification_query
                        != verification_query
                    )
                )
            ):
                raise RuntimeIncidentBoundsError(
                    "diagnosis_json shadow policy is invalid"
                )
        recovery_policy = parsed.get("recovery_playbook_policy")
        if recovery_policy is not None and (
            not isinstance(recovery_policy, dict)
            or set(recovery_policy) != _RECOVERY_POLICY_FIELDS
        ):
            raise RuntimeIncidentBoundsError(
                "diagnosis_json recovery policy fields are invalid"
            )
        if recovery_policy is not None:
            nominated = recovery_policy["nominated_playbook"]
            playbook_version = recovery_policy["playbook_version"]
            accepted = recovery_policy["accepted"]
            reasons = recovery_policy["refusal_reasons"]
            verification_query = recovery_policy["verification_query"]
            verification_status = recovery_policy["verification_status"]
            attempt_id = recovery_policy["attempt_id"]
            references = recovery_policy["evidence_references"]
            catalog_playbook = get_runtime_agent_playbook(nominated)
            if (
                recovery_policy["mode"] != "execute"
                or recovery_policy["policy_version"]
                != "runtime-execution-policy-v1"
                or not isinstance(accepted, bool)
                or recovery_policy["would_execute"] is not accepted
                or not isinstance(recovery_policy["action_executed"], bool)
                or (
                    nominated is not None
                    and (
                        not isinstance(nominated, str)
                        or not _PLAYBOOK_NAME_PATTERN.fullmatch(nominated)
                    )
                )
                or not isinstance(reasons, list)
                or len(reasons) > 8
                or not all(
                    isinstance(reason, str) and 0 < len(reason) <= 128
                    for reason in reasons
                )
                or (
                    nominated is None
                    and (
                        accepted
                        or playbook_version is not None
                        or verification_query is not None
                        or reasons != ["no_nomination"]
                    )
                )
                or (
                    nominated is not None
                    and catalog_playbook is None
                    and (
                        accepted
                        or playbook_version is not None
                        or verification_query is not None
                        or reasons != ["unknown_playbook"]
                    )
                )
                or (
                    catalog_playbook is not None
                    and (
                        catalog_playbook.version != playbook_version
                        or not catalog_playbook.executable_in_phase_6
                        or catalog_playbook.verification_query
                        != verification_query
                    )
                )
                or (
                    not accepted
                    and nominated is not None
                    and catalog_playbook is not None
                    and not reasons
                )
                or verification_status
                not in {
                    "refused",
                    "verified",
                    "already_verified",
                    "verification_failed",
                    "failed",
                    "fingerprint_mismatch",
                    "incident_missing",
                    "claim_lost",
                    "action_in_progress",
                    "circuit_busy",
                    "incident_action_frozen",
                    "action_outcome_unknown",
                    "circuit_open",
                }
                or (
                    attempt_id is not None
                    and (
                        isinstance(attempt_id, bool)
                        or not isinstance(attempt_id, int)
                        or attempt_id < 1
                    )
                )
                or not isinstance(references, list)
                or len(references) > 16
                or not all(
                    isinstance(reference, str)
                    and _EVIDENCE_REFERENCE_PATTERN.fullmatch(reference)
                    for reference in references
                )
                or (
                    recovery_policy["action_executed"]
                    and (
                        not accepted
                        or attempt_id is None
                        or verification_status
                        not in {
                            "verified",
                            "already_verified",
                            "verification_failed",
                            "failed",
                        }
                    )
                )
                or (
                    verification_status in {"verified", "already_verified"}
                    and not recovery_policy["action_executed"]
                )
            ):
                raise RuntimeIncidentBoundsError(
                    "diagnosis_json recovery policy is invalid"
                )

    for key, value in _walk_json_values(parsed):
        if key is not None and _sensitive_key(key):
            raise RuntimeIncidentBoundsError(f"{name} contains sensitive material")
        if isinstance(value, str):
            if len(value) > 512:
                raise RuntimeIncidentBoundsError(
                    f"{name} contains an unbounded string value"
                )
            if _contains_sensitive_material(value, detect_opaque=True):
                raise RuntimeIncidentBoundsError(
                    f"{name} contains sensitive material"
                )
        elif value is not None and not isinstance(
            value, (bool, int, float, dict, list)
        ):
            raise RuntimeIncidentBoundsError(
                f"{name} contains an unsupported value"
            )


def _validate_required_text(name: str, value: str, *, maximum: int) -> str:
    if value is None:
        raise RuntimeIncidentBoundsError(f"{name} is required")
    normalized = str(value)
    if not normalized or len(normalized) > maximum:
        raise RuntimeIncidentBoundsError(
            f"{name} must contain between 1 and {maximum} characters"
        )
    if _contains_sensitive_material(normalized):
        raise RuntimeIncidentBoundsError(f"{name} contains sensitive material")
    return normalized


def _validate_bounded_payload(
    name: str,
    value: str | None,
    *,
    maximum: int,
    check_sensitive: bool = False,
) -> str | None:
    if value is None:
        return None
    normalized = str(value)
    if len(normalized) > maximum:
        raise RuntimeIncidentBoundsError(
            f"{name} exceeds {maximum} characters"
        )
    if check_sensitive:
        _validate_redacted_json_contract(name, normalized)
    return normalized


def _detach(session, incident: RuntimeIncident) -> RuntimeIncident:
    session.refresh(incident)
    session.expunge(incident)
    return incident


def record_runtime_incident(
    session_factory: sessionmaker,
    *,
    source_kind: str,
    source_record_id: str,
    incident_type: str,
    severity: str,
    fingerprint: str,
    redacted_summary: str,
    occurred_at: datetime,
    feature_policy_version: str,
    prompt_version: str,
    tool_policy_version: str,
    generation: int = 1,
    diagnosis_json: str | None = None,
    evidence_refs_json: str | None = None,
    affected_raw_message_id: int | None = None,
    message_operation_contract_id: int | None = None,
) -> RuntimeIncident:
    """Insert or atomically coalesce one occurrence of a fingerprint generation."""

    values = {
        "source_kind": _validate_required_text(
            "source_kind", source_kind, maximum=64
        ),
        "source_record_id": _validate_required_text(
            "source_record_id", source_record_id, maximum=255
        ),
        "incident_type": _validate_required_text(
            "incident_type", incident_type, maximum=64
        ),
        "severity": _validate_required_text("severity", severity, maximum=16),
        "fingerprint": _validate_required_text(
            "fingerprint", fingerprint, maximum=64
        ),
        "generation": int(generation),
        "redacted_summary": _validate_bounded_payload(
            "redacted_summary",
            redacted_summary,
            maximum=MAX_REDACTED_SUMMARY_LENGTH,
            check_sensitive=True,
        ),
        "diagnosis_json": _validate_bounded_payload(
            "diagnosis_json",
            diagnosis_json,
            maximum=MAX_DIAGNOSIS_JSON_LENGTH,
            check_sensitive=True,
        ),
        "evidence_refs_json": _validate_bounded_payload(
            "evidence_refs_json",
            evidence_refs_json,
            maximum=MAX_EVIDENCE_REFS_JSON_LENGTH,
            check_sensitive=True,
        ),
        "feature_policy_version": _validate_required_text(
            "feature_policy_version", feature_policy_version, maximum=64
        ),
        "prompt_version": _validate_required_text(
            "prompt_version", prompt_version, maximum=64
        ),
        "tool_policy_version": _validate_required_text(
            "tool_policy_version", tool_policy_version, maximum=64
        ),
    }
    if values["generation"] < 1:
        raise ValueError("generation must be positive")

    insert_values = {
        **values,
        "status": _CLAIMABLE_STATUS,
        "repeat_count": 1,
        "first_occurred_at": occurred_at,
        "last_occurred_at": occurred_at,
        "notification_status": "pending",
        "recovery_status": "not_requested",
        "created_at": occurred_at,
        "updated_at": occurred_at,
    }
    atomic_link_requested = (
        affected_raw_message_id is not None
        or message_operation_contract_id is not None
    )
    if atomic_link_requested and (
        type(affected_raw_message_id) is not int
        or affected_raw_message_id < 1
        or type(message_operation_contract_id) is not int
        or message_operation_contract_id < 1
        or values["incident_type"] != "message_operation_failure"
    ):
        raise ValueError("invalid atomic message-operation incident link")

    with session_factory() as session:
        existing_severity_rank = case(
            *(
                (RuntimeIncident.severity == severity_name, rank)
                for severity_name, rank in _SEVERITY_RANKS.items()
            ),
            else_=0,
        )
        incoming_severity_rank = _SEVERITY_RANKS.get(
            str(values["severity"]).lower(), 0
        )
        occurrence_is_newer = RuntimeIncident.last_occurred_at <= occurred_at
        occurrence_is_older = RuntimeIncident.first_occurred_at >= occurred_at
        statement = sqlite_insert(RuntimeIncident).values(**insert_values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                RuntimeIncident.fingerprint,
                RuntimeIncident.generation,
            ],
            set_={
                "repeat_count": RuntimeIncident.repeat_count + 1,
                "first_occurred_at": case(
                    (occurrence_is_older, occurred_at),
                    else_=RuntimeIncident.first_occurred_at,
                ),
                "last_occurred_at": case(
                    (occurrence_is_newer, occurred_at),
                    else_=RuntimeIncident.last_occurred_at,
                ),
                "severity": case(
                    (
                        existing_severity_rank <= incoming_severity_rank,
                        values["severity"],
                    ),
                    else_=RuntimeIncident.severity,
                ),
                "redacted_summary": case(
                    (occurrence_is_newer, values["redacted_summary"]),
                    else_=RuntimeIncident.redacted_summary,
                ),
                "updated_at": case(
                    (RuntimeIncident.updated_at <= occurred_at, occurred_at),
                    else_=RuntimeIncident.updated_at,
                ),
            },
        ).returning(RuntimeIncident.id)
        incident_id = session.execute(statement).scalar_one()
        if atomic_link_requested:
            contract = session.get(
                MessageOperationContract, message_operation_contract_id
            )
            if (
                contract is None
                or contract.raw_message_id != affected_raw_message_id
                or contract.status != "violated"
                or contract.violation_code != values["source_record_id"]
                or contract.runtime_incident_id not in (None, incident_id)
            ):
                raise RuntimeIncidentBoundsError(
                    "atomic message-operation incident identity conflict"
                )
            relation_statement = sqlite_insert(
                RuntimeIncidentAffectedMessage
            ).values(
                runtime_incident_id=incident_id,
                raw_message_id=affected_raw_message_id,
                message_operation_contract_id=message_operation_contract_id,
                created_at=occurred_at,
            )
            session.execute(
                relation_statement.on_conflict_do_nothing(
                    index_elements=["runtime_incident_id", "raw_message_id"]
                )
            )
            relation = session.execute(
                select(RuntimeIncidentAffectedMessage).where(
                    RuntimeIncidentAffectedMessage.runtime_incident_id
                    == incident_id,
                    RuntimeIncidentAffectedMessage.raw_message_id
                    == affected_raw_message_id,
                )
            ).scalar_one()
            if (
                relation.message_operation_contract_id
                != message_operation_contract_id
            ):
                raise RuntimeIncidentBoundsError(
                    "affected message contract identity conflict"
                )
            contract.runtime_incident_id = incident_id
            contract.updated_at = occurred_at
        session.commit()
        return _detach(session, session.get(RuntimeIncident, incident_id))


def _claimable(now: datetime):
    return or_(
        and_(
            RuntimeIncident.status.in_((_CLAIMABLE_STATUS, "retry_pending")),
            or_(
                RuntimeIncident.agent_next_attempt_at.is_(None),
                RuntimeIncident.agent_next_attempt_at <= now,
            ),
        ),
        and_(
            RuntimeIncident.status == _CLAIMED_STATUS,
            RuntimeIncident.claim_expires_at.is_not(None),
            RuntimeIncident.claim_expires_at <= now,
        ),
    )


def list_claimable_runtime_incidents(
    session_factory: sessionmaker,
    *,
    now: datetime,
    limit: int = 20,
    incident_types: frozenset[str] | None = None,
) -> list[RuntimeIncident]:
    """Return a bounded oldest-first snapshot of claimable incident rows."""

    bounded_limit = max(1, min(int(limit), MAX_CLAIMABLE_LIMIT))
    if incident_types == frozenset():
        return []
    with session_factory() as session:
        query = session.query(RuntimeIncident).filter(_claimable(now))
        if incident_types is not None:
            query = query.filter(
                RuntimeIncident.incident_type.in_(tuple(sorted(incident_types)))
            )
        rows = (
            query
            .order_by(
                RuntimeIncident.last_occurred_at,
                RuntimeIncident.id,
            )
            .limit(bounded_limit)
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def find_reusable_runtime_incident_diagnosis(
    session_factory: sessionmaker,
    *,
    fingerprint: str,
    exclude_incident_id: int,
) -> RuntimeIncident | None:
    """Return the newest completed diagnosis for the same redacted fingerprint."""

    normalized_fingerprint = _validate_required_text(
        "fingerprint", fingerprint, maximum=64
    )
    with session_factory() as session:
        row = (
            session.query(RuntimeIncident)
            .filter(
                RuntimeIncident.fingerprint == normalized_fingerprint,
                RuntimeIncident.id != int(exclude_incident_id),
                RuntimeIncident.status == "diagnosed",
                RuntimeIncident.diagnosis_json.is_not(None),
                RuntimeIncident.evidence_refs_json.is_not(None),
            )
            .order_by(
                RuntimeIncident.generation.desc(),
                RuntimeIncident.updated_at.desc(),
                RuntimeIncident.id.desc(),
            )
            .first()
        )
        if row is None:
            return None
        session.expunge(row)
        return row


def get_runtime_incident(
    session_factory: sessionmaker,
    *,
    incident_id: int,
) -> RuntimeIncident | None:
    """Return one detached incident row for bounded read-only presentation."""

    with session_factory() as session:
        row = session.get(RuntimeIncident, int(incident_id))
        if row is None:
            return None
        session.expunge(row)
        return row


def claim_runtime_incident(
    session_factory: sessionmaker,
    *,
    incident_id: int,
    claim_token: str,
    claimed_at: datetime,
    claim_expires_at: datetime,
    prompt_version: str | None = None,
    incident_types: frozenset[str] | None = None,
) -> RuntimeIncident | None:
    """Claim one row through a compare-and-set update."""

    token = _validate_required_text("claim_token", claim_token, maximum=64)
    if claim_expires_at <= claimed_at:
        raise ValueError("claim_expires_at must be after claimed_at")
    if incident_types == frozenset():
        return None
    with session_factory() as session:
        claim_values: dict[str, object | None] = {
            "status": _CLAIMED_STATUS,
            "claim_token": token,
            "claimed_at": claimed_at,
            "claim_expires_at": claim_expires_at,
            "agent_attempt_count": RuntimeIncident.agent_attempt_count + 1,
            "agent_next_attempt_at": None,
            "updated_at": claimed_at,
        }
        if prompt_version is not None:
            claim_values["prompt_version"] = _validate_required_text(
                "prompt_version", prompt_version, maximum=64
            )
        claim_conditions = [
            RuntimeIncident.id == int(incident_id),
            _claimable(claimed_at),
        ]
        if incident_types is not None:
            claim_conditions.append(
                RuntimeIncident.incident_type.in_(tuple(sorted(incident_types)))
            )
        incident_id_result = session.execute(
            update(RuntimeIncident)
            .where(*claim_conditions)
            .values(**claim_values)
            .returning(RuntimeIncident.id)
        ).scalar_one_or_none()
        session.commit()
        if incident_id_result is None:
            return None
        return _detach(
            session, session.get(RuntimeIncident, int(incident_id_result))
        )


def transition_runtime_incident(
    session_factory: sessionmaker,
    *,
    incident_id: int,
    from_status: str | Sequence[str],
    to_status: str,
    now: datetime,
    claim_token: str | None = None,
    diagnosis_json: str | None = None,
    evidence_refs_json: str | None = None,
    playbook_name: str | None = None,
    recovery_status: str | None = None,
    queue_notification: bool = False,
    agent_next_attempt_at: datetime | None = None,
    prompt_version: str | None = None,
) -> bool:
    """Apply a token-checked lifecycle transition without loading stale state."""

    sources = (
        (from_status,)
        if isinstance(from_status, str)
        else tuple(str(status) for status in from_status)
    )
    if not sources:
        raise ValueError("from_status must not be empty")
    target = str(to_status)
    if any(target not in _ALLOWED_TRANSITIONS.get(source, ()) for source in sources):
        raise ValueError(f"invalid runtime incident transition: {sources!r} -> {target}")

    values: dict[str, object | None] = {
        "status": target,
        "updated_at": now,
    }
    if diagnosis_json is not None:
        values["diagnosis_json"] = _validate_bounded_payload(
            "diagnosis_json",
            diagnosis_json,
            maximum=MAX_DIAGNOSIS_JSON_LENGTH,
            check_sensitive=True,
        )
    if evidence_refs_json is not None:
        values["evidence_refs_json"] = _validate_bounded_payload(
            "evidence_refs_json",
            evidence_refs_json,
            maximum=MAX_EVIDENCE_REFS_JSON_LENGTH,
            check_sensitive=True,
        )
    if playbook_name is not None:
        values["playbook_name"] = _validate_required_text(
            "playbook_name", playbook_name, maximum=128
        )
    if recovery_status is not None:
        values["recovery_status"] = _validate_required_text(
            "recovery_status", recovery_status, maximum=32
        )
    if queue_notification:
        values.update(
            notification_status="pending",
            notification_claim_token=None,
            notification_claimed_at=None,
            notified_at=None,
        )
    if agent_next_attempt_at is not None:
        values["agent_next_attempt_at"] = agent_next_attempt_at
    if prompt_version is not None:
        values["prompt_version"] = _validate_required_text(
            "prompt_version", prompt_version, maximum=64
        )
    if target in {"diagnosed", "escalated", "resolved", "closed"}:
        values["agent_next_attempt_at"] = None
    if target != _CLAIMED_STATUS:
        values.update(
            claim_token=None,
            claimed_at=None,
            claim_expires_at=None,
        )

    predicates = [
        RuntimeIncident.id == int(incident_id),
        RuntimeIncident.status.in_(sources),
    ]
    if claim_token is not None:
        predicates.append(RuntimeIncident.claim_token == str(claim_token))
    elif _CLAIMED_STATUS in sources:
        return False
    if _CLAIMED_STATUS in sources:
        predicates.extend(
            (
                RuntimeIncident.claim_expires_at.is_not(None),
                RuntimeIncident.claim_expires_at > now,
            )
        )
    with session_factory() as session:
        result = session.execute(
            update(RuntimeIncident).where(*predicates).values(**values)
        )
        session.commit()
        return result.rowcount == 1


def defer_runtime_incident_action_claim(
    session_factory: sessionmaker,
    *,
    incident_id: int,
    claim_token: str,
    now: datetime,
    retry_at: datetime,
) -> bool:
    """Release action contention without consuming the model-attempt budget."""

    if retry_at <= now:
        raise ValueError("retry_at must be after now")
    with session_factory() as session:
        result = session.execute(
            update(RuntimeIncident)
            .where(
                RuntimeIncident.id == int(incident_id),
                RuntimeIncident.status == _CLAIMED_STATUS,
                RuntimeIncident.claim_token == str(claim_token),
                RuntimeIncident.claim_expires_at.is_not(None),
                RuntimeIncident.claim_expires_at > now,
            )
            .values(
                status="retry_pending",
                agent_attempt_count=case(
                    (
                        RuntimeIncident.agent_attempt_count > 0,
                        RuntimeIncident.agent_attempt_count - 1,
                    ),
                    else_=0,
                ),
                agent_next_attempt_at=retry_at,
                claim_token=None,
                claimed_at=None,
                claim_expires_at=None,
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1


def release_or_expire_runtime_incident_claim(
    session_factory: sessionmaker,
    *,
    incident_id: int,
    claim_token: str,
    now: datetime,
    force_release: bool = False,
) -> bool:
    """Return a matching claim to pending after expiry or explicit worker release."""

    predicates = [
        RuntimeIncident.id == int(incident_id),
        RuntimeIncident.status == _CLAIMED_STATUS,
        RuntimeIncident.claim_token == str(claim_token),
    ]
    if not force_release:
        predicates.extend(
            (
                RuntimeIncident.claim_expires_at.is_not(None),
                RuntimeIncident.claim_expires_at <= now,
            )
        )
    with session_factory() as session:
        result = session.execute(
            update(RuntimeIncident)
            .where(*predicates)
            .values(
                status=_CLAIMABLE_STATUS,
                claim_token=None,
                claimed_at=None,
                claim_expires_at=None,
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1
