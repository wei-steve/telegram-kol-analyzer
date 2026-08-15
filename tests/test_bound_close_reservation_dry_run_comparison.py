from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path

import pytest

from telegram_kol_research.bound_close_reservation_recovery import (
    BoundCloseReservationObservation,
    ReservationClassification,
    build_bound_close_reservation_recovery_plan,
    serialize_bound_close_reservation_recovery_plan,
)


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "compare_bound_close_reservation_dry_runs.py"
)
_SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "compare_bound_close_reservation_dry_runs",
    _SCRIPT_PATH,
)
assert _SCRIPT_SPEC is not None and _SCRIPT_SPEC.loader is not None
_SCRIPT_MODULE = importlib.util.module_from_spec(_SCRIPT_SPEC)
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)
main = _SCRIPT_MODULE.main


START = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
FP_A = "a" * 64
FP_B = "b" * 64
FP_C = "c" * 64
FP_D = "d" * 64


def _observation(
    *,
    reservation_ref: str = FP_A,
    classification: ReservationClassification = ReservationClassification.PROVEN_TERMINAL,
    reason_code: str = "exact_close_and_position_terminal",
) -> BoundCloseReservationObservation:
    return BoundCloseReservationObservation(
        reservation_ref=reservation_ref,
        classification=classification,
        reason_code=reason_code,
        source_fingerprint=FP_B,
        exchange_fingerprint=FP_C,
    )


def _document(
    *,
    started_at: datetime,
    source_fingerprint: str = FP_D,
    observations: tuple[BoundCloseReservationObservation, ...] | None = None,
) -> str:
    plan = build_bound_close_reservation_recovery_plan(
        source_fingerprint=source_fingerprint,
        observations=observations or (_observation(),),
    )
    return serialize_bound_close_reservation_recovery_plan(
        plan,
        capture_started_at=started_at,
        capture_completed_at=started_at + timedelta(seconds=30),
    )


def _write_private(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def _run(capsys, *paths: Path) -> tuple[int, str, str]:
    result = main([str(path) for path in paths])
    captured = capsys.readouterr()
    return result, captured.out, captured.err


def test_comparator_accepts_only_two_independent_semantically_stable_captures(
    tmp_path, capsys
):
    first = _write_private(tmp_path / "first.json", _document(started_at=START))
    second = _write_private(
        tmp_path / "second.json",
        _document(started_at=START + timedelta(minutes=2)),
    )

    assert _run(capsys, first, second) == (0, '{"status":"stable"}\n', "")


@pytest.mark.parametrize("path_count", [0, 1, 3])
def test_comparator_requires_exactly_two_paths(tmp_path, capsys, path_count):
    paths = tuple(
        _write_private(
            tmp_path / f"capture-{index}.json",
            _document(started_at=START + timedelta(minutes=index)),
        )
        for index in range(path_count)
    )

    code, output, error = _run(capsys, *paths)

    assert code == 2
    assert output == '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n'
    assert error == ""


def test_comparator_rejects_permissions_symlinks_and_non_regular_files(
    tmp_path, capsys
):
    valid = _write_private(tmp_path / "valid.json", _document(started_at=START))
    loose = _write_private(
        tmp_path / "loose.json",
        _document(started_at=START + timedelta(minutes=2)),
    )
    loose.chmod(0o640)
    code, output, error = _run(capsys, valid, loose)
    assert (code, output, error) == (
        2,
        '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n',
        "",
    )

    target = _write_private(
        tmp_path / "target.json",
        _document(started_at=START + timedelta(minutes=3)),
    )
    symlink = tmp_path / "capture-link.json"
    symlink.symlink_to(target)
    code, output, error = _run(capsys, valid, symlink)
    assert (code, output, error) == (
        2,
        '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n',
        "",
    )

    directory = tmp_path / "capture-dir"
    directory.mkdir(mode=0o700)
    code, output, error = _run(capsys, valid, directory)
    assert (code, output, error) == (
        2,
        '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n',
        "",
    )


def _assert_refused(capsys, first: Path, second: Path) -> None:
    code, output, error = _run(capsys, first, second)
    assert code == 2
    assert output == '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n'
    assert error == ""


def test_comparator_rejects_duplicate_keys_unknown_fields_and_boundedness(
    tmp_path, capsys
):
    valid = _write_private(tmp_path / "valid.json", _document(started_at=START))

    duplicate = _document(started_at=START + timedelta(minutes=2)).replace(
        '{"action_count":1,',
        '{"action_count":1,"action_count":1,',
        1,
    )
    _assert_refused(
        capsys,
        valid,
        _write_private(tmp_path / "duplicate.json", duplicate),
    )

    payload = json.loads(_document(started_at=START + timedelta(minutes=3)))
    payload["unknown_field"] = "TOPSECRET-MUST-NOT-BE-ECHOED"
    _assert_refused(
        capsys,
        valid,
        _write_private(tmp_path / "unknown.json", json.dumps(payload)),
    )

    oversized = _document(started_at=START + timedelta(minutes=4)) + (" " * 70_000)
    _assert_refused(
        capsys,
        valid,
        _write_private(tmp_path / "oversized.json", oversized),
    )

    nested = json.loads(_document(started_at=START + timedelta(minutes=5)))
    nested["unknown_field"] = [[[[[["TOPSECRET"]]]]]]
    _assert_refused(
        capsys,
        valid,
        _write_private(tmp_path / "deep.json", json.dumps(nested)),
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(status="refused", action_count=0),
        lambda payload: payload.update(exchange_writes=1),
        lambda payload: payload.update(history_replays=1),
        lambda payload: payload.update(database_writes=1),
        lambda payload: payload["counts"].update(total=2),
        lambda payload: payload.update(evidence_fingerprint="0" * 64),
        lambda payload: payload.update(exchange_snapshot_fingerprint="0" * 64),
        lambda payload: payload.update(confirmation_token="BOUND-CLOSE-0000000000000000"),
    ],
)
def test_comparator_rejects_nonready_writes_bad_counts_and_bad_seals(
    tmp_path, capsys, mutate
):
    first = _write_private(tmp_path / "first.json", _document(started_at=START))
    payload = json.loads(_document(started_at=START + timedelta(minutes=2)))
    mutate(payload)
    second = _write_private(tmp_path / "second.json", json.dumps(payload))

    _assert_refused(capsys, first, second)


def test_comparator_rejects_repeated_capture_identity(tmp_path, capsys):
    document = _document(started_at=START)
    first = _write_private(tmp_path / "first.json", document)
    second = _write_private(tmp_path / "second.json", document)

    _assert_refused(capsys, first, second)


def test_comparator_rejects_any_valid_semantic_drift_without_echoing_input(
    tmp_path, capsys
):
    first = _write_private(tmp_path / "first.json", _document(started_at=START))
    second = _write_private(
        tmp_path / "second.json",
        _document(
            started_at=START + timedelta(minutes=2),
            source_fingerprint="e" * 64,
        ),
    )

    code, output, error = _run(capsys, first, second)

    assert code == 2
    assert output == '{"reason_code":"bound_close_reservation_dry_runs_refused"}\n'
    assert "e" * 64 not in output
    assert error == ""


def test_comparator_does_not_write_or_change_capture_files(tmp_path, capsys):
    first = _write_private(tmp_path / "first.json", _document(started_at=START))
    second = _write_private(
        tmp_path / "second.json",
        _document(started_at=START + timedelta(minutes=2)),
    )
    before = [(path.read_bytes(), os.stat(path).st_mtime_ns) for path in (first, second)]

    assert _run(capsys, first, second)[0] == 0

    after = [(path.read_bytes(), os.stat(path).st_mtime_ns) for path in (first, second)]
    assert after == before
