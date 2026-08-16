from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
import json
import math
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_kol_research.mimo_v2_replay import (
    MimoV2ReplayInputError,
    compare_execution_projections,
    create_read_only_replay_snapshot,
    evaluate_replay_performance,
    load_replay_message_ids,
    nearest_rank_percentile,
    run_mimo_v2_replay,
    validate_replay_inputs,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import MediaAsset, RawMessage
from telegram_kol_research.mimo_v2_contract import parse_mimo_v2_payload
from telegram_kol_research.mimo_v2_execution_adapter import (
    adapt_mimo_v2_to_current_payload,
)


_COMPARISON_ARTIFACT_FIELDS = (
    "raw_message_id",
    "status",
    "reason_code",
    "v1_status",
    "v2_status",
    "v1_duration_ms",
    "v2_duration_ms",
    "adapter_duration_ms",
    "v1_projection_fingerprint",
    "v2_projection_fingerprint",
)
_SUMMARY_ARTIFACT_FIELDS = (
    "schema_version",
    "processed",
    "comparable",
    "unsafe_mismatches",
    "validation_failures",
    "production_writes",
    "notifications_sent",
    "execution_calls",
    "v1_p95_ms",
    "v2_p95_ms",
    "adapter_p95_ms",
    "v2_to_v1_ratio",
    "performance_passed",
    "performance_failure_codes",
    "passed",
)
_REPLAY_ARTIFACT_NAMES = (
    "comparisons.csv",
    "comparisons.json",
    "summary.json",
)


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


def _parsed_entry_v2(
    *,
    stop_loss: str = "1940",
    observed_text: str = "private replay text",
):
    strategy = dict(_entry_payload()["strategy"])
    strategy["stop_loss"] = stop_loss
    return parse_mimo_v2_payload(
        {
            "contract_version": "mimo-authoritative-v2",
            "summary": "entry",
            "confidence": 0.94,
            "intents": [
                {
                    "intent_type": "new_strategy",
                    "action": {
                        "kind": "entry",
                        "target": {"lifecycle_id": None, "thread_id": None},
                        "strategy": strategy,
                        "parameters": {},
                    },
                    "reason": "entry",
                    "confidence": 0.94,
                    "evidence_refs": ["text:observed_text"],
                }
            ],
            "evidence": {
                "text": {"observed_text": observed_text, "fields": {}},
                "images": [],
                "conflicts": [],
            },
        }
    )


def _validated_replay_inputs(
    tmp_path: Path,
    *,
    approved_ids: tuple[int, ...] | None = None,
):
    source, message_id = _seed_source_database(tmp_path)
    ids = approved_ids or (message_id,)
    id_file = tmp_path / "approved-ids.txt"
    id_file.write_text("".join(f"{value}\n" for value in ids), encoding="utf-8")
    media_root = tmp_path / "media"
    media_root.mkdir()
    artifact_dir = tmp_path / "artifacts"
    inputs = validate_replay_inputs(
        source_database=source,
        message_id_file=id_file,
        media_root=media_root,
        artifact_dir=artifact_dir,
        max_messages=200,
    )
    return inputs, message_id


class _SequenceClock:
    def __init__(self, *values: float):
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


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


def test_confidence_only_difference_is_not_execution_semantic_drift():
    v1 = _management_payload()
    v2 = deepcopy(v1)
    v2["confidence"] = 0.73
    v2["instructions"][0]["confidence"] = 0.73
    v2["lifecycle_event"]["confidence"] = 0.73

    comparison = compare_execution_projections(v1, v2)

    assert comparison.status == "safe_match"
    assert comparison.v1_fingerprint == comparison.v2_fingerprint


def test_confidence_crossing_execution_threshold_is_unsafe():
    v1 = _management_payload()
    v2 = deepcopy(v1)
    v2["confidence"] = 0.69
    v2["instructions"][0]["confidence"] = 0.69
    v2["lifecycle_event"]["confidence"] = 0.69

    comparison = compare_execution_projections(v1, v2)

    assert comparison.status == "unsafe_mismatch"
    assert comparison.v1_fingerprint != comparison.v2_fingerprint


def test_companion_thread_target_drift_is_unsafe():
    v1 = _management_payload()
    v2 = deepcopy(v1)
    v2["instructions"][0]["target"]["thread_id"] = 53

    comparison = compare_execution_projections(v1, v2)

    assert comparison.status == "unsafe_mismatch"
    assert comparison.v1_fingerprint != comparison.v2_fingerprint


@pytest.mark.parametrize(
    ("kind", "intent_type", "event_type", "management_action", "parameters"),
    (
        ("full_exit", "exit", "exit_position", "exit_full", {}),
        (
            "partial_take_profit",
            "position_management",
            "position_update",
            "partial_take_profit",
            {"management_fraction": 0.5},
        ),
    ),
)
def test_legacy_lifecycle_and_v2_instruction_shapes_compare_semantically(
    kind,
    intent_type,
    event_type,
    management_action,
    parameters,
):
    legacy = {
        "recognition_result": "非策略",
        "confidence": 0.9,
        "strategy": {},
        "instructions": [],
        "lifecycle_event": {
            "event_type": event_type,
            "management_action": management_action,
            "target_lifecycle_id": 832,
            **parameters,
            "confidence": 0.9,
            "reason": "legacy wording",
        },
    }
    parsed = parse_mimo_v2_payload(
        {
            "contract_version": "mimo-authoritative-v2",
            "summary": "v2 wording",
            "confidence": 0.81,
            "intents": [
                {
                    "intent_type": intent_type,
                    "action": {
                        "kind": kind,
                        "target": {"lifecycle_id": 832, "thread_id": None},
                        "strategy": None,
                        "parameters": parameters,
                    },
                    "reason": "v2 wording",
                    "confidence": 0.81,
                    "evidence_refs": ["text:observed_text"],
                }
            ],
            "evidence": {
                "text": {"observed_text": "source", "fields": {}},
                "images": [],
                "conflicts": [],
            },
        }
    )
    adapted = adapt_mimo_v2_to_current_payload(parsed).payload

    comparison = compare_execution_projections(legacy, adapted)

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


def test_target_projection_drift_is_unsafe():
    v1 = _management_payload()
    v2 = deepcopy(v1)
    v2["instructions"][0]["target"]["lifecycle_id"] = 791
    v2["lifecycle_event"]["target_lifecycle_id"] = 791

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


def test_runner_writes_only_disposable_copy_and_reuses_exact_context(tmp_path):
    inputs, message_id = _validated_replay_inputs(tmp_path)
    seen_database_paths: list[Path] = []
    seen_contexts: list[str] = []

    def fake_v1(session_factory, **kwargs):
        seen_database_paths.append(
            Path(session_factory.kw["bind"].url.database).resolve()
        )
        seen_contexts.append(kwargs["context_text"])
        with session_factory() as session:
            session.get(RawMessage, message_id).text = "changed only in copy"
            session.commit()
        return SimpleNamespace(
            payload=_entry_payload(),
            status="是策略",
            error_message=None,
        )

    def fake_v2(session_factory, **kwargs):
        seen_database_paths.append(
            Path(session_factory.kw["bind"].url.database).resolve()
        )
        seen_contexts.append(kwargs["context_text"])
        return SimpleNamespace(
            succeeded=True,
            parsed_result=_parsed_entry_v2(),
            error_code=None,
            error_message=None,
        )

    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=fake_v1,
        v2_runner=fake_v2,
        clock=_SequenceClock(0.0, 0.1, 0.1, 0.2, 0.2, 0.201),
    )

    assert result.processed == 1
    assert result.comparable == 1
    assert result.passed is True
    assert result.production_writes == 0
    assert result.notifications_sent == 0
    assert result.execution_calls == 0
    assert len(set(seen_contexts)) == 1
    assert all(path != inputs.source_database for path in seen_database_paths)
    assert all(not path.exists() for path in seen_database_paths)
    assert tuple(
        sorted(path.name for path in inputs.artifact_dir.iterdir())
    ) == _REPLAY_ARTIFACT_NAMES
    source_uri = inputs.source_database.as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(source_uri, uri=True) as connection:
        assert connection.execute(
            "SELECT text FROM raw_messages WHERE id = ?",
            (message_id,),
        ).fetchone() == ("private replay text",)


def test_runner_rejects_missing_requested_message_before_model_call(tmp_path):
    inputs, _ = _validated_replay_inputs(tmp_path, approved_ids=(999_999,))
    calls: list[str] = []

    with pytest.raises(MimoV2ReplayInputError, match="raw_message_not_found"):
        run_mimo_v2_replay(
            inputs=inputs,
            ai_recognition_config_path=tmp_path / "ai.yaml",
            v1_runner=lambda *args, **kwargs: calls.append("v1"),
            v2_runner=lambda *args, **kwargs: calls.append("v2"),
        )

    assert calls == []
    assert list(inputs.artifact_dir.iterdir()) == []


def test_runner_cleans_disposable_copy_after_runner_exception(tmp_path):
    inputs, _ = _validated_replay_inputs(tmp_path)
    seen_database_paths: list[Path] = []

    def failing_v1(session_factory, **kwargs):
        seen_database_paths.append(
            Path(session_factory.kw["bind"].url.database).resolve()
        )
        raise RuntimeError("provider leaked text must not escape")

    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=failing_v1,
        v2_runner=lambda *args, **kwargs: pytest.fail("v2 must not run"),
        clock=_SequenceClock(0.0, 0.1),
    )

    assert result.processed == 1
    assert result.validation_failures == 1
    assert result.comparisons[0].reason_code == "v1_runner_error"
    assert all(not path.exists() for path in seen_database_paths)
    assert tuple(
        sorted(path.name for path in inputs.artifact_dir.iterdir())
    ) == _REPLAY_ARTIFACT_NAMES


def test_runner_processes_messages_in_approved_order(tmp_path):
    source = tmp_path / "source.db"
    factory = create_session_factory(source)
    with factory() as session:
        first = RawMessage(chat_id=77, message_id=91, text="first")
        second = RawMessage(chat_id=77, message_id=92, text="second")
        session.add_all([first, second])
        session.commit()
        approved = (int(second.id), int(first.id))
    factory.kw["bind"].dispose()
    id_file = tmp_path / "approved-ids.txt"
    id_file.write_text("".join(f"{value}\n" for value in approved), encoding="utf-8")
    media_root = tmp_path / "media"
    media_root.mkdir()
    inputs = validate_replay_inputs(
        source_database=source,
        message_id_file=id_file,
        media_root=media_root,
        artifact_dir=tmp_path / "artifacts",
        max_messages=2,
    )
    calls: list[tuple[str, int]] = []

    def fake_v1(session_factory, **kwargs):
        calls.append(("v1", kwargs["raw_message_id"]))
        return SimpleNamespace(
            payload=_entry_payload(),
            status="是策略",
            error_message=None,
        )

    def fake_v2(session_factory, **kwargs):
        calls.append(("v2", kwargs["raw_message_id"]))
        return SimpleNamespace(
            succeeded=True,
            parsed_result=_parsed_entry_v2(),
            error_code=None,
            error_message=None,
        )

    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=fake_v1,
        v2_runner=fake_v2,
        clock=_SequenceClock(
            0.0, 0.1, 0.1, 0.2, 0.2, 0.201,
            1.0, 1.1, 1.1, 1.2, 1.2, 1.201,
        ),
    )

    assert result.processed == 2
    assert calls == [
        ("v1", approved[0]),
        ("v2", approved[0]),
        ("v1", approved[1]),
        ("v2", approved[1]),
    ]


def test_runner_classifies_unsafe_projection_mismatch(tmp_path):
    inputs, _ = _validated_replay_inputs(tmp_path)

    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=lambda *args, **kwargs: SimpleNamespace(
            payload=_entry_payload(),
            status="是策略",
            error_message=None,
        ),
        v2_runner=lambda *args, **kwargs: SimpleNamespace(
            succeeded=True,
            parsed_result=_parsed_entry_v2(stop_loss="1950"),
            error_code=None,
        ),
    )

    assert result.unsafe_mismatches == 1
    assert result.validation_failures == 0
    assert result.comparisons[0].reason_code == "execution_projection_mismatch"
    assert result.passed is False


def test_runner_classifies_v1_failure_without_calling_v2(tmp_path):
    inputs, _ = _validated_replay_inputs(tmp_path)
    v2_calls: list[int] = []

    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=lambda *args, **kwargs: SimpleNamespace(
            payload=None,
            status="识别失败",
            error_message="sensitive provider detail",
        ),
        v2_runner=lambda *args, **kwargs: v2_calls.append(
            kwargs["raw_message_id"]
        ),
    )

    assert v2_calls == []
    assert result.validation_failures == 1
    assert result.comparisons[0].reason_code == "v1_failed"
    assert "sensitive" not in repr(result)
    assert result.passed is False


@pytest.mark.parametrize(
    "error_code",
    ["provider_http_error", "contract_validation_failed"],
)
def test_runner_preserves_stable_v2_failure_codes(tmp_path, error_code):
    inputs, _ = _validated_replay_inputs(tmp_path)

    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=lambda *args, **kwargs: SimpleNamespace(
            payload=_entry_payload(),
            status="是策略",
            error_message=None,
        ),
        v2_runner=lambda *args, **kwargs: SimpleNamespace(
            succeeded=False,
            parsed_result=None,
            error_code=error_code,
            error_message="sensitive provider detail",
        ),
    )

    assert result.validation_failures == 1
    assert result.comparisons[0].reason_code == error_code
    assert "sensitive" not in repr(result)
    assert result.passed is False


def test_runner_classifies_adapter_failure(tmp_path, monkeypatch):
    inputs, _ = _validated_replay_inputs(tmp_path)

    def fail_adapter(*args, **kwargs):
        raise ValueError("sensitive adapted payload")

    monkeypatch.setattr(
        "telegram_kol_research.mimo_v2_replay.adapt_mimo_v2_to_current_payload",
        fail_adapter,
    )
    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=lambda *args, **kwargs: SimpleNamespace(
            payload=_entry_payload(),
            status="是策略",
            error_message=None,
        ),
        v2_runner=lambda *args, **kwargs: SimpleNamespace(
            succeeded=True,
            parsed_result=_parsed_entry_v2(),
            error_code=None,
        ),
    )

    assert result.validation_failures == 1
    assert result.comparisons[0].reason_code == "adapter_failure"
    assert "sensitive" not in repr(result)
    assert result.passed is False


def test_runner_classifies_missing_image_before_model_calls(tmp_path):
    inputs, message_id = _validated_replay_inputs(tmp_path)
    factory = create_session_factory(inputs.source_database)
    with factory() as session:
        session.add(
            MediaAsset(
                raw_message_id=message_id,
                telegram_file_id="missing",
                kind="image",
                mime_type="image/png",
                local_path="missing.png",
            )
        )
        session.commit()
    factory.kw["bind"].dispose()
    calls: list[str] = []

    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=lambda *args, **kwargs: calls.append("v1"),
        v2_runner=lambda *args, **kwargs: calls.append("v2"),
    )

    assert calls == []
    assert result.validation_failures == 1
    assert result.comparisons[0].reason_code == "image_unavailable"
    assert result.passed is False


def test_runner_continues_after_unexpected_v2_exception(tmp_path):
    source = tmp_path / "source.db"
    factory = create_session_factory(source)
    with factory() as session:
        first = RawMessage(chat_id=77, message_id=91, text="first")
        second = RawMessage(chat_id=77, message_id=92, text="second")
        session.add_all([first, second])
        session.commit()
        approved = (int(first.id), int(second.id))
    factory.kw["bind"].dispose()
    id_file = tmp_path / "approved-ids.txt"
    id_file.write_text("".join(f"{value}\n" for value in approved), encoding="utf-8")
    media_root = tmp_path / "media"
    media_root.mkdir()
    inputs = validate_replay_inputs(
        source_database=source,
        message_id_file=id_file,
        media_root=media_root,
        artifact_dir=tmp_path / "artifacts",
        max_messages=2,
    )
    completed_v2: list[int] = []

    def fake_v2(session_factory, **kwargs):
        raw_message_id = kwargs["raw_message_id"]
        if raw_message_id == approved[0]:
            raise RuntimeError("sensitive unexpected response")
        completed_v2.append(raw_message_id)
        return SimpleNamespace(
            succeeded=True,
            parsed_result=_parsed_entry_v2(),
            error_code=None,
        )

    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=lambda *args, **kwargs: SimpleNamespace(
            payload=_entry_payload(),
            status="是策略",
            error_message=None,
        ),
        v2_runner=fake_v2,
    )

    assert completed_v2 == [approved[1]]
    assert result.processed == 2
    assert result.validation_failures == 1
    assert result.comparable == 1
    assert result.comparisons[0].reason_code == "v2_runner_error"
    assert "sensitive" not in repr(result)
    assert result.passed is False


def test_replay_artifacts_are_bounded_deterministic_and_redacted(tmp_path):
    inputs, message_id = _validated_replay_inputs(tmp_path)
    secret_markers = (
        "PRIVATE_SOURCE_TEXT",
        "data:image/png;base64,PRIVATE_IMAGE_BYTES",
        "PRIVATE_PROMPT_TEXT",
        "Bearer PRIVATE_AUTHORIZATION",
        "api_key=PRIVATE_API_KEY",
        "password=PRIVATE_PASSWORD",
        "PRIVATE_RAW_PROVIDER_RESPONSE",
    )
    image_path = inputs.media_root / "private-image.png"
    image_path.write_text(secret_markers[1], encoding="utf-8")
    factory = create_session_factory(inputs.source_database)
    with factory() as session:
        message = session.get(RawMessage, message_id)
        message.text = " ".join(secret_markers)
        session.add(
            MediaAsset(
                raw_message_id=message_id,
                telegram_file_id="private-image",
                kind="image",
                mime_type="image/png",
                local_path=image_path.name,
                ocr_text=" ".join(secret_markers),
            )
        )
        session.commit()
    factory.kw["bind"].dispose()
    v1_payload = _entry_payload(reason=" ".join(secret_markers))

    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=lambda *args, **kwargs: SimpleNamespace(
            payload=v1_payload,
            status="是策略",
            error_message=None,
        ),
        v2_runner=lambda *args, **kwargs: SimpleNamespace(
            succeeded=True,
            parsed_result=_parsed_entry_v2(
                observed_text=" ".join(secret_markers)
            ),
            error_code=None,
            raw_response=" ".join(secret_markers),
        ),
        clock=_SequenceClock(0.0, 0.1, 0.1, 0.2, 0.2, 0.201),
    )

    assert result.passed is True
    assert tuple(
        sorted(path.name for path in inputs.artifact_dir.iterdir())
    ) == _REPLAY_ARTIFACT_NAMES
    comparisons_json = (inputs.artifact_dir / "comparisons.json").read_text(
        encoding="utf-8"
    )
    comparisons = json.loads(comparisons_json)
    assert len(comparisons) == 1
    assert tuple(comparisons[0]) == _COMPARISON_ARTIFACT_FIELDS
    summary = json.loads(
        (inputs.artifact_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert tuple(summary) == _SUMMARY_ARTIFACT_FIELDS
    csv_header = (
        inputs.artifact_dir / "comparisons.csv"
    ).read_text(encoding="utf-8").splitlines()[0]
    assert tuple(csv_header.split(",")) == _COMPARISON_ARTIFACT_FIELDS
    retained = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted(inputs.artifact_dir.iterdir())
    )
    for marker in secret_markers:
        assert marker not in retained


def test_replay_artifacts_use_atomic_replacement(tmp_path, monkeypatch):
    import telegram_kol_research.mimo_v2_replay as replay_module

    inputs, _ = _validated_replay_inputs(tmp_path)
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(replay_module.os, "replace", recording_replace)
    run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=lambda *args, **kwargs: SimpleNamespace(
            payload=_entry_payload(),
            status="是策略",
            error_message=None,
        ),
        v2_runner=lambda *args, **kwargs: SimpleNamespace(
            succeeded=True,
            parsed_result=_parsed_entry_v2(),
            error_code=None,
        ),
        clock=_SequenceClock(0.0, 0.1, 0.1, 0.2, 0.2, 0.201),
    )

    assert [source.name for source, _ in replacements] == [
        ".comparisons.json.tmp",
        ".comparisons.csv.tmp",
        ".summary.json.tmp",
    ]
    assert [destination.name for _, destination in replacements] == [
        "comparisons.json",
        "comparisons.csv",
        "summary.json",
    ]
    assert all(
        source.parent == inputs.artifact_dir
        and destination.parent == inputs.artifact_dir
        for source, destination in replacements
    )
    assert not list(inputs.artifact_dir.glob(".*.tmp"))


def test_replay_artifact_temp_files_use_exclusive_creation(tmp_path, monkeypatch):
    import telegram_kol_research.mimo_v2_replay as replay_module

    inputs, _ = _validated_replay_inputs(tmp_path)
    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=lambda *args, **kwargs: SimpleNamespace(
            payload=_entry_payload(),
            status="是策略",
            error_message=None,
        ),
        v2_runner=lambda *args, **kwargs: SimpleNamespace(
            succeeded=True,
            parsed_result=_parsed_entry_v2(),
            error_code=None,
        ),
        clock=_SequenceClock(0.0, 0.1, 0.1, 0.2, 0.2, 0.201),
    )
    real_open = os.open
    flags_seen: list[int] = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        flags_seen.append(flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(replay_module.os, "open", recording_open)

    replay_module.write_replay_artifacts(result)

    assert len(flags_seen) == 3
    assert all(flags & os.O_EXCL for flags in flags_seen)


def test_replay_artifacts_reject_nonfinite_values_before_replacement(tmp_path):
    import telegram_kol_research.mimo_v2_replay as replay_module

    inputs, _ = _validated_replay_inputs(tmp_path)
    result = run_mimo_v2_replay(
        inputs=inputs,
        ai_recognition_config_path=tmp_path / "ai.yaml",
        v1_runner=lambda *args, **kwargs: SimpleNamespace(
            payload=_entry_payload(),
            status="是策略",
            error_message=None,
        ),
        v2_runner=lambda *args, **kwargs: SimpleNamespace(
            succeeded=True,
            parsed_result=_parsed_entry_v2(),
            error_code=None,
        ),
        clock=_SequenceClock(0.0, 0.1, 0.1, 0.2, 0.2, 0.201),
    )
    before = {
        path.name: path.read_bytes()
        for path in inputs.artifact_dir.iterdir()
    }
    invalid_row = replace(
        result.comparisons[0],
        v1_duration_ms=math.nan,
    )
    invalid_result = replace(result, comparisons=(invalid_row,))

    with pytest.raises(MimoV2ReplayInputError, match="artifact_value_invalid"):
        replay_module.write_replay_artifacts(invalid_result)

    assert {
        path.name: path.read_bytes()
        for path in inputs.artifact_dir.iterdir()
    } == before


def test_replay_module_has_no_prohibited_writer_or_authority_imports():
    from telegram_kol_research import mimo_v2_replay as replay_module

    tree = ast.parse(Path(replay_module.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    prohibited = (
        "auto_trade",
        "deepcoin",
        "listener",
        "telegram_sync",
        "notification",
        "operator_bot",
        "authoritative_recognition",
    )

    assert not [
        module_name
        for module_name in imported
        if any(value in module_name for value in prohibited)
    ]
