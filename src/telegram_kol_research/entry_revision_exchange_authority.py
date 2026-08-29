"""Generation-fenced cross-process authority for entry exchange writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Literal
import uuid

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from telegram_kol_research.deployment_entry_freeze import (
    deployment_entry_admission_frozen,
)
from telegram_kol_research.models import TradingSetting
from telegram_kol_research.trading_settings import (
    TRADING_SETTINGS_KEY,
    TradingSettings,
    trading_settings_from_payload,
)


ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY = "entry_revision_exchange_authority"
_SCHEMA_VERSION = 2
_MAX_LEASE = timedelta(minutes=10)
_PROCESS_START_FALLBACK = time.monotonic_ns()
_OWNER_KINDS = frozenset(
    {
        "entry_revision_worker",
        "new_entry_worker",
    }
)
_IDLE_KEYS = frozenset(
    {"schema_version", "state", "generation", "released_at"}
)
_HELD_KEYS = frozenset(
    {
        "schema_version",
        "state",
        "generation",
        "owner_kind",
        "action_id",
        "owner_pid",
        "owner_start_ticks",
        "token_sha256",
        "plan_sha256",
        "evidence_sha256",
        "acquired_at",
        "deadline_at",
        "write_boundary_reached",
    }
)
_BLOCKED_KEYS = frozenset(
    {
        "schema_version",
        "state",
        "generation",
        "prior_owner_kind",
        "action_id",
        "token_sha256",
        "blocked_at",
        "reason_code",
        "write_boundary_reached",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(frozen=True, slots=True)
class EntryRevisionAuthorityProcessIdentity:
    pid: int
    start_ticks: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.pid, bool)
            or int(self.pid) <= 1
            or isinstance(self.start_ticks, bool)
            or int(self.start_ticks) <= 0
        ):
            raise ValueError("authority process identity is invalid")
        object.__setattr__(self, "pid", int(self.pid))
        object.__setattr__(self, "start_ticks", int(self.start_ticks))


@dataclass(frozen=True, slots=True)
class EntryRevisionExchangeAuthorityAcquisition:
    acquired: bool
    token: str | None = None
    generation: int | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class EntryRevisionExchangeAuthorityRelease:
    released: bool
    generation: int | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class EntryRevisionExchangeAuthoritySeed:
    seeded: bool
    generation: int | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class EntryRevisionExchangeWriteBoundary:
    marked: bool
    generation: int | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class EntryRevisionExchangeAuthorityBlock:
    blocked: bool
    generation: int | None = None
    reason_code: str | None = None


def is_canonical_idle_entry_revision_exchange_authority(
    value_json: str,
) -> bool:
    """Return whether a stored document is the exact parseable idle schema."""

    document = _authority_document(value_json)
    return document is not None and document["state"] == "idle"


def seed_entry_revision_exchange_authority(
    session_factory,
    *,
    seeded_at: datetime,
    initial_generation: int = 0,
) -> EntryRevisionExchangeAuthoritySeed:
    """Insert the only accepted initial idle row when it is exactly absent."""

    observed_at = _timestamp(seeded_at)
    generation = _generation(initial_generation)
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = _authority_row(session)
            if row is not None:
                session.rollback()
                return EntryRevisionExchangeAuthoritySeed(
                    seeded=False,
                    reason_code="entry_revision_exchange_authority_already_exists",
                )
            session.add(
                TradingSetting(
                    key=ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
                    value_json=_canonical_json(
                        _idle_document(
                            generation=generation,
                            released_at=observed_at,
                        )
                    ),
                    updated_at=observed_at,
                )
            )
            session.commit()
            return EntryRevisionExchangeAuthoritySeed(
                seeded=True,
                generation=generation,
            )
    except SQLAlchemyError:
        return EntryRevisionExchangeAuthoritySeed(
            seeded=False,
            reason_code="entry_revision_exchange_authority_unavailable",
        )


def acquire_entry_revision_exchange_authority(
    session_factory,
    *,
    owner_kind: Literal[
        "entry_revision_worker",
        "new_entry_worker",
    ],
    owner_id: str,
    acquired_at: datetime,
    expected_generation: int | None = None,
    action_id: str | None = None,
    owner_identity: EntryRevisionAuthorityProcessIdentity | None = None,
    deadline_at: datetime | None = None,
    authority_token: str | None = None,
    plan_sha256: str | None = None,
    evidence_sha256: str | None = None,
) -> EntryRevisionExchangeAuthorityAcquisition:
    """Acquire exact idle generation; absence and expiry both fail closed."""

    clean_owner_kind = _owner_kind(owner_kind)
    clean_owner_id = _bounded_text(owner_id, field_name="owner_id", maximum=128)
    clean_action_id = _bounded_text(
        action_id if action_id is not None else clean_owner_id,
        field_name="action_id",
        maximum=128,
    )
    observed_at = _timestamp(acquired_at)
    deadline = _timestamp(deadline_at or (observed_at + _MAX_LEASE))
    if deadline <= observed_at or deadline - observed_at > _MAX_LEASE:
        raise ValueError("authority deadline is invalid")
    expected = (
        None if expected_generation is None else _generation(expected_generation)
    )
    identity = owner_identity or _current_process_identity()
    raw_token = _authority_token(authority_token or uuid.uuid4().hex)
    token_sha256 = _token_sha256(raw_token)
    plan_hash = _optional_sha256(
        plan_sha256,
        fallback=f"plan:{clean_owner_kind}:{clean_owner_id}",
    )
    evidence_hash = _optional_sha256(
        evidence_sha256,
        fallback=f"evidence:{clean_owner_kind}:{clean_owner_id}",
    )
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if clean_owner_kind == "new_entry_worker":
                settings_reason = _new_entry_quiescence_reason(session)
                if settings_reason is not None:
                    session.rollback()
                    return EntryRevisionExchangeAuthorityAcquisition(
                        acquired=False,
                        reason_code=settings_reason,
                    )

            row = _authority_row(session)
            if row is None:
                session.rollback()
                return EntryRevisionExchangeAuthorityAcquisition(
                    acquired=False,
                    reason_code="entry_revision_exchange_authority_missing",
                )
            document = _authority_document(row.value_json)
            if document is None:
                session.rollback()
                return EntryRevisionExchangeAuthorityAcquisition(
                    acquired=False,
                    reason_code="entry_revision_exchange_authority_invalid",
                )
            if document["state"] == "blocked":
                session.rollback()
                return EntryRevisionExchangeAuthorityAcquisition(
                    acquired=False,
                    generation=int(document["generation"]),
                    reason_code="entry_revision_exchange_authority_blocked",
                )
            if document["state"] == "held":
                deadline_value = _parsed_timestamp(document["deadline_at"])
                assert deadline_value is not None
                if deadline_value <= observed_at:
                    row.value_json = _canonical_json(
                        _blocked_document(
                            document,
                            blocked_at=observed_at,
                            reason_code="authority_lease_expired",
                        )
                    )
                    row.updated_at = observed_at
                    session.commit()
                    return EntryRevisionExchangeAuthorityAcquisition(
                        acquired=False,
                        generation=int(document["generation"]),
                        reason_code=(
                            "entry_revision_exchange_authority_expired_blocked"
                        ),
                    )
                session.rollback()
                return EntryRevisionExchangeAuthorityAcquisition(
                    acquired=False,
                    generation=int(document["generation"]),
                    reason_code="entry_revision_exchange_authority_busy",
                )

            current_generation = int(document["generation"])
            if expected is not None and expected != current_generation:
                session.rollback()
                return EntryRevisionExchangeAuthorityAcquisition(
                    acquired=False,
                    generation=current_generation,
                    reason_code=(
                        "entry_revision_exchange_authority_generation_mismatch"
                    ),
                )
            generation = current_generation + 1
            held = {
                "acquired_at": observed_at.isoformat(),
                "action_id": clean_action_id,
                "deadline_at": deadline.isoformat(),
                "evidence_sha256": evidence_hash,
                "generation": generation,
                "owner_kind": clean_owner_kind,
                "owner_pid": identity.pid,
                "owner_start_ticks": identity.start_ticks,
                "plan_sha256": plan_hash,
                "schema_version": _SCHEMA_VERSION,
                "state": "held",
                "token_sha256": token_sha256,
                "write_boundary_reached": False,
            }
            row.value_json = _canonical_json(held)
            row.updated_at = observed_at
            session.commit()
            return EntryRevisionExchangeAuthorityAcquisition(
                acquired=True,
                token=raw_token,
                generation=generation,
            )
    except SQLAlchemyError:
        return EntryRevisionExchangeAuthorityAcquisition(
            acquired=False,
            reason_code="entry_revision_exchange_authority_unavailable",
        )


def mark_entry_revision_exchange_write_boundary(
    session_factory,
    *,
    token: str,
    owner_kind: str,
    expected_generation: int,
    marked_at: datetime,
) -> EntryRevisionExchangeWriteBoundary:
    """Persist the point after which automatic recovery is prohibited."""

    clean_token = _authority_token(token)
    clean_owner_kind = _owner_kind(owner_kind)
    generation = _generation(expected_generation)
    observed_at = _timestamp(marked_at)
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = _authority_row(session)
            document, reason = _exact_held_document(
                row,
                token=clean_token,
                owner_kind=clean_owner_kind,
                expected_generation=generation,
            )
            if reason is not None:
                session.rollback()
                return EntryRevisionExchangeWriteBoundary(
                    marked=False,
                    reason_code=reason,
                )
            assert row is not None and document is not None
            deadline_value = _parsed_timestamp(document["deadline_at"])
            assert deadline_value is not None
            if deadline_value <= observed_at:
                row.value_json = _canonical_json(
                    _blocked_document(
                        document,
                        blocked_at=observed_at,
                        reason_code="authority_lease_expired",
                    )
                )
                row.updated_at = observed_at
                session.commit()
                return EntryRevisionExchangeWriteBoundary(
                    marked=False,
                    generation=generation,
                    reason_code=(
                        "entry_revision_exchange_authority_expired_blocked"
                    ),
                )
            updated = dict(document)
            updated["write_boundary_reached"] = True
            row.value_json = _canonical_json(updated)
            row.updated_at = observed_at
            session.commit()
            return EntryRevisionExchangeWriteBoundary(
                marked=True,
                generation=generation,
            )
    except SQLAlchemyError:
        return EntryRevisionExchangeWriteBoundary(
            marked=False,
            reason_code="entry_revision_exchange_authority_unavailable",
        )


def block_entry_revision_exchange_authority(
    session_factory,
    *,
    token: str,
    owner_kind: str,
    expected_generation: int,
    reason_code: str,
    blocked_at: datetime,
) -> EntryRevisionExchangeAuthorityBlock:
    """Convert an exact held claim into a permanent fail-closed block."""

    clean_token = _authority_token(token)
    token_hash = _token_sha256(clean_token)
    clean_owner_kind = _owner_kind(owner_kind)
    generation = _generation(expected_generation)
    clean_reason = _reason_code(reason_code)
    observed_at = _timestamp(blocked_at)
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = _authority_row(session)
            if row is None:
                session.rollback()
                return EntryRevisionExchangeAuthorityBlock(
                    blocked=False,
                    reason_code="entry_revision_exchange_authority_missing",
                )
            document = _authority_document(row.value_json)
            if document is None:
                session.rollback()
                return EntryRevisionExchangeAuthorityBlock(
                    blocked=False,
                    reason_code="entry_revision_exchange_authority_invalid",
                )
            if document["state"] == "blocked":
                if (
                    int(document["generation"]) == generation
                    and document["prior_owner_kind"] == clean_owner_kind
                    and document["token_sha256"] == token_hash
                ):
                    session.rollback()
                    return EntryRevisionExchangeAuthorityBlock(
                        blocked=True,
                        generation=generation,
                    )
                session.rollback()
                return EntryRevisionExchangeAuthorityBlock(
                    blocked=False,
                    reason_code=(
                        "entry_revision_exchange_authority_owner_mismatch"
                    ),
                )
            exact, reason = _exact_held_document(
                row,
                token=clean_token,
                owner_kind=clean_owner_kind,
                expected_generation=generation,
            )
            if reason is not None:
                session.rollback()
                return EntryRevisionExchangeAuthorityBlock(
                    blocked=False,
                    reason_code=reason,
                )
            assert exact is not None
            row.value_json = _canonical_json(
                _blocked_document(
                    exact,
                    blocked_at=observed_at,
                    reason_code=clean_reason,
                )
            )
            row.updated_at = observed_at
            session.commit()
            return EntryRevisionExchangeAuthorityBlock(
                blocked=True,
                generation=generation,
            )
    except SQLAlchemyError:
        return EntryRevisionExchangeAuthorityBlock(
            blocked=False,
            reason_code="entry_revision_exchange_authority_unavailable",
        )


def release_entry_revision_exchange_authority(
    session_factory,
    *,
    token: str,
    owner_kind: str,
    released_at: datetime,
    expected_generation: int | None = None,
) -> EntryRevisionExchangeAuthorityRelease:
    """Release only the exact held generation; every mismatch stays held."""

    clean_token = _authority_token(token)
    clean_owner_kind = _owner_kind(owner_kind)
    observed_at = _timestamp(released_at)
    expected = (
        None if expected_generation is None else _generation(expected_generation)
    )
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = _authority_row(session)
            if row is None:
                session.rollback()
                return EntryRevisionExchangeAuthorityRelease(
                    released=False,
                    reason_code="entry_revision_exchange_authority_missing",
                )
            document = _authority_document(row.value_json)
            if document is None or document["state"] != "held":
                session.rollback()
                return EntryRevisionExchangeAuthorityRelease(
                    released=False,
                    reason_code="entry_revision_exchange_authority_invalid",
                )
            generation = int(document["generation"])
            if expected is not None and expected != generation:
                session.rollback()
                return EntryRevisionExchangeAuthorityRelease(
                    released=False,
                    generation=generation,
                    reason_code=(
                        "entry_revision_exchange_authority_generation_mismatch"
                    ),
                )
            if (
                document["token_sha256"] != _token_sha256(clean_token)
                or document["owner_kind"] != clean_owner_kind
            ):
                session.rollback()
                return EntryRevisionExchangeAuthorityRelease(
                    released=False,
                    generation=generation,
                    reason_code=(
                        "entry_revision_exchange_authority_owner_mismatch"
                    ),
                )
            deadline_value = _parsed_timestamp(document["deadline_at"])
            assert deadline_value is not None
            if deadline_value <= observed_at:
                row.value_json = _canonical_json(
                    _blocked_document(
                        document,
                        blocked_at=observed_at,
                        reason_code="authority_lease_expired",
                    )
                )
                row.updated_at = observed_at
                session.commit()
                return EntryRevisionExchangeAuthorityRelease(
                    released=False,
                    generation=generation,
                    reason_code=(
                        "entry_revision_exchange_authority_expired_blocked"
                    ),
                )
            row.value_json = _canonical_json(
                _idle_document(
                    generation=generation,
                    released_at=observed_at,
                )
            )
            row.updated_at = observed_at
            session.commit()
            return EntryRevisionExchangeAuthorityRelease(
                released=True,
                generation=generation,
            )
    except SQLAlchemyError:
        return EntryRevisionExchangeAuthorityRelease(
            released=False,
            reason_code="entry_revision_exchange_authority_unavailable",
        )


def _new_entry_quiescence_reason(session) -> str | None:
    settings, reason = _settings_in_session(
        session,
        invalid_reason="new_entry_worker_settings_invalid",
    )
    if reason is not None:
        return reason
    assert settings is not None
    if deployment_entry_admission_frozen():
        return "deployment_entry_frozen"
    return None


def _settings_in_session(
    session,
    *,
    invalid_reason: str,
) -> tuple[TradingSettings | None, str | None]:
    row = (
        session.query(TradingSetting)
        .filter(TradingSetting.key == TRADING_SETTINGS_KEY)
        .one_or_none()
    )
    if row is None:
        settings = TradingSettings()
    else:
        try:
            payload = json.loads(row.value_json)
            if not isinstance(payload, dict):
                raise ValueError("settings payload is not an object")
            settings = trading_settings_from_payload(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None, invalid_reason
    return settings, None


def _authority_document(value_json: str) -> dict[str, object] | None:
    try:
        document = json.loads(value_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("schema_version") != _SCHEMA_VERSION:
        return None
    state = document.get("state")
    if state == "idle":
        if frozenset(document) != _IDLE_KEYS:
            return None
        if _valid_generation(document.get("generation")) is None:
            return None
        if _parsed_timestamp(document.get("released_at")) is None:
            return None
        return document
    if state == "held":
        if frozenset(document) != _HELD_KEYS:
            return None
        if document.get("owner_kind") not in _OWNER_KINDS:
            return None
        if not _valid_text(document.get("action_id"), maximum=128):
            return None
        if _valid_positive_int(document.get("owner_pid"), minimum=2) is None:
            return None
        if _valid_positive_int(document.get("owner_start_ticks")) is None:
            return None
        if _valid_generation(document.get("generation")) is None:
            return None
        if any(
            not _valid_sha256(document.get(key))
            for key in ("token_sha256", "plan_sha256", "evidence_sha256")
        ):
            return None
        acquired_at = _parsed_timestamp(document.get("acquired_at"))
        deadline_at = _parsed_timestamp(document.get("deadline_at"))
        if (
            acquired_at is None
            or deadline_at is None
            or deadline_at <= acquired_at
            or deadline_at - acquired_at > _MAX_LEASE
            or type(document.get("write_boundary_reached")) is not bool
        ):
            return None
        return document
    if state == "blocked":
        if frozenset(document) != _BLOCKED_KEYS:
            return None
        if document.get("prior_owner_kind") not in _OWNER_KINDS:
            return None
        if not _valid_text(document.get("action_id"), maximum=128):
            return None
        if _valid_generation(document.get("generation")) is None:
            return None
        if not _valid_sha256(document.get("token_sha256")):
            return None
        if _parsed_timestamp(document.get("blocked_at")) is None:
            return None
        if not _valid_reason(document.get("reason_code")):
            return None
        if type(document.get("write_boundary_reached")) is not bool:
            return None
        return document
    return None


def _authority_row(session):
    return (
        session.query(TradingSetting)
        .filter(
            TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
        )
        .one_or_none()
    )


def _exact_held_document(
    row,
    *,
    token: str,
    owner_kind: str,
    expected_generation: int,
) -> tuple[dict[str, object] | None, str | None]:
    if row is None:
        return None, "entry_revision_exchange_authority_missing"
    document = _authority_document(row.value_json)
    if document is None or document["state"] != "held":
        return None, "entry_revision_exchange_authority_invalid"
    if int(document["generation"]) != expected_generation:
        return None, "entry_revision_exchange_authority_generation_mismatch"
    if (
        document["owner_kind"] != owner_kind
        or document["token_sha256"] != _token_sha256(token)
    ):
        return None, "entry_revision_exchange_authority_owner_mismatch"
    return document, None


def _idle_document(
    *,
    generation: int,
    released_at: datetime,
) -> dict[str, object]:
    return {
        "generation": generation,
        "released_at": released_at.isoformat(),
        "schema_version": _SCHEMA_VERSION,
        "state": "idle",
    }


def _blocked_document(
    held: dict[str, object],
    *,
    blocked_at: datetime,
    reason_code: str,
) -> dict[str, object]:
    return {
        "action_id": held["action_id"],
        "blocked_at": blocked_at.isoformat(),
        "generation": held["generation"],
        "prior_owner_kind": held["owner_kind"],
        "reason_code": _reason_code(reason_code),
        "schema_version": _SCHEMA_VERSION,
        "state": "blocked",
        "token_sha256": held["token_sha256"],
        "write_boundary_reached": held["write_boundary_reached"],
    }


def _current_process_identity() -> EntryRevisionAuthorityProcessIdentity:
    start_ticks = _PROCESS_START_FALLBACK
    try:
        raw = Path("/proc/self/stat").read_text(encoding="ascii")
        suffix = raw[raw.rindex(")") + 2 :].split()
        parsed = int(suffix[19])
        if parsed > 0:
            start_ticks = parsed
    except (OSError, UnicodeError, ValueError, IndexError):
        pass
    return EntryRevisionAuthorityProcessIdentity(
        pid=os.getpid(),
        start_ticks=start_ticks,
    )


def _owner_kind(value: object) -> str:
    clean = str(value or "").strip()
    if clean not in _OWNER_KINDS:
        raise ValueError("unknown entry revision exchange authority owner")
    return clean


def _authority_token(value: object) -> str:
    return _bounded_text(value, field_name="token", maximum=128, minimum=8)


def _token_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional_sha256(value: object, *, fallback: str) -> str:
    if value is None:
        return hashlib.sha256(fallback.encode("utf-8")).hexdigest()
    clean = str(value).strip()
    if not _SHA256.fullmatch(clean):
        raise ValueError("authority fingerprint is invalid")
    return clean


def _generation(value: object) -> int:
    parsed = _valid_generation(value)
    if parsed is None:
        raise ValueError("authority generation is invalid")
    return parsed


def _valid_generation(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _valid_positive_int(value: object, *, minimum: int = 1) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _bounded_text(
    value: object,
    *,
    field_name: str,
    maximum: int,
    minimum: int = 1,
) -> str:
    clean = str(value or "").strip()
    if len(clean) < minimum or len(clean) > maximum:
        raise ValueError(f"{field_name} is invalid")
    return clean


def _valid_text(value: object, *, maximum: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= maximum


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _reason_code(value: object) -> str:
    clean = str(value or "").strip()
    if not _REASON_CODE.fullmatch(clean):
        raise ValueError("authority reason code is invalid")
    return clean


def _valid_reason(value: object) -> bool:
    return isinstance(value, str) and _REASON_CODE.fullmatch(value) is not None


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("authority timestamp is invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parsed_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
