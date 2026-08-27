"""Descriptor-safe ownership contract for the Deepcoin specification cache."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


_BOUNDED_ERROR_CATEGORIES = frozenset(
    {
        "acl_unavailable",
        "invalid_parent",
        "invalid_target",
        "multiple_links",
        "permission_denied",
        "unexpected_owner",
        "verification_failed",
    }
)


class ContractCachePermissionError(RuntimeError):
    """Fail-closed error whose public text never includes paths or OS details."""

    def __init__(self, category: str) -> None:
        if category not in _BOUNDED_ERROR_CATEGORIES:
            category = "verification_failed"
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class ContractCachePermissionStatus:
    exists: bool
    contract_satisfied: bool
    owner_satisfied: bool
    group_satisfied: bool
    mode_satisfied: bool
    type_satisfied: bool
    link_satisfied: bool
    acl_satisfied: bool
    error_category: str | None = None


def _raise(category: str) -> None:
    raise ContractCachePermissionError(category) from None


def _open_parent(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path.parent, flags)
    except OSError:
        _raise("invalid_parent")


def _open_target(directory_fd: int, name: str) -> int | None:
    if not name or name in {".", ".."} or "/" in name:
        _raise("invalid_target")
    flags = (
        os.O_RDONLY
        | os.O_CLOEXEC
        | os.O_NONBLOCK
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        return os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        if exc.errno in {errno.EACCES, errno.EPERM}:
            _raise("permission_denied")
        _raise("invalid_target")


def _run_acl_command(arguments: list[str], *, fd: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            check=True,
            close_fds=True,
            pass_fds=(fd,),
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        _raise("acl_unavailable")


def _read_agent_acl_fd(fd: int, *, agent_user: str) -> str | None:
    result = _run_acl_command(
        ["/usr/bin/getfacl", "-cp", "--", f"/proc/self/fd/{fd}"],
        fd=fd,
    )
    prefix = f"user:{agent_user}:"
    matches = [
        line.removeprefix(prefix)
        for line in result.stdout.splitlines()
        if line.startswith(prefix)
    ]
    if len(matches) > 1:
        _raise("verification_failed")
    return matches[0] if matches else None


def _set_agent_acl_fd(fd: int, *, agent_user: str) -> None:
    _run_acl_command(
        [
            "/usr/bin/setfacl",
            "-m",
            f"u:{agent_user}:---,g::rw-,m::rw-",
            "--",
            f"/proc/self/fd/{fd}",
        ],
        fd=fd,
    )


def _validate_metadata(metadata: os.stat_result, *, worker_uid: int) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        _raise("invalid_target")
    if metadata.st_nlink != 1:
        _raise("multiple_links")
    if metadata.st_uid not in {0, worker_uid}:
        _raise("unexpected_owner")


def _status_from_fd(
    fd: int,
    *,
    worker_uid: int,
    runtime_gid: int,
    agent_user: str,
) -> ContractCachePermissionStatus:
    metadata = os.fstat(fd)
    _validate_metadata(metadata, worker_uid=worker_uid)
    owner_satisfied = metadata.st_uid == worker_uid
    group_satisfied = metadata.st_gid == runtime_gid
    mode_satisfied = stat.S_IMODE(metadata.st_mode) == 0o660
    acl_satisfied = _read_agent_acl_fd(fd, agent_user=agent_user) == "---"
    contract_satisfied = all(
        (owner_satisfied, group_satisfied, mode_satisfied, acl_satisfied)
    )
    return ContractCachePermissionStatus(
        exists=True,
        contract_satisfied=contract_satisfied,
        owner_satisfied=owner_satisfied,
        group_satisfied=group_satisfied,
        mode_satisfied=mode_satisfied,
        type_satisfied=True,
        link_satisfied=True,
        acl_satisfied=acl_satisfied,
    )


def _missing_status() -> ContractCachePermissionStatus:
    return ContractCachePermissionStatus(
        exists=False,
        contract_satisfied=True,
        owner_satisfied=True,
        group_satisfied=True,
        mode_satisfied=True,
        type_satisfied=True,
        link_satisfied=True,
        acl_satisfied=True,
    )


def inspect_contract_cache_permissions(
    path: Path,
    *,
    worker_uid: int,
    runtime_gid: int,
    agent_user: str,
) -> ContractCachePermissionStatus:
    """Inspect the fixed cache contract without changing file metadata or content."""

    path = Path(path)
    directory_fd = _open_parent(path)
    try:
        fd = _open_target(directory_fd, path.name)
        if fd is None:
            return _missing_status()
        try:
            return _status_from_fd(
                fd,
                worker_uid=worker_uid,
                runtime_gid=runtime_gid,
                agent_user=agent_user,
            )
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def converge_contract_cache_permissions(
    path: Path,
    *,
    worker_uid: int,
    runtime_gid: int,
    agent_user: str,
) -> ContractCachePermissionStatus:
    """Converge an existing trusted cache inode; a missing cache stays missing."""

    path = Path(path)
    directory_fd = _open_parent(path)
    try:
        fd = _open_target(directory_fd, path.name)
        if fd is None:
            return _missing_status()
        try:
            metadata = os.fstat(fd)
            _validate_metadata(metadata, worker_uid=worker_uid)
            try:
                os.fchown(fd, worker_uid, runtime_gid)
                os.fchmod(fd, 0o660)
                _set_agent_acl_fd(fd, agent_user=agent_user)
            except ContractCachePermissionError:
                raise
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EPERM}:
                    _raise("permission_denied")
                _raise("verification_failed")
            status = _status_from_fd(
                fd,
                worker_uid=worker_uid,
                runtime_gid=runtime_gid,
                agent_user=agent_user,
            )
            if not status.contract_satisfied:
                _raise("verification_failed")
            return status
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)
