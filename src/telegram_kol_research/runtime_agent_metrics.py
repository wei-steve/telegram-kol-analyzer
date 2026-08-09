"""Bounded Runtime Agent cost, quality, and review projections."""

from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.models import (
    RuntimeAgentDiagnosisReview,
    RuntimeAgentModelUsage,
    RuntimeAgentRecoveryAttempt,
    RuntimeIncident,
    RuntimeIncidentHandoffArtifact,
)
from telegram_kol_research.runtime_agent_evaluation import assert_runtime_agent_fixture_redacted
from telegram_kol_research.runtime_agent_policy import evaluate_shadow_playbook_nomination


class RuntimeAgentBudgetExceeded(RuntimeError):
    """Raised before provider execution when a conservative budget is full."""


_REGRESSION_SOURCE_FILES = (
    "src/telegram_kol_research/runtime_agent_prompt.py",
    "src/telegram_kol_research/runtime_agent_tools.py",
    "src/telegram_kol_research/runtime_agent_policy.py",
    "src/telegram_kol_research/runtime_agent_playbooks.py",
)


def build_runtime_agent_regression_manifest(*, project_root: Path) -> dict[str, Any]:
    root = Path(project_root)
    sources = {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in _REGRESSION_SOURCE_FILES
    }
    fixture_paths = sorted((root / "tests/fixtures/runtime_incidents").glob("*.json"))
    if not fixture_paths:
        raise ValueError("runtime agent regression corpus is empty")
    corpus_hash = hashlib.sha256()
    for path in fixture_paths:
        corpus_hash.update(path.name.encode("utf-8"))
        corpus_hash.update(b"\0")
        corpus_hash.update(path.read_bytes())
        corpus_hash.update(b"\0")
    return {
        "schema_version": 1,
        "source_files": sources,
        "corpus_case_count": len(fixture_paths),
        "corpus_sha256": corpus_hash.hexdigest(),
    }


def validate_runtime_agent_regression_manifest(
    *, project_root: Path, manifest_path: str
) -> None:
    root = Path(project_root)
    try:
        recorded = json.loads((root / manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("runtime agent regression manifest is invalid") from exc
    if recorded != build_runtime_agent_regression_manifest(project_root=root):
        raise ValueError(
            "runtime agent prompt/tool/policy/playbook or reviewed corpus changed"
        )


def fingerprint_runtime_agent_diagnosis(value: str) -> str:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("diagnosis is invalid") from exc
    if not isinstance(parsed, dict):
        raise ValueError("diagnosis is invalid")
    return hashlib.sha256(
        json.dumps(parsed, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def reserve_model_tokens(
    session_factory,
    *,
    incident_id: int,
    incident_fingerprint: str,
    call_key: str,
    reserved_tokens: int,
    per_incident_limit: int,
    daily_limit: int,
    now: datetime,
) -> RuntimeAgentModelUsage:
    """Atomically reserve worst-case tokens before a provider request."""

    amount = int(reserved_tokens)
    per_limit = int(per_incident_limit)
    day_limit = int(daily_limit)
    if not (1 <= amount <= 1_000_000):
        raise ValueError("reserved token count is invalid")
    if per_limit < amount or day_limit < amount:
        raise RuntimeAgentBudgetExceeded("incident or daily model token budget exceeded")
    moment = _utc_naive(now)
    budget_date = moment.date().isoformat()
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        existing = session.scalar(
            select(RuntimeAgentModelUsage).where(RuntimeAgentModelUsage.call_key == call_key)
        )
        if existing is not None:
            if (
                int(existing.runtime_incident_id) != int(incident_id)
                or existing.incident_fingerprint != incident_fingerprint
                or int(existing.reserved_tokens) != amount
                or existing.budget_date != budget_date
            ):
                session.rollback()
                raise ValueError("model token reservation identity conflicts")
            session.commit()
            session.refresh(existing)
            session.expunge(existing)
            return existing
        incident_total = int(
            session.scalar(
                select(func.coalesce(func.sum(RuntimeAgentModelUsage.reserved_tokens), 0)).where(
                    RuntimeAgentModelUsage.runtime_incident_id == int(incident_id)
                )
            )
            or 0
        )
        if incident_total + amount > per_limit:
            session.rollback()
            raise RuntimeAgentBudgetExceeded("per-incident model token budget exceeded")
        daily_total = int(
            session.scalar(
                select(func.coalesce(func.sum(RuntimeAgentModelUsage.reserved_tokens), 0)).where(
                    RuntimeAgentModelUsage.budget_date == budget_date
                )
            )
            or 0
        )
        if daily_total + amount > day_limit:
            session.rollback()
            raise RuntimeAgentBudgetExceeded("daily model token budget exceeded")
        row = RuntimeAgentModelUsage(
            runtime_incident_id=int(incident_id),
            incident_fingerprint=str(incident_fingerprint),
            call_key=str(call_key),
            budget_date=budget_date,
            status="reserved",
            reserved_tokens=amount,
            created_at=moment,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            duplicate = session.scalar(
                select(RuntimeAgentModelUsage).where(RuntimeAgentModelUsage.call_key == call_key)
            )
            if duplicate is None:
                raise
            session.expunge(duplicate)
            return duplicate
        session.refresh(row)
        session.expunge(row)
        return row


def settle_model_tokens(
    session_factory,
    *,
    reservation_id: int,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    now: datetime,
) -> None:
    prompt = int(prompt_tokens)
    completion = int(completion_tokens)
    total = int(total_tokens)
    if min(prompt, completion, total) < 0 or prompt + completion != total:
        raise ValueError("provider token usage is invalid")
    with session_factory() as session:
        row = session.get(RuntimeAgentModelUsage, int(reservation_id))
        if row is None or total > int(row.reserved_tokens):
            raise ValueError("provider token usage exceeds reservation")
        if row.status == "completed":
            if (row.prompt_tokens, row.completion_tokens, row.total_tokens) != (
                prompt,
                completion,
                total,
            ):
                raise ValueError("provider token settlement conflicts")
            return
        row.status = "completed"
        row.prompt_tokens = prompt
        row.completion_tokens = completion
        row.total_tokens = total
        row.completed_at = _utc_naive(now)
        session.commit()


def fail_model_token_reservation(session_factory, *, reservation_id: int, now: datetime) -> None:
    with session_factory() as session:
        row = session.get(RuntimeAgentModelUsage, int(reservation_id))
        if row is not None and row.status == "reserved":
            row.status = "failed"
            row.completed_at = _utc_naive(now)
            session.commit()


def record_diagnosis_review(
    session_factory,
    *,
    incident_id: int,
    verdict: str,
    diagnosis_fingerprint: str,
    fixture_case_id: str | None,
    now: datetime,
) -> RuntimeAgentDiagnosisReview:
    if verdict not in {"confirmed", "partial", "rejected"}:
        raise ValueError("review verdict is invalid")
    if len(diagnosis_fingerprint) != 64:
        raise ValueError("diagnosis fingerprint is invalid")
    with session_factory() as session:
        incident = session.get(RuntimeIncident, int(incident_id))
        if incident is None or not incident.diagnosis_json:
            raise ValueError("diagnosed incident is required")
        if fingerprint_runtime_agent_diagnosis(incident.diagnosis_json) != diagnosis_fingerprint:
            raise ValueError("diagnosis fingerprint does not match incident")
        row = session.scalar(
            select(RuntimeAgentDiagnosisReview).where(
                RuntimeAgentDiagnosisReview.runtime_incident_id == int(incident_id),
                RuntimeAgentDiagnosisReview.diagnosis_fingerprint == diagnosis_fingerprint,
            )
        )
        if row is None:
            row = RuntimeAgentDiagnosisReview(
                runtime_incident_id=int(incident_id),
                diagnosis_fingerprint=diagnosis_fingerprint,
                verdict=verdict,
                fixture_case_id=fixture_case_id,
                reviewed_at=_utc_naive(now),
            )
            session.add(row)
        elif row.verdict != verdict or row.fixture_case_id != fixture_case_id:
            raise ValueError("diagnosis review is immutable")
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _safe_mapping(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_runtime_agent_metrics(
    session_factory,
    *,
    since: datetime,
    until: datetime,
    max_incidents: int = 1000,
) -> dict[str, Any]:
    start = _utc_naive(since)
    end = _utc_naive(until)
    if end <= start or end - start > timedelta(days=31):
        raise ValueError("metrics window is invalid")
    limit = max(1, min(int(max_incidents), 1000))
    with session_factory() as session:
        incidents = list(
            session.scalars(
                select(RuntimeIncident)
                .where(RuntimeIncident.created_at >= start, RuntimeIncident.created_at < end)
                .order_by(RuntimeIncident.id)
                .limit(limit + 1)
            )
        )
        if len(incidents) > limit:
            raise ValueError("metrics incident scan is truncated")
        ids = [int(row.id) for row in incidents]
        usage_rows = list(
            session.scalars(
                select(RuntimeAgentModelUsage).where(RuntimeAgentModelUsage.runtime_incident_id.in_(ids))
            )
        ) if ids else []
        reviews = list(
            session.scalars(
                select(RuntimeAgentDiagnosisReview).where(RuntimeAgentDiagnosisReview.runtime_incident_id.in_(ids))
            )
        ) if ids else []
        recoveries = list(
            session.scalars(
                select(RuntimeAgentRecoveryAttempt).where(RuntimeAgentRecoveryAttempt.incident_id.in_(ids))
            )
        ) if ids else []
        handoffs = list(
            session.scalars(
                select(RuntimeIncidentHandoffArtifact)
                .where(RuntimeIncidentHandoffArtifact.runtime_incident_id.in_(ids))
                .order_by(
                    RuntimeIncidentHandoffArtifact.runtime_incident_id,
                    RuntimeIncidentHandoffArtifact.diagnosis_revision,
                )
            )
        ) if ids else []
    latest_handoff = {int(row.runtime_incident_id): row for row in handoffs}
    outcomes: dict[str, int] = {}
    tool_steps: list[int] = []
    latencies: list[int] = []
    for row in incidents:
        handoff = latest_handoff.get(int(row.id))
        if handoff is not None:
            outcomes[handoff.outcome_kind] = outcomes.get(handoff.outcome_kind, 0) + 1
            latencies.append(
                max(0, int((handoff.created_at - row.created_at).total_seconds() * 1000))
            )
        attempted = _safe_mapping(row.diagnosis_json).get("attempted_queries", [])
        if isinstance(attempted, list):
            tool_steps.append(len(attempted))
    completed_usage = [row for row in usage_rows if row.status == "completed"]
    confirmed = sum(row.verdict == "confirmed" for row in reviews)
    return {
        "bounded": True,
        "window": {"since": start.isoformat(), "until": end.isoformat()},
        "incident_count": len(incidents),
        "diagnosis_outcomes": outcomes,
        "escalation_count": sum(row.status == "escalated" for row in incidents),
        "auto_recovery_verified": sum(row.status == "verified" for row in recoveries),
        "verification_mismatch_count": sum(row.error_code == "verification_mismatch" for row in recoveries),
        "average_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
        "average_tool_steps": round(sum(tool_steps) / len(tool_steps), 3) if tool_steps else 0.0,
        "token_usage": {
            "model_calls": len(completed_usage),
            "prompt_tokens": sum(int(row.prompt_tokens or 0) for row in completed_usage),
            "completion_tokens": sum(int(row.completion_tokens or 0) for row in completed_usage),
            "total_tokens": sum(int(row.total_tokens or 0) for row in completed_usage),
            "reserved_tokens": sum(int(row.reserved_tokens) for row in usage_rows),
        },
        "codex_hypothesis_accuracy": {
            "reviewed": len(reviews),
            "confirmed": confirmed,
            "rate": (round(confirmed / len(reviews), 4) if reviews else None),
        },
    }


def build_confirmed_incident_fixture(session_factory, *, incident_id: int, case_id: str) -> dict[str, Any]:
    """Build a bounded redacted regression seed; never writes repository files."""

    with session_factory() as session:
        incident = session.get(RuntimeIncident, int(incident_id))
        review = session.scalar(
            select(RuntimeAgentDiagnosisReview)
            .where(
                RuntimeAgentDiagnosisReview.runtime_incident_id == int(incident_id),
                RuntimeAgentDiagnosisReview.verdict == "confirmed",
            )
            .order_by(RuntimeAgentDiagnosisReview.id.desc())
        )
        if incident is None or review is None:
            raise ValueError("confirmed review is required")
        if review.fixture_case_id != case_id:
            raise ValueError("confirmed fixture case ID does not match review")
        summary = _safe_mapping(incident.redacted_summary)
        diagnosis = _safe_mapping(incident.diagnosis_json)
        evidence_refs = json.loads(incident.evidence_refs_json or "[]")
        if not isinstance(evidence_refs, list):
            evidence_refs = []
        attempted = diagnosis.get("attempted_queries", [])
        if not isinstance(attempted, list):
            attempted = []
        playbook = diagnosis.get("recommended_playbook_name")
        playbook = playbook if isinstance(playbook, str) else None
        confidence = diagnosis.get("confidence")
        confidence = confidence if confidence in {"low", "medium", "high"} else "low"
        classification = diagnosis.get("classification")
        classification = classification if isinstance(classification, str) else incident.incident_type
        decision = evaluate_shadow_playbook_nomination(
            incident={
                "id": int(incident.id),
                "incident_type": incident.incident_type,
                "source_kind": incident.source_kind,
                "generation": int(incident.generation),
                "redacted_summary": summary,
            },
            nominated_playbook=playbook,
            enabled_playbooks=frozenset({playbook}) if playbook else frozenset(),
            evidence_references=tuple(str(item) for item in evidence_refs[:32]),
        )
    fixture = {
        "schema_version": 1,
        "case_id": str(case_id)[:128],
        "redacted": True,
        "incident": {
            "incident_type": incident.incident_type,
            "source_kind": incident.source_kind,
            "severity": incident.severity,
            "redacted_summary": summary,
        },
        "expectation": {
            "classification": classification,
            "required_tools": attempted[:4],
            "forbidden_tools": ["select_strategy", "resolve_context"],
            "max_confidence": confidence,
            "max_tool_steps": max(1, len(attempted)),
            "max_estimated_tokens": 32768,
            "allow_playbook": playbook is not None,
            "allowed_playbooks": ([playbook] if playbook else []),
            "shadow_playbook_name": playbook,
            "shadow_policy_accepted": decision.accepted,
        },
        "reviewed_output": {
            "classification": classification,
            "selected_tools": attempted[:4],
            "confidence": confidence,
            "missing_evidence": diagnosis.get("missing_evidence", []),
            "recommended_playbook_name": playbook,
            "auto_handle_eligible": bool(diagnosis.get("auto_handle_eligible", False)),
            "estimated_tokens": 0,
        },
    }
    encoded = json.dumps(fixture, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 16_384:
        raise ValueError("confirmed fixture exceeds byte budget")
    assert_runtime_agent_fixture_redacted(fixture)
    return fixture
