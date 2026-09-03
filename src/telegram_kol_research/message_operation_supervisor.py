"""Deterministic, shadow-first evaluation of message-operation outcomes."""

from __future__ import annotations

import re
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exists, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.config import RuntimeIncidentConfig
from telegram_kol_research.message_operation_types import (
    MESSAGE_OPERATION_VIOLATIONS,
)
from telegram_kol_research.message_operation_contracts import (
    POLICY_VERSION,
    project_message_operation_contract,
    run_message_operation_shadow_once,
)
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    ExecutionBinding,
    ExecutionEvent,
    ManagementMessageTarget,
    MessageInstructionItem,
    MessageOperationContract,
    MessageOperationItem,
    MessageOperationStage1Notification,
    PositionMutationIntent,
    PositionProtectionRevision,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyManagementBatch,
    StrategyManagementLeg,
    RuntimeIncident,
    RuntimeIncidentAffectedMessage,
    RuntimeIncidentHandoffArtifact,
)
from telegram_kol_research.runtime_incident_adapters import (
    capture_message_operation_failure,
)
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.runtime_incident_snapshot import (
    build_instruction_execution_contradiction_snapshot,
)


OUTCOME_STATES = frozenset(
    {
        "observing",
        "missing",
        "verified",
        "recognition_failed",
        "hold",
        "unresolved",
        "context_exhausted",
        "safety_refusal",
        "action_refused",
        "partial",
        "unknown",
        "local_success",
        "exchange_mismatch",
        "restart_skip",
        "reconciliation_disproved",
        "duplicate_verified",
        "superseded_verified",
    }
)
EVALUATION_STATUSES = frozenset(
    {
        "observing",
        "verified",
        "violated",
        "duplicate_verified",
        "superseded_verified",
    }
)
_REFERENCE = re.compile(r"[a-z][a-z0-9_-]{1,31}:[A-Za-z0-9._-]{1,128}\Z")
_MANAGEMENT_DESCENDANTS = frozenset(
    {"management_envelope", "management_target", "management_item"}
)
_IMMEDIATE_VIOLATIONS = {
    "recognition_failed": "recognition_failed",
    "hold": "context_unresolved",
    "unresolved": "context_unresolved",
    "context_exhausted": "context_exhausted",
    "safety_refusal": "action_refused",
    "action_refused": "action_refused",
    "partial": "partial_operation",
    "unknown": "unknown_operation_result",
    "exchange_mismatch": "exchange_readback_mismatch",
    "restart_skip": "restart_or_lease_skip",
    "reconciliation_disproved": "reconciliation_disproved_success",
}


class MessageOperationEvaluationError(ValueError):
    """Raised when unbounded or unknown outcome evidence is presented."""


_COVERAGE_LIMIT_MAX = 1_000
_COVERAGE_AGE_MAX_SECONDS = 1_000_000_000
_TERMINAL_RECOGNITION_STATES = frozenset({"completed", "failed"})
_NONTERMINAL_RECOGNITION_STATES = frozenset(
    {
        "",
        "pending",
        "running",
        "execution_pending",
        "execution_running",
        "execution_uncertain",
    }
)


def audit_multi_instruction_completeness(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
) -> tuple[dict[str, object], ...]:
    """Detect severe projection and per-item outcome gaps without model calls."""

    with session_factory() as session:
        decision = session.execute(
            select(RecognitionDecision).where(
                RecognitionDecision.raw_message_id == int(raw_message_id)
            )
        ).scalar_one_or_none()
        try:
            payload = json.loads(
                decision.authoritative_payload_json if decision is not None else "{}"
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        instructions = payload.get("instructions") if isinstance(payload, dict) else None
        if not isinstance(instructions, list) or len(instructions) <= 1:
            return ()
        declared_count = len(instructions)
        candidates = session.execute(
            select(SignalCandidate).where(
                SignalCandidate.raw_message_id == int(raw_message_id),
                SignalCandidate.parse_source == "mimo_authoritative",
            )
        ).scalars().all()
        items = session.execute(
            select(MessageInstructionItem).where(
                MessageInstructionItem.raw_message_id == int(raw_message_id),
                MessageInstructionItem.retired_at.is_(None),
            )
        ).scalars().all()
        contract = session.execute(
            select(MessageOperationContract)
            .where(MessageOperationContract.raw_message_id == int(raw_message_id))
            .order_by(MessageOperationContract.id.desc())
        ).scalars().first()

    violations: list[dict[str, object]] = []
    if len(candidates) != declared_count or len(items) != declared_count:
        violations.append(
            {
                "violation_code": "missing_instruction_projection",
                "severity": "high",
                "declared_count": declared_count,
                "candidate_count": len(candidates),
                "item_count": len(items),
            }
        )
    statuses = {str(item.status or "").strip().lower() for item in items}
    terminal_statuses = {"submitted", "skipped", "failed", "unknown", "blocked"}
    if statuses & terminal_statuses and statuses & {"pending", "executing"}:
        violations.append(
            {
                "violation_code": "unevaluated_sibling_instruction",
                "severity": "high",
                "declared_count": declared_count,
            }
        )
    if (
        statuses & {"failed", "unknown", "blocked"}
        and contract is not None
        and str(contract.status or "").lower() == "verified"
    ):
        violations.append(
            {
                "violation_code": "hidden_instruction_failure",
                "severity": "high",
                "declared_count": declared_count,
            }
        )
    return tuple(violations)


def apply_multi_instruction_completeness_violations(
    session_factory: sessionmaker,
    *,
    after_raw_message_id: int,
    limit: int,
    observed_at: datetime,
) -> int:
    """Persist severe multi-instruction gaps into the supervisor contract path."""

    if (
        type(after_raw_message_id) is not int
        or after_raw_message_id < 0
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise MessageOperationEvaluationError(
            "invalid multi-instruction completeness bounds"
        )
    trading_settings = load_trading_settings(session_factory)
    if trading_settings.multi_instruction_mode != "live":
        return 0
    effective_watermark = max(
        after_raw_message_id,
        trading_settings.multi_instruction_activation_after_raw_message_id,
    )
    with session_factory() as session:
        raw_ids = session.execute(
            select(RecognitionDecision.raw_message_id)
            .where(RecognitionDecision.raw_message_id > effective_watermark)
            .order_by(RecognitionDecision.raw_message_id)
            .limit(limit)
        ).scalars().all()
    applied = 0
    priority = (
        "missing_instruction_projection",
        "unevaluated_sibling_instruction",
        "hidden_instruction_failure",
    )
    for raw_id in raw_ids:
        violations = audit_multi_instruction_completeness(
            session_factory,
            raw_message_id=int(raw_id),
        )
        if not violations:
            continue
        codes = {str(row["violation_code"]) for row in violations}
        violation_code = next(code for code in priority if code in codes)
        with session_factory() as session:
            contract = session.execute(
                select(MessageOperationContract)
                .where(MessageOperationContract.raw_message_id == int(raw_id))
                .order_by(MessageOperationContract.id.desc())
            ).scalars().first()
            if contract is None:
                continue
            if str(contract.status or "").lower() == "violated":
                continue
            contract.status = "violated"
            contract.violation_code = violation_code
            contract.updated_at = observed_at
            session.commit()
            applied += 1
    return applied


def build_message_operation_coverage_snapshot(
    session_factory: sessionmaker,
    *,
    after_raw_message_id: int,
    supervisor_last_success_at: datetime | None,
    observed_at: datetime,
    limit: int = _COVERAGE_LIMIT_MAX,
    coverage_enabled: bool = True,
) -> dict[str, object]:
    """Build one bounded, read-only end-to-end coverage projection."""

    if (
        type(after_raw_message_id) is not int
        or not 0 <= after_raw_message_id <= 2**63 - 1
    ):
        raise MessageOperationEvaluationError("invalid coverage raw-message watermark")
    if type(limit) is not int or not 1 <= limit <= _COVERAGE_LIMIT_MAX:
        raise MessageOperationEvaluationError("coverage limit must be between 1 and 1000")
    if type(coverage_enabled) is not bool:
        raise MessageOperationEvaluationError("coverage_enabled must be boolean")
    now = _aware_utc(observed_at)
    heartbeat = (
        None
        if supervisor_last_success_at is None
        else _aware_utc(supervisor_last_success_at)
    )
    if not coverage_enabled:
        return _disabled_message_operation_coverage_snapshot()

    execution_contradictions = build_instruction_execution_contradiction_snapshot(
        session_factory,
        observed_at=now,
        limit=20,
    )

    with session_factory() as session:
        source_rows = session.execute(
            select(
                RawMessage.id,
                RawMessage.posted_at,
                RawMessage.created_at,
                RecognitionDecision.comparison_status,
            )
            .outerjoin(
                RecognitionDecision,
                RecognitionDecision.raw_message_id == RawMessage.id,
            )
            .where(RawMessage.id > after_raw_message_id)
            .order_by(RawMessage.id)
            .limit(limit + 1)
        ).all()
    scan_truncated = len(source_rows) > limit
    source_rows = source_rows[:limit]

    executable_raw_ids: list[int] = []
    missing_contract_raw_ids: list[int] = []
    missing_contract_times: list[datetime] = []
    terminal_raw_ids: list[int] = []
    for raw_id, posted_at, created_at, comparison_status in source_rows:
        normalized_status = str(comparison_status or "").strip().lower()
        if normalized_status in _NONTERMINAL_RECOGNITION_STATES:
            continue
        if normalized_status not in _TERMINAL_RECOGNITION_STATES:
            continue
        terminal_raw_ids.append(int(raw_id))
        projection = project_message_operation_contract(
            session_factory,
            raw_message_id=int(raw_id),
        )
        if projection is None:
            continue
        executable_raw_ids.append(int(raw_id))
        with session_factory() as session:
            contract_exists = session.execute(
                select(MessageOperationContract.id).where(
                    MessageOperationContract.raw_message_id == int(raw_id),
                    MessageOperationContract.policy_version == POLICY_VERSION,
                )
            ).scalar_one_or_none()
        if contract_exists is None:
            missing_contract_raw_ids.append(int(raw_id))
            missing_contract_times.append(posted_at or created_at)

    with session_factory() as session:
        contracts = (
            session.execute(
                select(MessageOperationContract)
                .where(
                    MessageOperationContract.raw_message_id.in_(terminal_raw_ids),
                    MessageOperationContract.policy_version == POLICY_VERSION,
                )
                .order_by(MessageOperationContract.id)
            ).scalars().all()
            if terminal_raw_ids
            else []
        )
        contract_ids = [int(row.id) for row in contracts]
        stage1_rows = (
            session.execute(
                select(MessageOperationStage1Notification).where(
                    MessageOperationStage1Notification.message_operation_contract_id.in_(
                        contract_ids
                    )
                )
            ).scalars().all()
            if contract_ids
            else []
        )
        incident_rows = (
            session.execute(
                select(RuntimeIncident)
                .join(
                    RuntimeIncidentAffectedMessage,
                    RuntimeIncidentAffectedMessage.runtime_incident_id
                    == RuntimeIncident.id,
                )
                .where(
                    RuntimeIncidentAffectedMessage.message_operation_contract_id.in_(
                        contract_ids
                    ),
                    RuntimeIncident.incident_type == "message_operation_failure",
                )
                .distinct()
                .order_by(RuntimeIncident.id)
            ).scalars().all()
            if contract_ids
            else []
        )
        incident_ids = [int(row.id) for row in incident_rows]
        handoff_rows = (
            session.execute(
                select(RuntimeIncidentHandoffArtifact)
                .where(
                    RuntimeIncidentHandoffArtifact.runtime_incident_id.in_(
                        incident_ids
                    )
                )
                .order_by(
                    RuntimeIncidentHandoffArtifact.runtime_incident_id,
                    RuntimeIncidentHandoffArtifact.diagnosis_revision,
                    RuntimeIncidentHandoffArtifact.id,
                )
            ).scalars().all()
            if incident_ids
            else []
        )

    stage1_by_contract = {
        int(row.message_operation_contract_id): row for row in stage1_rows
    }
    latest_handoff_by_incident: dict[int, RuntimeIncidentHandoffArtifact] = {}
    for row in handoff_rows:
        latest_handoff_by_incident[int(row.runtime_incident_id)] = row

    violated_contracts = [row for row in contracts if row.status == "violated"]
    violations_without_stage1 = sum(
        1 for row in violated_contracts if int(row.id) not in stage1_by_contract
    )
    incidents_without_terminal = sum(
        1
        for row in incident_rows
        if (
            latest_handoff_by_incident.get(int(row.id)) is None
            or latest_handoff_by_incident[int(row.id)].status != "delivered"
        )
    )

    latest_handoffs = tuple(latest_handoff_by_incident.values())
    failure_outcomes = {"provider_failed", "tool_failed", "evidence_incomplete"}
    nonterminal_times: list[datetime] = list(missing_contract_times)
    nonterminal_times.extend(
        row.created_at for row in contracts if row.status == "observing"
    )
    nonterminal_times.extend(
        row.created_at
        for row in stage1_rows
        if row.status not in {"delivered", "exhausted"}
    )
    nonterminal_times.extend(
        row.first_occurred_at
        for row in incident_rows
        if (
            latest_handoff_by_incident.get(int(row.id)) is None
            or latest_handoff_by_incident[int(row.id)].status != "delivered"
        )
    )
    nonterminal_times.extend(
        row.created_at
        for row in latest_handoffs
        if row.status not in {"delivered", "exhausted"}
    )
    oldest_age = max(
        (
            max(0, int((now - _aware_utc(value)).total_seconds()))
            for value in nonterminal_times
            if isinstance(value, datetime)
        ),
        default=0,
    )
    oldest_age = min(oldest_age, _COVERAGE_AGE_MAX_SECONDS)

    return {
        "schema_version": 1,
        "coverage_enabled": coverage_enabled,
        "scan_truncated": scan_truncated,
        "executable_messages_total": len(executable_raw_ids),
        "contracts_created_total": len(contracts),
        "contracts_verified_total": sum(
            row.status in {"verified", "duplicate", "superseded"}
            for row in contracts
        ),
        "contracts_violated_total": len(violated_contracts),
        "executable_without_contract_total": len(missing_contract_raw_ids),
        "violations_without_stage1_total": violations_without_stage1,
        "stage1_pending": sum(
            row.status in {"pending", "delivering"} for row in stage1_rows
        ),
        "stage1_delivered": sum(row.status == "delivered" for row in stage1_rows),
        "stage1_failed": sum(
            row.status in {"failed", "exhausted"} for row in stage1_rows
        ),
        "agent_pending": sum(
            row.status in {"pending", "claimed", "retry_pending"}
            for row in incident_rows
        ),
        "agent_diagnosed": sum(
            row.outcome_kind in {"diagnosed", "reused"} for row in latest_handoffs
        ),
        "agent_failed": sum(
            row.outcome_kind in failure_outcomes for row in latest_handoffs
        ),
        "agent_timed_out": sum(
            row.outcome_kind == "timed_out" for row in latest_handoffs
        ),
        "incidents_without_terminal_stage2_total": incidents_without_terminal,
        "handoffs_persisted_total": len(handoff_rows),
        "stage2_pending": sum(
            row.status in {"pending", "delivering"} for row in latest_handoffs
        ),
        "stage2_delivered": sum(
            row.status == "delivered" for row in latest_handoffs
        ),
        "stage2_failed": sum(
            row.status in {"failed", "exhausted"} for row in latest_handoffs
        ),
        "oldest_nonterminal_age_seconds": oldest_age,
        "supervisor_last_success_at": (
            heartbeat.isoformat() if heartbeat is not None else None
        ),
        "instruction_execution_scan_truncated": bool(
            execution_contradictions["scan_truncated"]
        ),
        "instruction_execution_contradictions_total": int(
            execution_contradictions["contradictions_total"]
        ),
        "instruction_execution_facts": [
            dict(row) for row in execution_contradictions["facts"]
        ],
    }


def _disabled_message_operation_coverage_snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "coverage_enabled": False,
        "scan_truncated": False,
        "executable_messages_total": 0,
        "contracts_created_total": 0,
        "contracts_verified_total": 0,
        "contracts_violated_total": 0,
        "executable_without_contract_total": 0,
        "violations_without_stage1_total": 0,
        "stage1_pending": 0,
        "stage1_delivered": 0,
        "stage1_failed": 0,
        "agent_pending": 0,
        "agent_diagnosed": 0,
        "agent_failed": 0,
        "agent_timed_out": 0,
        "incidents_without_terminal_stage2_total": 0,
        "handoffs_persisted_total": 0,
        "stage2_pending": 0,
        "stage2_delivered": 0,
        "stage2_failed": 0,
        "oldest_nonterminal_age_seconds": 0,
        "supervisor_last_success_at": None,
        "instruction_execution_scan_truncated": False,
        "instruction_execution_contradictions_total": 0,
        "instruction_execution_facts": [],
    }


def run_message_operation_supervisor_cycle(
    session_factory: sessionmaker,
    *,
    after_raw_message_id: int,
    capture_after_raw_message_id: int,
    limit: int,
    observed_at: datetime,
    runtime_incident_config: RuntimeIncidentConfig,
) -> dict[str, int]:
    """Run one bounded projection plus outcome cycle with zero model calls."""

    if not runtime_incident_config.captures("message_operation_failure"):
        raise MessageOperationEvaluationError(
            "message operation supervisor capture policy invalid"
        )

    projection = run_message_operation_shadow_once(
        session_factory,
        after_raw_message_id=after_raw_message_id,
        limit=limit,
        now=observed_at,
    )
    outcomes = run_message_operation_outcome_shadow_once(
        session_factory,
        limit=limit,
        observed_at=observed_at,
    )
    completeness_violations = apply_multi_instruction_completeness_violations(
        session_factory,
        after_raw_message_id=after_raw_message_id,
        limit=limit,
        observed_at=observed_at,
    )
    captures = _capture_terminal_message_operation_violations(
        session_factory,
        config=runtime_incident_config,
        after_raw_message_id=capture_after_raw_message_id,
        limit=limit,
        occurred_at=observed_at,
    )
    return {
        **projection,
        **{f"outcome_{key}": value for key, value in outcomes.items()},
        "multi_instruction_violations": completeness_violations,
        **captures,
    }


def _capture_terminal_message_operation_violations(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    after_raw_message_id: int,
    limit: int,
    occurred_at: datetime,
) -> dict[str, int]:
    if (
        type(after_raw_message_id) is not int
        or not 0 <= after_raw_message_id <= 2**63 - 1
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise MessageOperationEvaluationError("invalid violation capture bounds")
    with session_factory() as session:
        rows = session.execute(
            select(
                MessageOperationContract.id,
                MessageOperationContract.raw_message_id,
                MessageOperationContract.violation_code,
                MessageOperationContract.evidence_refs_json,
            )
            .where(
                MessageOperationContract.raw_message_id > after_raw_message_id,
                MessageOperationContract.policy_version == POLICY_VERSION,
                MessageOperationContract.status == "violated",
                MessageOperationContract.runtime_incident_id.is_(None),
            )
            .order_by(MessageOperationContract.id)
            .limit(limit)
        ).all()
    result = {"violations_captured": 0, "capture_errors": 0}
    for contract_id, raw_message_id, violation_code, evidence_refs_json in rows:
        try:
            evidence_refs = json.loads(evidence_refs_json)
            if not isinstance(evidence_refs, list):
                raise MessageOperationEvaluationError(
                    "invalid violation evidence references"
                )
            capture_message_operation_failure(
                session_factory,
                config=config,
                contract_id=int(contract_id),
                raw_message_id=int(raw_message_id),
                violation_code=str(violation_code or ""),
                evidence_refs=evidence_refs,
                occurred_at=occurred_at,
                shadow_only=False,
            )
            with session_factory() as session:
                durable_link = session.execute(
                    select(RuntimeIncidentAffectedMessage.id).where(
                        RuntimeIncidentAffectedMessage.message_operation_contract_id
                        == int(contract_id),
                        RuntimeIncidentAffectedMessage.raw_message_id
                        == int(raw_message_id),
                    )
                ).scalar_one_or_none()
                contract_link = session.execute(
                    select(MessageOperationContract.runtime_incident_id).where(
                        MessageOperationContract.id == int(contract_id)
                    )
                ).scalar_one_or_none()
            if durable_link is None or contract_link is None:
                raise MessageOperationEvaluationError(
                    "violation incident capture did not persist exact links"
                )
            result["violations_captured"] += 1
        except Exception:
            result["capture_errors"] += 1
    return result


def materialize_message_operation_stage1_outbox(
    session_factory: sessionmaker,
    *,
    after_contract_id: int,
    created_at: datetime,
    limit: int = 100,
) -> int:
    """Idempotently materialize one Stage 1 row per affected source message."""

    if type(after_contract_id) is not int or after_contract_id < 0:
        raise MessageOperationEvaluationError("invalid Stage 1 contract watermark")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise MessageOperationEvaluationError("Stage 1 limit must be between 1 and 100")
    now = _aware_utc(created_at)
    with session_factory() as session:
        affected = session.execute(
            select(RuntimeIncidentAffectedMessage)
            .join(
                RuntimeIncident,
                RuntimeIncident.id
                == RuntimeIncidentAffectedMessage.runtime_incident_id,
            )
            .where(
                RuntimeIncidentAffectedMessage.message_operation_contract_id
                > after_contract_id,
                RuntimeIncident.incident_type == "message_operation_failure",
                ~exists(
                    select(MessageOperationStage1Notification.id).where(
                        MessageOperationStage1Notification.runtime_incident_id
                        == RuntimeIncidentAffectedMessage.runtime_incident_id,
                        MessageOperationStage1Notification.raw_message_id
                        == RuntimeIncidentAffectedMessage.raw_message_id,
                        MessageOperationStage1Notification.notification_kind
                        == "message_operation_stage1",
                    )
                ),
            )
            .order_by(
                RuntimeIncidentAffectedMessage.runtime_incident_id,
                RuntimeIncidentAffectedMessage.raw_message_id,
            )
            .limit(limit)
        ).scalars().all()
        created = 0
        for relation in affected:
            statement = sqlite_insert(MessageOperationStage1Notification).values(
                runtime_incident_id=relation.runtime_incident_id,
                raw_message_id=relation.raw_message_id,
                message_operation_contract_id=(
                    relation.message_operation_contract_id
                ),
                notification_kind="message_operation_stage1",
                status="pending",
                attempt_count=0,
                created_at=now,
                updated_at=now,
            )
            result = session.execute(
                statement.on_conflict_do_nothing(
                    index_elements=[
                        "runtime_incident_id",
                        "raw_message_id",
                        "notification_kind",
                    ]
                )
            )
            created += max(0, int(result.rowcount or 0))
        session.commit()
        return created


def _bounded_text(name: str, value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise MessageOperationEvaluationError(f"invalid {name}")
    return value


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise MessageOperationEvaluationError("deadline must be a datetime")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _evidence_refs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MessageOperationEvaluationError("invalid evidence references")
    refs = tuple(value)
    if (
        len(refs) > 32
        or not all(isinstance(ref, str) and _REFERENCE.fullmatch(ref) for ref in refs)
        or len(set(refs)) != len(refs)
    ):
        raise MessageOperationEvaluationError("invalid evidence references")
    return refs


@dataclass(frozen=True, slots=True)
class ItemOutcomeEvidence:
    instruction_key: str
    expected_descendant_kind: str
    expected_terminal_kind: str
    state: str
    observed_terminal_kind: str | None = None
    exchange_required: bool = False
    exchange_verified: bool = False
    evidence_refs: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ItemOutcomeEvidence":
        if not isinstance(value, Mapping):
            raise MessageOperationEvaluationError("item evidence must be a mapping")
        allowed = {
            "instruction_key",
            "expected_descendant_kind",
            "expected_terminal_kind",
            "state",
            "observed_terminal_kind",
            "exchange_required",
            "exchange_verified",
            "evidence_refs",
        }
        if set(value) - allowed:
            raise MessageOperationEvaluationError("unsupported item evidence field")
        state = _bounded_text("state", value.get("state"), 64)
        if state not in OUTCOME_STATES:
            raise MessageOperationEvaluationError("unsupported outcome state")
        observed = value.get("observed_terminal_kind")
        if observed is not None:
            observed = _bounded_text("observed_terminal_kind", observed, 64)
        exchange_required = value.get("exchange_required", False)
        exchange_verified = value.get("exchange_verified", False)
        if type(exchange_required) is not bool or type(exchange_verified) is not bool:
            raise MessageOperationEvaluationError("exchange evidence flags must be boolean")
        return cls(
            instruction_key=_bounded_text(
                "instruction_key", value.get("instruction_key"), 128
            ),
            expected_descendant_kind=_bounded_text(
                "expected_descendant_kind",
                value.get("expected_descendant_kind"),
                64,
            ),
            expected_terminal_kind=_bounded_text(
                "expected_terminal_kind", value.get("expected_terminal_kind"), 64
            ),
            state=state,
            observed_terminal_kind=observed,
            exchange_required=exchange_required,
            exchange_verified=exchange_verified,
            evidence_refs=_evidence_refs(value.get("evidence_refs")),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "instruction_key": self.instruction_key,
            "expected_descendant_kind": self.expected_descendant_kind,
            "expected_terminal_kind": self.expected_terminal_kind,
            "state": self.state,
            "observed_terminal_kind": self.observed_terminal_kind,
            "exchange_required": self.exchange_required,
            "exchange_verified": self.exchange_verified,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class ContractOutcomeEvidence:
    items: tuple[ItemOutcomeEvidence, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ContractOutcomeEvidence":
        if not isinstance(value, Mapping) or set(value) != {"items"}:
            raise MessageOperationEvaluationError(
                "contract evidence must contain exactly items"
            )
        raw_items = value["items"]
        if (
            not isinstance(raw_items, Sequence)
            or isinstance(raw_items, (str, bytes))
            or len(raw_items) > 32
        ):
            raise MessageOperationEvaluationError("invalid contract evidence items")
        items = tuple(ItemOutcomeEvidence.from_mapping(item) for item in raw_items)
        keys = [item.instruction_key for item in items]
        if len(keys) != len(set(keys)):
            raise MessageOperationEvaluationError("duplicate instruction evidence")
        return cls(items=items)

    def to_mappings(self) -> tuple[dict[str, object], ...]:
        return tuple(item.to_mapping() for item in self.items)


@dataclass(frozen=True, slots=True)
class MessageOperationEvaluation:
    status: str
    violation_code: str | None
    should_create_incident: bool
    evidence_refs: tuple[str, ...]
    item_states: tuple[str, ...]


def _item_violation(
    item: ItemOutcomeEvidence,
    *,
    deadline_elapsed: bool,
) -> str | None:
    immediate = _IMMEDIATE_VIOLATIONS.get(item.state)
    if immediate is not None:
        return immediate
    if item.state == "verified":
        if item.observed_terminal_kind != item.expected_terminal_kind:
            return "exchange_readback_mismatch"
        if item.exchange_required and not item.exchange_verified:
            return "local_success_unverified" if deadline_elapsed else None
        return None
    if item.state == "local_success":
        if item.exchange_required and item.exchange_verified:
            return None
        return "local_success_unverified" if deadline_elapsed else None
    if item.state == "missing" and deadline_elapsed:
        return (
            "missing_management_descendant"
            if item.expected_descendant_kind in _MANAGEMENT_DESCENDANTS
            else "no_operation_created"
        )
    if item.state == "observing" and deadline_elapsed:
        return "operation_timeout"
    return None


def evaluate_message_operation_contract(
    *,
    contract: object,
    evidence: ContractOutcomeEvidence,
    observed_at: datetime,
) -> MessageOperationEvaluation:
    """Return one closed, fail-closed outcome without performing any write."""

    observed = _aware_utc(observed_at)
    deadline = _aware_utc(getattr(contract, "deadline_at", None))
    deadline_elapsed = observed >= deadline
    refs = tuple(
        dict.fromkeys(ref for item in evidence.items for ref in item.evidence_refs)
    )
    states = tuple(item.state for item in evidence.items)

    for item in evidence.items:
        violation = _item_violation(item, deadline_elapsed=deadline_elapsed)
        if violation is not None:
            if violation not in MESSAGE_OPERATION_VIOLATIONS:
                raise MessageOperationEvaluationError("unknown violation code")
            return MessageOperationEvaluation(
                status="violated",
                violation_code=violation,
                should_create_incident=True,
                evidence_refs=refs,
                item_states=states,
            )

    if not evidence.items:
        status = "violated" if deadline_elapsed else "observing"
        violation = "no_operation_created" if deadline_elapsed else None
    elif all(item.state == "duplicate_verified" for item in evidence.items):
        status, violation = "duplicate_verified", None
    elif all(item.state == "superseded_verified" for item in evidence.items):
        status, violation = "superseded_verified", None
    elif all(
        item.state in {"verified", "duplicate_verified", "superseded_verified"}
        or (
            item.state == "local_success"
            and (not item.exchange_required or item.exchange_verified)
        )
        for item in evidence.items
    ):
        status, violation = "verified", None
    else:
        status, violation = "observing", None
    if status not in EVALUATION_STATUSES:
        raise MessageOperationEvaluationError("unknown evaluation status")
    return MessageOperationEvaluation(
        status=status,
        violation_code=violation,
        should_create_incident=status == "violated",
        evidence_refs=refs,
        item_states=states,
    )


def _bounded_json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 65_536:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _stored_refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or len(value) > 4096:
        return ()
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    try:
        return _evidence_refs(decoded)
    except MessageOperationEvaluationError:
        return ()


def _cap_collected_refs(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for group in groups for ref in group))[:32]


def _instruction_state(item: MessageInstructionItem) -> tuple[str, str | None]:
    status = str(item.status or "").strip().lower()
    error = _bounded_json_object(item.error_json)
    error_type = str(error.get("type") or "")
    error_message = str(error.get("message") or "")
    if (
        status == "failed"
        and error_type == "RecoveryLiveSubmitError"
        and error_message.startswith("signal_enqueue_blocked:")
    ):
        return "safety_refusal", "verified_refusal"
    if status in {"failed", "partial_failed"}:
        return "partial", None
    if status in {"unknown", "submit_unknown", "recovery_required"}:
        return "unknown", None
    if status in {"succeeded", "completed", "confirmed"}:
        return "local_success", None
    if status == "duplicate":
        return "duplicate_verified", None
    return "observing", None


def _target_state(target: ManagementMessageTarget) -> tuple[str, str | None]:
    admission = str(target.admission_state or "").strip().lower()
    execution = str(target.execution_state or "").strip().lower()
    if admission == "refused":
        return "safety_refusal", "verified_refusal"
    if execution == "confirmed":
        return "local_success", None
    if execution == "failed":
        return "partial", None
    if execution in {"submit_unknown", "recovery_required"}:
        return "unknown", None
    return "observing", None


def _explicit_durable_failure_state(*values: object) -> str | None:
    normalized = " ".join(str(value or "").strip().lower() for value in values)
    if "reconciliation_disproved" in normalized:
        return "reconciliation_disproved"
    if "exchange_readback_mismatch" in normalized or "readback_mismatch" in normalized:
        return "exchange_mismatch"
    if "restart_or_lease_skip" in normalized or (
        "skip" in normalized and ("restart" in normalized or "lease" in normalized)
    ):
        return "restart_skip"
    return None


def _expected_exchange_actions(instruction_kind: str) -> frozenset[str]:
    return {
        "new_entry": frozenset(
            {
                "entry",
                "entry_signal",
                "open",
                "open_position",
                "submit_entry",
                "create_trigger_entry",
                "create_limit_entry",
                "open_market_position",
                "recreate_trigger_entry",
            }
        ),
        "add_entry": frozenset(
            {
                "add_entry",
                "add_position",
                "create_trigger_entry",
                "create_limit_entry",
                "open_market_position",
                "recreate_trigger_entry",
            }
        ),
        "take_profit": frozenset(
            {
                "partial_take_profit",
                "take_profit",
                "reduce_position",
                "close_half",
                "close_position",
                "close_bound_position_market",
                "strategy_management_close_submit",
            }
        ),
        "stop_loss": frozenset(
            {
                "stop_loss",
                "move_stop_to_break_even",
                "move_stop_to_protect",
                "break_even",
                "modify_stop",
                "protection",
                "set_position_sltp",
                "set_position_tpsl",
                "adjust_position_tpsl",
                "create_backup_stop",
                "strategy_management_protection_restore",
                "trigger_protection_stop_rescue",
                "migrate_native_tpsl_backup_stop",
            }
        ),
        "cancel": frozenset(
            {
                "cancel",
                "cancel_entry",
                "cancel_order",
                "cancel_pending_entry",
                "cancel_trigger_order",
                "cancel_trigger_entry",
                "cancel_regular_entry",
                "cancel_entry_absent_confirmed",
                "cancel_revision_entry_leg",
                "reply_cancel_after_entry",
                "strategy_management_cancel_deferred_trigger_entry",
                "strategy_management_cancel_deferred_regular_entry",
            }
        ),
        "exit": frozenset(
            {
                "exit",
                "exit_position",
                "full_exit",
                "full_close",
                "close_position",
                "close_bound_position_market",
                "strategy_management_close_submit",
            }
        ),
        "manage": frozenset({"manage"}),
        "other_management": frozenset({"other_management"}),
        "unresolved_executable": frozenset(),
    }[instruction_kind]


def _event_matches_source_message(event: ExecutionEvent, raw: RawMessage) -> bool:
    return bool(
        event.source_message_id == raw.id
        or (
            event.source_message_id == raw.message_id
            and event.chat_id == raw.chat_id
        )
        or (event.chat_id == raw.chat_id and event.message_id == raw.message_id)
    )


def _exchange_outcome(
    session,
    *,
    raw: RawMessage,
    row: MessageOperationItem,
    strategy_instance_ids: set[str],
    base_state: str,
    allow_raw_scope: bool,
    allow_item_success: bool,
    retrospective: bool,
) -> tuple[str, str | None, tuple[str, ...]]:
    """Return only independently corroborated durable exchange outcomes."""

    source_time = raw.posted_at or raw.created_at
    expected_actions = _expected_exchange_actions(row.instruction_kind)

    binding_filters = []
    if strategy_instance_ids:
        binding_filters.append(
            ExecutionBinding.strategy_instance_id.in_(tuple(strategy_instance_ids))
        )
    elif allow_raw_scope:
        binding_filters.append(
            (ExecutionBinding.chat_id == raw.chat_id)
            & (ExecutionBinding.message_id == raw.message_id)
        )
    bindings = (
        session.execute(
            select(ExecutionBinding)
            .where(or_(*binding_filters))
            .order_by(ExecutionBinding.updated_at.desc(), ExecutionBinding.id.desc())
            .limit(32)
        ).scalars().all()
        if binding_filters
        else []
    )
    binding_ids = {int(binding.id) for binding in bindings}
    refs = [f"execution_binding:{binding.id}" for binding in bindings]

    def outcome_refs(*decisive: str) -> tuple[str, ...]:
        return _cap_collected_refs(decisive, refs)

    event_filters = []
    if strategy_instance_ids:
        event_filters.append(
            ExecutionEvent.strategy_instance_id.in_(tuple(strategy_instance_ids))
        )
    elif allow_raw_scope:
        event_filters.extend(
            (
                ExecutionEvent.source_message_id == raw.id,
                (ExecutionEvent.chat_id == raw.chat_id)
                & (ExecutionEvent.message_id == raw.message_id),
            )
        )
    if binding_ids:
        event_filters.append(ExecutionEvent.execution_binding_id.in_(binding_ids))
    events = (
        session.execute(
            select(ExecutionEvent)
            .where(or_(*event_filters))
            .where(ExecutionEvent.created_at >= source_time)
            .order_by(ExecutionEvent.created_at.desc(), ExecutionEvent.id.desc())
            .limit(64)
        ).scalars().all()
        if event_filters
        else []
    )
    refs.extend(f"execution_event:{event.id}" for event in events)
    for event in events:
        failure = _explicit_durable_failure_state(
            event.action, event.status, event.reason
        )
        failure_is_exact = _event_matches_source_message(event, raw)
        if failure is not None and failure_is_exact:
            return failure, None, outcome_refs(f"execution_event:{event.id}")
    for binding in bindings:
        failure = _explicit_durable_failure_state(
            binding.status, binding.last_exchange_status
        )
        if failure is not None and row.instruction_kind == "new_entry":
            return failure, None, outcome_refs(f"execution_binding:{binding.id}")
        if (
            row.instruction_kind == "new_entry"
            and str(binding.last_exchange_status or "").strip().lower()
            in {
                "position_attribution_conflict",
                "position_ownership_unassigned",
            }
        ):
            return "exchange_mismatch", None, outcome_refs(
                f"execution_binding:{binding.id}"
            )

    verified_event_statuses = {"confirmed", "filled", "verified"}
    verified_event = next(
        (
            event
            for event in events
            if str(event.status or "").strip().lower() in verified_event_statuses
            and str(event.action or "").strip().lower() in expected_actions
            and _event_matches_source_message(event, raw)
        ),
        None,
    )
    if verified_event is not None and allow_item_success:
        return (
            "verified",
            row.expected_terminal_kind,
            outcome_refs(f"execution_event:{verified_event.id}"),
        )
    verified_binding = next(
        (
            binding
            for binding in bindings
            if row.instruction_kind == "new_entry"
            and str(binding.status or "").strip().lower() == "active"
            and str(binding.last_exchange_status or "").strip().lower()
            in {"position_ownership_verified", "manual_bound_live_position"}
        ),
        None,
    )
    if verified_binding is not None and allow_item_success:
        return (
            "verified",
            row.expected_terminal_kind,
            outcome_refs(f"execution_binding:{verified_binding.id}"),
        )

    batch_filters = [StrategyManagementBatch.raw_message_id == raw.id]
    if strategy_instance_ids:
        batch_filters.append(
            StrategyManagementBatch.strategy_instance_id.in_(
                tuple(strategy_instance_ids)
            )
        )
    batches = session.execute(
        select(StrategyManagementBatch)
        .where(*batch_filters)
        .order_by(StrategyManagementBatch.id.desc())
        .limit(32)
    ).scalars().all()
    refs.extend(f"strategy_management_batch:{batch.id}" for batch in batches)
    batch_ids = {int(batch.id) for batch in batches}
    legs = (
        session.execute(
            select(StrategyManagementLeg)
            .where(StrategyManagementLeg.management_batch_id.in_(batch_ids))
            .order_by(StrategyManagementLeg.id)
            .limit(32)
        ).scalars().all()
        if batch_ids
        else []
    )
    refs.extend(f"strategy_management_leg:{leg.id}" for leg in legs)
    leg_ids = {int(leg.execution_order_leg_id) for leg in legs}

    if leg_ids:
        mutations = session.execute(
            select(PositionMutationIntent)
            .where(PositionMutationIntent.execution_order_leg_id.in_(leg_ids))
            .where(PositionMutationIntent.created_at >= source_time)
            .order_by(PositionMutationIntent.created_at.desc(), PositionMutationIntent.id.desc())
            .limit(32)
        ).scalars().all()
        refs.extend(f"position_mutation_intent:{item.id}" for item in mutations)
        for mutation in mutations:
            failure = _explicit_durable_failure_state(
                mutation.status, mutation.error_json
            )
            if (
                failure is not None
                and not retrospective
                and str(mutation.operation or "").strip().lower()
                in expected_actions
            ):
                return failure, None, outcome_refs(
                    f"position_mutation_intent:{mutation.id}"
                )
        verified_mutation = next(
            (
                mutation
                for mutation in mutations
                if str(mutation.status or "").strip().lower() == "confirmed"
                and mutation.confirmed_at is not None
                and str(mutation.operation or "").strip().lower()
                in expected_actions
            ),
            None,
        )
        if verified_mutation is not None and allow_item_success:
            return (
                "verified",
                row.expected_terminal_kind,
                outcome_refs(f"position_mutation_intent:{verified_mutation.id}"),
            )

    if leg_ids and row.instruction_kind == "stop_loss":
        revisions = session.execute(
            select(PositionProtectionRevision)
            .where(PositionProtectionRevision.execution_order_leg_id.in_(leg_ids))
            .where(PositionProtectionRevision.created_at >= source_time)
            .order_by(
                PositionProtectionRevision.created_at.desc(),
                PositionProtectionRevision.id.desc(),
            )
            .limit(32)
        ).scalars().all()
        refs.extend(f"protection_revision:{revision.id}" for revision in revisions)
        active_revision = next(
            (
                revision
                for revision in revisions
                if str(revision.status or "").strip().lower() == "active"
            ),
            None,
        )
        if active_revision is not None and allow_item_success:
            return (
                "verified",
                row.expected_terminal_kind,
                outcome_refs(f"protection_revision:{active_revision.id}"),
            )
    return base_state, None, outcome_refs()


def _recognition_state(
    decision: RecognitionDecision | None,
    context: ContextResolutionAttempt | None,
) -> tuple[str, str | None]:
    if decision is None:
        return "missing", None
    authoritative = str(decision.authoritative_status or "").strip().lower()
    if authoritative in {"recognition-failed", "recognition_failed", "failed"}:
        return "recognition_failed", None
    if context is None:
        return "missing", None
    status = str(context.status or "").strip().lower()
    if status in {"exhausted", "failed"}:
        return "context_exhausted", None
    payload = _bounded_json_object(context.decision_json)
    resolution = str(payload.get("decision") or "").strip().lower()
    if resolution in {"hold", "unresolved"}:
        return resolution, None
    return "observing", None


def _operation_row_strategy_ids(
    session,
    *,
    row: MessageOperationItem,
    raw_message_id: int,
    candidates_by_id: Mapping[int, SignalCandidate],
) -> frozenset[str]:
    instruction: MessageInstructionItem | None = None
    prefix, _, raw_id = row.instruction_key.partition(":")
    try:
        identity = int(raw_id)
    except (TypeError, ValueError):
        identity = 0
    if prefix == "message_instruction":
        instruction = session.get(MessageInstructionItem, identity)
    elif prefix == "management_target":
        target = session.get(ManagementMessageTarget, identity)
        if target is not None and target.message_instruction_item_id is not None:
            instruction = session.get(
                MessageInstructionItem, target.message_instruction_item_id
            )
    elif prefix == "signal_candidate":
        candidate = candidates_by_id.get(identity)
        if candidate is not None:
            instruction = session.execute(
                select(MessageInstructionItem)
                .where(
                    MessageInstructionItem.raw_message_id == raw_message_id,
                    MessageInstructionItem.signal_candidate_id == candidate.id,
                )
                .order_by(MessageInstructionItem.id.desc())
            ).scalars().first()
    if (
        instruction is None
        or instruction.raw_message_id != raw_message_id
        or not instruction.strategy_instance_id
    ):
        return frozenset()
    return frozenset({instruction.strategy_instance_id})


def collect_message_operation_evidence(
    session_factory: sessionmaker,
    *,
    contract_id: int,
) -> ContractOutcomeEvidence:
    """Collect only bounded durable evidence for one additive contract."""

    if type(contract_id) is not int or contract_id < 1:
        raise MessageOperationEvaluationError("contract_id must be positive")
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        if contract is None:
            raise MessageOperationEvaluationError("contract not found")
        raw = session.get(RawMessage, contract.raw_message_id)
        if raw is None:
            raise MessageOperationEvaluationError("contract raw message not found")
        rows = session.execute(
            select(MessageOperationItem)
            .where(MessageOperationItem.contract_id == contract_id)
            .order_by(MessageOperationItem.sequence, MessageOperationItem.id)
        ).scalars().all()
        if len(rows) > 32:
            raise MessageOperationEvaluationError("contract item evidence is unbounded")
        decision = session.execute(
            select(RecognitionDecision).where(
                RecognitionDecision.raw_message_id == contract.raw_message_id
            )
        ).scalar_one_or_none()
        context = session.execute(
            select(ContextResolutionAttempt)
            .where(ContextResolutionAttempt.raw_message_id == contract.raw_message_id)
            .order_by(ContextResolutionAttempt.id.desc())
        ).scalars().first()
        candidates = session.execute(
            select(SignalCandidate)
            .where(SignalCandidate.raw_message_id == contract.raw_message_id)
            .order_by(SignalCandidate.id)
            .limit(32)
        ).scalars().all()
        candidates_by_id = {int(candidate.id): candidate for candidate in candidates}
        strategy_ids_by_key = {
            row.instruction_key: _operation_row_strategy_ids(
                session,
                row=row,
                raw_message_id=contract.raw_message_id,
                candidates_by_id=candidates_by_id,
            )
            for row in rows
        }
        scope_counts: dict[tuple[str, frozenset[str]], int] = {}
        for row in rows:
            scope = (row.instruction_kind, strategy_ids_by_key[row.instruction_key])
            scope_counts[scope] = scope_counts.get(scope, 0) + 1
        evidence_items: list[ItemOutcomeEvidence] = []
        for row in rows:
            state = "missing"
            observed_terminal: str | None = None
            refs = list(_stored_refs(row.evidence_refs_json))
            exchange_refs: tuple[str, ...] = ()
            strategy_instance_ids = set(strategy_ids_by_key[row.instruction_key])
            if row.instruction_key.startswith("management_target:"):
                try:
                    target_id = int(row.instruction_key.split(":", 1)[1])
                except (TypeError, ValueError):
                    target_id = 0
                target = session.get(ManagementMessageTarget, target_id)
                if target is not None and target.raw_message_id == contract.raw_message_id:
                    state, observed_terminal = _target_state(target)
                    refs.append(f"management_target:{target.id}")
                    instruction = (
                        session.get(
                            MessageInstructionItem,
                            target.message_instruction_item_id,
                        )
                        if target.message_instruction_item_id is not None
                        else None
                    )
                    if instruction is not None and instruction.strategy_instance_id:
                        strategy_instance_ids.add(instruction.strategy_instance_id)
            elif row.instruction_key.startswith("message_instruction:"):
                try:
                    item_id = int(row.instruction_key.split(":", 1)[1])
                except (TypeError, ValueError):
                    item_id = 0
                instruction = session.get(MessageInstructionItem, item_id)
                if (
                    instruction is not None
                    and instruction.raw_message_id == contract.raw_message_id
                ):
                    state, observed_terminal = _instruction_state(instruction)
                    refs.append(f"message_instruction:{instruction.id}")
                    if instruction.strategy_instance_id:
                        strategy_instance_ids.add(instruction.strategy_instance_id)
                    candidate = candidates_by_id.get(instruction.signal_candidate_id)
                    if candidate is not None:
                        refs.append(f"signal_candidate:{candidate.id}")
            elif row.instruction_key.startswith("signal_candidate:"):
                try:
                    candidate_id = int(row.instruction_key.split(":", 1)[1])
                except (TypeError, ValueError):
                    candidate_id = 0
                candidate = candidates_by_id.get(candidate_id)
                if candidate is not None:
                    refs.append(f"signal_candidate:{candidate.id}")
                    state = "observing"
                    instruction = session.execute(
                        select(MessageInstructionItem)
                        .where(
                            MessageInstructionItem.raw_message_id
                            == contract.raw_message_id,
                            MessageInstructionItem.signal_candidate_id == candidate.id,
                        )
                        .order_by(MessageInstructionItem.id.desc())
                    ).scalars().first()
                    if instruction is not None:
                        state, observed_terminal = _instruction_state(instruction)
                        refs.append(f"message_instruction:{instruction.id}")
                        if instruction.strategy_instance_id:
                            strategy_instance_ids.add(
                                instruction.strategy_instance_id
                            )
            elif row.instruction_key.startswith("recognition_decision:"):
                state, observed_terminal = _recognition_state(decision, context)
                if decision is not None:
                    refs.append(f"recognition_decision:{decision.id}")
                if context is not None:
                    refs.append(f"context_resolution_attempt:{context.id}")
            if row.expected_descendant_kind != "context_resolution_attempt" and state not in {
                "recognition_failed",
                "hold",
                "unresolved",
                "context_exhausted",
                "safety_refusal",
                "action_refused",
                "partial",
                "unknown",
                "duplicate_verified",
                "superseded_verified",
            }:
                state, exchange_terminal, exchange_refs = _exchange_outcome(
                    session,
                    raw=raw,
                    row=row,
                    strategy_instance_ids=strategy_instance_ids,
                    base_state=state,
                    allow_raw_scope=len(rows) == 1,
                    allow_item_success=(
                        scope_counts[
                            (
                                row.instruction_kind,
                                strategy_ids_by_key[row.instruction_key],
                            )
                        ]
                        == 1
                    ),
                    retrospective=contract.status == "verified",
                )
                if exchange_terminal is not None:
                    observed_terminal = exchange_terminal
            if state == "verified" and observed_terminal is None:
                observed_terminal = row.expected_terminal_kind
            exchange_required = row.expected_terminal_kind in {
                "verified_entry",
                "verified_execution",
                "verified_cancel",
                "verified_exit",
                "verified_protection",
            }
            exchange_verified = state == "verified"
            evidence_items.append(
                ItemOutcomeEvidence(
                    instruction_key=row.instruction_key,
                    expected_descendant_kind=row.expected_descendant_kind,
                    expected_terminal_kind=row.expected_terminal_kind,
                    state=state,
                    observed_terminal_kind=observed_terminal,
                    exchange_required=exchange_required,
                    exchange_verified=exchange_verified,
                    evidence_refs=_cap_collected_refs(exchange_refs, refs),
                )
            )
        return ContractOutcomeEvidence(items=tuple(evidence_items))


def _item_storage_status(
    item: ItemOutcomeEvidence,
    *,
    deadline_elapsed: bool,
) -> str:
    if item.state == "duplicate_verified":
        return "duplicate"
    if item.state == "superseded_verified":
        return "superseded"
    if _item_violation(item, deadline_elapsed=deadline_elapsed) is not None:
        return "violated"
    if item.state == "verified" or (
        item.state == "local_success"
        and (not item.exchange_required or item.exchange_verified)
    ):
        return "verified"
    return "observing"


def _apply_shadow_evaluation(
    session_factory: sessionmaker,
    *,
    contract_id: int,
    evidence: ContractOutcomeEvidence,
    evaluation: MessageOperationEvaluation,
    observed_at: datetime,
) -> None:
    storage_status = {
        "observing": "observing",
        "verified": "verified",
        "violated": "violated",
        "duplicate_verified": "duplicate",
        "superseded_verified": "superseded",
    }[evaluation.status]
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        if contract is None or contract.status not in {"observing", "verified"}:
            return
        prior_status = contract.status
        if prior_status == "verified" and evaluation.violation_code not in {
            "exchange_readback_mismatch",
            "reconciliation_disproved_success",
        }:
            contract.updated_at = observed_at
            session.commit()
            return
        rows = session.execute(
            select(MessageOperationItem)
            .where(MessageOperationItem.contract_id == contract_id)
            .order_by(MessageOperationItem.sequence, MessageOperationItem.id)
        ).scalars().all()
        if [row.instruction_key for row in rows] != [
            item.instruction_key for item in evidence.items
        ]:
            raise MessageOperationEvaluationError("contract evidence changed")
        deadline_elapsed = _aware_utc(observed_at) >= _aware_utc(contract.deadline_at)
        for row, item in zip(rows, evidence.items, strict=True):
            row.status = _item_storage_status(item, deadline_elapsed=deadline_elapsed)
            row.observed_terminal_kind = (
                "verified_refusal"
                if item.state in {"safety_refusal", "action_refused"}
                else item.observed_terminal_kind
            )
            row.evidence_refs_json = json.dumps(
                item.evidence_refs,
                ensure_ascii=True,
                separators=(",", ":"),
            )
            row.updated_at = observed_at
        result = session.execute(
            update(MessageOperationContract)
            .where(
                MessageOperationContract.id == contract_id,
                MessageOperationContract.status == prior_status,
            )
            .values(
                status=storage_status,
                violation_code=evaluation.violation_code,
                evidence_refs_json=json.dumps(
                    evaluation.evidence_refs,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                runtime_incident_id=None,
                agent_requested=False,
                updated_at=observed_at,
            )
        )
        if result.rowcount != 1:
            session.rollback()
            return
        session.commit()


def run_message_operation_outcome_shadow_once(
    session_factory: sessionmaker,
    *,
    limit: int,
    observed_at: datetime,
) -> dict[str, int]:
    """Evaluate one bounded batch and persist observation state only."""

    if type(limit) is not int or not 1 <= limit <= 100:
        raise MessageOperationEvaluationError("limit must be between 1 and 100")
    with session_factory() as session:
        contract_ids = session.execute(
            select(MessageOperationContract.id)
            .where(MessageOperationContract.status.in_(("observing", "verified")))
            .order_by(
                MessageOperationContract.status == "verified",
                MessageOperationContract.updated_at,
                MessageOperationContract.deadline_at,
                MessageOperationContract.id,
            )
            .limit(limit)
        ).scalars().all()
    result = {
        "errors": 0,
        "evaluated": 0,
        "observing": 0,
        "verified": 0,
        "violated": 0,
        "duplicate_verified": 0,
        "superseded_verified": 0,
        "incidents_created": 0,
        "model_calls": 0,
        "rechecked_verified": 0,
    }
    for contract_id in contract_ids:
        try:
            with session_factory() as session:
                contract = session.get(MessageOperationContract, contract_id)
                if contract is None or contract.status not in {"observing", "verified"}:
                    continue
                prior_status = contract.status
                session.expunge(contract)
            evidence = collect_message_operation_evidence(
                session_factory, contract_id=contract_id
            )
            evaluation = evaluate_message_operation_contract(
                contract=contract,
                evidence=evidence,
                observed_at=observed_at,
            )
            _apply_shadow_evaluation(
                session_factory,
                contract_id=contract_id,
                evidence=evidence,
                evaluation=evaluation,
                observed_at=observed_at,
            )
            result["evaluated"] += 1
            if prior_status == "verified":
                result["rechecked_verified"] += 1
            effective_status = evaluation.status
            if prior_status == "verified" and evaluation.violation_code not in {
                "exchange_readback_mismatch",
                "reconciliation_disproved_success",
            }:
                effective_status = "verified"
            result[effective_status] += 1
        except Exception:
            result["errors"] += 1
    return result
