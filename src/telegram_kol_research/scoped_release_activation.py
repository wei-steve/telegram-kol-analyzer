from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
import hashlib
import json
import fcntl
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
from typing import Any, Mapping, Protocol
import uuid
from urllib.request import Request, urlopen

from telegram_kol_research.deployment_action_plan import (
    ActionPlan,
    DeploymentAction,
    DeploymentManifest,
    ManifestValidationError,
    _reject_duplicate_json_keys,
    build_action_plan,
    parse_manifest,
)
from telegram_kol_research.runtime_deployment_identity import (
    validate_runtime_authority_scope,
)


RELEASE_MANIFEST = ".telegram-kol-release.json"
STAGE_RECEIPT = ".telegram-kol-stage-receipt.json"
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AUTHORITY_COMPONENTS = frozenset({"ingest", "worker"})
_RUNTIME_COMPONENTS = frozenset({"web", "ingest", "worker"})
_AUTHORITY_RUNTIME_SCOPE = frozenset({"web", "monitor", "ingest", "worker"})
_LEGACY_SOURCE_UNITS = ("telegram-kol.service",)
_PROCESSLESS_UNITS = frozenset({"telegram-kol-monitor.timer"})
_UNITS = {
    "web": ("telegram-kol-web.service",),
    "ingest": ("telegram-kol-ingest.service",),
    "worker": ("telegram-kol-worker.service",),
    "monitor": (
        "telegram-kol-monitor.service",
        "telegram-kol-monitor-diagnostic.service",
        "telegram-kol-monitor-test-notification.service",
    ),
}
_PORTS = {"web": 8000, "ingest": 8001, "worker": 8002}
RUNTIME_CONTROL_LOCK_PATH = Path("/run/telegram-kol-update.lock")
def _command_matches_role(command: bytes, *, role: str) -> bool:
    try:
        parts = tuple(
            part.decode("utf-8", errors="strict")
            for part in command.split(b"\0")
            if part
        )
    except UnicodeError:
        return False
    expected = ("web", "--runtime-role", role)
    return any(
        parts[index : index + 3] == expected
        for index in range(len(parts) - 2)
    )


class ActivationError(RuntimeError):
    pass


@contextmanager
def exclusive_runtime_control_lock(
    *,
    lock_path: Path = RUNTIME_CONTROL_LOCK_PATH,
    expected_uid: int = 0,
):
    """Hold the single supported service-control lock across a whole operation."""

    descriptor = None
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(Path(lock_path), flags, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ActivationError("runtime control lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ActivationError("runtime control is locked") from exc
    except ActivationError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ActivationError("runtime control lock is unavailable") from exc
    try:
        yield
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class ActivationPaths:
    release_root: Path
    action_manifest: Path
    authorization: Path
    authorization_consumed: Path
    dropin_root: Path
    database_path: Path


@dataclass(frozen=True, slots=True)
class ReleaseEvidence:
    commit: str
    manifest_sha256: str
    content_sha256: str
    action_manifest: Mapping[str, Any]
    release_path: Path


class RuntimeAdapter(Protocol):
    def active_write_count(self, database_path: Path) -> int: ...

    def runtime_identity(self, role: str) -> Mapping[str, Any]: ...

    def stop_unit(self, unit: str) -> None: ...

    def start_unit(self, unit: str) -> None: ...

    def maintenance_unit_state(self, unit: str) -> tuple[str, str]: ...

    def unmask_unit(self, unit: str) -> None: ...

    def mask_unit(self, unit: str) -> None: ...

    def main_pid(self, unit: str) -> int: ...

    def cgroup_pids(self, unit: str) -> tuple[int, ...]: ...

    def matching_processes(self) -> tuple[int, ...]: ...

    def daemon_reload(self) -> None: ...

    def monitor_timer_active(self) -> bool: ...

    def prove_monitor_rollback_release(
        self, release: ReleaseEvidence
    ) -> Mapping[str, Any]: ...

    def verify_monitor_release(
        self, release: ReleaseEvidence
    ) -> Mapping[str, Any]: ...


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _canonical_manifest(manifest: DeploymentManifest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": manifest.action.value,
        "authority_changed": manifest.authority_changed,
        "components": [component.value for component in manifest.components],
        "exchange_write_semantics_changed": manifest.exchange_write_semantics_changed,
        "production_data_mutation": manifest.production_data_mutation,
        "requires_restart": manifest.requires_restart,
        "risk_level": manifest.risk_level.value,
        "schema_changed": manifest.schema_changed,
    }
    if manifest.rollback_releases:
        payload["rollback_releases"] = {
            target.component.value: {
                "commit": target.commit,
                "manifest_sha256": target.manifest_sha256,
            }
            for target in manifest.rollback_releases
        }
    return payload


def _plan_payload(plan: ActionPlan) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": plan.action.value,
        "components": [component.value for component in plan.components],
        "gates": [
            {
                "disposition": gate.disposition.value,
                "id": gate.gate_id,
                "reason": gate.reason,
            }
            for gate in plan.gates
        ],
        "risk_level": plan.risk_level.value,
        "schema_version": 1,
    }
    if plan.rollback_releases:
        payload["rollback_releases"] = {
            target.component.value: {
                "commit": target.commit,
                "manifest_sha256": target.manifest_sha256,
            }
            for target in plan.rollback_releases
        }
    return payload


def action_plan_sha256(manifest: DeploymentManifest) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(_plan_payload(build_action_plan(manifest)))
    ).hexdigest()


def _read_manifest(path: Path) -> tuple[DeploymentManifest, dict[str, Any], str]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ActivationError("action manifest is unsafe")
        if metadata.st_size > 65_536:
            raise ActivationError("action manifest is too large")
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        if not isinstance(payload, dict):
            raise ActivationError("action manifest is invalid")
        manifest = parse_manifest(payload)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        ManifestValidationError,
    ) as exc:
        raise ActivationError("action manifest is invalid") from exc
    if manifest.action is not DeploymentAction.ACTIVATE:
        raise ActivationError("action manifest must declare activate")
    canonical = _canonical_manifest(manifest)
    return manifest, canonical, action_plan_sha256(manifest)


def _content_digest(root: Path) -> str:
    digest = hashlib.sha256()
    try:
        paths = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        for path in paths:
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ActivationError("release validation failed")
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ActivationError("release validation failed")
            if relative in {RELEASE_MANIFEST, STAGE_RECEIPT}:
                continue
            relative_bytes = relative.encode("utf-8")
            digest.update(len(relative_bytes).to_bytes(8, "big"))
            digest.update(relative_bytes)
            digest.update(b"x" if metadata.st_mode & 0o111 else b"-")
            digest.update(metadata.st_size.to_bytes(8, "big"))
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
    except (OSError, UnicodeError) as exc:
        raise ActivationError("release validation failed") from exc
    return digest.hexdigest()


def _runtime_support_digest(root: Path) -> str:
    digest = hashlib.sha256()
    dependency_names = {
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "setup.cfg",
        "setup.py",
    }
    try:
        for path in sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        ):
            relative = path.relative_to(root).as_posix()
            runtime_support = (
                relative in dependency_names
                or relative.startswith("config/")
                or relative.startswith("deploy/systemd/")
            )
            if not runtime_support:
                continue
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ActivationError("release scope validation failed")
            encoded = relative.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            digest.update(b"x" if metadata.st_mode & 0o111 else b"-")
            digest.update(metadata.st_size.to_bytes(8, "big"))
            digest.update(path.read_bytes())
    except OSError as exc:
        raise ActivationError("release scope validation failed") from exc
    return digest.hexdigest()


def _load_canonical_json(path: Path, *, expected_uid: int) -> tuple[dict[str, Any], bytes]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or metadata.st_size > 65_536
        ):
            raise ActivationError("release validation failed")
        encoded = path.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActivationError("release validation failed") from exc
    if not isinstance(payload, dict) or encoded != _canonical_json_bytes(payload):
        raise ActivationError("release validation failed")
    return payload, encoded


def validate_release(
    release_root: Path,
    commit: str,
    *,
    expected_uid: int,
) -> ReleaseEvidence:
    if not _SHA1_RE.fullmatch(commit):
        raise ActivationError("release validation failed")
    root = release_root / commit
    try:
        root_metadata = root.lstat()
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or root_metadata.st_uid != expected_uid
            or stat.S_IMODE(root_metadata.st_mode) != 0o555
        ):
            raise ActivationError("release validation failed")
        for path in root.rglob("*"):
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if metadata.st_uid != expected_uid or stat.S_ISLNK(metadata.st_mode):
                raise ActivationError("release validation failed")
            if stat.S_ISDIR(metadata.st_mode) and mode != 0o555:
                raise ActivationError("release validation failed")
            if stat.S_ISREG(metadata.st_mode) and mode not in {0o444, 0o555}:
                raise ActivationError("release validation failed")
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise ActivationError("release validation failed")
    except OSError as exc:
        raise ActivationError("release validation failed") from exc

    manifest, manifest_bytes = _load_canonical_json(
        root / RELEASE_MANIFEST, expected_uid=expected_uid
    )
    receipt, _ = _load_canonical_json(root / STAGE_RECEIPT, expected_uid=expected_uid)
    content_sha = _content_digest(root)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    try:
        staged_manifest = parse_manifest(manifest["action_manifest"])
    except (KeyError, ManifestValidationError) as exc:
        raise ActivationError("release validation failed") from exc
    if staged_manifest.action is not DeploymentAction.STAGE:
        raise ActivationError("release validation failed")
    staged_plan_sha = action_plan_sha256(staged_manifest)
    if (
        manifest.get("contract") != "immutable-release-v1"
        or manifest.get("schema_version") != 1
        or manifest.get("commit") != commit
        or manifest.get("content_sha256") != content_sha
        or manifest.get("action_plan_sha256") != staged_plan_sha
        or not _SHA1_RE.fullmatch(str(manifest.get("tree", "")))
        or receipt.get("contract") != "immutable-release-v1"
        or receipt.get("schema_version") != 1
        or receipt.get("status") != "staged"
        or receipt.get("commit") != commit
        or receipt.get("release_name") != commit
        or receipt.get("content_sha256") != content_sha
        or receipt.get("manifest_sha256") != manifest_sha
        or receipt.get("action_plan_sha256") != staged_plan_sha
        or receipt.get("tree") != manifest.get("tree")
        or receipt.get("branch") != manifest.get("branch")
    ):
        raise ActivationError("release validation failed")
    return ReleaseEvidence(
        commit=commit,
        manifest_sha256=manifest_sha,
        content_sha256=content_sha,
        action_manifest=manifest["action_manifest"],
        release_path=root,
    )


def _same_declared_change(stage: Mapping[str, Any], activate: Mapping[str, Any]) -> bool:
    keys = set(activate) - {"action", "rollback_releases"}
    return keys == set(stage) - {"action"} and all(stage[key] == activate[key] for key in keys)


def _rollback_release_payload(manifest: DeploymentManifest) -> dict[str, dict[str, str]]:
    return {
        target.component.value: {
            "commit": target.commit,
            "manifest_sha256": target.manifest_sha256,
        }
        for target in manifest.rollback_releases
    }


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ActivationError("authorization is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActivationError("authorization is invalid") from exc
    if parsed.tzinfo is None:
        raise ActivationError("authorization is invalid")
    return parsed.astimezone(UTC)


def _validate_authorization(
    path: Path,
    *,
    expected_uid: int,
    commit: str,
    components: list[str],
    plan_sha256: str,
    source_mode: str,
    now: datetime,
    rollback_releases: Mapping[str, Mapping[str, str]] | None = None,
    controller_commit: str | None = None,
    controller_bundle_sha256: str | None = None,
) -> None:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_size > 16_384
        ):
            raise ActivationError("authorization is invalid")
        encoded = path.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActivationError("authorization is invalid") from exc
    if not isinstance(payload, dict) or encoded != _canonical_json_bytes(payload):
        raise ActivationError("authorization is invalid")
    issued_at = _parse_time(payload.get("issued_at"))
    expires_at = _parse_time(payload.get("expires_at"))
    legacy_fields = {
        "action_plan_sha256",
        "commit",
        "components",
        "contract",
        "expires_at",
        "issued_at",
        "nonce",
        "schema_version",
        "source_mode",
    }
    per_role_fields = legacy_fields | {
        "rollback_releases",
        "controller_commit",
        "controller_bundle_sha256",
    }
    per_role = rollback_releases is not None
    expected_fields = per_role_fields if per_role else legacy_fields
    if (
        set(payload) != expected_fields
        or payload.get("contract")
        != (
            "scoped-activation-authorization-v3"
            if per_role
            else "scoped-activation-authorization-v2"
        )
        or payload.get("schema_version") != (3 if per_role else 2)
        or payload.get("commit") != commit
        or payload.get("components") != components
        or payload.get("action_plan_sha256") != plan_sha256
        or payload.get("source_mode") != source_mode
        or not _SHA256_RE.fullmatch(str(payload.get("nonce", "")))
        or issued_at > now
        or expires_at <= now
        or expires_at - issued_at > timedelta(minutes=15)
        or (
            per_role
            and (
                payload.get("rollback_releases") != rollback_releases
                or payload.get("controller_commit") != controller_commit
                or payload.get("controller_bundle_sha256")
                != controller_bundle_sha256
                or not _SHA1_RE.fullmatch(str(controller_commit or ""))
                or not _SHA256_RE.fullmatch(str(controller_bundle_sha256 or ""))
            )
        )
    ):
        raise ActivationError("authorization is invalid")


def _validate_identity(
    payload: Mapping[str, Any],
    *,
    role: str,
    release: ReleaseEvidence,
    now: datetime,
    require_entry_frozen: bool,
) -> None:
    observed_at = _parse_time(payload.get("observed_at"))
    health = payload.get("health")
    if (
        payload.get("contract") != "runtime-deployment-identity-v1"
        or payload.get("runtime_role") != role
        or payload.get("release_commit") != release.commit
        or payload.get("manifest_sha256") != release.manifest_sha256
        or payload.get("loaded_artifact_verified") is not True
        or type(payload.get("pid")) is not int
        or payload["pid"] <= 1
        or type(payload.get("process_start_ticks")) is not int
        or payload["process_start_ticks"] <= 0
        or payload.get("systemd_main_pid") != payload.get("pid")
        or payload.get("systemd_start_ticks") != payload.get("process_start_ticks")
        or observed_at > now + timedelta(seconds=1)
        or now - observed_at > timedelta(seconds=10)
        or not isinstance(health, Mapping)
        or health.get("event_loop") is not True
        or (
            require_entry_frozen
            and (
                payload.get("entry_admission_frozen") is not True
                or health.get("message_processing") is not False
            )
        )
        or (
            role == "ingest"
            and (
                health.get("ingest_live_listener") is not True
                or health.get("ingest_reconcile") is not True
            )
        )
        or (role == "worker" and health.get("worker_command") is not True)
    ):
        raise ActivationError("runtime identity proof failed")


def prove_release_runtime(
    runtime: RuntimeAdapter,
    *,
    release_root: Path,
    expected_uid: int,
    expected_releases: Mapping[str, ReleaseEvidence],
    components: list[str],
    require_authority: bool,
    require_entry_frozen: bool,
    now: datetime,
    max_attempts: int = 2,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, ReleaseEvidence]]:
    if max_attempts not in {1, 2}:
        raise ValueError("runtime proof attempt count is invalid")
    roles = [role for role in components if role in _RUNTIME_COMPONENTS]
    if require_authority:
        roles = ["web", "ingest", "worker"]
    last_error: ActivationError | None = None
    for attempt in range(max_attempts):
        try:
            identities: dict[str, Mapping[str, Any]] = {}
            releases: dict[str, ReleaseEvidence] = {}
            for role in roles:
                identity = runtime.runtime_identity(role)
                expected_release = expected_releases.get(role)
                if expected_release is None:
                    observed_commit = str(identity.get("release_commit") or "")
                    expected_release = validate_release(
                        release_root,
                        observed_commit,
                        expected_uid=expected_uid,
                    )
                _validate_identity(
                    identity,
                    role=role,
                    release=expected_release,
                    now=datetime.now(UTC),
                    require_entry_frozen=require_entry_frozen,
                )
                identities[role] = identity
                releases[role] = expected_release
            if not require_authority:
                return identities, releases
            try:
                validate_runtime_authority_scope(identities)
            except ValueError as exc:
                raise ActivationError("authority proof failed")
            return identities, releases
        except ActivationError as exc:
            last_error = exc
            if attempt + 1 < max_attempts:
                time.sleep(float(getattr(runtime, "identity_retry_delay_seconds", 0)))
    assert last_error is not None
    raise last_error


def _prove_restarted_processes(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
    components: list[str],
) -> None:
    for role in components:
        if role not in _RUNTIME_COMPONENTS:
            continue
        previous = before.get(role)
        current = after.get(role)
        if previous is None or current is None:
            raise ActivationError("runtime restart proof failed")
        if (
            previous.get("pid"),
            previous.get("process_start_ticks"),
        ) == (
            current.get("pid"),
            current.get("process_start_ticks"),
        ):
            raise ActivationError("runtime restart proof failed")


def _prove_undeclared_processes_unchanged(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
    components: list[str],
) -> None:
    declared = set(components)
    for role in set(before) & set(after):
        if role in declared:
            continue
        for key in ("release_commit", "manifest_sha256", "pid", "process_start_ticks"):
            if before[role].get(key) != after[role].get(key):
                raise ActivationError("undeclared runtime changed during activation")


def render_release_dropin(
    release: ReleaseEvidence,
    *,
    component: str,
    entry_frozen: bool,
) -> str:
    lines = [
        "[Service]",
        f'Environment="PYTHONPATH={release.release_path}/src"',
        'Environment="PYTHONDONTWRITEBYTECODE=1"',
        f'Environment="TELEGRAM_KOL_RELEASE_COMMIT={release.commit}"',
        f'Environment="TELEGRAM_KOL_RELEASE_MANIFEST_SHA256={release.manifest_sha256}"',
        f"ReadOnlyPaths={release.release_path}",
    ]
    if entry_frozen and component in _RUNTIME_COMPONENTS:
        lines.append('Environment="TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN=1"')
    if component == "monitor":
        lines.extend(
            (
                f'Environment="TELEGRAM_KOL_MONITOR_RELEASE_PATH={release.release_path}"',
                f'Environment="TELEGRAM_KOL_MONITOR_RELEASE_COMMIT={release.commit}"',
                "Environment=\"TELEGRAM_KOL_MONITOR_RELEASE_MANIFEST_SHA256="
                f"{release.manifest_sha256}\"",
            )
        )
        lines.append(
            "ExecStartPre=/opt/telegram-kol-analyzer/.venv/bin/python -m "
            "telegram_kol_research.runtime_deployment_identity --verify-self"
        )
    return "\n".join(lines) + "\n"


def _atomic_write_dropin(root: Path, unit: str, content: str) -> None:
    directory = root / f"{unit}.d"
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    destination = directory / "10-telegram-kol-release.conf"
    temporary = directory / ".10-telegram-kol-release.conf.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ActivationError("drop-in publication failed") from exc


def publish_release_dropins(
    paths: ActivationPaths,
    release: ReleaseEvidence,
    components: list[str],
    *,
    entry_frozen: bool,
) -> None:
    for component in components:
        for unit in _UNITS[component]:
            _atomic_write_dropin(
                paths.dropin_root,
                unit,
                render_release_dropin(
                    release,
                    component=component,
                    entry_frozen=entry_frozen,
                ),
            )


def publish_component_release_dropins(
    paths: ActivationPaths,
    releases: Mapping[str, ReleaseEvidence],
    components: list[str],
    *,
    entry_frozen: bool,
) -> None:
    for component in components:
        release = releases[component]
        for unit in _UNITS[component]:
            _atomic_write_dropin(
                paths.dropin_root,
                unit,
                render_release_dropin(
                    release,
                    component=component,
                    entry_frozen=entry_frozen,
                ),
            )


def _controlled_units(components: list[str]) -> list[str]:
    units = []
    for component in ("ingest", "web", "worker"):
        if component in components:
            units.extend(_UNITS[component])
    if "monitor" in components:
        units.append("telegram-kol-monitor.timer")
        units.extend(_UNITS["monitor"])
    return units


def _start_order(components: list[str]) -> list[str]:
    units = []
    for component in ("worker", "web", "ingest"):
        if component in components:
            units.extend(_UNITS[component])
    return units


def _stop_units(runtime: RuntimeAdapter, components: list[str]) -> None:
    for unit in _controlled_units(components):
        runtime.stop_unit(unit)


def _best_effort_stop_units(runtime: RuntimeAdapter, components: list[str]) -> None:
    for unit in _controlled_units(components):
        try:
            runtime.stop_unit(unit)
        except Exception:
            pass


def _start_units(runtime: RuntimeAdapter, components: list[str]) -> None:
    for unit in _start_order(components):
            runtime.start_unit(unit)


def _require_stopped_legacy_boundary(
    runtime: RuntimeAdapter,
    components: list[str],
) -> None:
    if set(components) != _AUTHORITY_RUNTIME_SCOPE:
        raise ActivationError("stopped legacy activation requires full runtime scope")
    units = (*_controlled_units(components), *_LEGACY_SOURCE_UNITS)
    for unit in units:
        try:
            active_state, enabled_state = runtime.maintenance_unit_state(unit)
            main_pid = runtime.main_pid(unit)
            cgroup_pids = runtime.cgroup_pids(unit)
        except Exception as exc:
            raise ActivationError("stopped legacy runtime state is unknown") from exc
        if (
            active_state != "inactive"
            or enabled_state != "inhibited"
            or main_pid != 0
            or cgroup_pids
        ):
            raise ActivationError("stopped legacy runtime is not persistently stopped")
    try:
        matching = runtime.matching_processes()
    except Exception as exc:
        raise ActivationError("stopped legacy runtime state is unknown") from exc
    if matching:
        raise ActivationError("stopped legacy runtime process remains")


def require_stopped_legacy_runtime_boundary(runtime: RuntimeAdapter) -> None:
    """Prove the complete legacy/split runtime is persistently inhibited."""

    _require_stopped_legacy_boundary(
        runtime,
        ["web", "monitor", "ingest", "worker"],
    )


def _unmask_units(runtime: RuntimeAdapter, components: list[str]) -> None:
    for unit in _controlled_units(components):
        runtime.unmask_unit(unit)


def _mask_units(runtime: RuntimeAdapter, components: list[str]) -> None:
    for unit in _controlled_units(components):
        runtime.mask_unit(unit)


def _reinhibit_and_stop_all(
    runtime: RuntimeAdapter,
    components: list[str],
) -> None:
    units = (*_controlled_units(components), *_LEGACY_SOURCE_UNITS)
    failed = False
    for unit in units:
        try:
            runtime.mask_unit(unit)
        except Exception:
            failed = True
    for unit in units:
        try:
            runtime.stop_unit(unit)
        except Exception:
            failed = True
    for unit in units:
        try:
            active_state, enabled_state = runtime.maintenance_unit_state(unit)
            if (
                active_state != "inactive"
                or enabled_state != "inhibited"
                or runtime.main_pid(unit) != 0
                or runtime.cgroup_pids(unit)
            ):
                failed = True
        except Exception:
            failed = True
    for _ in range(2):
        try:
            if runtime.matching_processes():
                failed = True
        except Exception:
            failed = True
    if failed:
        raise ActivationError("stopped legacy reinhibit proof failed")


def consume_activation_authorization(source: Path, consumed: Path) -> None:
    if source.parent != consumed.parent:
        raise ActivationError("authorization consumption must remain in one directory")
    try:
        os.link(source, consumed, follow_symlinks=False)
        source.unlink()
        descriptor = os.open(source.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ActivationError("authorization could not be consumed") from exc


def _activate_monitor(
    runtime: RuntimeAdapter,
    components: list[str],
    release: ReleaseEvidence,
) -> Mapping[str, Any] | None:
    if "monitor" in components:
        evidence = runtime.verify_monitor_release(release)
        if not isinstance(evidence, Mapping):
            raise ActivationError("monitor runtime proof failed")
        return evidence
    return None


def _resolve_rollback_releases(
    *,
    manifest: DeploymentManifest,
    rollback_commit: str,
    candidate: ReleaseEvidence,
    release_root: Path,
    expected_uid: int,
) -> tuple[dict[str, ReleaseEvidence], dict[str, dict[str, str]] | None]:
    components = [component.value for component in manifest.components]
    if manifest.rollback_releases:
        if rollback_commit:
            raise ActivationError(
                "legacy rollback commit cannot accompany per-role rollback releases"
            )
        releases: dict[str, ReleaseEvidence] = {}
        payload = _rollback_release_payload(manifest)
        validated: dict[str, ReleaseEvidence] = {}
        for target in manifest.rollback_releases:
            if target.commit == candidate.commit:
                raise ActivationError("candidate and rollback releases must differ")
            release = validated.get(target.commit)
            if release is None:
                release = validate_release(
                    release_root,
                    target.commit,
                    expected_uid=expected_uid,
                )
                validated[target.commit] = release
            if release.manifest_sha256 != target.manifest_sha256:
                raise ActivationError("rollback release manifest does not match declaration")
            if _runtime_support_digest(candidate.release_path) != _runtime_support_digest(
                release.release_path
            ):
                raise ActivationError(
                    "release scope validation failed: runtime config, dependencies, or units changed"
                )
            releases[target.component.value] = release
        return releases, payload

    rollback = validate_release(
        release_root,
        rollback_commit,
        expected_uid=expected_uid,
    )
    if candidate.commit == rollback.commit:
        raise ActivationError("candidate and rollback releases must differ")
    if _runtime_support_digest(candidate.release_path) != _runtime_support_digest(
        rollback.release_path
    ):
        raise ActivationError(
            "release scope validation failed: runtime config, dependencies, or units changed"
        )
    return {component: rollback for component in components}, None


def _revalidate_release_set(
    release_root: Path,
    releases: Mapping[str, ReleaseEvidence],
    *,
    expected_uid: int,
) -> None:
    validated: set[tuple[str, str]] = set()
    for release in releases.values():
        identity = (release.commit, release.manifest_sha256)
        if identity in validated:
            continue
        observed = validate_release(
            release_root,
            release.commit,
            expected_uid=expected_uid,
        )
        if observed.manifest_sha256 != release.manifest_sha256:
            raise ActivationError("release changed after preflight")
        validated.add(identity)


def activate_release(
    *,
    expected_commit: str,
    rollback_commit: str,
    paths: ActivationPaths,
    runtime: RuntimeAdapter,
    expected_uid: int,
    now: datetime | None = None,
    source_mode: str = "immutable",
    controller_commit: str | None = None,
    controller_bundle_sha256: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    observed_now = (now or datetime.now(UTC)).astimezone(UTC)
    manifest, canonical, plan_sha = _read_manifest(paths.action_manifest)
    components = [component.value for component in manifest.components]
    if manifest.schema_changed or manifest.production_data_mutation:
        raise ActivationError(
            "L3 database activation requires a separate backup/integrity executor"
        )
    authority_components = set(components) & _AUTHORITY_COMPONENTS
    if authority_components and set(components) != _AUTHORITY_RUNTIME_SCOPE:
        raise ActivationError(
            "authority activation must declare web, monitor, ingest, and worker"
        )
    if source_mode not in {"immutable", "stopped_legacy"}:
        raise ActivationError("activation source mode is invalid")
    if source_mode == "stopped_legacy" and manifest.rollback_releases:
        raise ActivationError(
            "per-role rollback releases require immutable source mode"
        )
    candidate = validate_release(
        paths.release_root,
        expected_commit,
        expected_uid=expected_uid,
    )
    rollback_releases: dict[str, ReleaseEvidence] = {}
    rollback_payload: dict[str, dict[str, str]] | None = None
    if source_mode == "immutable":
        rollback_releases, rollback_payload = _resolve_rollback_releases(
            manifest=manifest,
            rollback_commit=rollback_commit,
            candidate=candidate,
            release_root=paths.release_root,
            expected_uid=expected_uid,
        )
    if not _same_declared_change(candidate.action_manifest, canonical):
        raise ActivationError("staged and activation declarations differ")

    require_authority = bool(set(components) & _AUTHORITY_COMPONENTS)
    _validate_authorization(
        paths.authorization,
        expected_uid=expected_uid,
        commit=expected_commit,
        components=components,
        plan_sha256=plan_sha,
        source_mode=source_mode,
        now=observed_now,
        rollback_releases=rollback_payload,
        controller_commit=controller_commit,
        controller_bundle_sha256=controller_bundle_sha256,
    )
    affected_runtime_roles = [
        component for component in components if component in _RUNTIME_COMPONENTS
    ]
    if source_mode == "stopped_legacy":
        _require_stopped_legacy_boundary(runtime, components)
        before_identities: dict[str, Mapping[str, Any]] = {}
        before_releases: dict[str, ReleaseEvidence] = {}
        preserve_entry_freeze = True
    else:
        before_identities, before_releases = prove_release_runtime(
            runtime,
            release_root=paths.release_root,
            expected_uid=expected_uid,
            expected_releases={
                role: rollback_releases[role] for role in affected_runtime_roles
            },
            components=components,
            require_authority=require_authority,
            require_entry_frozen=False,
            now=observed_now,
        )
        if rollback_payload is not None and "monitor" in components:
            runtime.prove_monitor_rollback_release(rollback_releases["monitor"])
        preserve_entry_freeze = require_authority or any(
            identity.get("entry_admission_frozen") is True
            for identity in before_identities.values()
        )
    if require_authority and runtime.active_write_count(paths.database_path) != 0:
        raise ActivationError("active exchange write blocks activation")

    monitor_was_active = bool(
        source_mode == "immutable"
        and "monitor" in components
        and runtime.monitor_timer_active()
    )
    _validate_authorization(
        paths.authorization,
        expected_uid=expected_uid,
        commit=expected_commit,
        components=components,
        plan_sha256=plan_sha,
        source_mode=source_mode,
        now=datetime.now(UTC),
        rollback_releases=rollback_payload,
        controller_commit=controller_commit,
        controller_bundle_sha256=controller_bundle_sha256,
    )
    if dry_run:
        result: dict[str, Any] = {
            "status": "validated",
            "commit": expected_commit,
            "rollback_commit": (
                rollback_releases[components[0]].commit
                if rollback_payload is None and rollback_releases
                else None
            ),
            "components": components,
            "source_mode": source_mode,
            "authorization_consumed": False,
        }
        if rollback_payload is not None:
            result["rollback_releases"] = rollback_payload
        return result
    consume_activation_authorization(
        paths.authorization,
        paths.authorization_consumed,
    )

    mutation_started = False
    try:
        mutation_started = True
        if source_mode == "stopped_legacy":
            _require_stopped_legacy_boundary(runtime, components)
        else:
            _stop_units(runtime, components)
        if require_authority and runtime.active_write_count(paths.database_path) != 0:
            raise ActivationError("post-stop active exchange write is unknown or nonzero")
        publish_release_dropins(
            paths,
            candidate,
            components,
            entry_frozen=preserve_entry_freeze,
        )
        runtime.daemon_reload()
        validate_release(paths.release_root, expected_commit, expected_uid=expected_uid)
        if source_mode == "immutable":
            _revalidate_release_set(
                paths.release_root,
                rollback_releases,
                expected_uid=expected_uid,
            )
        if source_mode == "stopped_legacy":
            _unmask_units(runtime, components)
        _start_units(runtime, components)
        after_identities, _ = prove_release_runtime(
            runtime,
            release_root=paths.release_root,
            expected_uid=expected_uid,
            expected_releases={
                **before_releases,
                **{role: candidate for role in affected_runtime_roles},
            },
            components=components,
            require_authority=require_authority,
            require_entry_frozen=preserve_entry_freeze,
            now=datetime.now(UTC),
        )
        if source_mode == "immutable":
            _prove_restarted_processes(before_identities, after_identities, components)
            _prove_undeclared_processes_unchanged(
                before_identities,
                after_identities,
                components,
            )
        monitor_verification = _activate_monitor(runtime, components, candidate)
        if monitor_was_active or (
            source_mode == "stopped_legacy" and "monitor" in components
        ):
            runtime.start_unit("telegram-kol-monitor.timer")
    except Exception as exc:
        if not mutation_started:
            raise
        if source_mode == "stopped_legacy":
            try:
                _reinhibit_and_stop_all(runtime, components)
            except Exception as stop_exc:
                raise ActivationError(
                    "activation failed; maintenance_stop_failed"
                ) from stop_exc
            raise ActivationError("activation failed; maintenance_stopped") from exc
        try:
            _stop_units(runtime, components)
            _revalidate_release_set(
                paths.release_root,
                rollback_releases,
                expected_uid=expected_uid,
            )
            publish_component_release_dropins(
                paths,
                rollback_releases,
                components,
                entry_frozen=preserve_entry_freeze,
            )
            runtime.daemon_reload()
            _start_units(runtime, components)
            prove_release_runtime(
                runtime,
                release_root=paths.release_root,
                expected_uid=expected_uid,
                expected_releases=before_releases,
                components=components,
                require_authority=require_authority,
                require_entry_frozen=preserve_entry_freeze,
                now=datetime.now(UTC),
            )
            if "monitor" in components:
                _activate_monitor(runtime, components, rollback_releases["monitor"])
            if monitor_was_active or (
                source_mode == "stopped_legacy" and "monitor" in components
            ):
                runtime.start_unit("telegram-kol-monitor.timer")
        except Exception as rollback_exc:
            _best_effort_stop_units(runtime, components)
            raise ActivationError("activation failed; rollback_failed") from rollback_exc
        raise ActivationError("activation failed; rollback_complete") from exc

    result = {
        "status": "activated",
        "commit": expected_commit,
        "rollback_commit": (
            rollback_releases[components[0]].commit
            if source_mode == "immutable"
            and rollback_payload is None
            and rollback_releases
            else None
        ),
        "components": components,
        "source_mode": source_mode,
        "authorization_consumed": True,
        "monitor_verification": monitor_verification,
    }
    if rollback_payload is not None:
        result["rollback_releases"] = rollback_payload
    return result


_MONITOR_DIAGNOSTIC_UNIT = "telegram-kol-monitor-diagnostic.service"


def _best_effort_monitor_journal_evidence(output: str) -> dict[str, Any]:
    candidates: list[Mapping[str, Any]] = []
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if (
            isinstance(payload, Mapping)
            and payload.get("contract") == "monitor-deployment-diagnostic-v1"
        ):
            candidates.append(payload)
    if not candidates:
        return {
            "reason": "diagnostic_payload_not_found",
            "status": "unavailable",
        }
    return {
        "association": "best_effort_latest_unit_payload",
        "observed_payload_count": len(candidates),
        "payload": dict(candidates[-1]),
        "status": "available",
    }


def _emit_monitor_gate_evidence(evidence: Mapping[str, Any]) -> None:
    print(
        "MONITOR_ACTIVATION_GATE_EVIDENCE="
        + _canonical_json_bytes(evidence).decode("utf-8").rstrip("\n"),
        file=sys.stderr,
        flush=True,
    )


def _parse_monitor_systemd_status(output: str) -> tuple[str | None, str | None, bool]:
    properties: dict[str, str] = {}
    malformed = False
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in properties:
            malformed = True
            continue
        properties[key] = value
    malformed = malformed or set(properties) != {"Result", "ExecMainStatus"}
    return properties.get("Result"), properties.get("ExecMainStatus"), malformed


class SystemRuntimeAdapter:
    identity_retry_delay_seconds = 60
    _INHIBIT_NAME = "00-telegram-kol-maintenance-inhibit.conf"
    _INHIBIT_CONTENT = (
        b"[Unit]\n"
        b"ConditionPathExists=/dev/null/telegram-kol-maintenance-never\n"
    )
    _RUNTIME_MARKERS = (
        b"telegram-kol-research\x00web\x00",
        b"telegram-kol-research\x00monitor-production-safety\x00",
    )

    def __init__(
        self,
        *,
        python: Path = Path(sys.executable),
        dropin_root: Path = Path("/etc/systemd/system"),
        expected_uid: int | None = None,
    ) -> None:
        self.python = python
        self.dropin_root = Path(dropin_root)
        self.expected_uid = os.geteuid() if expected_uid is None else int(expected_uid)

    @staticmethod
    def _run(
        command: list[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
                env=environment,
            )
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            raise ActivationError("runtime command failed") from exc
        if result.returncode != 0:
            raise ActivationError("runtime command failed")
        return result

    def active_write_count(self, database_path: Path) -> int:
        result = self._run(
            [
                str(self.python),
                "-m",
                "telegram_kol_research.deployment_activation_quiescence_check",
                str(database_path),
            ]
        )
        match = re.fullmatch(
            r"active_write_count=(\d+) global_authority_state=idle\n?",
            result.stdout,
        )
        if match is None:
            raise ActivationError("active write evidence is malformed")
        return int(match.group(1))

    def runtime_identity(self, role: str) -> Mapping[str, Any]:
        request = Request(
            f"http://127.0.0.1:{_PORTS[role]}/api/runtime/deployment-identity",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise ActivationError("runtime identity proof failed")
                payload = json.loads(response.read(65_537).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ActivationError("runtime identity proof failed")
            unit = _UNITS[role][0]
            result = self._run(
                ["systemctl", "show", unit, "--property=MainPID", "--value"]
            )
            main_pid = int(result.stdout.strip())
            raw = Path(f"/proc/{main_pid}/stat").read_text(encoding="ascii")
            suffix = raw[raw.rindex(")") + 2 :].split()
            start_ticks = int(suffix[19])
            loaded_cwd = os.readlink(f"/proc/{main_pid}/cwd")
            command = Path(f"/proc/{main_pid}/cmdline").read_bytes()
            if loaded_cwd != payload.get("loaded_cwd") or not _command_matches_role(
                command,
                role=role,
            ):
                raise ActivationError("runtime identity proof failed")
        except Exception as exc:
            raise ActivationError("runtime identity proof failed") from exc
        payload["systemd_main_pid"] = main_pid
        payload["systemd_start_ticks"] = start_ticks
        return payload

    def stop_unit(self, unit: str) -> None:
        self._run(["systemctl", "stop", unit])

    def start_unit(self, unit: str) -> None:
        self._run(["systemctl", "start", unit])

    def maintenance_unit_state(self, unit: str) -> tuple[str, str]:
        active = self._run(
            ["systemctl", "show", unit, "--property=ActiveState", "--value"]
        ).stdout.strip()
        inhibit = self._inhibit_path(unit)
        if self._exact_inhibit_file(inhibit):
            pending = self._run(
                [
                    "systemctl",
                    "show",
                    unit,
                    "--property=NeedDaemonReload",
                    "--value",
                ]
            ).stdout.strip()
            if pending != "no":
                raise ActivationError("runtime command failed")
            return active, "inhibited"
        try:
            enabled_result = subprocess.run(
                ["systemctl", "is-enabled", unit],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            raise ActivationError("runtime command failed") from exc
        enabled = enabled_result.stdout.strip()
        if enabled_result.returncode not in {0, 1} or not active or not enabled:
            raise ActivationError("runtime command failed")
        return active, enabled

    def unmask_unit(self, unit: str) -> None:
        path = self._inhibit_path(unit)
        if path.exists() or path.is_symlink():
            if not self._exact_inhibit_file(path):
                raise ActivationError("maintenance inhibit file is unsafe")
            path.unlink()
            self._fsync_directory(path.parent)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        self._run(["systemctl", "daemon-reload"])

    def mask_unit(self, unit: str) -> None:
        path = self._inhibit_path(unit)
        path.parent.mkdir(mode=0o755, parents=False, exist_ok=True)
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != self.expected_uid
            or stat.S_IMODE(parent.st_mode) != 0o755
        ):
            raise ActivationError("maintenance inhibit directory is unsafe")
        if path.exists() or path.is_symlink():
            if not self._exact_inhibit_file(path):
                raise ActivationError("maintenance inhibit file is unsafe")
        else:
            temporary = path.with_name(
                f".{self._INHIBIT_NAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o444)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(self._INHIBIT_CONTENT)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                self._fsync_directory(path.parent)
            except Exception:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
        self._run(["systemctl", "daemon-reload"])

    def _inhibit_path(self, unit: str) -> Path:
        allowed = {
            *_controlled_units(list(_AUTHORITY_RUNTIME_SCOPE)),
            *_LEGACY_SOURCE_UNITS,
        }
        if unit not in allowed:
            raise ActivationError("maintenance unit is invalid")
        return self.dropin_root / f"{unit}.d" / self._INHIBIT_NAME

    def _exact_inhibit_file(self, path: Path) -> bool:
        try:
            metadata = path.lstat()
            return bool(
                stat.S_ISREG(metadata.st_mode)
                and not stat.S_ISLNK(metadata.st_mode)
                and metadata.st_uid == self.expected_uid
                and stat.S_IMODE(metadata.st_mode) == 0o444
                and metadata.st_nlink == 1
                and metadata.st_size == len(self._INHIBIT_CONTENT)
                and path.read_bytes() == self._INHIBIT_CONTENT
            )
        except OSError:
            return False

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def main_pid(self, unit: str) -> int:
        raw = self._run(
            ["systemctl", "show", unit, "--property=MainPID", "--value"]
        ).stdout.strip()
        if not raw and unit in _PROCESSLESS_UNITS:
            return 0
        try:
            value = int(raw)
        except ValueError as exc:
            raise ActivationError("runtime command failed") from exc
        if value < 0:
            raise ActivationError("runtime command failed")
        return value

    def cgroup_pids(self, unit: str) -> tuple[int, ...]:
        group = self._run(
            ["systemctl", "show", unit, "--property=ControlGroup", "--value"]
        ).stdout.strip()
        if not group:
            return ()
        if not group.startswith("/") or ".." in Path(group).parts:
            raise ActivationError("runtime command failed")
        try:
            values = (
                Path("/sys/fs/cgroup")
                .joinpath(group.lstrip("/"), "cgroup.procs")
                .read_text(encoding="ascii")
                .split()
            )
            return tuple(sorted({int(value) for value in values}))
        except (OSError, ValueError) as exc:
            raise ActivationError("runtime command failed") from exc

    def matching_processes(self) -> tuple[int, ...]:
        matches = []
        try:
            candidates = tuple(Path("/proc").iterdir())
        except OSError as exc:
            raise ActivationError("runtime command failed") from exc
        for candidate in candidates:
            if not candidate.name.isdigit():
                continue
            try:
                command = (candidate / "cmdline").read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError as exc:
                raise ActivationError("runtime command failed") from exc
            if any(marker in command for marker in self._RUNTIME_MARKERS):
                matches.append(int(candidate.name))
        return tuple(sorted(matches))

    def daemon_reload(self) -> None:
        self._run(["systemctl", "daemon-reload"])

    def monitor_timer_active(self) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "telegram-kol-monitor.timer"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ActivationError("monitor timer state is unknown") from exc
        state = result.stdout.strip()
        if result.returncode == 0 and state == "active":
            return True
        if result.returncode == 3 and state == "inactive":
            return False
        raise ActivationError("monitor timer state is unknown")

    def prove_monitor_rollback_release(
        self, release: ReleaseEvidence
    ) -> Mapping[str, Any]:
        units = (*_UNITS["monitor"], "telegram-kol-monitor.timer")
        observed_units: dict[str, dict[str, Any]] = {}
        paths: set[Path] = set()
        for unit in units:
            result = self._run(
                [
                    "systemctl",
                    "show",
                    unit,
                    "--property=Environment",
                    "--property=FragmentPath",
                    "--property=DropInPaths",
                ]
            )
            properties: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator and key in {"Environment", "FragmentPath", "DropInPaths"}:
                    if key in properties:
                        raise ActivationError("monitor rollback identity proof failed")
                    properties[key] = value
            required_properties = {"FragmentPath", "DropInPaths"}
            if unit not in _PROCESSLESS_UNITS:
                # Timer units have no exec context, so systemd does not expose
                # Environment for them. They still participate in file-mtime proof.
                required_properties.add("Environment")
            if not required_properties.issubset(properties):
                raise ActivationError("monitor rollback identity proof failed")
            unit_paths = [properties["FragmentPath"], *shlex.split(properties["DropInPaths"])]
            if not properties["FragmentPath"]:
                raise ActivationError("monitor rollback identity proof failed")
            environment: dict[str, str] = {}
            if unit not in _PROCESSLESS_UNITS:
                for assignment in shlex.split(properties["Environment"]):
                    key, separator, value = assignment.partition("=")
                    if separator:
                        if key in environment:
                            raise ActivationError("monitor rollback identity proof failed")
                        environment[key] = value
            if unit in _UNITS["monitor"]:
                expected = {
                    "TELEGRAM_KOL_RELEASE_COMMIT": release.commit,
                    "TELEGRAM_KOL_RELEASE_MANIFEST_SHA256": release.manifest_sha256,
                    "TELEGRAM_KOL_MONITOR_RELEASE_PATH": str(release.release_path),
                    "TELEGRAM_KOL_MONITOR_RELEASE_COMMIT": release.commit,
                    "TELEGRAM_KOL_MONITOR_RELEASE_MANIFEST_SHA256": release.manifest_sha256,
                }
                if any(environment.get(key) != value for key, value in expected.items()):
                    raise ActivationError("monitor rollback identity proof failed")
            for raw_path in unit_paths:
                path = Path(raw_path)
                if not path.is_absolute():
                    raise ActivationError("monitor rollback identity proof failed")
                paths.add(path)
            observed_units[unit] = {
                "dropin_paths": shlex.split(properties["DropInPaths"]),
                "fragment_path": properties["FragmentPath"],
            }

        latest_mtime_ns = 0
        for path in paths:
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ActivationError("monitor rollback identity proof failed") from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
            ):
                raise ActivationError("monitor rollback identity proof failed")
            latest_mtime_ns = max(latest_mtime_ns, metadata.st_mtime_ns)

        status = self._run(
            [
                "systemctl",
                "show",
                _MONITOR_DIAGNOSTIC_UNIT,
                "--property=Result",
                "--property=ExecMainStatus",
            ]
        )
        systemd_result, exec_main_status, malformed = _parse_monitor_systemd_status(
            status.stdout
        )
        journal = self._best_effort_monitor_journal()
        payload = journal.get("payload") if isinstance(journal, Mapping) else None
        if (
            malformed
            or systemd_result != "success"
            or exec_main_status != "0"
            or journal.get("status") != "available"
            or not isinstance(payload, Mapping)
            or payload.get("contract") != "monitor-deployment-diagnostic-v1"
            or payload.get("release_commit") != release.commit
            or payload.get("manifest_sha256") != release.manifest_sha256
            or payload.get("loaded_artifact_verified") is not True
            or payload.get("result_complete") is not True
            or payload.get("sources_complete") is not True
        ):
            raise ActivationError("monitor rollback identity proof failed")
        try:
            checked_at = _parse_time(payload.get("checked_at"))
        except ActivationError as exc:
            raise ActivationError("monitor rollback identity proof failed") from exc
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        elapsed = checked_at.astimezone(UTC) - epoch
        checked_at_ns = (
            (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000_000
            + elapsed.microseconds * 1_000
        )
        if checked_at_ns <= latest_mtime_ns:
            raise ActivationError("monitor rollback identity proof failed")
        return {
            "contract": "monitor-rollback-identity-v1",
            "diagnostic_checked_at": checked_at.isoformat(),
            "latest_configuration_mtime_ns": latest_mtime_ns,
            "manifest_sha256": release.manifest_sha256,
            "release_commit": release.commit,
            "units": observed_units,
        }

    @staticmethod
    def _best_effort_monitor_journal() -> dict[str, Any]:
        command = [
            "journalctl",
            "-u",
            _MONITOR_DIAGNOSTIC_UNIT,
            "--lines=100",
            "--output=cat",
            "--no-pager",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return {
                "reason": "journal_command_timeout",
                "status": "unavailable",
            }
        except OSError:
            return {
                "reason": "journal_command_unavailable",
                "status": "unavailable",
            }
        except Exception:
            return {
                "reason": "journal_evidence_exception",
                "status": "unavailable",
            }
        if result.returncode != 0:
            return {
                "reason": "journal_command_nonzero",
                "returncode": result.returncode,
                "status": "unavailable",
            }
        try:
            return _best_effort_monitor_journal_evidence(result.stdout)
        except Exception:
            return {
                "reason": "journal_evidence_exception",
                "status": "unavailable",
            }

    def verify_monitor_release(
        self, release: ReleaseEvidence
    ) -> Mapping[str, Any]:
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(release.release_path / "src"),
                "TELEGRAM_KOL_RELEASE_COMMIT": release.commit,
                "TELEGRAM_KOL_RELEASE_MANIFEST_SHA256": release.manifest_sha256,
                "TELEGRAM_KOL_RUNTIME_ROLE": "monitor",
            }
        )
        prechecks = (
            (
                "runtime_identity_precheck",
                "runtime_identity_precheck_failed",
                [
                    str(self.python),
                    "-m",
                    "telegram_kol_research.runtime_deployment_identity",
                ],
                environment,
            ),
            (
                "systemd_unit_precheck",
                "systemd_unit_precheck_failed",
                [
                    "systemd-analyze",
                    "verify",
                    *_UNITS["monitor"],
                    "telegram-kol-monitor.timer",
                ],
                None,
            ),
        )
        for basis, reason, command, command_environment in prechecks:
            try:
                self._run(command, environment=command_environment)
            except ActivationError:
                evidence = {
                    "contract": "monitor-activation-gate-evidence-v1",
                    "decision": {
                        "basis": basis,
                        "exec_main_status": None,
                        "passed": False,
                        "reason": reason,
                        "start_returncode": None,
                        "systemd_result": None,
                    },
                    "journal_evidence": {
                        "reason": "diagnostic_not_started",
                        "status": "unavailable",
                    },
                    "schema_version": 1,
                }
                _emit_monitor_gate_evidence(evidence)
                raise
        try:
            start_result = subprocess.run(
                ["systemctl", "start", _MONITOR_DIAGNOSTIC_UNIT],
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            start_result = None
            start_failure = exc
        else:
            start_failure = None
        if start_failure is not None or start_result.returncode != 0:
            try:
                status = self._run(
                    [
                        "systemctl",
                        "show",
                        _MONITOR_DIAGNOSTIC_UNIT,
                        "--property=Result",
                        "--property=ExecMainStatus",
                    ]
                )
                systemd_result, exec_main_status, status_malformed = (
                    _parse_monitor_systemd_status(status.stdout)
                )
                status_observation = (
                    "malformed" if status_malformed else "available"
                )
            except ActivationError:
                systemd_result = None
                exec_main_status = None
                status_observation = "unavailable"
            start_observation = (
                "timeout"
                if isinstance(start_failure, subprocess.TimeoutExpired)
                else "unavailable"
                if isinstance(start_failure, (OSError, UnicodeError))
                else "nonzero_or_signal"
            )
            evidence = {
                "contract": "monitor-activation-gate-evidence-v1",
                "decision": {
                    "basis": "systemctl_start_and_systemd_result_exec_main_status",
                    "exec_main_status": exec_main_status,
                    "passed": False,
                    "reason": "diagnostic_start_failed",
                    "start_observation": start_observation,
                    "start_returncode": (
                        None if start_result is None else start_result.returncode
                    ),
                    "systemd_result": systemd_result,
                    "systemd_status_observation": status_observation,
                },
                "journal_evidence": self._best_effort_monitor_journal(),
                "schema_version": 1,
            }
            _emit_monitor_gate_evidence(evidence)
            raise ActivationError("runtime command failed") from start_failure
        try:
            result = self._run(
                [
                    "systemctl",
                    "show",
                    _MONITOR_DIAGNOSTIC_UNIT,
                    "--property=Result",
                    "--property=ExecMainStatus",
                ]
            )
        except ActivationError:
            evidence = {
                "contract": "monitor-activation-gate-evidence-v1",
                "decision": {
                    "basis": "systemctl_start_and_systemd_result_exec_main_status",
                    "exec_main_status": None,
                    "passed": False,
                    "reason": "systemd_status_unavailable",
                    "start_returncode": start_result.returncode,
                    "systemd_result": None,
                    "systemd_status_observation": "unavailable",
                },
                "journal_evidence": self._best_effort_monitor_journal(),
                "schema_version": 1,
            }
            _emit_monitor_gate_evidence(evidence)
            raise

        systemd_result, exec_main_status, malformed = _parse_monitor_systemd_status(
            result.stdout
        )
        passed = bool(
            not malformed
            and systemd_result == "success"
            and exec_main_status == "0"
        )
        decision: dict[str, Any] = {
            "basis": "systemctl_start_and_systemd_result_exec_main_status",
            "exec_main_status": exec_main_status,
            "passed": passed,
            "start_returncode": start_result.returncode,
            "systemd_result": systemd_result,
        }
        if not passed:
            decision["reason"] = (
                "systemd_status_malformed" if malformed else "diagnostic_exit_failed"
            )
        evidence = {
            "contract": "monitor-activation-gate-evidence-v1",
            "decision": decision,
            "journal_evidence": self._best_effort_monitor_journal(),
            "schema_version": 1,
        }
        _emit_monitor_gate_evidence(evidence)
        if not passed:
            raise ActivationError("monitor runtime proof failed")
        return evidence


def _required_absolute_env(name: str, default: str | None = None) -> Path:
    raw = os.environ.get(name, default or "")
    if not raw:
        raise ActivationError(f"{name} is required")
    path = Path(raw)
    if not path.is_absolute():
        raise ActivationError(f"{name} must be absolute")
    return path


def _activation_source_mode() -> str:
    source_mode = os.environ.get("ACTIVATION_SOURCE_MODE", "immutable")
    if source_mode not in {"immutable", "stopped_legacy"}:
        raise ActivationError("activation source mode is invalid")
    return source_mode


def _activation_dry_run() -> bool:
    raw = os.environ.get("ACTIVATION_DRY_RUN", "0")
    if raw not in {"0", "1"}:
        raise ActivationError("activation dry-run mode is invalid")
    return raw == "1"


def main() -> int:
    try:
        expected_commit = os.environ.get("EXPECTED_COMMIT", "").lower()
        rollback_commit = os.environ.get("ROLLBACK_COMMIT", "").lower()
        controller_commit = os.environ.get("ACTIVATION_CONTROLLER_COMMIT", "").lower()
        controller_bundle_sha256 = os.environ.get(
            "ACTIVATION_CONTROLLER_BUNDLE_SHA256", ""
        ).lower()
        source_mode = _activation_source_mode()
        dry_run = _activation_dry_run()
        if not _SHA1_RE.fullmatch(expected_commit):
            raise ActivationError("EXPECTED_COMMIT must be a full SHA")
        release_root = _required_absolute_env(
            "RELEASE_ROOT", "/opt/telegram-kol-releases"
        )
        action_manifest = _required_absolute_env("ACTION_MANIFEST")
        paths = ActivationPaths(
            release_root=release_root,
            action_manifest=action_manifest,
            authorization=_required_absolute_env("ACTIVATION_AUTHORIZATION"),
            authorization_consumed=_required_absolute_env(
                "ACTIVATION_AUTHORIZATION_CONSUMED"
            ),
            dropin_root=_required_absolute_env(
                "SERVICE_DROPIN_ROOT", "/etc/systemd/system"
            ),
            database_path=_required_absolute_env(
                "DATABASE_PATH", "/opt/telegram-kol-analyzer/data/research.db"
            ),
        )
        parsed_manifest, _, _ = _read_manifest(action_manifest)
        if source_mode == "immutable":
            if parsed_manifest.rollback_releases:
                if rollback_commit:
                    raise ActivationError(
                        "ROLLBACK_COMMIT is prohibited with per-role rollback releases"
                    )
                if not _SHA1_RE.fullmatch(controller_commit) or not _SHA256_RE.fullmatch(
                    controller_bundle_sha256
                ):
                    raise ActivationError("activation controller identity is invalid")
            elif not _SHA1_RE.fullmatch(rollback_commit):
                raise ActivationError("ROLLBACK_COMMIT must be a full SHA")
        test_mode = os.environ.get("ACTIVATOR_TEST_MODE", "0")
        if test_mode not in {"0", "1"}:
            raise ActivationError("ACTIVATOR_TEST_MODE must be 0 or 1")
        expected_uid = os.geteuid()
        if test_mode == "0" and expected_uid != 0:
            raise ActivationError("activation must run as root")
        root_metadata = release_root.lstat()
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or root_metadata.st_uid != expected_uid
            or stat.S_IMODE(root_metadata.st_mode) & 0o022
        ):
            raise ActivationError("release root ownership or mode is unsafe")
        lock_path = (
            _required_absolute_env("ACTIVATOR_LOCK_PATH")
            if test_mode == "1" and os.environ.get("ACTIVATOR_LOCK_PATH")
            else Path("/run/telegram-kol-update.lock")
        )
        with exclusive_runtime_control_lock(
            lock_path=lock_path,
            expected_uid=expected_uid,
        ):
            result = activate_release(
                expected_commit=expected_commit,
                rollback_commit=rollback_commit,
                paths=paths,
                runtime=SystemRuntimeAdapter(
                    dropin_root=paths.dropin_root,
                    expected_uid=expected_uid,
                ),
                expected_uid=expected_uid,
                source_mode=source_mode,
                controller_commit=controller_commit or None,
                controller_bundle_sha256=controller_bundle_sha256 or None,
                dry_run=dry_run,
            )
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 0
    except ActivationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    except (OSError, ValueError):
        print("ERROR: activation failed", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
