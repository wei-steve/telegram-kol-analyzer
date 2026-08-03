"""Additive compare-and-set storage for shadow invariant observations."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json

from sqlalchemy import case, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import RuntimeIncidentObservation
from telegram_kol_research.runtime_incident_scanner import InvariantObservation


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _record_with_session(session, *, observation: InvariantObservation, observed_at: datetime) -> dict[str, str]:
    now = _naive(observed_at)
    fingerprint = sha256(
        f"{observation.rule_id}|{observation.rule_version}|{observation.object_kind}|{observation.object_id}".encode()
    ).hexdigest()
    evidence_json = json.dumps(observation.evidence_references, separators=(",", ":"))
    summary_json = json.dumps(dict(observation.summary), sort_keys=True, separators=(",", ":"))
    identity = {
        "rule_id": observation.rule_id,
        "rule_version": observation.rule_version,
        "object_kind": observation.object_kind,
        "object_id": observation.object_id,
    }
    initial_state = (
        "observing" if observation.outcome != "normal" else "resolved_without_incident"
    )
    inserted = session.execute(
            sqlite_insert(RuntimeIncidentObservation)
            .values(
                **identity,
                fingerprint=fingerprint,
                severity=observation.severity,
                state=initial_state,
                consecutive_count=1 if observation.outcome == "abnormal" else 0,
                first_observed_at=now,
                last_observed_at=now,
                evidence_refs_json=evidence_json,
                evidence_fingerprint=observation.evidence_fingerprint,
                summary_json=summary_json,
                recovered_at=now if observation.outcome == "normal" else None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=("rule_id", "rule_version", "object_kind", "object_id")
            )
    ).rowcount
    if not inserted:
        values = {
                "last_observed_at": now,
                "updated_at": now,
                "severity": observation.severity,
                "evidence_refs_json": evidence_json,
                "evidence_fingerprint": observation.evidence_fingerprint,
                "summary_json": summary_json,
            }
        if observation.outcome == "abnormal":
            values.update(
                    consecutive_count=case(
                        (
                            RuntimeIncidentObservation.evidence_fingerprint
                            == observation.evidence_fingerprint,
                            RuntimeIncidentObservation.consecutive_count + 1,
                        ),
                        else_=1,
                    ),
                    state=case(
                        (
                            (
                                RuntimeIncidentObservation.evidence_fingerprint
                                == observation.evidence_fingerprint
                            )
                            & (RuntimeIncidentObservation.consecutive_count >= 1),
                            "shadow_confirmed",
                        ),
                        else_="observing",
                    ),
                    recovered_at=None,
                )
        elif observation.outcome == "normal":
            values.update(
                    consecutive_count=0,
                    state=case(
                        (RuntimeIncidentObservation.confirmed_incident_id.is_not(None), "resolved"),
                        else_="resolved_without_incident",
                    ),
                    recovered_at=now,
                )
        session.execute(
            update(RuntimeIncidentObservation).where(
                    RuntimeIncidentObservation.rule_id == identity["rule_id"],
                    RuntimeIncidentObservation.rule_version == identity["rule_version"],
                    RuntimeIncidentObservation.object_kind == identity["object_kind"],
                    RuntimeIncidentObservation.object_id == identity["object_id"],
                    RuntimeIncidentObservation.last_observed_at <= now,
            ).values(**values)
        )
    return identity


def record_observations(
    session_factory: sessionmaker,
    *,
    observations: tuple[InvariantObservation, ...],
    observed_at: datetime,
) -> tuple[RuntimeIncidentObservation, ...]:
    """Persist a complete bounded cycle under one SQLite transaction."""
    if len(observations) > 100:
        raise ValueError("scanner observation cycle is unbounded")
    with session_factory() as session:
        identities = [
            _record_with_session(
                session, observation=observation, observed_at=observed_at
            )
            for observation in observations
        ]
        session.commit()
        rows = []
        for identity in identities:
            row = session.query(RuntimeIncidentObservation).filter_by(**identity).one()
            session.refresh(row)
            session.expunge(row)
            rows.append(row)
        return tuple(rows)


def record_observation(
    session_factory: sessionmaker,
    *,
    observation: InvariantObservation,
    observed_at: datetime,
) -> RuntimeIncidentObservation:
    return record_observations(
        session_factory,
        observations=(observation,),
        observed_at=observed_at,
    )[0]
