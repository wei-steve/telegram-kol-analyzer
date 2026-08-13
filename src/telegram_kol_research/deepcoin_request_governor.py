"""Cross-process UID and endpoint budgets for Deepcoin requests."""

from __future__ import annotations

import fcntl
import errno
import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from telegram_kol_research.deepcoin_request_policy import (
    RequestPriority,
    RequestProfile,
    normalize_request_path,
    request_profile,
)


class GovernorMode(StrEnum):
    DISABLED = "disabled"
    TELEMETRY = "telemetry"
    ENFORCE_READS = "enforce_reads"
    ENFORCE_ALL = "enforce_all"


@dataclass(frozen=True, slots=True)
class DeepcoinGovernorEnvironment:
    mode: GovernorMode
    state_directory: Path | None


class DeepcoinGovernorError(RuntimeError):
    """Base class for local pre-send governor failures."""


class DeepcoinGovernorDeadlineExceeded(DeepcoinGovernorError):
    """The safe request budget cannot be acquired inside the caller deadline."""


class DeepcoinGovernorStateError(DeepcoinGovernorError):
    """Shared governor state is malformed and cannot safely be enforced."""


class _DeepcoinGovernorLockBusy(DeepcoinGovernorError):
    """Telemetry could not inspect shared state without blocking the request."""


@dataclass(frozen=True, slots=True)
class GovernorLease:
    uid_scope_hash: str
    normalized_path: str
    waited_ms: int
    observed_delay_ms: int
    state_error: str | None = None


_STATE_VERSION = 1
_MAX_STATE_BYTES = 65_536
_MAX_STARTS = 4_096


def load_deepcoin_governor_environment(
    environ: Mapping[str, str] | None = None,
) -> DeepcoinGovernorEnvironment:
    """Load the transport gate without guessing an enforcement directory."""

    values = os.environ if environ is None else environ
    raw_mode = values.get("DEEPCOIN_REQUEST_GOVERNOR_MODE", "disabled")
    if not isinstance(raw_mode, str):
        return _disabled_environment()
    try:
        mode = GovernorMode(raw_mode.strip().lower())
    except ValueError:
        return _disabled_environment()
    if mode == GovernorMode.DISABLED:
        return _disabled_environment()

    raw_directory = values.get("DEEPCOIN_GOVERNOR_STATE_DIR")
    if (
        not isinstance(raw_directory, str)
        or not raw_directory.strip()
        or len(raw_directory) > 4_096
    ):
        return _disabled_environment()
    directory = Path(raw_directory)
    try:
        if (
            not directory.is_absolute()
            or directory.is_symlink()
            or not directory.is_dir()
        ):
            return _disabled_environment()
        metadata = directory.stat()
        permissions = metadata.st_mode & 0o777
        if metadata.st_uid != os.getuid() or permissions != 0o700:
            return _disabled_environment()
        resolved = directory.resolve(strict=True)
    except OSError:
        return _disabled_environment()
    return DeepcoinGovernorEnvironment(
        mode=mode,
        state_directory=resolved,
    )


def build_deepcoin_request_governor_from_environment(
    *,
    base_url: str,
    api_key: str,
    environ: Mapping[str, str] | None = None,
) -> "DeepcoinRequestGovernor | None":
    config = load_deepcoin_governor_environment(environ)
    if (
        config.mode == GovernorMode.DISABLED
        or config.state_directory is None
    ):
        return None
    return DeepcoinRequestGovernor(
        base_url=base_url,
        api_key=api_key,
        mode=config.mode,
        state_directory=config.state_directory,
    )


def _disabled_environment() -> DeepcoinGovernorEnvironment:
    return DeepcoinGovernorEnvironment(
        mode=GovernorMode.DISABLED,
        state_directory=None,
    )


class DeepcoinRequestGovernor:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        mode: GovernorMode | str,
        state_directory: str | Path,
        monotonic_factory: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        profile_resolver: Callable[[str, str], RequestProfile] | None = None,
    ) -> None:
        self._mode = GovernorMode(mode)
        self._state_directory = Path(state_directory)
        self._clock = monotonic_factory
        self._sleep = sleep_fn
        self._profile_resolver = profile_resolver or request_profile
        scope_source = (
            f"{str(base_url).rstrip('/')}\0{str(api_key)}".encode("utf-8")
        )
        self.uid_scope_hash = hashlib.sha256(scope_source).hexdigest()

    @property
    def mode(self) -> GovernorMode:
        return self._mode

    def enforces(self, method: str) -> bool:
        normalized_method = str(method or "").strip().upper()
        return self._mode == GovernorMode.ENFORCE_ALL or (
            self._mode == GovernorMode.ENFORCE_READS
            and normalized_method == "GET"
        )

    def acquire(
        self,
        *,
        method: str,
        request_path: str,
        priority: RequestPriority | str,
        deadline_monotonic: float | None,
    ) -> GovernorLease:
        normalized_method = str(method or "").strip().upper()
        normalized_path = normalize_request_path(request_path)
        normalized_priority = RequestPriority(priority)
        if self._mode == GovernorMode.DISABLED:
            return GovernorLease(
                uid_scope_hash=self.uid_scope_hash,
                normalized_path=normalized_path,
                waited_ms=0,
                observed_delay_ms=0,
            )

        profile = self._profile_resolver(normalized_method, normalized_path)
        enforce = self.enforces(normalized_method)
        deadline = _normalize_deadline(deadline_monotonic)
        waited_seconds = 0.0
        while True:
            before_lock = _finite_nonnegative(
                self._clock(), field="monotonic clock"
            )
            if enforce and deadline is not None and before_lock > deadline:
                raise DeepcoinGovernorDeadlineExceeded(
                    "deepcoin_governor_deadline_exceeded"
                )
            try:
                with self._locked_endpoint_state(
                    method=normalized_method,
                    request_path=normalized_path,
                    enforce=enforce,
                    deadline_monotonic=deadline,
                ) as locked:
                    now = _finite_nonnegative(
                        self._clock(), field="monotonic clock"
                    )
                    if enforce and deadline is not None and now > deadline:
                        raise DeepcoinGovernorDeadlineExceeded(
                            "deepcoin_governor_deadline_exceeded"
                        )
                    waited_seconds += max(0.0, now - before_lock)
                    starts = self._load_starts(locked.state_path, now=now)
                    delay = _required_delay(
                        starts=starts,
                        now=now,
                        profile=profile,
                        priority=normalized_priority,
                    )
                    if not enforce:
                        starts.append(now)
                        self._write_starts(locked.state_path, starts)
                        return GovernorLease(
                            uid_scope_hash=self.uid_scope_hash,
                            normalized_path=normalized_path,
                            waited_ms=0,
                            observed_delay_ms=_milliseconds(delay),
                        )
                    if delay <= 0:
                        starts.append(now)
                        self._write_starts(locked.state_path, starts)
                        return GovernorLease(
                            uid_scope_hash=self.uid_scope_hash,
                            normalized_path=normalized_path,
                            waited_ms=_milliseconds(waited_seconds),
                            observed_delay_ms=_milliseconds(waited_seconds),
                        )
            except DeepcoinGovernorStateError:
                if enforce:
                    raise
                return GovernorLease(
                    uid_scope_hash=self.uid_scope_hash,
                    normalized_path=normalized_path,
                    waited_ms=0,
                    observed_delay_ms=0,
                            state_error="governor_state_invalid",
                )
            except _DeepcoinGovernorLockBusy:
                return GovernorLease(
                    uid_scope_hash=self.uid_scope_hash,
                    normalized_path=normalized_path,
                    waited_ms=0,
                    observed_delay_ms=0,
                    state_error="governor_lock_busy",
                )

            if (
                deadline is not None
                and now + delay > deadline
            ):
                raise DeepcoinGovernorDeadlineExceeded(
                    "deepcoin_governor_deadline_exceeded"
                )
            self._sleep(delay)
            waited_seconds += delay

    def state_path_for(self, *, method: str, request_path: str) -> Path:
        endpoint_source = (
            f"{str(method or '').strip().upper()}\0"
            f"{normalize_request_path(request_path)}"
        ).encode("utf-8")
        endpoint_hash = hashlib.sha256(endpoint_source).hexdigest()
        return self._state_directory / (
            f"{self.uid_scope_hash}-{endpoint_hash}.json"
        )

    def _locked_endpoint_state(
        self,
        *,
        method: str,
        request_path: str,
        enforce: bool,
        deadline_monotonic: float | None,
    ):
        state_path = self.state_path_for(method=method, request_path=request_path)
        return _LockedEndpointState(
            self._state_directory,
            state_path,
            enforce=enforce,
            deadline_monotonic=deadline_monotonic,
            monotonic_factory=self._clock,
            sleep_fn=self._sleep,
        )

    def _load_starts(self, state_path: Path, *, now: float) -> list[float]:
        if not state_path.exists():
            return []
        try:
            if state_path.stat().st_size > _MAX_STATE_BYTES:
                raise DeepcoinGovernorStateError("governor_state_too_large")
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
                raise DeepcoinGovernorStateError("governor_state_invalid")
            raw_starts = payload.get("starts")
            if not isinstance(raw_starts, list) or len(raw_starts) > _MAX_STARTS:
                raise DeepcoinGovernorStateError("governor_state_invalid")
            starts = [
                _finite_nonnegative(value, field="governor start")
                for value in raw_starts
            ]
        except DeepcoinGovernorStateError:
            raise
        except (
            OSError,
            RecursionError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise DeepcoinGovernorStateError("governor_state_invalid") from exc
        if starts != sorted(starts):
            raise DeepcoinGovernorStateError("governor_state_invalid")
        if starts and starts[-1] > now:
            return []
        return [started for started in starts if now - started < 60.0]

    def _write_starts(self, state_path: Path, starts: list[float]) -> None:
        bounded = starts[-_MAX_STARTS:]
        encoded = json.dumps(
            {"starts": bounded, "version": _STATE_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_STATE_BYTES:
            raise DeepcoinGovernorStateError("governor_state_too_large")
        temporary = state_path.with_name(
            f".{state_path.name}.{os.getpid()}.tmp"
        )
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, state_path)
            os.chmod(state_path, 0o600)
        except OSError as exc:
            raise DeepcoinGovernorStateError("governor_state_write_failed") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass(slots=True)
class _LockedStatePaths:
    state_path: Path
    descriptor: int


class _LockedEndpointState:
    def __init__(
        self,
        directory: Path,
        state_path: Path,
        *,
        enforce: bool,
        deadline_monotonic: float | None,
        monotonic_factory: Callable[[], float],
        sleep_fn: Callable[[float], None],
    ) -> None:
        self._directory = directory
        self._state_path = state_path
        self._enforce = enforce
        self._deadline = deadline_monotonic
        self._clock = monotonic_factory
        self._sleep = sleep_fn
        self._descriptor: int | None = None

    def __enter__(self) -> _LockedStatePaths:
        descriptor: int | None = None
        try:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._directory, 0o700)
            lock_path = self._state_path.with_suffix(".lock")
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            os.chmod(lock_path, 0o600)
            while True:
                try:
                    fcntl.flock(
                        descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    if not self._enforce:
                        raise _DeepcoinGovernorLockBusy(
                            "governor_lock_busy"
                        ) from exc
                    now = _finite_nonnegative(
                        self._clock(), field="monotonic clock"
                    )
                    if self._deadline is not None and now >= self._deadline:
                        raise DeepcoinGovernorDeadlineExceeded(
                            "deepcoin_governor_deadline_exceeded"
                        ) from exc
                    delay = 0.01
                    if self._deadline is not None:
                        delay = min(delay, max(0.0, self._deadline - now))
                    self._sleep(delay)
        except (_DeepcoinGovernorLockBusy, DeepcoinGovernorDeadlineExceeded):
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise DeepcoinGovernorStateError("governor_lock_failed") from exc
        self._descriptor = descriptor
        return _LockedStatePaths(
            state_path=self._state_path,
            descriptor=descriptor,
        )

    def __exit__(self, exc_type, exc, traceback) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _required_delay(
    *,
    starts: list[float],
    now: float,
    profile: RequestProfile,
    priority: RequestPriority,
) -> float:
    per_second = (
        profile.background_per_second
        if priority == RequestPriority.BACKGROUND
        else profile.per_second
    )
    per_minute = (
        profile.background_per_minute
        if priority == RequestPriority.BACKGROUND
        else profile.per_minute
    )
    recent_second = [started for started in starts if now - started < 1.0]
    delays: list[float] = []
    if profile.min_interval_seconds > 0 and starts:
        delays.append(profile.min_interval_seconds - (now - starts[-1]))
    if len(recent_second) >= per_second:
        delays.append(1.0 - (now - recent_second[-per_second]))
    if len(starts) >= per_minute:
        delays.append(60.0 - (now - starts[-per_minute]))
    return max([0.0, *delays])


def _finite_nonnegative(value: object, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise DeepcoinGovernorStateError(f"{field} is invalid") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise DeepcoinGovernorStateError(f"{field} is invalid")
    return parsed


def _normalize_deadline(value: object) -> float | None:
    if value is None:
        return None
    try:
        deadline = float(value)
    except (TypeError, ValueError) as exc:
        raise DeepcoinGovernorDeadlineExceeded(
            "deepcoin_governor_deadline_invalid"
        ) from exc
    if not math.isfinite(deadline) or deadline < 0:
        raise DeepcoinGovernorDeadlineExceeded(
            "deepcoin_governor_deadline_invalid"
        )
    return deadline


def _milliseconds(seconds: float) -> int:
    return max(0, round(float(seconds) * 1000))
