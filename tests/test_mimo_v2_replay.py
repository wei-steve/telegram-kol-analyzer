from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.mimo_v2_contract import parse_mimo_v2_payload
from telegram_kol_research.mimo_v2_execution_adapter import (
    adapt_mimo_v2_to_current_payload,
)
from telegram_kol_research.models import RawMessage
from telegram_kol_research.mimo_v2_replay import (
    evaluate_replay_performance,
    run_mimo_v2_replay,
)


def _seed_source_database(path: Path, *, count: int = 2) -> list[int]:
    factory = create_session_factory(path)
    with factory() as session:
        rows = [
            RawMessage(
                chat_id=900,
                message_id=index,
                text=f"PRIVATE SOURCE MESSAGE {index}",
            )
            for index in range(1, count + 1)
        ]
        session.add_all(rows)
        session.commit()
        return [int(row.id) for row in rows]


def _v1_payload(*, symbol: str = "BTC") -> dict:
    return {
        "instructions": [
            {
                "kind": "entry",
                "confidence": 0.95,
                "reason": "legacy wording",
                "strategy": {
                    "symbol": symbol,
                    "side": "long",
                    "entry": "60000",
                    "stop_loss": "59000",
                    "take_profit": "62000",
                    "leverage": "10",
                    "order_type": "limit",
                },
                "target": {"lifecycle_id": None, "thread_id": None},
                "parameters": {},
            }
        ],
        "recognition_result": "是策略",
        "reason": "legacy wording",
        "strategy": {
            "symbol": symbol,
            "side": "long",
            "entry": "60000",
            "stop_loss": "59000",
            "take_profit": "62000",
            "leverage": "10",
            "order_type": "limit",
        },
        "lifecycle_event": {
            "event_type": "none",
            "confidence": 0.0,
            "reason": "none",
        },
        "input_reading": {
            "observed_text": "PRIVATE SOURCE MESSAGE",
            "image_quality": "none",
        },
        "confidence": 0.95,
    }


def _v2_result(*, symbol: str = "BTC"):
    parsed = parse_mimo_v2_payload(
        {
            "contract_version": "mimo-authoritative-v2",
            "summary": "structured entry",
            "confidence": 0.95,
            "intents": [
                {
                    "intent_type": "new_strategy",
                    "action": {
                        "kind": "entry",
                        "target": {"lifecycle_id": None, "thread_id": None},
                        "strategy": {
                            "symbol": symbol,
                            "side": "long",
                            "entry": "60000",
                            "stop_loss": "59000",
                            "take_profit": "62000",
                            "leverage": "10",
                            "order_type": "limit",
                        },
                        "parameters": {},
                    },
                    "reason": "structured wording",
                    "confidence": 0.95,
                    "evidence_refs": ["text:observed_text"],
                }
            ],
            "evidence": {
                "text": {
                    "observed_text": "PRIVATE SOURCE MESSAGE",
                    "fields": {},
                },
                "images": [],
                "conflicts": [],
            },
        }
    )
    return SimpleNamespace(
        error_code=None,
        error_message=None,
        parsed_result=parsed,
        adapted_result=adapt_mimo_v2_to_current_payload(parsed),
        replay_duration_ms=110.0,
    )


def _v1_runner(**kwargs):
    return SimpleNamespace(
        error_message=None,
        status="是策略",
        payload=_v1_payload(),
        replay_duration_ms=100.0,
    )


def _v2_runner(**kwargs):
    return _v2_result()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_replay_uses_isolated_database_and_writes_redacted_artifacts(tmp_path):
    source = tmp_path / "production.db"
    message_ids = _seed_source_database(source)
    before = _sha256(source)
    artifact_dir = tmp_path / "replay-artifacts"
    isolated_database_paths: list[Path] = []

    def v1_runner(**kwargs):
        isolated_database_paths.append(
            Path(kwargs["session_factory"].kw["bind"].url.database)
        )
        return _v1_runner(**kwargs)

    result = run_mimo_v2_replay(
        source_database=source,
        artifact_dir=artifact_dir,
        raw_message_ids=[message_ids[0]],
        v1_runner=v1_runner,
        v2_runner=_v2_runner,
    )

    assert result.processed == 1
    assert result.unsafe_mismatches == 0
    assert result.production_writes == 0
    assert result.notifications_sent == 0
    assert result.passed is True
    assert _sha256(source) == before
    assert isolated_database_paths
    assert all(path != source for path in isolated_database_paths)
    assert all(not path.exists() for path in isolated_database_paths)

    summary_text = (artifact_dir / "summary.json").read_text(encoding="utf-8")
    comparison_text = (artifact_dir / "comparisons.csv").read_text(
        encoding="utf-8"
    )
    assert "PRIVATE SOURCE MESSAGE" not in summary_text
    assert "PRIVATE SOURCE MESSAGE" not in comparison_text
    assert "api_key" not in summary_text.lower()
    assert json.loads(summary_text)["passed"] is True


def test_replay_rejects_unbounded_selection_and_nonempty_artifact_dir(tmp_path):
    source = tmp_path / "production.db"
    message_ids = _seed_source_database(source, count=3)

    with pytest.raises(ValueError, match="max_messages"):
        run_mimo_v2_replay(
            source_database=source,
            artifact_dir=tmp_path / "too-many",
            raw_message_ids=message_ids,
            max_messages=2,
            v1_runner=_v1_runner,
            v2_runner=_v2_runner,
        )

    artifact_dir = tmp_path / "not-empty"
    artifact_dir.mkdir()
    (artifact_dir / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact_dir"):
        run_mimo_v2_replay(
            source_database=source,
            artifact_dir=artifact_dir,
            raw_message_ids=[message_ids[0]],
            v1_runner=_v1_runner,
            v2_runner=_v2_runner,
        )


def test_replay_classifies_execution_drift_as_unsafe(tmp_path):
    source = tmp_path / "production.db"
    [message_id] = _seed_source_database(source, count=1)

    result = run_mimo_v2_replay(
        source_database=source,
        artifact_dir=tmp_path / "artifacts",
        raw_message_ids=[message_id],
        v1_runner=_v1_runner,
        v2_runner=lambda **kwargs: _v2_result(symbol="ETH"),
    )

    assert result.passed is False
    assert result.unsafe_mismatches == 1
    assert result.comparisons[0].classification == "unsafe_mismatch"


def test_replay_classifies_model_failures_without_leaking_error_text(tmp_path):
    source = tmp_path / "production.db"
    [message_id] = _seed_source_database(source, count=1)

    result = run_mimo_v2_replay(
        source_database=source,
        artifact_dir=tmp_path / "artifacts",
        raw_message_ids=[message_id],
        v1_runner=_v1_runner,
        v2_runner=lambda **kwargs: SimpleNamespace(
            error_code="provider_http_error",
            error_message="Bearer SECRET_TOKEN",
            parsed_result=None,
            adapted_result=None,
            replay_duration_ms=100.0,
        ),
    )

    assert result.passed is False
    assert result.comparisons[0].classification == "v2_failed"
    serialized = (tmp_path / "artifacts" / "summary.json").read_text(
        encoding="utf-8"
    )
    assert "SECRET_TOKEN" not in serialized


def test_replay_performance_uses_p95_and_enforces_both_gates():
    passing = evaluate_replay_performance(
        v1_duration_ms=[100.0, 100.0, 100.0],
        v2_duration_ms=[110.0, 110.0, 110.0],
        adapter_duration_ms=[1.0, 2.0, 3.0],
    )
    slow_adapter = evaluate_replay_performance(
        v1_duration_ms=[100.0],
        v2_duration_ms=[100.0],
        adapter_duration_ms=[50.0],
    )
    slow_v2 = evaluate_replay_performance(
        v1_duration_ms=[100.0],
        v2_duration_ms=[116.0],
        adapter_duration_ms=[1.0],
    )

    assert passing.passed is True
    assert passing.v1_p95_ms == 100.0
    assert passing.v2_p95_ms == 110.0
    assert passing.adapter_p95_ms == 3.0
    assert slow_adapter.passed is False
    assert "adapter_p95_at_or_above_50ms" in slow_adapter.failure_reasons
    assert slow_v2.passed is False
    assert "v2_p95_above_115_percent_of_v1" in slow_v2.failure_reasons
