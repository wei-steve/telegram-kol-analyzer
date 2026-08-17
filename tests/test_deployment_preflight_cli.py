from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import TradeSignal


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = "2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa"


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "telegram_kol_research.deployment_preflight_cli",
            *args,
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _changed_writer_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "writer-repository"
    writer = repository / "src/telegram_kol_research/deepcoin_client.py"
    writer.parent.mkdir(parents=True)
    repository.mkdir(exist_ok=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Gate Test")
    _git(repository, "config", "user.email", "gate@example.invalid")
    writer.write_text("WRITER = 'production'\n", encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "production")
    production = _git(repository, "rev-parse", "HEAD")
    writer.write_text("WRITER = 'candidate'\n", encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "candidate")
    return repository, production, _git(repository, "rev-parse", "HEAD")


def _inputs(tmp_path: Path, *, trade_status: str | None = None) -> dict[str, Path]:
    database = tmp_path / "gate.db"
    session_factory = create_session_factory(database)
    if trade_status is not None:
        with session_factory() as session:
            session.add(
                TradeSignal(
                    signal_uid=f"gate-{trade_status}",
                    source_type="recovery",
                    venue="deepcoin",
                    kol_id="redacted",
                    chat_id=1,
                    message_id=2,
                    symbol="BTC",
                    side="long",
                    action="open_position",
                    status=trade_status,
                    payload_json="{}",
                )
            )
            session.commit()
    session_factory.kw["bind"].dispose()

    snapshot = tmp_path / "snapshot.json"
    schema = tmp_path / "schema.json"
    watermark = tmp_path / "watermark.json"
    snapshot.write_text(
        json.dumps({"complete": True, "protected_live_positions": 0}),
        encoding="utf-8",
    )
    schema.write_text(
        json.dumps(
            {
                "backup_verified": False,
                "migration_dry_run_verified": False,
                "watermark_verified": False,
            }
        ),
        encoding="utf-8",
    )
    watermark.write_text(
        json.dumps({"raw_messages": 0, "execution_events": 0}),
        encoding="utf-8",
    )
    return {
        "database": database,
        "snapshot": snapshot,
        "schema": schema,
        "watermark": watermark,
    }


def _collect_args(
    inputs: dict[str, Path],
    output: Path,
    *,
    repository: Path = ROOT,
    production_commit: str = PRODUCTION,
    candidate_commit: str | None = None,
) -> list[str]:
    return [
        "collect",
        "--phase",
        "preliminary",
        "--repository",
        str(repository),
        "--production-commit",
        production_commit,
        "--candidate-commit",
        candidate_commit or _head(),
        "--database-path",
        str(inputs["database"]),
        "--snapshot-status",
        str(inputs["snapshot"]),
        "--schema-verification",
        str(inputs["schema"]),
        "--database-watermark",
        str(inputs["watermark"]),
        "--output",
        str(output),
        "--now",
        "2026-08-17T08:00:00+00:00",
    ]


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("collect",),
        ("collect", "--phase", "bogus"),
        ("verify", "--expected-phase", "bogus"),
        ("unknown-command",),
    ],
)
def test_argparse_errors_return_invalid_not_warn(args: tuple[str, ...]) -> None:
    result = _run(*args)

    assert result.returncode == 4
    assert result.returncode != 2


def test_surface_command_returns_only_sanitized_facts() -> None:
    result = _run(
        "surface",
        "--repository",
        str(ROOT),
        "--production-commit",
        PRODUCTION,
        "--candidate-commit",
        _head(),
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "manifest_version",
        "production_writer_fingerprint",
        "candidate_writer_fingerprint",
        "writer_changed",
        "schema_changed",
        "changed_path_count",
    }
    assert str(ROOT) not in result.stdout


@pytest.mark.parametrize(
    ("trade_status", "expected_code", "expected_decision"),
    [
        (None, 0, "PASS"),
        ("pending", 2, "WARN"),
        ("unknown_exchange_outcome", 2, "WARN"),
        ("processing", 3, "BLOCK"),
    ],
)
def test_collect_returns_stable_decision_codes(
    tmp_path: Path,
    trade_status: str | None,
    expected_code: int,
    expected_decision: str,
) -> None:
    inputs = _inputs(tmp_path, trade_status=trade_status)
    output = tmp_path / "artifact.json"

    result = _run(*_collect_args(inputs, output))

    assert result.returncode == expected_code
    assert f"decision={expected_decision}" in result.stdout
    assert output.is_file()
    assert output.stat().st_mode & 0o777 == 0o600


def test_changed_writer_unknown_returns_block_exit_code(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, trade_status="unknown_exchange_outcome")
    repository, production, candidate = _changed_writer_repository(tmp_path)
    output = tmp_path / "changed-writer-unknown.json"

    result = _run(
        *_collect_args(
            inputs,
            output,
            repository=repository,
            production_commit=production,
            candidate_commit=candidate,
        )
    )

    assert result.returncode == 3
    assert "decision=BLOCK" in result.stdout
    assert "writer_changed_with_unknown_outcome" in result.stdout


def test_unchanged_writer_unknown_collect_and_verify_are_warn(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path, trade_status="unknown_exchange_outcome")
    artifact = tmp_path / "unknown-warn.json"
    collected = _run(*_collect_args(inputs, artifact))
    assert collected.returncode == 2

    verified = _run(
        "verify",
        "--expected-phase",
        "preliminary",
        "--repository",
        str(ROOT),
        "--production-commit",
        PRODUCTION,
        "--candidate-commit",
        _head(),
        "--database-path",
        str(inputs["database"]),
        "--snapshot-status",
        str(inputs["snapshot"]),
        "--schema-verification",
        str(inputs["schema"]),
        "--database-watermark",
        str(inputs["watermark"]),
        "--input",
        str(artifact),
        "--now",
        "2026-08-17T08:00:00+00:00",
    )

    assert verified.returncode == 2
    assert "decision=WARN" in verified.stdout
    assert "unknown_outcome_with_unchanged_writer" in verified.stdout


def test_verify_recollects_and_verifies_exact_facts(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    artifact = tmp_path / "artifact.json"
    collected = _run(*_collect_args(inputs, artifact))
    assert collected.returncode == 0

    result = _run(
        "verify",
        "--expected-phase",
        "preliminary",
        "--repository",
        str(ROOT),
        "--production-commit",
        PRODUCTION,
        "--candidate-commit",
        _head(),
        "--database-path",
        str(inputs["database"]),
        "--snapshot-status",
        str(inputs["snapshot"]),
        "--schema-verification",
        str(inputs["schema"]),
        "--database-watermark",
        str(inputs["watermark"]),
        "--input",
        str(artifact),
        "--now",
        "2026-08-17T08:00:00+00:00",
    )

    assert result.returncode == 0
    assert "decision=PASS" in result.stdout


def test_final_requires_saved_preliminary_fingerprint(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    preliminary_path = tmp_path / "preliminary.json"
    assert _run(*_collect_args(inputs, preliminary_path)).returncode == 0
    preliminary = json.loads(preliminary_path.read_text(encoding="utf-8"))
    final_path = tmp_path / "final.json"
    final_args = [
        *_collect_args(inputs, final_path),
        "--preliminary-artifact",
        str(preliminary_path),
    ]
    phase_index = final_args.index("preliminary")
    final_args[phase_index] = "final"

    missing_fingerprint = _run(*final_args)
    assert missing_fingerprint.returncode == 4

    collected = _run(
        *final_args,
        "--preliminary-fingerprint",
        str(preliminary["fingerprint"]),
    )
    assert collected.returncode == 0
    assert "decision=PASS" in collected.stdout


def test_final_rejects_parent_resigned_after_phase_a(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    preliminary_path = tmp_path / "preliminary.json"
    assert _run(*_collect_args(inputs, preliminary_path)).returncode == 0
    preliminary = json.loads(preliminary_path.read_text(encoding="utf-8"))
    saved_fingerprint = str(preliminary["fingerprint"])
    preliminary["evidence_counts"]["inactive"] = 1
    payload = dict(preliminary)
    payload.pop("fingerprint")
    preliminary["fingerprint"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    preliminary_path.write_text(json.dumps(preliminary), encoding="utf-8")
    final_path = tmp_path / "final.json"
    final_args = [
        *_collect_args(inputs, final_path),
        "--preliminary-artifact",
        str(preliminary_path),
        "--preliminary-fingerprint",
        saved_fingerprint,
    ]
    final_args[final_args.index("preliminary")] = "final"

    result = _run(*final_args)

    assert result.returncode == 4
    assert not final_path.exists()


def test_unreadable_artifact_and_invalid_json_return_four(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    missing = tmp_path / "secret-artifact-name.json"
    result = _run(
        "verify",
        "--expected-phase",
        "preliminary",
        "--repository",
        str(ROOT),
        "--production-commit",
        PRODUCTION,
        "--candidate-commit",
        _head(),
        "--database-path",
        str(inputs["database"]),
        "--snapshot-status",
        str(inputs["snapshot"]),
        "--schema-verification",
        str(inputs["schema"]),
        "--database-watermark",
        str(inputs["watermark"]),
        "--input",
        str(missing),
    )

    assert result.returncode == 4
    assert "secret-artifact-name" not in result.stderr

    inputs["snapshot"].write_text("not-json secret-payload", encoding="utf-8")
    result = _run(*_collect_args(inputs, tmp_path / "output.json"))
    assert result.returncode == 4
    assert "secret-payload" not in result.stderr
