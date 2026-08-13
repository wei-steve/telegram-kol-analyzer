"""Cross-process UID and endpoint budgets for Deepcoin requests."""

from __future__ import annotations

import fcntl
import errno
import hashlib
import json
import math
import os
import stat
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


@dataclass(frozen=True, slots=True)
class _StateDirectoryIdentity:
    device: int
    inode: int
    owner_uid: int


def _state_directory_identity(directory: Path) -> _StateDirectoryIdentity:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = os.lstat(directory)
    except OSError as exc:
        raise DeepcoinGovernorStateError(
            "governor_state_directory_invalid"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DeepcoinGovernorStateError("governor_state_directory_invalid")
    return _StateDirectoryIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner_uid=metadata.st_uid,
    )


def _no_follow_flag() -> int:
    value = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(value, int):
        raise DeepcoinGovernorStateError("governor_state_platform_unsupported")
    return value


def _directory_flag() -> int:
    value = getattr(os, "O_DIRECTORY", None)
    if not isinstance(value, int):
        raise DeepcoinGovernorStateError("governor_state_platform_unsupported")
    return value


def _close_on_exec_flag() -> int:
    value = getattr(os, "O_CLOEXEC", 0)
    return value if isinstance(value, int) else 0


def _open_verified_state_directory(
    directory: Path,
    *,
    expected: _StateDirectoryIdentity | None,
) -> int:
    if expected is None:
        raise DeepcoinGovernorStateError("governor_state_directory_invalid")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            directory,
            os.O_RDONLY
            | _directory_flag()
            | _no_follow_flag()
            | _close_on_exec_flag(),
        )
        metadata = os.fstat(descriptor)
    except (OSError, DeepcoinGovernorStateError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise DeepcoinGovernorStateError(
            "governor_state_directory_invalid"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != expected.device
        or metadata.st_ino != expected.inode
        or metadata.st_uid != expected.owner_uid
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise DeepcoinGovernorStateError("governor_state_directory_invalid")
    return descriptor


def _verified_private_regular_file(
    descriptor: int,
    *,
    error_code: str,
) -> os.stat_result:
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise DeepcoinGovernorStateError(error_code) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise DeepcoinGovernorStateError(error_code)
    return metadata


def _open_or_create_private_file(
    directory_descriptor: int,
    name: str,
    *,
    error_code: str,
) -> int:
    common_flags = os.O_RDWR | _no_follow_flag() | _close_on_exec_flag()
    for _ in range(3):
        try:
            return os.open(
                name,
                common_flags,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            try:
                return os.open(
                    name,
                    common_flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise DeepcoinGovernorStateError(error_code) from exc
        except OSError as exc:
            raise DeepcoinGovernorStateError(error_code) from exc
    raise DeepcoinGovernorStateError(error_code)


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
        self._state_directory_identity = (
            None
            if self._mode == GovernorMode.DISABLED
            else _state_directory_identity(self._state_directory)
        )
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
                    starts = self._load_starts(locked, now=now)
                    delay = _required_delay(
                        starts=starts,
                        now=now,
                        profile=profile,
                        priority=normalized_priority,
                    )
                    if not enforce:
                        starts.append(now)
                        self._write_starts(locked, starts)
                        return GovernorLease(
                            uid_scope_hash=self.uid_scope_hash,
                            normalized_path=normalized_path,
                            waited_ms=0,
                            observed_delay_ms=_milliseconds(delay),
                        )
                    if delay <= 0:
                        starts.append(now)
                        self._write_starts(locked, starts)
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

            if deadline is not None and now + delay > deadline:
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
            expected_directory_identity=self._state_directory_identity,
            enforce=enforce,
            deadline_monotonic=deadline_monotonic,
            monotonic_factory=self._clock,
            sleep_fn=self._sleep,
        )

    def _load_starts(
        self,
        locked: "_LockedStatePaths",
        *,
        now: float,
    ) -> list[float]:
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    locked.state_name,
                    os.O_RDONLY | _no_follow_flag() | _close_on_exec_flag(),
                    dir_fd=locked.directory_descriptor,
                )
            except FileNotFoundError:
                return []
            metadata = _verified_private_regular_file(
                descriptor,
                error_code="governor_state_invalid",
            )
            if metadata.st_size > _MAX_STATE_BYTES:
                raise DeepcoinGovernorStateError("governor_state_too_large")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                raw_payload = handle.read(_MAX_STATE_BYTES + 1)
            if len(raw_payload) > _MAX_STATE_BYTES:
                raise DeepcoinGovernorStateError("governor_state_too_large")
            payload = json.loads(raw_payload)
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
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if starts != sorted(starts):
            raise DeepcoinGovernorStateError("governor_state_invalid")
        if starts and starts[-1] > now:
            return []
        return [started for started in starts if now - started < 60.0]

    def _write_starts(
        self,
        locked: "_LockedStatePaths",
        starts: list[float],
    ) -> None:
        bounded = starts[-_MAX_STARTS:]
        encoded = json.dumps(
            {"starts": bounded, "version": _STATE_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_STATE_BYTES:
            raise DeepcoinGovernorStateError("governor_state_too_large")
        temporary_name: str | None = None
        descriptor: int | None = None
        try:
            temporary_name = (
                f".{locked.state_name}.{os.getpid()}."
                f"{os.urandom(16).hex()}.tmp"
            )
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _no_follow_flag()
                | _close_on_exec_flag(),
                0o600,
                dir_fd=locked.directory_descriptor,
            )
            _verified_private_regular_file(
                descriptor,
                error_code="governor_state_write_failed",
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary_name,
                locked.state_name,
                src_dir_fd=locked.directory_descriptor,
                dst_dir_fd=locked.directory_descriptor,
            )
            os.fsync(locked.directory_descriptor)
        except DeepcoinGovernorStateError:
            raise
        except OSError as exc:
            raise DeepcoinGovernorStateError("governor_state_write_failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(
                        temporary_name,
                        dir_fd=locked.directory_descriptor,
                    )
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


@dataclass(slots=True)
class _LockedStatePaths:
    state_path: Path
    descriptor: int
    directory_descriptor: int
    state_name: str


class _LockedEndpointState:
    def __init__(
        self,
        directory: Path,
        state_path: Path,
        *,
        expected_directory_identity: _StateDirectoryIdentity | None,
        enforce: bool,
        deadline_monotonic: float | None,
        monotonic_factory: Callable[[], float],
        sleep_fn: Callable[[float], None],
    ) -> None:
        self._directory = directory
        self._state_path = state_path
        self._expected_directory_identity = expected_directory_identity
        self._enforce = enforce
        self._deadline = deadline_monotonic
        self._clock = monotonic_factory
        self._sleep = sleep_fn
        self._descriptor: int | None = None
        self._directory_descriptor: int | None = None

    def __enter__(self) -> _LockedStatePaths:
        descriptor: int | None = None
        directory_descriptor: int | None = None
        try:
            directory_descriptor = _open_verified_state_directory(
                self._directory,
                expected=self._expected_directory_identity,
            )
            lock_name = self._state_path.with_suffix(".lock").name
            descriptor = _open_or_create_private_file(
                directory_descriptor,
                lock_name,
                error_code="governor_lock_failed",
            )
            _verified_private_regular_file(
                descriptor,
                error_code="governor_lock_failed",
            )
            os.fchmod(descriptor, 0o600)
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
        except (
            _DeepcoinGovernorLockBusy,
            DeepcoinGovernorDeadlineExceeded,
            DeepcoinGovernorStateError,
        ):
            if descriptor is not None:
                os.close(descriptor)
            if directory_descriptor is not None:
                os.close(directory_descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if directory_descriptor is not None:
                os.close(directory_descriptor)
            raise DeepcoinGovernorStateError("governor_lock_failed") from exc
        self._descriptor = descriptor
        self._directory_descriptor = directory_descriptor
        return _LockedStatePaths(
            state_path=self._state_path,
            descriptor=descriptor,
            directory_descriptor=directory_descriptor,
            state_name=self._state_path.name,
        )

    def __exit__(self, exc_type, exc, traceback) -> None:
        descriptor = self._descriptor
        directory_descriptor = self._directory_descriptor
        self._descriptor = None
        self._directory_descriptor = None
        if descriptor is None:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            if directory_descriptor is not None:
                os.close(directory_descriptor)


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
