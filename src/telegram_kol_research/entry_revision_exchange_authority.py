"""Generation-fenced cross-process authority for entry exchange writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
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
from telegram_kol_research.entry_revision_exchange_authority_contract import (
    ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    is_canonical_idle_entry_revision_exchange_authority,
)
from telegram_kol_research.models import TradingSetting
from telegram_kol_research.trading_settings import (
    TRADING_SETTINGS_KEY,
    TradingSettings,
    trading_settings_from_payload,
)

logger = logging.getLogger(__name__)


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
_LEGACY_BLOCKED_KEYS = frozenset(
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
# 6-pre-6: a blocked document has to say *who* was holding it, or nobody can
# tell a crashed owner from a live one -- which is exactly how 2026-09-09's
# deadlock outlived its owner. The legacy shape stays legal on purpose: a
# document written by the previous release is still sitting in production
# while this one deploys, and rejecting it would turn a recoverable block into
# an unparseable one, which is strictly worse than the bug being fixed.
_BLOCKED_KEYS = _LEGACY_BLOCKED_KEYS | {"owner_pid", "owner_start_ticks"}
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
                # 6-pre-6. A block used to be terminal until a human cleared it,
                # so one crashed owner refused every entry after it. Before
                # refusing, ask whether the owner still exists: if it provably
                # does not, the block has nothing left to protect. The reset
                # runs in its own transaction, so this one is released first
                # and the acquisition is retried by the caller's next tick
                # rather than being smuggled into a transaction that has
                # already read a stale document.
                session.rollback()
                blocked_generation = int(document["generation"])
                reset = reset_blocked_entry_revision_authority(
                    session_factory,
                    now=observed_at,
                )
                return EntryRevisionExchangeAuthorityAcquisition(
                    acquired=False,
                    generation=(
                        reset.generation if reset.reset else blocked_generation
                    ),
                    reason_code=(
                        "entry_revision_exchange_authority_blocked_reset"
                        if reset.reset
                        else "entry_revision_exchange_authority_blocked"
                    ),
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
        keys = frozenset(document)
        if keys not in (_BLOCKED_KEYS, _LEGACY_BLOCKED_KEYS):
            return None
        if keys == _BLOCKED_KEYS:
            if _valid_positive_int(document.get("owner_pid"), minimum=2) is None:
                return None
            if _valid_positive_int(document.get("owner_start_ticks")) is None:
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
        # Carried over from the held document so a later reader can ask whether
        # that process is still alive. Without them a block is indistinguishable
        # from a deadlock and there is nothing safe to do but wait for a human.
        "owner_pid": held["owner_pid"],
        "owner_start_ticks": held["owner_start_ticks"],
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


# --------------------------------------------------------------------------
# 6-pre-6: a block must not be able to outlive the process that caused it.
# --------------------------------------------------------------------------

#: How long a blocked document may sit before it is reset even though nothing
#: could be proved about its owner. Matched to the lease itself: once the lease
#: could not have been valid anyway, holding the whole entry path hostage buys
#: nothing.
BLOCKED_RESET_GRACE = _MAX_LEASE

RESET_AUDIT_ACTION = "entry_revision_authority_reset"
BLOCKED_RESET_INCIDENT_TYPE = "entry_revision_authority_blocked_reset"


@dataclass(frozen=True, slots=True)
class EntryRevisionAuthorityReset:
    reset: bool
    generation: int | None = None
    reason_code: str | None = None
    prior_owner_pid: int | None = None


def entry_revision_authority_owner_is_alive(
    *,
    owner_pid: int | None,
    owner_start_ticks: int | None,
) -> bool | None:
    """``True``/``False`` if it can be proved, ``None`` when it cannot.

    Three states, not two, and the third one matters: "I could not tell" must
    never be collapsed into "it is dead", or a reset would race a living owner
    that is mid-write. Only an answer this function is sure of authorizes an
    immediate reset; everything else waits for the grace period instead.

    Identity is the pair, never the pid alone -- pids are reused, and a reused
    pid reads as "alive" while the real owner is long gone.
    """

    if owner_pid is None or owner_start_ticks is None:
        return None
    try:
        raw = Path(f"/proc/{int(owner_pid)}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError):
        return None
    try:
        suffix = raw[raw.rindex(")") + 2 :].split()
        observed = int(suffix[19])
    except (ValueError, IndexError):
        return None
    if observed <= 0:
        return None
    return observed == int(owner_start_ticks)


def reset_blocked_entry_revision_authority(
    session_factory,
    *,
    now: datetime,
    liveness_probe=None,
    audit_recorder=None,
    incident_reporter=None,
) -> EntryRevisionAuthorityReset:
    """Return a blocked authority to idle when nothing can still be holding it.

    Two independent grounds, and both are needed. The owner being provably gone
    is the fast one -- it fires within a tick of a crash, which is the case that
    lost a real entry on 2026-09-09. The grace period is the slow one, and it
    exists for every case the first cannot decide: a legacy document with no
    owner recorded, an unreadable ``/proc``, a pid from another machine.

    The idle document's key set is fixed and validated exactly, so the reason
    this happened cannot be written into it. It goes to the audit row and the
    alert instead -- which is why both are attempted before the caller is told
    the reset succeeded.
    """

    observed_at = _timestamp(now)
    probe = liveness_probe or entry_revision_authority_owner_is_alive
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row = _authority_row(session)
        if row is None:
            session.rollback()
            return EntryRevisionAuthorityReset(
                reset=False,
                reason_code="entry_revision_exchange_authority_missing",
            )
        document = _authority_document(row.value_json)
        if document is None:
            session.rollback()
            return EntryRevisionAuthorityReset(
                reset=False,
                reason_code="entry_revision_exchange_authority_invalid",
            )
        if document["state"] != "blocked":
            session.rollback()
            return EntryRevisionAuthorityReset(
                reset=False,
                generation=int(document["generation"]),
                reason_code="entry_revision_exchange_authority_not_blocked",
            )

        owner_pid = document.get("owner_pid")
        owner_start_ticks = document.get("owner_start_ticks")
        alive = probe(
            owner_pid=owner_pid if owner_pid is None else int(owner_pid),
            owner_start_ticks=(
                owner_start_ticks
                if owner_start_ticks is None
                else int(owner_start_ticks)
            ),
        )
        if alive is True:
            session.rollback()
            return EntryRevisionAuthorityReset(
                reset=False,
                generation=int(document["generation"]),
                reason_code="entry_revision_exchange_authority_owner_alive",
                prior_owner_pid=int(owner_pid) if owner_pid is not None else None,
            )
        blocked_at = _parsed_timestamp(document["blocked_at"])
        assert blocked_at is not None
        elapsed = observed_at - blocked_at
        if alive is None and elapsed < BLOCKED_RESET_GRACE:
            session.rollback()
            return EntryRevisionAuthorityReset(
                reset=False,
                generation=int(document["generation"]),
                reason_code="entry_revision_exchange_authority_block_recent",
            )

        ground = "owner_process_gone" if alive is False else "block_grace_elapsed"
        next_generation = int(document["generation"]) + 1
        row.value_json = _canonical_json(
            _idle_document(generation=next_generation, released_at=observed_at)
        )
        row.updated_at = observed_at
        session.commit()

    detail = {
        "ground": ground,
        "prior_generation": int(document["generation"]),
        "generation": next_generation,
        "prior_owner_kind": str(document["prior_owner_kind"]),
        "prior_owner_pid": owner_pid,
        "prior_action_id": str(document["action_id"]),
        "blocked_reason_code": str(document["reason_code"]),
        "blocked_at": str(document["blocked_at"]),
        "write_boundary_reached": bool(document["write_boundary_reached"]),
        "blocked_for_seconds": int(elapsed.total_seconds()),
    }
    _record_reset_audit(session_factory, detail=detail, now=observed_at,
                        audit_recorder=audit_recorder)
    _report_reset_incident(session_factory, detail=detail, now=observed_at,
                           incident_reporter=incident_reporter)
    return EntryRevisionAuthorityReset(
        reset=True,
        generation=next_generation,
        reason_code=ground,
        prior_owner_pid=int(owner_pid) if owner_pid is not None else None,
    )


def _record_reset_audit(session_factory, *, detail, now, audit_recorder) -> None:
    """The only place the reason survives -- the idle document cannot hold it."""

    try:
        if audit_recorder is not None:
            audit_recorder(detail=detail, now=now)
            return
        from telegram_kol_research.execution_events import (
            ExecutionEventRecord,
            record_execution_event,
        )

        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                action=RESET_AUDIT_ACTION,
                status="skipped",
                reason=str(detail["ground"]),
                before={"state": "blocked", **{
                    key: detail[key] for key in (
                        "prior_generation", "prior_owner_kind", "prior_owner_pid",
                        "prior_action_id", "blocked_reason_code", "blocked_at",
                        "write_boundary_reached", "blocked_for_seconds",
                    )
                }},
                after={"state": "idle", "generation": detail["generation"]},
                created_at=now,
            ),
        )
    except Exception:  # pragma: no cover - the reset is already committed
        logger.warning("entry_revision_authority_reset_audit_failed", exc_info=True)


def _report_reset_incident(session_factory, *, detail, now, incident_reporter) -> None:
    """Nobody asked for this reset, so somebody has to be told it happened."""

    try:
        if incident_reporter is not None:
            incident_reporter(detail=detail, now=now)
            return
        from telegram_kol_research.runtime_incidents import record_runtime_incident

        summary = {
            "component": "entry_revision_exchange_authority",
            "reason_code": str(detail["ground"]),
            "impact": (
                f"authority_unblocked_after_{int(detail['blocked_for_seconds'])}s "
                f"prior_gen={detail['prior_generation']} "
                f"owner={detail['prior_owner_kind']}"
            ),
            "containment": "authority_reset_to_idle_entries_can_proceed",
        }
        record_runtime_incident(
            session_factory,
            source_kind="entry_revision_exchange_authority",
            source_record_id=str(detail["prior_generation"]),
            incident_type=BLOCKED_RESET_INCIDENT_TYPE,
            severity="high",
            fingerprint=hashlib.sha256(
                f"{BLOCKED_RESET_INCIDENT_TYPE}:{detail['prior_generation']}".encode()
            ).hexdigest(),
            redacted_summary=_canonical_json(summary),
            occurred_at=now,
            feature_policy_version="phase-6-pre-6-authority-lease-v1",
            prompt_version="none",
            tool_policy_version="no-exchange-write",
            evidence_refs_json=_canonical_json(
                [f"entry_revision_authority:{detail['prior_generation']}"]
            ),
        )
    except Exception:  # pragma: no cover - the reset is already committed
        logger.warning("entry_revision_authority_reset_alert_failed", exc_info=True)


TERMINAL_RELEASE_AUDIT_ACTION = "entry_revision_authority_terminal_release"


@dataclass(frozen=True, slots=True)
class EntryRevisionAuthorityTerminalRelease:
    released: bool
    generation: int | None = None
    reason_code: str | None = None


def release_entry_revision_authority_for_terminal_batch(
    session_factory,
    *,
    batch_id: int,
    generation: int,
    owner_kind: Literal["entry_revision_worker", "new_entry_worker"],
    now: datetime,
    audit_recorder=None,
) -> EntryRevisionAuthorityTerminalRelease:
    """Return a lease whose batch has finished, without needing its token.

    The ordinary release requires the token, which lives only in the memory of
    the call that took the lease. That is right for the normal path and wrong
    for the one that stranded a real entry on 2026-09-09: when a batch reaches
    a terminal state through a branch that returns early, the token is gone and
    the lease is held by nobody, for ten minutes, after which the next
    applicant turns the document to ``blocked``.

    So this is a deliberate hole in the token rule, and it is kept as small as
    the fact that justifies it. It refuses unless the batch is **actually** in
    a terminal state -- re-read from the database here, never taken from the
    caller's word -- and it matches the exact generation and owner kind, so it
    cannot return a lease that some later holder has since taken.

    It does not weaken the isolation the terminal branches rely on: the lease
    is still held for as long as the batch is running, including while an
    ambiguous write is unresolved. It is returned at the moment the batch stops
    being able to do anything with it.

    ``tests/test_entry_revision_authority_deadlock.py`` fails if any module
    other than the entry revision executor calls this.
    """

    from telegram_kol_research.models import StrategyRevisionBatch

    clean_owner_kind = _owner_kind(owner_kind)
    expected_generation = _generation(generation)
    observed_at = _timestamp(now)
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if batch is None:
            session.rollback()
            return EntryRevisionAuthorityTerminalRelease(
                released=False, reason_code="entry_revision_batch_missing"
            )
        batch_status = str(batch.status or "")
        batch_reason = str(batch.reason_code or "")
        if batch_status not in _TERMINAL_BATCH_STATES_FOR_RELEASE:
            session.rollback()
            return EntryRevisionAuthorityTerminalRelease(
                released=False, reason_code="entry_revision_batch_not_terminal"
            )
        row = _authority_row(session)
        if row is None:
            session.rollback()
            return EntryRevisionAuthorityTerminalRelease(
                released=False,
                reason_code="entry_revision_exchange_authority_missing",
            )
        document = _authority_document(row.value_json)
        if document is None:
            session.rollback()
            return EntryRevisionAuthorityTerminalRelease(
                released=False,
                reason_code="entry_revision_exchange_authority_invalid",
            )
        if document["state"] != "held":
            # Already idle, or already blocked and handled by the reset path.
            session.rollback()
            return EntryRevisionAuthorityTerminalRelease(
                released=False,
                generation=int(document["generation"]),
                reason_code="entry_revision_exchange_authority_not_held",
            )
        if (
            int(document["generation"]) != expected_generation
            or document["owner_kind"] != clean_owner_kind
        ):
            # Somebody else holds it now. Returning it here would be theft.
            session.rollback()
            return EntryRevisionAuthorityTerminalRelease(
                released=False,
                generation=int(document["generation"]),
                reason_code="entry_revision_exchange_authority_owner_mismatch",
            )
        next_generation = expected_generation + 1
        row.value_json = _canonical_json(
            _idle_document(generation=next_generation, released_at=observed_at)
        )
        row.updated_at = observed_at
        session.commit()

    _record_terminal_release_audit(
        session_factory,
        detail={
            "batch_id": int(batch_id),
            "prior_generation": expected_generation,
            "generation": next_generation,
            "owner_kind": clean_owner_kind,
            "batch_status": batch_status,
            "batch_reason_code": batch_reason,
            "write_boundary_reached": bool(document["write_boundary_reached"]),
        },
        now=observed_at,
        audit_recorder=audit_recorder,
    )
    return EntryRevisionAuthorityTerminalRelease(
        released=True,
        generation=next_generation,
        reason_code="entry_revision_batch_terminal",
    )


#: Kept here rather than imported from the executor so the guard cannot be
#: widened from the calling side without this module changing too.
_TERMINAL_BATCH_STATES_FOR_RELEASE = frozenset(
    {"succeeded", "recovery_required", "blocked", "resolved", "superseded"}
)


def _record_terminal_release_audit(
    session_factory, *, detail, now, audit_recorder
) -> None:
    try:
        if audit_recorder is not None:
            audit_recorder(detail=detail, now=now)
            return
        from telegram_kol_research.execution_events import (
            ExecutionEventRecord,
            record_execution_event,
        )

        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                action=TERMINAL_RELEASE_AUDIT_ACTION,
                status="skipped",
                reason=str(detail["batch_reason_code"] or detail["batch_status"]),
                before={
                    "state": "held",
                    "generation": detail["prior_generation"],
                    "owner_kind": detail["owner_kind"],
                    "batch_id": detail["batch_id"],
                    "batch_status": detail["batch_status"],
                    "write_boundary_reached": detail["write_boundary_reached"],
                },
                after={"state": "idle", "generation": detail["generation"]},
                created_at=now,
            ),
        )
    except Exception:  # pragma: no cover - the release is already committed
        logger.warning(
            "entry_revision_authority_terminal_release_audit_failed", exc_info=True
        )


def release_authority_for_finished_batches(
    session_factory,
    *,
    now: datetime,
    audit_recorder=None,
) -> EntryRevisionAuthorityTerminalRelease:
    """Sweep: return a lease still held on behalf of a batch that has finished.

    This is deliberately a separate step rather than a line inside
    ``execute_entry_revision``. The terminal branches there return while the
    write outcome is still ambiguous, and the isolation they provide -- no
    other batch may write while that is unresolved -- is the reason they exist;
    releasing inline would remove it. By the time this sweep runs on the next
    maintenance tick, the batch is durably terminal, the ambiguity is recorded
    on the batch itself, and the lease is protecting nothing.

    Seconds, not the twenty minutes the lease-expiry-then-block path would
    take, which for an entry is the difference between recovering and having
    lost it.
    """

    observed_at = _timestamp(now)
    with session_factory() as session:
        row = _authority_row(session)
        document = _authority_document(row.value_json) if row is not None else None
    if document is None or document["state"] != "held":
        return EntryRevisionAuthorityTerminalRelease(
            released=False,
            reason_code="entry_revision_exchange_authority_not_held",
        )
    action_id = str(document["action_id"])
    if not action_id.startswith("batch:"):
        # A ``signal:`` holder belongs to the new-entry path, which has no
        # batch row to prove terminality against. Left alone on purpose.
        return EntryRevisionAuthorityTerminalRelease(
            released=False,
            generation=int(document["generation"]),
            reason_code="entry_revision_authority_holder_not_a_batch",
        )
    try:
        batch_id = int(action_id.split(":", 1)[1])
    except (ValueError, IndexError):
        return EntryRevisionAuthorityTerminalRelease(
            released=False,
            generation=int(document["generation"]),
            reason_code="entry_revision_authority_holder_unparsable",
        )
    return release_entry_revision_authority_for_terminal_batch(
        session_factory,
        batch_id=batch_id,
        generation=int(document["generation"]),
        owner_kind=str(document["owner_kind"]),
        now=observed_at,
        audit_recorder=audit_recorder,
    )
