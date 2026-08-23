"""Atomic, no-fallback context-resolution model authority cutover."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .ai_recognition_config import (
    AiRecognitionConfig,
    load_ai_recognition_config,
    save_ai_recognition_config,
)


@dataclass(frozen=True)
class ContextAuthorityCutoverReceipt:
    mode: str
    before_sha256: str
    after_sha256: str
    old_model_id: str
    new_model_id: str
    backup_path: str | None


def plan_context_authority_cutover(
    config_path: str | Path,
    *,
    new_model_id: str,
    expected_old_model_id: str,
) -> ContextAuthorityCutoverReceipt:
    path = Path(config_path)
    before = path.read_bytes()
    config = load_ai_recognition_config(path)
    _validate_old_and_new_models(
        config,
        expected_old_model_id=expected_old_model_id,
        new_model_id=new_model_id,
    )
    temporary, candidate = _prepare_candidate(
        source_path=path,
        destination_path=path,
        config=config,
        new_model_id=new_model_id,
    )
    try:
        return ContextAuthorityCutoverReceipt(
            mode="dry_run",
            before_sha256=_sha256(before),
            after_sha256=_sha256(candidate),
            old_model_id=config.context_resolution_model_id,
            new_model_id=new_model_id,
            backup_path=None,
        )
    finally:
        temporary.unlink(missing_ok=True)


def apply_context_authority_cutover(
    config_path: str | Path,
    *,
    new_model_id: str,
    expected_before_sha256: str,
    expected_old_model_id: str,
    backup_path: str | Path,
) -> ContextAuthorityCutoverReceipt:
    path = Path(config_path)
    backup = Path(backup_path)
    current = path.read_bytes()
    current_sha = _sha256(current)

    if current_sha != expected_before_sha256:
        repeated = _match_repeated_apply(
            path=path,
            backup=backup,
            current=current,
            expected_before_sha256=expected_before_sha256,
            expected_old_model_id=expected_old_model_id,
            new_model_id=new_model_id,
        )
        if repeated is not None:
            return repeated
        raise ValueError("before SHA mismatch")

    config = load_ai_recognition_config(path)
    _validate_old_and_new_models(
        config,
        expected_old_model_id=expected_old_model_id,
        new_model_id=new_model_id,
    )
    temporary, candidate = _prepare_candidate(
        source_path=path,
        destination_path=path,
        config=config,
        new_model_id=new_model_id,
    )
    try:
        _write_exact_backup(backup, current, source_stat=path.stat())
        os.replace(os.fspath(temporary), os.fspath(path))
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    return ContextAuthorityCutoverReceipt(
        mode="applied",
        before_sha256=current_sha,
        after_sha256=_sha256(candidate),
        old_model_id=config.context_resolution_model_id,
        new_model_id=new_model_id,
        backup_path=str(backup),
    )


def rollback_context_authority_cutover(
    config_path: str | Path,
    *,
    backup_path: str | Path,
    expected_current_sha256: str,
    expected_backup_sha256: str,
) -> ContextAuthorityCutoverReceipt:
    path = Path(config_path)
    backup = Path(backup_path)
    current = path.read_bytes()
    backup_bytes = backup.read_bytes()
    current_sha = _sha256(current)
    backup_sha = _sha256(backup_bytes)
    if current_sha != expected_current_sha256:
        raise ValueError("current SHA mismatch")
    if backup_sha != expected_backup_sha256:
        raise ValueError("backup SHA mismatch")

    current_config = load_ai_recognition_config(path)
    backup_config = load_ai_recognition_config(backup)
    temporary = _write_temporary_bytes(
        destination_path=path,
        content=backup_bytes,
        source_stat=backup.stat(),
    )
    try:
        os.replace(os.fspath(temporary), os.fspath(path))
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    return ContextAuthorityCutoverReceipt(
        mode="rolled_back",
        before_sha256=current_sha,
        after_sha256=backup_sha,
        old_model_id=current_config.context_resolution_model_id,
        new_model_id=backup_config.context_resolution_model_id,
        backup_path=str(backup),
    )


def _match_repeated_apply(
    *,
    path: Path,
    backup: Path,
    current: bytes,
    expected_before_sha256: str,
    expected_old_model_id: str,
    new_model_id: str,
) -> ContextAuthorityCutoverReceipt | None:
    if not backup.is_file():
        return None
    backup_bytes = backup.read_bytes()
    if _sha256(backup_bytes) != expected_before_sha256:
        return None
    backup_config = load_ai_recognition_config(backup)
    _validate_old_and_new_models(
        backup_config,
        expected_old_model_id=expected_old_model_id,
        new_model_id=new_model_id,
    )
    temporary, candidate = _prepare_candidate(
        source_path=backup,
        destination_path=path,
        config=backup_config,
        new_model_id=new_model_id,
    )
    try:
        if candidate != current:
            return None
    finally:
        temporary.unlink(missing_ok=True)
    return ContextAuthorityCutoverReceipt(
        mode="already_applied",
        before_sha256=expected_before_sha256,
        after_sha256=_sha256(current),
        old_model_id=expected_old_model_id,
        new_model_id=new_model_id,
        backup_path=str(backup),
    )


def _validate_old_and_new_models(
    config: AiRecognitionConfig,
    *,
    expected_old_model_id: str,
    new_model_id: str,
) -> None:
    if config.context_resolution_model_id != expected_old_model_id:
        raise ValueError("old model mismatch")
    candidate = next(
        (model for model in config.ai_models if model.id == new_model_id),
        None,
    )
    if candidate is None:
        raise ValueError("new model does not exist")
    if not candidate.supports_text:
        raise ValueError("new model must support text")
    if not candidate.provider.is_configured:
        raise ValueError("new model is not configured")


def _prepare_candidate(
    *,
    source_path: Path,
    destination_path: Path,
    config: AiRecognitionConfig,
    new_model_id: str,
) -> tuple[Path, bytes]:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.cutover-",
        suffix=".tmp",
        dir=destination_path.parent,
    )
    os.close(descriptor)
    temporary = Path(name)
    try:
        saved = save_ai_recognition_config(
            temporary,
            replace(config, context_resolution_model_id=new_model_id),
        )
        if saved.context_resolution_model_id != new_model_id:
            raise ValueError("new model normalization mismatch")
        _preserve_metadata(temporary, source_path.stat())
        _fsync_file(temporary)
        return temporary, temporary.read_bytes()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_exact_backup(path: Path, content: bytes, *, source_stat: os.stat_result) -> None:
    if path.exists():
        raise FileExistsError(f"backup already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    _preserve_metadata(path, source_stat)
    _fsync_directory(path.parent)


def _write_temporary_bytes(
    *,
    destination_path: Path,
    content: bytes,
    source_stat: os.stat_result,
) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.rollback-",
        suffix=".tmp",
        dir=destination_path.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _preserve_metadata(temporary, source_stat)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _preserve_metadata(path: Path, source_stat: os.stat_result) -> None:
    os.chmod(path, stat.S_IMODE(source_stat.st_mode))
    try:
        os.chown(path, source_stat.st_uid, source_stat.st_gid)
    except PermissionError:
        pass


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Atomically switch context-resolution model authority."
    )
    parser.add_argument("config_path")
    parser.add_argument("--new-model-id")
    parser.add_argument("--expected-old-model")
    effects = parser.add_mutually_exclusive_group()
    effects.add_argument("--apply", action="store_true")
    effects.add_argument("--rollback", action="store_true")
    parser.add_argument("--expected-before-sha")
    parser.add_argument("--backup-path")
    parser.add_argument("--expected-current-sha")
    parser.add_argument("--expected-backup-sha")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            if not all(
                (
                    args.backup_path,
                    args.expected_current_sha,
                    args.expected_backup_sha,
                )
            ):
                parser.error(
                    "rollback requires --backup-path, --expected-current-sha, "
                    "and --expected-backup-sha"
                )
            receipt = rollback_context_authority_cutover(
                args.config_path,
                backup_path=args.backup_path,
                expected_current_sha256=args.expected_current_sha,
                expected_backup_sha256=args.expected_backup_sha,
            )
        elif args.apply:
            if not all(
                (
                    args.new_model_id,
                    args.expected_old_model,
                    args.expected_before_sha,
                    args.backup_path,
                )
            ):
                parser.error(
                    "apply requires --new-model-id, --expected-old-model, "
                    "--expected-before-sha, and --backup-path"
                )
            receipt = apply_context_authority_cutover(
                args.config_path,
                new_model_id=args.new_model_id,
                expected_before_sha256=args.expected_before_sha,
                expected_old_model_id=args.expected_old_model,
                backup_path=args.backup_path,
            )
        else:
            if not args.new_model_id or not args.expected_old_model:
                parser.error(
                    "dry-run requires --new-model-id and --expected-old-model"
                )
            receipt = plan_context_authority_cutover(
                args.config_path,
                new_model_id=args.new_model_id,
                expected_old_model_id=args.expected_old_model,
            )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(asdict(receipt), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
