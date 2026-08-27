from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_kol_research.contract_cache_permissions import (
    ContractCachePermissionError,
    converge_contract_cache_permissions,
    inspect_contract_cache_permissions,
)


def _stub_agent_acl(monkeypatch, *, permission: str = "---") -> list[str]:
    from telegram_kol_research import contract_cache_permissions

    writes: list[str] = []
    current_permission = {"value": permission}
    monkeypatch.setattr(
        contract_cache_permissions,
        "_read_agent_acl_fd",
        lambda _fd, *, agent_user: current_permission["value"],
    )

    def record_write(_fd, *, agent_user):
        writes.append(agent_user)
        current_permission["value"] = "---"

    monkeypatch.setattr(
        contract_cache_permissions,
        "_set_agent_acl_fd",
        record_write,
    )
    return writes


def _inspect(path: Path):
    return inspect_contract_cache_permissions(
        path,
        worker_uid=os.getuid(),
        runtime_gid=os.getgid(),
        agent_user="telegram-kol-agent",
    )


def _converge(path: Path):
    return converge_contract_cache_permissions(
        path,
        worker_uid=os.getuid(),
        runtime_gid=os.getgid(),
        agent_user="telegram-kol-agent",
    )


def test_missing_cache_is_valid_without_creating_an_empty_file(tmp_path, monkeypatch):
    _stub_agent_acl(monkeypatch)
    path = tmp_path / "deepcoin_contract_specs_cache.json"

    inspected = _inspect(path)
    converged = _converge(path)

    assert inspected.exists is False
    assert inspected.contract_satisfied is True
    assert converged.exists is False
    assert converged.contract_satisfied is True
    assert not path.exists()


def test_worker_owned_regular_file_converges_to_exact_contract(tmp_path, monkeypatch):
    acl_writes = _stub_agent_acl(monkeypatch, permission="rw-")
    path = tmp_path / "deepcoin_contract_specs_cache.json"
    path.write_text('{"fixture": true}', encoding="utf-8")
    path.chmod(0o600)
    original_inode = path.stat().st_ino

    status = _converge(path)

    metadata = path.stat()
    assert status.contract_satisfied is True
    assert status.error_category is None
    assert metadata.st_ino == original_inode
    assert metadata.st_uid == os.getuid()
    assert metadata.st_gid == os.getgid()
    assert stat.S_IMODE(metadata.st_mode) == 0o660
    assert path.read_text(encoding="utf-8") == '{"fixture": true}'
    assert acl_writes == ["telegram-kol-agent"]


def test_root_owned_regular_file_can_migrate_to_worker(tmp_path, monkeypatch):
    from telegram_kol_research import contract_cache_permissions

    _stub_agent_acl(monkeypatch)
    path = tmp_path / "deepcoin_contract_specs_cache.json"
    path.write_text("{}", encoding="utf-8")
    real_fstat = os.fstat
    first_inspection = {"pending": True}

    def report_root_owner_once(fd):
        metadata = real_fstat(fd)
        if first_inspection["pending"]:
            first_inspection["pending"] = False
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_nlink=metadata.st_nlink,
                st_uid=0,
                st_gid=metadata.st_gid,
            )
        return metadata

    monkeypatch.setattr(contract_cache_permissions.os, "fstat", report_root_owner_once)

    status = converge_contract_cache_permissions(
        path,
        worker_uid=os.getuid(),
        runtime_gid=os.getgid(),
        agent_user="telegram-kol-agent",
    )

    assert status.contract_satisfied is True
    assert path.stat().st_uid == os.getuid()
    assert path.stat().st_gid == os.getgid()


def test_unknown_owner_is_rejected_without_changing_inode(tmp_path, monkeypatch):
    _stub_agent_acl(monkeypatch)
    path = tmp_path / "private-cache-name.json"
    path.write_text("sensitive-cache-content", encoding="utf-8")
    if os.geteuid() == 0:
        os.chown(path, 65534, -1)
        worker_uid = 65533
    else:
        worker_uid = os.getuid() + 1
    before = path.stat()

    with pytest.raises(ContractCachePermissionError) as caught:
        converge_contract_cache_permissions(
            path,
            worker_uid=worker_uid,
            runtime_gid=os.getgid(),
            agent_user="telegram-kol-agent",
        )

    after = path.stat()
    assert caught.value.category == "unexpected_owner"
    assert str(caught.value) == "unexpected_owner"
    assert "private-cache-name" not in str(caught.value)
    assert "sensitive-cache-content" not in str(caught.value)
    assert after.st_ino == before.st_ino
    assert after.st_uid == before.st_uid
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "hardlink"])
def test_unsafe_target_types_fail_closed(kind, tmp_path, monkeypatch):
    _stub_agent_acl(monkeypatch)
    path = tmp_path / "deepcoin_contract_specs_cache.json"
    protected = tmp_path / "protected"
    protected.write_text("unchanged", encoding="utf-8")
    if kind == "symlink":
        path.symlink_to(protected)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        os.link(protected, path)

    with pytest.raises(ContractCachePermissionError) as caught:
        _converge(path)

    assert caught.value.category in {"invalid_target", "multiple_links"}
    assert protected.read_text(encoding="utf-8") == "unchanged"


def test_repeated_converge_is_idempotent(tmp_path, monkeypatch):
    _stub_agent_acl(monkeypatch)
    path = tmp_path / "deepcoin_contract_specs_cache.json"
    path.write_text("{}", encoding="utf-8")

    first = _converge(path)
    first_metadata = path.stat()
    second = _converge(path)
    second_metadata = path.stat()

    assert first == second
    assert second.contract_satisfied is True
    assert second_metadata.st_ino == first_metadata.st_ino
    assert second_metadata.st_mtime_ns == first_metadata.st_mtime_ns


def test_inspect_is_read_only(tmp_path, monkeypatch):
    _stub_agent_acl(monkeypatch, permission="r--")
    path = tmp_path / "deepcoin_contract_specs_cache.json"
    content = '{"fixture": "unchanged"}'
    path.write_text(content, encoding="utf-8")
    path.chmod(0o640)
    before = path.stat()

    status = _inspect(path)

    after = path.stat()
    assert status.exists is True
    assert status.contract_satisfied is False
    assert status.mode_satisfied is False
    assert status.acl_satisfied is False
    assert path.read_text(encoding="utf-8") == content
    assert after.st_uid == before.st_uid
    assert after.st_gid == before.st_gid
    assert after.st_mode == before.st_mode
    assert after.st_mtime_ns == before.st_mtime_ns


def _replace_as_nobody(parent: Path, target: Path, temporary_name: str) -> int:
    pid = os.fork()
    if pid == 0:
        try:
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)
            temporary = parent / temporary_name
            temporary.write_text("replacement", encoding="utf-8")
            os.replace(temporary, target)
        except PermissionError:
            os._exit(10)
        except BaseException:
            os._exit(20)
        os._exit(0)
    _, wait_status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(wait_status)


@pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="requires Linux root for real cross-UID sticky-directory semantics",
)
def test_linux_sticky_directory_replace_requires_target_ownership(
    tmp_path, monkeypatch
):
    _stub_agent_acl(monkeypatch)
    parent = tmp_path / "sticky"
    parent.mkdir(mode=0o777)
    parent.chmod(0o1777)
    target = parent / "deepcoin_contract_specs_cache.json"
    target.write_text("root-owned", encoding="utf-8")
    os.chown(target, 0, 0)

    assert _replace_as_nobody(parent, target, "before.tmp") == 10
    (parent / "before.tmp").unlink(missing_ok=True)

    status = converge_contract_cache_permissions(
        target,
        worker_uid=65534,
        runtime_gid=65534,
        agent_user="telegram-kol-agent",
    )

    assert status.contract_satisfied is True
    assert _replace_as_nobody(parent, target, "after.tmp") == 0
    assert target.read_text(encoding="utf-8") == "replacement"
