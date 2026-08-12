import json
from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_evidence import (
    has_material_strategy_evidence,
    load_current_message_evidence,
    normalize_entry_strategy_fragments,
    normalize_mimo_evidence,
    persist_mimo_v2_message_evidence,
    save_message_evidence_version,
)
from telegram_kol_research.mimo_v2_contract import parse_mimo_v2_payload
from telegram_kol_research.models import MediaAsset, MimoRecognitionRun, RawMessage
from telegram_kol_research.prompt_defaults import DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT


@pytest.mark.parametrize(
    ("strategy", "expected_material"),
    [
        (None, False),
        ({"symbol": None, "side": None, "entry": None}, False),
        ({"symbol": "", "side": "  "}, False),
        ({"symbol": "BTC", "side": None}, True),
    ],
)
def test_material_strategy_evidence(strategy, expected_material):
    assert has_material_strategy_evidence(strategy) is expected_material


def test_non_strategy_prompt_prefers_null_strategy_evidence():
    assert '非策略消息优先输出 "strategy": null' in (
        DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    )


@pytest.mark.parametrize(
    ("entry_context", "expected_multiplier"),
    [
        (
            {
                "kind": "entry_preamble",
                "symbol": "btc",
                "side": "SHORT",
                "risk_multiplier": "0.5",
                "confidence": 0.96,
                "reason": "半仓操作",
            },
            "0.5",
        ),
        (
            {
                "kind": "entry_preamble",
                "symbol": "ETH",
                "side": "long",
                "risk_multiplier": "0.30",
                "confidence": 0.91,
                "reason": "30% 仓位",
            },
            "0.3",
        ),
    ],
)
def test_normalize_mimo_evidence_accepts_explicit_entry_preamble(
    entry_context, expected_multiplier
):
    *_, normalized = normalize_mimo_evidence(
        {
            "recognition_result": "非策略",
            "confidence": 0.9,
            "entry_context": entry_context,
        },
        input_kind="text",
        error_message=None,
    )

    assert normalized["entry_context"] == {
        "kind": "entry_preamble",
        "symbol": entry_context["symbol"].upper(),
        "side": entry_context["side"].lower(),
        "risk_multiplier": expected_multiplier,
        "confidence": entry_context["confidence"],
        "reason": entry_context["reason"],
    }
    assert "entry_context_rejection_reason" not in normalized


@pytest.mark.parametrize(
    "entry_context",
    [
        {"kind": "entry_preamble", "symbol": "BTC", "side": "short", "risk_multiplier": "0"},
        {"kind": "entry_preamble", "symbol": "BTC", "side": "short", "risk_multiplier": "1.1"},
        {"kind": "entry_preamble", "symbol": "", "side": "short", "risk_multiplier": "0.5"},
        {"kind": "entry_preamble", "symbol": "BTC", "side": "flat", "risk_multiplier": "0.5"},
        {"kind": "other", "symbol": "BTC", "side": "short", "risk_multiplier": "0.5"},
    ],
)
def test_malformed_entry_preamble_is_omitted_without_failing_recognition(entry_context):
    extraction_status, *_, normalized = normalize_mimo_evidence(
        {
            "recognition_result": "非策略",
            "confidence": 0.8,
            "entry_context": entry_context,
        },
        input_kind="text",
        error_message=None,
    )

    assert extraction_status == "completed"
    assert "entry_context" not in normalized
    assert normalized["entry_context_rejection_reason"] == "entry_context_invalid"


def test_normalize_entry_fragments_separates_total_risk_and_leg_allocation():
    fragments = normalize_entry_strategy_fragments(
        [
            {
                "kind": "risk_multiplier",
                "symbol": "btc",
                "side": "LONG",
                "risk_multiplier": "1.00",
                "confidence": 0.95,
                "reason": "正常仓位操作",
            },
            {
                "kind": "leg_allocation",
                "symbol": "BTC",
                "side": "long",
                "allocations": ["0.50", "0.5"],
                "confidence": 0.94,
                "reason": "两个点位各半仓",
            },
        ]
    )

    assert [fragment.to_dict() for fragment in fragments] == [
        {
            "kind": "risk_multiplier",
            "symbol": "BTC",
            "side": "long",
            "risk_multiplier": "1",
            "confidence": 0.95,
            "reason": "正常仓位操作",
        },
        {
            "kind": "leg_allocation",
            "symbol": "BTC",
            "side": "long",
            "allocations": ["0.5", "0.5"],
            "confidence": 0.94,
            "reason": "两个点位各半仓",
        },
    ]


def test_normalize_entry_fragments_accepts_numeric_sizing_and_supplemental_price():
    fragments = normalize_entry_strategy_fragments(
        [
            {
                "kind": "risk_multiplier",
                "symbol": "BTC",
                "side": "long",
                "risk_multiplier": "0.50",
                "confidence": 0.98,
                "reason": "轻仓入场，明确50%仓位",
            },
            {
                "kind": "supplemental_entry",
                "symbol": "BTC",
                "side": "long",
                "entry_price": "63400.0",
                "confidence": 0.92,
                "reason": "补仓63400附近",
            },
        ]
    )

    assert fragments[0].to_dict()["risk_multiplier"] == "0.5"
    assert fragments[1].to_dict()["entry_price"] == "63400"


@pytest.mark.parametrize(
    "fragment",
    [
        {"kind": "risk_multiplier", "symbol": "BTC", "side": "long", "risk_multiplier": True, "confidence": 0.9, "reason": "x"},
        {"kind": "risk_multiplier", "symbol": "BTC", "side": "long", "risk_multiplier": "0", "confidence": 0.9, "reason": "x"},
        {"kind": "risk_multiplier", "symbol": "BTC", "side": "long", "risk_multiplier": "1.1", "confidence": 0.9, "reason": "x"},
        {"kind": "leg_allocation", "symbol": "BTC", "side": "long", "allocations": ["0.5", "0.4"], "confidence": 0.9, "reason": "x"},
        {"kind": "supplemental_entry", "symbol": "BTC", "side": "long", "entry_price": "附近", "confidence": 0.9, "reason": "x"},
        {"kind": "supplemental_entry", "symbol": "BTC", "side": "flat", "entry_price": "63400", "confidence": 0.9, "reason": "x"},
    ],
)
def test_normalize_entry_fragments_rejects_unsafe_values(fragment):
    assert normalize_entry_strategy_fragments([fragment]) == ()


def test_normalize_mimo_evidence_keeps_valid_entry_fragments_and_rejects_bad_items():
    *_, normalized = normalize_mimo_evidence(
        {
            "recognition_result": "非策略",
            "confidence": 0.9,
            "entry_fragments": [
                {
                    "kind": "risk_multiplier",
                    "symbol": "BTC",
                    "side": "short",
                    "risk_multiplier": "0.5",
                    "confidence": 0.95,
                    "reason": "半仓操作",
                },
                {"kind": "supplemental_entry", "symbol": "", "side": "short"},
            ],
        },
        input_kind="text",
        error_message=None,
    )

    assert len(normalized["entry_fragments"]) == 1
    assert normalized["entry_fragments"][0]["risk_multiplier"] == "0.5"
    assert normalized["entry_fragments_rejected_count"] == 1


def test_legacy_entry_context_is_adapted_when_fragment_array_is_absent():
    *_, normalized = normalize_mimo_evidence(
        {
            "recognition_result": "非策略",
            "confidence": 0.9,
            "entry_context": {
                "kind": "entry_preamble",
                "symbol": "BTC",
                "side": "short",
                "risk_multiplier": "0.5",
                "confidence": 0.95,
                "reason": "半仓操作",
            },
        },
        input_kind="text",
        error_message=None,
    )

    assert normalized["entry_fragments"] == [
        {
            "kind": "risk_multiplier",
            "symbol": "BTC",
            "side": "short",
            "risk_multiplier": "0.5",
            "confidence": 0.95,
            "reason": "半仓操作",
        }
    ]


def _message(session_factory) -> RawMessage:
    with session_factory() as session:
        row = RawMessage(
            chat_id=-1001,
            message_id=1460,
            posted_at=datetime(2026, 7, 21, 9, 3, tzinfo=UTC),
            text="BTC 做多",
            archived_target_group=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _parsed_v2_result(*, asset_ids: tuple[int, int]):
    first_asset_id, second_asset_id = asset_ids
    return parse_mimo_v2_payload(
        {
            "contract_version": "mimo-authoritative-v2",
            "summary": "管理已有 ETH 空单并保留两张图片证据",
            "confidence": 0.94,
            "intents": [
                {
                    "intent_type": "position_management",
                    "action": {
                        "kind": "move_stop_to_protect",
                        "target": {"lifecycle_id": 790, "thread_id": 52},
                        "strategy": None,
                        "parameters": {"stop_loss": "1940"},
                    },
                    "reason": "消息明确要求移动止损到1940",
                    "confidence": 0.95,
                    "evidence_refs": [
                        "text:stop_loss",
                        f"image:{first_asset_id}:symbol",
                        f"image:{second_asset_id}:side",
                    ],
                }
            ],
            "evidence": {
                "text": {
                    "observed_text": "移动止损到1940",
                    "fields": {
                        "stop_loss": {
                            "value": "1940",
                            "source": "text",
                            "confidence": 0.98,
                        }
                    },
                },
                "images": [
                    {
                        "asset_id": first_asset_id,
                        "image_type": "position_screenshot",
                        "quality": "clear",
                        "observed_text": "ETHUSDT 永续，空",
                        "summary": "ETHUSDT空仓持仓截图",
                        "fields": {
                            "symbol": {
                                "value": "ETH",
                                "source": "image",
                                "confidence": 0.99,
                            }
                        },
                        "confidence": 0.97,
                    },
                    {
                        "asset_id": second_asset_id,
                        "image_type": "order_screenshot",
                        "quality": "cropped",
                        "observed_text": "空，止损1940",
                        "summary": "止损委托截图",
                        "fields": {
                            "side": {
                                "value": "short",
                                "source": "image",
                                "confidence": 0.96,
                            }
                        },
                        "confidence": 0.91,
                    },
                ],
                "conflicts": ["第二张图被裁剪，无法确认完整委托号"],
            },
        }
    )


def test_v2_evidence_preserves_each_image_and_links_authoritative_run(tmp_path):
    session_factory = create_session_factory(tmp_path / "v2-evidence.db")
    message = _message(session_factory)
    with session_factory() as session:
        first = MediaAsset(
            raw_message_id=message.id,
            telegram_file_id="photo-1",
            kind="photo",
            mime_type="image/jpeg",
        )
        second = MediaAsset(
            raw_message_id=message.id,
            telegram_file_id="photo-2",
            kind="photo",
            mime_type="image/png",
        )
        session.add_all([first, second])
        session.flush()
        run = MimoRecognitionRun(
            raw_message_id=message.id,
            run_kind="v2_authoritative",
            contract_version="mimo-authoritative-v2",
            model="mimo-v2.5",
            input_kind="text+image",
            input_fingerprint="sha256:analysis-input",
            prompt_versions_json='{"mimo_v2":12}',
            status="completed",
            attempt_count=1,
            selected_attempt_ordinal=1,
            became_authoritative=True,
            canonical_payload_fingerprint=None,
            projection_fingerprint="b" * 64,
            completed_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        )
        session.add(run)
        session.commit()
        asset_ids = (first.id, second.id)
        run_id = run.id

    saved = persist_mimo_v2_message_evidence(
        session_factory,
        raw_message_id=message.id,
        result=_parsed_v2_result(asset_ids=asset_ids),
        run_id=run_id,
        model="mimo-v2.5",
        prompt_versions={"mimo_v2": 12},
        media_root=tmp_path / "media",
    )

    text_evidence = json.loads(saved.text_evidence_json)
    image_evidence = json.loads(saved.image_evidence_json)
    normalized = json.loads(saved.normalized_evidence_json)
    assert text_evidence["observed_text"] == "移动止损到1940"
    assert [row["asset_id"] for row in image_evidence["images"]] == list(asset_ids)
    assert image_evidence["images"][0]["summary"] == "ETHUSDT空仓持仓截图"
    assert image_evidence["images"][1]["quality"] == "cropped"
    assert image_evidence["conflicts"] == [
        "第二张图被裁剪，无法确认完整委托号"
    ]
    assert normalized["contract_version"] == "mimo-authoritative-v2"
    assert normalized["intents"][0]["intent_type"] == "position_management"
    assert normalized["intents"][0]["evidence_refs"][1].startswith("image:")
    assert saved.mimo_recognition_run_id == run_id
    assert "data:image" not in saved.image_evidence_json


def test_v2_evidence_rejects_image_asset_from_another_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "v2-foreign-image.db")
    message = _message(session_factory)
    with session_factory() as session:
        other = RawMessage(
            chat_id=-1001,
            message_id=1461,
            posted_at=datetime(2026, 7, 21, 9, 4, tzinfo=UTC),
            text="other",
        )
        session.add(other)
        session.flush()
        first = MediaAsset(raw_message_id=message.id, kind="photo")
        foreign = MediaAsset(raw_message_id=other.id, kind="photo")
        session.add_all([first, foreign])
        session.commit()
        asset_ids = (first.id, foreign.id)

    with pytest.raises(ValueError, match="mimo_v2_image_asset_mismatch"):
        persist_mimo_v2_message_evidence(
            session_factory,
            raw_message_id=message.id,
            result=_parsed_v2_result(asset_ids=asset_ids),
            media_root=tmp_path / "media",
        )


def test_save_message_evidence_is_idempotent_for_same_input(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    message = _message(session_factory)

    first = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:one",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="completed",
        confidence=0.94,
        text_evidence={"symbol": {"value": "BTC", "source": "text"}},
        image_evidence={"images": []},
        normalized_evidence={"symbol": "BTC", "side": "long"},
    )
    repeated = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:one",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="completed",
        confidence=0.94,
        text_evidence={"symbol": {"value": "BTC", "source": "text"}},
        image_evidence={"images": []},
        normalized_evidence={"symbol": "BTC", "side": "long"},
    )

    assert first.id == repeated.id
    assert repeated.version == 1
    assert load_current_message_evidence(session_factory, message.id).id == first.id


def test_changed_input_supersedes_prior_evidence_version(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    message = _message(session_factory)

    first = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:one",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="completed",
        confidence=0.8,
        text_evidence={},
        image_evidence={"images": []},
        normalized_evidence={},
    )
    second = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:two",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="completed",
        confidence=0.9,
        text_evidence={"symbol": {"value": "BTC", "source": "text"}},
        image_evidence={"images": []},
        normalized_evidence={"symbol": "BTC"},
    )

    assert second.version == 2
    assert second.id != first.id
    with session_factory() as session:
        old = session.get(type(first), first.id)
        assert old.superseded_at is not None
    assert load_current_message_evidence(session_factory, message.id).id == second.id


def test_failed_extraction_can_recover_once_for_same_message_input(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovered.db")
    message = _message(session_factory)
    failed = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:same-input",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="failed",
        confidence=0.0,
        text_evidence={},
        image_evidence={"images": []},
        normalized_evidence={},
    )
    failed_id = failed.id

    recovered = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:same-input",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="completed",
        confidence=0.91,
        text_evidence={"observed_text": "BTC 做多"},
        image_evidence={"images": [{"asset_id": 1}]},
        normalized_evidence={"recognition_result": "是策略"},
    )

    assert recovered.id == failed_id
    assert recovered.version == 1
    assert recovered.extraction_status == "completed"
    assert recovered.confidence == 0.91


def test_concurrent_same_input_recovery_keeps_one_completed_winner(tmp_path):
    session_factory = create_session_factory(tmp_path / "concurrent-recovery.db")
    message = _message(session_factory)
    failed = save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:concurrent",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="failed",
        confidence=0.0,
        text_evidence={},
        image_evidence={"images": []},
        normalized_evidence={},
    )
    failed_id = failed.id

    def recover(label: str):
        return save_message_evidence_version(
            session_factory,
            raw_message_id=message.id,
            input_fingerprint="sha256:concurrent",
            model="mimo-v2.5",
            prompt_versions={"evidence": "v1"},
            extraction_status="completed",
            confidence=0.9,
            text_evidence={"observed_text": label},
            image_evidence={"images": []},
            normalized_evidence={"winner": label},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        recovered = list(executor.map(recover, ["first", "second"]))

    assert {row.id for row in recovered} == {failed_id}
    with session_factory() as session:
        rows = session.query(type(failed)).all()
        assert len(rows) == 1
        assert rows[0].extraction_status == "completed"
        assert rows[0].normalized_evidence_json in {
            '{"winner":"first"}',
            '{"winner":"second"}',
        }
