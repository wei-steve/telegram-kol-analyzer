"""Idempotent monitor incident routing and channel-failure-only fallback."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from telegram_kol_research.production_monitor_contract import (
    MONITOR_FALLBACK_REASONS,
    parse_monitor_projection,
)
from telegram_kol_research.production_monitor_state import (
    MONITOR_STATE_MAX_ACCEPTANCES,
    FallbackDeliveryState,
    IncidentAcceptanceState,
    ProductionMonitorState,
)


MONITOR_INTAKE_SLA = timedelta(minutes=10)
MONITOR_NOTIFICATION_SLA = timedelta(minutes=10)
MONITOR_FALLBACK_RETRY_DELAY = timedelta(minutes=5)
MONITOR_MAX_FALLBACK_BYTES = 512

_FALLBACK_COMPONENTS = frozenset(
    {"runtime_incident_intake", "runtime_incident_notification"}
)
_NOTIFICATION_STATUSES = frozenset(
    {"pending", "delivering", "delivered", "failed", "exhausted"}
)
_AGENT_STATUSES = frozenset(
    {
        "pending",
        "claimed",
        "diagnosed",
        "retry_pending",
        "escalated",
        "resolved",
        "closed",
        "timed_out",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BRIDGE_READINESS_FIELDS = frozenset(
    {
        "available",
        "schema_version",
        "contract",
        "capture_selector",
        "notification_channel",
        "notification_selector",
        "agent_channel",
        "agent_selector",
        "notification_watermark",
    }
)


class MonitorIntakeError(RuntimeError):
    """One closed failure of the loopback monitor intake or its recheck."""

    def __init__(self, code: str) -> None:
        if code not in {"schema_refused", "transport_unavailable"}:
            raise ValueError("monitor intake error code is not closed")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MonitorAcceptance:
    submission_id: str
    accepted_at: datetime
    notification_status: str
    notification_claimed_at: datetime | None
    notification_claim_expires_at: datetime | None
    notification_failed_at: datetime | None
    agent_status: str


@dataclass(frozen=True, slots=True)
class MonitorRoutingOutcome:
    accepted: bool
    fallback_reason: str | None
    fallback_status: str | None
    state: ProductionMonitorState


def _validate_loopback_url(
    url: str, *, expected_path: str, label: str
) -> None:
    parsed = urlsplit(str(url))
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.port != 8000
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != expected_path
    ):
        raise ValueError(f"{label} URL must be exact loopback HTTP")


def _validate_monitor_token(token: str) -> None:
    if not isinstance(token, str) or re.fullmatch(
        r"[A-Za-z0-9_-]{32,128}", token
    ) is None:
        raise ValueError("monitor incident token is invalid")


def request_monitor_acceptance(
    *,
    url: str,
    token: str,
    projection: Mapping[str, object],
    now: datetime,
    request: Callable[..., Any] | None = None,
) -> MonitorAcceptance:
    """POST only to the fixed loopback bridge and parse its closed response."""

    _validate_loopback_url(
        url,
        expected_path="/api/runtime-incidents/monitor-capture",
        label="monitor incident",
    )
    _validate_monitor_token(token)
    canonical = parse_monitor_projection(projection)

    def default_request(**kwargs):
        import httpx

        with httpx.Client(timeout=10.0, trust_env=False) as client:
            return client.post(**kwargs)

    try:
        response = (request or default_request)(
            url=url,
            headers={"x-monitor-capture-token": token},
            json=canonical,
        )
    except Exception:
        raise MonitorIntakeError("transport_unavailable") from None
    status_code = getattr(response, "status_code", None)
    if status_code == 422:
        raise MonitorIntakeError("schema_refused")
    if status_code != 200:
        raise MonitorIntakeError("transport_unavailable")
    content = getattr(response, "content", b"")
    if not isinstance(content, (bytes, bytearray)) or len(content) > 4096:
        raise MonitorIntakeError("schema_refused")
    try:
        payload = response.json()
    except Exception:
        raise MonitorIntakeError("schema_refused") from None
    return parse_monitor_acceptance(payload, now=now)


def request_monitor_bridge_readiness(
    *,
    url: str,
    token: str,
    request: Callable[..., Any] | None = None,
) -> Mapping[str, object]:
    """Read and strictly validate the side-effect-free v2 bridge contract."""

    _validate_loopback_url(
        url,
        expected_path="/api/runtime-incidents/monitor-v2-bridge-readiness",
        label="monitor bridge readiness",
    )
    _validate_monitor_token(token)

    def default_request(**kwargs):
        if kwargs.pop("trust_env", None) is not False:
            raise RuntimeError("monitor bridge transport policy unavailable")
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            return client.get(**kwargs)

    try:
        response = (request or default_request)(
            url=url,
            headers={"x-monitor-capture-token": token},
            timeout=5.0,
            trust_env=False,
        )
    except Exception:
        raise MonitorIntakeError("transport_unavailable") from None
    status_code = getattr(response, "status_code", None)
    if type(status_code) is not int or status_code != 200:
        raise MonitorIntakeError("transport_unavailable")
    content = getattr(response, "content", b"")
    if not isinstance(content, (bytes, bytearray)) or len(content) > 4096:
        raise MonitorIntakeError("schema_refused")
    try:
        payload = response.json()
        if not isinstance(payload, Mapping) or frozenset(payload) != (
            _BRIDGE_READINESS_FIELDS
        ):
            raise ValueError("invalid response")
        if type(payload["available"]) is not bool:
            raise ValueError("invalid availability")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != 2
        ):
            raise ValueError("invalid schema")
        if (
            type(payload["contract"]) is not str
            or payload["contract"] != "production_monitor_v2"
        ):
            raise ValueError("invalid contract")
        if (
            type(payload["capture_selector"]) is not str
            or payload["capture_selector"] not in {"included", "excluded"}
        ):
            raise ValueError("invalid capture selector")
        if (
            type(payload["notification_channel"]) is not str
            or payload["notification_channel"] not in {"enabled", "disabled"}
        ):
            raise ValueError("invalid notification channel")
        if (
            type(payload["notification_selector"]) is not str
            or payload["notification_selector"]
            not in {"included", "excluded", "legacy_all_refused"}
        ):
            raise ValueError("invalid notification selector")
        if (
            type(payload["agent_channel"]) is not str
            or payload["agent_channel"] not in {"enabled", "disabled"}
        ):
            raise ValueError("invalid agent channel")
        if (
            type(payload["agent_selector"]) is not str
            or payload["agent_selector"]
            not in {"included", "excluded", "legacy_all_refused"}
        ):
            raise ValueError("invalid agent selector")
        if (
            type(payload["notification_watermark"]) is not str
            or payload["notification_watermark"]
            not in {"configured", "absent", "invalid_refused"}
        ):
            raise ValueError("invalid watermark")
        expected_available = (
            payload["capture_selector"] == "included"
            and payload["notification_channel"] == "enabled"
            and payload["notification_selector"] == "included"
            and payload["notification_watermark"] == "configured"
        )
        if payload["available"] is not expected_available:
            raise ValueError("inconsistent availability")
    except (KeyError, TypeError, ValueError):
        raise MonitorIntakeError("schema_refused") from None
    return dict(payload)


def parse_monitor_acceptance(
    payload: Mapping[str, object], *, now: datetime
) -> MonitorAcceptance:
    """Parse the closed v2 loopback response into channel health facts."""

    fields = frozenset(
        {
            "accepted",
            "submission_id",
            "accepted_at",
            "notification_status",
            "notification_claimed_at",
            "notification_claim_expires_at",
            "notification_failed_at",
            "agent_status",
        }
    )
    try:
        if (
            not isinstance(payload, Mapping)
            or frozenset(payload) != fields
            or payload["accepted"] is not True
        ):
            raise ValueError("invalid response")
        submission_id = str(payload["submission_id"])
        if _SHA256.fullmatch(submission_id) is None:
            raise ValueError("invalid submission identity")
        value = MonitorAcceptance(
            submission_id=submission_id,
            accepted_at=datetime.fromisoformat(str(payload["accepted_at"])),
            notification_status=str(payload["notification_status"]),
            notification_claimed_at=(
                None
                if payload["notification_claimed_at"] is None
                else datetime.fromisoformat(
                    str(payload["notification_claimed_at"])
                )
            ),
            notification_claim_expires_at=(
                None
                if payload["notification_claim_expires_at"] is None
                else datetime.fromisoformat(
                    str(payload["notification_claim_expires_at"])
                )
            ),
            notification_failed_at=(
                None
                if payload["notification_failed_at"] is None
                else datetime.fromisoformat(
                    str(payload["notification_failed_at"])
                )
            ),
            agent_status=str(payload["agent_status"]),
        )
        return _validated_acceptance(
            value,
            submission_id=submission_id,
            now=_timestamp(now, field="now"),
        )
    except (KeyError, TypeError, ValueError):
        raise MonitorIntakeError("schema_refused") from None


def request_monitor_fallback(
    *,
    url: str,
    token: str,
    message: str,
    request: Callable[..., Any] | None = None,
) -> None:
    """Deliver a fixed fallback through the authenticated localhost proxy."""

    _validate_loopback_url(
        url,
        expected_path="/api/runtime-incidents/monitor-fallback",
        label="monitor fallback",
    )
    _validate_monitor_token(token)
    payload = _payload_from_fixed_fallback(message)

    def default_request(**kwargs):
        import httpx

        with httpx.Client(timeout=10.0, trust_env=False) as client:
            return client.post(**kwargs)

    try:
        response = (request or default_request)(
            url=url,
            headers={"x-monitor-capture-token": token},
            json=payload,
        )
    except Exception:
        raise RuntimeError("monitor fallback delivery unavailable") from None
    content = getattr(response, "content", b"")
    if (
        getattr(response, "status_code", None) != 200
        or not isinstance(content, (bytes, bytearray))
        or len(content) > 512
    ):
        raise RuntimeError("monitor fallback delivery unavailable")
    try:
        response_payload = response.json()
    except Exception:
        raise RuntimeError("monitor fallback delivery unavailable") from None
    if response_payload != {"delivered": True}:
        raise RuntimeError("monitor fallback delivery unavailable")


def parse_fixed_fallback_payload(payload: Mapping[str, object]) -> dict[str, str]:
    """Validate the closed localhost fallback envelope and canonicalize it."""

    fields = (
        "delivery_id",
        "reason",
        "component",
        "observed_at",
        "deadline_at",
        "rechecked_at",
    )
    if not isinstance(payload, Mapping) or frozenset(payload) != frozenset(fields):
        raise ValueError("fixed fallback payload is invalid")
    if any(not isinstance(payload[field], str) for field in fields):
        raise ValueError("fixed fallback payload is invalid")
    if _SHA256.fullmatch(payload["delivery_id"]) is None:
        raise ValueError("fixed fallback payload is invalid")
    try:
        observed = datetime.fromisoformat(payload["observed_at"])
        deadline = datetime.fromisoformat(payload["deadline_at"])
        rechecked = datetime.fromisoformat(payload["rechecked_at"])
        message = format_fixed_fallback(
            reason=payload["reason"],
            component=payload["component"],
            observed_at=observed,
            deadline_at=deadline,
            rechecked_at=rechecked,
        )
    except (TypeError, ValueError):
        raise ValueError("fixed fallback payload is invalid") from None
    canonical_lines = message.splitlines()
    canonical = {
        field: canonical_lines[index].split("=", 1)[1]
        for index, field in enumerate(fields[1:], start=1)
    }
    expected_delivery_id = hashlib.sha256(
        "\0".join(
            canonical[field]
            for field in (
                "reason",
                "component",
                "observed_at",
                "deadline_at",
            )
        ).encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(payload["delivery_id"], expected_delivery_id):
        raise ValueError("fixed fallback payload is invalid")
    return {"delivery_id": expected_delivery_id, **canonical}


def format_fixed_fallback(
    *,
    reason: str,
    component: str,
    observed_at: datetime,
    deadline_at: datetime,
    rechecked_at: datetime,
) -> str:
    """Format only the closed channel-failure envelope, never incident data."""

    if reason not in MONITOR_FALLBACK_REASONS or component not in _FALLBACK_COMPONENTS:
        raise ValueError("fallback labels must be closed")
    observed = _timestamp(observed_at, field="observed_at")
    deadline = _timestamp(deadline_at, field="deadline_at")
    rechecked = _timestamp(rechecked_at, field="rechecked_at")
    if not observed <= deadline <= rechecked:
        raise ValueError("fallback timestamps are inconsistent")
    message = (
        "Production monitor channel failure\n"
        f"reason={reason}\n"
        f"component={component}\n"
        f"observed_at={observed.isoformat()}\n"
        f"deadline_at={deadline.isoformat()}\n"
        f"rechecked_at={rechecked.isoformat()}"
    )
    if len(message.encode("utf-8")) > MONITOR_MAX_FALLBACK_BYTES:
        raise ValueError("fallback message exceeds safe bound")
    return message


def _payload_from_fixed_fallback(message: str) -> dict[str, str]:
    if not isinstance(message, str) or len(message.encode("utf-8")) > MONITOR_MAX_FALLBACK_BYTES:
        raise ValueError("fixed fallback message is invalid")
    lines = message.splitlines()
    fields = (
        "reason",
        "component",
        "observed_at",
        "deadline_at",
        "rechecked_at",
    )
    if len(lines) != 6 or lines[0] != "Production monitor channel failure":
        raise ValueError("fixed fallback message is invalid")
    payload: dict[str, str] = {}
    for line, field in zip(lines[1:], fields, strict=True):
        prefix = f"{field}="
        if not line.startswith(prefix):
            raise ValueError("fixed fallback message is invalid")
        payload[field] = line[len(prefix) :]
    payload["delivery_id"] = hashlib.sha256(
        "\0".join(
            payload[field]
            for field in (
                "reason",
                "component",
                "observed_at",
                "deadline_at",
            )
        ).encode("utf-8")
    ).hexdigest()
    canonical = parse_fixed_fallback_payload(payload)
    if format_fixed_fallback(
        reason=canonical["reason"],
        component=canonical["component"],
        observed_at=datetime.fromisoformat(canonical["observed_at"]),
        deadline_at=datetime.fromisoformat(canonical["deadline_at"]),
        rechecked_at=datetime.fromisoformat(canonical["rechecked_at"]),
    ) != message:
        raise ValueError("fixed fallback message is invalid")
    return canonical


def route_monitor_incident(
    *,
    projection: Mapping[str, object],
    previous_state: ProductionMonitorState,
    now: datetime,
    submit: Callable[[Mapping[str, object]], MonitorAcceptance],
    recheck: Callable[[Mapping[str, object]], MonitorAcceptance],
    deliver_fallback: Callable[[str], object],
) -> MonitorRoutingOutcome:
    """Route one confirmed projection without coupling channel health to exit."""

    canonical = parse_monitor_projection(projection)
    operation_now = _timestamp(now, field="now")
    checked_at = _timestamp(
        datetime.fromisoformat(canonical["checked_at"]), field="checked_at"
    )
    if (
        canonical["execution_status"] != "COMPLETED"
        or canonical["observed_health"] != "UNHEALTHY"
        or not canonical["reason_codes"]
        or canonical["adapter_failures"]
        or canonical["fallback_reason"] is not None
    ):
        raise ValueError("only globally complete confirmed incidents may route")
    if operation_now < checked_at:
        raise ValueError("routing time precedes the monitor observation")
    if not isinstance(previous_state, ProductionMonitorState):
        raise ValueError("monitor routing state is invalid")
    if not callable(submit) or not callable(recheck) or not callable(deliver_fallback):
        raise ValueError("monitor routing dependency is invalid")

    submission_id = canonical["submission_id"]
    prior_acceptance = next(
        (
            item
            for item in previous_state.incident_acceptances
            if item.submission_id == submission_id
            or item.candidate_fingerprint == canonical["anomaly_fingerprint"]
        ),
        None,
    )
    acceptance: MonitorAcceptance | None = None
    initial_failed = False
    try:
        acceptance = _validated_acceptance(
            (recheck if prior_acceptance is not None else submit)(canonical),
            submission_id=submission_id,
            now=operation_now,
        )
    except (MonitorIntakeError, TimeoutError, ConnectionError):
        initial_failed = True

    recheck_failed = False
    if acceptance is None and initial_failed and prior_acceptance is None:
        try:
            acceptance = _validated_acceptance(
                recheck(canonical),
                submission_id=submission_id,
                now=operation_now,
            )
        except (MonitorIntakeError, TimeoutError, ConnectionError):
            recheck_failed = True

    if acceptance is None and prior_acceptance is not None:
        return MonitorRoutingOutcome(
            accepted=True,
            fallback_reason=None,
            fallback_status=None,
            state=previous_state,
        )

    intake_fingerprint = _channel_fingerprint(
        reason="incident_intake_unavailable",
        component="runtime_incident_intake",
        episode_fingerprint=canonical["anomaly_fingerprint"],
    )
    intake_tracker = (
        previous_state.fallback
        if previous_state.fallback is not None
        and previous_state.fallback.fingerprint == intake_fingerprint
        else None
    )
    if acceptance is None and initial_failed and intake_tracker is None:
        tracking_state = replace(
            previous_state,
            fallback=FallbackDeliveryState(
                fingerprint=intake_fingerprint,
                status="PENDING",
                attempts=0,
                last_attempt_at=None,
                next_attempt_at=operation_now + MONITOR_INTAKE_SLA,
            ),
        )
        return MonitorRoutingOutcome(
            accepted=False,
            fallback_reason=None,
            fallback_status=None,
            state=tracking_state,
        )
    intake_deadline = (
        operation_now + MONITOR_INTAKE_SLA
        if intake_tracker is None or intake_tracker.next_attempt_at is None
        else intake_tracker.next_attempt_at
    )
    if acceptance is None and initial_failed and operation_now >= intake_deadline:
        if recheck_failed:
            return _attempt_fallback(
                previous_state=previous_state,
                accepted=False,
                reason="incident_intake_unavailable",
                component="runtime_incident_intake",
                observed_at=intake_deadline - MONITOR_INTAKE_SLA,
                deadline_at=intake_deadline,
                now=operation_now,
                deliver=deliver_fallback,
                fingerprint=intake_fingerprint,
            )
    if acceptance is None:
        return MonitorRoutingOutcome(
            accepted=prior_acceptance is not None,
            fallback_reason=(
                "incident_intake_unavailable"
                if intake_tracker is not None and intake_tracker.attempts > 0
                else None
            ),
            fallback_status=(
                intake_tracker.status
                if intake_tracker is not None and intake_tracker.attempts > 0
                else None
            ),
            state=previous_state,
        )

    acceptance_base_state = (
        replace(previous_state, fallback=None)
        if previous_state.fallback is not None
        and previous_state.fallback.fingerprint == intake_fingerprint
        else previous_state
    )
    accepted_state = _with_acceptance(
        acceptance_base_state,
        projection=canonical,
        acceptance=acceptance,
    )
    # Agent diagnosis is deliberately not consulted here. Its normal queueing,
    # retries, or timeout cannot make the monitor send a duplicate alert.
    notification_fingerprint = _channel_fingerprint(
        reason="deterministic_notification_unavailable",
        component="runtime_incident_notification",
        episode_fingerprint=canonical["anomaly_fingerprint"],
    )
    if acceptance.notification_status == "delivered":
        if (
            accepted_state.fallback is not None
            and accepted_state.fallback.fingerprint == notification_fingerprint
        ):
            accepted_state = replace(accepted_state, fallback=None)
        accepted_state = _mark_episode_terminal(
            accepted_state, canonical["anomaly_fingerprint"]
        )
        return MonitorRoutingOutcome(
            accepted=True,
            fallback_reason=None,
            fallback_status=None,
            state=accepted_state,
        )
    local_acceptance = next(
        item
        for item in accepted_state.incident_acceptances
        if item.candidate_fingerprint == canonical["anomaly_fingerprint"]
    )
    notification_observed_at = local_acceptance.accepted_at
    notification_deadline = local_acceptance.accepted_at + MONITOR_NOTIFICATION_SLA
    if operation_now < notification_deadline:
        return MonitorRoutingOutcome(
            accepted=True,
            fallback_reason=None,
            fallback_status=None,
            state=accepted_state,
        )
    if (
        acceptance.notification_status == "delivering"
        and acceptance.notification_claim_expires_at is not None
        and operation_now < acceptance.notification_claim_expires_at
    ):
        return MonitorRoutingOutcome(
            accepted=True,
            fallback_reason=None,
            fallback_status=None,
            state=accepted_state,
        )
    try:
        confirmed = _validated_acceptance(
            recheck(canonical),
            submission_id=submission_id,
            now=operation_now,
        )
    except (MonitorIntakeError, TimeoutError, ConnectionError):
        # A failed recheck cannot prove that the deterministic notification
        # remains failed, so it stays pending instead of guessing.
        return MonitorRoutingOutcome(
            accepted=True,
            fallback_reason=None,
            fallback_status=None,
            state=accepted_state,
        )
    accepted_state = _with_acceptance(
        accepted_state,
        projection=canonical,
        acceptance=confirmed,
    )
    if confirmed.notification_status == "delivered" or (
        confirmed.notification_status == "delivering"
        and confirmed.notification_claim_expires_at is not None
        and operation_now < confirmed.notification_claim_expires_at
    ):
        if (
            confirmed.notification_status == "delivered"
            and accepted_state.fallback is not None
            and accepted_state.fallback.fingerprint == notification_fingerprint
        ):
            accepted_state = replace(accepted_state, fallback=None)
        if confirmed.notification_status == "delivered":
            accepted_state = _mark_episode_terminal(
                accepted_state, canonical["anomaly_fingerprint"]
            )
        return MonitorRoutingOutcome(
            accepted=True,
            fallback_reason=None,
            fallback_status=None,
            state=accepted_state,
        )
    fallback_outcome = _attempt_fallback(
        previous_state=accepted_state,
        accepted=True,
        reason="deterministic_notification_unavailable",
        component="runtime_incident_notification",
        observed_at=notification_observed_at,
        deadline_at=notification_deadline,
        now=operation_now,
        deliver=deliver_fallback,
        fingerprint=notification_fingerprint,
    )
    if fallback_outcome.fallback_status == "DELIVERED":
        return replace(
            fallback_outcome,
            state=_mark_episode_terminal(
                fallback_outcome.state, canonical["anomaly_fingerprint"]
            ),
        )
    return fallback_outcome


def route_monitor_incident_persisted(
    *,
    state_store,
    projection: Mapping[str, object],
    now: datetime,
    submit: Callable[[Mapping[str, object]], MonitorAcceptance],
    recheck: Callable[[Mapping[str, object]], MonitorAcceptance],
    deliver_fallback: Callable[[str], object],
) -> MonitorRoutingOutcome:
    """Persist the complete routing transition under the sentinel state lease."""

    from telegram_kol_research.production_monitor_state import (
        ProductionMonitorStateStore,
    )

    if not isinstance(state_store, ProductionMonitorStateStore):
        raise ValueError("monitor routing store is invalid")
    with state_store.single_flight() as lease:
        if not lease.acquired:
            raise RuntimeError("monitor routing state is busy")
        previous_state = lease.load()
        outcome = route_monitor_incident(
            projection=projection,
            previous_state=previous_state,
            now=now,
            submit=submit,
            recheck=recheck,
            deliver_fallback=deliver_fallback,
        )
        persisted = lease.save(outcome.state)
    return replace(outcome, state=persisted)


def recheck_due_monitor_notifications_persisted(
    *,
    state_store,
    now: datetime,
    recheck: Callable[[Mapping[str, object]], MonitorAcceptance],
    deliver_fallback: Callable[[str], object],
) -> tuple[MonitorRoutingOutcome, ...]:
    """Recheck accepted aggregate episodes even after their candidate set changes."""

    from telegram_kol_research.production_monitor_state import (
        ProductionMonitorStateStore,
    )

    if not isinstance(state_store, ProductionMonitorStateStore):
        raise ValueError("monitor routing store is invalid")
    operation_now = _timestamp(now, field="now")
    with state_store.single_flight() as lease:
        if not lease.acquired:
            raise RuntimeError("monitor routing state is busy")
        snapshot = lease.load()
    due: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for acceptance in snapshot.incident_acceptances:
        if (
            acceptance.projection_json is None
            or acceptance.routing_terminal
            or acceptance.accepted_at + MONITOR_NOTIFICATION_SLA > operation_now
            or acceptance.submission_id in seen
        ):
            continue
        projection = parse_monitor_projection(
            json.loads(acceptance.projection_json)
        )
        seen.add(acceptance.submission_id)
        due.append(projection)
    outcomes = []
    for projection in due:
        outcomes.append(
            route_monitor_incident_persisted(
                state_store=state_store,
                projection=projection,
                now=operation_now,
                submit=recheck,
                recheck=recheck,
                deliver_fallback=deliver_fallback,
            )
        )
    return tuple(outcomes)


def _with_acceptance(
    state: ProductionMonitorState,
    *,
    projection: Mapping[str, object],
    acceptance: MonitorAcceptance,
) -> ProductionMonitorState:
    anomaly_fingerprint = str(projection["anomaly_fingerprint"])
    existing = {
        item.submission_id: item
        for item in state.incident_acceptances
        if not (
            item.projection_json is not None
            and item.routing_terminal
            and item.candidate_fingerprint != anomaly_fingerprint
        )
    }
    existing_fingerprints = {
        item.candidate_fingerprint for item in existing.values()
    }
    active_candidates = tuple(
        sorted(
            (
                item
                for item in state.candidates
                if item.lifecycle == "CONFIRMED"
                and item.reason_code in projection["reason_codes"]
            ),
            key=lambda item: item.fingerprint,
        )
    )
    if anomaly_fingerprint not in existing_fingerprints:
        existing[acceptance.submission_id] = IncidentAcceptanceState(
            candidate_fingerprint=anomaly_fingerprint,
            submission_id=acceptance.submission_id,
            accepted_at=acceptance.accepted_at,
            projection_json=json.dumps(
                dict(projection),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
            member_fingerprints=tuple(
                item.fingerprint for item in active_candidates
            ),
            routing_terminal=(acceptance.notification_status == "delivered"),
        )
        existing_fingerprints.add(anomaly_fingerprint)
    elif acceptance.notification_status == "delivered":
        for submission_id, item in tuple(existing.items()):
            if item.candidate_fingerprint == anomaly_fingerprint:
                existing[submission_id] = replace(item, routing_terminal=True)
    if active_candidates:
        for candidate in active_candidates:
            if candidate.fingerprint not in existing_fingerprints:
                candidate_submission_id = hashlib.sha256(
                    "\0".join(
                        (
                            "accepted-monitor-candidate",
                            acceptance.submission_id,
                            candidate.fingerprint,
                        )
                    ).encode("utf-8")
                ).hexdigest()
                existing[candidate_submission_id] = IncidentAcceptanceState(
                    candidate_fingerprint=candidate.fingerprint,
                    submission_id=candidate_submission_id,
                    accepted_at=acceptance.accepted_at,
                )
                existing_fingerprints.add(candidate.fingerprint)
    ordered = tuple(sorted(existing.values(), key=lambda item: item.submission_id))
    if len(ordered) > MONITOR_STATE_MAX_ACCEPTANCES:
        pending_aggregates = tuple(
            item
            for item in ordered
            if item.projection_json is not None and not item.routing_terminal
        )
        if len(pending_aggregates) > MONITOR_STATE_MAX_ACCEPTANCES:
            raise ValueError("pending monitor acceptances exceed safe bound")
        pending_ids = {item.submission_id for item in pending_aggregates}
        remaining = sorted(
            (item for item in ordered if item.submission_id not in pending_ids),
            key=lambda item: (item.accepted_at, item.submission_id),
            reverse=True,
        )
        ordered = tuple(
            sorted(
                (
                    *pending_aggregates,
                    *remaining[
                        : MONITOR_STATE_MAX_ACCEPTANCES
                        - len(pending_aggregates)
                    ],
                ),
                key=lambda item: item.submission_id,
            )
        )
    return replace(state, incident_acceptances=ordered)


def _mark_episode_terminal(
    state: ProductionMonitorState, anomaly_fingerprint: str
) -> ProductionMonitorState:
    return replace(
        state,
        incident_acceptances=tuple(
            replace(item, routing_terminal=True)
            if item.projection_json is not None
            and item.candidate_fingerprint == anomaly_fingerprint
            else item
            for item in state.incident_acceptances
        ),
    )


def _attempt_fallback(
    *,
    previous_state: ProductionMonitorState,
    accepted: bool,
    reason: str,
    component: str,
    observed_at: datetime,
    deadline_at: datetime,
    now: datetime,
    deliver: Callable[[str], object],
    fingerprint: str,
) -> MonitorRoutingOutcome:
    message = format_fixed_fallback(
        reason=reason,
        component=component,
        observed_at=observed_at,
        deadline_at=deadline_at,
        rechecked_at=now,
    )
    previous = previous_state.fallback
    if previous is not None and previous.fingerprint == fingerprint:
        if previous.status == "DELIVERED":
            return MonitorRoutingOutcome(
                accepted=accepted,
                fallback_reason=reason,
                fallback_status="DELIVERED",
                state=previous_state,
            )
        if previous.next_attempt_at is not None and now < previous.next_attempt_at:
            return MonitorRoutingOutcome(
                accepted=accepted,
                fallback_reason=reason,
                fallback_status="PENDING",
                state=previous_state,
            )
        attempts = previous.attempts + 1
    else:
        attempts = 1
    try:
        deliver(message)
    except Exception:
        fallback = FallbackDeliveryState(
            fingerprint=fingerprint,
            status="PENDING",
            attempts=attempts,
            last_attempt_at=now,
            next_attempt_at=now + MONITOR_FALLBACK_RETRY_DELAY,
        )
        status = "PENDING"
    else:
        fallback = FallbackDeliveryState(
            fingerprint=fingerprint,
            status="DELIVERED",
            attempts=attempts,
            last_attempt_at=now,
            next_attempt_at=None,
        )
        status = "DELIVERED"
    return MonitorRoutingOutcome(
        accepted=accepted,
        fallback_reason=reason,
        fallback_status=status,
        state=replace(previous_state, fallback=fallback),
    )


def _channel_fingerprint(
    *, reason: str, component: str, episode_fingerprint: str
) -> str:
    """Identify one channel outage within a stable anomaly episode."""

    return hashlib.sha256(
        "\0".join((reason, component, episode_fingerprint)).encode("utf-8")
    ).hexdigest()


def _validated_acceptance(
    value: object,
    *,
    submission_id: str,
    now: datetime,
) -> MonitorAcceptance:
    if not isinstance(value, MonitorAcceptance):
        raise MonitorIntakeError("schema_refused")
    accepted_at = _timestamp(value.accepted_at, field="accepted_at")
    claimed_at = (
        None
        if value.notification_claimed_at is None
        else _timestamp(
            value.notification_claimed_at, field="notification_claimed_at"
        )
    )
    claim_expires_at = (
        None
        if value.notification_claim_expires_at is None
        else _timestamp(
            value.notification_claim_expires_at,
            field="notification_claim_expires_at",
        )
    )
    failed_at = (
        None
        if value.notification_failed_at is None
        else _timestamp(value.notification_failed_at, field="notification_failed_at")
    )
    if (
        value.submission_id != submission_id
        or accepted_at > now
        or value.notification_status not in _NOTIFICATION_STATUSES
        or value.agent_status not in _AGENT_STATUSES
        or (
            value.notification_status == "delivering"
            and (
                claimed_at is None
                or claim_expires_at is None
                or claimed_at < accepted_at
                or claimed_at > now
                or claim_expires_at <= claimed_at
            )
        )
        or (
            value.notification_status != "delivering"
            and claim_expires_at is not None
        )
        or (
            value.notification_status in {"failed", "exhausted"}
            and (failed_at is None or failed_at < accepted_at or failed_at > now)
        )
        or (
            value.notification_status not in {"failed", "exhausted"}
            and failed_at is not None
        )
    ):
        raise MonitorIntakeError("schema_refused")
    return MonitorAcceptance(
        submission_id=submission_id,
        accepted_at=accepted_at,
        notification_status=value.notification_status,
        notification_claimed_at=claimed_at,
        notification_claim_expires_at=claim_expires_at,
        notification_failed_at=failed_at,
        agent_status=value.agent_status,
    )


def _timestamp(value: datetime, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
