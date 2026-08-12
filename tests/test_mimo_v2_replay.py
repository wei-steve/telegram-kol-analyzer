from __future__ import annotations

from copy import deepcopy
import math
import sqlite3
from pathlib import Path

import pytest

from telegram_kol_research.mimo_v2_replay import (
    MimoV2ReplayInputError,
    compare_execution_projections,
    create_read_only_replay_snapshot,
    evaluate_replay_performance,
    load_replay_message_ids,
    nearest_rank_percentile,
    validate_replay_inputs,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage


def _write_ids(tmp_path: Path, content: str = "7\n") -> Path:
    path = tmp_path / "approved-ids.txt"
    path.write_text(content, encoding="utf-8")
    return path


def _valid_boundaries(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source_database = tmp_path / "source.db"
    source_database.write_bytes(b"SQLite fixture placeholder")
    message_id_file = _write_ids(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    artifact_dir = tmp_path / "artifacts"
    return source_database, message_id_file, media_root, artifact_dir


def _seed_source_database(tmp_path: Path) -> tuple[Path, int]:
    source = tmp_path / "source.db"
    session_factory = create_session_factory(source)
    with session_factory() as session:
        message = RawMessage(chat_id=77, message_id=91, text="private replay text")
        session.add(message)
        session.commit()
        message_id = int(message.id)
    session_factory.kw["bind"].dispose()
    return source, message_id


def _source_component_signatures(source: Path) -> dict[str, tuple[bytes, int, int, int]]:
    signatures: dict[str, tuple[bytes, int, int, int]] = {}
    for suffix in ("", "-wal", "-shm"):
        path = source.with_name(source.name + suffix)
        if not path.exists():
            continue
        stat = path.stat()
        signatures[suffix or "main"] = (
            path.read_bytes(),
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_mode,
        )
    return signatures


def _management_payload(
    *,
    stop_loss: str = "1940",
    reason: str = "move stop",
) -> dict[str, object]:
    return {
        "recognition_result": "非策略",
        "confidence": 0.95,
        "strategy": {},
        "instructions": [
            {
                "kind": "move_stop_to_protect",
                "confidence": 0.95,
                "reason": reason,
                "strategy": None,
                "target": {"lifecycle_id": 790, "thread_id": 52},
                "parameters": {"stop_loss": stop_loss},
            }
        ],
        "lifecycle_event": {
            "event_type": "position_update",
            "management_action": "move_stop_to_protect",
            "target_lifecycle_id": 790,
            "stop_loss": stop_loss,
            "confidence": 0.95,
            "reason": reason,
        },
        "reason": reason,
        "summary": reason,
        "input_reading": {"observed_text": reason},
    }


def _entry_payload(*, reason: str = "entry") -> dict[str, object]:
    strategy = {
        "symbol": "ETH",
        "side": "short",
        "entry": "1880-1890",
        "stop_loss": "1940",
        "take_profit": "1800",
        "leverage": "20",
        "order_type": "limit",
    }
    return {
        "recognition_result": "是策略",
        "confidence": 0.94,
        "strategy": strategy,
        "instructions": [
            {
                "kind": "entry",
                "confidence": 0.94,
                "reason": reason,
                "strategy": strategy,
                "target": {"lifecycle_id": None, "thread_id": None},
                "parameters": {},
            }
        ],
        "lifecycle_event": {
            "event_type": "none",
            "confidence": 0.0,
            "reason": "none",
        },
        "reason": reason,
        "summary": reason,
        "input_reading": {"observed_text": reason},
    }


def _no_action_payload(*, reason: str) -> dict[str, object]:
    return {
        "recognition_result": "非策略",
        "confidence": 0.12,
        "strategy": {},
        "instructions": [],
        "lifecycle_event": {
            "event_type": "none",
            "confidence": 0.0,
            "reason": reason,
        },
        "reason": reason,
        "summary": reason,
        "input_reading": {"observed_text": reason},
    }


def test_load_replay_message_ids_is_bounded_and_stable(tmp_path):
    source = _write_ids(tmp_path, "7\n# incident\n9\n7\n")

    assert load_replay_message_ids(source, max_messages=2) == (7, 9)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1 2"])
def test_load_replay_message_ids_rejects_malformed_values(tmp_path, value):
    source = _write_ids(tmp_path, value)

    with pytest.raises(MimoV2ReplayInputError, match="message_id_invalid"):
        load_replay_message_ids(source, max_messages=200)


def test_load_replay_message_ids_rejects_empty_file(tmp_path):
    source = _write_ids(tmp_path, "\n# none\n")

    with pytest.raises(MimoV2ReplayInputError, match="message_id_list_empty"):
        load_replay_message_ids(source, max_messages=200)


def test_load_replay_message_ids_rejects_more_than_bound(tmp_path):
    source = _write_ids(tmp_path, "1\n2\n3\n")

    with pytest.raises(MimoV2ReplayInputError, match="message_id_limit_exceeded"):
        load_replay_message_ids(source, max_messages=2)


@pytest.mark.parametrize("max_messages", [0, 201, True])
def test_load_replay_message_ids_rejects_invalid_bound(tmp_path, max_messages):
    source = _write_ids(tmp_path)

    with pytest.raises(MimoV2ReplayInputError, match="max_messages_invalid"):
        load_replay_message_ids(source, max_messages=max_messages)


def test_validate_replay_path_boundary_creates_private_artifact_dir(tmp_path):
    source_database, message_id_file, media_root, artifact_dir = (
        _valid_boundaries(tmp_path)
    )

    inputs = validate_replay_inputs(
        source_database=source_database,
        message_id_file=message_id_file,
        media_root=media_root,
        artifact_dir=artifact_dir,
        max_messages=200,
    )

    assert inputs.raw_message_ids == (7,)
    assert inputs.artifact_dir == artifact_dir.resolve()
    assert artifact_dir.is_dir()
    assert artifact_dir.stat().st_mode & 0o777 == 0o700


def test_validate_replay_path_boundary_accepts_existing_empty_directory(tmp_path):
    source_database, message_id_file, media_root, artifact_dir = (
        _valid_boundaries(tmp_path)
    )
    artifact_dir.mkdir()

    inputs = validate_replay_inputs(
        source_database=source_database,
        message_id_file=message_id_file,
        media_root=media_root,
        artifact_dir=artifact_dir,
        max_messages=200,
    )

    assert inputs.artifact_dir == artifact_dir.resolve()
    assert list(artifact_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("boundary", "reason"),
    [
        ("source_database", "source_database_invalid"),
        ("message_id_file", "message_id_file_invalid"),
        ("media_root", "media_root_invalid"),
    ],
)
def test_validate_replay_path_boundary_rejects_missing_input(
    tmp_path,
    boundary,
    reason,
):
    source_database, message_id_file, media_root, artifact_dir = (
        _valid_boundaries(tmp_path)
    )
    values = {
        "source_database": source_database,
        "message_id_file": message_id_file,
        "media_root": media_root,
    }
    values[boundary].unlink() if values[boundary].is_file() else values[boundary].rmdir()

    with pytest.raises(MimoV2ReplayInputError, match=reason):
        validate_replay_inputs(
            **values,
            artifact_dir=artifact_dir,
            max_messages=200,
        )


@pytest.mark.parametrize(
    ("boundary", "reason"),
    [
        ("source_database", "source_database_invalid"),
        ("message_id_file", "message_id_file_invalid"),
        ("media_root", "media_root_invalid"),
        ("artifact_dir", "artifact_dir_invalid"),
    ],
)
def test_validate_replay_path_boundary_rejects_symlink(
    tmp_path,
    boundary,
    reason,
):
    source_database, message_id_file, media_root, artifact_dir = (
        _valid_boundaries(tmp_path)
    )
    targets = {
        "source_database": source_database,
        "message_id_file": message_id_file,
        "media_root": media_root,
        "artifact_dir": tmp_path / "real-artifacts",
    }
    if boundary == "artifact_dir":
        targets[boundary].mkdir()
    links = {
        key: tmp_path / f"{key}-link"
        for key in targets
    }
    links[boundary].symlink_to(
        targets[boundary],
        target_is_directory=targets[boundary].is_dir(),
    )
    values = {
        "source_database": source_database,
        "message_id_file": message_id_file,
        "media_root": media_root,
        "artifact_dir": artifact_dir,
    }
    values[boundary] = links[boundary]

    with pytest.raises(MimoV2ReplayInputError, match=reason):
        validate_replay_inputs(**values, max_messages=200)


def test_validate_replay_path_boundary_rejects_nonempty_artifact_dir(tmp_path):
    source_database, message_id_file, media_root, artifact_dir = (
        _valid_boundaries(tmp_path)
    )
    artifact_dir.mkdir()
    (artifact_dir / "existing.json").write_text("{}", encoding="utf-8")

    with pytest.raises(MimoV2ReplayInputError, match="artifact_dir_not_empty"):
        validate_replay_inputs(
            source_database=source_database,
            message_id_file=message_id_file,
            media_root=media_root,
            artifact_dir=artifact_dir,
            max_messages=200,
        )


def test_online_snapshot_reads_source_without_modifying_it(tmp_path):
    source, message_id = _seed_source_database(tmp_path)
    before = _source_component_signatures(source)
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    working = private_root / "working.db"

    create_read_only_replay_snapshot(source, working)

    assert _source_component_signatures(source) == before
    with sqlite3.connect(working) as connection:
        assert connection.execute(
            "SELECT id FROM raw_messages WHERE id = ?",
            (message_id,),
        ).fetchone() == (message_id,)


def test_online_snapshot_rejects_existing_destination(tmp_path):
    source, _ = _seed_source_database(tmp_path)
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    working = private_root / "working.db"
    working.write_bytes(b"do not overwrite")

    with pytest.raises(MimoV2ReplayInputError, match="snapshot_destination_invalid"):
        create_read_only_replay_snapshot(source, working)

    assert working.read_bytes() == b"do not overwrite"


def test_online_snapshot_requires_private_real_parent(tmp_path):
    source, _ = _seed_source_database(tmp_path)
    public_root = tmp_path / "public"
    public_root.mkdir(mode=0o755)

    with pytest.raises(MimoV2ReplayInputError, match="snapshot_destination_invalid"):
        create_read_only_replay_snapshot(source, public_root / "working.db")


def test_identical_execution_projection_is_safe():
    payload = _entry_payload()

    comparison = compare_execution_projections(payload, deepcopy(payload))

    assert comparison.status == "safe_match"
    assert comparison.reason_code == "execution_projections_equal"
    assert comparison.v1_fingerprint == comparison.v2_fingerprint


def test_execution_projection_field_mismatch_is_unsafe():
    comparison = compare_execution_projections(
        _management_payload(stop_loss="1940"),
        _management_payload(stop_loss="1950"),
    )

    assert comparison.status == "unsafe_mismatch"
    assert comparison.reason_code == "execution_projection_mismatch"
    assert comparison.v1_fingerprint != comparison.v2_fingerprint


def test_v1_action_omitted_by_v2_is_unsafe():
    comparison = compare_execution_projections(
        _management_payload(),
        _no_action_payload(reason="commentary"),
    )

    assert comparison.status == "unsafe_mismatch"


def test_new_v2_executable_action_is_unsafe():
    comparison = compare_execution_projections(
        _no_action_payload(reason="commentary"),
        _management_payload(),
    )

    assert comparison.status == "unsafe_mismatch"


def test_non_executable_wording_difference_is_safe():
    comparison = compare_execution_projections(
        _no_action_payload(reason="one"),
        _no_action_payload(reason="two"),
    )

    assert comparison.status == "safe_match"
    assert comparison.reason_code == "both_non_executable"


def test_reason_and_observed_text_do_not_change_projection():
    comparison = compare_execution_projections(
        _management_payload(reason="move stop: 1940"),
        _management_payload(reason="move stop—1940?!"),
    )

    assert comparison.status == "safe_match"
    assert comparison.v1_fingerprint == comparison.v2_fingerprint


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("symbol", "BTC"),
        ("side", "long"),
        ("entry", "1870"),
        ("stop_loss", "1950"),
        ("take_profit", "1790"),
        ("leverage", "10"),
        ("order_type", "market"),
    ],
)
def test_strategy_projection_drift_is_unsafe(field, changed):
    v1 = _entry_payload()
    v2 = deepcopy(v1)
    v2["strategy"][field] = changed
    v2["instructions"][0]["strategy"][field] = changed

    assert compare_execution_projections(v1, v2).status == "unsafe_mismatch"


def test_target_and_confidence_projection_drift_is_unsafe():
    v1 = _management_payload()
    v2 = deepcopy(v1)
    v2["instructions"][0]["target"]["lifecycle_id"] = 791
    v2["lifecycle_event"]["target_lifecycle_id"] = 791
    v2["confidence"] = 0.94

    assert compare_execution_projections(v1, v2).status == "unsafe_mismatch"


def test_instruction_order_projection_drift_is_unsafe():
    v1 = _management_payload()
    second = deepcopy(v1["instructions"][0])
    second["kind"] = "partial_take_profit"
    second["parameters"] = {"management_fraction": 0.5}
    v1["instructions"].append(second)
    v2 = deepcopy(v1)
    v2["instructions"].reverse()

    assert compare_execution_projections(v1, v2).status == "unsafe_mismatch"


@pytest.mark.parametrize(
    ("event_type", "management_action"),
    [
        ("exit_position", "exit_full"),
        ("exit_position", "exit_partial"),
        ("cancel_entry", "cancel_pending_entry"),
    ],
)
def test_lifecycle_action_confusion_is_unsafe(event_type, management_action):
    v1 = _management_payload()
    v2 = deepcopy(v1)
    v2["lifecycle_event"]["event_type"] = event_type
    v2["lifecycle_event"]["management_action"] = management_action

    assert compare_execution_projections(v1, v2).status == "unsafe_mismatch"


def test_nearest_rank_percentile_is_deterministic():
    assert nearest_rank_percentile(range(1, 21), 0.95) == 19.0
    assert nearest_rank_percentile([3, 1, 2], 0.5) == 2.0
    assert nearest_rank_percentile([], 0.95) is None


@pytest.mark.parametrize("value", [-1, math.inf, math.nan, True])
def test_nearest_rank_percentile_rejects_invalid_timing(value):
    with pytest.raises(MimoV2ReplayInputError, match="latency_sample_invalid"):
        nearest_rank_percentile([value], 0.95)


def test_performance_gate_passes_at_v2_ratio_boundary():
    gate = evaluate_replay_performance(
        v1_duration_ms=[100, 100],
        v2_duration_ms=[115, 115],
        adapter_duration_ms=[49, 49],
    )

    assert gate.passed is True
    assert gate.failure_codes == ()
    assert gate.v2_to_v1_ratio == pytest.approx(1.15)


def test_performance_gate_fails_when_v2_exceeds_ratio():
    gate = evaluate_replay_performance(
        v1_duration_ms=[100, 100],
        v2_duration_ms=[116, 116],
        adapter_duration_ms=[1, 1],
    )

    assert gate.passed is False
    assert "v2_latency_ratio_exceeded" in gate.failure_codes


def test_performance_gate_requires_adapter_p95_strictly_below_50_ms():
    gate = evaluate_replay_performance(
        v1_duration_ms=[100],
        v2_duration_ms=[100],
        adapter_duration_ms=[50],
    )

    assert gate.passed is False
    assert "adapter_latency_exceeded" in gate.failure_codes


def test_performance_gate_fails_without_comparable_pairs():
    gate = evaluate_replay_performance(
        v1_duration_ms=[],
        v2_duration_ms=[],
        adapter_duration_ms=[],
    )

    assert gate.passed is False
    assert gate.failure_codes == ("no_comparable_pairs",)
    assert gate.v1_p95_ms is None
