from __future__ import annotations

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


def _collect_args(inputs: dict[str, Path], output: Path) -> list[str]:
    return [
        "collect",
        "--phase",
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
