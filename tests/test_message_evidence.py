from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json

import pytest

import telegram_kol_research.message_evidence as evidence_module
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_evidence import (
    build_current_message_input_fingerprint,
    claim_message_evidence_extraction,
    has_material_strategy_evidence,
    load_current_message_evidence,
    normalize_entry_strategy_fragments,
    normalize_mimo_evidence,
    save_message_evidence_version,
)
from telegram_kol_research.mimo_v2_contract import parse_mimo_v2_payload
from telegram_kol_research.mimo_v2_execution_adapter import (
    adapt_mimo_v2_to_current_payload,
)
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


def _parsed_v2_result(
    *,
    asset_ids: tuple[int, int],
    observed_text: str = "移动止损到1940",
    confidence: float = 0.94,
):
    first_asset_id, second_asset_id = asset_ids
    return parse_mimo_v2_payload(
        {
            "contract_version": "mimo-authoritative-v2",
            "summary": "管理已有 ETH 空单并保留两张图片证据",
            "confidence": confidence,
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
                    "observed_text": observed_text,
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


def _seed_v2_evidence_inputs(tmp_path):
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
        asset_ids = (int(first.id), int(second.id))
        result = _parsed_v2_result(asset_ids=asset_ids)
        adapted = adapt_mimo_v2_to_current_payload(result)
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
            canonical_payload_fingerprint=adapted.canonical_v2_fingerprint,
            projection_fingerprint=adapted.projection_fingerprint,
            completed_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        )
        session.add(run)
        session.commit()
        return session_factory, message, asset_ids, result, int(run.id)


def test_v2_evidence_preserves_sources_and_links_completed_authoritative_run(
    tmp_path,
):
    factory, message, asset_ids, result, run_id = _seed_v2_evidence_inputs(
        tmp_path
    )

    saved = evidence_module.persist_mimo_v2_message_evidence(
        factory,
        raw_message_id=message.id,
        result=result,
        run_id=run_id,
        model="mimo-v2.5",
        prompt_versions={"mimo_v2": 12},
        media_root=tmp_path / "media",
    )

    text_evidence = json.loads(saved.text_evidence_json)
    image_evidence = json.loads(saved.image_evidence_json)
    normalized = json.loads(saved.normalized_evidence_json)
    assert text_evidence["observed_text"] == "移动止损到1940"
    assert [row["asset_id"] for row in image_evidence["images"]] == list(
        asset_ids
    )
    assert image_evidence["images"][0]["summary"] == "ETHUSDT空仓持仓截图"
    assert image_evidence["images"][1]["quality"] == "cropped"
    assert image_evidence["conflicts"] == [
        "第二张图被裁剪，无法确认完整委托号"
    ]
    assert normalized["contract_version"] == "mimo-authoritative-v2"
    assert normalized["intents"][0]["intent_type"] == "position_management"
    assert saved.mimo_recognition_run_id == run_id
    assert "data:image" not in saved.image_evidence_json


def test_v2_evidence_preserves_zero_confidence(tmp_path):
    factory, message, asset_ids, _, run_id = _seed_v2_evidence_inputs(tmp_path)
    result = _parsed_v2_result(asset_ids=asset_ids, confidence=0.0)
    adapted = adapt_mimo_v2_to_current_payload(result)
    with factory() as session:
        run = session.get(MimoRecognitionRun, run_id)
        run.canonical_payload_fingerprint = adapted.canonical_v2_fingerprint
        run.projection_fingerprint = adapted.projection_fingerprint
        session.commit()

    saved = evidence_module.persist_mimo_v2_message_evidence(
        factory,
        raw_message_id=message.id,
        result=result,
        run_id=run_id,
        model="mimo-v2.5",
        prompt_versions={"mimo_v2": 12},
        media_root=tmp_path / "media",
    )

    assert saved.confidence == 0.0
    assert json.loads(saved.normalized_evidence_json)["confidence"] == 0.0


def test_v2_evidence_rejects_image_asset_from_another_message(tmp_path):
    factory, message, asset_ids, _, run_id = _seed_v2_evidence_inputs(tmp_path)
    with factory() as session:
        other = RawMessage(chat_id=-1001, message_id=1461, text="other")
        session.add(other)
        session.flush()
        foreign = MediaAsset(raw_message_id=other.id, kind="photo")
        session.add(foreign)
        session.commit()
        result = _parsed_v2_result(asset_ids=(asset_ids[0], int(foreign.id)))
        adapted = adapt_mimo_v2_to_current_payload(result)
        run = session.get(MimoRecognitionRun, run_id)
        run.canonical_payload_fingerprint = adapted.canonical_v2_fingerprint
        run.projection_fingerprint = adapted.projection_fingerprint
        session.commit()

    with pytest.raises(ValueError, match="mimo_v2_image_asset_mismatch"):
        evidence_module.persist_mimo_v2_message_evidence(
            factory,
            raw_message_id=message.id,
            result=result,
            run_id=run_id,
            model="mimo-v2.5",
            prompt_versions={"mimo_v2": 12},
            media_root=tmp_path / "media",
        )


@pytest.mark.parametrize(
    ("status", "became_authoritative"),
    (("running", False), ("completed", False)),
)
def test_v2_evidence_rejects_unselected_or_running_run(
    tmp_path,
    status,
    became_authoritative,
):
    factory, message, _, result, run_id = _seed_v2_evidence_inputs(tmp_path)
    with factory() as session:
        run = session.get(MimoRecognitionRun, run_id)
        run.status = status
        run.became_authoritative = became_authoritative
        session.commit()

    with pytest.raises(ValueError, match="mimo_v2_run_not_authoritative"):
        evidence_module.persist_mimo_v2_message_evidence(
            factory,
            raw_message_id=message.id,
            result=result,
            run_id=run_id,
            model="mimo-v2.5",
            prompt_versions={"mimo_v2": 12},
            media_root=tmp_path / "media",
        )


def test_v2_evidence_rejects_canonical_fingerprint_mismatch(tmp_path):
    factory, message, _, result, run_id = _seed_v2_evidence_inputs(tmp_path)
    with factory() as session:
        run = session.get(MimoRecognitionRun, run_id)
        run.canonical_payload_fingerprint = "0" * 64
        session.commit()

    with pytest.raises(ValueError, match="mimo_v2_run_payload_mismatch"):
        evidence_module.persist_mimo_v2_message_evidence(
            factory,
            raw_message_id=message.id,
            result=result,
            run_id=run_id,
            model="mimo-v2.5",
            prompt_versions={"mimo_v2": 12},
            media_root=tmp_path / "media",
        )


def test_v2_evidence_rejects_projection_fingerprint_mismatch(tmp_path):
    factory, message, _, result, run_id = _seed_v2_evidence_inputs(tmp_path)
    with factory() as session:
        run = session.get(MimoRecognitionRun, run_id)
        run.projection_fingerprint = "0" * 64
        session.commit()

    with pytest.raises(ValueError, match="mimo_v2_run_projection_mismatch"):
        evidence_module.persist_mimo_v2_message_evidence(
            factory,
            raw_message_id=message.id,
            result=result,
            run_id=run_id,
            model="mimo-v2.5",
            prompt_versions={"mimo_v2": 12},
            media_root=tmp_path / "media",
        )


@pytest.mark.parametrize(
    ("model", "prompt_versions"),
    (
        ("other-model", {"mimo_v2": 12}),
        ("mimo-v2.5", {"mimo_v2": 13}),
    ),
)
def test_v2_evidence_rejects_run_provenance_mismatch(
    tmp_path,
    model,
    prompt_versions,
):
    factory, message, _, result, run_id = _seed_v2_evidence_inputs(tmp_path)

    with pytest.raises(ValueError, match="mimo_v2_run_provenance_mismatch"):
        evidence_module.persist_mimo_v2_message_evidence(
            factory,
            raw_message_id=message.id,
            result=result,
            run_id=run_id,
            model=model,
            prompt_versions=prompt_versions,
            media_root=tmp_path / "media",
        )


def test_v2_evidence_direct_persist_rejects_stale_expected_input(tmp_path):
    factory, message, _, result, run_id = _seed_v2_evidence_inputs(tmp_path)

    with pytest.raises(ValueError, match="mimo_v2_message_input_mismatch"):
        evidence_module.persist_mimo_v2_message_evidence(
            factory,
            raw_message_id=message.id,
            result=result,
            run_id=run_id,
            model="mimo-v2.5",
            prompt_versions={"mimo_v2": 12},
            media_root=tmp_path / "media",
            expected_input_fingerprint="sha256:stale",
        )


def test_v2_evidence_rejects_embedded_image_bytes(tmp_path):
    factory, message, asset_ids, _, run_id = _seed_v2_evidence_inputs(tmp_path)
    result = _parsed_v2_result(
        asset_ids=asset_ids,
        observed_text="data:image/png;base64,AAAA",
    )
    adapted = adapt_mimo_v2_to_current_payload(result)
    with factory() as session:
        run = session.get(MimoRecognitionRun, run_id)
        run.canonical_payload_fingerprint = adapted.canonical_v2_fingerprint
        run.projection_fingerprint = adapted.projection_fingerprint
        session.commit()

    with pytest.raises(ValueError, match="mimo_v2_evidence_embedded_image_bytes"):
        evidence_module.persist_mimo_v2_message_evidence(
            factory,
            raw_message_id=message.id,
            result=result,
            run_id=run_id,
            model="mimo-v2.5",
            prompt_versions={"mimo_v2": 12},
            media_root=tmp_path / "media",
        )


def test_v2_evidence_finalize_refuses_changed_message_input(tmp_path):
    factory, message, _, result, run_id = _seed_v2_evidence_inputs(tmp_path)
    media_root = tmp_path / "media"
    fingerprint = build_current_message_input_fingerprint(
        factory,
        message.id,
        media_root=media_root,
    )
    claim_token = claim_message_evidence_extraction(
        factory,
        raw_message_id=message.id,
        input_fingerprint=fingerprint,
    )
    with factory() as session:
        current = session.get(RawMessage, message.id)
        current.text = "edited while MiMo was running"
        session.commit()

    saved = evidence_module.finalize_claimed_mimo_v2_message_evidence(
        factory,
        raw_message_id=message.id,
        claim_token=claim_token,
        expected_input_fingerprint=fingerprint,
        result=result,
        run_id=run_id,
        model="mimo-v2.5",
        prompt_versions={"mimo_v2": 12},
        media_root=media_root,
    )

    assert saved is None
    assert load_current_message_evidence(factory, message.id) is None


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
