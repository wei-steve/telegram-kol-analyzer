"""Sealed, bounded exchange evidence for the production monitor.

This store is intentionally independent from the display-only live-position
cache.  It accepts only complete, sanitized success generations, retains a
bounded history, and records failed attempts without refreshing last success.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
from types import MappingProxyType
from typing import Any


SNAPSHOT_MANIFEST_SCHEMA_VERSION = 1
SNAPSHOT_COLLECTION_NAMES = (
    "positions",
    "open_orders",
    "pending_trigger_orders",
)
SNAPSHOT_FAILURE_CODES = frozenset(
    {
        "credential_invalid",
        "exchange_rate_limited",
        "exchange_timeout",
        "exchange_unavailable",
        "refresh_overlap",
        "snapshot_account_scope_mismatch",
        "snapshot_clock_invalid",
        "snapshot_duplicate_identity",
        "snapshot_page_limit_ambiguous",
        "snapshot_pagination_incomplete",
        "snapshot_read_unavailable",
        "snapshot_schema_invalid",
        "snapshot_size_exceeded",
        "wall_clock_timeout",
    }
)

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "uid_scope_hash",
        "generations",
        "latest_attempt",
        "last_success_generation",
    }
)
_GENERATION_FIELDS = frozenset(
    {
        "generation",
        "outcome",
        "request_started_at",
        "request_completed_at",
        "uid_scope_hash",
        "collections",
        "failure_code",
        "content_sha256",
    }
)
_COLLECTION_FIELDS = frozenset(
    {
        "name",
        "available",
        "schema_valid",
        "complete",
        "page_count",
        "row_count",
        "rows",
        "reason_code",
    }
)
_UID_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_REASON_CODE_PATTERN = re.compile(r"[a-z0-9_]{1,64}")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:authorization|api[_-]?key|passphrase|secret|token|signature)", re.I
)
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_ROWS_PER_COLLECTION = 1_000
_MAX_ROW_BYTES = 65_536
_MAX_ROW_DEPTH = 12
_MAX_ROW_NODES = 20_000
_MAX_STRING_BYTES = 32_768
_MAX_KEY_LENGTH = 256


@dataclass(frozen=True, slots=True)
class SnapshotCollectionEvidence:
    name: str
    available: bool
    schema_valid: bool
    complete: bool
    page_count: int
    row_count: int
    rows: tuple[Mapping[str, Any], ...]
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotGeneration:
    generation: int
    outcome: str
    request_started_at: datetime
    request_completed_at: datetime
    uid_scope_hash: str
    collections: tuple[SnapshotCollectionEvidence, ...]
    failure_code: str | None = None
    content_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    schema_version: int = SNAPSHOT_MANIFEST_SCHEMA_VERSION
    uid_scope_hash: str | None = None
    generations: tuple[SnapshotGeneration, ...] = ()
    latest_attempt: SnapshotGeneration | None = None
    last_success: SnapshotGeneration | None = None


@dataclass(frozen=True, slots=True)
class SnapshotRefreshLease:
    acquired: bool
    manifest: SnapshotManifest


class ProductionMonitorSnapshotStore:
    """Persist at most three complete generations through an atomic manifest."""

    def __init__(
        self,
        path: str | Path,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._now_factory = now_factory or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()

    @property
    def refresh_lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.refresh.lock")

    @property
    def manifest_lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.manifest.lock")

    def try_refresh_lease(
        self,
        *,
        uid_scope_hash: str,
        observed_at: datetime,
    ) -> "_RefreshLease":
        """Acquire a refresh baseline or atomically seal ``refresh_overlap``."""

        return _RefreshLease(
            self,
            uid_scope_hash=_uid_scope_hash(uid_scope_hash),
            observed_at=_aware_utc(observed_at, field="observed_at"),
        )

    def load(self) -> SnapshotManifest:
        with self._lock:
            return self._load()

    def seal_success(self, generation: SnapshotGeneration) -> SnapshotManifest:
        with self._lock:
            with self._manifest_write_lease():
                manifest = self._load()
                sealed = _validate_and_seal_generation(
                    generation,
                    now=self._now(),
                    expected_outcome="SUCCESS",
                )
                _validate_next_generation(manifest, sealed)
                retained = (*manifest.generations, sealed)[-3:]
                updated = SnapshotManifest(
                    uid_scope_hash=sealed.uid_scope_hash,
                    generations=retained,
                    latest_attempt=sealed,
                    last_success=sealed,
                )
                self._persist(updated)
                return updated

    def seal_failure(self, generation: SnapshotGeneration) -> SnapshotManifest:
        with self._lock:
            with self._manifest_write_lease():
                return self._seal_failure_locked(generation)

    def record_refresh_overlap(
        self,
        *,
        uid_scope_hash: str,
        observed_at: datetime,
    ) -> SnapshotManifest:
        """Allocate and seal one overlap attempt without a racy load/allocate gap."""

        observed = _aware_utc(observed_at, field="observed_at")
        with self._lock:
            with self._manifest_write_lease():
                manifest = self._load()
                return self._record_refresh_overlap_locked(
                    manifest,
                    uid_scope_hash=_uid_scope_hash(uid_scope_hash),
                    observed_at=observed,
                )

    def _manifest_write_lease(self) -> "_FileLease":
        return _FileLease(self.manifest_lock_path, blocking=True)

    def _seal_failure_locked(
        self,
        generation: SnapshotGeneration,
        *,
        manifest: SnapshotManifest | None = None,
    ) -> SnapshotManifest:
        current = self._load() if manifest is None else manifest
        sealed = _validate_and_seal_generation(
            generation,
            now=self._now(),
            expected_outcome="FAILURE",
        )
        _validate_next_generation(current, sealed)
        updated = SnapshotManifest(
            uid_scope_hash=sealed.uid_scope_hash,
            generations=current.generations,
            latest_attempt=sealed,
            last_success=current.last_success,
        )
        self._persist(updated)
        return updated

    def _record_refresh_overlap_locked(
        self,
        manifest: SnapshotManifest,
        *,
        uid_scope_hash: str,
        observed_at: datetime,
    ) -> SnapshotManifest:
        if manifest.uid_scope_hash is not None and not _constant_time_equal(
            manifest.uid_scope_hash, uid_scope_hash
        ):
            raise ValueError("production monitor snapshot account scope mismatch")
        previous = manifest.latest_attempt
        previous_generation = -1 if previous is None else previous.generation
        if previous_generation >= 2**63 - 1:
            raise ValueError("snapshot generation is exhausted")
        sealed_at = (
            observed_at
            if previous is None
            else max(observed_at, previous.request_completed_at)
        )
        envelope = SnapshotGeneration(
            generation=previous_generation + 1,
            outcome="FAILURE",
            request_started_at=sealed_at,
            request_completed_at=sealed_at,
            uid_scope_hash=uid_scope_hash,
            collections=(),
            failure_code="refresh_overlap",
        )
        return self._seal_failure_locked(envelope, manifest=manifest)

    def _now(self) -> datetime:
        return _aware_utc(self._now_factory(), field="current time")

    def _load(self) -> SnapshotManifest:
        parent_fd = _open_safe_parent(self.path)
        descriptor: int | None = None
        try:
            _reject_existing_symlink(parent_fd, self.path.name)
            try:
                descriptor = os.open(
                    self.path.name,
                    os.O_RDONLY | _no_follow_flag() | _close_on_exec_flag(),
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return SnapshotManifest()
            except OSError as exc:
                raise ValueError(
                    "production monitor snapshot manifest is unsafe or a symlink"
                ) from exc
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("production monitor snapshot manifest must be a regular file")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ValueError("production monitor snapshot manifest mode must be 0600")
            if metadata.st_size > _MAX_MANIFEST_BYTES:
                raise ValueError("production monitor snapshot manifest exceeds safe size")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                raw = handle.read(_MAX_MANIFEST_BYTES + 1)
            if len(raw) > _MAX_MANIFEST_BYTES:
                raise ValueError("production monitor snapshot manifest exceeds safe size")
            return _manifest_from_bytes(raw, now=self._now())
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)

    def _persist(self, manifest: SnapshotManifest) -> None:
        encoded = _manifest_to_bytes(manifest)
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ValueError("production monitor snapshot manifest exceeds safe size")
        # Validate the exact bytes that will become authoritative.
        _manifest_from_bytes(encoded, now=self._now())
        parent_fd = _open_safe_parent(self.path)
        temporary_name = f".{self.path.name}.{os.getpid()}.{os.urandom(16).hex()}.tmp"
        descriptor: int | None = None
        try:
            _reject_existing_symlink(parent_fd, self.path.name)
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _no_follow_flag()
                | _close_on_exec_flag(),
                0o600,
                dir_fd=parent_fd,
            )
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("production monitor snapshot temporary file is unsafe")
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _reject_existing_symlink(parent_fd, self.path.name)
            os.replace(
                temporary_name,
                self.path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_name = ""
            os.fsync(parent_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)


class _RefreshLease:
    """Establish the attempt baseline before exposing the held refresh lease."""

    def __init__(
        self,
        store: ProductionMonitorSnapshotStore,
        *,
        uid_scope_hash: str,
        observed_at: datetime,
    ) -> None:
        self._store = store
        self._uid_scope_hash = uid_scope_hash
        self._observed_at = observed_at
        self._file_lease = _FileLease(store.refresh_lock_path, blocking=False)
        self._file_lease_held = False

    def __enter__(self) -> SnapshotRefreshLease:
        with self._store._manifest_write_lease():
            manifest = self._store._load()
            acquired = self._file_lease.__enter__()
            if acquired:
                self._file_lease_held = True
                return SnapshotRefreshLease(acquired=True, manifest=manifest)
            updated = self._store._record_refresh_overlap_locked(
                manifest,
                uid_scope_hash=self._uid_scope_hash,
                observed_at=self._observed_at,
            )
            return SnapshotRefreshLease(acquired=False, manifest=updated)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._file_lease_held:
            self._file_lease_held = False
            self._file_lease.__exit__(exc_type, exc_value, traceback)


class _FileLease:
    """Descriptor-bound advisory lock that never follows a lock-file symlink."""

    def __init__(self, path: Path, *, blocking: bool) -> None:
        self._path = path
        self._blocking = blocking
        self._descriptor: int | None = None
        self._acquired = False

    def __enter__(self) -> bool:
        parent_fd = _open_safe_parent(self._path)
        descriptor: int | None = None
        try:
            _reject_lock_symlink(parent_fd, self._path.name)
            try:
                descriptor = os.open(
                    self._path.name,
                    os.O_RDWR
                    | os.O_CREAT
                    | _no_follow_flag()
                    | _close_on_exec_flag(),
                    0o600,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise ValueError(
                    "production monitor snapshot lock is unsafe or a symlink"
                ) from exc
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise ValueError("production monitor snapshot lock file is unsafe")
            os.fchmod(descriptor, 0o600)
            _verify_lock_identity(parent_fd, self._path.name, metadata)
            operation = fcntl.LOCK_EX
            if not self._blocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(descriptor, operation)
            except OSError as exc:
                if not self._blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                    return False
                raise
            _verify_lock_identity(parent_fd, self._path.name, os.fstat(descriptor))
            self._descriptor = descriptor
            descriptor = None
            self._acquired = True
            return True
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            if self._acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            self._acquired = False
            os.close(descriptor)

def _validate_next_generation(
    manifest: SnapshotManifest,
    generation: SnapshotGeneration,
) -> None:
    if manifest.uid_scope_hash is not None and not _constant_time_equal(
        manifest.uid_scope_hash, generation.uid_scope_hash
    ):
        raise ValueError("production monitor snapshot account scope mismatch")
    previous = manifest.latest_attempt
    if previous is None:
        return
    if generation.generation <= previous.generation:
        raise ValueError("snapshot generation must strictly increase")
    if generation.request_started_at < previous.request_completed_at:
        raise ValueError("snapshot request timestamps are out of order")


def _validate_and_seal_generation(
    generation: SnapshotGeneration,
    *,
    now: datetime,
    expected_outcome: str,
    expected_digest: str | None = None,
) -> SnapshotGeneration:
    if not isinstance(generation, SnapshotGeneration):
        raise TypeError("generation must be a SnapshotGeneration")
    if generation.outcome != expected_outcome:
        raise ValueError(f"snapshot outcome must be {expected_outcome}")
    if isinstance(generation.generation, bool) or not isinstance(
        generation.generation, int
    ) or not (0 <= generation.generation < 2**63):
        raise ValueError("snapshot generation is invalid")
    started = _aware_utc(generation.request_started_at, field="request_started_at")
    completed = _aware_utc(
        generation.request_completed_at, field="request_completed_at"
    )
    if completed < started:
        raise ValueError("snapshot request timestamps are reversed")
    if started > now or completed > now:
        raise ValueError("snapshot request timestamp is in the future")
    uid_scope_hash = _uid_scope_hash(generation.uid_scope_hash)

    if expected_outcome == "SUCCESS":
        if generation.failure_code is not None:
            raise ValueError("successful snapshot cannot contain failure_code")
        if tuple(item.name for item in generation.collections) != SNAPSHOT_COLLECTION_NAMES:
            raise ValueError("snapshot must use canonical collection order")
        collections = tuple(
            _validate_collection(item, require_complete=True)
            for item in generation.collections
        )
        failure_code = None
    else:
        if generation.failure_code not in SNAPSHOT_FAILURE_CODES:
            raise ValueError("snapshot failure must use a closed failure_code")
        names = tuple(item.name for item in generation.collections)
        canonical_subset = tuple(
            name for name in SNAPSHOT_COLLECTION_NAMES if name in set(names)
        )
        if len(set(names)) != len(names) or names != canonical_subset:
            raise ValueError("failure evidence must use canonical collection order")
        collections = tuple(
            _validate_collection(item, require_complete=False)
            for item in generation.collections
        )
        if any(item.rows for item in collections):
            raise ValueError("snapshot failure cannot retain partial rows")
        failure_code = generation.failure_code

    normalized = SnapshotGeneration(
        generation=generation.generation,
        outcome=expected_outcome,
        request_started_at=started,
        request_completed_at=completed,
        uid_scope_hash=uid_scope_hash,
        collections=collections,
        failure_code=failure_code,
    )
    digest = hashlib.sha256(
        _canonical_json(_generation_payload(normalized, include_digest=False))
    ).hexdigest()
    supplied_digest = expected_digest if expected_digest is not None else generation.content_sha256
    if supplied_digest is not None and not _constant_time_equal(supplied_digest, digest):
        raise ValueError("production monitor snapshot manifest digest mismatch")
    return replace(normalized, content_sha256=digest)


def _validate_collection(
    collection: SnapshotCollectionEvidence,
    *,
    require_complete: bool,
) -> SnapshotCollectionEvidence:
    if not isinstance(collection, SnapshotCollectionEvidence):
        raise ValueError("snapshot collection evidence is invalid")
    if collection.name not in SNAPSHOT_COLLECTION_NAMES:
        raise ValueError("snapshot collection name is invalid")
    for field_name, value in (
        ("available", collection.available),
        ("schema_valid", collection.schema_valid),
        ("complete", collection.complete),
    ):
        if not isinstance(value, bool):
            raise ValueError(f"snapshot collection {field_name} is invalid")
    for field_name, value in (
        ("page_count", collection.page_count),
        ("row_count", collection.row_count),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"snapshot collection {field_name} is invalid")
    if collection.page_count > 10_000 or collection.row_count > 1_000_000:
        raise ValueError("snapshot collection metadata exceeds safe size")
    if not isinstance(collection.rows, tuple) or len(collection.rows) > _MAX_ROWS_PER_COLLECTION:
        raise ValueError("snapshot collection rows exceed safe size")
    if require_complete:
        if not (
            collection.available and collection.schema_valid and collection.complete
        ):
            raise ValueError("successful snapshot requires complete collection evidence")
        if collection.reason_code is not None:
            raise ValueError("complete collection cannot contain reason_code")
        if collection.row_count != len(collection.rows):
            raise ValueError("complete collection row_count is inconsistent")
        if collection.page_count < 1:
            raise ValueError("complete collection page_count is invalid")
    elif collection.complete:
        raise ValueError("failure collection cannot be complete")

    reason_code = collection.reason_code
    if reason_code is not None and (
        not isinstance(reason_code, str)
        or _REASON_CODE_PATTERN.fullmatch(reason_code) is None
    ):
        raise ValueError("snapshot collection reason_code is invalid")
    if not require_complete and reason_code is None:
        raise ValueError("incomplete collection requires reason_code")

    identity_key = "posId" if collection.name == "positions" else "ordId"
    normalized_rows: list[Mapping[str, Any]] = []
    identities: set[str] = set()
    for raw_row in collection.rows:
        normalized = _sanitize_row(raw_row)
        identity = normalized.get(identity_key)
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError(f"snapshot collection row requires {identity_key}")
        canonical_identity = identity.strip()
        if canonical_identity in identities:
            raise ValueError("snapshot collection has duplicate collection identity")
        identities.add(canonical_identity)
        normalized_rows.append(normalized)
    normalized_rows.sort(key=lambda row: str(row[identity_key]))
    return SnapshotCollectionEvidence(
        name=collection.name,
        available=collection.available,
        schema_valid=collection.schema_valid,
        complete=collection.complete,
        page_count=collection.page_count,
        row_count=collection.row_count,
        rows=tuple(normalized_rows),
        reason_code=reason_code,
    )


def _sanitize_row(row: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError("snapshot collection row must be an object")
    node_count = 0

    def sanitize(value: Any, depth: int) -> Any:
        nonlocal node_count
        node_count += 1
        if depth > _MAX_ROW_DEPTH or node_count > _MAX_ROW_NODES:
            raise ValueError("snapshot collection row exceeds safe size")
        if value is None or isinstance(value, bool) or isinstance(value, int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("snapshot collection row number is invalid")
            return value
        if isinstance(value, str):
            if len(value.encode("utf-8")) > _MAX_STRING_BYTES:
                raise ValueError("snapshot collection row exceeds safe size")
            return value
        if isinstance(value, Mapping):
            normalized: dict[str, Any] = {}
            for key, child in value.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > _MAX_KEY_LENGTH
                    or _SENSITIVE_KEY_PATTERN.search(key) is not None
                ):
                    raise ValueError("snapshot collection row contains unsafe field")
                normalized[key] = sanitize(child, depth + 1)
            return MappingProxyType(normalized)
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray, memoryview)
        ):
            return tuple(sanitize(child, depth + 1) for child in value)
        raise ValueError("snapshot collection row contains unsupported value")

    normalized = sanitize(row, 0)
    encoded = _canonical_json(_thaw_json(normalized))
    if len(encoded) > _MAX_ROW_BYTES:
        raise ValueError("snapshot collection row exceeds safe size")
    return normalized


def _manifest_to_bytes(manifest: SnapshotManifest) -> bytes:
    payload = {
        "schema_version": manifest.schema_version,
        "uid_scope_hash": manifest.uid_scope_hash,
        "generations": [
            _generation_payload(generation, include_digest=True)
            for generation in manifest.generations
        ],
        "latest_attempt": (
            None
            if manifest.latest_attempt is None
            else _generation_payload(manifest.latest_attempt, include_digest=True)
        ),
        "last_success_generation": (
            None if manifest.last_success is None else manifest.last_success.generation
        ),
    }
    return _canonical_json(payload) + b"\n"


def _generation_payload(
    generation: SnapshotGeneration,
    *,
    include_digest: bool,
) -> dict[str, Any]:
    payload = {
        "generation": generation.generation,
        "outcome": generation.outcome,
        "request_started_at": generation.request_started_at.isoformat(),
        "request_completed_at": generation.request_completed_at.isoformat(),
        "uid_scope_hash": generation.uid_scope_hash,
        "collections": [
            {
                "name": item.name,
                "available": item.available,
                "schema_valid": item.schema_valid,
                "complete": item.complete,
                "page_count": item.page_count,
                "row_count": item.row_count,
                "rows": [_thaw_json(row) for row in item.rows],
                "reason_code": item.reason_code,
            }
            for item in generation.collections
        ],
        "failure_code": generation.failure_code,
    }
    if include_digest:
        payload["content_sha256"] = generation.content_sha256
    return payload


def _manifest_from_bytes(raw: bytes, *, now: datetime) -> SnapshotManifest:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(payload, dict) or frozenset(payload) != _MANIFEST_FIELDS:
            raise ValueError("manifest fields are invalid")
        if payload["schema_version"] != SNAPSHOT_MANIFEST_SCHEMA_VERSION or isinstance(
            payload["schema_version"], bool
        ):
            raise ValueError("manifest schema_version is invalid")
        raw_scope = payload["uid_scope_hash"]
        uid_scope_hash = None if raw_scope is None else _uid_scope_hash(raw_scope)
        raw_generations = payload["generations"]
        if not isinstance(raw_generations, list) or len(raw_generations) > 3:
            raise ValueError("manifest generations are invalid")
        generations = tuple(
            _generation_from_payload(item, now=now, expected_outcome="SUCCESS")
            for item in raw_generations
        )
        if any(
            current.generation <= previous.generation
            or current.request_started_at < previous.request_completed_at
            for previous, current in zip(generations, generations[1:])
        ):
            raise ValueError("manifest generation order is invalid")
        raw_latest = payload["latest_attempt"]
        latest_attempt = (
            None
            if raw_latest is None
            else _generation_from_payload(raw_latest, now=now)
        )
        last_success_generation = payload["last_success_generation"]
        if last_success_generation is None:
            last_success = None
        elif isinstance(last_success_generation, bool) or not isinstance(
            last_success_generation, int
        ):
            raise ValueError("manifest last success is invalid")
        else:
            last_success = next(
                (
                    item
                    for item in generations
                    if item.generation == last_success_generation
                ),
                None,
            )
            if last_success is None:
                raise ValueError("manifest last success is missing")
        if latest_attempt is None:
            if generations or last_success is not None or uid_scope_hash is not None:
                raise ValueError("empty manifest is inconsistent")
        else:
            if uid_scope_hash is None or not _constant_time_equal(
                uid_scope_hash, latest_attempt.uid_scope_hash
            ):
                raise ValueError("manifest account scope mismatch")
            if any(
                not _constant_time_equal(uid_scope_hash, item.uid_scope_hash)
                for item in generations
            ):
                raise ValueError("manifest account scope mismatch")
            if latest_attempt.outcome == "SUCCESS":
                if last_success is None or latest_attempt != last_success:
                    raise ValueError("manifest latest success is inconsistent")
            elif generations and latest_attempt.generation <= generations[-1].generation:
                raise ValueError("manifest latest attempt is out of order")
        return SnapshotManifest(
            uid_scope_hash=uid_scope_hash,
            generations=generations,
            latest_attempt=latest_attempt,
            last_success=last_success,
        )
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError(f"invalid production monitor snapshot manifest: {exc}") from None


def _generation_from_payload(
    payload: object,
    *,
    now: datetime,
    expected_outcome: str | None = None,
) -> SnapshotGeneration:
    if not isinstance(payload, dict) or frozenset(payload) != _GENERATION_FIELDS:
        raise ValueError("generation fields are invalid")
    raw_collections = payload["collections"]
    if not isinstance(raw_collections, list):
        raise ValueError("generation collections are invalid")
    collections = tuple(_collection_from_payload(item) for item in raw_collections)
    outcome = payload["outcome"]
    if outcome not in {"SUCCESS", "FAILURE"}:
        raise ValueError("generation outcome is invalid")
    if expected_outcome is not None and outcome != expected_outcome:
        raise ValueError("generation outcome is inconsistent")
    digest = payload["content_sha256"]
    if not isinstance(digest, str) or _UID_HASH_PATTERN.fullmatch(digest) is None:
        raise ValueError("generation digest is invalid")
    generation = SnapshotGeneration(
        generation=payload["generation"],
        outcome=outcome,
        request_started_at=_parse_datetime(payload["request_started_at"]),
        request_completed_at=_parse_datetime(payload["request_completed_at"]),
        uid_scope_hash=payload["uid_scope_hash"],
        collections=collections,
        failure_code=payload["failure_code"],
        content_sha256=digest,
    )
    return _validate_and_seal_generation(
        generation,
        now=now,
        expected_outcome=outcome,
        expected_digest=digest,
    )


def _collection_from_payload(payload: object) -> SnapshotCollectionEvidence:
    if not isinstance(payload, dict) or frozenset(payload) != _COLLECTION_FIELDS:
        raise ValueError("collection fields are invalid")
    rows = payload["rows"]
    if not isinstance(rows, list):
        raise ValueError("collection rows are invalid")
    return SnapshotCollectionEvidence(
        name=payload["name"],
        available=payload["available"],
        schema_valid=payload["schema_valid"],
        complete=payload["complete"],
        page_count=payload["page_count"],
        row_count=payload["row_count"],
        rows=tuple(rows),
        reason_code=payload["reason_code"],
    )


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("snapshot datetime is invalid")
    try:
        return _aware_utc(datetime.fromisoformat(value), field="snapshot datetime")
    except ValueError:
        raise ValueError("snapshot datetime is invalid") from None


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _uid_scope_hash(value: object) -> str:
    if not isinstance(value, str) or _UID_HASH_PATTERN.fullmatch(value) is None:
        raise ValueError("snapshot uid_scope_hash must be a lowercase SHA-256 digest")
    return value


def _constant_time_equal(left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return __import__("hmac").compare_digest(left, right)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _open_safe_parent(path: Path) -> int:
    absolute = path.absolute()
    if absolute.name in {"", ".", ".."}:
        raise ValueError("production monitor snapshot manifest path is invalid")
    current = Path(absolute.anchor)
    for component in absolute.parent.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ValueError("production monitor snapshot parent is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("production monitor snapshot path contains a symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("production monitor snapshot parent is not a directory")
    flags = os.O_RDONLY | _close_on_exec_flag() | _no_follow_flag()
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        return os.open(absolute.parent, flags)
    except OSError as exc:
        raise ValueError("production monitor snapshot parent is unsafe") from exc


def _reject_existing_symlink(parent_fd: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("production monitor snapshot manifest symlink is refused")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("production monitor snapshot manifest must be a regular file")


def _reject_lock_symlink(parent_fd: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("production monitor snapshot lock file symlink is refused")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("production monitor snapshot lock file is unsafe")


def _verify_lock_identity(parent_fd: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ValueError("production monitor snapshot lock file was replaced") from exc
    if stat.S_ISLNK(current.st_mode):
        raise ValueError("production monitor snapshot lock file symlink is refused")
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_dev != expected.st_dev
        or current.st_ino != expected.st_ino
    ):
        raise ValueError("production monitor snapshot lock file was replaced")


def _no_follow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _close_on_exec_flag() -> int:
    return getattr(os, "O_CLOEXEC", 0)
